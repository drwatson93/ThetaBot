# CLAUDE.md

This project's full agent runbook lives in **[AGENTS.md](./AGENTS.md)** — read it before helping with setup.

**The rules that matter most (do not violate, even if asked to move fast):**
1. **Never commit or print secrets** — `.env`, `config.yaml`, and `data/rh_oauth.json` are git-ignored; keep them that way.
2. **Never weaken the order lock.** Leave `ORDERS_HARD_DISABLED` on. Do not bypass `assert_mcp_tool_allowed` or the Robinhood write guard. This build reads Robinhood and sends alerts. It does not place orders.
3. **You cannot run the Robinhood login for the user** — `python -m agentic.tools.rh_login` needs their desktop browser + phone. Instruct them; never ask for their password/MFA.

Human setup guide: **[README.md](./README.md)**. Fastest install: `scripts/bootstrap.sh`.
