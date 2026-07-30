"""MCP entrypoint, stdio transport. Registers the four spend-inspection tools."""

from __future__ import annotations

from datetime import datetime

from mcp.server.mcpserver import MCPServer

from aispend.mcp_server.tools.budget_check import check_budget as _check_budget
from aispend.mcp_server.tools.efficiency_flags import (
    get_efficiency_flags as _get_efficiency_flags,
)
from aispend.mcp_server.tools.expensive_requests import (
    get_expensive_requests as _get_expensive_requests,
)
from aispend.mcp_server.tools.spend_summary import get_spend_summary as _get_spend_summary
from aispend.storage.db import close_pool

mcp = MCPServer("aispend")


@mcp.tool()
def get_spend_summary(since: datetime, until: datetime, group_by: str | None = None) -> dict:
    """Total AI spend between `since` and `until`.

    `group_by` may be "model", "source_tool", "day", or None for a plain total.
    """
    return _get_spend_summary(since, until, group_by)


@mcp.tool()
def get_expensive_requests(
    limit: int, since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    """The `limit` most expensive requests, optionally restricted to a time window."""
    return _get_expensive_requests(limit, since, until)


@mcp.tool()
def get_efficiency_flags(
    since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    """Advisory report of requests that probably didn't need an Opus-tier model."""
    return _get_efficiency_flags(since, until)


@mcp.tool()
def check_budget(
    threshold: float, since: datetime | None = None, until: datetime | None = None
) -> dict:
    """Reports whether total spend in the window is over or under `threshold`."""
    return _check_budget(threshold, since, until)


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        # Close the pool while the interpreter is still alive. Left to garbage
        # collection at shutdown, psycopg_pool's finalizer tries to join its
        # worker threads and raises, which on a stdio transport means a stray
        # traceback on stderr every time the server exits.
        close_pool()


if __name__ == "__main__":
    main()
