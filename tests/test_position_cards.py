"""Position-card percent P/L and the live exit-plan sentence.

Tickers in this file are synthetic (ZZZ, QQQX) only.
"""
from __future__ import annotations

import copy
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic.config import RuleConfig, Settings
from agentic.domain.enums import OptionType, PositionStatus, Strategy
from agentic.domain.models import Position
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.position_cards import card_pnl_percent, holding_plan, live_profit_pct, pnl_percent


def _rules(
    *,
    profit_pct: float = 0.5,
    profit_on: bool = True,
    profit_approval: bool = False,
    trailing: bool = False,
    trail_gap: float | None = None,
    stop_on: bool = True,
    stop_approval: bool = False,
    loss_mult: float | None = 2.0,
    delta_stop: float | None = 0.5,
    dte_on: bool = True,
    dte_approval: bool = False,
    dte_threshold: int = 2,
    dte_action: str = "close",
    signal_on: bool = True,
    signal_approval: bool = True,
) -> list[RuleConfig]:
    profit_params: dict = {"profit_pct": profit_pct, "trailing": trailing}
    if trail_gap is not None:
        profit_params["trail_gap"] = trail_gap
    return [
        RuleConfig(
            name="profit-target", rule_type="PROFIT_TARGET", enabled=profit_on,
            requires_approval=profit_approval, params=profit_params,
        ),
        RuleConfig(
            name="stop-loss", rule_type="STOP_LOSS", enabled=stop_on,
            requires_approval=stop_approval,
            params={"loss_mult": loss_mult, "delta_stop": delta_stop},
        ),
        RuleConfig(
            name="dte-close", rule_type="DTE", enabled=dte_on,
            requires_approval=dte_approval,
            params={"dte_threshold": dte_threshold, "action": dte_action},
        ),
        RuleConfig(
            name="tv-signal", rule_type="SIGNAL", enabled=signal_on,
            requires_approval=signal_approval, params={"match": "underlying"},
        ),
    ]


_ALL_ON_80 = (
    "Closes at 80% of the credit. "
    "Buys it back if cost reaches 2× the credit or |delta| reaches 0.5. "
    "Buys the option back at 2 days left. "
    "A TradingView signal closes it, with your approval."
)


def test_pnl_percent_gain_loss_and_zero():
    # $27 on a $39 credit is +69%. The same dollars the other way are -69%.
    assert pnl_percent(27, 39) == 69
    assert pnl_percent(-27, 39) == -69
    assert pnl_percent(0, 39) == 0
    assert pnl_percent(-0.0, 39) == 0


def test_pnl_percent_rounds_half_away_from_zero():
    # 0.5, 2.5, and their negatives are the cases Python's round() gets wrong
    # (half to even). This rule sends halves away from zero.
    assert pnl_percent(1, 200) == 1       # 0.5 → 1
    assert pnl_percent(-1, 200) == -1     # -0.5 → -1
    assert pnl_percent(3, 200) == 2       # 1.5 → 2
    assert pnl_percent(-3, 200) == -2     # -1.5 → -2
    assert pnl_percent(5, 200) == 3       # 2.5 → 3, not 2
    assert pnl_percent(-5, 200) == -3     # -2.5 → -3, not -2
    assert pnl_percent(139, 200) == 70    # 69.5 → 70
    assert pnl_percent(-139, 200) == -70  # -69.5 → -70
    assert pnl_percent(69.4, 100) == 69
    assert pnl_percent(-69.4, 100) == -69


@pytest.mark.parametrize(
    "pnl,credit",
    [
        (None, 39),
        (27, None),
        (None, None),
        (27, 0),
        (27, -39),
        (27, -0.01),
        (float("nan"), 39),
        (float("inf"), 39),
        (float("-inf"), 39),
        (27, float("nan")),
        (27, float("inf")),
        (27, float("-inf")),
        (True, 39),
        (27, False),
    ],
)
def test_pnl_percent_missing_or_unusable_credit_is_none(pnl, credit):
    assert pnl_percent(pnl, credit) is None


def test_card_pnl_percent_uses_the_credit_the_card_shows():
    # credit_received 0.39 × 100 × 1 contract = $39, unrealized $27 → +69.
    assert card_pnl_percent(27, 0.39, 1) == 69
    # Quantity is part of that same product: $54 on 2 × $39 = $78 → still +69.
    assert card_pnl_percent(54, 0.39, 2) == 69
    assert card_pnl_percent(-27, 0.39, 1) == -69
    assert card_pnl_percent(0, 0.39, 1) == 0
    assert card_pnl_percent(27, 0, 1) is None
    assert card_pnl_percent(27, -0.39, 1) is None
    assert card_pnl_percent(None, 0.39, 1) is None
    assert card_pnl_percent(27, None, 1) is None
    assert card_pnl_percent(27, 0.39, None) is None


def test_plan_all_on_uses_the_configured_profit_pct():
    text = holding_plan(_rules(profit_pct=0.8))
    assert text == _ALL_ON_80
    assert "50%" not in text
    assert "rolls it for a credit" not in text
    assert "ping" not in text.lower()


def test_plan_stop_loss_off_is_omitted():
    text = holding_plan(_rules(profit_pct=0.8, stop_on=False))
    assert text == (
        "Closes at 80% of the credit. "
        "Buys the option back at 2 days left. "
        "A TradingView signal closes it, with your approval."
    )
    assert "cost reaches" not in text
    assert "|delta|" not in text


def test_plan_one_stop_trigger_off():
    only_loss = holding_plan(_rules(profit_pct=0.8, delta_stop=None))
    assert "Buys it back if cost reaches 2× the credit." in only_loss
    assert "|delta|" not in only_loss

    only_delta = holding_plan(_rules(profit_pct=0.8, loss_mult=None))
    assert "Buys it back if |delta| reaches 0.5." in only_delta
    assert "cost reaches" not in only_delta


def test_plan_dte_uses_threshold_and_real_action():
    assert "Buys the option back at 7 days left." in holding_plan(
        _rules(dte_threshold=7)
    )
    assert "Buys the option back at 1 day left." in holding_plan(
        _rules(dte_threshold=1)
    )
    alert = holding_plan(_rules(dte_action="alert", dte_threshold=2))
    assert "Alerts you at 2 days left and does not buy the option back." in alert
    assert "Buys the option back" not in alert

    other = holding_plan(_rules(dte_action="notify", dte_threshold=5))
    assert (
        "At 5 days left it alerts you (action: notify) and does not buy the option back."
        in other
    )
    assert "Buys the option back" not in other


def test_plan_tv_signal_off_and_without_approval():
    """A disabled tv-signal is left out of the plan.

    That matches SignalProcessor only after PR #13
    (branch cursor/rules-tab-editor-3b4c), which does not match or close when
    the rule is off. On main the processor ignores ``enabled`` and still
    closes. This test does not change that runtime.
    """
    off = holding_plan(_rules(profit_pct=0.8, signal_on=False))
    assert "TradingView" not in off
    assert off == (
        "Closes at 80% of the credit. "
        "Buys it back if cost reaches 2× the credit or |delta| reaches 0.5. "
        "Buys the option back at 2 days left."
    )
    auto = holding_plan(_rules(signal_approval=False, profit_on=False, stop_on=False, dte_on=False))
    assert auto == (
        "A TradingView signal closes it. "
        "A symbol matching more than one open position still asks for approval."
    )


def test_plan_all_off():
    text = holding_plan(_rules(
        profit_on=False, stop_on=False, dte_on=False, signal_on=False,
    ))
    assert text == "No exit rules are on"
    assert holding_plan([]) == "No exit rules are on"
    assert holding_plan(None) == "No exit rules are on"


def test_plan_trailing_profit_target_describes_the_exit():
    text = holding_plan(_rules(
        profit_pct=0.8, trailing=True, trail_gap=0.2,
        stop_on=False, dte_on=False, signal_on=False,
    ))
    assert text == (
        "Arms at 80% of the credit, then buys it back after a 20% give-back "
        "from the peak, floored at 80%."
    )
    # Missing trail_gap uses the rule's 0.20 default. A truthy flag arms the trail.
    flagged = RuleConfig(
        name="profit-target", rule_type="PROFIT_TARGET", enabled=True,
        requires_approval=False,
        params={"profit_pct": 0.8, "trailing": 1},
    )
    assert holding_plan([flagged]) == text


def test_plan_approval_is_not_an_automatic_close():
    profit = holding_plan(_rules(
        profit_pct=0.8, profit_approval=True, stop_on=False, dte_on=False, signal_on=False,
    ))
    assert profit == "Asks for approval to close at 80% of the credit."
    stop = holding_plan(_rules(
        stop_approval=True, loss_mult=2.0, delta_stop=None,
        profit_on=False, dte_on=False, signal_on=False,
    ))
    assert stop == "Asks for approval to buy it back if cost reaches 2× the credit."
    dte = holding_plan(_rules(
        dte_approval=True, dte_threshold=2, dte_action="close",
        profit_on=False, stop_on=False, signal_on=False,
    ))
    assert dte == "Asks for approval to buy the option back at 2 days left."
    trail = holding_plan(_rules(
        profit_pct=0.8, trailing=True, trail_gap=0.15, profit_approval=True,
        stop_on=False, dte_on=False, signal_on=False,
    ))
    assert trail == (
        "Arms at 80% of the credit, then asks for approval to buy it back "
        "after a 15% give-back from the peak, floored at 80%."
    )


def test_plan_keeps_configured_threshold_precision():
    text = holding_plan(_rules(
        profit_pct=0.575, loss_mult=2.25, delta_stop=0.575,
        dte_on=False, signal_on=False,
    ))
    assert text == (
        "Closes at 57.5% of the credit. "
        "Buys it back if cost reaches 2.25× the credit or |delta| reaches 0.575."
    )


def test_plan_dte_matches_dte_rule_exactly():
    # Only the exact string "close" buys back. "Close" does not.
    titled = holding_plan(_rules(
        dte_action="Close", dte_threshold=2,
        profit_on=False, stop_on=False, signal_on=False,
    ))
    assert titled == "At 2 days left it alerts you (action: Close) and does not buy the option back."
    assert "Buys the option back" not in titled

    missing = RuleConfig(
        name="dte-close", rule_type="DTE", enabled=True, requires_approval=False,
        params={"action": "close"},
    )
    assert holding_plan([missing]) == "Buys the option back at 21 days left."

    no_action = RuleConfig(
        name="dte-close", rule_type="DTE", enabled=True, requires_approval=False,
        params={"dte_threshold": 4},
    )
    assert holding_plan([no_action]) == (
        "Alerts you at 4 days left and does not buy the option back."
    )


def test_live_profit_pct_is_unrounded_and_off_when_disabled():
    assert live_profit_pct(_rules(profit_pct=0.575)) == 0.575
    assert live_profit_pct(_rules(profit_on=False)) is None
    bare = RuleConfig(
        name="profit-target", rule_type="PROFIT_TARGET", enabled=True,
        requires_approval=False, params={},
    )
    assert live_profit_pct([bare]) == 0.5


def _pos(underlying: str, credit: float, mark: float | None, *, strike: float) -> Position:
    return Position(
        occ_symbol=f"{underlying}261218P00010000",
        underlying=underlying,
        option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT,
        quantity=1,
        strike=strike,
        expiration=date.today() + timedelta(days=20),
        credit_received=credit,
        current_mark=mark,
        status=PositionStatus.OPEN,
    )


def _client(tmp_path, monkeypatch, settings: Settings, positions: list[Position], *, db_name: str = "cards.db"):
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    db = Database(tmp_path / db_name)
    audit = AuditStore(db)
    store = PositionStore(db)
    for pos in positions:
        store.upsert(pos)
    deps = WebDeps(
        settings=settings,
        signals=SignalStore(db),
        killswitch=KillSwitch(db, audit),
        approval_gate=None,
        audit=audit,
        positions=store,
        orders=OrderStore(db),
        decisions=DecisionStore(db),
    )
    return TestClient(create_app(deps)), settings


def test_api_cards_use_displayed_dollars(tmp_path, monkeypatch):
    settings = Settings(mode="paper", broker="paper", market_data="paper", rules=_rules(profit_pct=0.8))
    client, _settings = _client(tmp_path, monkeypatch, settings, [
        _pos("ZZZ", 0.39, 0.12, strike=10),    # +$27 on $39 → +69%
        _pos("QQQX", 0.39, 0.66, strike=20),   # -$27 on $39 → -69%
    ])
    body = client.get("/api/positions").json()
    assert body["plan"] == _ALL_ON_80
    assert body["profit_pct"] == 0.8
    by = {row["underlying"]: row for row in body["positions"]}
    assert set(by) == {"ZZZ", "QQQX"}

    gain, loss = by["ZZZ"], by["QQQX"]
    assert gain["unrealized_pnl"] == 27
    assert gain["credit_received"] * 100 * gain["quantity"] == 39
    assert gain["pnl_pct"] == 69
    assert loss["unrealized_pnl"] == -27
    assert loss["credit_received"] * 100 * loss["quantity"] == 39
    assert loss["pnl_pct"] == -69
    # The field is the pure helper, not a second formula.
    assert gain["pnl_pct"] == pnl_percent(gain["unrealized_pnl"], 39)
    assert loss["pnl_pct"] == pnl_percent(loss["unrealized_pnl"], 39)


def test_api_bad_credit_or_missing_pnl_is_null(tmp_path, monkeypatch):
    settings = Settings(mode="paper", rules=_rules(profit_on=False, stop_on=False, dte_on=False, signal_on=False))
    client, _settings = _client(tmp_path, monkeypatch, settings, [
        _pos("ZZZ", 0, 0.20, strike=10),       # zero credit
        _pos("QQQX", -0.39, 0.10, strike=20),  # negative credit
    ])
    raw = client.get("/api/positions")
    assert "Infinity" not in raw.text
    assert "NaN" not in raw.text
    by = {row["underlying"]: row for row in raw.json()["positions"]}
    assert by["ZZZ"]["pnl_pct"] is None
    assert by["QQQX"]["pnl_pct"] is None
    assert raw.json()["plan"] == "No exit rules are on"

    # Missing mark → missing unrealized P/L. Same synthetic names, fresh db via tmp_path.
    client2, _settings2 = _client(tmp_path, monkeypatch, settings, [
        _pos("ZZZ", 0.39, None, strike=10),
    ], db_name="missing.db")
    row = client2.get("/api/positions").json()["positions"][0]
    assert row["underlying"] == "ZZZ"
    assert row["unrealized_pnl"] is None
    assert row["pnl_pct"] is None


def test_hot_rules_edit_changes_the_plan_without_restart(tmp_path, monkeypatch):
    """POST /api/config updates settings.rules in this process. The next read
    of the plan uses the new profit target. No new app, no new process."""
    settings = Settings(
        mode="paper", broker="paper", market_data="paper",
        i_understand_live_trading=False, rules=_rules(profit_pct=0.5),
    )
    client, live = _client(tmp_path, monkeypatch, settings, [
        _pos("ZZZ", 0.39, 0.12, strike=10),
        _pos("QQQX", 0.39, 0.66, strike=20),
    ])
    before = client.get("/api/positions").json()
    assert before["plan"] == (
        "Closes at 50% of the credit. "
        "Buys it back if cost reaches 2× the credit or |delta| reaches 0.5. "
        "Buys the option back at 2 days left. "
        "A TradingView signal closes it, with your approval."
    )
    assert {row["underlying"] for row in before["positions"]} == {"ZZZ", "QQQX"}

    rules = copy.deepcopy(live.model_dump(mode="json")["rules"])
    for rule in rules:
        if rule["name"] == "profit-target":
            rule["params"]["profit_pct"] = 0.8
    saved = client.post("/api/config", json={"rules": rules})
    assert saved.status_code == 200, saved.text
    assert saved.json()["ok"] is True

    after = client.get("/api/positions").json()
    assert after["plan"] == _ALL_ON_80
    assert "50%" not in after["plan"]
    assert after["profit_pct"] == 0.8
    assert live.rules[0].params["profit_pct"] == 0.8
    assert live.mode == "paper"
    assert live.i_understand_live_trading is False
    # Money on the cards did not move when the rule changed.
    by = {row["underlying"]: row for row in after["positions"]}
    assert by["ZZZ"]["pnl_pct"] == 69
    assert by["QQQX"]["pnl_pct"] == -69


def test_dashboard_markup_has_fourth_square_and_drops_the_old_plan(tmp_path, monkeypatch):
    client, _settings = _client(
        tmp_path, monkeypatch, Settings(mode="paper", rules=_rules()), [],
    )
    html = client.get("/dashboard").text
    assert "Percent P/L" in html
    assert "p.pnl_pct" in html
    assert "formatPnlPct" in html
    assert "gaugeTargetLeft" in html
    assert "data.profit_pct" in html
    assert 'style="left:75%"' not in html
    assert "data.plan" in html
    assert "repeat(4,1fr)" in html
    assert "rolls it for a credit" not in html
    assert "auto-closes at +50% profit" not in html
    assert "pings you at 2 days" not in html
    assert "function planFor" not in html


def test_format_pnl_pct_runs_in_javascript():
    """Execute the dashboard's formatPnlPct and gaugeTargetLeft, not a copy."""
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None and Path("/exec-daemon/node").exists():
        node = "/exec-daemon/node"
    if node is None:
        pytest.skip("node is not installed")
    src = Path("src/agentic/web/dashboard.py").read_text(encoding="utf-8")
    start = src.index("function formatPnlPct")
    end = src.index("async function loadHolds")
    script = src[start:end] + """
function check(cond, msg){ if(!cond){ console.error(msg); process.exit(1); } }
const gain = formatPnlPct(69);
check(gain.text === "+69%" && gain.cls === "pos-c", "gain");
const loss = formatPnlPct(-69);
check(loss.text === "-69%" && loss.cls === "neg", "loss");
const zero = formatPnlPct(0);
check(zero.text === "0%" && zero.cls === "", "zero");
const missing = formatPnlPct(null);
check(missing.text === "\\u2014" && missing.cls === "muted", "missing");
const bad = formatPnlPct(NaN);
check(bad.text === "\\u2014" && bad.cls === "muted", "nan");
const inf = formatPnlPct(Infinity);
check(inf.text === "\\u2014" && inf.cls === "muted", "inf");
check(gaugeTargetLeft(0.5) === 75, "half");
check(gaugeTargetLeft(0.8) === 90, "eighty");
check(gaugeTargetLeft(null) === null, "off");
"""
    subprocess.run([node, "-e", script], check=True)
