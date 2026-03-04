# Design: /ai Natural Language Mode

**Date:** 2026-03-04
**Status:** Approved

## Overview

Add a `/ai` command that puts the bot into a natural language interpretation mode. The user can type human-like queries (e.g. "will I make it to S-Bahn in Erding") and the bot uses the OpenAI function calling API to determine what action to take.

## Scope

Supported actions:
- **Departures** — extract station name (and optionally line), show departures using existing flow
- **Service disruptions** — extract optional line, show MVG messages using existing flow

## UX Flow

1. User sends `/ai`
2. Bot sets `context.user_data["await_ai"] = True`
3. Bot replies with a localized prompt: *"Ask me anything about S-Bahn departures or disruptions."*
4. User sends a natural language message
5. `on_text_input` detects `await_ai`, calls `on_ai_input`, clears flag
6. Bot calls OpenAI with function calling, routes to the appropriate existing handler
7. On ambiguous input (no tool call returned): bot replies with an example hint and re-sets `await_ai = True`

## Mode Exit

`await_ai` is cleared when:
- The user sends any text and it is processed (single-shot)
- Any command handler is invoked (`/start`, `/line`, `/messages`, `/departures`, `/lang`, `/feedback`)
- `/start` already resets all user_data, so this is implicit there

Each `cmd_*` function adds `context.user_data["await_ai"] = False` at the top.

## AI Backend

- **Model:** `gpt-4o-mini` (fast, cheap)
- **Method:** `openai.chat.completions.create` with `tools` parameter
- **Tool 1:** `show_departures(station_name: str, line: str | null)`
- **Tool 2:** `show_disruptions(line: str | null)`
- **System prompt:** Short, in English — describes available S-Bahn lines (S1–S8) and asks to extract intent
- **tool_choice:** `"auto"` — allows model to decide; if no tool is called, treat as unclear

## Routing After Tool Call

| Tool called | Action |
|-------------|--------|
| `show_departures` | Run `find_station_candidates(station_name)` → `_send_departures_for_eva(...)` |
| `show_disruptions` | Run MVG messages fetch, optionally filter by extracted `line` |
| No tool call | Reply with `ai_not_understood` string, re-set `await_ai = True` |

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `OPENAI_API_KEY` not set | `/ai` replies `ai_not_available`, no crash |
| OpenAI API error/timeout | Reply `ai_error`, offer retry (re-set `await_ai`) |
| Station not found | Fall through to existing candidate picker UI |
| Unknown line in disruptions | Show all disruptions (ignore extracted line) |

## New UI Strings (all 3 langs: en, de, uk)

| Key | Example (EN) |
|-----|-------------|
| `ai_prompt` | "Ask me about departures or disruptions." |
| `ai_not_available` | "AI mode is not available." |
| `ai_error` | "AI failed to interpret your request. Please try again." |
| `ai_not_understood` | "I couldn't understand that. Try: 'departures from Erding' or 'disruptions on S2'." |

## Dependencies

- Add `openai` to `requirements.txt` (will trigger Render redeploy — confirm before pushing)
- New env var: `OPENAI_API_KEY`

## New Code (estimated ~80–100 lines in sbahn-bot.py)

- `cmd_ai(update, context)` — handles `/ai` command
- `on_ai_input(update, context)` — processes the natural language message
- `_interpret_with_openai(text, lang)` — calls OpenAI, returns `(action, params)` or `None`
- Handler registration: `CommandHandler("ai", cmd_ai)` in `build_app()`
- Route `await_ai` check in `on_text_input` (before `await_station`)
