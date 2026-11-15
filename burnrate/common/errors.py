"""Domain errors. Every service answers failures as {"error": {"code": ..., "message": ...}}."""
from __future__ import annotations


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"error": err}


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Unauthorized(DomainError):
    status = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class PaymentRequired(DomainError):
    status = 402
    code = "subscription_required"


class RateLimited(DomainError):
    status = 429
    code = "rate_limited"


class Unavailable(DomainError):
    status = 503
    code = "unavailable"
