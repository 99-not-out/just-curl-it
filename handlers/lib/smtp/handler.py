#!/usr/bin/env python3
"""HTTP -> SMTP. Stdlib only.

  POST /send   JSON {from?, to, cc?, subject?, body?, html?}
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from urllib.parse import urlsplit

import jci

ROUTES = [
    "GET  /_info",
    "POST /send   (JSON: {from?, to, cc?, subject?, body?, html?})",
]


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def send_mail(cfg: dict, data: dict) -> dict:
    msg = EmailMessage()
    msg["From"] = data.get("from") or cfg.get("from") or "jci@localhost"
    to = data["to"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    if data.get("cc"):
        cc = data["cc"]
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    msg["Subject"] = data.get("subject", "(no subject)")
    msg.set_content(data.get("body", ""))
    if data.get("html"):
        msg.add_alternative(data["html"], subtype="html")

    host, port = cfg.get("smtp_host", "localhost"), int(cfg.get("smtp_port", 25))
    t = jci.timeout(cfg)
    # Explicit local_hostname: on some macOS systems socket.getfqdn() returns
    # a trailing newline, which recent smtplib refuses to send.
    cls = smtplib.SMTP_SSL if _truthy(cfg.get("ssl")) else smtplib.SMTP
    with cls(host, port, local_hostname="jci", timeout=t) as s:
        if _truthy(cfg.get("starttls")) and cls is smtplib.SMTP:
            s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], cfg.get("password", ""))
        s.send_message(msg)
    return {"sent": True, "to": to, "subject": msg["Subject"], "ui": cfg.get("ui_url", "")}


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path.rstrip("/") or "/"
    if method == "GET" and p == "/_info":
        return jci.info("send", ROUTES)
    if method == "POST" and p == "/send":
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            return jci.send_error(400, f"bad JSON body: {e}")
        if not isinstance(data, dict) or "to" not in data:
            return jci.send_error(400, "missing required field: to")
        try:
            return jci.send_json(200, send_mail(jci.load_backend(), data))
        except (smtplib.SMTPException, OSError) as e:
            return jci.send_error(502, f"{type(e).__name__}: {e}")
    jci.send_error(404, f"no route for {method} {p}")


if __name__ == "__main__":
    jci.main(route)
