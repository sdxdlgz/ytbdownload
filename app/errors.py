from __future__ import annotations

from typing import Any


class AppError(Exception):
    """An expected error that is safe to expose through the API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class WorkerCancelled(Exception):
    """Raised by a worker hook when a cancellation is requested."""


class ResourceLimitExceeded(Exception):
    """Raised when a download crosses a configured hard resource limit."""
