"""habar end to end with a fake Bitrix24 (httpx.MockTransport) and a fake SMTP server.
No real portal or mail server is contacted, no secrets are needed."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import habar.app as habar_app
from habar.bitrix import Bitrix, BitrixError
from habar.mailer import MailError, Mailer

WEBHOOK = "https://example.bitrix24.kz/rest/1/secretcode"
KEY = {"X-API-Key": "test-key"}


class FakeBitrixPortal:
    """Answers like Bitrix24; `fail` = how many requests in a row get HTTP 503 first."""

    def __init__(self, fail: int = 0, refuse: str = None):
        self.calls, self.fail, self.refuse, self.next_id = [], fail, refuse, 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content)))
        if self.fail:
            self.fail -= 1
            return httpx.Response(503)
        if self.refuse:
            return httpx.Response(400, json={"error": "ERROR", "error_description": self.refuse})
        self.next_id += 1
        if method == "tasks.task.add":
            return httpx.Response(200, json={"result": {"task": {"id": str(self.next_id)}}})
        return httpx.Response(200, json={"result": self.next_id})


class FakeMailer:
    def __init__(self, broken: bool = False):
        self.sent, self.broken = [], broken

    def send(self, to, subject, body, reply_to=None):
        if self.broken:
            raise MailError("e-mail failed: connection refused")
        self.sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})


@pytest.fixture
def setup(monkeypatch):
    """Returns (client, portal, mailer); swap them per test via the returned dict."""
    monkeypatch.setenv("HABAR_API_KEY", "test-key")
    monkeypatch.setenv("MANAGER_EMAIL", "manager@example.com")
    state = {"portal": FakeBitrixPortal(), "mailer": FakeMailer()}
    monkeypatch.setattr(habar_app, "get_bitrix", lambda: Bitrix(
        WEBHOOK, 7, client=httpx.Client(transport=httpx.MockTransport(state["portal"])), sleep=lambda s: None))
    monkeypatch.setattr(habar_app, "get_mailer", lambda: state["mailer"])
    habar_app.replays.answers.clear()
    state["client"] = TestClient(habar_app.app)
    return state


LEAD = {"title": "Новый клиент", "message": "Хочу демо", "notify": ["manager@example.com"],
        "contact": {"name": "Айгерим", "email": "aigerim@example.com", "phone": "+77001234567"}}


def test_lead_created_and_manager_informed(setup):
    r = setup["client"].post("/events", json=LEAD, headers=KEY)
    assert r.status_code == 200
    assert r.json()["crm"] == {"status": "created", "type": "lead", "id": 101}
    method, payload = setup["portal"].calls[0]
    assert method == "crm.lead.add"
    assert payload["fields"]["EMAIL"] == [{"VALUE": "aigerim@example.com", "VALUE_TYPE": "WORK"}]
    mail = setup["mailer"].sent[0]
    assert mail["to"] == ["manager@example.com"] and "101" in mail["body"]
    assert mail["reply_to"] == "aigerim@example.com"  # the manager can answer the client directly


def test_task_goes_to_the_responsible_person(setup):
    r = setup["client"].post("/events", json={"title": "Проверить договор", "crm": "task"}, headers=KEY)
    assert r.json()["crm"]["id"] == 101
    assert setup["portal"].calls[0][1]["fields"]["RESPONSIBLE_ID"] == 7


def test_api_key_is_required(setup):
    assert setup["client"].post("/events", json=LEAD).status_code == 401
    assert setup["client"].post("/events", json=LEAD, headers={"X-API-Key": "wrong"}).status_code == 401
    assert setup["portal"].calls == []


def test_bad_input_is_rejected_before_anything_happens(setup):
    r = setup["client"].post("/events", json={"title": "", "contact": {"email": "not-an-email"}}, headers=KEY)
    assert r.status_code == 422 and setup["portal"].calls == []


def test_temporary_bitrix_errors_are_retried(setup):
    setup["portal"] = FakeBitrixPortal(fail=2)  # 503, 503, then success
    r = setup["client"].post("/events", json=LEAD, headers=KEY)
    assert r.json()["crm"]["status"] == "created" and len(setup["portal"].calls) == 3


def test_refusal_is_reported_without_the_secret_webhook(setup):
    setup["portal"] = FakeBitrixPortal(refuse="Access denied.")
    r = setup["client"].post("/events", json=LEAD, headers=KEY)
    assert r.status_code == 207  # CRM failed, e-mail still went out
    assert "Access denied." in r.json()["crm"]["error"] and "secretcode" not in r.text
    assert r.json()["email"]["status"] == "sent"
    assert len(setup["portal"].calls) == 1  # a refusal is not retried


def test_broken_mail_never_loses_the_lead(setup):
    setup["mailer"] = FakeMailer(broken=True)
    r = setup["client"].post("/events", json=LEAD, headers=KEY)
    assert r.status_code == 207
    assert r.json()["crm"]["status"] == "created" and r.json()["email"]["status"] == "failed"


def test_everything_failing_is_a_502(setup):
    setup["portal"], setup["mailer"] = FakeBitrixPortal(fail=10), FakeMailer(broken=True)
    assert setup["client"].post("/events", json=LEAD, headers=KEY).status_code == 502


def test_same_idempotency_key_creates_one_lead(setup):
    headers = {**KEY, "Idempotency-Key": "order-555"}
    first = setup["client"].post("/events", json=LEAD, headers=headers)
    again = setup["client"].post("/events", json=LEAD, headers=headers)
    assert first.json() == again.json() and again.headers["Idempotent-Replay"] == "true"
    assert len(setup["portal"].calls) == 1 and len(setup["mailer"].sent) == 1


def test_website_form_creates_lead_and_auto_reply(setup):
    r = setup["client"].post("/forms/contact", data={"name": "Ержан", "email": "erzhan@example.com",
                                                     "message": "Нужна консультация"})
    assert r.status_code == 200
    assert setup["portal"].calls[0][1]["fields"]["NAME"] == "Ержан"
    recipients = [m["to"] for m in setup["mailer"].sent]
    assert ["manager@example.com"] in recipients and ["erzhan@example.com"] in recipients


def test_form_spam_bots_are_dropped_silently(setup):
    r = setup["client"].post("/forms/contact", data={"name": "bot", "email": "bot@spam.example",
                                                     "website": "http://spam.example"})
    assert r.status_code == 200 and setup["portal"].calls == [] and setup["mailer"].sent == []


def test_missing_configuration_is_explained():
    with pytest.raises(BitrixError, match="BITRIX_WEBHOOK"):
        Bitrix("")
    with pytest.raises(MailError, match="SMTP_HOST"):
        Mailer("", 465, "", "")
