"""The REST transport — the browser GUI's back end.

Same tool functions as the MCP server, over HTTP via one generic dispatch
endpoint. Verifies dispatch, error mapping, and that the dashboard is served.
"""

from pathlib import Path
import re

import pytest

from fastapi.testclient import TestClient

from interfaces.rest_server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def repo_root_is_disposable(tmp_path, monkeypatch):
    """Nothing a test uploads may land in the operator's real data/ directory.

    A failed upload now RETAINS the document (so the model can be offered on it),
    which quietly wrote test fixtures into the live repo until this existed.
    Individual tests still re-point REPO_ROOT when they assert on the location.
    """
    monkeypatch.setattr("interfaces.rest_server.REPO_ROOT", tmp_path)


def test_read_tool_returns_json():
    r = client.post("/api/tool/list_sources", json={})
    assert r.status_code == 200 and isinstance(r.json(), list) and r.json()


def test_status_tool_shape():
    r = client.post("/api/tool/status", json={})
    assert r.status_code == 200
    assert "llm" in r.json() and "sources_total" in r.json()


def test_unknown_tool_is_404():
    r = client.post("/api/tool/does_not_exist", json={})
    assert r.status_code == 404


def test_toolerror_maps_to_400():
    r = client.post("/api/tool/run_scraper", json={"source_key": "first_federal_loan"})
    assert r.status_code == 400 and "No scraper built" in r.json()["detail"]


def test_bad_arguments_map_to_400():
    r = client.post("/api/tool/activate_parser", json={"wrong_arg": "x"})
    assert r.status_code == 400


def test_dashboard_is_served():
    """The built frontend (frontend/ -> Vite -> interfaces/web/dist/) is served
    at '/', asset-hashed JS and all. Minification renames the callTool
    identifier itself, so the thing worth pinning is the string literal it
    fetches — '/api/tool/' — the actual transport-swap-point contract between
    the frontend and this server."""
    r = client.get("/")
    assert r.status_code == 200 and "K-Realty" in r.text

    bundle_src = re.search(r'src="([^"]+\.js)"', r.text).group(1)
    bundle = client.get(bundle_src)
    assert bundle.status_code == 200 and "/api/tool/" in bundle.text


def test_upload_ingest_success(monkeypatch):
    called = {}

    def fake_ingest_document(source_key: str, path: str):
        called["source_key"] = source_key
        called["path"] = path
        return {"source_key": source_key, "count": 2}

    monkeypatch.setattr("interfaces.rest_server.mcp_tools.ingest_document", fake_ingest_document)

    r = client.post(
        "/api/upload_ingest/dfcu_financial_bank",
        files={"file": ("statement.csv", b"Date,Description,Amount\n", "text/csv")},
    )

    assert r.status_code == 200
    assert r.json()["count"] == 2
    assert isinstance(r.json().get("steps"), list)
    assert any(s.get("key") == "save_temp_file" and s.get("status") == "success" for s in r.json()["steps"])
    assert any(s.get("key") == "ingest_document" and s.get("status") == "success" for s in r.json()["steps"])
    assert any(s.get("key") == "cleanup_temp_file" and s.get("status") == "success" for s in r.json()["steps"])
    assert called["source_key"] == "dfcu_financial_bank"
    assert called["path"]


def test_upload_sample_keeps_the_file_for_the_agent(tmp_path, monkeypatch):
    """Unlike upload_ingest, this must NOT delete the file — a background build
    re-reads the sample for minutes, and a revise pass needs it again."""
    monkeypatch.setattr("interfaces.rest_server.REPO_ROOT", tmp_path)

    r = client.post(
        "/api/upload_sample/dfcu_financial_bank",
        files={"file": ("statement.csv", b"Date,Description,Amount\n", "text/csv")},
    )

    assert r.status_code == 200
    body = r.json()
    saved = Path(body["sample_path"])
    assert saved.exists(), "the sample must survive the request"
    assert saved.read_bytes() == b"Date,Description,Amount\n"
    assert saved.suffix == ".csv"
    assert saved.parent == tmp_path / "data" / "samples"   # gitignored, like all financial docs
    assert body["bytes"] == 24


@pytest.mark.parametrize("key", ["..", "../../etc", "..%2F..%2Fetc", "a/../../b"])
def test_upload_sample_cannot_escape_the_samples_dir(tmp_path, monkeypatch, key):
    """source_key reaches the filesystem, so it must never write outside
    data/samples/ — whether it is rejected, unrouted, or sanitized."""
    monkeypatch.setattr("interfaces.rest_server.REPO_ROOT", tmp_path)

    r = client.post(f"/api/upload_sample/{key}", files={"file": ("x.csv", b"a", "text/csv")})

    if r.status_code == 200:
        saved = Path(r.json()["sample_path"]).resolve()
        assert (tmp_path / "data" / "samples").resolve() in saved.parents
    else:
        # 400 = sanitized to nothing; 404 = the path never matched the route.
        assert r.status_code in (400, 404)


def test_upload_sample_filename_suffix_cannot_escape(tmp_path, monkeypatch):
    """The suffix comes from the client-supplied filename."""
    monkeypatch.setattr("interfaces.rest_server.REPO_ROOT", tmp_path)

    r = client.post(
        "/api/upload_sample/dfcu_financial_bank",
        files={"file": ("../../evil.csv", b"a", "text/csv")},
    )

    assert r.status_code == 200
    saved = Path(r.json()["sample_path"]).resolve()
    assert (tmp_path / "data" / "samples").resolve() in saved.parents


def test_upload_ingest_maps_toolerror_to_400(monkeypatch):
    class FakeToolError(RuntimeError):
        pass

    monkeypatch.setattr("interfaces.rest_server.mcp_tools.ToolError", FakeToolError)

    def fake_ingest_toolerror(source_key: str, path: str):
        raise FakeToolError("not ingestible")

    monkeypatch.setattr("interfaces.rest_server.mcp_tools.ingest_document", fake_ingest_toolerror)

    r = client.post(
        "/api/upload_ingest/dfcu_financial_bank",
        files={"file": ("statement.csv", b"Date,Description,Amount\n", "text/csv")},
    )

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["message"] == "not ingestible"
    assert isinstance(detail.get("steps"), list)
    assert any(s.get("key") == "ingest_document" and s.get("status") == "failed" for s in detail["steps"])
