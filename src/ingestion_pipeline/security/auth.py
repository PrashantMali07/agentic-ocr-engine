"""Fail-closed service credentials for backend-to-backend API access.

INGESTION_API_KEY_SHA256_JSON maps the SHA-256 digest of a random API key to
its tenant, subject, and roles. Only the key digest is stored in configuration.
"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ROLES = {"extract", "review", "admin"}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject: str
    roles: frozenset[str]


def _credentials() -> dict[str, Principal]:
    raw = os.getenv("INGESTION_API_KEY_SHA256_JSON", "")
    try:
        configured = json.loads(raw)
        if not isinstance(configured, dict) or not configured:
            raise ValueError("missing credentials")
        result = {}
        for digest, value in configured.items():
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("invalid key digest")
            if not isinstance(value, dict):
                raise ValueError("invalid principal")
            tenant = value.get("tenant_id")
            subject = value.get("subject")
            roles = value.get("roles")
            if (
                not isinstance(tenant, str) or not tenant or len(tenant) > 128
                or not isinstance(subject, str) or not subject or len(subject) > 128
                or not isinstance(roles, list) or not roles
                or any(not isinstance(role, str) or role not in _ROLES for role in roles)
            ):
                raise ValueError("invalid principal fields")
            result[digest] = Principal(tenant, subject, frozenset(roles))
        return result
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="API authentication is not configured") from exc


def require_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return authenticate_token(credentials.credentials)


def authenticate_token(token: str) -> Principal:
    allowed = _credentials()
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for digest, principal in allowed.items():
        if hmac.compare_digest(presented, digest):
            return principal
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


def require_extractor(principal: Principal = Depends(require_principal)) -> Principal:
    if not ({"extract", "admin"} & principal.roles):
        raise HTTPException(status_code=403, detail="Extractor role required")
    return principal


def require_reviewer(principal: Principal = Depends(require_principal)) -> Principal:
    if not ({"review", "admin"} & principal.roles):
        raise HTTPException(status_code=403, detail="Reviewer role required")
    return principal


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="Administrator role required")
    return principal
