# habar

**One small API that turns website forms and system events into Bitrix24 CRM leads and tasks, with e-mail notifications.**

[![tests](https://github.com/hFridayy/habar/actions/workflows/tests.yml/badge.svg)](https://github.com/hFridayy/habar/actions/workflows/tests.yml)

A visitor fills in a form on your site, or another system sends
an event, and habar:

1. creates a **lead** (or a **task**) in **Bitrix24 CRM** through its REST API,
2. e-mails the **manager**, with Reply-To set to the client so they can answer in one click,
3. sends the client an **auto-reply** ("we received your request").

## Built for real-world failures

| Problem | What habar does |
|---|---|
| Bitrix24 is slow or returns 5xx / 429 | retries 3 times with growing pauses (0.5 s, 1 s, 2 s) |
| Bitrix24 refuses (missing field, no access) | reports the reason at once, no pointless retries |
| The mail server is down | the lead is still created; the answer says exactly which channel failed (HTTP 207) |
| The caller retries the same request | `Idempotency-Key` header: the same key never creates a second lead |
| Spam bots fill in the website form | hidden honeypot field: bot submissions are dropped silently |
| Secrets in error messages | the webhook URL (it contains a secret) never appears in errors or logs |
| Unauthorized callers | `/events` requires an `X-API-Key`, compared in constant time |

## Quick start

```bash
git clone https://github.com/hFridayy/habarauto && cd habar
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in the Bitrix24 webhook, SMTP settings and an API key
habar                     # http://127.0.0.1:8000/docs - interactive API documentation
```

Or with Docker: `docker build -t habar . && docker run --env-file .env -p 8000:8000 habar`

**Bitrix24 webhook:** in your portal, Developer resources → Other → Incoming webhook, with the `crm` and `task`
scopes. Copy the URL into `BITRIX_WEBHOOK`. Some Bitrix24 plans limit REST access; a free trial of a paid plan works.

**E-mail:** use an app password (Gmail: Google Account → Security → App passwords), never your main password.

## API

```bash
curl -X POST http://127.0.0.1:8000/events \
  -H "X-API-Key: $HABAR_API_KEY" -H "Idempotency-Key: order-555" -H "Content-Type: application/json" \
  -d '{"title": "Новый клиент", "message": "Хочу демо",
       "notify": ["manager@company.kz"], "reply_to_contact": true,
       "contact": {"name": "user name", "email": "username@example.com", "phone": "+7700xxxxxxx"}}'
```
```json
{"crm": {"status": "created", "type": "lead", "id": 101},
 "email": {"status": "sent", "to": ["manager@company.kz"]},
 "auto_reply": {"status": "sent", "to": "usernameim@example.com"}}
```

| Endpoint | Purpose |
|---|---|
| `POST /events` | any system: `crm` = `lead` (default), `task` or `none`; e-mail anyone in `notify` |
| `POST /forms/contact` | an HTML form on a website (example: [examples/contact-form.html](examples/contact-form.html)) |
| `GET /health` | health check for monitoring and load balancers |

Answers: **200** everything worked, **207** some channels failed (see each one's `status`), **502** all failed.

## Tests

`pytest -q`: 12 tests run against a fake Bitrix24 portal and a fake mail server, covering retries, refusals,
partial failures, idempotency, authentication, validation and spam. No real accounts needed.

## Notes

- Leads use `crm.lead.add`, the method from Bitrix24's web-form guides; Bitrix24 now recommends `crm.item.add`
  for new code, which is a planned switch.
- Idempotency keys are kept in memory (the last 1,000). With several server instances, move them to Redis.

## License

MIT
