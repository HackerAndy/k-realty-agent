# Template candidate: generic (tier 1, pattern) — a REST/HTTP front-end over the
# harness's core, for a standalone browser GUI. See promotion-log.md.
"""REST transport — the third front-end, for a browser GUI that runs WITHOUT Claude.

Exposes the same tool functions as interfaces/mcp_tools.py (the MCP layer uses),
over plain HTTP, plus serves the static dashboard. One generic endpoint —
`POST /api/tool/{name}` — mirrors MCP's "call a tool by name with args", so the
browser's data adapter is a 1:1 swap with the Live-Artifact one:

    standalone : callTool(name, args) -> fetch('/api/tool/'+name)
    in Claude  : callTool(name, args) -> window.claude.callMcpTool(name, args)

Same GUI, same tools, same core/ — only the transport differs.

    poetry run agent-web    # serves http://127.0.0.1:8765
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from interfaces import mcp_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"

# Same tool surface the MCP server exposes — one source of truth.
TOOLS = {fn.__name__: fn for fn in mcp_tools.ALL_TOOLS}

app = FastAPI(title="k-realty")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/tool/{name}")
def call_tool(name: str, args: dict = Body(default={})):
    """Dispatch to a tool function by name — the REST mirror of an MCP tool call."""
    fn = TOOLS.get(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool '{name}'. Known: {sorted(TOOLS)}")
    try:
        return fn(**(args or {}))
    except mcp_tools.ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"Bad arguments for '{name}': {exc}") from exc


def main() -> None:
    """Pin cwd to the repo root (relative paths), load secrets + the LLM provider,
    then serve on localhost. Bind 0.0.0.0 only if you deliberately want it on the
    network (e.g. served from a Pi) — it exposes financial data, so keep it local
    by default."""
    os.chdir(REPO_ROOT)
    from core.tools import llm_provider
    from core.tools.credential_store import ensure_master_key

    ensure_master_key()
    llm_provider.load_into_env()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
