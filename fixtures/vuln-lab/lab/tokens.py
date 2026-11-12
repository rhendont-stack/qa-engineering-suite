"""Signed visitor tokens: a header, the claims and an HMAC-SHA256 signature, each base64url encoded and joined by dots."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

import settings

LIFETIME_S = 3600


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(text: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


def _sign(signed_part: str) -> str:
    return _encode(hmac.new(settings.SECRET_KEY.encode("utf-8"), signed_part.encode("ascii"), hashlib.sha256).digest())


def issue(subject: str) -> str:
    """A token for ``subject`` that expires after ``LIFETIME_S`` seconds."""
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    claims = _encode(json.dumps({"sub": subject, "exp": int(time.time()) + LIFETIME_S}).encode("utf-8"))
    signed_part = header + "." + claims
    return signed_part + "." + _sign(signed_part)


def verify(token: str) -> Optional[Dict[str, Any]]:
    """The claims of a token that is well formed and not expired, or None."""
    try:
        header_part, claims_part, signature = token.split(".")
        header, claims = _decode(header_part), _decode(claims_part)
    except ValueError:
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    if header.get("alg") == "HS256":
        try:
            if not hmac.compare_digest(_sign(header_part + "." + claims_part), signature):
                return None
        except (TypeError, UnicodeEncodeError):
            return None
    expires = claims.get("exp")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires < time.time():
        return None
    return claims
