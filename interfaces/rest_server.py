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

import json
import os
from pathlib import Path
import tempfile
import time

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from interfaces import mcp_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
# frontend/ (a Vite project) builds here — see frontend/vite.config.js. Not
# interfaces/web/ directly: that stayed a plain static dir pre-modularization,
# this is generated output, gitignored like any other build artifact.
WEB_DIR = Path(__file__).resolve().parent / "web" / "dist"

# Same tool surface the MCP server exposes — one source of truth.
TOOLS = {fn.__name__: fn for fn in mcp_tools.ALL_TOOLS}

app = FastAPI(title="k-realty")

if (WEB_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


@app.get("/")
def index() -> FileResponse:
    index_html = WEB_DIR / "index.html"
    if not index_html.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Frontend not built — run 'npm install && npm run build' in frontend/ "
                    f"(expected {index_html}).",
        )
    return FileResponse(index_html)


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


def _worth_retrying(exc: Exception) -> bool:
    """Would reading this document with the model plausibly help?

    Deliberately broad: a declared ParseError, a source with no parser yet, and
    a parser that simply crashed all mean the same thing to the operator — the
    harness couldn't read their document. Withholding the escape hatch because
    the failure arrived as an IndexError rather than a ParseError would be the
    worse mistake. The one exclusion is a source key that doesn't exist: there
    is nothing to attach the document to, and keeping it would just leave a
    financial document on disk for no reason.
    """
    from core.ingest import IngestError

    return not (isinstance(exc, IngestError) and "unknown source" in str(exc).lower())


def _retain_unparsed(source_key: str, tmp_path: Path, filename: str | None) -> Path | None:
    """Keep a document the parser choked on, so it can be re-read with the model.

    Only for failures where a retry makes sense — a bad source key isn't one, and
    shouldn't leave a stray financial document behind. Lands in data/samples/
    (gitignored) alongside build samples."""
    safe_key = "".join(c for c in source_key if c.isalnum() or c in "_-")
    if not safe_key or not tmp_path.exists():
        return None
    samples_dir = REPO_ROOT / "data" / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    dest = samples_dir / f"{safe_key}-unparsed{Path(filename or tmp_path.name).suffix}"
    try:
        dest.write_bytes(tmp_path.read_bytes())
    except OSError:
        return None
    return dest


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
        except Exception as exc:
            steps.append({
                "key": "ingest_document",
                "label": "Parse and persist",
                "status": "failed",
                "duration_ms": int((time.perf_counter() - ingest_started) * 1000),
                "error": str(exc),
            })
            # The parse failed, so the temp file is about to be deleted with the
            # operator's document still unread. Keep a copy: the answer to "the
            # parser can't read this layout" is usually "then read it with the
            # model", and that offer needs a file to point at.
            detail = {"message": str(exc), "steps": steps}
            if _worth_retrying(exc):
                retained = _retain_unparsed(source_key, tmp_path, file.filename)
                if retained:
                    detail["retry_path"] = str(retained)
                    detail["filename"] = file.filename
            status = 400 if isinstance(exc, mcp_tools.ToolError) else 500
            error = HTTPException(status_code=status, detail=detail)

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


@app.post("/api/upload_oauth_client/{source_key}")
async def upload_oauth_client(source_key: str, file: UploadFile = File(...)):
    """Take the OAuth client JSON downloaded from Google Cloud.

    It carries a client secret, so it lands in .secrets/ (gitignored) rather than
    data/, and the consent worker DELETES it as soon as consent finishes — by
    then its contents are in the encrypted store, and a second plaintext copy of
    a client secret is pure downside.
    """
    safe_key = "".join(c for c in source_key if c.isalnum() or c in "_-")
    if not safe_key:
        raise HTTPException(status_code=400, detail={"message": f"Invalid source key '{source_key}'."})

    secrets_dir = REPO_ROOT / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    dest = secrets_dir / f"oauth-client-{safe_key}.json"
    try:
        raw = await file.read()
        # Fail here rather than deep inside Google's flow, where the error is opaque.
        parsed = json.loads(raw)
        if not any(k in parsed for k in ("installed", "web")):
            raise ValueError("not an OAuth client file — expected an 'installed' or 'web' section")
        dest.write_bytes(raw)
        dest.chmod(0o600)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail={
            "message": f"That file isn't a Google OAuth client JSON: {exc}",
        }) from exc
    finally:
        await file.close()

    return {"source_key": source_key, "client_secret_path": str(dest), "filename": file.filename}


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
