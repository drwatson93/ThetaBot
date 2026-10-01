"""In-app settings editor — read and safely edit strategy/paper params from the dashboard.

  GET  /api/config   -> current effective config, plus which knobs come from the overlay
  POST /api/config   -> owner-only (owner Basic **or** CONTROL_TOKEN): validate, hot-apply, persist

Design constraints (see docs/STAGE2_PLAN.md):

* HARD GUARDRAIL — the editor may only touch strategy/paper params. It can NEVER change
  ``mode``, ``i_understand_live_trading``, ``broker``, ``broker_fallback``, ``market_data``,
  ``robinhood`` or ``web``. Arming live stays a deliberate file/env action, never a button.
  Code-level locks (real-orders HARD DISABLED, limit-only in the executor) are not Settings
  fields and cannot be changed by any login.
* HOT-APPLY IN PLACE — services share one ``Settings`` object by reference, and ``RiskSizer``
  captures ``settings.entry.sizing`` by reference at init. So edits mutate nested *leaf* fields
  in place and never replace a sub-model, or the running sizer would keep a stale reference.
* PERSISTED to the writable overlay (``config.OVERLAY_PATH``), not the read-only mounted base,
  so edits survive a redeploy.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..config import (
    LOCKED_RUNTIME_PATHS, Settings, load_overlay, save_overlay, strip_locked_overlay,
)
from ..domain.enums import AuditEventType
from .auth import require_auth, require_owner

if TYPE_CHECKING:
    from .app import WebDeps

log = logging.getLogger("agentic.web.settings")

# Allowlist (default-deny): only these top-level keys may be edited via the API. Everything else —
# notably mode, i_understand_live_trading, broker, broker_fallback, market_data, robinhood, web —
# is immutable at runtime.
EDITABLE_TOP_LEVEL = frozenset({
    "paper_buying_power",
    "paper_seed_positions",
    "poll_interval_seconds",
    "poll_interval_closed_seconds",
    "reconcile_interval_seconds",
    "approval_timeout_seconds",
    "max_quote_age_seconds",
    "auto_trip_after_errors",
    "trading_start",
    "execution",
    "entry",
    "macro",
    "ai",
    "news",
    "roll",
    "reporting",
    "tax_reserve",
    "notify",
    "rules",
})


class SettingsEditError(ValueError):
    """Raised when a patch is rejected (protected key or invalid value)."""


def _locked_paths_in(obj: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if not isinstance(obj, dict):
        return found
    for key, val in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if any(path == locked or path.startswith(locked + ".") for locked in LOCKED_RUNTIME_PATHS):
            found.append(path)
            continue
        if isinstance(val, dict):
            found.extend(_locked_paths_in(val, path))
    return found


def _reject_protected(patch: dict) -> None:
    bad = [k for k in patch if k not in EDITABLE_TOP_LEVEL]
    locked = _locked_paths_in(patch)
    blocked = sorted(set(bad) | set(locked))
    if blocked:
        raise SettingsEditError(
            f"These settings are not editable at runtime: {', '.join(blocked)}. "
            "mode / live-arming / broker / account changes must be made "
            "deliberately in the mounted config or environment."
        )


def _apply_in_place(live: BaseModel, validated: BaseModel, patch: dict) -> None:
    """Copy patched leaves from ``validated`` onto ``live`` IN PLACE.

    Recurses into sub-models rather than replacing them, so references captured elsewhere
    (e.g. RiskSizer's ``settings.entry.sizing``) see the new values. Unknown extras (not
    Settings fields) are skipped — they cannot disable code-level locks.
    """
    for key, val in patch.items():
        if not hasattr(live, key):
            continue
        cur = getattr(live, key)
        if isinstance(val, dict) and isinstance(cur, BaseModel):
            _apply_in_place(cur, getattr(validated, key), val)
        else:
            setattr(live, key, getattr(validated, key))


def apply_patch(settings: Settings, patch: dict, *, overlay_path=None) -> list[str]:
    """Validate, hot-apply (in place), and persist a settings patch. Returns changed top keys.

    Raises SettingsEditError on a protected key or a value that fails Settings validation.
    """
    if not isinstance(patch, dict) or not patch:
        raise SettingsEditError("Request body must be a non-empty object of settings to change.")
    _reject_protected(patch)

    from ..config import _deep_merge  # local import to avoid a public surface for the helper

    merged = _deep_merge(settings.model_dump(), patch)
    try:
        validated = Settings.model_validate(merged)
    except ValidationError as exc:
        raise SettingsEditError(f"Invalid setting value(s): {exc.errors()}") from exc

    _apply_in_place(settings, validated, patch)

    overlay = _deep_merge(load_overlay(overlay_path), patch)
    overlay, stripped = strip_locked_overlay(overlay)
    if stripped:
        log.warning("Dropped locked overlay keys on save: %s", ", ".join(stripped))
    save_overlay(overlay, overlay_path)
    log.info("Settings edited via API: %s", sorted(patch))
    return sorted(patch)


def _leaf_diff(before: Any, after: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Dotted-path map of {old, new} for leaves that actually changed."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: dict[str, dict[str, Any]] = {}
        for key in set(before) | set(after):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_leaf_diff(before.get(key), after.get(key), path))
        return out
    if before != after:
        return {prefix: {"old": before, "new": after}}
    return {}


def overlay_source_paths(overlay: dict | None = None) -> list[str]:
    """Dotted paths of values that come from the data-disk overlay (override config.yaml)."""
    ov = load_overlay() if overlay is None else overlay
    ov, _ = strip_locked_overlay(ov)
    return _overlay_leaf_paths(ov)


def _overlay_leaf_paths(obj: Any, prefix: str = "") -> list[str]:
    if not isinstance(obj, dict):
        return [prefix] if prefix else []
    if not obj:
        return [prefix] if prefix else []
    out: list[str] = []
    for key, val in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            out.extend(_overlay_leaf_paths(val, path))
        else:
            out.append(path)
    return out


def _config_payload(settings: Settings, *, role: str | None = None) -> dict[str, Any]:
    overlay, _ = strip_locked_overlay(load_overlay())
    body: dict[str, Any] = {
        "editable": _editable_view(settings),
        # Read-only context so the UI can SHOW (never edit) the safety-critical state.
        "readonly": {
            "mode": settings.mode,
            "live_armed": settings.is_live,
            "broker": settings.broker,
            "broker_fallback": settings.broker_fallback,
            "market_data": settings.market_data,
            "account_number": settings.robinhood.account_number,
        },
        "from_overlay": overlay_source_paths(overlay),
        "overlay": overlay,
    }
    if role is not None:
        body["role"] = role
    return body


def _editable_view(settings: Settings) -> dict[str, Any]:
    dump = settings.model_dump(mode="json")
    return {k: dump[k] for k in EDITABLE_TOP_LEVEL if k in dump}


def make_settings_router(deps: "WebDeps") -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/config")
    async def get_config(role: str = Depends(require_auth)) -> dict:
        return _config_payload(deps.settings, role=role)

    @router.post("/config")
    async def post_config(
        patch: dict = Body(...),
        role: str = Depends(require_owner),
    ) -> JSONResponse:
        """Owner-only: owner Basic or CONTROL_TOKEN. Viewer and PAUSE_TOKEN cannot."""
        try:
            before = deps.settings.model_dump(mode="json")
            old_watchlist = list(deps.settings.entry.watchlist or [])
            changed = apply_patch(deps.settings, patch)
            after = deps.settings.model_dump(mode="json")
            new_watchlist = list(deps.settings.entry.watchlist or [])
            deps.audit.record(
                AuditEventType.CONFIG_EDIT,
                {
                    "who": "owner",
                    "changed": changed,
                    "values": _leaf_diff(before, after),
                    "watchlist": {"old": old_watchlist, "new": new_watchlist},
                },
                source="dashboard",
            )
            body = _config_payload(deps.settings, role=role)
            body["ok"] = True
            body["changed"] = changed
            return JSONResponse(body)
        except SettingsEditError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return router
