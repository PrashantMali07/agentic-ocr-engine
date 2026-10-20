"""Reject unauthenticated document uploads before multipart body parsing."""

from fastapi import HTTPException
from starlette.responses import JSONResponse

from .auth import authenticate_token


class EarlyDocumentAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not (path.startswith("/extract") or path.startswith("/validate-attachments")
                or path.startswith("/batch-extract")):
            return await self.app(scope, receive, send)
        raw = next((value for key, value in scope.get("headers", []) if key.lower() == b"authorization"), None)
        try:
            if raw is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            scheme, _, token = raw.decode("latin-1").partition(" ")
            if scheme.lower() != "bearer" or not token or any(ch.isspace() for ch in token):
                raise HTTPException(status_code=401, detail="Authentication required")
            authenticate_token(token)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        return await self.app(scope, receive, send)
