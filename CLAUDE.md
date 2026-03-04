# CLAUDE.md — S-Bahn Tracker Bot

## Project Overview
Telegram bot tracking Munich S-Bahn departures and service messages.
Single-file monolith: `sbahn-bot.py`. Deploy target: Render (webhook mode) or local polling.

## Key Rules

### Code Style
- Python 3.11+, async/await throughout (PTB v20 async)
- Keep all logic in `sbahn-bot.py` — do NOT split into modules unless explicitly asked
- Use existing helpers: `T(context, key)` for UI strings, `TR_MSG(context, text)` for translation
- All new UI text must be added to `UI_STRINGS` for all three languages: `en`, `de`, `uk`
- Never hardcode user-facing strings outside `UI_STRINGS`

### Telegram / PTB
- Use `safe_send_html()` for all outgoing messages (handles HTML parse errors)
- Callback data prefixes are fixed — do not invent new ones without updating the handler router
- Callback data max length is 64 bytes — keep payloads short

### APIs
- DB Timetables API returns XML — always parse with `xml.etree.ElementTree`
- `PLAN_CACHE` TTL: 90s success, 60s error — do not bypass cache without reason
- EVA overrides live in `EVA_OVERRIDES` dict — add new ones there, not inline

### What NOT to Do
- Do not add Redis, Sentry, or other infra unless explicitly requested
- Do not refactor into modules unless explicitly requested
- Do not add tests unless explicitly requested
- Do not add error handling for impossible cases inside internal functions
- Do not auto-commit or push — always ask first
- Do not modify `requirements.txt` without asking (Render redeploys on change)

## Environment
- Local dev: polling mode (no `WEBHOOK_BASE` set)
- Production: webhook mode on Render (`WEBHOOK_BASE` + `PORT` set)
- Required secrets: `BOT_TOKEN`, `DB_CLIENT_ID`, `DB_API_KEY`
- Optional: `DEEPL_AUTH_KEY`, `AMPLITUDE_API_KEY`, `ADMIN_CHAT_ID`, `FEEDBACK_SALT`

## Hosting — Render.com
- Hosted as a **Web Service** on Render's **free plan**
- Process type: `worker` (defined in `Procfile`: `worker: python sbahn-bot.py`)
- Free plan spins down after inactivity — webhook mode keeps it alive via Telegram pings
- Do NOT add always-on background threads or heavy startup tasks (free plan memory/CPU limits)
- Redeploy is triggered automatically on push to `main` — confirm before pushing

## Known Tech Debt (do not fix without being asked)
- `safe_send_html` is defined twice (~line 978 and ~1036)
- Station search uses blocking `requests` in async context (via executor)
- No structured logging — uses print statements
- In-memory cache only — lost on restart
