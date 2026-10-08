"""
exceptions.py – Custom exception classes used throughout the application.

We define our own hierarchy so that route exception handlers can distinguish
between different error types and return appropriate HTTP status codes and
consistent JSON bodies, without leaking internal details to clients.
"""


class AppError(Exception):
    """Base class for all application-level errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    """Raised when a requested resource does not exist in the database."""


class ConflictError(AppError):
    """Raised when an action is invalid for the resource's current state.

    Example: downloading a certificate that is still PENDING.
    """


class ValidationError(AppError):
    """Raised when request-level data is semantically invalid after Pydantic
    has already parsed the request (e.g. all recipients are invalid).
    """
