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
import tempfile
import time

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
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
        detail = exc.args[0] if exc.args else str(exc)
        raise HTTPException(status_code=400, detail=detail) from exc
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"Bad arguments for '{name}': {exc}") from exc


@app.post("/api/upload_ingest/{source_key}")
async def upload_ingest(source_key: str, file: UploadFile = File(...)):
    """Upload a source document and ingest it for the given source key."""
    suffix = Path(file.filename or "uploaded").suffix
    tmp_path: Path | None = None
    steps: list[dict] = []
    result: dict | None = None
    error: HTTPException | None = None

    save_started = time.perf_counter()
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            chunk = await file.read()
            tmp.write(chunk)
            tmp_path = Path(tmp.name)
        steps.append({
            "key": "save_temp_file",
            "label": "Save temp file",
            "status": "success",
            "duration_ms": int((time.perf_counter() - save_started) * 1000),
            "details": {"bytes": len(chunk), "suffix": suffix},
        })
    except Exception as exc:
        steps.append({
            "key": "save_temp_file",
            "label": "Save temp file",
            "status": "failed",
            "duration_ms": int((time.perf_counter() - save_started) * 1000),
            "error": str(exc),
        })
        error = HTTPException(status_code=500, detail={"message": f"Upload save failed: {exc}", "steps": steps})

    if error is None and tmp_path is not None:
        ingest_started = time.perf_counter()
        try:
            result = mcp_tools.ingest_document(source_key=source_key, path=str(tmp_path))
            steps.append({
                "key": "ingest_document",
                "label": "Parse and persist",
                "status": "success",
                "duration_ms": int((time.perf_counter() - ingest_started) * 1000),
                "details": {"count": result.get("count")},
            })
        except mcp_tools.ToolError as exc:
            steps.append({
                "key": "ingest_document",
                "label": "Parse and persist",
                "status": "failed",
                "duration_ms": int((time.perf_counter() - ingest_started) * 1000),
                "error": str(exc),
            })
            error = HTTPException(status_code=400, detail={"message": str(exc), "steps": steps})
        except Exception as exc:
            steps.append({
                "key": "ingest_document",
                "label": "Parse and persist",
                "status": "failed",
                "duration_ms": int((time.perf_counter() - ingest_started) * 1000),
                "error": str(exc),
            })
            error = HTTPException(status_code=500, detail={"message": str(exc), "steps": steps})

    cleanup_started = time.perf_counter()
    try:
        await file.close()
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        steps.append({
            "key": "cleanup_temp_file",
            "label": "Cleanup temp file",
            "status": "success",
            "duration_ms": int((time.perf_counter() - cleanup_started) * 1000),
        })
    except Exception as exc:
        steps.append({
            "key": "cleanup_temp_file",
            "label": "Cleanup temp file",
            "status": "failed",
            "duration_ms": int((time.perf_counter() - cleanup_started) * 1000),
            "error": str(exc),
        })
        if error is None:
            error = HTTPException(status_code=500, detail={"message": f"Cleanup failed: {exc}", "steps": steps})

    if error is not None:
        raise error

    return {**(result or {}), "steps": steps}


@app.post("/api/upload_sample/{source_key}")
async def upload_sample(source_key: str, file: UploadFile = File(...)):
    """Save a sample document for the agent to build a parser against.

    Unlike /api/upload_ingest this KEEPS the file: the build runs in a background
    process for minutes and re-reads the sample, and a revise pass needs it again.
    Lands in data/samples/ (gitignored, like all financial documents)."""
    suffix = Path(file.filename or "sample").suffix
    safe_key = "".join(c for c in source_key if c.isalnum() or c in "_-")
    if not safe_key:
        raise HTTPException(status_code=400, detail={"message": f"Invalid source key '{source_key}'."})

    samples_dir = REPO_ROOT / "data" / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    dest = samples_dir / f"{safe_key}-sample{suffix}"
    try:
        dest.write_bytes(await file.read())
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"message": f"Couldn't save the sample: {exc}"}) from exc
    finally:
        await file.close()

    return {"source_key": source_key, "sample_path": str(dest),
            "filename": file.filename, "bytes": dest.stat().st_size}


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
