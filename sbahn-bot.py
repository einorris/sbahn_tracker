# sbahn_bot.py
# UI на константах (EN/DE/UK), DeepL используется только для переводов внешних сообщений (MVG).
# Легко расширяется новыми языками: добавь словарь в UI_STRINGS и код языка в SUPPORTED_LANGS.

import os
import re
import time
import unicodedata
import hashlib
import html
import requests
import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from datetime import timezone, timedelta
import asyncio
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

# ================== CONFIG ==================

# ================== CONFIG ==================
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
FEEDBACK_SALT = os.getenv("FEEDBACK_SALT", "")

def _anon_id(user_id: int) -> str:
    try:
        base = f"{user_id}:{FEEDBACK_SALT}"
        import hashlib as _hl
        return _hl.sha256(base.encode("utf-8")).hexdigest()[:10]
    except Exception:
        return "anonymous"
EVA_OVERRIDES = {
    ("münchen hbf", 8000261): 8098263,
    # дальше можно добавлять новые пары: ("имя станции в lower", "ориг_eva"): "замена_eva"
}
# === Amplitude analytics ===
AMPLITUDE_API_KEY = os.getenv("AMPLITUDE_API_KEY", "").strip()
AMPLITUDE_ENDPOINT = os.getenv("AMPLITUDE_ENDPOINT", "https://api2.amplitude.com/2/httpapi").strip()

def _analytics_user_id(user_id: int) -> str:
    """
    ID для Amplitude:
      - если задан FEEDBACK_SALT -> используем анонимный хеш
      - иначе просто 'tg-<telegram_id>'
    """
    if FEEDBACK_SALT:
        return _anon_id(user_id)
    return f"tg-{user_id}"

def track_analytics_event(telegram_user_id: int, event_type: str, event_props: Optional[dict] = None):
    """
    Отправка одного события в Amplitude HTTP V2.
    Не ломает бота, если Amplitude недоступен / не настроен.
    """
    if not AMPLITUDE_API_KEY:
        return

    try:
        payload = {
            "api_key": AMPLITUDE_API_KEY,
            "events": [{
                "user_id": _analytics_user_id(telegram_user_id),
                "event_type": event_type,
                "time": int(time.time() * 1000),  # ms since epoch, как просит Amplitude
                "event_properties": event_props or {},
            }],
        }
        # Низкий timeout, чтобы бот не зависал на аналитике
        requests.post(AMPLITUDE_ENDPOINT, json=payload, timeout=2)
    except Exception:
        # Аналитика не должна ломать основной функционал
        pass
BOT_TOKEN   = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
CLIENT_ID   = os.getenv("DB_CLIENT_ID") or "YOUR_DB_CLIENT_ID"
API_KEY_DB  = os.getenv("DB_API_KEY")  or "YOUR_DB_API_KEY"
DEEPL_AUTH_KEY = os.getenv("DEEPL_AUTH_KEY")  # xxxxxxxx:fx

MVG_URL = "https://www.mvg.de/api/bgw-pt/v3/messages"
DB_BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"

HTTP_TIMEOUT = 5   # seconds
HTTP_RETRIES = 2
STATION_SEARCH_DEADLINE_SEC = 7  # hard cap per station search
STATION_HTTP_TIMEOUT = 3         # tighter timeout for station lookup
   # additional attempts (1 + 2)

# Short, safe callback keys
CB_LANG_PREFIX   = "LANG:"    # LANG:de / LANG:en / LANG:uk / ...
CB_LINE_PREFIX   = "L:"       # e.g. L:S2
CB_ACT_MSG       = "A:MSG"
CB_ACT_DEP       = "A:DEP"
CB_ACT_DEP_MANUAL = "A:DEP_MANUAL"  # новый callback для "ввести станцию вручную"
CB_BACK_MAIN     = "B:MAIN"
CB_DETAIL_PREFIX = "D:"
CB_PICK_STATION  = "ST:"      # choosing a specific station from candidates
CB_BACK_ACTIONS  = "B:ACT"    # back to Actions (Messages / Departures)



# 1) нормалізуємо прості HTML у plain text + перенос рядків
_BR_RE = re.compile(r'<\s*br\s*/?\s*>', re.I)
_A_RE  = re.compile(r'<\s*a\b[^>]*href\s*=\s*"([^"]+)"[^>]*>(.*?)</\s*a\s*>', re.I | re.S)


# ================== UI STRINGS (константы) ==================
# Чтобы добавить русский:
# 1) Добавь "ru": {...} ниже
# 2) Добавь "ru" в SUPPORTED_LANGS
UI_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "choose_language": "Choose language",
        "choose_line": "Choose an S-Bahn line:",
        "lines": "Lines:",
        "you_selected_line": "You selected {line}. Choose an action:",
        "actions": "Actions:",
        "btn_cancel_feedback": "✖️ Cancel",
        "feedback_prompt": "Tell me what didn’t work or what to improve. I’ll pass it on anonymously. Send your message now, or press Cancel.",
        "feedback_thanks": "Thanks! Your feedback was delivered anonymously.",
        "feedback_unavailable": "Feedback destination is not configured. Please try later.",
        "feedback_cancelled": "Feedback canceled.",
        "btn_service_messages": "🚧 Disruptions & messages",
        "btn_train_departures": "🚉 Train departures (by station)",
        "btn_train_departures_short": "🚉 Train departures",
        "btn_change_line": "🔀 Change Line",
        "btn_back_main": "⬅️ Back to Main Menu",
        "btn_back": "⬅️ Back",
        "btn_search_again": "🔎 Search again",
        "choose_next": "Choose what to do next:",
        
        "no_messages_for_line": "No current messages for {line}.",
        "details": "🔍 Details",
        "message_details_not_found": "Message details not found.",
        "enter_station_prompt": "Please enter the station name (e.g., Erding or Ostbahnhof):",
        "enter_station_or_choose_prompt": "Please enter the station name or choose one of the previous options:",
        "searching_station": "🔍 Searching departures for “{station}”...",
        "no_station_found": "🚫 No matching stations were found in Deutsche Bahn database.",
        "choose_station": "Please choose the station:",
        "station_search_error": "⚠️ Station search error: {error}",
        "departures_header": "🚉 Departures from {station}{line_suffix}",
        "no_departures": "ℹ️ No departures in the next 60 minutes.",
        "live_unavailable": "⚠️ Live updates are temporarily unavailable. Showing planned times only.",
        "fetch_error": "⚠️ Error while fetching timetable: {error}",
        "invalid_station_id": "⚠️ Invalid station identifier.",
        "language_updated": "Language updated. Choose what to do next:",
        "cmd_lang_usage": "Use: /lang de|en|uk",
        "service_messages_for_line": "🚧 Messages for {line}",
        # departure formatting parts
        "at_word": " at ",
        "platform_word": "Pl.",
        "canceled_word": "Cancelled 😭",
        "minutes_suffix": " min",
    },
    "de": {
        "choose_language": "Sprache wählen",
        "choose_line": "S-Bahn-Linie auswählen:",
        "lines": "Linien:",
        "you_selected_line": "Du hast {line} gewählt. Aktion auswählen:",
        "actions": "Aktionen:",
        "btn_cancel_feedback": "✖️ Abbrechen",
        "feedback_prompt": "Was hat nicht geklappt oder was können wir verbessern? Die Nachricht wird anonym weitergeleitet. Jetzt schreiben oder Abbrechen drücken.",
        "feedback_thanks": "Danke! Dein Feedback wurde anonym übermittelt.",
        "feedback_unavailable": "Feedback-Ziel ist nicht konfiguriert. Bitte später erneut versuchen.",
        "feedback_cancelled": "Feedback abgebrochen.",
        "btn_service_messages": "🚧 Störungen & Meldungen",
        "btn_train_departures": "🚉 Abfahrten (nach Station)",
        "btn_train_departures_short": "🚉 Abfahrten",
        "btn_change_line": "🔀 Linie wechseln",
        "btn_back_main": "⬅️ Zurück zum Hauptmenü",
        "btn_back": "⬅️ Zurück",
        "btn_search_again": "🔎 Erneut suchen",
        "choose_next": "Was möchtest du als Nächstes tun?",
        "no_messages_for_line": "Keine aktuellen Meldungen für {line}.",
        "details": "🔍 Details",
        "message_details_not_found": "Details nicht gefunden.",
        "enter_station_prompt": "Bitte gib den Stationsnamen ein (z. B. Erding oder Ostbahnhof):",
        "enter_station_or_choose_prompt": "Bitte gib den Stationsnamen ein oder wähle eine der vorherigen Optionen:",
        "searching_station": "🔍 Suche Abfahrten für „{station}“…",
        "no_station_found": "🚫 Keine passenden Stationen in der DB-Datenbank gefunden.",
        "choose_station": "Bitte Station auswählen:",
        "station_search_error": "⚠️ Fehler bei der Stationssuche: {error}",
        "departures_header": "🚉 Abfahrten ab {station}{line_suffix}",
        "no_departures": "ℹ️ Keine Abfahrten in den nächsten 60 Minuten.",
        "live_unavailable": "⚠️ Live-Daten vorübergehend nicht verfügbar. Es werden nur Planzeiten angezeigt.",
        "fetch_error": "⚠️ Fehler beim Laden des Fahrplans: {error}",
        "invalid_station_id": "⚠️ Ungültige Stations-ID.",
        "language_updated": "Sprache aktualisiert. Was möchtest du als Nächstes tun?",
        "cmd_lang_usage": "Verwendung: /lang de|en|uk",
        "service_messages_for_line": "📰 Betriebsmeldungen für {line}",
        # departure formatting parts
        "at_word": " um ",
        "platform_word": "Gl.",
        "canceled_word": "Fällt aus 😭",
        "minutes_suffix": " min",
    },
    "uk": {
        "choose_language": "Виберіть мову",
        "choose_line": "Слава Ісу! Оберіть лінію S-Bahn:",
        "lines": "Лінії:",
        "you_selected_line": "Ви обрали {line}. Оберіть дію:",
        "actions": "Дії:",
        "btn_cancel_feedback": "✖️ Скасувати",
        "feedback_prompt": "Що не спрацювало або що можна покращити? Повідомлення буде надіслано анонімно. Надішліть його зараз або натисніть Скасувати.",
        "feedback_thanks": "Дякуємо! Ваш відгук надіслано анонімно.",
        "feedback_unavailable": "Місце призначення для відгуків не налаштовано. Спробуйте пізніше.",
        "feedback_cancelled": "Відгук скасовано.",
        "btn_service_messages": "🚧 Несправності та оголошення",
        "btn_train_departures": "🚉 Відправлення (за станцією)",
        "btn_train_departures_short": "🚉 Відправлення",
        "btn_change_line": "🔀 Змінити лінію",
        "btn_back_main": "⬅️ Назад до головного меню",
        "btn_back": "⬅️ Назад",
        "btn_search_again": "🔎 Шукати ще раз",
        "choose_next": "Що робимо далі?",
        "no_messages_for_line": "Немає актуальних оголошень для {line}.",
        "details": "🔍 Деталі",
        "message_details_not_found": "Деталі не знайдено.",
        "enter_station_prompt": "Введіть назву станції (наприклад, Erding чи Ostbahnhof):",
        "enter_station_or_choose_prompt": "Введіть назву станції або оберіть один із попередніх варіантів:",
        "searching_station": "🔍 Пошук відправлень з «{station}»…",
        "no_station_found": "🚫 У базі Deutsche Bahn станцію не знайдено.",
        "choose_station": "Оберіть станцію:",
        "station_search_error": "⚠️ Помилка пошуку станції: {error}",
        "departures_header": "🚉 Відправлення зі станції {station}{line_suffix}",
        "no_departures": "ℹ️ Відправлень у найближчі 60 хвилин немає (перевірте лінію!)",
        "live_unavailable": "⚠️ Дані в реальному часі тимчасово недоступні. Показано лише планові часи.",
        "fetch_error": "⚠️ Помилка завантаження розкладу: {error}",
        "invalid_station_id": "⚠️ Невірний ідентифікатор станції.",
        "language_updated": "Слава Україні! Що робимо далі?",
        "cmd_lang_usage": "Формат: /lang de|en|uk",
        "service_messages_for_line": "🚧 Сервісні оголошення для {line}",
        # departure formatting parts
        "at_word": " о ",
        "platform_word": "Пл.",
        "canceled_word": "Скасовано 😭",
        "minutes_suffix": " хв",
    },
}

SUPPORTED_LANGS = list(UI_STRINGS.keys())  # ["en", "de", "uk"] — расширяемо

def mvg_html_to_text(s: str) -> str:
    if not s:
        return ""
    # <a href="...">текст</a> -> "текст (url)"
    s = _A_RE.sub(lambda m: f"{m.group(2).strip()} ({m.group(1).strip()})", s)
    # <br> -> \n
    s = _BR_RE.sub("\n", s)
    # абзаци/списки у щось людське
    s = re.sub(r'</\s*p\s*>', '\n\n', s, flags=re.I)
    s = re.sub(r'<\s*p\s*>', '', s, flags=re.I)
    s = re.sub(r'<\s*/\s*li\s*>', '\n', s, flags=re.I)
    s = re.sub(r'<\s*li\s*>', '• ', s, flags=re.I)
    s = re.sub(r'</\s*ul\s*>', '\n', s, flags=re.I)
    # прибрати решту тегів
    s = re.sub(r'<[^>]+>', '', s)
    # розкодувати &amp; &nbsp; тощо
    s = html.unescape(s)
    # трохи зачистки пробілів
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

# 2) підготовка до Telegram HTML: екрануємо спецсимволи після чистки
def to_tg_html(s: str) -> str:
    return html.escape(mvg_html_to_text(s))

# ================== TRANSLATION (DeepL — только для внешних текстов) ==================
DEEPL_URL = "https://api-free.deepl.com/v2/translate"

def _deepl_supported_target(lang_code: str) -> str:
    # Можно расширить при добавлении языков (RU->"RU" и т.д.).
    mapping = {"de": "DE", "en": "EN", "uk": "UK"}
    return mapping.get(lang_code, "EN")

def deepl_translate(text: str, target_lang: str, is_html: bool) -> str:
    """Переводим только внешние тексты (MVG). UI НЕ переводим через DeepL."""
    if not text or not DEEPL_AUTH_KEY:
        return text
    try:
        data = {"text": text, "target_lang": target_lang}
        if is_html:
            data["tag_handling"] = "html"
        r = requests.post(
            DEEPL_URL,
            data=data,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_AUTH_KEY}"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["translations"][0]["text"]
    except Exception:
        return text

# ================== UI i18n HELPERS ==================
def get_user_lang(context) -> str:
    return context.user_data.get("lang", "en")

def T(context, key: str, **kwargs) -> str:
    """Берем UI-строку по ключу для текущего языка, с фолбэком на EN. Поддерживает format(**kwargs)."""
    lang = get_user_lang(context)
    tmpl = UI_STRINGS.get(lang, UI_STRINGS["en"]).get(key) or UI_STRINGS["en"].get(key) or key
    try:
        return tmpl.format(**kwargs)
    except Exception:
        return tmpl

def TR_MSG(context, text_de: str, is_html: bool=False) -> str:
    """
    Контент MVG обычно на DE. Если пользователь не DE — переводим DeepL в его язык.
    """
    lang = get_user_lang(context)
    if lang == "de":
        return text_de
    return deepl_translate(text_de, _deepl_supported_target(lang), is_html)

# ================== MVG HELPERS ==================
def _norm_mvg_line_label(x: str) -> str:
    s = re.sub(r"\s+", "", str(x or "")).upper()   # "S 8" -> "S8", "8" -> "8"
    if not s:
        return ""
    if s.startswith("S"):
        return s
    # якщо просто номер, робимо "S8"
    if s[0].isdigit():
        return "S" + s
    return s

def message_is_visible(msg) -> bool:
    now_ms = datetime.datetime.now(timezone.utc).timestamp() * 1000
    # 1) інциденти (як у тебе було)
    for d in (msg.get("incidentDurations") or []):
        start = d.get("from") or 0
        end   = d.get("to")   or float("inf")
        if start <= now_ms <= end:
            return True
    # 2) публікаційне вікно (планові/загальні повідомлення)
    pd = msg.get("publicationDuration")
    if isinstance(pd, dict):
        start = pd.get("from") or 0
        end   = pd.get("to")   or float("inf")
        if start <= now_ms <= end:
            return True
    # 3) запасний варіант: якщо опубліковано нещодавно (48 год)
    pub = msg.get("publication")
    if pub and pub >= now_ms - 48*3600*1000:
        return True
    return False
def fetch_messages():
    for attempt in range(HTTP_RETRIES + 1):
        try:
            resp = requests.get(MVG_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == HTTP_RETRIES:
                raise
            time.sleep(0.3 * (2**attempt))

def is_active(incident_durations):
    if not incident_durations:
        return False
    now_ms = datetime.datetime.now(timezone.utc).timestamp() * 1000
    for d in incident_durations:
        start = d.get("from"); end = d.get("to")
        if start and end and start <= now_ms <= end:
            return True
    return False

def filter_line_messages(messages, line_label):
    sel = _norm_mvg_line_label(line_label)
    seen = {}
    for msg in messages or []:
        for line in (msg.get("lines") or []):
            ttype = (line.get("transportType") or "").upper()
            lbl   = _norm_mvg_line_label(line.get("label") or line.get("name") or "")
            # допускаємо SBAHN/S; якщо у когось буде "S-Bahn" чи "SBahn" — теж не зашкодить розширити
            if ttype in ("SBAHN", "S") and lbl == sel:
                if message_is_visible(msg):
                    title = (msg.get("title") or "").strip()
                    pub = msg.get("publication", 0)
                    if title in seen:
                        if pub > seen[title].get("publication", 0):
                            seen[title] = msg
                    else:
                        seen[title] = msg
    # свіжі знизу
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=False)

# ================== STATION SEARCH (только Бавария) ==================
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _apply_aliases(q: str) -> str:
    qn = _norm(q)
    aliases = {
        "munich hbf": "München Hbf",
        "munich hauptbahnhof": "München Hbf",
        "muenchen hbf": "München Hbf",
        "muenchen hauptbahnhof": "München Hbf",
        "münchen hauptbahnhof": "München Hbf",
        "hbf tief": "München Hbf",
        "hauptbahnhof": "München Hbf",

        "marienplatz": "München Marienplatz",
        "marienplatz (tief)": "München Marienplatz",

        "karlsplatz": "München Karlsplatz (Stachus)",
        "stachus": "München Karlsplatz (Stachus)",

        "isartor": "München Isartor",
        "rosenheimer platz": "Мünchen Rosenheimer Platz",
        "hackerbrücke": "München Hackerbrücke",
        "hackerbruecke": "München Hackerbrücke",
        "donnersbergerbrücke": "Мünchen Donnersbergerbrücke",
        "laim": "München Laim",
        "pasing": "München-Pasing",
        "muenchen pasing": "München-Pasing",
        "münchen pasing": "München-Pasing",

        "ostbahnhof": "München Ost",
        "munich east": "München Ost",
        "munchen ost": "München Ost",
        "muenchen ostbahnhof": "München Ost",
        "münchen ostbahnhof": "München Ost",
        "leuchtenbergring": "München Leuchtenbergring",
        "berg am laim": "München-Berg am Laim",
        "trudering": "München-Trudering",
        "riem": "München-Riem",

        # Airport
        "munich airport": "Flughafen München",
        "airport": "Flughafen München",
        "muc": "Flughafen München",
        "flughafen münchen": "Flughafen München",
        "flughafen": "Flughafen München",
        "flughafen muenchen": "Flughafen München",
        "visitor park": "Flughafen München Besucherpark",
        "besucherpark": "Flughafen München Besucherpark",
        "Flughafen besucherpark": "Flughafen München Besucherpark",
        "München Flughafen Besucherpark": "Flughafen München Besucherpark",

        # S2 to Erding
        "erding": "Erding",
        "altenerding": "Altenerding",
        "aufhausen (oberbay)": "Aufhausen (Oberbay)",
        "markt schwaben": "Markt Schwaben",
        "grub (oberbay)": "Grub (Oberbay)",
        "heimstetten": "Heimstetten",
        "daglfing": "München-Daglfing",
        "englschalking": "München-Englschalking",
    }
    return aliases.get(qn, q)

def _station_search(query: str):
    """
    Station search with two controlled variants:
      1) exact searchstring
      2) city-prefix wildcard: München*{query}* and Muenchen*{query}*
    Always filters DE-BY and stations having evaNumbers.
    Hard overall deadline + tighter per-request timeout to avoid hangs.
    """
    url = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
    headers = {
        "Accept": "application/json",
        "DB-Client-Id": CLIENT_ID,
        "DB-Api-Key": API_KEY_DB,
    }
    start = time.monotonic()
    base_params = {"federalstate": "bayern"}
    variants = [
        {**base_params, "searchstring": query},
        {**base_params, "searchstring": f"München*{query}*"},
        {**base_params, "searchstring": f"Muenchen*{query}*"},
    ]
    out = []
    for params in variants:
        if time.monotonic() - start > STATION_SEARCH_DEADLINE_SEC:
            break
        for attempt in range(HTTP_RETRIES + 1):
            if time.monotonic() - start > STATION_SEARCH_DEADLINE_SEC:
                break
            try:
                r = requests.get(url, headers=headers, params=params,
                                 timeout=min(STATION_HTTP_TIMEOUT, HTTP_TIMEOUT))
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                stations = None
                if isinstance(data, list):
                    stations = data
                elif isinstance(data, dict):
                    for key in ("result","results","stations","stopPlaces","stopplaces"):
                        val = data.get(key)
                        if isinstance(val, list):
                            stations = val
                            break
                if not stations:
                    continue
                # Bavaria only + have evaNumbers
                stations = [s for s in stations if s.get("evaNumbers")]
                if stations:
                    out = stations
                    break
            except Exception:
                # try next attempt/variant
                pass
            if attempt < HTTP_RETRIES:
                time.sleep(0.25 * (2 ** attempt))
        if out:
            break
    return out if out else []

def _pick_best_station(results, query_norm: str):
    best = None; best_score = -1
    for s in results:
        if not s.get("evaNumbers"):
            continue
        name = s.get("name", ""); nn = _norm(name)
        score = 0
        if nn == query_norm: score += 100
        if nn.startswith(query_norm) or query_norm.startswith(nn): score += 50
        if query_norm in nn: score += 25
        if s.get("federalStateCode") == "DE-BY": score += 5
        if score > best_score:
            best = s; best_score = score
    return best

def rank_stations(results, query_norm: str):
    ranked = []
    for s in results:
        if not s.get("evaNumbers"):
            continue
        name = s.get("name", "")
        nn = _norm(name)
        score = 0
        if nn == query_norm:
            score += 100
        if nn.startswith(query_norm) or query_norm.startswith(nn):
            score += 50
        if query_norm in nn:
            score += 25
        if s.get("federalStateCode") == "DE-BY":
            score += 5
        ranked.append((s, score))
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked

def find_station_candidates(user_input: str, limit: int = 3):
    """
    Returns (best_exact_match, candidates).

    Uses aliases only for ranking, but queries the DB API with both:
      * aliased form (e.g. "München Hbf")
      * original user input (e.g. "Hauptbahnhof", "Riem")

    This allows patterns like "München*Riem*" to work correctly while still
    preferring canonical Munich station names in scoring.
    """
    primary = _apply_aliases(user_input)
    qn = _norm(primary)

    # Собираем результаты по aliased и raw запросам, дедуп по EVA/station id
    combined = []
    seen_keys = set()

    for q in dict.fromkeys([primary, user_input]):  # порядок сохраняем, дубликаты убираем
        if not q:
            continue
        for s in _station_search(q):
            eva_list = s.get("evaNumbers") or []
            eva = None
            if isinstance(eva_list, list) and eva_list:
                eva = eva_list[0].get("number")
            key = eva or s.get("stationNumber") or s.get("id") or s.get("name")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            combined.append(s)

    ranked = rank_stations(combined, qn)

    # 100% exact name match vs canonical (aliased) name
    if ranked and ranked[0][1] >= 100 and _norm(ranked[0][0].get("name", "")) == qn:
        return ranked[0][0], []

    if not ranked:
        return None, []

    candidates = [s for (s, _) in ranked[:limit]]
    return None, candidates


def get_station_id_and_name(station_query: str) -> Tuple[Optional[int], Optional[str]]:
    primary = _apply_aliases(station_query)
    qn = _norm(primary)

    combined = []
    seen_keys = set()
    for q in dict.fromkeys([primary, station_query]):
        if not q:
            continue
        for s in _station_search(q):
            eva_list = s.get("evaNumbers") or []
            eva = None
            if isinstance(eva_list, list) and eva_list:
                eva = eva_list[0].get("number")
            key = eva or s.get("stationNumber") or s.get("id") or s.get("name")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            combined.append(s)

    best = _pick_best_station(combined, qn)
    if best:
        return best["evaNumbers"][0]["number"], best.get("name") or station_query
    return None, None


# ================== DB PLAN/FCHG MODELS ==================
@dataclass
class Event:
    id: str
    line_label: str
    pt: Optional[datetime.datetime] = None
    ct: Optional[datetime.datetime] = None
    pp: Optional[str] = None
    cp: Optional[str] = None
    dest: Optional[str] = None
    canceled: bool = False
    raw_tl: Dict[str, str] = field(default_factory=dict)
    raw_node_attrs: Dict[str, str] = field(default_factory=dict)

    def effective_time(self) -> Optional[datetime.datetime]:
        return self.ct or self.pt

    def delay_minutes(self) -> Optional[int]:
        if self.pt and self.ct:
            delta = int((self.ct - self.pt).total_seconds() // 60)
            return delta if delta != 0 else None
        return None

# Cache for /plan
PLAN_CACHE: Dict[Tuple[int,str,str], Tuple[float,List[Event]]] = {}

def _requests_get(url: str, headers: dict) -> Optional[str]:
    """GET with simple retries."""
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        if attempt < HTTP_RETRIES:
            time.sleep(0.3 * (2**attempt))
    return None

def _parse_time(code: Optional[str], tz: ZoneInfo) -> Optional[datetime.datetime]:
    if not code or len(code) < 10:
        return None
    try:
        yy = int(code[0:2]); mm = int(code[2:4]); dd = int(code[4:6])
        HH = int(code[6:8]);  MM = int(code[8:10])
        return datetime.datetime(2000+yy, mm, dd, HH, MM, tzinfo=tz)
    except Exception:
        return None

def _line_from_nodes(tl: Optional[ET.Element], dp_or_ar: ET.Element) -> str:
    l_attr = (dp_or_ar.attrib.get("l") or "").strip()
    if l_attr:
        up = l_attr.upper()
        if up.startswith("S"):
            return up
        if re.match(r"^\d+[A-Z]?$", up):
            return f"S{up}"
        return f"S{up}"

    if tl is not None:
        c = (tl.attrib.get("c") or "").upper()
        n = (tl.attrib.get("n") or "").strip()
        if c == "S":
            n_clean = re.sub(r"[^0-9A-Z]", "", n).upper()
            if n_clean:
                return n_clean if n_clean.startswith("S") else f"S{n_clean}"
            return "S"
        if c and n:
            return f"{c} {n}"
        if c:
            return c
    return "S"

def _dest_from_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    parts = path.split("|")
    return parts[-1] if parts else None

def fetch_plan(eva: int, date: str, hour: str, tz: ZoneInfo) -> List[Event]:
    key = (eva, date, hour)
    now = time.time()
    cached = PLAN_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    headers = {"Accept": "application/xml","DB-Client-Id": CLIENT_ID,"DB-Api-Key": API_KEY_DB}
    url = f"{DB_BASE}/plan/{eva}/{date}/{hour}"
    xml_text = _requests_get(url, headers)
    events: List[Event] = []
    if not xml_text:
        PLAN_CACHE[key] = (now + 60, events)
        return events

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        PLAN_CACHE[key] = (now + 60, events)
        return events

    for s in root.findall("s"):
        sid = s.attrib.get("id")
        if not sid:
            continue
        tl = s.find("tl")
        if tl is None or (tl.attrib.get("c") or "").upper() != "S":
            continue

        dp = s.find("dp")
        if dp is None:
            continue

        pt = _parse_time(dp.attrib.get("pt"), tz)
        pp = dp.attrib.get("pp")
        dest = _dest_from_path(dp.attrib.get("ppth"))
        line = _line_from_nodes(tl, dp)

        events.append(Event(
            id=sid,
            line_label=line,
            pt=pt,
            pp=pp,
            dest=dest,
            raw_tl = tl.attrib if tl is not None else {},
            raw_node_attrs = dict(dp.attrib),
        ))

    PLAN_CACHE[key] = (now + 90, events)
    return events

def fetch_fchg(eva: int, tz: ZoneInfo) -> Dict[str, Event]:
    """Parse changes; only departures matter for our list."""
    headers = {"Accept": "application/xml","DB-Client-Id": CLIENT_ID,"DB-Api-Key": API_KEY_DB}
    url = f"{DB_BASE}/fchg/{eva}"
    xml_text = _requests_get(url, headers)
    changes: Dict[str, Event] = {}
    if not xml_text:
        return changes
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return changes

    for s in root.findall("s"):
        sid = s.attrib.get("id")
        if not sid:
            continue

        tl = s.find("tl")
        dp = s.find("dp")
        if dp is None:
            continue

        ct = _parse_time(dp.attrib.get("ct"), tz)
        cp = dp.attrib.get("cp")
        # cancellation at this stop
        cs = (dp.attrib.get("cs") or "").lower()
        canceled = cs in ("c", "x", "1", "true", "y")

        pt = _parse_time(dp.attrib.get("pt"), tz)
        pp = dp.attrib.get("pp")
        cpth = dp.attrib.get("cpth")
        ppth = dp.attrib.get("ppth")
        dest = _dest_from_path(cpth if cpth else ppth)

        line = _line_from_nodes(tl, dp)

        changes[sid] = Event(
            id=sid,
            line_label=line,
            pt=pt,
            ct=ct,
            pp=pp,
            cp=cp,
            dest=dest,
            canceled=canceled,
            raw_tl = tl.attrib if tl is not None else {},
            raw_node_attrs = dict(dp.attrib),
        )
    return changes

def merge_plan_with_changes(plan: List[Event], changes: Dict[str, Event]) -> List[Event]:
    by_id: Dict[str, Event] = {e.id: e for e in plan}
    for sid, ch in changes.items():
        if sid in by_id:
            base = by_id[sid]
            if ch.line_label: base.line_label = ch.line_label
            if ch.ct: base.ct = ch.ct
            if ch.cp: base.cp = ch.cp
            if ch.pt and not base.pt: base.pt = ch.pt
            if ch.pp and not base.pp: base.pp = ch.pp
            if ch.dest: base.dest = ch.dest
            base.canceled = base.canceled or ch.canceled
            base.raw_tl.update(ch.raw_tl)
            base.raw_node_attrs.update(ch.raw_node_attrs)
        else:
            by_id[sid] = ch
    return list(by_id.values())

# ================== SERVICE: get_departures(eva) ==================
def get_departures_window(
    eva: int,
    now_local: datetime.datetime,
    max_items: int = 15,
    selected_line: Optional[str] = None
) -> Tuple[List[Event], bool]:
    """
    Returns (events, live_ok)
      - events: 0..15 within [now-5m, now+60m]
      - live_ok: whether fchg endpoint succeeded
    """
    tz = ZoneInfo("Europe/Berlin")
    now_local = now_local.astimezone(tz)
    prev = now_local - timedelta(minutes=5)
    horizon = now_local + timedelta(minutes=60)

    # Часы для получения плана
    d1 = now_local.strftime("%y%m%d")
    h1 = now_local.strftime("%H")
    dt2 = now_local + timedelta(hours=1)
    d2 = dt2.strftime("%y%m%d")
    h2 = dt2.strftime("%H")

    # Если окно начинается в предыдущем часе, подтягиваем и его,
    # чтобы корректно отображать поезда с отправлением 12:59,
    # которые уехали фактически позже, уже после смены часа.
    plan_all: Dict[str, Event] = {}
    if prev.hour != now_local.hour:
        dt0 = now_local - timedelta(hours=1)
        d0 = dt0.strftime("%y%m%d")
        h0 = dt0.strftime("%H")
        plan0 = fetch_plan(eva, d0, h0, tz)
        for e in plan0:
            plan_all[e.id] = e

    # Текущий и следующий час
    plan1 = fetch_plan(eva, d1, h1, tz)
    plan2 = fetch_plan(eva, d2, h2, tz)
    for e in plan1 + plan2:
        plan_all[e.id] = e

    plan_list = list(plan_all.values())

    live_ok = True
    try:
        changes = fetch_fchg(eva, tz)
    except Exception:
        changes = {}
        live_ok = False

    merged = merge_plan_with_changes(plan_list, changes)

    if selected_line:
        sel = selected_line.upper().strip()
        merged = [e for e in merged if (e.line_label or "").upper().startswith(sel)]

    def in_window(ev: Event) -> bool:
        t = ev.effective_time() or ev.pt
        if not t:
            return False
        return (prev <= t <= horizon)

    filtered = [e for e in merged if in_window(e)]
    filtered.sort(key=lambda e: e.effective_time() or e.pt)
    return filtered[:max_items], live_ok


def format_departure_html(ev, context) -> str:
    import html as _html
    line_label = ev.line_label or "S"
    dest       = ev.dest or "—"
    arrow      = " → "

    # localized parts
    at_word       = T(context, "at_word")
    platform_word = T(context, "platform_word")
    canceled_word = T(context, "canceled_word")

    t_eff = ev.effective_time() or ev.pt
    if t_eff:
        hhmm_eff = t_eff.strftime("%H:%M")
        time_html = hhmm_eff
        if ev.pt and ev.ct and ev.ct != ev.pt:
            hhmm_pt = ev.pt.strftime("%H:%M")
            time_html = f"<s>{hhmm_pt}</s> {hhmm_eff}"
    else:
        time_html = ""

    p_old = ev.pp or ""
    p_new = ev.cp or ""
    if p_new and p_old and p_new != p_old:
        platform_html = f"{platform_word} {_html.escape(p_old)} → {_html.escape(p_new)}"
    elif p_new:
        platform_html = f"{platform_word} {_html.escape(p_new)}"
    elif p_old:
        platform_html = f"{platform_word} {_html.escape(p_old)}"
    else:
        platform_html = ""

    delay_html = ""  # оставлено выключенным

    tail_parts = []
    if time_html:
        tail_parts.append(time_html)
    if platform_html:
        tail_parts.append(platform_html)
    if delay_html:
        tail_parts.append(delay_html)

    tail = (", " + ", ".join(tail_parts)) if tail_parts else ""
    base = f"{_html.escape(line_label)}{arrow}{_html.escape(dest)}"
    if time_html:
        base = f"{base}{at_word}{time_html}"
        tail = (", " + ", ".join([p for p in [platform_html, delay_html] if p])) if (platform_html or delay_html) else ""

    result = base + (tail if tail else "")

    if ev.canceled:
        return f"<s>{result}</s>  {canceled_word}"

    return result

# ================== AUTO-DELETE (asyncio, без JobQueue) ==================
async def _sleep_and_delete(bot, chat_id: int, message_id: int, delay: int):
    if delay <= 0:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # Игнорируем любые ошибки удаления (сообщение уже удалено/нет прав/и т.п.)
        pass

def schedule_autodelete(context: ContextTypes.DEFAULT_TYPE, message):
    if AUTO_DELETE_SECONDS <= 0 or message is None:
        return
    # Создаём задачу в текущем asyncio loop
    asyncio.create_task(_sleep_and_delete(context.bot, message.chat_id, message.message_id, AUTO_DELETE_SECONDS))


async def safe_send_html(message_func, text_html: str):
    try:
        return await message_func(text_html, parse_mode="HTML", disable_web_page_preview=True)
    except BadRequest:
        txt = text_html
        txt = re.sub(r"(?is)<\s*br\b[^>]*>", "\n", txt)
        txt = re.sub(r"(?is)</\s*p\s*>", "\n\n", txt)
        txt = re.sub(r"(?is)<[^>]+>", "", txt)
        txt = html.unescape(txt)
        return await message_func(txt, disable_web_page_preview=True)

# Удобные обёртки, чтобы не забывать планировать удаление
async def reply_and_autodelete(context: ContextTypes.DEFAULT_TYPE, message_obj, text: str, **kwargs):
    msg = await message_obj.reply_text(text, **kwargs)
    schedule_autodelete(context, msg)
    return msg

async def send_html_and_autodelete(context: ContextTypes.DEFAULT_TYPE, message_func, text_html: str):
    msg = await safe_send_html(message_func, text_html)
    schedule_autodelete(context, msg)
    return msg

async def edit_and_autodelete(context: ContextTypes.DEFAULT_TYPE, callback_query, text: str, **kwargs):
    await callback_query.edit_message_text(text, **kwargs)
    try:
        schedule_autodelete(context, callback_query.message)
    except Exception:
        pass

# ================== UI HELPERS ==================

# ================== UI HELPERS ==================
def nav_menu(context):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(T(context, "btn_service_messages"), callback_data=CB_ACT_MSG),
            InlineKeyboardButton(T(context, "btn_train_departures_short"),  callback_data=CB_ACT_DEP),
        ],
        [InlineKeyboardButton(T(context, "btn_change_line"), callback_data=CB_BACK_MAIN)]
    ])

def line_picker_markup(context):
    rows = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(1,5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(5,9)],
    ]
    return InlineKeyboardMarkup(rows)

def lang_picker_markup():
    labels = {
        "de": "Deutsch",
        "en": "English",
        "uk": "Українська",
        # "ru": "Русский",
    }
    buttons = [InlineKeyboardButton(labels.get(code, code), callback_data=f"{CB_LANG_PREFIX}{code}") for code in SUPPORTED_LANGS]
    return InlineKeyboardMarkup([buttons])

async def safe_send_html(message_func, text_html: str):
    try:
        return await message_func(text_html, parse_mode="HTML", disable_web_page_preview=True)
    except BadRequest:
        txt = text_html
        txt = re.sub(r"(?is)<\s*br\b[^>]*>", "\n", txt)
        txt = re.sub(r"(?is)</\s*p\s*>", "\n\n", txt)
        txt = re.sub(r"(?is)<[^>]+>", "", txt)
        txt = html.unescape(txt)
        return await message_func(txt, disable_web_page_preview=True)

def short_id_for_message(msg):
    basis = f"{msg.get('id','')}-{msg.get('title','')}-{msg.get('publication','')}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]

# ================== BOT HANDLERS (Messages) ==================
def fetch_line_messages_safe(line: str):
    data = fetch_messages()
    return filter_line_messages(data, line)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    # 🔹 Amplitude: событие на /start
    user = update.effective_user
    if user:
        ref = None
        try:
            # /start foo → context.args = ["foo"]
            if getattr(context, "args", None):
                ref = context.args[0]
        except Exception:
            pass

        props = {
            "via": "command",  # можно потом разделять, если появится другой вход
        }
        if ref:
            props["ref"] = ref

        track_analytics_event(
            user.id,
            "bot_start",
            props,
        )
    await update.message.reply_text(T(context, "choose_language"), reply_markup=lang_picker_markup())

async def on_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.replace(CB_LANG_PREFIX, "")
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    context.user_data["lang"] = lang
     # 🔹 Amplitude: выбор языка через кнопку
    user = update.effective_user
    if user:
        track_analytics_event(
            user.id,
            "language_selected",
            {
                "lang": lang,
                "via": "inline_button",
            },
        )

    #await q.edit_message_text(T(context, "choose_line"))
    #await q.message.reply_text(T(context, "tip_lang"))
    await q.message.reply_text(T(context, "choose_line"), reply_markup=line_picker_markup(context))

async def on_line_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = q.data.replace(CB_LINE_PREFIX, "")
    context.user_data["line"] = line
    # 🔹 Amplitude: выбор языка через кнопку
    user = update.effective_user
    if user:
        track_analytics_event(
            user.id,
            "line_selected",
            {
                "line": line,
                "via": "inline_button",
            },
        )
    #await q.edit_message_text(T(context, "you_selected_line", line=line))
    await q.message.reply_text(
        T(context, "you_selected_line", line=line),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(T(context, "btn_service_messages"), callback_data=CB_ACT_MSG)],
            [InlineKeyboardButton(T(context, "btn_train_departures"), callback_data=CB_ACT_DEP)],
            [InlineKeyboardButton(T(context, "btn_back_main"), callback_data=CB_BACK_MAIN)],
        ])
    )

async def on_show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = context.user_data.get("line", "S2")
    # 🔹 Amplitude: запрос сервисных сообщений
    user = update.effective_user
    if user:
        track_analytics_event(
            user.id,
            "service_messages_request",
            {
                "line": line,
                "via": "button",
            },
        )
        
    try:
        msgs = fetch_line_messages_safe(line)
        context.user_data["msg_map"] = {}

        if not msgs:
            await q.message.reply_text(T(context, "no_messages_for_line", line=line))
            await q.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))
            return

        await q.message.reply_text(T(context, "service_messages_for_line", line=line), parse_mode="HTML")

        for m in msgs:
            mid = short_id_for_message(m)
            context.user_data["msg_map"][mid] = m

            title_de = m.get("title", "Ohne Titel")
            pub      = m.get("publication")
            pub_s    = datetime.datetime.fromtimestamp(pub/1000, timezone.utc).strftime("%d.%m.%Y %H:%M") if pub else "?"

            title_shown = TR_MSG(context, title_de, is_html=True)
            #text = f"<b>{html.escape(title_shown)}</b>\n🕓 {pub_s} UTC"
            text = f"<b>{to_tg_html(title_shown)}</b>\n🕓 {pub_s} UTC"


            kb = InlineKeyboardMarkup([[InlineKeyboardButton(T(context, "details"), callback_data=f"{CB_DETAIL_PREFIX}{mid}")]])
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        await q.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))

    except Exception as e:
        await q.message.reply_text(T(context, "fetch_error", error=html.escape(str(e))))
        await q.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))

async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mid = q.data.replace(CB_DETAIL_PREFIX, "")
    m = (context.user_data.get("msg_map") or {}).get(mid)
    if not m:
        await q.message.reply_text(T(context, "message_details_not_found"))
        await q.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))
        return

    title_de = m.get("title", "Ohne Titel")
    desc_de  = m.get("description", "") or ""
    pub      = m.get("publication")
    pub_s    = datetime.datetime.fromtimestamp(pub/1000, timezone.utc).strftime("%d.%m.%Y %H:%M") if pub else "?"

    title_out = TR_MSG(context, title_de, is_html=True)
    desc_out  = TR_MSG(context, desc_de, is_html=True)

    #text_html = f"📢 <b>{html.escape(title_out)}</b>\n🕓 {pub_s} UTC\n\n{desc_out}"
    #await safe_send_html(q.message.reply_text, text_html)
    text_html = f"📢 <b>{to_tg_html(title_out)}</b>\n🕓 {pub_s} UTC\n\n{to_tg_html(desc_out)}"
    await q.message.reply_text(text_html, parse_mode="HTML", disable_web_page_preview=True)
    
    await q.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))

# ================== DEPARTURES (PLAN ⊕ FCHG) ==================
async def on_departures_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # включаем режим "следующее текстовое сообщение — это ввод станции"
    context.user_data["await_station"] = True

    recent = context.user_data.get("recent_stations") or []
    station_map = context.user_data.get("station_map") or {}
    rows = []

    prompt_key = "enter_station_prompt"

    if recent:
        # есть последние успешные станции → рисуем по ним кнопки
        for item in recent:
            eva = item.get("eva")
            name = item.get("name")
            if not eva or not name:
                continue
            eva_str = str(eva)
            station_map[eva_str] = name
            rows.append([
                InlineKeyboardButton(
                    name,
                    callback_data=f"{CB_PICK_STATION}{eva_str}",
                )
            ])
        context.user_data["station_map"] = station_map
        prompt_key = "enter_station_or_choose_prompt"

    # в любом случае добавляем "Назад"
    rows.append([
        InlineKeyboardButton(
            T(context, "btn_back"),
            callback_data=CB_BACK_ACTIONS,
        )
    ])

    await q.edit_message_text(
        T(context, prompt_key),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_station_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_station"):
        return
    context.user_data["await_station"] = False

    station_in = update.message.text.strip()
     # 🔹 Amplitude: поиск станции
    user = update.effective_user
    if user:
        track_analytics_event(
            user.id,
            "station_search",
            {
                "query": station_in,
                "line": context.user_data.get("line"),
            },
        )
    await update.message.reply_text(T(context, "searching_station", station=station_in))

    try:
        best_exact, candidates = find_station_candidates(station_in, limit=3)

        if best_exact:
            eva = best_exact["evaNumbers"][0]["number"]
            station_name = best_exact.get("name") or station_in
            # apply specific EVA for specific station names
            eva = EVA_OVERRIDES.get((station_name, eva), eva)

            await _send_departures_for_eva(update.message, context, eva, station_name)
            return

        if not candidates:
             # 🔹 Amplitude: неудачній поиск станции
            user = update.effective_user
            if user:
                track_analytics_event(
                    user.id,
                    "station_search_not_found",
                    {
                    "query": station_in,
                    "line": context.user_data.get("line"),
                    },
                )
            # >>> Здесь показываем компактное меню "Search again" + "Back"
            await update.message.reply_text(
                T(context, "no_station_found"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(T(context, "btn_search_again"), callback_data=CB_ACT_DEP)],
                    [InlineKeyboardButton(T(context, "btn_back"), callback_data=CB_BACK_ACTIONS)],
                ])
            )
            return

        # cache names for on_station_picked
        context.user_data["station_map"] = {}

        rows = []
        for s in candidates:
            name = s.get("name", "—")
            eva = s["evaNumbers"][0]["number"]
            eva = EVA_OVERRIDES.get((name, eva), eva)

            context.user_data["station_map"][str(eva)] = name
            
            muni  = s.get("municipality") or ""
            state = s.get("federalStateCode") or ""
            label = f"{name} ({eva})"
            if muni or state:
                extra = " — ".join([p for p in [muni, state] if p])
                label = f"{name} · {extra} ({eva})"
            rows.append([InlineKeyboardButton(label, callback_data=f"{CB_PICK_STATION}{eva}")])
        
  
        rows.append([InlineKeyboardButton(T(context, "btn_back"), callback_data=CB_BACK_ACTIONS)])

        await update.message.reply_text(
            T(context, "choose_station"),
            reply_markup=InlineKeyboardMarkup(rows)
        )
    except Exception as e:
        await update.message.reply_text(T(context, "station_search_error", error=html.escape(str(e))))
        await update.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))

async def on_back_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["await_station"] = False
    await q.edit_message_text(
        T(context, "choose_next"),
        reply_markup=nav_menu(context)
    )

async def _send_departures_for_eva(message_obj, context, eva: int, station_name: str):
    now_local = datetime.datetime.now(ZoneInfo("Europe/Berlin"))

    # 🔹 Amplitude: запрос расписания
    user = getattr(message_obj, "from_user", None)
    if user:
        track_analytics_event(
            user.id,
            "departures_request",
            {
                "line": context.user_data.get("line"),
                "station_name": station_name,
                "eva": eva,
            },
        )

    try:
        selected_line = context.user_data.get("line")
        events, live_ok = get_departures_window(
            eva,
            now_local,
            max_items=15,
            selected_line=selected_line,
        )
    except Exception as e:
        await message_obj.reply_text(
            T(context, "fetch_error", error=str(e)),
            reply_markup=nav_menu(context),
        )
        return

    # ✅ Обновляем историю последних станций (максимум 3, без дублей по EVA)
    recent = context.user_data.get("recent_stations") or []
    entry = {"eva": eva, "name": station_name}
    recent = [r for r in recent if r.get("eva") != eva]
    recent.insert(0, entry)
    context.user_data["recent_stations"] = recent[:3]

    line_suffix = f" — {selected_line}" if selected_line else ""
    header = T(context, "departures_header", station=station_name, line_suffix=line_suffix)
    await safe_send_html(message_obj.reply_text, f"<b>{html.escape(header)}</b>")

    out_lines = []
    for ev in events:
        line_html = format_departure_html(ev, context)
        if line_html:
            out_lines.append(line_html)

    if not out_lines:
        # 🔹 Amplitude: no_departures_found
        if user:
            track_analytics_event(
                user.id,
                "no_departures_found",
                {
                    "line": context.user_data.get("line"),
                    "station_name": station_name,
                    "eva": eva,
                },
            )
        await message_obj.reply_text(
            T(context, "no_departures"),
            reply_markup=nav_menu(context),
        )
        return

    footer = ""
    if not live_ok:
        footer = "\n\n" + T(context, "live_unavailable")

    await safe_send_html(message_obj.reply_text, "\n".join(out_lines) + footer)
    await message_obj.reply_text(
        T(context, "choose_next"),
        reply_markup=nav_menu(context),
    )

async def on_station_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data  # e.g. "ST:8001825"
    if not data.startswith(CB_PICK_STATION):
        return
    eva_str = data[len(CB_PICK_STATION):].strip()

    station_map = context.user_data.get("station_map") or {}
    station_name = station_map.get(eva_str) or f"EVA {eva_str}"

    try:
        eva = int(eva_str)
    except ValueError:
        await q.message.reply_text(
            T(context, "invalid_station_id"),
            reply_markup=nav_menu(context)
        )
        return

    await _send_departures_for_eva(q.message, context, eva, station_name)

# ----- Back / Change line -----
async def on_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(context)
    context.user_data.clear()
    context.user_data["lang"] = lang
    await q.edit_message_text(T(context, "choose_line"), reply_markup=line_picker_markup(context))

# ----- TG commands --------
async def cmd_line(update, context):
    # /line S2   -> set immediately
    if context.args:
        line = context.args[0].upper()
        if not line.startswith("S"):
            line = "S" + line
        context.user_data["line"] = line
        await update.message.reply_text(T(context, "you_selected_line", line=line), reply_markup=nav_menu(context))
        return
    await update.message.reply_text(T(context, "choose_line"), reply_markup=line_picker_markup(context))

async def cmd_messages(update, context):
    line = context.user_data.get("line", "S2")

    # 🔹 Amplitude: запрос сервисных сообщений по команде
    user = update.effective_user
    if user:
        track_analytics_event(
            user.id,
            "service_messages_request",
            {
                "line": line,
                "via": "command",
            },
        )
    
    try:
        msgs = fetch_line_messages_safe(line)
        context.user_data["msg_map"] = {}

        if not msgs:
            await update.message.reply_text(T(context, "no_messages_for_line", line=line))
            await update.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))
            return

        await update.message.reply_text(T(context, "service_messages_for_line", line=line), parse_mode="HTML")

        for m in msgs:
            mid = short_id_for_message(m)
            context.user_data["msg_map"][mid] = m
            title_de = m.get("title", "Ohne Titel")
            pub      = m.get("publication")
            pub_s    = datetime.datetime.fromtimestamp(pub/1000, timezone.utc).strftime("%d.%m.%Y %H:%M") if pub else "?"
            title_shown = TR_MSG(context, title_de, is_html=True)
            text = f"<b>{html.escape(title_shown)}</b>\n🕓 {pub_s} UTC"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(T(context, "details"), callback_data=f"{CB_DETAIL_PREFIX}{mid}")]])
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        await update.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))
    except Exception as e:
        await update.message.reply_text(T(context, "fetch_error", error=html.escape(str(e))))
        await update.message.reply_text(T(context, "choose_next"), reply_markup=nav_menu(context))

async def cmd_departures(update, context):
    if context.args:
        # /departures Erding
        update.message.text = " ".join(context.args)
        await on_station_input(update, context)
        return
    context.user_data["await_station"] = True
    await update.message.reply_text(
        T(context, "enter_station_prompt"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(T(context, "btn_back"), callback_data=CB_BACK_ACTIONS)]])
    )

async def cmd_lang(update, context):
    if context.args:
        lang = context.args[0].lower()
        if lang not in SUPPORTED_LANGS:
            await update.message.reply_text(T(context, "cmd_lang_usage"))
            return
        context.user_data["lang"] = lang
         # 🔹 Amplitude: выбор языка через /lang
        user = update.effective_user
        if user:
            track_analytics_event(
                user.id,
                "language_selected",
                {
                    "lang": lang,
                    "via": "command",
                },
            )
        
        await update.message.reply_text(T(context, "language_updated"), reply_markup=nav_menu(context))
        return
    await update.message.reply_text(T(context, "choose_language"), reply_markup=lang_picker_markup())
# ================== FEEDBACK VIA /feedback ==================
async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_CHAT_ID == 0:
        await update.message.reply_text(T(context, "feedback_unavailable"))
        return
    context.user_data["await_feedback"] = True
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(T(context, "btn_cancel_feedback"), callback_data="A:FDBK_CANCEL")]])
    await update.message.reply_text(T(context, "feedback_prompt"), reply_markup=kb)

async def on_feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["await_feedback"] = False
    await q.edit_message_text(T(context, "feedback_cancelled"), reply_markup=nav_menu(context))

async def on_feedback_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_feedback"):
        return
    context.user_data["await_feedback"] = False

    if ADMIN_CHAT_ID == 0:
        await update.message.reply_text(T(context, "feedback_unavailable"), reply_markup=nav_menu(context))
        return

    user = update.effective_user
    chat = update.effective_chat
    text = update.message.text or ""
    anon = _anon_id(user.id) if FEEDBACK_SALT else "anonymous"
    lang = get_user_lang(context)
    line = context.user_data.get("line", "—")
    ts   = datetime.datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M:%S")

    payload = (
        f"📮 BOT Feedback\n"
        f"• anon_id: <code>{html.escape(anon)}</code>\n"
        f"• lang: {html.escape(lang)}\n"
        f"• line: {html.escape(line)}\n"
        f"• chat_id: <code>{chat.id}</code>\n"
        f"• time: {ts}\n\n"
        f"{html.escape(text)}"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=payload, parse_mode="HTML", disable_web_page_preview=True)
        await update.message.reply_text(T(context, "feedback_thanks"), reply_markup=nav_menu(context))
    except Exception as e:
        await update.message.reply_text(T(context, "fetch_error", error=html.escape(str(e))), reply_markup=nav_menu(context))
async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Спочатку фідбек
    if context.user_data.get("await_feedback"):
        return await on_feedback_message(update, context)
    # Далі пошук станції
    if context.user_data.get("await_station"):
        return await on_station_input(update, context)
    # Інакше ігноруємо
    return

# ===== Application factory =====
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters

def build_app() -> "Application":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers (як у тебе було; не видаляй свої)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("departures", cmd_departures))
    app.add_handler(CommandHandler("messages", cmd_messages))
    app.add_handler(CommandHandler("line", cmd_line))
    app.add_handler(CommandHandler("feedback", cmd_feedback))

    app.add_handler(CallbackQueryHandler(on_language, pattern=r"^LANG:"))
    app.add_handler(CallbackQueryHandler(on_line_selected,     pattern=r"^L:"))
    app.add_handler(CallbackQueryHandler(on_show_messages,     pattern=r"^A:MSG$"))
    app.add_handler(CallbackQueryHandler(on_departures_prompt, pattern=r"^A:DEP$"))
    app.add_handler(CallbackQueryHandler(on_back_main,         pattern=r"^B:MAIN$"))
    app.add_handler(CallbackQueryHandler(on_feedback_cancel,   pattern=r"^A:FDBK_CANCEL$"))
    app.add_handler(CallbackQueryHandler(on_station_picked,    pattern=r"^ST:"))
    app.add_handler(CallbackQueryHandler(on_back_actions,      pattern=r"^B:ACT$"))
    app.add_handler(CallbackQueryHandler(on_details,           pattern=r"^D:"))

    # ЄДИНИЙ роутер для тексту (щоб /feedback не ламався)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))
    return app
# ===== Webhook server (FastAPI) =====
import os, asyncio
from fastapi import FastAPI, Request, Response
import uvicorn
from telegram import Update

WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE")              # наприклад https://your-app.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")        # довільний токен, можна пустим
PORT           = int(os.getenv("PORT", "10000"))

fastapi_app = FastAPI()
tg_app = None  # сюди покладемо PTB Application після build_app()

@fastapi_app.get("/healthz")
async def healthz():
    return {"ok": True}

@fastapi_app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)

@fastapi_app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    # опціональна перевірка секрету
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return Response(status_code=401)
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.update_queue.put(update)
    return Response(status_code=200)

# ===== Runners =====
async def run_webhook():
    global tg_app
    tg_app = build_app()

    await tg_app.initialize()
    await tg_app.start()
    if not WEBHOOK_BASE:
        raise RuntimeError("WEBHOOK_BASE is not set for webhook mode")

    await tg_app.bot.set_webhook(
        url=f"{WEBHOOK_BASE}/webhook/{BOT_TOKEN}",
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True
    )

    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await tg_app.stop()
        await tg_app.shutdown()

async def run_polling():
    app = build_app()
    # стандартний асинхронний цикл polling без .run_polling(), щоби стилістично було симетрично
    await app.initialize()
    await app.start()
    try:
        await app.updater.start_polling(drop_pending_updates=True)  # PTB v20: updater існує в Application
        # або використай app.run_polling() якщо так зручніше:
        # await app.run_polling(close_loop=False)
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.stop()
        await app.shutdown()

# ================== WIRING ==================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["webhook", "polling", "auto"], default=os.getenv("RUN_MODE", "auto"))
    args = parser.parse_args()

    # auto: якщо є WEBHOOK_BASE або PORT (Render), стартуємо webhook; інакше polling
    mode = args.mode
    if mode == "auto":
        mode = "webhook" if WEBHOOK_BASE or os.getenv("PORT") else "polling"

    print(f"🚀 Starting bot in {mode} mode...")
    if mode == "webhook":
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())

