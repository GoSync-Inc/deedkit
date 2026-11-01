"""One error shape for everything the service itself answers."""

from starlette.responses import JSONResponse


class ServiceError(Exception):
    """An answer the service chose: the HTTP status, a machine code and a short message."""

    def __init__(self, status, code, message, *, retry_after=None, headers=None, close=False):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.retry_after, self.close = retry_after, close
        self.headers = dict(headers or {})

    def response(self):
        headers = dict(self.headers)
        if self.retry_after is not None:
            headers["Retry-After"] = str(max(1, int(self.retry_after + 0.999)))
        if self.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if self.close:
            headers["Connection"] = "close"
        return JSONResponse(
            {"error": {"code": self.code, "message": self.message}}, status_code=self.status, headers=headers
        )


class ConfigError(Exception):
    """The service refuses to start; the text is a machine reason, never a value from a file."""


class PolicyError(Exception):
    """The key policy cannot be used; the text is a machine reason."""
