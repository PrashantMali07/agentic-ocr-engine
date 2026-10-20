"""Reject oversized HTTP bodies before multipart can spool them to disk."""

from starlette.responses import JSONResponse


class _TooLarge(Exception):
    pass


class RequestBodyLimit:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        length = next((v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
        used = 0
        started = False

        async def bounded_receive():
            nonlocal used
            message = await receive()
            if message["type"] == "http.request":
                used += len(message.get("body", b""))
                if used > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except _TooLarge:
            if not started:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
