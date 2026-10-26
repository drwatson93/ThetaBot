# ThetaBot

**An options-*wheel* scanner that runs off your own Robinhood account — one login, one small server, no paid data feeds.**

It screens cash-secured puts on stocks you'd be happy to own and covered calls on shares you already hold, then sends an alert. It does not place the order. This is the **community edition**: all market data comes from the *same* Robinhood connection, so you don't need an Alpaca subscription, a TradingView Pro plan, or anything else.

---

> ## ⚠️ Read this before anything else
>
> - **This build scans and sends alerts. It does not place orders.** `ORDERS_HARD_DISABLED` stays on. An old config that still says `mode: live` is ignored. A human who trades an alert can still be assigned.
> - **Selling puts means you can be assigned** — i.e. *forced to buy 100 shares per contract* at the strike. Only ever put names on your watchlist that you are genuinely willing to own, in sizes you can afford.
> - **Options carry real, sometimes total, loss risk**, including sharp overnight/gap losses. A wheel is *not* a guaranteed-income machine.
> - **This is not financial advice**, not a solicitation, and comes with **no warranty of any kind**. You run it entirely at your own risk.
> - **Not affiliated with, endorsed by, or supported by Robinhood.** It uses Robinhood's agentic connection; you are responsible for complying with Robinhood's Terms of Service. Robinhood can change or revoke that access at any time.
> - **Watch the alerts before you act on them.** The bot will not enter or close a position for you.
>
> If you are not comfortable reading logs, using SSH, and losing every dollar in the account, **do not run this.**

---

## Table of contents
1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [What you need](#what-you-need)
4. [Setup overview](#setup-overview)
5. [Step 1 — Robinhood account + one-time login](#step-1--robinhood-account--one-time-login)
6. [Step 2 — Spin up a Hostinger VPS](#step-2--spin-up-a-hostinger-vps)
7. [Host on Render (read-only scanner)](#host-on-render-read-only-scanner)
8. [Step 3 — Install & deploy](#step-3--install--deploy)
9. [Step 4 — First run](#step-4--first-run)
10. [Configuration reference](#configuration-reference)
11. [Accessing your dashboard securely](#accessing-your-dashboard-securely)
12. [Optional integrations (Alpaca / TradingView)](#optional-integrations-for-different-setups)
13. [Operating the bot](#operating-the-bot)
14. [Safety & risk controls](#safety--risk-controls)
15. [Troubleshooting](#troubleshooting)
16. [Disclaimer & license](#disclaimer--license)

---

## What it does

Reads Robinhood (quotes, chains, positions, buying power) and scans for cash-secured puts and covered calls. Each new pick is posted to `TRADE_ALERT_URL` when alerts mode is `instant`. Each open alert is marked taken, stale, or expired; a held short gets one exit webhook at 50% of the credit by the halfway point, or at 80% at any time; expiry P&L lands on the scorecard. It never places an order. `ORDERS_HARD_DISABLED` stays on.

A person who acts on an alert can still be assigned. Use 7–14 day expirations and only names they will own.

## How it works

| Component | Job |
|---|---|
| **Scanner** | During the scan window: screen, size, and POST one alert per new CSP or covered call. |
| **Score** | Mark each open alert taken, stale, or expired. One exit webhook per held short. One scorecard row is created with the alert and graded after expiry. |
| **Monitor** | Price positions already on the account and send a close signal. It does not submit an order. |
| **Dashboard** | Password-protected page: health, positions, the decision log, setups, the weekly brief, and config edits. |

One Docker container. State is a SQLite file plus the login token on a persistent volume. Chains, quotes, greeks, IV, open interest, and daily bars come from the Robinhood login (`market_data: robinhood` in the example config). No separate data subscription. Pause mutes new alert webhooks.

## What you need

- **A Robinhood account** with **options trading enabled** and **agentic access** set up (a primary account + the agentic sub-account — see Step 1). Options Level 2 or higher is required to sell cash-secured puts.
- **A VPS** to run it 24/7. This guide uses **Hostinger** (Step 2). The bot is lightweight — the smallest KVM plan is plenty.
- **A desktop computer** for the one-time Robinhood login (the login flow needs a browser + your phone).
- Basic comfort with **SSH and the command line**.
- **~30–45 minutes** for first-time setup.

You do **not** need: an Alpaca account, a TradingView subscription, a paid market-data feed, or any cloud provider beyond the VPS.

## Setup overview

```
Desktop:   run the one-time Robinhood login  →  produces data/rh_oauth.json
Hostinger: create a VPS (Ubuntu) → install Docker
VPS:       clone this repo → add your secrets + config → upload rh_oauth.json → docker compose up
Browser:   open the dashboard → verify health → confirm it is scanning
```

---

## Step 1 — Robinhood account + one-time login

1. **Have a Robinhood account with options enabled.** You need options Level 2 or higher to sell cash-secured puts. Set this up in the Robinhood app first.
2. **Enable agentic access.** Robinhood's agentic connection is a dedicated login. Follow Robinhood's in-app flow and create the agentic sub-account the scanner will read. **That read does not place orders**, on that account or any other.
3. **Run the one-time login on your desktop** (not the VPS — the flow opens a browser and pairs with your phone):

   ```bash
   # on your desktop, with this repo cloned and Python 3.13 + deps installed:
   pip install -e ".[all]"
   python -m agentic.tools.rh_login
   ```

   This walks through Robinhood's OAuth flow and writes **`data/rh_oauth.json`** (your access + refresh tokens). You'll upload that file to the VPS in Step 3. The bot **auto-refreshes** the token from then on — you won't have to log in again unless the refresh chain is broken.

> **Guard `rh_oauth.json` like a password.** It is the Robinhood login. Never commit it, never share it, never paste it anywhere. It is git-ignored by default.

---

## Step 2 — Spin up a Hostinger VPS

> 💡 **Get a Hostinger VPS here → https://www.hostinger.com?REFERRALCODE=LRBKTHIELNOA** — using this link supports the project at no extra cost to you.
>
> ⚡ **Skip the manual install:** when creating the VPS, paste this into Hostinger's **"Post-install script"** field and the server boots with Docker + ThetaBot already set up:
>
> ```
> #!/bin/bash
> curl -fsSL https://raw.githubusercontent.com/drwatson93/ThetaBot/main/scripts/bootstrap.sh | bash
> ```
> *(Requires the repo to be public. Then just SSH in, edit `.env` + `config.yaml`, upload your token, and start it.)*

1. Go to Hostinger → **VPS Hosting** and choose a plan. **KVM 1** (1 vCPU / 4 GB RAM / ~50 GB) is more than enough — the bot uses a tiny fraction of it. KVM 2 gives comfortable headroom if you want it.
2. **Operating system:** choose **Ubuntu 24.04** (or 22.04). If Hostinger offers an **"Ubuntu with Docker"** template, pick it — Docker comes pre-installed and you can skip part of Step 3.
3. Set a strong **root password** (or, better, add your **SSH key**) when prompted.
4. Choose a **datacenter** near you (or near US markets — latency isn't critical for this slow strategy).
5. Once it provisions, note the VPS's **public IP address** from the Hostinger control panel (hPanel → VPS → your server).
6. **SSH in** from your desktop:

   ```bash
   ssh root@YOUR_VPS_IP
   ```

## Host on Render (read-only scanner)

Real Robinhood quotes, no orders:

1. On your **desktop**, run the one-time login (see Step 1) and copy the one-line `RH_OAUTH_JSON` it prints.
2. Copy `config.render.example.yaml` into a Render **Secret File** named `config.yaml`. Fill `entry.watchlist` with names you are willing to own. `mode` and `i_understand_live_trading` are ignored if an old file still has them.
3. In Render: **New → Blueprint**, point it at [drwatson93/ThetaBot](https://github.com/drwatson93/ThetaBot), apply `render.yaml`. Paste `RH_OAUTH_JSON` when prompted. Copy `DASHBOARD_PASSWORD` from the Environment tab after create. Optionally set `PAUSE_TOKEN` (Environment → add, a long random string *different* from `CONTROL_TOKEN`) so a monitoring bot can pause via `POST /control/pause-only` without receiving `CONTROL_TOKEN`.
4. Open `https://<service>.onrender.com/health`. You want `"orders_hard_disabled":true`, a `scanner` object, and `feed_age_seconds`, plus `"robinhood_connected":true` and `"practice":true`. A closed market or an old quote stays HTTP 200. If `robinhood_connected` is false, the scanner has no real chains — re-check the token.
5. Confirm `/health` still reports `orders_hard_disabled: true`.

The Blueprint uses one Starter web service, Virginia, a 1 GB disk at `/app/data` (required: the OAuth refresh token is rewritten on every use), and auto-deploy **off**. Free Render instances will not work (no disk, they sleep after 15 minutes).

To replace a broken token later: run `rh_login` on the desktop again, paste the new JSON into `RH_OAUTH_JSON`, set `RH_OAUTH_RESEED=1` for one deploy, then turn that flag off.

## Step 3 — Install & deploy

> ### ⚡ Fastest path — one command
> On a fresh Ubuntu VPS (or if you used the Hostinger post-install script above, this is already done):
> ```bash
> curl -fsSL https://raw.githubusercontent.com/drwatson93/ThetaBot/main/scripts/bootstrap.sh | bash
> ```
> That installs Docker, clones ThetaBot, and creates your `.env` + `config.yaml`. Then jump to editing them below (steps 3 & 5) — you can skip the manual Docker install and clone.

Manual steps (or to understand what the script did):
On the VPS:

1. **Install Docker** (skip if your template already has it):

   ```bash
   curl -fsSL https://get.docker.com | sh
   docker --version   # confirm it's installed
   ```

2. **Clone this repo** and enter it:

   ```bash
   git clone https://github.com/drwatson93/ThetaBot.git
   cd ThetaBot
   ```

3. **Create your secrets file** from the template:

   ```bash
   cp .env.example .env
   nano .env
   ```

   Fill in — at minimum:

   ```ini
   # Dashboard login (pick your own — you'll use these to open the web UI)
   DASHBOARD_USER=admin
   DASHBOARD_PASSWORD=change-this-to-something-strong

   # A random token that protects pause/resume and the other control endpoints.
   # Never give this to a monitoring bot.
   CONTROL_TOKEN=another-long-random-string

   # Optional. Pause-only token for a monitoring bot (cannot resume or change alerts).
   PAUSE_TOKEN=

   # (Optional) desktop push alerts via Pushover — leave blank to skip
   PUSHOVER_TOKEN=
   PUSHOVER_USER=

   # (Optional) AI commentary via Claude — leave blank to skip
   ANTHROPIC_API_KEY=
   ```

   You do **not** set any Alpaca or TradingView keys in this edition.

4. **Upload your Robinhood token.** The easy path is a bind-mounted `data/` folder. Create it and copy the file you made in Step 1:

   ```bash
   mkdir -p data
   # from your DESKTOP, in a separate terminal:
   scp data/rh_oauth.json root@YOUR_VPS_IP:/root/ThetaBot/data/rh_oauth.json
   ```

   Then make sure `docker-compose.yml` mounts your host `data/` folder (this edition ships that way):

   ```yaml
   volumes:
     - ./data:/app/data
   ```

5. **Set your strategy config:**

   ```bash
   cp config.example.yaml config.yaml
   nano config.yaml
   ```

   The lines to check first:

   ```yaml
   entry:
     enabled: true
     watchlist: [AAA, BBB, ...]    # names you are happy to own
   market_data: robinhood          # default: all data from your Robinhood login (no Alpaca)
   ```

   `mode` and `i_understand_live_trading` are ignored if an old file still has them. This build sends alerts and does not place orders.

   (Full options in the [Configuration reference](#configuration-reference).)

6. **Launch:**

   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch it boot; Ctrl-C to stop watching
   ```

## Step 4 — First run

1. **Open the dashboard via SSH tunnel** (port 8000 is bound to localhost only): `ssh -L 8000:localhost:8000 user@YOUR_VPS` then `http://localhost:8000` — log in with the `DASHBOARD_USER` / `DASHBOARD_PASSWORD` you set.
2. **Confirm health:** the top banner should show the broker connected. Check `http://localhost:8000/health` returns `ok` and `orders_hard_disabled: true`.
3. **Let it scan.** It records CSP and covered-call alerts. It does not place orders. Read the decision log — make sure its choices make sense to *you*.
4. **Add HTTPS.** Exposing a password over plain `http://` is risky. The simplest option is to put **Caddy** in front for automatic HTTPS with a domain you point at the VPS — see the wiki/`docs/`. At minimum, restrict port 8000 with a firewall to your own IP.
5. **Old keys are ignored.** `mode` and `i_understand_live_trading` in an old `config.yaml` are ignored.

---

## Accessing your dashboard securely

The dashboard runs on your server at port **8000**. **You do not need a domain or a website.** Pick the access method that fits — simplest to most polished:

### 1. Direct IP — not the default
Port 8000 is bound to `127.0.0.1` only. `http://YOUR_VPS_IP:8000` will not work unless you deliberately republish the port. Prefer the SSH tunnel or Cloudflare Tunnel below.

### 2. SSH tunnel — recommended default (nothing exposed, free)
Nothing to install, no open ports. From your computer:
```bash
ssh -L 8000:localhost:8000 root@YOUR_VPS_IP
```
Then open `http://localhost:8000` while that SSH session is running. The dashboard is never reachable from the public internet — the most private option, and it costs nothing.

### 3. Cloudflare Tunnel — best for always-on access (HTTPS, no open ports) ✅ packaged
A persistent, encrypted URL you can hit from anywhere (including your phone), with **no inbound ports open on your VPS**. It's built in — just enable it:
1. At **dash.cloudflare.com → Zero Trust → Networks → Tunnels → Create a tunnel** (free).
2. Add a **public hostname** (a subdomain on a domain you've added to Cloudflare) and route it to the service **`http://agentic:8000`**.
3. Put the tunnel **token** in `.env`:  `CLOUDFLARE_TUNNEL_TOKEN=...`
4. Start with the tunnel profile:
   ```bash
   docker compose --profile tunnel up -d
   ```
Now it's at `https://yourname.example.com`, port 8000 still closed to the world. For a real login wall, add a **Cloudflare Access** policy (email/Google) to that hostname — highly recommended.

### 4. Your own domain + HTTPS (Caddy)
Prefer to self-host HTTPS like a classic web app? Point a domain at the VPS and run Caddy in front for automatic Let's Encrypt certs. More moving parts than the tunnel; only if you specifically want it.

**Bottom line:** most people should use the **SSH tunnel** for occasional checks, or a **Cloudflare Tunnel** for a secure always-on URL. A public website is optional.

## Configuration reference

`config.yaml` (hot-reloadable via the dashboard). Key sections:

```yaml
# mode and i_understand_live_trading are ignored if an old file still has them
trading_start: "10:00"             # America/New_York; scan window opens at this time

market_data: robinhood       # robinhood | alpaca | paper (paper: local quote stub, no chain)

entry:
  enabled: true
  watchlist: [AAA, BBB, XYZ, ...]   # ONLY names you'd own
  feed: indicative               # ignored for the robinhood provider; use opra only with Alpaca
  earnings_gate: true            # never hold a short put through earnings
  criteria:                      # the CSP screen — matches config.example.yaml
    delta_min: 0.10
    delta_max: 0.20              # ~how likely you are to be assigned; 0.10-0.20 measured best (docs/backtests.md)
    dte_min: 7
    dte_max: 14
    min_annualized_yield: 0.20   # (premium/strike)/dte*365 floor (0.20 = 20%)
    min_open_interest: 100
    min_volume: 10
    max_spread_pct: 0.10
    min_iv_rv_ratio: 1.3         # only sell when IV is >= 1.3x the name's 20-day realized vol
  cc_criteria:                   # the covered-call screen (post-assignment)
    delta_min: 0.20
    delta_max: 0.30
    min_annualized_yield: 0.20
    cc_below_basis_after_days: null
    cc_otm_band: [0.05, 0.10]
  sizing:
    max_position_size_pct: 0.10  # cap each CSP at 10% of account value
    max_concurrent_positions: 5
    total_bp_utilization_target: 0.50
    buying_power_reserve_pct: 0.10

risk:                            # optional sector cap; loss_breaker_* keys are ignored
  max_pct_per_sector: null

rules:                           # each rule needs rule_type (required by the config schema)
  - name: profit-target
    rule_type: PROFIT_TARGET
    enabled: true
    requires_approval: false
    params:
      profit_pct: 0.5            # alert once 50% of the credit is captured
```

### Exit rules from the dashboard

The owner login can tune exit rules on the dashboard **Rules** tab. The view-only login sees the same numbers and cannot save (the server returns 403). Each rule has an on/off switch. The number fields are:

| Field | Rule | Allowed |
|---|---|---|
| `profit_pct` | profit target | 0.05–0.95 |
| `loss_mult` | stop loss | 1.0–10, or off |
| `delta_stop` | stop loss | 0.1–1.0, or off |
| `dte_threshold` | DTE | whole number 0–30 |

**Stop-loss triggers.** `null` means that trigger is off. `0` is not off (it would trip immediately) and is rejected. Turning off `loss_mult` leaves `delta_stop` active, and the other way around. Turning **both** triggers off while the rule stays enabled is rejected — disable the rule itself (`enabled: false`) to turn stop-loss off. You can turn the rule back on later and set either trigger again.

Saving posts the full rules list plus the signature of the rules the form was loaded from. The page shows old → new and locks the inputs until you confirm or cancel. On confirm it reads the form again and refuses to post if that differs from the diff you reviewed. The list must be the same rules that are already running: same names, same `rule_type`, same order. Adding, removing, or retyping a rule is rejected and nothing is changed. If another save landed first, the server returns 409, changes nothing, and the page tells you to reload. Fields this form does not edit stay as they are. `match` is display-only: a TradingView alert always tries an exact contract, then the underlying.

A successful save that actually changes a value is applied in place, written to the data-disk overlay (it overrides `config.yaml`), and recorded as a `CONFIG_EDIT` audit row. Changed fields are stored as `rules.<name>.<field>: {old, new}` — for example `rules.stop-loss.params.loss_mult` from `2.0` to `null`. A save whose leaf diff is empty writes no `CONFIG_EDIT` row, so a no-op does not ping Watchman. Every real change still does.

The monitor reloads these rules on its next poll. A disabled profit-target, stop-loss, or DTE rule is not evaluated. Turning **tv-signal** off is read on the next cycle. An alert that arrives while it is off is stored expired, and any still-new alerts are expired every cycle, including outside the scan window. The processor checks the rule again before each alert in a batch, so turning it back on does not replay them. A later alert is recorded only. This screen does not place an order. `ORDERS_HARD_DISABLED` stays on.

Tune the yield floor and delta band to your own risk tolerance. Higher `min_annualized_yield` = fewer, richer, higher-IV trades; lower = more, thinner ones. The defaults above are the ones that survived a real-option-print backtest under both fair and worst-case fills; **[docs/backtests.md](docs/backtests.md)** lists every lever tested, what held up, and what did not.

## Optional integrations (for different setups)

**By default, ThetaBot needs nothing but your Robinhood login** — quotes and chains come from that same login. If you already pay for another data source or extra signals, you can plug them in. Neither is required.

### Option A — Alpaca for market data

Robinhood's data is complete and works well for this slow strategy, but you can use **Alpaca** instead (e.g. you already have it, or want a separate data source):

1. Create an account at **[alpaca.markets](https://alpaca.markets)** and generate API keys.
2. **Real-time options data (OPRA) requires Alpaca's paid market-data subscription.** The free tier is delayed (`indicative`). Real-time quotes use `entry.feed: opra`.
3. Add the keys to `.env`:
   ```ini
   ALPACA_API_KEY=your_key
   ALPACA_API_SECRET=your_secret
   ```
4. Switch the provider (and feed) in `config.yaml`:
   ```yaml
   market_data: alpaca
   entry:
     feed: opra        # real-time (paid). indicative is the delayed free tier.
   ```
5. `docker compose up -d` to apply. Everything else is identical.

### Option B — TradingView for extra trend signals

ThetaBot can *optionally* gate entries on **ADX** (trend strength) and **Bollinger %B** (position in the band), fed from TradingView alerts. This is **pure enrichment — the bot runs fine without it**, and these gates are **off by default**.

Requires a **TradingView plan that supports webhook alerts** (Pro+ or higher).

1. Choose a webhook token and add it to `.env`:
   ```ini
   TRADINGVIEW_WEBHOOK_TOKEN=some_long_random_string
   ```
2. In TradingView, create alerts on your watchlist symbols with the **Webhook URL**:
   ```
   https://YOUR_HOST/webhook/tradingview?token=some_long_random_string
   ```
   and an alert message that posts the indicator values as JSON (the symbol plus its `adx` and `bb_percent_b`). The webhook handler at `POST /webhook/tradingview` ingests those into each name's context.
3. Turn the gates on in `config.yaml`:
   ```yaml
   entry:
     criteria:
       min_adx: 20            # skip weak / choppy trends
       min_bb_percent_b: 20   # skip price pinned to the lower band
   ```

If no fresh alert has arrived for a symbol, these gates simply **don't apply** (fail-open) — they never block a scan for lack of data. To turn them back off, remove the two lines (or set them to `null`).

## Operating the bot

- **Dashboard** (`/`) -- tabbed and mobile-friendly:
  - **Overview:** health, open positions, realized/unrealized P&L (this week **and** all-time), win rate, the reason behind each close, and **Ready to add** (tier names the account can now afford).
  - **Watchlist:** the daily read on every name you watch. **Setups today** labels each name from completed daily bars -- *washout* (oversold), *quiet base*, *support test*, *coiling* (volatility squeeze), *breakout* / *breakdown* (confirmed on volume) -- plus a column for today's unfinished bar breaking a range or support right now. Labels carry a put-seller bias (favorable / avoid / neutral). Below it, **Which setups have edge here** scores every fire on what actually happened 5 and 10 bars later on *your* names (hit rate, average move, worst 10-day dip), and **Per-name risk profile** measures, for each name, how often a put a given expected-move cushion below spot was touched or finished in the money over the last year, and suggests a wider cushion for names that run hot. Every watchlist add is profiled automatically. Endpoints: `/api/setups`, `/api/setups/accuracy`, `/api/risk-profile`.
  - **Brief:** the **Weekly tactical brief** (`/api/brief`) -- market backdrop, this week's catalysts, setups firing, per-name expected-move **cushion** (strike distance in option-implied moves), earnings-inside-the-contract flags, open-position **management** (roll window / moneyness), and **assignment capacity** (collateral if every short put were assigned vs. buying power). Every generated brief is **saved**: the tab opens on the latest one, a picker lists earlier ones (`/api/briefs`, `/api/briefs/{id}`), and **Generate new** builds a fresh one. Descriptive only -- not advice.
  - **Tuning:** change config from your phone, with no SSH and no restart. Multi-CSP cap, the entry gates, the **market regime** block (confirmed-downtrend skip), **setup gates** (`avoid_setups` / `require_setups`, a *prefer setups* ranking tilt, a put-seller preset that avoids the breakout family, and *apply suggested cushions* from the risk profile), the delta band, and per-ticker overrides, each schema-validated and persisted to a writable overlay (safety-critical keys stay locked). All setup gates are **off by default** and only ever tighten.
  - **On a phone** the tabs sit in a bottom bar within thumb reach, every card folds on a tap (remembered per device), and wide tables keep their first column pinned while you scroll sideways.
- **Buying power:** CSP size uses the broker's buying-power figure.
- **Ready to add** (`entry.watchlist_tiers`, optional): list quality names with the collateral one contract needs; once the account can hold one under the per-name cap, they show up on the Overview and Tuning tabs with an **Add** button that carries their `per_ticker` overrides. The bot never adds names on its own (`GET /api/tiers`).
- **Assignment clock** (`cc_below_basis_after_days`, opt-in, per ticker): by default calls are never sold below cost basis, which on volatile names can park capital for months. With a clock set, shares still under water that many days after assignment may be written against below basis inside a 5-10% OTM band, so the capital turns over. The holdings table shows each name's days held and whether below-basis calls are allowed.
- **Open interest:** `GET /api/option-oi?symbol=AAA` returns the full call+put chain with OI, volume, IV and greeks (Robinhood-sourced; Alpaca's snapshots carry no OI).
- **Pause / resume:** pause mutes new alert webhooks. Resume needs the dashboard login or `CONTROL_TOKEN`. A monitoring bot can pause (only) with `POST /control/pause-only?token=$PAUSE_TOKEN&reason=...` — that token cannot resume.
- **Logs:** `docker compose logs -f` on the VPS.
- **Updating:** `git pull && docker compose up -d --build`.
- **Backups:** your whole state is `data/` (the SQLite DB + `rh_oauth.json`). Back that folder up.

## Safety & risk controls

The bot is built to *survive*, not to gamble:

- **Only names you list** can show up on an alert.
- **Position + concentration caps** so no single trade dominates.
- **Entry gates** — skips earnings, broken downtrends, illiquid contracts, and (opt-in) thin cushion or thin variance-risk-premium (the expected-move and IV/realized gates). An opt-in **confirmed-downtrend skip** pauses new puts market-wide once SPY has spent five straight sessions under its 200-day.
- **No order path.** `ORDERS_HARD_DISABLED` stays on.
- **Pause** — mutes alert webhooks. It does not close positions, because this build never opened them.
- **No hidden leverage** — cash-secured puts are fully collateralized.

None of this removes market risk. A sharp gap down on a held name is still a real loss.

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard won't load | `docker compose ps` (is it running?), firewall allows port 8000, correct IP. |
| Banner shows "degraded"/broker down | `rh_oauth.json` present in `data/`? Token still valid? Re-run `rh_login` on desktop and re-upload. |
| No alerts firing | Normal if nothing clears your criteria. Lower `min_annualized_yield` or check the decision log for the gate that's rejecting. |
| "paused" and no webhooks | Pause is on — check `/api/ops` for the reason; resume from the dashboard. |
| Token stopped refreshing | Re-run the desktop `rh_login` and re-upload `rh_oauth.json`. |

## Disclaimer & license

This software is provided **"as is", without warranty of any kind**, express or implied. The authors are **not liable** for any losses, damages, or account actions arising from its use. It is **not financial, investment, tax, or legal advice**, and nothing here is a recommendation to buy or sell any security.

**Trading options involves substantial risk of loss.** Cash-secured puts can result in forced purchase of shares; covered calls can cap gains and result in shares being sold. Only trade with money you can afford to lose.

This project is **not affiliated with, endorsed by, or sponsored by Robinhood Markets, Inc.** "Robinhood" is a trademark of its owner. You are solely responsible for complying with Robinhood's Terms of Service and all applicable laws and regulations in your jurisdiction.

By running this software you accept full responsibility for its behavior and any resulting trades.

_License: MIT (see `LICENSE`)._
