"""
Tests for the optional LLM draft-response step (app/llm.py, wired into
app/supportrag.py). These mock the network call - the suite stays
offline and deterministic, same as every other test here - but they
exercise the actual integration code path (SupportRAGService calling
generate_grounded_draft), not just app/llm.py in isolation.

Covers the properties that matter most for this feature specifically:
- Off by default: no API key configured -> template, unchanged.
- On, success -> the LLM's text is used and draft_source is "llm".
- On, but the call fails in any way -> falls back to the template
  rather than raising or returning a broken suggestion.
- NEVER called when retrieval abstained - the one hard safety rule of
  this feature.
"""
from tests.conftest import csrf_headers
from unittest.mock import patch, Mock

import pytest
import requests

from app.main import app
from app.config import Settings, get_settings

FAKE_LLM_REPLY = "Try restarting your VPN client and checking your certificate."


def _settings_with_llm(**overrides):
    def factory():
        return Settings(llm_api_key="fake-test-key", database_url="sqlite:///:memory:", **overrides)
    return factory


@pytest.fixture
def llm_client(admin_client):
    app.dependency_overrides[get_settings] = _settings_with_llm()
    yield admin_client
    del app.dependency_overrides[get_settings]


def _mock_groq_response(text=FAKE_LLM_REPLY, status_code=200):
    mock_resp = Mock()
    mock_resp.status_code = status_code
    mock_resp.text = "mock body"
    mock_resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return mock_resp


def test_llm_disabled_by_default_uses_template(admin_client):
    # Plain `admin_client` has no LLM key configured, just a real session.
    ticket = admin_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    data = resp.json()
    assert data["draft_source"] == "template"
    assert "review and edit before sending" in data["draft_response"]


@patch("app.llm.requests.post")
def test_llm_success_is_used_as_the_draft(mock_post, llm_client):
    mock_post.return_value = _mock_groq_response()
    ticket = llm_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    data = resp.json()
    assert data["draft_source"] == "llm"
    assert data["draft_response"] == FAKE_LLM_REPLY
    # The category/confidence/sources still come from retrieval, not the LLM.
    assert data["category"] == "connectivity"
    assert len(data["sources"]) > 0


@patch("app.llm.requests.post")
def test_llm_timeout_falls_back_to_template(mock_post, llm_client):
    mock_post.side_effect = requests.exceptions.Timeout("simulated timeout")
    ticket = llm_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    assert resp.status_code == 200  # never breaks the endpoint
    data = resp.json()
    assert data["draft_source"] == "template"
    assert data["category"] == "connectivity"  # retrieval still works fine


@patch("app.llm.requests.post")
def test_llm_error_status_falls_back_to_template(mock_post, llm_client):
    # Simulates a decommissioned/renamed free-tier model (404) or a
    # rate limit (429) - either way, must not break the suggestion.
    mock_post.return_value = _mock_groq_response(status_code=404)
    ticket = llm_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    assert resp.status_code == 200
    assert resp.json()["draft_source"] == "template"


@patch("app.llm.requests.post")
def test_llm_malformed_response_falls_back_to_template(mock_post, llm_client):
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.text = '{"unexpected": "shape"}'
    mock_resp.json.return_value = {"unexpected": "shape"}
    mock_post.return_value = mock_resp
    ticket = llm_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    assert resp.status_code == 200
    assert resp.json()["draft_source"] == "template"


@patch("app.llm.requests.post")
def test_llm_empty_content_falls_back_to_template(mock_post, llm_client):
    # The real bug found via live testing: a reasoning-capable free-tier
    # model (Groq's gpt-oss family) can return HTTP 200 with a non-empty
    # `reasoning` field but an EMPTY `content` field, if it spends its
    # whole token budget on internal reasoning before writing an answer.
    # This used to fail completely silently - same log output as
    # "disabled". Now it must fall back cleanly AND be logged.
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.text = '{"choices": [{"message": {"content": "", "reasoning": "thinking..."}, "finish_reason": "length"}]}'
    mock_resp.json.return_value = {
        "choices": [{
            "message": {"content": "", "reasoning": "thinking really hard..."},
            "finish_reason": "length",
        }]
    }
    mock_post.return_value = mock_resp
    ticket = llm_client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    assert resp.status_code == 200
    data = resp.json()
    assert data["draft_source"] == "template"
    assert data["category"] == "connectivity"  # retrieval is unaffected


@patch("app.llm.requests.post")
def test_llm_never_called_when_retrieval_abstains(mock_post, llm_client):
    # The hard safety rule: no confident retrieval match -> no LLM call
    # at all, regardless of whether one is configured.
    ticket = llm_client.post("/tickets", json={"description": "zz flumox glorbnax qwerty"}).json()
    resp = llm_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(llm_client))
    data = resp.json()
    assert data["abstained"] is True
    mock_post.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]},  # the B7 reproduction
    {"choices": [{"message": "not-a-dict"}]},
    {"choices": []},
    {"choices": "nope"},
    ["not", "a", "dict"],
    {"choices": [{"message": {"content": 42}}]},
])
def test_b7_unexpected_provider_shapes_fall_back_instead_of_raising(payload):
    from unittest.mock import patch, MagicMock
    from app.llm import generate_grounded_draft
    resp = MagicMock(status_code=200, content=b"{}")
    resp.json.return_value = payload
    with patch("app.llm.requests.post", return_value=resp):
        from app.config import Settings
        result = generate_grounded_draft(
            Settings(llm_api_key="k", database_url="sqlite://"),
            ticket_description="VPN broken", source_title="VPN", source_content="Restart the client.",
        )
    assert result is None
