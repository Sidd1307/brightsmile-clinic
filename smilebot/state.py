"""Client-held conversation state, HMAC-signed so the browser can carry it but not forge it.

Keeping state off the server means any serverless instance can handle any turn, so concurrent
conversations need no shared memory or sticky sessions.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import zlib

_SECRET = os.environ.get("SESSION_SECRET", "dev-only-secret-change-me").encode()


class BadState(ValueError):
    pass


def new_state() -> dict:
    return {
        "v": 1, "stage": "greet", "expecting": None, "slots": {}, "details": {}, "fails": {},
        "offered": [], "pending_email": None, "urgent": False, "transcript": [], "booking": None, "lead_id": None,
    }


def sign(state: dict) -> str:
    body = base64.urlsafe_b64encode(zlib.compress(json.dumps(state, separators=(",", ":")).encode())).decode()
    mac = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def verify(token: str | None) -> dict:
    if not token:
        return new_state()
    try:
        body, mac = token.rsplit(".", 1)
    except ValueError as e:
        raise BadState("malformed") from e
    good = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, good):
        raise BadState("signature mismatch")
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(body)))
