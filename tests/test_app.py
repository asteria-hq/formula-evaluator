"""HTTP surface: auth, input validation, gates and status mapping."""

import base64
import importlib
from typing import Any

import httpx
import pytest
from conftest import WorkbookFactory
from fastapi.testclient import TestClient

API_KEY = "test-secret-key"  # pragma: allowlist secret


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Reload the module so the env-read module constants take effect.

    ``app.py`` reads its key and gates at import time, so setting the env after
    import would have no effect.
    """
    monkeypatch.setenv("FORMULA_EVALUATOR_API_KEY", API_KEY)
    monkeypatch.delenv("FORMULA_EVALUATOR_AUTH_DISABLED", raising=False)
    import app as app_module

    importlib.reload(app_module)
    return TestClient(app_module.app)


def post(client: TestClient, workbook: bytes = b"", **body: Any) -> httpx.Response:
    payload = {"workbook_b64": base64.b64encode(workbook).decode(), **body}
    return client.post("/evaluate", json=payload, headers={"X-API-Key": API_KEY})


def test_health_needs_no_auth(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


class TestAuth:
    @pytest.mark.parametrize("key", [None, "", "wrong", API_KEY + "x"])
    def test_bad_key_is_rejected(
        self, client: TestClient, simple_workbook: bytes, key: str | None
    ) -> None:
        headers = {} if key is None else {"X-API-Key": key}
        response = client.post(
            "/evaluate",
            json={"workbook_b64": base64.b64encode(simple_workbook).decode()},
            headers=headers,
        )
        assert response.status_code == 401

    def test_unconfigured_key_denies_rather_than_opens(
        self, monkeypatch: pytest.MonkeyPatch, simple_workbook: bytes
    ) -> None:
        """An unset key must fail closed unless the dev opt-out is explicit."""
        monkeypatch.delenv("FORMULA_EVALUATOR_API_KEY", raising=False)
        monkeypatch.delenv("FORMULA_EVALUATOR_AUTH_DISABLED", raising=False)
        import app as app_module

        importlib.reload(app_module)
        response = TestClient(app_module.app).post(
            "/evaluate", json={"workbook_b64": base64.b64encode(simple_workbook).decode()}
        )
        assert response.status_code == 503


class TestInputValidation:
    def test_invalid_base64_is_400(self, client: TestClient) -> None:
        response = client.post(
            "/evaluate",
            json={"workbook_b64": "not base64!!"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 400

    def test_empty_workbook_is_400(self, client: TestClient) -> None:
        assert post(client, b"").status_code == 400

    def test_oversized_workbook_is_413(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, simple_workbook: bytes
    ) -> None:
        import app as app_module

        monkeypatch.setattr(app_module, "MAX_WORKBOOK_BYTES", 10)
        assert post(client, simple_workbook).status_code == 413

    def test_unreadable_workbook_is_422(self, client: TestClient) -> None:
        assert post(client, b"definitely not a workbook").status_code == 422


class TestEvaluateEndpoint:
    def test_returns_computed_values(self, client: TestClient, simple_workbook: bytes) -> None:
        response = post(client, simple_workbook)
        assert response.status_code == 200
        body = response.json()
        assert body["values"]["Data!E1"]["value"] == pytest.approx(450.0)
        assert body["values"]["Data!E4"] == {"value": None, "error": "#DIV/0!"}
        assert body["formula_cells"] == 4
        assert body["sheets"] == ["Data"]

    def test_what_if_applies_overrides(self, client: TestClient, simple_workbook: bytes) -> None:
        body = post(client, simple_workbook, overrides={"Data!C2": 500}).json()
        assert body["values"]["Data!E1"]["value"] == pytest.approx(850.0)
        assert body["overrides_applied"] == 1

    def test_unknown_override_is_422(self, client: TestClient, simple_workbook: bytes) -> None:
        response = post(client, simple_workbook, overrides={"Data!ZZ1": 1})
        assert response.status_code == 422

    def test_unsupported_functions_are_422_naming_them(
        self, client: TestClient, workbook_factory: WorkbookFactory
    ) -> None:
        data = workbook_factory({"Data": {"G1": "=SUM(OFFSET(C2,0,0,3,1))"}})
        response = post(client, data)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["reason"] == "unsupported_functions"
        assert detail["functions"] == ["OFFSET"]

    def test_timeout_maps_to_504(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, simple_workbook: bytes
    ) -> None:
        import app as app_module
        from runner import EvaluationTimeoutError

        def blow_up(*args: Any, **kwargs: Any) -> None:
            raise EvaluationTimeoutError("evaluation exceeded 60s and was cancelled")

        monkeypatch.setattr(app_module, "run_evaluation", blow_up)
        response = post(client, simple_workbook)
        assert response.status_code == 504
        assert "cancelled" in response.json()["detail"]
