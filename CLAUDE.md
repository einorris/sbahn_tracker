# CLAUDE.md — S-Bahn Tracker Bot

## Project Overview
Telegram bot tracking Munich S-Bahn departures and service messages.
Single-file monolith: `sbahn-bot.py` (~1920 lines). Deploy target: Render (webhook mode) or local polling.

## Working Approach

### Always Plan Before Coding
For any non-trivial change (new feature, UX flow change, new API integration, refactor):
1. **Enter plan mode first** — explore the codebase, understand existing patterns, design the approach
2. **Ask clarifying questions** before writing any code
3. **Get user approval on the plan** before implementing
4. Only skip planning for obvious one-liner fixes or typo corrections
5. always update memory state after any change completed

This prevents wasted effort, catches edge cases early, and keeps the user aligned.

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

### OpenAI / AI Mode
- AI is now the **default UX** — all free-text is routed to `on_ai_input()` by default
- `_interpret_with_openai()` returns `(action, args, detected_lang)` — always unpack all three
- Language is auto-detected from user input via OpenAI and set in `context.user_data["lang"]`
- Both `_AI_TOOLS` functions (`show_departures`, `show_disruptions`) have a `"language"` field
- `/ai` command still exists but is now just a reminder — AI is always active for free text
- `on_text_input()` routing priority: `await_feedback` → `await_station` → AI fallthrough

### What NOT to Do
- Do not add Redis, Sentry, or other infra unless explicitly requested
- Do not refactor into modules unless explicitly requested
- Do not add tests unless explicitly requested
- Do not add error handling for impossible cases inside internal functions
- Do not auto-commit or push — always ask first
- Do not modify `requirements.txt` without asking (Render redeploys on change)

## Architecture — Key Patterns

### UX Flow (current)
```
/start → welcome message (English default) → AI mode active immediately
  Free text → on_ai_input → OpenAI gpt-4o-mini (function calling)
    → show_departures → find station → _send_departures_for_eva
    → show_disruptions → fetch_line_messages_safe → render
    → unclear → re-prompt (stays in AI mode)

/feedback → sets await_feedback → expects feedback text (NOT routed to AI)
/line, /departures, /messages, /lang → command-mode, then AI resumes for next text
```

### Text Input Routing (`on_text_input`, ~line 1788)
```python
await_feedback → on_feedback_message()   # protected — never goes to AI
await_station  → on_station_input()      # station search flow
default        → on_ai_input()           # AI is the fallthrough
```

### Localization
- `UI_STRINGS` dict: `en`, `de`, `uk` keys — all UI text
- `T(context, key)` — get localized string with English fallback
- `TR_MSG(context, text_de)` — translate dynamic content (MVG messages) via DeepL
- `get_user_lang(context)` — returns `context.user_data.get("lang", "en")`
- Language auto-detected from user input by OpenAI and stored in `context.user_data["lang"]`
- `/lang de|en|uk` command can manually override

### Key Helpers (do not duplicate)
| Helper | Line | Purpose |
|--------|------|---------|
| `T(context, key)` | ~325 | Localized UI string |
| `TR_MSG(context, text_de)` | ~334 | Translate dynamic content via DeepL |
| `safe_send_html(msg, html)` | ~1039 | Send HTML message safely |
| `nav_menu(context)` | — | Navigation inline keyboard |
| `EVA_OVERRIDES` | — | EVA number corrections dict |
| `PLAN_CACHE` | — | In-memory timetable cache |

### Callback Data Prefixes (fixed, do not change without updating router)
| Prefix | Meaning |
|--------|---------|
| `LANG:` | Language selection |
| `L:` | Line selection (e.g. `L:S2`) |
| `A:MSG` | Show service messages |
| `A:DEP` | Show departures prompt |
| `B:MAIN` | Back to line picker |
| `B:ACT` | Back to actions menu |
| `D:` | Show message details |
| `ST:` | Station picked by EVA |

## Environment
- Local dev: polling mode (no `WEBHOOK_BASE` set)
- Production: webhook mode on Render (`WEBHOOK_BASE` + `PORT` set)
- Required secrets: `BOT_TOKEN`, `DB_CLIENT_ID`, `DB_API_KEY`
- Optional: `DEEPL_AUTH_KEY`, `AMPLITUDE_API_KEY`, `ADMIN_CHAT_ID`, `FEEDBACK_SALT`, `OPENAI_API_KEY`, `WEBHOOK_SECRET`

## Hosting — Render.com
- Hosted as a **Web Service** on Render's **free plan**
- Process type: `worker` (defined in `Procfile`: `worker: python sbahn-bot.py`)
- Free plan spins down after inactivity — webhook mode keeps it alive via Telegram pings
- Do NOT add always-on background threads or heavy startup tasks (free plan memory/CPU limits)
- Redeploy is triggered automatically on push to `main` — confirm before pushing

## Known Tech Debt (do not fix without being asked)
- `safe_send_html` is defined twice (~line 978 and ~1039)
- Station search uses blocking `requests` in async context (via executor)
- No structured logging — uses print statements
- In-memory cache only — lost on restart
- No tests, no CI/CD
