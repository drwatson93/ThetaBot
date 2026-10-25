# ThetaBot — Quickstart

The 5-minute version. Full detail: **[README.md](./README.md)**. Setting up with an AI assistant? Point it at **[AGENTS.md](./AGENTS.md)**.

> ⚠️ This build **scans and sends alerts**. It does not place orders. A human who trades an alert can still be assigned shares — only watch names you are willing to own. Read the README's warnings. Not financial advice.

## Before you start
- A **Robinhood** account with **options enabled** + **agentic access** (set up in the Robinhood app).
- ~$5/mo for a small VPS.

## 1. Get your Robinhood token (on your computer, once)
```bash
python -m agentic.tools.rh_login      # opens a browser + pairs with your phone
```
Creates `data/rh_oauth.json`. Keep it secret — never commit or share it.

## 2. Get a server
Create a VPS (Hostinger — **https://www.hostinger.com?REFERRALCODE=LRBKTHIELNOA**), choose **Ubuntu**, and paste this into the **"Post-install script"** field so it installs automatically:
```
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/drwatson93/ThetaBot/main/scripts/bootstrap.sh | bash
```
(Or SSH in later and run that one line yourself.)

## 3. Configure
```bash
nano ~/ThetaBot/.env           # set DASHBOARD_PASSWORD and CONTROL_TOKEN (optional PAUSE_TOKEN)
nano ~/ThetaBot/config.yaml     # your watchlist; market_data: robinhood
```
Upload your token from your computer:
```bash
scp data/rh_oauth.json root@YOUR_VPS_IP:~/ThetaBot/data/
```
`mode` and `i_understand_live_trading` are ignored if an old file still has them. The bot alerts; it does not place orders.

## 4. Start
```bash
cd ~/ThetaBot && docker compose up -d --build
```

## 5. View the dashboard (no website needed)
```bash
ssh -L 8000:localhost:8000 root@YOUR_VPS_IP     # then open http://localhost:8000
```
Want a secure always-on URL instead? See **README → Accessing your dashboard securely** (Cloudflare Tunnel).

## 6. Confirm it is scanning
Open `http://localhost:8000/health`. You want `"orders_hard_disabled":true` and `"practice":true`. There is no live switch. Buying power for sizing is the broker's buying-power figure. Edit `config.yaml` (not the dashboard) to change the watchlist, then `docker compose up -d`.
