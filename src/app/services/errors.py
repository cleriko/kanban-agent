from __future__ import annotations


class ServiceError(Exception):
    """Base for errors that map cleanly onto HTTP status codes."""

    status = 500
    code = "internal"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


class NotFound(ServiceError):
    status = 404
    code = "not_found"


class Invalid(ServiceError):
    status = 400
    code = "bad_request"


class Conflict(ServiceError):
    status = 409
    code = "conflict"


class Unavailable(ServiceError):
    status = 503
    code = "unavailable"
