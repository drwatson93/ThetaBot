"""HTTP Basic Auth gate for the dashboard + data/control endpoints.

Two logins, one Basic realm:

* **owner** — ``DASHBOARD_USER`` / ``DASHBOARD_PASSWORD``. Full dashboard, including every
  mutation. No extra token prompt in the browser.
* **viewer** — ``VIEWER_USER`` / ``VIEWER_PASSWORD`` (optional; unset = disabled). GET pages
  and ``/api/*`` JSON reads only. Every non-GET (except ``/control/pause-only``) returns 403.

``CONTROL_TOKEN`` remains an alternative credential for scripted owner calls (query
``?token=``). ``PAUSE_TOKEN`` is unrelated: it only authorizes ``POST /control/pause-only``.

Auth is fail-closed when ``DASHBOARD_PASSWORD`` is unset or a placeholder. Username defaults
to ``admin``. Comparisons are constant-time.

Not applied to: ``/health``, ``/webhook/tradingview`` (shared-secret; viewers still 403),
UUID-guarded approve/reject (per-decision HMAC; viewers still 403), or
``/control/pause-only`` (PAUSE_TOKEN only).
"""
from __future__ import annotations

import hmac
from typing import Literal

from fastapi import Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..config import get_secret, is_usable_secret

_basic = HTTPBasic(auto_error=False)

Role = Literal["owner", "viewer"]
OWNER: Role = "owner"
VIEWER: Role = "viewer"


def auth_enabled() -> bool:
    return is_usable_secret(get_secret("DASHBOARD_PASSWORD"))


def viewer_enabled() -> bool:
    """True only when both VIEWER_USER and a real VIEWER_PASSWORD are set."""
    user = (get_secret("VIEWER_USER") or "").strip()
    return bool(user) and is_usable_secret(get_secret("VIEWER_PASSWORD"))


def owner_username() -> str:
    return (get_secret("DASHBOARD_USER", "admin") or "admin").strip() or "admin"


def viewer_username() -> str:
    return (get_secret("VIEWER_USER") or "").strip()


def control_token_ok(token: str | None) -> bool:
    expected = get_secret("CONTROL_TOKEN")
    if not is_usable_secret(expected):
        return False
    return bool(token) and hmac.compare_digest(token, expected)


def _match(credentials: HTTPBasicCredentials | None, user: str, password: str | None) -> bool:
    if credentials is None or not user or not is_usable_secret(password):
        return False
    return hmac.compare_digest(credentials.username, user) and hmac.compare_digest(
        credentials.password, password
    )


def identify_role(credentials: HTTPBasicCredentials | None) -> Role | None:
    if _match(credentials, owner_username(), get_secret("DASHBOARD_PASSWORD")):
        return OWNER
    if viewer_enabled() and _match(credentials, viewer_username(), get_secret("VIEWER_PASSWORD")):
        return VIEWER
    return None


def _locked() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Dashboard is locked: DASHBOARD_PASSWORD is not configured",
        headers={"WWW-Authenticate": "Basic"},
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


def _viewer_forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="view-only login cannot change state",
    )


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> Role:
    """FastAPI dependency: owner or viewer Basic auth. Returns the role."""
    if not auth_enabled():
        raise _locked()
    role = identify_role(credentials)
    if role is None:
        raise _unauthorized()
    return role


def deny_viewer(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> Role | None:
    """403 if the client authenticated as viewer; otherwise return the role or None.

    Used on mutating routes that have their own token (webhook, one-tap approve) so a
    view-only login cannot sneak through, while unauthenticated token holders still work.
    """
    role = identify_role(credentials)
    if role == VIEWER:
        raise _viewer_forbidden()
    return role


def require_owner(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    token: str | None = Query(default=None),
    x_control_token: str | None = Header(default=None, alias="X-Control-Token"),
) -> Role:
    """Owner Basic **or** CONTROL_TOKEN. Viewer is always 403."""
    role = identify_role(credentials)
    if role == VIEWER:
        raise _viewer_forbidden()
    if role == OWNER:
        return OWNER
    if control_token_ok(token) or control_token_ok(x_control_token):
        return OWNER
    if not auth_enabled():
        raise _locked()
    raise _unauthorized()
