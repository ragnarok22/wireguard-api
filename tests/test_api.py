import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def api_module(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token")
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(repo_root))
    if "api" in sys.modules:
        del sys.modules["api"]
    return importlib.import_module("api")


@pytest.fixture()
def client(api_module):
    return TestClient(api_module.app)


def test_rejects_invalid_token(client):
    response = client.post("/", json={"token": "wrong", "command": "echo hi"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid authentication token"


def test_executes_command(monkeypatch, api_module, client):
    monkeypatch.setattr(api_module.subprocess, "check_output", lambda *_, **__: "ok\n")

    response = client.post("/", json={"token": "secret-token", "command": "echo ok"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_returns_error_output(monkeypatch, api_module, client):
    def fail(cmd, *_, **__):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, output="boom")

    monkeypatch.setattr(api_module.subprocess, "check_output", fail)

    response = client.post("/", json={"token": "secret-token", "command": "echo boom"})

    assert response.status_code == 200
    assert response.json() == {"status": "Error: boom"}
