"""Security helpers for panel-facing API endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_panel_token(token: str) -> dict:
    settings = get_settings()

    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token format") from exc

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid token encoding") from exc

    if header.get("alg") != "HS256":
        raise HTTPException(status_code=401, detail="Unsupported token algorithm")

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(
        settings.panel_jwt_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected_sig):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    if payload.get("sub") != settings.panel_jwt_subject:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    return payload


def require_panel_jwt(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_panel_token: str | None = Header(default=None, alias="X-Panel-Token"),
) -> dict:
    token = credentials.credentials if credentials else x_panel_token
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return verify_panel_token(token)

