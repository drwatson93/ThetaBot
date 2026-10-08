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
* RULES — ``rules`` is editable, but only in place: the same names, rule types, and order.
  Tunable numbers are range-checked. A null stop-loss trigger is off. Mode, live-arming,
  broker, and the hard-coded real-order lock are not rule fields and stay unreachable here.
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


# Identity of a rule is (name, rule_type). The dashboard may tune these fields only.
_ALLOWED_RULE_TYPES = frozenset({"PROFIT_TARGET", "STOP_LOSS", "DTE", "SIGNAL"})
# kind, lo, hi, nullable. Null is meaningful only for the two stop-loss triggers.
_TUNABLE_PARAMS: dict[str, dict[str, tuple[str, float, float, bool]]] = {
    "PROFIT_TARGET": {"profit_pct": ("float", 0.05, 0.95, False)},
    "STOP_LOSS": {
        "loss_mult": ("float", 1.0, 10.0, True),
        "delta_stop": ("float", 0.1, 1.0, True),
    },
    "DTE": {"dte_threshold": ("int", 0, 30, False)},
    "SIGNAL": {},
}


def _check_rule_number(rule_name: str, key: str, value: Any, spec: tuple) -> Any:
    kind, lo, hi, nullable = spec
    if value is None:
        if nullable:
            return None
        raise SettingsEditError(f"{rule_name}: {key} is required.")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsEditError(f"{rule_name}: {key} must be a number.")
    if kind == "int" and not isinstance(value, int):
        raise SettingsEditError(
            f"{rule_name}: {key} must be a whole number from {int(lo)} to {int(hi)}."
        )
    if not (lo <= value <= hi):
        if kind == "int":
            raise SettingsEditError(
                f"{rule_name}: {key} must be a whole number from {int(lo)} to {int(hi)}."
            )
        raise SettingsEditError(f"{rule_name}: {key} must be between {lo} and {hi}.")
    return value


def prepare_rules_update(current: list, submitted: Any) -> list[dict]:
    """Validate a full ``rules`` replacement against the rules that are running.

    The submitted list must be the same rules (same names, types, and order). Omitted
    parameters stay as they are. JSON null on ``loss_mult`` or ``delta_stop`` turns that
    trigger off. An enabled stop-loss with both triggers null is rejected — disable the
    rule instead. Raises SettingsEditError before any value is applied.
    """
    if not isinstance(submitted, list):
        raise SettingsEditError("rules must be a list of the current rules.")
    if len(submitted) != len(current):
        raise SettingsEditError(
            "Cannot add or remove rules. Submit the same rules that are already running."
        )
    current_names = [c.name for c in current]
    prepared: list[dict] = []
    for i, (cur, item) in enumerate(zip(current, submitted)):
        if not isinstance(item, dict):
            raise SettingsEditError(f"rules[{i}] must be an object.")
        if "name" not in item:
            raise SettingsEditError(f"rules[{i}] is missing name.")
        name = item["name"]
        if name != cur.name:
            if name not in current_names:
                raise SettingsEditError(f"Unknown rule name: {name!r}.")
            raise SettingsEditError(
                "Rules must keep the same names, types, and order."
            )
        if "rule_type" not in item:
            raise SettingsEditError(f"{cur.name}: rule_type is required.")
        rtype = item["rule_type"]
        if rtype not in _ALLOWED_RULE_TYPES:
            raise SettingsEditError(f"Unknown rule type: {rtype!r}.")
        if rtype != cur.rule_type:
            raise SettingsEditError(f"Cannot change rule_type for {cur.name!r}.")
        if "enabled" not in item or not isinstance(item["enabled"], bool):
            raise SettingsEditError(f"{cur.name}: enabled must be true or false.")
        enabled = item["enabled"]
        if "requires_approval" in item and item["requires_approval"] != cur.requires_approval:
            raise SettingsEditError(f"Cannot change requires_approval for {cur.name!r}.")
        raw_params = item.get("params", {})
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            raise SettingsEditError(f"{cur.name}: params must be an object.")
        tunable = _TUNABLE_PARAMS[cur.rule_type]
        current_params = dict(cur.params or {})
        for key, val in raw_params.items():
            if key in tunable:
                continue
            if key not in current_params:
                raise SettingsEditError(f"Unknown parameter {key!r} on {cur.name!r}.")
            if current_params[key] != val:
                raise SettingsEditError(f"Cannot change {cur.name} params.{key}.")
        params = dict(current_params)
        for key, spec in tunable.items():
            if key in raw_params:
                params[key] = _check_rule_number(cur.name, key, raw_params[key], spec)
            elif key in params:
                params[key] = _check_rule_number(cur.name, key, params[key], spec)
            elif not spec[3]:
                raise SettingsEditError(f"{cur.name}: {key} is required.")
        if cur.rule_type == "STOP_LOSS" and enabled:
            if params.get("loss_mult") is None and params.get("delta_stop") is None:
                raise SettingsEditError(
                    f"{cur.name} is enabled but both triggers are off. "
                    "Turn the rule off, or leave loss_mult or delta_stop on. "
                    "Null means that trigger is off."
                )
        prepared.append({
            "name": cur.name,
            "rule_type": cur.rule_type,
            "enabled": enabled,
            "requires_approval": cur.requires_approval,
            "params": params,
        })
    return prepared


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
    # Copy so a rejected rules list never mutates the caller's body, and so the
    # validated replacement (not a partial client object) is what gets applied.
    patch = dict(patch)
    if "rules" in patch:
        patch["rules"] = prepare_rules_update(list(settings.rules), patch["rules"])

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


def _named_dict_list(obj: Any) -> bool:
    return (
        isinstance(obj, list)
        and len(obj) > 0
        and all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in obj)
    )


def _leaf_diff(before: Any, after: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Dotted-path map of {old, new} for leaves that actually changed.

    A ``rules`` list is walked per rule name so a CONFIG_EDIT row records each changed
    field (``rules.stop-loss.params.loss_mult``) instead of the whole list.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        out: dict[str, dict[str, Any]] = {}
        for key in set(before) | set(after):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_leaf_diff(before.get(key), after.get(key), path))
        return out
    if (
        prefix == "rules"
        and _named_dict_list(before)
        and _named_dict_list(after)
        and [item["name"] for item in before] == [item["name"] for item in after]
    ):
        out = {}
        for old, new in zip(before, after):
            out.update(_leaf_diff(old, new, f"{prefix}.{old['name']}"))
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
