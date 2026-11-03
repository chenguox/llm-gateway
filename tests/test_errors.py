from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient


def test_unknown_route_has_consistent_error(client: TestClient) -> None:
    response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "HTTP_ERROR", "message": "Not Found"}}


def test_http_error_preserves_headers(application: FastAPI, client: TestClient) -> None:
    @application.get("/test/unauthorized")
    def unauthorized() -> None:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    response = client.get("/test/unauthorized")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["message"] == "Authentication required"


def test_validation_error_omits_input(application: FastAPI, client: TestClient) -> None:
    @application.get("/test/validation")
    def validation(x_token: Annotated[int, Header()]) -> dict[str, int]:
        return {"value": x_token}

    response = client.get("/test/validation", headers={"x-token": "private-value"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["location"] == ["header", "x-token"]
    assert "private-value" not in response.text


def test_unexpected_error_hides_internal_message(application: FastAPI, client: TestClient) -> None:
    @application.get("/test/failure")
    def failure() -> None:
        raise RuntimeError("private upstream details")

    response = client.get("/test/failure")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}
    }
    assert "private upstream details" not in response.text
