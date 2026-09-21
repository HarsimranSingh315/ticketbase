"""
Tests for cli.py's own logic: config (env vars), auth header injection,
and error handling (401, connection failure, timeout). These mock
`requests.request` rather than needing a live server - a real live-
server end-to-end run was also done manually (see docs/current-state.md)
before this file existed; this is what keeps that behavior covered
going forward without needing a server in every test run.
"""
import importlib
from unittest.mock import patch, Mock

import pytest
import requests
from click.testing import CliRunner


def _reload_cli(monkeypatch, **env):
    """cli.py reads its config (API_BASE, API_KEY, TIMEOUT) at import
    time, so testing different configs means reloading the module with
    different env vars set first - a plain function call wouldn't pick
    up a changed env var after the first import."""
    for key in ("TICKETBASE_API_URL", "TICKETBASE_API_KEY", "TICKETBASE_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import cli as cli_module
    importlib.reload(cli_module)
    return cli_module


def test_default_config_when_no_env_vars_set(monkeypatch):
    cli_module = _reload_cli(monkeypatch)
    assert cli_module.API_BASE == "http://127.0.0.1:8000"
    assert cli_module.API_KEY == ""
    assert cli_module.TIMEOUT == 10.0


def test_config_reads_from_environment(monkeypatch):
    cli_module = _reload_cli(
        monkeypatch,
        TICKETBASE_API_URL="http://example.com:9000",
        TICKETBASE_API_KEY="secret123",
        TICKETBASE_TIMEOUT="30",
    )
    assert cli_module.API_BASE == "http://example.com:9000"
    assert cli_module.API_KEY == "secret123"
    assert cli_module.TIMEOUT == 30.0


@patch("cli.requests.request")
def test_api_key_sent_as_header_when_configured(mock_request, monkeypatch):
    cli_module = _reload_cli(monkeypatch, TICKETBASE_API_KEY="my-key")
    mock_request.return_value = Mock(status_code=200)

    cli_module._request("GET", "/tickets")

    _, kwargs = mock_request.call_args
    assert kwargs["headers"]["X-API-Key"] == "my-key"


@patch("cli.requests.request")
def test_no_auth_header_when_no_key_configured(mock_request, monkeypatch):
    cli_module = _reload_cli(monkeypatch)
    mock_request.return_value = Mock(status_code=200)

    cli_module._request("GET", "/tickets")

    _, kwargs = mock_request.call_args
    assert "X-API-Key" not in kwargs["headers"]


@patch("cli.requests.request")
def test_401_exits_with_clear_message_not_a_traceback(mock_request, monkeypatch, capsys):
    cli_module = _reload_cli(monkeypatch)
    mock_request.return_value = Mock(status_code=401)

    with pytest.raises(SystemExit) as exc_info:
        cli_module._request("GET", "/tickets")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "API key" in captured.err


@patch("cli.requests.request")
def test_connection_error_exits_cleanly(mock_request, monkeypatch, capsys):
    cli_module = _reload_cli(monkeypatch)
    mock_request.side_effect = requests.exceptions.ConnectionError()

    with pytest.raises(SystemExit) as exc_info:
        cli_module._request("GET", "/tickets")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "is the server running" in captured.err


@patch("cli.requests.request")
def test_timeout_exits_cleanly(mock_request, monkeypatch, capsys):
    cli_module = _reload_cli(monkeypatch)
    mock_request.side_effect = requests.exceptions.Timeout()

    with pytest.raises(SystemExit) as exc_info:
        cli_module._request("GET", "/tickets")

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err


@patch("cli.requests.request")
def test_status_command_fetches_version_before_writing(mock_request, monkeypatch):
    """Regression test: this command used to send no `version` at all,
    which the API now requires (optimistic concurrency) - it would 422
    against the real server. Confirms the fix fetches the ticket first
    and includes its version in the write."""
    cli_module = _reload_cli(monkeypatch)

    get_response = Mock(status_code=200)
    get_response.json.return_value = {
        "id": 1, "description": "test", "category": None, "category_confirmed": False,
        "status": "open", "priority": "medium", "version": 5, "created_at": "2026-01-01T00:00:00",
    }
    patch_response = Mock(status_code=200)
    patch_response.json.return_value = {**get_response.json.return_value, "status": "in_progress"}
    mock_request.side_effect = [get_response, patch_response]

    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["status", "1", "in_progress"])

    assert result.exit_code == 0
    patch_call = mock_request.call_args_list[1]
    assert patch_call.kwargs["json"]["version"] == 5


@patch("cli.requests.request")
def test_status_command_handles_version_conflict(mock_request, monkeypatch):
    cli_module = _reload_cli(monkeypatch)

    get_response = Mock(status_code=200)
    get_response.json.return_value = {
        "id": 1, "description": "test", "category": None, "category_confirmed": False,
        "status": "open", "priority": "medium", "version": 3, "created_at": "2026-01-01T00:00:00",
    }
    conflict_response = Mock(status_code=409)
    mock_request.side_effect = [get_response, conflict_response]

    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["status", "1", "resolved"])

    assert result.exit_code == 1
    assert "changed since it was last read" in result.output
