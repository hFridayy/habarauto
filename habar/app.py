"""The API.

  POST /events         any system: create a lead or task in Bitrix24 and send e-mails (needs X-API-Key)
  POST /forms/contact  a website contact form: lead in Bitrix24 + mail to the manager + auto-reply
  GET  /health         is the service up

Every channel (CRM, e-mail, auto-reply) is tried on its own and reported on its own, so a broken
mail server never loses a lead. An Idempotency-Key header makes retries by the caller safe:
the same key never creates a second lead.
"""
import hmac
import os
import re
import threading
from collections import OrderedDict
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .bitrix import Bitrix, BitrixError
from .mailer import MailError, Mailer

load_dotenv()

EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

app = FastAPI(title="habar", version="0.1.0",
              description="Website forms and events -> Bitrix24 CRM + e-mail notifications")


class Contact(BaseModel):
    name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=200, pattern=EMAIL)
    phone: str | None = Field(None, max_length=50)


class Event(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    message: str = Field("", max_length=10_000)
    crm: Literal["lead", "task", "none"] = "lead"
    notify: list[str] = Field(default_factory=list, max_length=20, description="e-mail addresses to inform")
    contact: Contact | None = None
    reply_to_contact: bool = Field(False, description="send the contact a 'we received your request' e-mail")


# ---------------------------------------------------------------- connections (replaced in tests)

def get_bitrix() -> Bitrix:
    return Bitrix(os.environ.get("BITRIX_WEBHOOK", ""), int(os.environ.get("BITRIX_RESPONSIBLE_ID", "1")))


def get_mailer() -> Mailer:
    return Mailer(os.environ.get("SMTP_HOST", ""), int(os.environ.get("SMTP_PORT", "465")),
                  os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", ""),
                  os.environ.get("SMTP_FROM") or None)


# ---------------------------------------------------------------- idempotency

class Replays:
    """Remembers the answers to the last `size` Idempotency-Keys."""

    def __init__(self, size: int = 1000):
        self.size, self.answers, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            return self.answers.get(key)

    def put(self, key, answer):
        with self.lock:
            self.answers[key] = answer
            while len(self.answers) > self.size:
                self.answers.popitem(last=False)


replays = Replays()
key_locks: dict = {}
key_locks_guard = threading.Lock()


# ---------------------------------------------------------------- the work

def process(event: Event) -> dict:
    results = {}
    contact = event.contact or Contact()
    if event.crm != "none":
        try:
            bitrix = get_bitrix()
            if event.crm == "lead":
                lead_id = bitrix.add_lead(event.title, event.message, contact.name, contact.email, contact.phone)
                results["crm"] = {"status": "created", "type": "lead", "id": lead_id}
            else:
                task_id = bitrix.add_task(event.title, event.message)
                results["crm"] = {"status": "created", "type": "task", "id": task_id}
        except BitrixError as e:
            results["crm"] = {"status": "failed", "error": str(e)}

    if event.notify:
        bad = [a for a in event.notify if not _is_email(a)]
        if bad:
            results["email"] = {"status": "failed", "error": f"not e-mail addresses: {bad}"}
        else:
            try:
                get_mailer().send(event.notify, f"[habar] {event.title}", _manager_text(event, results),
                                  reply_to=contact.email)
                results["email"] = {"status": "sent", "to": event.notify}
            except MailError as e:
                results["email"] = {"status": "failed", "error": str(e)}

    if event.reply_to_contact and contact.email:
        try:
            get_mailer().send([contact.email], "Мы получили вашу заявку / We received your request",
                              _auto_reply(contact.name))
            results["auto_reply"] = {"status": "sent", "to": contact.email}
        except MailError as e:
            results["auto_reply"] = {"status": "failed", "error": str(e)}
    return results


def answer_status(results: dict) -> int:
    """200 = everything worked, 207 = some channels failed, 502 = every channel failed."""
    failed = sum(r["status"] == "failed" for r in results.values())
    if not failed:
        return 200
    return 502 if failed == len(results) else 207


def _is_email(text: str) -> bool:
    return re.fullmatch(EMAIL, text) is not None


def _manager_text(event: Event, results: dict) -> str:
    c = event.contact or Contact()
    lines = [event.title, "", event.message, "",
             f"Contact: {c.name or '-'} | {c.email or '-'} | {c.phone or '-'}"]
    if results.get("crm", {}).get("status") == "created":
        lines.append(f"Bitrix24 {results['crm']['type']} id: {results['crm']['id']}")
    return "\n".join(lines)


def _auto_reply(name: str = None) -> str:
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    return (f"{hello}\n\nМы получили вашу заявку и свяжемся с вами в ближайшее время.\n\n"
            "Hello! We have received your request and will contact you soon.")


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/events")
def create_event(event: Event, x_api_key: str = Header(None),
                 idempotency_key: str = Header(None, max_length=200)):
    expected = os.environ.get("HABAR_API_KEY", "")
    if not expected:
        raise HTTPException(503, "server is not configured: set HABAR_API_KEY")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "missing or wrong X-API-Key")
    if not idempotency_key:
        results = process(event)
        return JSONResponse(results, status_code=answer_status(results))

    with key_locks_guard:  # one lock per key: two identical requests at once still create one lead
        lock = key_locks.setdefault(idempotency_key, threading.Lock())
    with lock:
        saved = replays.get(idempotency_key)
        if saved:
            return JSONResponse(saved[1], status_code=saved[0], headers={"Idempotent-Replay": "true"})
        results = process(event)
        replays.put(idempotency_key, (answer_status(results), results))
    return JSONResponse(results, status_code=answer_status(results))


@app.post("/forms/contact")
def contact_form(name: str = Form(..., max_length=200), email: str = Form(..., max_length=200),
                 phone: str = Form("", max_length=50), message: str = Form("", max_length=10_000),
                 website: str = Form("")):
    """For an HTML form on a website. `website` is a hidden honeypot field: people leave it empty,
    spam bots fill it in, and their submissions are quietly dropped."""
    if website:
        return {"status": "ok"}
    if not _is_email(email):
        raise HTTPException(422, "please enter a valid e-mail address")
    manager = os.environ.get("MANAGER_EMAIL", "")
    event = Event(title=f"Заявка с сайта: {name}", message=message, crm="lead",
                  notify=[manager] if manager else [], reply_to_contact=True,
                  contact=Contact(name=name, email=email, phone=phone or None))
    results = process(event)
    if results.get("crm", {}).get("status") != "created" and results.get("email", {}).get("status") != "sent":
        raise HTTPException(502, "sorry, your request could not be saved - please try again later")
    return {"status": "ok"}


def main() -> None:
    import uvicorn
    uvicorn.run("habar.app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
