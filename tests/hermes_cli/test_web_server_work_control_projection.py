"""Contract tests for the Desktop's read-only Work Control projection."""

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli import work_control_projection as projection_module
from hermes_cli.work_control_projection import (
    ProjectionUnavailable,
    WorkControlProjection,
)
from urllib.request import Request


def _payload(version: int = 1) -> dict:
    return {
        "schema_version": "hermes-kernel-projection.v1",
        "authority": "remote",
        "generated_at": "2026-09-04T12:00:00Z",
        "ttl_seconds": 30,
        "records": [
            {
                "work_id": "work-1",
                "front": "Hermes",
                "profile": "kindra",
                "status": "running",
                "authority_version": version,
                "updated_at": "2026-09-04T12:00:00Z",
                "prompt": "must never reach the renderer",
                "token": "work-read-secret",
            }
        ],
        "internal": {"credential": "must never reach the renderer"},
    }


def test_projection_is_default_off_and_does_not_call_authority(monkeypatch):
    monkeypatch.delenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", raising=False)
    called = False

    def fetcher():
        nonlocal called
        called = True
        return _payload()

    adapter = WorkControlProjection(fetcher=fetcher)
    try:
        adapter.get(force=True)
    except ProjectionUnavailable as exc:
        assert str(exc) == "work-control projection disabled"
    else:
        raise AssertionError("default-off projection must fail closed")
    assert called is False


def test_projection_sanitizes_authority_response(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", "1")
    projected = WorkControlProjection(fetcher=lambda: _payload()).get(force=True)

    assert set(projected) == {
        "schema_version",
        "authority",
        "generated_at",
        "ttl_seconds",
        "records",
        "stale",
        "source",
    }
    assert set(projected["records"][0]) == {
        "work_id",
        "front",
        "profile",
        "status",
        "authority_version",
        "updated_at",
    }
    assert "work-read-secret" not in repr(projected)
    assert "must never reach the renderer" not in repr(projected)


def test_projection_retains_last_snapshot_as_stale_on_outage(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", "true")
    responses = [_payload(7), OSError("authority down")]

    def fetcher():
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    adapter = WorkControlProjection(fetcher=fetcher)
    assert adapter.get(force=True)["stale"] is False
    stale = adapter.get(force=True)
    assert stale["stale"] is True
    assert stale["source"] == "unavailable"
    assert stale["records"][0]["authority_version"] == 7
    assert stale["error"] == "work-control projection unavailable"


def test_projection_refuses_authority_version_regression(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", "yes")
    responses = [_payload(7), _payload(6)]
    adapter = WorkControlProjection(fetcher=lambda: responses.pop(0))
    assert adapter.get(force=True)["records"][0]["authority_version"] == 7
    stale = adapter.get(force=True)
    assert stale["stale"] is True
    assert stale["source"] == "authority_version_regression"
    assert stale["records"][0]["authority_version"] == 7


def test_remote_projection_rejects_http_before_building_authenticated_request(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_URL", "http://jarvis.example.test/projection")
    monkeypatch.setenv("HERMES_WORK_CONTROL_READ_TOKEN", "must-not-leave")
    monkeypatch.setattr(
        projection_module,
        "Request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("request must not be built")),
    )

    try:
        WorkControlProjection()._fetch_remote()
    except ProjectionUnavailable as exc:
        assert str(exc) == "work-control authority must use https"
    else:
        raise AssertionError("http authority must fail closed")


def test_remote_projection_redirect_handler_never_builds_forward_request():
    original = Request(
        "https://jarvis.example.test/projection",
        headers={"X-API-Key": "must-not-be-forwarded"},
    )

    try:
        projection_module._RejectRedirects().redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "https://attacker.example.test/collect",
        )
    except ProjectionUnavailable as exc:
        assert str(exc) == "work-control authority redirects are forbidden"
    else:
        raise AssertionError("redirect must fail before a new request is created")


def test_local_get_is_read_only_and_returns_sanitized_projection(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", "1")
    monkeypatch.setattr(
        web_server.work_control_projection,
        "get",
        lambda: WorkControlProjection(fetcher=lambda: _payload()).get(force=True),
    )
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    response = client.get("/api/work-control/projection")
    assert response.status_code == 200
    assert response.json()["records"][0]["work_id"] == "work-1"
    assert "work-read-secret" not in response.text
    assert client.post("/api/work-control/projection").status_code == 405


def test_local_get_fails_closed_before_first_snapshot(monkeypatch):
    monkeypatch.setenv("HERMES_WORK_CONTROL_PROJECTION_ENABLED", "1")
    monkeypatch.setattr(
        web_server.work_control_projection,
        "get",
        lambda: (_ for _ in ()).throw(ProjectionUnavailable("authority down")),
    )
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

    response = client.get("/api/work-control/projection")
    assert response.status_code == 503
    assert response.json() == {
        "schema_version": "hermes-kernel-projection.v1",
        "records": [],
        "stale": True,
        "source": "unavailable",
    }
    assert "authority down" not in response.text
