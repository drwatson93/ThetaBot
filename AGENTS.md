# AGENTS.md — instructions for AI coding assistants

You are reading this because a human asked you to **set up and run ThetaBot**. This is the agent
runbook. The human-facing guide is `README.md`. Follow the safety rules exactly.

---

## Non-negotiable safety rules — read before doing ANYTHING

ThetaBot **reads** the user's Robinhood account and **sends alerts**. It does not place orders.
`ORDERS_HARD_DISABLED` stays on. A person who trades an alert can still be assigned shares.

1. **NEVER commit, print, echo, paste, or upload secrets.** That means `.env`, `config.yaml`, and
   anything under `data/` — especially `data/rh_oauth.json`. They are git-ignored. Do not `cat`
   them into the chat. Do not add them to a commit. If you are about to run `git add`, verify none
   are staged.
2. **NEVER weaken the order lock.** Do not change `ORDERS_HARD_DISABLED`, bypass
   `assert_mcp_tool_allowed`, or route around the Robinhood write guard
   (`RobinhoodWriteBlocked`). Do not edit `tests/test_orders_hard_disabled.py` to make a write
   pass. Old `mode` and `i_understand_live_trading` keys are ignored.
3. **You cannot do the Robinhood login for them.** `python -m agentic.tools.rh_login` opens a
   browser and pairs with their phone. The human runs it on their desktop. Never ask for their
   Robinhood password or MFA.
4. **Only names the user chooses go on the watchlist.** Selling puts can force them to buy shares.
5. **Do not change scan criteria, risk limits, deltas, or sizing during setup** unless the user
   asks. Get it running as configured.
6. **This is not financial advice.** If they ask "should I trade X" or "will this make money,"
   decline and point them to the README disclaimer.

If you cannot follow one of these, stop and tell the user why.

---

## What ThetaBot is

An alert-only options scanner. It reads Robinhood (read-only), screens cash-secured puts and
covered calls, posts each pick to a webhook, and scores the alert (taken / stale / expired, one
exit webhook, a scorecard row at expiry). It runs as one Docker container. In this edition, market
data comes from that same Robinhood login (`market_data: robinhood` in the example config). No
Alpaca or TradingView account is required.

## Repo map

```
src/agentic/            # the application (package is "agentic")
  services/scanner.py     # screens, sizes, sends scan alerts, scores them
  services/monitor.py     # prices open positions and sends close signals (no orders)
  services/risk_breaker.py# sector cap for the CSP sizer
  marketdata/robinhood_md.py  # this edition's data source (Robinhood reads)
  marketdata/alpaca_md.py     # optional alternative data source
  brokers/robinhood_mcp.py    # read-only Robinhood connection; order lock is in this file
  config.py               # settings and defaults
  web/dashboard.py        # web dashboard and JSON API
config.example.yaml     # copy to config.yaml
.env.example            # copy to .env — dashboard login and tokens
docker-compose.yml      # bind-mounts ./data and ./config.yaml
tests/                  # pytest suite
```

## Setup procedure (follow in order; verify after each step)

**Confirm first:** (a) a Robinhood account with options trading and agentic access; (b) a place to
run it 24/7 (the README walks through a Hostinger VPS); (c) they already ran the desktop login
and have `data/rh_oauth.json`.

1. **Secrets file.**
   ```bash
   cp .env.example .env
   ```
   The user fills in `DASHBOARD_PASSWORD` and `CONTROL_TOKEN` (long random strings). Optional
   `PAUSE_TOKEN` (a *different* long random string) lets a monitor pause via
   `POST /control/pause-only` without receiving `CONTROL_TOKEN`. Other optional keys can stay
   blank. Do **not** print the resulting `.env`.

2. **Robinhood token.** The user runs `python -m agentic.tools.rh_login` **on their desktop**,
   which writes `data/rh_oauth.json`. They copy that file to `./data/` on the server. Verify it
   exists: `ls -l data/rh_oauth.json` — do not print its contents.

3. **Strategy config.**
   ```bash
   cp config.example.yaml config.yaml
   ```
   Confirm these keys. `mode` and `i_understand_live_trading` are not settings; an old file that
   still has them loads and the keys are ignored.
   ```yaml
   market_data: robinhood
   entry:
     enabled: true
     watchlist: [ ... ]        # ONLY names the user is willing to own
   ```

4. **Launch.**
   ```bash
   docker compose up -d --build
   ```

5. **Verify health** (do this, report the result):
   ```bash
   curl -sf http://localhost:8000/health && echo OK
   docker compose logs --tail=50
   ```
   Expect `/health` → `{"status":"ok","orders_hard_disabled":true,...}` plus a `scanner` object,
   and logs showing the broker connected and a scan loop starting. The dashboard is localhost-only:
   `ssh -L 8000:localhost:8000 user@server`, then `http://localhost:8000`.

6. **Run the tests** (optional — proves the clone is coherent):
   ```bash
   pip install -e ".[all]" && python -m pytest -q
   ```

7. **Leave the order lock alone.** Confirm `/health` still reports `orders_hard_disabled: true`.
   Recommend HTTPS in front of the dashboard (README Step 4).

## Choosing a data source (if the user asks)

- **`market_data: robinhood`** — nothing extra. Recommend this. The example config sets it.
- **`alpaca`** — only if the user wants it. Real-time OPRA is a **paid** Alpaca subscription.
  Add `ALPACA_API_KEY` / `ALPACA_API_SECRET` to `.env`, set `market_data: alpaca` and
  `entry.feed: opra`. See README → "Optional integrations."
- **TradingView signals** — optional ADX/%B entry gates. Needs a TradingView plan with webhooks.
  Off by default and fail-open. See README → "Optional integrations."

## Verification and operations

| Goal | Command |
|---|---|
| Is it up? | `curl -sf http://localhost:8000/health` |
| Paused, broker, scanner | `curl -s -u USER:PASS http://localhost:8000/api/ops` |
| Recent logs | `docker compose logs --tail=100 -f` |
| Restart after a config change | `docker compose up -d` |
| Update to latest | `git pull && docker compose up -d --build` |
| Mute new alert webhooks | pause from the dashboard, or `POST /control/pause-only` |

## Common failure modes

| Symptom | Likely cause → fix |
|---|---|
| `/health` fails / container restarts | `docker compose logs`; usually a `.env` or `config.yaml` typo. |
| Banner "degraded" / broker not connected | `data/rh_oauth.json` missing or expired → user re-runs desktop `rh_login` and re-uploads. |
| No alerts | normal if nothing clears the screen; check the decision log, or lower `entry.criteria.min_annualized_yield`. |
| Paused, no webhooks | pause is on → check `/api/ops`. Resume from the dashboard. Unsent alerts stay pending. |

## What NOT to do

- Do not weaken or remove the safety rules above, or the order lock.
- Do not hardcode a secret into source, config, or a commit.
- Do not `git add` `.env`, `config.yaml`, or `data/`.
- Do not change scan criteria, risk limits, or sizing during setup.

When in doubt, ask the user.
