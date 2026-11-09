"""Bitrix24 REST API through an incoming webhook: create CRM leads and tasks.

The webhook URL contains a secret, so it never appears in errors or logs.
Temporary problems (timeouts, HTTP 5xx, 429) are retried with growing pauses; a refusal
(HTTP 4xx, e.g. a missing field or no access) is reported at once, because retrying won't help.
"""
import time

import httpx


class BitrixError(Exception):
    """Bitrix24 refused the request or could not be reached."""


class Bitrix:
    def __init__(self, webhook: str, responsible_id: int = 1, client: httpx.Client = None,
                 attempts: int = 3, sleep=time.sleep):
        if not webhook:
            raise BitrixError("Bitrix24 is not set up: add BITRIX_WEBHOOK to .env")
        self.webhook = webhook.rstrip("/")
        self.responsible_id = responsible_id
        self.client = client or httpx.Client(timeout=20)
        self.attempts, self.sleep = attempts, sleep

    def call(self, method: str, payload: dict):
        """POST one REST method and return its `result`."""
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.post(f"{self.webhook}/{method}", json=payload)
            except httpx.TransportError as e:  # timeout, DNS, connection reset
                problem = f"not reachable ({type(e).__name__})"
            else:
                if response.status_code < 400:
                    return response.json()["result"]
                if response.status_code != 429 and response.status_code < 500:
                    raise BitrixError(f"{method} refused: {_reason(response)}")
                problem = f"HTTP {response.status_code}"
            if attempt < self.attempts:
                self.sleep(0.5 * 2 ** (attempt - 1))  # 0.5 s, 1 s, 2 s ...
        raise BitrixError(f"{method} failed after {self.attempts} attempts: {problem}")

    def add_lead(self, title: str, comments: str = "", name: str = None, email: str = None,
                 phone: str = None, source: str = "WEB") -> int:
        fields = {"TITLE": title, "COMMENTS": comments, "SOURCE_ID": source, "OPENED": "Y"}
        if name:
            fields["NAME"] = name
        if email:
            fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
        return int(self.call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}))

    def add_task(self, title: str, description: str = "") -> int:
        fields = {"TITLE": title, "DESCRIPTION": description, "RESPONSIBLE_ID": self.responsible_id}
        return int(self.call("tasks.task.add", {"fields": fields})["task"]["id"])


def _reason(response: httpx.Response) -> str:
    try:
        return response.json().get("error_description") or response.json().get("error") or response.reason_phrase
    except ValueError:
        return response.reason_phrase
