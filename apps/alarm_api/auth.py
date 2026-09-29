"""Bearer token authentication for the simulator.

`GET /health` is deliberately unauthenticated — the Postman collection calls it with no
Authorization header, and a liveness probe that needs a credential is useless to an
orchestrator. Every other route requires the token, and the check is wired as a
**router-level dependency** rather than middleware so that exemption is visible in the
route table instead of being a string comparison buried in a request hook.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from apps.alarm_api.config import get_settings
from apps.alarm_api.deps import SettingsDep

_SCHEME = "bearer"


def require_bearer_token(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject the request unless it carries the configured bearer token.

    The token comes from the settings the *app* was built with (see `deps.get_api_settings`),
    so an app constructed with an explicit settings object authenticates against that object
    rather than against the environment.

    The 401 body uses the same `{"error", "message"}` envelope as every other failure so
    the connector has one shape to parse, and it never echoes the supplied token —
    reflecting a credential into a response is how credentials end up in logs.
    """
    if settings is None:  # pragma: no cover - FastAPI always injects the dependency
        settings = get_settings()

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_credentials",
                "message": "An Authorization: Bearer <token> header is required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != _SCHEME or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_authorization_header",
                "message": "Expected an Authorization header of the form 'Bearer <token>'.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # compare_digest, not ==: constant-time comparison. The token is a demo value, but a
    # timing-safe comparison is the habit worth having in the code a reviewer reads.
    if not hmac.compare_digest(token.strip(), settings.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_token",
                "message": "The supplied bearer token is not valid for this API.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
