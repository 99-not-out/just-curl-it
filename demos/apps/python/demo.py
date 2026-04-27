"""Demo: drive multiple jci-routed backends from a single Python process.

Run this from inside a `jci use demos` shell. Stdlib only — uses urllib.

What this proves: any libc-resolving Python (e.g. Homebrew's python3) gets
the same shim treatment as curl. No client libraries are imported here —
postgres, redis, kafka, mongo, rabbit are all reached over plain HTTP.

Requires brewed Python on macOS:  brew install python
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()


def post(url: str, body: str | bytes, ct: str = "text/plain") -> str:
    if isinstance(body, str):
        body = body.encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": ct, "Content-Length": str(len(body))},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    section("shim meta")
    print(get("http://mylocal.shim/routes"))

    section("postgres")
    q = urllib.parse.quote("select now()::text, version()")
    print(get(f"http://mylocal.postgres/query?sql={q}"))

    section("redis: set + get")
    post("http://mylocal.redis/key/from-python", "hello from python")
    print(get("http://mylocal.redis/key/from-python"))

    section("mongo: insert + find")
    post("http://mylocal.mongo/db/demo/coll/python_users",
         json.dumps({"name": "alice", "lang": "python"}),
         ct="application/json")
    print(get("http://mylocal.mongo/db/demo/coll/python_users?filter=%7B%7D"))

    section("kafka: produce + peek")
    post("http://mylocal.kafka/topic/pydemo/records", "py-key|hello-from-python")
    print(get("http://mylocal.kafka/topic/pydemo/records?limit=5"))

    section("rabbit: publish + get")
    post("http://mylocal.rabbit/queue/pydemo/publish", "hello from python")
    print(get("http://mylocal.rabbit/queue/pydemo/get"))

    section("smtp")
    print(post("http://mylocal.smtp/send",
               json.dumps({"to": "py@example.com",
                           "subject": "hi from python",
                           "body": "sent via jci"}),
               ct="application/json"))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.stderr.write(f"jci-demo: {e}\n")
        sys.stderr.write("are you in `jci use demos`? is `docker compose up -d` running?\n")
        sys.exit(1)
