#!/usr/bin/env python3
"""HTTP -> SMTP adapter.

Routes:
  GET  /_info
  POST /send   JSON body: {from?, to, cc?, subject?, body?, html?}
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from urllib.parse import urlsplit

from shim import read_request, send_json, load_backend

HOST = os.environ.get("INTERCEPT_HOST", "")

_INFO = {
    "handler": "smtp",
    "routes": [
        "GET  /_info",
        "POST /send  (JSON: {from?, to, cc?, subject?, body?, html?})",
    ],
}


def send_mail(data: dict) -> dict:
    import smtplib
    from email.message import EmailMessage

    cfg = load_backend(HOST)
    msg = EmailMessage()
    msg["From"] = data.get("from", "curl@intercept.demo")
    to = data["to"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    if data.get("cc"):
        cc = data["cc"]
        msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    msg["Subject"] = data.get("subject", "(no subject)")
    msg.set_content(data.get("body", ""))
    if data.get("html"):
        msg.add_alternative(data["html"], subtype="html")
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.send_message(msg)
    return {
        "sent": True,
        "to": to,
        "subject": msg["Subject"],
        "ui": cfg.get("ui_url", ""),
    }


def route(method: str, path: str, headers: dict, body: bytes) -> None:
    p = urlsplit(path).path.rstrip("/") or "/"

    if method == "GET" and p == "/_info":
        try:
            backend = load_backend(HOST)
        except Exception as e:
            backend = {"error": str(e)}
        return send_json(200, {**_INFO, "host": HOST, "backend": backend},
                         handler="smtp")

    if method == "POST" and p == "/send":
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            return send_json(400, {"error": f"bad JSON body: {e}"}, handler="smtp")
        if "to" not in data:
            return send_json(400, {"error": "missing required field: to"},
                             handler="smtp")
        return send_json(200, send_mail(data), handler="smtp")

    send_json(404, {"error": f"no route for {method} {p}"}, handler="smtp")


def main() -> None:
    try:
        method, path, headers, body = read_request()
    except Exception as e:
        return send_json(400, {"error": f"bad request: {e}"}, handler="smtp")
    try:
        route(method, path, headers, body)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        send_json(500, {"error": f"{type(e).__name__}: {e}"}, handler="smtp")


if __name__ == "__main__":
    main()
