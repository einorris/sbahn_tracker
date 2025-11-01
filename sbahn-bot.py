import os
import requests
import datetime
import html
import xml.etree.ElementTree as ET
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from datetime import timezone

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
CLIENT_ID = os.getenv("DB_CLIENT_ID") or "YOUR_DB_CLIENT_ID"
API_KEY = os.getenv("DB_API_KEY") or "YOUR_DB_API_KEY"

MVG_URL = "https://www.mvg.de/api/bgw-pt/v3/messages"

# === HELPERS ===

def fetch_messages():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(MVG_URL, headers=headers)
    r.raise_for_status()
    return r.json()

def is_active(incident_durations):
    if not incident_durations:
        return False
    now = datetime.datetime.now(timezone.utc).timestamp() * 1000
    for d in incident_durations:
        start = d.get("from")
        end = d.get("to")
        if start and end and start <= now <= end:
            return True
    return False

def filter_line_messages(messages, line):
    result = []
    seen_titles = set()

    for msg in messages:
        for l in msg.get("lines", []):
            if l.get("transportType") == "SBAHN" and l.get("label") == line:
                if is_active(msg.get("incidentDurations", [])):
                    title = msg.get("title", "")
                    # keep only freshest duplicate
                    if title not in seen_titles:
                        seen_titles.add(title)
                        result.append(msg)
    result.sort(key=lambda m: m.get("publication", 0), reverse=True)
    return result

def get_station_id(station_name):
    url = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
    params = {"searchstring": station_name}
    headers = {
        "Accept": "application/json",
        "DB-Client-Id": CLIENT_ID,
        "DB-Api-Key": API_KEY,
    }
    r = requests.get(url, headers=headers, params=params)
    if r.status_code != 200:
        return None
    data = r.json().get("result", [])
    for s in data:
        if "evaNumbers" in s and s["evaNumbers"]:
            return s["evaNumbers"][0]["number"]
    return None

def parse_db_time(code):
    try:
        year = int(code[0:2]) + 2000
        month = int(code[2:4])
        day = int(code[4:6])
        hour = int(code[6:8])
        minute = int(code[8:10])
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
    except Exception:
        return code

# === BOT LOGIC ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"line_S{i}") for i in range(1, 5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"line_S{i}") for i in range(5, 9)],
    ]
    await update.message.reply_text("🚆 Choose an S-Bahn line:", reply_markup=InlineKeyboardMarkup(keyboard))

async def line_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    line = query.data.replace("line_", "")
    context.user_data["line"] = line

    keyboard = [
        [InlineKeyboardButton("📰 Service Messages", callback_data="service_messages")],
        [InlineKeyboardButton("🚉 Departures (by station)", callback_data="departures")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ]
    await query.edit_message_text(f"You selected {line}. What would you like to see?",
                                  reply_markup=InlineKeyboardMarkup(keyboard))

async def show_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    line = context.user_data.get("line", "S2")

    try:
        data = fetch_messages()
        messages = filter_line_messages(data, line)
        if not messages:
            await query.edit_message_text(f"No current messages for {line}.",
                                          reply_markup=InlineKeyboardMarkup(
                                              [[InlineKeyboardButton("⬅️ Back", callback_data="back_line")]]
                                          ))
            return

        for msg in messages:
            title = html.escape(msg.get("title", "No title"))
            desc = msg.get("description", "")
            pub = msg.get("publication")
            pub_str = datetime.datetime.fromtimestamp(pub / 1000, datetime.UTC).strftime("%d.%m.%Y %H:%M") if pub else "?"
            preview = f"<b>{title}</b>\n🕓 Published: {pub_str}"
            keyboard = [[InlineKeyboardButton("📄 Details", callback_data=f"details|{msg.get('id')}")]]
            await query.message.reply_text(preview, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        await query.message.reply_text(f"Error while loading messages: {e}")

async def show_departures_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Enter the station name (e.g., *Erding*):",
        parse_mode="Markdown"
    )
    context.user_data["expecting_station"] = True

async def handle_station_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles user input for station name and shows departures."""
    if not context.user_data.get("expecting_station"):
        return

    context.user_data["expecting_station"] = False
    station_name = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching departures for *{station_name}*...", parse_mode="Markdown")

    try:
        # === 1. Find EVA-ID ===
        station_id = get_station_id(station_name)
        if not station_id:
            await update.message.reply_text("🚫 Station not found in Deutsche Bahn database.")
            return

        now = datetime.datetime.now()
        hour = now.strftime("%H")
        date = now.strftime("%y%m%d")

        url = f"https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan/{station_id}/{date}/{hour}"
        headers = {
            "Accept": "application/xml",
            "DB-Client-Id": CLIENT_ID,
            "DB-Api-Key": API_KEY,
        }

        # === 2. Request Timetable ===
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            await update.message.reply_text(f"❌ Error fetching timetable (status {r.status_code}).\n\n{r.text[:300]}")
            return

        # === 3. Check for empty response ===
        if not r.text.strip():
            await update.message.reply_text("⚠️ Empty response from DB API.")
            return

        # === 4. Parse XML ===
        try:
            root = ET.fromstring(r.text)
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to parse XML:\n\n{html.escape(str(e))}")
            return

        rows = []
        for s in root.findall("s"):
            tl = s.find("tl")
            if tl is None:
                continue

            line_code = tl.attrib.get("c", "")
            if not line_code.startswith("S"):  # only S-Bahn
                continue

            node = s.find("dp")
            if node is None:
                continue

            time_fmt = parse_db_time(node.attrib.get("pt", ""))
            path = node.attrib.get("ppth", "")
            destination = path.split("|")[-1] if path else "Unknown"

            rows.append((line_code, time_fmt, destination))

        # === 5. Output ===
        if not rows:
            await update.message.reply_text("ℹ️ No departures found for this station at the current hour.")
            return

        rows.sort(key=lambda x: x[1])
        text = f"<b>🚉 Departures for {station_name}</b>\n\n"
        for line, time, dest in rows[:10]:
            text += f"• {line} → {dest} at {time}\n"

        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        await update.message.reply_text(f"💥 Unexpected error: {html.escape(str(e))}")


async def handle_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg_id = query.data.split("|", 1)[1]
    data = fetch_messages()
    msg = next((m for m in data if str(m.get("id")) == msg_id), None)
    if not msg:
        await query.message.reply_text("Message not found.")
        return

    desc = msg.get("description", "")
    title = msg.get("title", "No title")
    await query.message.reply_text(f"<b>{title}</b>\n\n{desc}", parse_mode="HTML")

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_main":
        await start(update, context)
    elif data == "back_line":
        await line_selected(update, context)

# === MAIN ===

if __name__ == "__main__":
    import psutil
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        if 'python' in p.info['name'] and 'sbahn_bot.py' in " ".join(p.info['cmdline']):
            if p.pid != os.getpid():
                print("⚠️ Bot already running, exiting.")
                exit(0)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(line_selected, pattern="^line_"))
    app.add_handler(CallbackQueryHandler(show_service_messages, pattern="^service_messages"))
    app.add_handler(CallbackQueryHandler(show_departures_prompt, pattern="^departures"))
    app.add_handler(CallbackQueryHandler(handle_details, pattern="^details"))
    app.add_handler(CallbackQueryHandler(go_back, pattern="^back_"))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("check", start))
    app.add_handler(CommandHandler("back", start))
    app.add_handler(CommandHandler("messages", start))
    app.add_handler(CommandHandler("departures", start))
    app.add_handler(CommandHandler("restart", start))
    app.add_handler(CommandHandler("stop", start))
    app.add_handler(CommandHandler("status", start))
    app.add_handler(CommandHandler("ping", start))
    app.add_handler(CommandHandler("version", start))
    app.add_handler(CommandHandler("uptime", start))
    app.add_handler(CommandHandler("debug", start))
    app.add_handler(CommandHandler("info", start))
    app.add_handler(CommandHandler("reset", start))
    app.add_handler(CommandHandler("reload", start))
    app.add_handler(CommandHandler("clear", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", start))
    app.add_handler(CommandHandler("restart", start))
    app.add_handler(CommandHandler("status", start))
    app.add_handler(CommandHandler("check", start))
    app.add_handler(CommandHandler("messages", start))
    app.add_handler(CommandHandler("departures", start))
    app.add_handler(CommandHandler("back", start))
    app.add_handler(CommandHandler("ping", start))
    app.add_handler(CommandHandler("info", start))
    app.add_handler(CommandHandler("debug", start))
    app.add_handler(CommandHandler("version", start))
    app.add_handler(CommandHandler("uptime", start))
    app.add_handler(CommandHandler("clear", start))
    app.add_handler(CommandHandler("reset", start))
    app.add_handler(CommandHandler("reload", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("restart", start))
    app.add_handler(CommandHandler("stop", start))
    app.add_handler(CommandHandler("status", start))
    app.add_handler(CommandHandler("check", start))
    app.add_handler(CommandHandler("messages", start))
    app.add_handler(CommandHandler("departures", start))
    app.add_handler(CommandHandler("back", start))
    app.add_handler(CommandHandler("ping", start))
    app.add_handler(CommandHandler("info", start))
    app.add_handler(CommandHandler("debug", start))
    app.add_handler(CommandHandler("version", start))
    app.add_handler(CommandHandler("uptime", start))
    app.add_handler(CommandHandler("clear", start))
    app.add_handler(CommandHandler("reset", start))
    app.add_handler(CommandHandler("reload", start))

    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", start))
    app.add_handler(CommandHandler("restart", start))
    app.add_handler(CommandHandler("status", start))
    app.add_handler(CommandHandler("check", start))
    app.add_handler(CommandHandler("messages", start))
    app.add_handler(CommandHandler("departures", start))
    app.add_handler(CommandHandler("back", start))
    app.add_handler(CommandHandler("ping", start))
    app.add_handler(CommandHandler("info", start))
    app.add_handler(CommandHandler("debug", start))
    app.add_handler(CommandHandler("version", start))
    app.add_handler(CommandHandler("uptime", start))
    app.add_handler(CommandHandler("clear", start))
    app.add_handler(CommandHandler("reset", start))
    app.add_handler(CommandHandler("reload", start))
    app.add_handler(CommandHandler("help", start))

    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", start))
    app.add_handler(CommandHandler("messages", start))
    app.add_handler(CommandHandler("departures", start))
    app.add_handler(CommandHandler("back", start))

    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", start))

    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", start))

    print("🚀 Bot started (polling mode)")
    app.run_polling()
