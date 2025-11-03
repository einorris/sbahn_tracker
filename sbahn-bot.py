# sbahn_bot.py
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
BOT_TOKEN   = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
CLIENT_ID   = os.getenv("DB_CLIENT_ID") or "YOUR_DB_CLIENT_ID"
API_KEY_DB  = os.getenv("DB_API_KEY")  or "YOUR_DB_API_KEY"
DEEPL_AUTH_KEY = os.getenv("DEEPL_AUTH_KEY")  # xxxxxxxx:fx

MVG_URL = "https://www.mvg.de/api/bgw-pt/v3/messages"
DB_BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"

HTTP_TIMEOUT = 5  # сек
HTTP_RETRIES = 2   # доп. попытки (итого 1+2)

# Short, safe callback keys
CB_LANG_PREFIX   = "LANG:"    # LANG:de / LANG:en / LANG:uk
CB_LINE_PREFIX   = "L:"       # e.g. L:S2
CB_ACT_MSG       = "A:MSG"
CB_ACT_DEP       = "A:DEP"
CB_BACK_MAIN     = "B:MAIN"
CB_DETAIL_PREFIX = "D:"

SUPPORTED_LANGS = ["de", "en", "uk"]  # Deutsch, English, Українська

# ================== TRANSLATION (DeepL) ==================
DEEPL_URL = "https://api-free.deepl.com/v2/translate"

def _deepl_supported_target(lang_code: str) -> str:
    return {"de": "DE", "en": "EN", "uk": "UK"}.get(lang_code, "EN")

def deepl_translate(text: str, target_lang: str, is_html: bool) -> str:
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

def get_user_lang(context) -> str:
    return context.user_data.get("lang", "en")

def TR_UI(context, text_en: str, is_html: bool=False) -> str:
    lang = get_user_lang(context)
    if lang == "en":
        return text_en
    return deepl_translate(text_en, _deepl_supported_target(lang), is_html)

def TR_MSG(context, text_de: str, is_html: bool=False) -> str:
    lang = get_user_lang(context)
    if lang == "de":
        return text_de
    return deepl_translate(text_de, _deepl_supported_target(lang), is_html)

# ================== MVG HELPERS ==================
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
    seen = {}
    for msg in messages:
        for line in msg.get("lines", []):
            if (line.get("transportType") in ("SBAHN", "S")) and (line.get("label") == line_label):
                if is_active(msg.get("incidentDurations", [])):
                    title = (msg.get("title") or "").strip()
                    pub = msg.get("publication", 0)
                    if title in seen:
                        if pub > seen[title].get("publication", 0):
                            seen[title] = msg
                    else:
                        seen[title] = msg
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=True)

# ================== STATION SEARCH ==================
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
    "hbf tief": "München Hbf",  # иногда прилетает как «Hbf (tief)»
    "hauptbahnhof": "München Hbf",

    "marienplatz": "München Marienplatz",
    "marienplatz (tief)": "München Marienplatz",

    "karlsplatz": "München Karlsplatz (Stachus)",
    "stachus": "München Karlsplatz (Stachus)",
    "karlsplatz (stachus)": "München Karlsplatz (Stachus)",

    "isartor": "München Isartor",
    "rosenheimer platz": "München Rosenheimer Platz",
    "hackerbrücke": "München Hackerbrücke",
    "hackerbruecke": "München Hackerbrücke",
    "donnersbergerbruecke": "München Donnersbergerbrücke",
    "donnersbergerbrücke": "München Donnersbergerbrücke",
    "laim": "München Laim",
    "pasing": "München-Pasing",
    "muenchen pasing": "München-Pasing",
    "münchen pasing": "München-Pasing",

    # Восток
    "ostbahnhof": "München Ost",
    "munich east": "München Ost",
    "muenchen ostbahnhof": "München Ost",
    "münchen ostbahnhof": "München Ost",
    "leuchtenbergring": "München Leuchtenbergring",
    "berg am laim": "München-Berg am Laim",
    "trudering": "München-Trudering",
    "riem": "München-Riem",

    # Юг/юго-восток (S3/S7/S20)
    "gising": "München Giesing",   # частая опечатка
    "giesing": "München Giesing",
    "harras": "München Harras",
    "mittersendling": "Mittersendling",
    "siemenswerke": "Siemenswerke",
    "solln": "München Solln",
    "fasangarten": "Fasangarten",
    "neuperlach süd": "Neuperlach Süd",
    "neuperlach sud": "Neuperlach Süd",
    "ebersberg" : "Ebersberg (Oberbay)",

    # Север/северо-восток (S1/S8/S3)
    "feldmoching": "München-Feldmoching",
    "moosach": "München-Moosach",
    "oberwiesenfeld": "Oberwiesenfeld",  # иногда лезет из U-Bahn — игнорируй при желании
    "unterföhring": "Unterföhring",
    "unterfoehring": "Unterföhring",
    "ismaning": "Ismaning",

    # Аэропорт
    "munich airport": "München Flughafen Terminal",
    "muc": "München Flughafen Terminal",
    "flughafen münchen": "München Flughafen Terminal",
    "flughafen muenchen": "München Flughafen Terminal",
    "munich international airport": "München Flughafen Terminal",
    "visitor park": "München Flughafen Besucherpark",
    "besucherpark": "München Flughafen Besucherpark",

    # Восточная дуга S2 до Эрдинга
    "erding": "Erding",
    "altenerding": "Altenerding",
    "aufhausen (oberbay)": "Aufhausen (Oberbay)",
    "markt schwaben": "Markt Schwaben",
    "grub (oberbay)": "Grub (Oberbay)",
    "heimstetten": "Heimstetten",
    "daglfing": "München-Daglfing",
    "englschalking": "München-Englschalking",
    "rietmoos": "Riemerling", 
    }
    return aliases.get(qn, q)

def _station_search(query: str):
    url = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
    headers = {
        "Accept": "application/json",
        "DB-Client-Id": CLIENT_ID,
        "DB-Api-Key": API_KEY_DB,
    }
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, params={"searchstring": query}, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                return []
            return r.json().get("result", []) or []
        except Exception:
            if attempt == HTTP_RETRIES:
                return []
            time.sleep(0.3 * (2**attempt))

def _pick_best_station(results, query_norm: str):
    best = None; best_score = -1
    for s in results:
        if not s.get("evaNumbers"): continue
        name = s.get("name", ""); nn = _norm(name)
        score = 0
        if nn == query_norm: score += 100
        if nn.startswith(query_norm) or query_norm.startswith(nn): score += 50
        if query_norm in nn: score += 25
        if s.get("federalStateCode") == "DE-BY": score += 5
        if score > best_score:
            best = s; best_score = score
    return best

def get_station_id_and_name(station_query: str) -> Tuple[Optional[int], Optional[str]]:
    primary = _apply_aliases(station_query)
    qn = _norm(primary)

    results = _station_search(primary)
    best = _pick_best_station(results, qn)
    if best:
        eva = best["evaNumbers"][0]["number"]
        return eva, best.get("name") or station_query

    wildcard = f"*{station_query}*"
    results = _station_search(wildcard)
    best = _pick_best_station(results, _norm(station_query))
    if best:
        eva = best["evaNumbers"][0]["number"]
        return eva, best.get("name") or station_query

    for variant in (f"München*{station_query}*", f"Muenchen*{station_query}*"):
        results = _station_search(variant)
        best = _pick_best_station(results, _norm(variant.replace("*"," ")))
        if best:
            eva = best["evaNumbers"][0]["number"]
            return eva, best.get("name") or station_query

    return None, None

# ================== DB PLAN/FCHG MODELS ==================
@dataclass
class Event:
    id: str
    line_label: str               # e.g. S2 / ICE / etc
    pt: Optional[datetime.datetime] = None   # planned time
    ct: Optional[datetime.datetime] = None   # changed time
    pp: Optional[str] = None      # planned platform
    cp: Optional[str] = None      # changed platform
    dest: Optional[str] = None    # terminal station
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

# ---------- cache for /plan ----------
# key: (eva, yyyymmdd, HH) -> (expires_ts, List[Event])
PLAN_CACHE: Dict[Tuple[int,str,str], Tuple[float,List[Event]]] = {}

def _requests_get(url: str, headers: dict) -> Optional[str]:
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                return None
            return r.text
        except Exception:
            if attempt == HTTP_RETRIES:
                return None
            time.sleep(0.3 * (2**attempt))

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
    """Return normalized line label:
       - prefer dp/ar attribute 'l' (already 'S2' or just '2')
       - fallback to <tl c=... n=...>
    """
    l_attr = (dp_or_ar.attrib.get("l") or "").strip()
    if l_attr:
        up = l_attr.upper()
        # already like "S2", "S3E"
        if up.startswith("S"):
            return up
        # digits or digits+suffix -> prefix S
        if re.match(r"^\d+[A-Z]?$", up):
            return f"S{up}"
        # anything else -> still prefix S to be safe
        return f"S{up}"

    # fallback via <tl>
    if tl is not None:
        c = (tl.attrib.get("c") or "").upper()   # category: S, ICE, RE, ...
        n = (tl.attrib.get("n") or "").strip()
        if c == "S":
            # if tl has a number, use it
            n_clean = re.sub(r"[^0-9A-Z]", "", n).upper()
            if n_clean:
                # if n already starts with S (rare), avoid double S
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
        # ✅ Только S-Bahn
        if tl is None or (tl.attrib.get("c") or "").upper() != "S":
            continue

        dp = s.find("dp")
        if dp is None:
            continue  # только отправления

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
        # ✅ Только S-Bahn
        if tl is None or (tl.attrib.get("c") or "").upper() != "S":
            continue

        dp = s.find("dp")
        if dp is None:
            continue  # только отправления

        ct = _parse_time(dp.attrib.get("ct"), tz)
        cp = dp.attrib.get("cp")
        canceled = False
        cflag = dp.attrib.get("c") or dp.attrib.get("cn") or ""
        if str(cflag).lower() in ("1","y","true","c","x"):
            canceled = True

        pt = _parse_time(dp.attrib.get("pt"), tz)
        pp = dp.attrib.get("pp")
        dest = _dest_from_path(dp.attrib.get("cpth") or dp.attrib.get("ppth"))
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
    """Apply fchg over plan. Add ad-hoc from fchg if missing in plan."""
    by_id: Dict[str, Event] = {e.id: e for e in plan}
    # Apply changes
    for sid, ch in changes.items():
        if sid in by_id:
            base = by_id[sid]
            # Line can change (rare) — prefer change's line if present
            if ch.line_label: base.line_label = ch.line_label
            # Time/platform overrides
            if ch.ct: base.ct = ch.ct
            if ch.cp: base.cp = ch.cp
            if ch.pt and not base.pt: base.pt = ch.pt
            if ch.pp and not base.pp: base.pp = ch.pp
            # Destination/path might change
            if ch.dest: base.dest = ch.dest
            # Cancellation
            base.canceled = base.canceled or ch.canceled
            # Keep attrs (for debugging)
            base.raw_tl.update(ch.raw_tl)
            base.raw_node_attrs.update(ch.raw_node_attrs)
        else:
            # Ad-hoc departure — include as is
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
    - events: 0..15 merged and filtered departures within [now-5m, now+60m]
    - live_ok: whether fchg endpoint succeeded
    """
    tz = ZoneInfo("Europe/Berlin")
    now_local = now_local.astimezone(tz)
    prev = now_local - timedelta(minutes=5)
    horizon = now_local + timedelta(minutes=60)

    # determine two hours: current and next (handle wrap at 23->00 next day)
    d1 = now_local.strftime("%y%m%d")
    h1 = now_local.strftime("%H")
    dt2 = now_local + timedelta(hours=1)
    d2 = dt2.strftime("%y%m%d")
    h2 = dt2.strftime("%H")

    # fetch plan for both hours (cached)
    plan1 = fetch_plan(eva, d1, h1, tz)
    plan2 = fetch_plan(eva, d2, h2, tz)
    plan_all = {e.id: e for e in (plan1 + plan2)}  # dedupe by id
    plan_list = list(plan_all.values())

    # fetch live changes
    live_ok = True
    try:
        changes = fetch_fchg(eva, tz)
    except Exception:
        changes = {}
        live_ok = False

    merged = merge_plan_with_changes(plan_list, changes)

    # optional filter by selected S-line, e.g. "S2"
    if selected_line:
        sel = selected_line.upper().strip()
        merged = [e for e in merged if (e.line_label or "").upper().startswith(sel)]


    # filter window + only departures
    def in_window(ev: Event) -> bool:
        t = ev.effective_time() or ev.pt
        if not t:
            return False
        return (prev <= t <= horizon)

    filtered = [e for e in merged if in_window(e)]
    # sort by effective time (ct if exists, else pt)
    filtered.sort(key=lambda e: e.effective_time() or e.pt)
    return filtered[:max_items], live_ok

def format_departure_html(ev, context) -> str:
    """Возвращает одну строку HTML с учетом:
    - Линия (S2 / ICE 702 и т.п.)
    - Направление (терминальная станция)
    - Время: ct если есть, иначе pt; если ct != pt — pt зачеркнут
    - Платформа: cp если есть, иначе pp; если смена — "Gleis X → Y"
    - Задержка: +N мин
    - Отмена: 'Fällt aus'
    Пример: S2 → München Ost, <s>12:53</s> 12:58, Gleis 4, +5 min
    """
    # Линия/направление
    line_label = ev.line_label or "S"
    dest       = ev.dest or "—"
    arrow      = " → "

    # Время
    t_eff = ev.effective_time() or ev.pt
    if not t_eff:
        return f"{line_label}{arrow}{dest}"  # fallback

    hhmm_eff = t_eff.strftime("%H:%M")

    time_html = hhmm_eff
    if ev.pt and ev.ct:
        # Если запланированное время отличается — зачеркнем pt
        if ev.ct != ev.pt:
            hhmm_pt = ev.pt.strftime("%H:%M")
            time_html = f"<s>{hhmm_pt}</s> {hhmm_eff}"

    # Платформа
    platform_lbl = "Gleis"  # в DE UX привычно "Gleis"
    p_old = ev.pp or ""
    p_new = ev.cp or ""
    if p_new and p_old and p_new != p_old:
        platform_html = f"{platform_lbl} {html.escape(p_old)} → {html.escape(p_new)}"
    elif p_new:
        platform_html = f"{platform_lbl} {html.escape(p_new)}"
    elif p_old:
        platform_html = f"{platform_lbl} {html.escape(p_old)}"
    else:
        platform_html = ""

    # Задержка
    delay_html = ""
    dm = ev.delay_minutes()
    if dm is not None and dm != 0:
        sign = "+" if dm > 0 else ""
        # оставим "min" (у тебя так уже локализуется), можно заменить на TR_UI(context, " min")
        delay_html = f"{sign}{dm} min"

    # Отмена
    cancel_html = "Fällt aus" if ev.canceled else ""

    # Склейка блоков ", "-разделителями, без пустых
    tail_parts = [p for p in [time_html, platform_html, delay_html, cancel_html] if p]
    tail = ", ".join(tail_parts)

    return f"{html.escape(line_label)}{arrow}{html.escape(dest)}, {tail}"

# ================== UI HELPERS ==================
def nav_menu(context):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(TR_UI(context, "📰 Service Messages"),   callback_data=CB_ACT_MSG),
            InlineKeyboardButton(TR_UI(context, "🚉 Train departures"), callback_data=CB_ACT_DEP),
        ],
        [InlineKeyboardButton(TR_UI(context, "🆕 Change Line"), callback_data=CB_BACK_MAIN)]
    ])

def line_picker_markup():
    rows = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(1,5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(5,9)],
    ]
    return InlineKeyboardMarkup(rows)

def lang_picker_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Deutsch",      callback_data=f"{CB_LANG_PREFIX}de"),
            InlineKeyboardButton("English",      callback_data=f"{CB_LANG_PREFIX}en"),
            InlineKeyboardButton("Українська",   callback_data=f"{CB_LANG_PREFIX}uk"),
        ]
    ])

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

# ================== BOT HANDLERS (Messages — без изменений) ==================
def fetch_line_messages_safe(line: str):
    data = fetch_messages()
    return filter_line_messages(data, line)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Choose language / Sprache wählen / Оберіть мову:", reply_markup=lang_picker_markup())

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur = get_user_lang(context)
    key_state = "OK" if DEEPL_AUTH_KEY else "MISSING"
    await update.message.reply_text(
        f"Language: {cur}\nDeepL key: {key_state}\n\nChoose language / Sprache wählen / Оберіть мову:",
        reply_markup=lang_picker_markup()
    )

async def on_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.replace(CB_LANG_PREFIX, "")
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    context.user_data["lang"] = lang

    await q.edit_message_text(TR_UI(context, "🚆 Choose an S-Bahn line:"))
    await q.message.reply_text(TR_UI(context, "Tip: You can change language anytime with /lang"))
    await q.message.reply_text(TR_UI(context, "Lines:"), reply_markup=line_picker_markup())

async def on_line_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = q.data.replace(CB_LINE_PREFIX, "")
    context.user_data["line"] = line
    await q.edit_message_text(TR_UI(context, f"You selected {line}. Choose an action:"))
    await q.message.reply_text(
        TR_UI(context, "Actions:"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(TR_UI(context, "📰 Service Messages"), callback_data=CB_ACT_MSG)],
            [InlineKeyboardButton(TR_UI(context, "🚉 Train departures (by station)"), callback_data=CB_ACT_DEP)],
            [InlineKeyboardButton(TR_UI(context, "⬅️ Back to Main Menu"), callback_data=CB_BACK_MAIN)],
        ])
    )

async def on_show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = context.user_data.get("line", "S2")

    try:
        msgs = fetch_line_messages_safe(line)
        context.user_data["msg_map"] = {}

        if not msgs:
            await q.message.reply_text(TR_UI(context, f"No current messages for {line}."))
            await q.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))
            return

        await q.message.reply_text(TR_UI(context, f"📰 Service Messages for {line}"), parse_mode="HTML")

        for m in msgs:
            mid = short_id_for_message(m)
            context.user_data["msg_map"][mid] = m

            title_de = m.get("title", "Ohne Titel")
            pub      = m.get("publication")
            pub_s    = datetime.datetime.fromtimestamp(pub/1000, datetime.UTC).strftime("%d.%m.%Y %H:%M") if pub else "?"

            title_shown = TR_MSG(context, title_de, is_html=True)

            text = f"<b>{html.escape(title_shown)}</b>\n🕓 {pub_s} UTC"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(TR_UI(context, "🔍 Details"), callback_data=f"{CB_DETAIL_PREFIX}{mid}")]])
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        await q.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))

    except Exception as e:
        await q.message.reply_text(TR_UI(context, f"⚠️ Error: {html.escape(str(e))}"))
        await q.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))

async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mid = q.data.replace(CB_DETAIL_PREFIX, "")
    m = (context.user_data.get("msg_map") or {}).get(mid)
    if not m:
        await q.message.reply_text(TR_UI(context, "Message details not found."))
        await q.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))
        return

    title_de = m.get("title", "Ohne Titel")
    desc_de  = m.get("description", "") or ""
    pub      = m.get("publication")
    pub_s    = datetime.datetime.fromtimestamp(pub/1000, datetime.UTC).strftime("%d.%m.%Y %H:%M") if pub else "?"

    title_out = TR_MSG(context, title_de, is_html=True)
    desc_out  = TR_MSG(context, desc_de, is_html=True)

    text_html = f"📢 <b>{html.escape(title_out)}</b>\n🕓 {pub_s} UTC\n\n{desc_out}"
    await safe_send_html(q.message.reply_text, text_html)
    await q.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))

# ================== NEW: DEPARTURES (PLAN ⊕ FCHG) ==================
async def on_departures_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["await_station"] = True
    await q.edit_message_text(TR_UI(context, "Please enter the station name (e.g., Erding or Ostbahnhof):"))

async def on_station_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_station"):
        return
    context.user_data["await_station"] = False

    station_in = update.message.text.strip()
    await update.message.reply_text(TR_UI(context, f"🔍 Searching departures for {station_in}..."))

    eva, station_name = get_station_id_and_name(station_in)
    if not eva:
        await update.message.reply_text(TR_UI(context, "🚫 Station not found in Deutsche Bahn database."), reply_markup=nav_menu(context))
        return

    now_local = datetime.datetime.now(ZoneInfo("Europe/Berlin"))
    try:
        selected_line = context.user_data.get("line")  # например, "S2"
        events, live_ok = get_departures_window(eva, now_local, max_items=15, selected_line=selected_line)

    except Exception as e:
        await update.message.reply_text(TR_UI(context, f"⚠️ Error while fetching timetable: {str(e)}"), reply_markup=nav_menu(context))
        return

        # ... остаётся как есть до получения events ...

    if selected_line:
        header = TR_UI(context, f"🚉 Departures from {station_name} — {selected_line}")
    else:
        header = TR_UI(context, f"🚉 Departures from {station_name}")

    # ✅ Формируем HTML-строки с зачеркнутым pt (если ct есть и отличается)
    rows_html: List[str] = []
    for ev in events:
        rows_html.append(format_departure_html(ev, context))

    if not rows_html:
        warn = TR_UI(context, "ℹ️ No departures in the next 60 minutes.")
        await update.message.reply_text(warn, reply_markup=nav_menu(context))
        return

    body_html = "<br>".join(rows_html)
    footer_html = ""
    if not live_ok:
        footer_html = "<br><br>" + TR_UI(context, "⚠️ Live updates are temporarily unavailable. Showing planned times only.")

    # Сначала жирный заголовок, потом тело — всё одним HTML
    await safe_send_html(update.message.reply_text, f"<b>{html.escape(header)}</b><br>{body_html}{footer_html}")
    await update.message.reply_text(TR_UI(context, "Choose what to do next:"), reply_markup=nav_menu(context))


# ----- Back / Change line -----
async def on_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(context)
    context.user_data.clear()
    context.user_data["lang"] = lang
    await q.edit_message_text(TR_UI(context, "🚆 Choose an S-Bahn line:"), reply_markup=line_picker_markup())

# ================== WIRING ==================
if __name__ == "__main__":
    print("🚀 Bot starting (polling)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lang",  cmd_lang))

    # Language picker
    app.add_handler(CallbackQueryHandler(on_language,            pattern=r"^LANG:"))

    # Line & actions
    app.add_handler(CallbackQueryHandler(on_line_selected,       pattern=r"^L:"))
    app.add_handler(CallbackQueryHandler(on_show_messages,       pattern=r"^A:MSG$"))
    app.add_handler(CallbackQueryHandler(on_departures_prompt,   pattern=r"^A:DEP$"))
    app.add_handler(CallbackQueryHandler(on_back_main,           pattern=r"^B:MAIN$"))

    # Details
    app.add_handler(CallbackQueryHandler(on_details,             pattern=r"^D:"))

    # Free text for station input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_station_input))

    print("✅ Bot started (polling).")
    app.run_polling()
