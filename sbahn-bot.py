# sbahn_bot.py
import os
import re
import unicodedata
import hashlib
import html
import requests
import datetime
import xml.etree.ElementTree as ET

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
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
CLIENT_ID = os.getenv("DB_CLIENT_ID") or "YOUR_DB_CLIENT_ID"
API_KEY   = os.getenv("DB_API_KEY")  or "YOUR_DB_API_KEY"

MVG_URL = "https://www.mvg.de/api/bgw-pt/v3/messages"

# Short, safe callback keys
CB_LINE_PREFIX   = "L:"      # e.g. "L:S2"
CB_ACT_MSG       = "A:MSG"
CB_ACT_DEP       = "A:DEP"
CB_BACK_MAIN     = "B:MAIN"
CB_DETAIL_PREFIX = "D:"      # e.g. "D:abc123def0"

# ================== HELPERS ==================
def fetch_messages():
    resp = requests.get(MVG_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def is_active(incident_durations):
    if not incident_durations:
        return False
    now_ms = datetime.datetime.now(timezone.utc).timestamp() * 1000
    for d in incident_durations:
        start = d.get("from")
        end   = d.get("to")
        if start and end and start <= now_ms <= end:
            return True
    return False

def filter_line_messages(messages, line_label):
    """Active messages for a given S-Bahn line; keep newest per title."""
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

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _apply_aliases(q: str) -> str:
    qn = _norm(q)
    aliases = {
        "ostbahnhof": "muenchen ost",
        "muenchen ostbahnhof": "muenchen ost",
        "hauptbahnhof": "muenchen hbf",
        "muenchen hauptbahnhof": "muenchen hbf",
        "munich east": "muenchen ost",
        "munich main": "muenchen hbf",
    }
    return aliases.get(qn, q)

def _station_search(query: str):
    """Low-level Station Data API call. Returns list or []."""
    url = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
    headers = {
        "Accept": "application/json",
        "DB-Client-Id": CLIENT_ID,
        "DB-Api-Key": API_KEY,
    }
    r = requests.get(url, headers=headers, params={"searchstring": query}, timeout=12)
    if r.status_code != 200:
        return []
    return r.json().get("result", []) or []

def _pick_best_station(results, query_norm: str):
    """Score candidates and pick best with EVA."""
    best = None
    best_score = -1
    for s in results:
        if not s.get("evaNumbers"):
            continue
        name = s.get("name", "")
        nn = _norm(name)
        score = 0
        if nn == query_norm:                 score += 100
        if nn.startswith(query_norm) or query_norm.startswith(nn): score += 50
        if query_norm in nn:                 score += 25
        if s.get("federalStateCode") == "DE-BY": score += 5
        if score > best_score:
            best = s
            best_score = score
    return best

def get_station_id_and_name(station_query: str):
    """
    Return (eva_id, display_name) or (None, None).
    Steps:
      1) aliases (e.g., Ostbahnhof -> Muenchen Ost), exact/contains.
      2) wildcard "*{query}*"
      3) "München*{query}*" and "Muenchen*{query}*"
    """
    # Step 1: aliases + primary search
    primary = _apply_aliases(station_query)
    qn = _norm(primary)

    # Try as-is
    results = _station_search(primary)
    best = _pick_best_station(results, qn)
    if best:
        eva = best["evaNumbers"][0]["number"]
        return eva, best.get("name") or station_query

    # Step 2: wildcard "*{query}*"
    wildcard = f"*{station_query}*"
    results = _station_search(wildcard)
    best = _pick_best_station(results, _norm(station_query))
    if best:
        eva = best["evaNumbers"][0]["number"]
        return eva, best.get("name") or station_query

    # Step 3: München*{query}* (UTF-8) and Muenchen*{query}* (ASCII)
    wild_muc_utf  = f"München*{station_query}*"
    wild_muc_ascii = f"Muenchen*{station_query}*"

    for variant in (wild_muc_utf, wild_muc_ascii):
        results = _station_search(variant)
        best = _pick_best_station(results, _norm(variant.replace("*"," ")))
        if best:
            eva = best["evaNumbers"][0]["number"]
            return eva, best.get("name") or station_query

    return None, None

def parse_db_time_to_aware_dt(code: str, tz: ZoneInfo):
    """Convert DB 'yymmddHHMM' (local time) to aware datetime (Europe/Berlin)."""
    try:
        yy = int(code[0:2]); mm = int(code[2:4]); dd = int(code[4:6])
        HH = int(code[6:8]);  MM = int(code[8:10])
        return datetime.datetime(2000 + yy, mm, dd, HH, MM, tzinfo=tz)
    except Exception:
        return None

def short_id_for_message(msg):
    """Short stable id for callback data (<=64 bytes total)."""
    basis = f"{msg.get('id','')}-{msg.get('title','')}-{msg.get('publication','')}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]

def nav_menu():
    """Post-content navigation."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📰 Show Messages",   callback_data=CB_ACT_MSG),
            InlineKeyboardButton("🚉 Show Departures", callback_data=CB_ACT_DEP),
        ],
        [InlineKeyboardButton("🆕 Change Line", callback_data=CB_BACK_MAIN)]
    ])

def line_picker_markup():
    rows = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(1,5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"{CB_LINE_PREFIX}S{i}") for i in range(5,9)],
    ]
    return InlineKeyboardMarkup(rows)

# --- safe HTML sender (async) ---
async def safe_send_html(message_func, text_html: str):
    """
    Try to send as HTML; if Telegram rejects (unsupported tags), fall back to plain text.
    """
    try:
        return await message_func(text_html, parse_mode="HTML", disable_web_page_preview=True)
    except BadRequest:
        txt = text_html
        txt = re.sub(r"(?is)<\s*br\b[^>]*>", "\n", txt)   # <br ...> -> NL
        txt = re.sub(r"(?is)</\s*p\s*>", "\n\n", txt)     # </p> -> blank line
        txt = re.sub(r"(?is)<[^>]+>", "", txt)            # strip all tags
        txt = html.unescape(txt)
        return await message_func(txt, disable_web_page_preview=True)

# ================== BOT HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🚆 Choose an S-Bahn line:", reply_markup=line_picker_markup())

async def on_line_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = q.data.replace(CB_LINE_PREFIX, "")
    context.user_data["line"] = line
    await q.edit_message_text(
        f"You selected {line}. Choose an action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📰 Service Messages", callback_data=CB_ACT_MSG)],
            [InlineKeyboardButton("🚉 Departures (by station)", callback_data=CB_ACT_DEP)],
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data=CB_BACK_MAIN)],
        ])
    )

# ----- Messages -----
async def on_show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    line = context.user_data.get("line", "S2")

    try:
        data = fetch_messages()
        msgs = filter_line_messages(data, line)

        context.user_data["msg_map"] = {}

        if not msgs:
            await q.message.reply_text(f"No current messages for {line}.")
            await q.message.reply_text("Choose what to do next:", reply_markup=nav_menu())
            return

        await q.message.reply_text(f"📰 <b>Service Messages for {line}</b>\n", parse_mode="HTML")

        for m in msgs:
            mid = short_id_for_message(m)
            context.user_data["msg_map"][mid] = m

            title = html.escape(m.get("title", "No title"))
            pub   = m.get("publication")
            pub_s = datetime.datetime.fromtimestamp(pub/1000, datetime.UTC).strftime("%d.%m.%Y %H:%M") if pub else "?"

            text = f"<b>{title}</b>\n🕓 {pub_s} UTC"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Details", callback_data=f"{CB_DETAIL_PREFIX}{mid}")]])
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

        await q.message.reply_text("Choose what to do next:", reply_markup=nav_menu())

    except Exception as e:
        await q.message.reply_text(f"⚠️ Error: {html.escape(str(e))}")
        await q.message.reply_text("Choose what to do next:", reply_markup=nav_menu())

async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mid = q.data.replace(CB_DETAIL_PREFIX, "")
    m = (context.user_data.get("msg_map") or {}).get(mid)
    if not m:
        await q.message.reply_text("Message details not found.")
        await q.message.reply_text("Choose what to do next:", reply_markup=nav_menu())
        return

    title = html.escape(m.get("title", "No title"))
    desc  = m.get("description", "") or ""
    pub   = m.get("publication")
    pub_s = datetime.datetime.fromtimestamp(pub/1000, datetime.UTC).strftime("%d.%m.%Y %H:%M") if pub else "?"

    text_html = f"📢 <b>{title}</b>\n🕓 {pub_s} UTC\n\n{desc}"
    await safe_send_html(q.message.reply_text, text_html)
    await q.message.reply_text("Choose what to do next:", reply_markup=nav_menu())

# ----- Departures -----
async def on_departures_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["await_station"] = True
    await q.edit_message_text("Please enter the station name (e.g., *Erding* or *Ostbahnhof*):", parse_mode="Markdown")

async def on_station_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_station"):
        return
    context.user_data["await_station"] = False

    station_in = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching departures for *{station_in}*...", parse_mode="Markdown")

    eva, station_name = get_station_id_and_name(station_in)
    if not eva:
        await update.message.reply_text("🚫 Station not found in Deutsche Bahn database.", reply_markup=nav_menu())
        return

    tz = ZoneInfo("Europe/Berlin")
    now_local = datetime.datetime.now(tz)
    horizon   = now_local + timedelta(hours=1)

    date = now_local.strftime("%y%m%d")
    hour = now_local.strftime("%H")
    headers = {"Accept":"application/xml","DB-Client-Id":CLIENT_ID,"DB-Api-Key":API_KEY}

    def fetch_xml(url):
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200 or not r.text.strip():
            return None
        try:
            return ET.fromstring(r.text)
        except Exception:
            return None

    root = fetch_xml(f"https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan/{eva}/{date}/{hour}")
    if root is None:
        root = fetch_xml(f"https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/fchg/{eva}")

    rows = []
    if root is not None:
        for s in root.findall("s"):
            tl = s.find("tl")
            if tl is None:
                continue
            line_code = tl.attrib.get("c", "")
            if not line_code.startswith("S"):
                continue
            dp = s.find("dp")
            if dp is None:
                continue
            code = dp.attrib.get("pt", "")
            dt   = parse_db_time_to_aware_dt(code, tz)
            if not dt:
                continue
            if not (now_local <= dt <= horizon):
                continue
            path = dp.attrib.get("ppth", "")
            dest = path.split("|")[-1] if path else "Unknown"
            rows.append((line_code, dt, dest))

    if not rows:
        await update.message.reply_text("ℹ️ No departures in the next 60 minutes.", reply_markup=nav_menu())
        return

    rows.sort(key=lambda x: x[1])
    # Формат: S2 → Holzkirchen at 20:50  (без даты)
    out = f"<b>🚉 Departures from {html.escape(station_name)}</b>\n\n"
    for line_code, dt, dest in rows[:12]:
        out += f"• {line_code} → {html.escape(dest)} at {dt.strftime('%H:%M')}\n"

    await safe_send_html(update.message.reply_text, out)
    await update.message.reply_text("Choose what to do next:", reply_markup=nav_menu())

# ----- Back / Change line -----
async def on_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text("🚆 Choose an S-Bahn line:", reply_markup=line_picker_markup())

# ================== WIRING ==================
if __name__ == "__main__":
    print("🚀 Bot starting (polling)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_line_selected,       pattern=r"^L:"))       # L:S2
    app.add_handler(CallbackQueryHandler(on_show_messages,       pattern=r"^A:MSG$"))
    app.add_handler(CallbackQueryHandler(on_departures_prompt,   pattern=r"^A:DEP$"))
    app.add_handler(CallbackQueryHandler(on_back_main,           pattern=r"^B:MAIN$"))
    app.add_handler(CallbackQueryHandler(on_details,             pattern=r"^D:"))

    # Free text for station input
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_station_input))

    print("✅ Bot started (polling).")
    app.run_polling()
