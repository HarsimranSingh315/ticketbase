"""
Tests for outbound calling: placing a call via a ticket's linked
customer contact, the LocalSinkCallAdapter/TwilioCallAdapter selection
logic, and the call-history UI on the ticket detail page.

TwilioCallAdapter itself is tested with a mocked Twilio client - same
honesty pattern as ResendAdapter before a real key existed for it:
written and unit-tested, not verified against a live account (no
Twilio account available in this environment). Everything else here
(the route, the local sink path, the UI) was first verified manually
end-to-end before being written as permanent tests - see
docs/current-state.md.
"""
from unittest.mock import patch, Mock

from tests.conftest import get_csrf_token


def _link_customer_with_callable_contact(admin_client, db_session, phone="+15551234567"):
    from app import crud
    customer = crud.create_customer(db_session, "Acme Corp")
    contact = crud.create_contact(db_session, customer_id=customer.id, name="Jane Doe", phone=phone)
    return customer, contact


# --- Adapter selection ---

def test_get_call_adapter_defaults_to_local_sink():
    from app.telephony import get_call_adapter, LocalSinkCallAdapter
    from app.config import Settings
    settings = Settings(database_url="sqlite:///:memory:")
    adapter = get_call_adapter(settings)
    assert isinstance(adapter, LocalSinkCallAdapter)


def test_get_call_adapter_uses_twilio_when_both_credentials_set():
    from app.telephony import get_call_adapter, TwilioCallAdapter
    from app.config import Settings
    settings = Settings(database_url="sqlite:///:memory:", twilio_account_sid="AC_test", twilio_auth_token="test_token")
    adapter = get_call_adapter(settings)
    assert isinstance(adapter, TwilioCallAdapter)


def test_get_call_adapter_stays_local_sink_with_only_one_credential():
    """Partial config (e.g. account SID but no auth token) must not
    accidentally select the real adapter."""
    from app.telephony import get_call_adapter, LocalSinkCallAdapter
    from app.config import Settings
    settings = Settings(database_url="sqlite:///:memory:", twilio_account_sid="AC_test")
    adapter = get_call_adapter(settings)
    assert isinstance(adapter, LocalSinkCallAdapter)


# --- LocalSinkCallAdapter ---

def test_local_sink_adapter_returns_a_clearly_labeled_fake_sid():
    from app.telephony import LocalSinkCallAdapter
    adapter = LocalSinkCallAdapter()
    result = adapter.place_call(to_number="+15551234567", from_number="+18005551212")
    assert result.success is True
    assert result.call_sid.startswith("local-call-")


# --- TwilioCallAdapter (mocked - no live account available) ---

def test_twilio_adapter_requires_twiml_url():
    from app.telephony import TwilioCallAdapter
    with patch("twilio.rest.Client"):
        adapter = TwilioCallAdapter("AC_test", "test_token")
        result = adapter.place_call(to_number="+15551234567", from_number="+18005551212", twiml_url=None)
    assert result.success is False
    assert "twiml_url" in result.error


@patch("twilio.rest.Client")
def test_twilio_adapter_success_returns_real_call_sid(mock_client_class):
    from app.telephony import TwilioCallAdapter
    mock_call = Mock(sid="CA_real_looking_sid")
    mock_client_class.return_value.calls.create.return_value = mock_call

    adapter = TwilioCallAdapter("AC_test", "test_token")
    result = adapter.place_call(to_number="+15551234567", from_number="+18005551212", twiml_url="https://example.com/webhooks/twilio/voice")

    assert result.success is True
    assert result.call_sid == "CA_real_looking_sid"


@patch("twilio.rest.Client")
def test_twilio_adapter_failure_returns_clean_error_not_an_exception(mock_client_class):
    """A failed real call attempt must never raise all the way up into
    a 500 - the route always gets a checkable result."""
    from app.telephony import TwilioCallAdapter
    mock_client_class.return_value.calls.create.side_effect = Exception("simulated Twilio API failure")

    adapter = TwilioCallAdapter("AC_test", "test_token")
    result = adapter.place_call(to_number="+15551234567", from_number="+18005551212", twiml_url="https://example.com/webhooks/twilio/voice")

    assert result.success is False
    assert "simulated Twilio API failure" in result.error


# --- The route, end-to-end (local sink path - genuinely runnable here) ---

def test_place_call_creates_a_call_row(admin_client, db_session, monkeypatch):
    from app import crud
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+18005551212")
    from app.config import get_settings
    get_settings.cache_clear()

    customer, contact = _link_customer_with_callable_contact(admin_client, db_session)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(f"/ui/tickets/{ticket['id']}/call", data={"contact_id": contact.id, "csrf_token": csrf2}, follow_redirects=False)
    assert resp.status_code == 303

    calls = crud.list_calls_for_ticket(db_session, ticket["id"])
    assert len(calls) == 1
    assert calls[0].direction.value == "outbound"
    assert calls[0].contact_id == contact.id
    assert calls[0].twilio_call_sid.startswith("local-call-")
    get_settings.cache_clear()


def test_place_call_without_phone_number_configured_shows_clear_error(admin_client, db_session):
    customer, contact = _link_customer_with_callable_contact(admin_client, db_session)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(f"/ui/tickets/{ticket['id']}/call", data={"contact_id": contact.id, "csrf_token": csrf2})
    assert "TWILIO_PHONE_NUMBER" in resp.text


def test_cannot_call_a_contact_with_no_phone_on_file(admin_client, db_session, monkeypatch):
    from app import crud
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+18005551212")
    from app.config import get_settings
    get_settings.cache_clear()

    customer = crud.create_customer(db_session, "Acme Corp")
    contact = crud.create_contact(db_session, customer_id=customer.id, name="No Phone Contact", phone=None)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(f"/ui/tickets/{ticket['id']}/call", data={"contact_id": contact.id, "csrf_token": csrf2})
    assert resp.status_code == 404
    get_settings.cache_clear()


def test_reviewer_cannot_place_calls(reviewer_client, db_session):
    from app import crud
    customer, contact = _link_customer_with_callable_contact(reviewer_client, db_session)
    ticket = reviewer_client.post("/tickets", json={"description": "test"}).json()
    resp = reviewer_client.post(f"/ui/tickets/{ticket['id']}/call", data={"contact_id": contact.id, "csrf_token": "x"})
    assert resp.status_code == 403


# --- UI ---

def test_call_button_shown_for_contact_with_phone(admin_client, db_session):
    customer, contact = _link_customer_with_callable_contact(admin_client, db_session)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "Call Jane Doe" in detail.text


def test_no_call_button_when_no_contact_has_a_phone(admin_client, db_session):
    from app import crud
    customer = crud.create_customer(db_session, "Acme Corp")
    crud.create_contact(db_session, customer_id=customer.id, name="No Phone Contact", phone=None)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "No contact with a phone number on file" in detail.text


def test_placed_call_appears_in_ticket_call_history(admin_client, db_session, monkeypatch):
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+18005551212")
    from app.config import get_settings
    get_settings.cache_clear()

    customer, contact = _link_customer_with_callable_contact(admin_client, db_session)
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/customer", data={"customer_id": customer.id, "version": ticket["version"], "csrf_token": csrf})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/call", data={"contact_id": contact.id, "csrf_token": csrf2})

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "Outbound call" in detail.text
    assert "queued" in detail.text
    get_settings.cache_clear()
