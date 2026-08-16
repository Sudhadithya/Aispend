"""Measures the latency the proxy adds: identical load, sent direct vs. proxied.

The only number that matters for a proxy is what it costs the caller, so this
sends the same request the same way to two places — the stub upstream itself,
and the proxy in front of that stub — and reports the difference at p50/p95/p99.

Three details that make the delta trustworthy:

- **The driver is a raw asyncio HTTP/1.1 client, not httpx.** httpx costs ~4ms
  per request on this machine, against a stub that answers in 0.6ms; a client
  slower than the server under test measures the client. Raw sockets put the
  driver's floor an order of magnitude below the signal. (The *proxy* still
  uses httpx to reach upstream — that cost is genuinely the proxy's, and is
  exactly what we want counted.)
- The phases alternate over several rounds and their samples are pooled, so a
  background process that wakes up mid-run lands on both phases rather than
  taxing whichever one happened to go second.
- Concurrency is closed-loop: N connections each hold exactly one request in
  flight, so "50 concurrent" means 50 in flight, not 50 issued per second
  regardless of whether the last batch finished.

Run (stub on 8788, proxy on 8787 pointed at the stub):
    .venv/Scripts/python.exe bench/bench.py --requests 4000 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

PAYLOAD = {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "benchmark"}],
}


def build_request(host: str, port: int, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return (
        b"POST /v1/messages HTTP/1.1\r\n"
        b"Host: " + f"{host}:{port}".encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )


async def read_response(reader: asyncio.StreamReader) -> int:
    """Consumes one HTTP/1.1 response and returns its status code.

    Handles both framings we see: the stub answers with Content-Length, while
    the proxy's StreamingResponse for SSE is chunked.
    """
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])

    length: int | None = None
    chunked = False
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"content-length":
            length = int(value)
        elif name == b"transfer-encoding" and b"chunked" in value.lower():
            chunked = True

    if chunked:
        while True:
            size = int((await reader.readuntil(b"\r\n")).strip(), 16)
            await reader.readexactly(size + 2)  # chunk body plus its trailing CRLF
            if size == 0:
                break
    elif length:
        await reader.readexactly(length)
    return status


async def _connection(host: str, port: int, request: bytes, n: int):
    """Returns (latencies, non_200_count).

    A non-200 is counted and its latency dropped rather than raised on: under
    load the proxy does occasionally 502, and losing an entire run to one blip
    hides the very thing worth reporting. The count is printed alongside the
    percentiles so an error-laden run can't be mistaken for a clean one.
    """
    reader, writer = await asyncio.open_connection(host, port)
    samples: list[float] = []
    errors = 0
    try:
        for _ in range(n):
            start = time.perf_counter()
            writer.write(request)
            await writer.drain()
            status = await read_response(reader)
            if status == 200:
                samples.append((time.perf_counter() - start) * 1000)
            else:
                errors += 1
    finally:
        writer.close()
    return samples, errors


async def _phase(target: tuple[str, int], payload: dict, *, requests: int, concurrency: int):
    host, port = target
    request = build_request(host, port, payload)
    per_connection, remainder = divmod(requests, concurrency)
    results = await asyncio.gather(
        *(
            _connection(host, port, request, per_connection + (1 if i < remainder else 0))
            for i in range(concurrency)
        )
    )
    samples = [sample for connection_samples, _ in results for sample in connection_samples]
    return samples, sum(errors for _, errors in results)


def percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile. `sorted_samples` must already be sorted."""
    rank = max(1, min(len(sorted_samples), round(q / 100 * len(sorted_samples))))
    return sorted_samples[rank - 1]


def _summarise(name: str, samples: list[float], wall_seconds: float, errors: int) -> dict:
    ordered = sorted(samples)
    return {
        "phase": name,
        "n": len(ordered),
        "errors": errors,
        "p50": percentile(ordered, 50),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "mean": sum(ordered) / len(ordered),
        "rps": len(ordered) / wall_seconds,
    }


def _parse_target(value: str) -> tuple[str, int]:
    host, _, port = value.partition(":")
    return host, int(port)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # argparse skips `type` for defaults, so the defaults are already parsed.
    parser.add_argument("--direct", type=_parse_target, default=("127.0.0.1", 8788))
    parser.add_argument("--proxy", type=_parse_target, default=("127.0.0.1", 8787))
    parser.add_argument("--requests", type=int, default=4000, help="per phase, across all rounds")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=4, help="alternations between the phases")
    parser.add_argument("--warmup", type=int, default=200, help="discarded requests per phase")
    parser.add_argument("--stream", action="store_true", help="exercise the SSE path")
    parser.add_argument("--label", default="", help="tag written into the JSON output")
    parser.add_argument("--out", help="write raw results here as JSON")
    # Naming the two targets makes the same interleaving usable for the other
    # comparison that matters: two proxy variants against each other.
    parser.add_argument("--direct-name", default="direct")
    parser.add_argument("--proxy-name", default="proxied")
    args = parser.parse_args()

    payload = PAYLOAD | ({"stream": True} if args.stream else {})
    targets = {args.direct_name: args.direct, args.proxy_name: args.proxy}

    for target in targets.values():
        await _phase(target, payload, requests=args.warmup, concurrency=args.concurrency)

    samples: dict[str, list[float]] = {name: [] for name in targets}
    elapsed: dict[str, float] = dict.fromkeys(targets, 0.0)
    errors: dict[str, int] = dict.fromkeys(targets, 0)
    per_round = args.requests // args.rounds
    for _ in range(args.rounds):
        for name, target in targets.items():
            start = time.perf_counter()
            round_samples, round_errors = await _phase(
                target, payload, requests=per_round, concurrency=args.concurrency
            )
            samples[name] += round_samples
            errors[name] += round_errors
            elapsed[name] += time.perf_counter() - start

    rows = [_summarise(name, samples[name], elapsed[name], errors[name]) for name in targets]
    direct, proxied = rows

    print(
        f"\n{args.label or 'proxy overhead'} — concurrency {args.concurrency}, "
        f"{args.rounds} rounds, {'streaming' if args.stream else 'non-streaming'}"
    )
    print(f"{'phase':<10}{'n':>7}{'err':>5}{'p50':>10}{'p95':>10}{'p99':>10}{'mean':>10}{'rps':>10}")
    for row in rows:
        cells = "".join(f"{row[k]:>9.2f}m" for k in ("p50", "p95", "p99", "mean"))
        print(f"{row['phase']:<10}{row['n']:>7}{row['errors']:>5}{cells}{row['rps']:>10.0f}")
    added = "".join(f"{proxied[k] - direct[k]:>+9.2f}m" for k in ("p50", "p95", "p99", "mean"))
    print(f"{'added':<10}{'':>7}{'':>5}{added}")
    ratios = "".join(f"{proxied[k] / direct[k]:>9.2f}x" for k in ("p50", "p95", "p99", "mean"))
    print(f"{'ratio':<10}{'':>7}{'':>5}{ratios}")

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(
                {"label": args.label, "args": {**vars(args), "direct": args.direct,
                                               "proxy": args.proxy},
                 "summary": rows, "samples": samples},
                handle,
            )


if __name__ == "__main__":
    asyncio.run(main())
