# ThetaBot — Quickstart

The short version. Full detail: **[README.md](./README.md)**. Setting up with an AI assistant? Point it at **[AGENTS.md](./AGENTS.md)**.

> Reads Robinhood, scans, and posts alerts to a webhook. It scores each alert and never places an order. `ORDERS_HARD_DISABLED` stays on. A person who trades an alert can still be assigned — only watch names you will own. Not financial advice.

## Before you start
- A **Robinhood** account with **options enabled** and **agentic access** (set up in the Robinhood app).
- A small VPS.

## 1. Get your Robinhood token (on your computer, once)
```bash
python -m agentic.tools.rh_login
```
Creates `data/rh_oauth.json`. Keep it secret — never commit or share it.

## 2. Get a server
Create a VPS (Hostinger — **https://www.hostinger.com?REFERRALCODE=LRBKTHIELNOA**), choose **Ubuntu**, and paste this into the **"Post-install script"** field:
```
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/drwatson93/ThetaBot/main/scripts/bootstrap.sh | bash
```
(Or SSH in later and run that one line yourself.)

## 3. Configure
```bash
nano ~/ThetaBot/.env            # DASHBOARD_PASSWORD and CONTROL_TOKEN (optional PAUSE_TOKEN)
nano ~/ThetaBot/config.yaml     # watchlist; market_data: robinhood
```
Upload the token from your computer:
```bash
scp data/rh_oauth.json root@YOUR_VPS_IP:~/ThetaBot/data/
```
Old `mode` and `i_understand_live_trading` keys are ignored.

## 4. Start
```bash
cd ~/ThetaBot && docker compose up -d --build
```

## 5. Open the dashboard
```bash
ssh -L 8000:localhost:8000 root@YOUR_VPS_IP     # then open http://localhost:8000
```
For an always-on URL, see **README → Accessing your dashboard securely**.

## 6. Confirm it is scanning
Open `http://localhost:8000/health`. You want `"orders_hard_disabled":true`, `"practice":true`, and a `scanner` object. Edit `config.yaml` to change the watchlist, then `docker compose up -d`.
