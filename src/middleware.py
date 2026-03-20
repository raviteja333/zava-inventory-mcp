from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable, TypeVar


F = TypeVar("F", bound=Callable[..., Any])


class AuthenticationError(PermissionError):
    """Raised when the provided API key is missing or invalid."""


def get_expected_api_key() -> str:
    """Read the expected API key from environment variables."""
    api_key = os.environ.get("ZAVA_API_KEY") or os.environ.get("API_KEY", "")
    if not api_key:
        raise AuthenticationError(
            "API key is not configured. Set ZAVA_API_KEY (preferred) or API_KEY in the environment."
        )
    return api_key


def validate_api_key(api_key: str | None) -> bool:
    """Return True when the supplied API key matches the configured key."""
    if not api_key:
        return False
    return api_key.strip() == get_expected_api_key().strip()


def _extract_api_key_from_context(context: Any | None) -> str | None:
    """Try to get API key from request headers in the injected MCP Context."""
    if context is None:
        return None

    request_context = getattr(context, "request_context", None)
    if request_context is None:
        return None

    request = getattr(request_context, "request", None)
    if request is None:
        return None

    headers = getattr(request, "headers", None)
    if headers is None:
        return None

    # 1) x-api-key header (explicit, safe for simple API key style)
    api_key = headers.get("x-api-key")
    if api_key:
        return api_key

    # 2) Authorization: Bearer <token>
    authorization = headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return None


def require_api_key(func: F) -> F:
    """Decorator for MCP tools that require an API key, optionally from header."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        api_key = kwargs.get("api_key")

        # if the tool has a context arg, try to read from headers
        if not api_key:
            context_controls = [
                kwargs.get("context"),
                kwargs.get("ctx"),
                kwargs.get("request_context"),
            ]
            for context in context_controls:
                api_key = _extract_api_key_from_context(context)
                if api_key:
                    break

        if not validate_api_key(api_key):
            raise AuthenticationError("Unauthorized: invalid or missing API key.")

        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
