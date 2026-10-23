"""Errors returned by the local web console."""

from collections.abc import Iterable

from pydantic import ValidationError


class ConsoleError(Exception):
    """A request failure with an HTTP status and user-facing details."""

    def __init__(self, status: int, message: str, details: Iterable[str] = ()):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = list(details)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def describe_validation_error(exc: ValidationError) -> list[str]:
    """Format validation errors without echoing submitted values."""
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}" if location else error["msg"])
    return details
