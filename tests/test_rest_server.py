"""The REST transport — the browser GUI's back end.

Same tool functions as the MCP server, over HTTP via one generic dispatch
endpoint. Verifies dispatch, error mapping, and that the dashboard is served.
"""

from fastapi.testclient import TestClient

from interfaces.rest_server import app

client = TestClient(app)


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
    r = client.get("/")
    assert r.status_code == 200 and "K-Realty" in r.text and "callTool" in r.text


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
