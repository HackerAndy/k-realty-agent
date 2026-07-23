# Template candidate: generic (tier 1, pattern) — a local MCP front-end over the
# harness's core. See agent-harness-template/docs/promotion-log.md.
"""Local MCP server — the harness's second front-end, alongside the TUI.

Runs as a local stdio subprocess (e.g. launched by Claude Desktop) and exposes
the harness's capabilities as MCP tools, so you can drive ingestion / scraping /
reporting from a Claude host (chat or a Live Artifact dashboard) instead of the
terminal. Because it runs LOCALLY, it keeps full access to .secrets/, data/, the
browser profiles, and Playwright — nothing about the local, faithful design changes.

The tool logic lives in interfaces/mcp_tools.py (unit-tested); this file only
wires those functions into the MCP transport and starts it.

    poetry run agent-mcp    # (stdio; configure it in claude_desktop_config.json)
    npx @modelcontextprotocol/inspector /Users/drew/Documents/code/HackerAndy/k-realty-agent/.venv/bin/python -m interfaces.mcp_server    
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interfaces import mcp_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
mcp = FastMCP("k-realty")

for _tool in mcp_tools.ALL_TOOLS:
    mcp.tool()(_tool)


def main() -> None:
    """Load local secrets + the configured LLM provider, then serve over stdio.
    Pins cwd to the repo root so the harness's relative paths (data/, .secrets/,
    services.yaml) resolve no matter where Claude Desktop launches the server."""
    os.chdir(REPO_ROOT)
    from core.tools import llm_provider
    from core.tools.credential_store import ensure_master_key

    ensure_master_key()
    llm_provider.load_into_env()
    mcp.run()
    # mcp.run(transport="streamable-http")   # uvicorn execution for true HTTP transport

if __name__ == "__main__":
    main()
