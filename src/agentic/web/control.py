"""Control endpoints: the kill switch and owner settings.

  POST /control/pause                    -> engage the kill switch (Basic + CONTROL_TOKEN)
  POST /control/pause-only               -> engage the kill switch (PAUSE_TOKEN only)
  POST /control/resume                   -> release it (Basic + CONTROL_TOKEN)
  POST /control/alerts-mode              -> set instant|regular|off (Basic + CONTROL_TOKEN)
  POST /control/test-alert               -> send one test webhook (Basic + CONTROL_TOKEN)
  GET  /control/status                   -> current control state

Owner mutations (pause/resume/alerts/…) accept owner Basic **or** CONTROL_TOKEN.
``/control/pause-only`` is the exception: it skips dashboard login so a monitoring bot
can fail-closed pause with PAUSE_TOKEN, which cannot resume or change alerts.
View-only login gets 403 on every mutation. Approve, close, and roll actions are
not on this router.
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..config import get_secret, is_usable_secret
from ..domain.enums import AuditEventType
from .auth import require_auth, require_owner

if TYPE_CHECKING:
    from .app import WebDeps

log = logging.getLogger("agentic.web.control")

# Failed PAUSE_TOKEN guesses, per client host. Successful pauses are never delayed.
_PAUSE_FAIL_MAX = 10
_PAUSE_FAIL_WINDOW_S = 60.0
_pause_fail_times: dict[str, list[float]] = {}


def _pause_token_authorized(token: str | None) -> bool:
    expected = get_secret("PAUSE_TOKEN")
    if not is_usable_secret(expected):
        return False
    return bool(token) and hmac.compare_digest(token, expected)


def _pause_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _pause_auth_rate_limited(key: str) -> bool:
    now = time.monotonic()
    times = [t for t in _pause_fail_times.get(key, []) if now - t < _PAUSE_FAIL_WINDOW_S]
    _pause_fail_times[key] = times
    return len(times) >= _PAUSE_FAIL_MAX


def _record_pause_auth_fail(key: str) -> None:
    _pause_fail_times.setdefault(key, []).append(time.monotonic())


def make_control_router(deps: "WebDeps") -> APIRouter:
    router = APIRouter(prefix="/control")

    @router.post("/pause", dependencies=[Depends(require_owner)])
    async def pause(reason: str = "manual") -> JSONResponse:
        deps.killswitch.pause(reason)
        return JSONResponse({"status": "paused", "reason": reason})

    @router.post("/pause-only")
    async def pause_only(
        request: Request,
        token: str | None = None,
        reason: str = "pause_token",
        x_pause_token: str | None = Header(default=None, alias="X-Pause-Token"),
    ) -> JSONResponse:
        """Engage the kill switch using PAUSE_TOKEN. No dashboard login, no resume."""
        client = _pause_client_key(request)
        if _pause_auth_rate_limited(client):
            return JSONResponse({"status": "rate_limited"}, status_code=429)
        provided = token or x_pause_token
        if not _pause_token_authorized(provided):
            _record_pause_auth_fail(client)
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        why = (reason or "pause_token").strip() or "pause_token"
        if deps.killswitch.is_paused():
            return JSONResponse({
                "status": "paused",
                "reason": deps.killswitch.reason(),
                "already_paused": True,
            })
        deps.killswitch.pause(why, source="pause_token")
        return JSONResponse({"status": "paused", "reason": why, "already_paused": False})

    @router.post("/resume", dependencies=[Depends(require_owner)])
    async def resume(reason: str = "manual") -> JSONResponse:
        deps.killswitch.resume(reason)
        return JSONResponse({"status": "resumed", "reason": reason})

    @router.post("/alerts-mode", dependencies=[Depends(require_owner)])
    async def alerts_mode(mode: str = "instant") -> JSONResponse:
        """Owner-only: persist alerts mode (instant | regular | off). Viewer / PAUSE_TOKEN cannot."""
        from ..notify.trade_alerts import VALID_MODES, alerts_from_deps
        wanted = (mode or "").strip().lower()
        if wanted not in VALID_MODES:
            return JSONResponse(
                {"ok": False, "error": "mode must be instant, regular, or off",
                 "valid": list(VALID_MODES)},
                status_code=400,
            )
        al = alerts_from_deps(deps)
        al.set_mode(wanted, source="control")
        return JSONResponse({"ok": True, "alerts": al.status()})

    @router.post("/test-alert", dependencies=[Depends(require_owner)])
    async def test_alert() -> JSONResponse:
        """Owner-only: send one clearly labeled test webhook. Viewer / PAUSE_TOKEN cannot."""
        from ..notify.trade_alerts import alerts_from_deps
        al = alerts_from_deps(deps)
        result = await al.send_test()
        body = {"ok": result["ok"], "alerts": al.status()}
        if result.get("error"):
            body["error"] = result["error"]
        return JSONResponse(body)

    @router.post("/test-notify", dependencies=[Depends(require_owner)])
    async def test_notify() -> JSONResponse:
        """Send a test push through the bot's own notifier — verifies phone delivery end-to-end."""
        if deps.notifier is None:
            return JSONResponse({"ok": False, "error": "no notifier configured"}, status_code=400)
        try:
            await deps.notifier.send(
                "AgenticRobinhood test",
                "Notifications are working. You'll get pings like this when the bot acts, "
                "plus a daily digest and a weekly report.",
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "sent": True})

    @router.post("/test-ai", dependencies=[Depends(require_owner)])
    async def test_ai() -> JSONResponse:
        """Verify the AI reviewer end-to-end: build a client from the CURRENT env and make one real
        Opus call on a synthetic candidate. Confirms ANTHROPIC_API_KEY loads + the model responds."""
        from ..ai.client import build_reviewer_client
        from ..ai.reviewer import AIReviewer

        cfg = deps.settings.ai
        client = build_reviewer_client(cfg)
        if client is None:
            return JSONResponse(
                {"ok": False, "enabled": cfg.enabled,
                 "error": "AI client unavailable — check ai.enabled and that ANTHROPIC_API_KEY is "
                          "set in the environment (a redeploy is needed after adding it)."},
                status_code=200,
            )
        result = await AIReviewer(cfg, client).selftest()
        return JSONResponse(result, status_code=200)

    @router.post("/preview-weekly", dependencies=[Depends(require_owner)])
    async def preview_weekly() -> JSONResponse:
        """Render the exact weekly report the Friday push would send (trailing 7-day window +
        cumulative + AI narrative) WITHOUT sending it — for previewing on demand."""
        from ..services.reporting import render_weekly

        title, body = await render_weekly(
            deps.settings, deps.positions, deps.orders, deps.decisions)
        return JSONResponse({"title": title, "body": body})

    @router.post("/purge-stale", dependencies=[Depends(require_owner)])
    async def purge_stale() -> JSONResponse:
        """One-time cleanup of P&L noise (reconcile-wipe / re-entry-overwrite artifacts + demo
        seeds). Safe + idempotent: only removes non-open rows with no real outcome."""
        pos = deps.positions.purge_stale()
        jrn = deps.trade_journal.purge_incomplete() if deps.trade_journal is not None else 0
        deps.audit.record(
            AuditEventType.RECONCILE,
            {"purge_stale": True, "positions_removed": pos, "journal_removed": jrn},
            source="control",
        )
        return JSONResponse({"ok": True, "purged_positions": pos, "purged_journal": jrn})

    @router.post("/heal-decision/{decision_id}", dependencies=[Depends(require_owner)])
    async def heal_decision(decision_id: str, status: str = "DONE") -> JSONResponse:
        """Admin override for an entry decision's status. For records the reconcile self-heal
        can't reach — a false FAILED whose position already closed before the heal shipped
        (e.g. ONDS260731P00007500). Flips the DB status only; never places an order. Auth: owner
        Basic or CONTROL_TOKEN (no per-decision token needed since nothing executes)."""
        if deps.entry_decisions is None:
            return JSONResponse({"ok": False, "error": "no entry_decisions store"}, status_code=503)
        try:
            from ..domain.enums import DecisionStatus
            target = DecisionStatus(status.upper())
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": f"invalid status {status!r}",
                 "valid": [s.value for s in DecisionStatus]},
                status_code=400,
            )
        d = deps.entry_decisions.get(decision_id)
        if d is None:
            return JSONResponse({"ok": False, "status": "not_found"}, status_code=404)
        old = d.status.value
        deps.entry_decisions.set_status(decision_id, target)
        deps.audit.record(
            AuditEventType.DECISION,
            {"heal": True, "occ": d.occ_symbol, "from": old, "to": target.value},
            source="control", decision_id=decision_id,
        )
        return JSONResponse({"ok": True, "occ": d.occ_symbol, "from": old, "to": target.value})

    @router.get("/mcp-tools", dependencies=[Depends(require_auth)])
    async def mcp_tools(tool: str | None = None) -> JSONResponse:
        """Diagnostic: the tool list the Robinhood MCP exposed at last connect + resolved roles.
        Pass ``?tool=a,b`` to get those tools' full description + input schema (to plan wiring).
        Read-only — surfaces which RH capabilities are available (technicals, options historicals,
        earnings, financials, Level II, tax lots…)."""
        broker = getattr(deps.scanner, "broker", None) if deps.scanner is not None else None
        if broker is None:
            return JSONResponse({"ok": False, "error": "no broker on scanner"}, status_code=503)
        tools = list(getattr(broker, "_tools", None) or [])
        defs = getattr(broker, "_tool_defs", None) or {}
        if tool:
            want = [t.strip() for t in tool.split(",") if t.strip()]
            return JSONResponse({
                "ok": True,
                "schemas": {n: defs.get(n, {"error": "not found"}) for n in want},
            })
        return JSONResponse({
            "ok": True,
            "broker": broker.capabilities().name,
            "note": "tools as of the last broker connect (startup)",
            "tool_count": len(tools),
            "tools": sorted(tools),
            "resolved_roles": getattr(broker, "_roles", None) or {},
        })

    @router.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict:
        from ..notify.trade_alerts import alerts_from_deps
        return {
            "paused": deps.killswitch.is_paused(),
            "reason": deps.killswitch.reason(),
            "mode": deps.settings.mode,
            "live_armed": deps.settings.is_live,
            "alerts": alerts_from_deps(deps).status(),
        }

    @router.get("/broker-status", dependencies=[Depends(require_auth)])
    async def broker_status() -> JSONResponse:
        """Definitive read of the ACTIVE broker: name (paper vs robinhood_mcp), whether it's the
        real account, and a live buying-power / open-position read (which exercises the RH
        connection — a read_error here means OAuth/connection is broken)."""
        broker = getattr(deps.scanner, "broker", None) if deps.scanner is not None else None
        if broker is None:
            return JSONResponse({"ok": False, "error": "no broker on scanner"}, status_code=503)
        caps = broker.capabilities()
        out: dict = {
            "ok": True,
            "broker": caps.name,
            "is_paper": caps.is_paper,
            "supports_options_orders": caps.supports_options_orders,
            "notes": getattr(caps, "notes", None),
        }
        try:
            out["buying_power"] = await broker.get_buying_power()
            out["open_positions"] = len(await broker.get_open_positions())
        except Exception as exc:  # noqa: BLE001 — surface the live-read failure verbatim
            out["read_error"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse(out)

    return router
