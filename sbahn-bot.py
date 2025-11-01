import os
import requests
import datetime
import html
import xml.etree.ElementTree as ET
from datetime import timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
    """Filter active messages for given S-Bahn line, keeping newest per title"""
    result = []
    seen_titles = set()
    for msg in messages:
        for l in msg.get("lines", []):
            if l.get("transportType") == "SBAHN" and l.get("label") == line:
                if is_active(msg.get("incidentDurations", [])):
                    title = msg.get("title", "")
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


def navigation_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📰 Show Messages", callback_data="service_messages"),
            InlineKeyboardButton("🚉 Show Departures", callback_data="departures"),
        ],
        [InlineKeyboardButton("🆕 Change Line", callback_data="back_main")],
    ])


# === BOT LOGIC ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"line_S{i}") for i in range(1, 5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"line_S{i}") for i in range(5, 9)],
    ]
    await update.message.reply_text(
        "🚆 Choose an S-Bahn line:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def line_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    line = query.data.replace("line_", "")
    context.user_data["line"] = line

    keyboard = [
        [InlineKeyboardButton("📰 Service Messages", callback_data="service_messages")],
        [InlineKeyboardButton("🚉 Departures (by station)", callback_data="departures")],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")],
    ]
    await query.edit_message_text(
        f"You selected {line}. What would you like to see?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    line = context.user_data.get("line", "S2")

    try:
        data = fetch_messages()
        messages = filter_line_messages(data, line)

        if not messages:
            await query.edit_message_text(
                f"No current messages for {line}.",
                reply_markup=navigation_buttons(),
            )
            return

        for msg in messages:
            msg_id = str(msg.get("id", msg.get("title", "")))[:50]
            title = html.escape(msg.get("title", "No title"))
            pub = msg.get("publication")
            pub_str = (
                datetime.datetime.fromtimestamp(pub / 1000, datetime.UTC).strftime("%d.%m.%Y %H:%M")
                if pub else "?"
            )

            text = f"📰 <b>{title}</b>\n🕓 {pub_str}\n"
            keyboard = [[InlineKeyboardButton("🔍 Details", callback_data=f"details_{msg_id}")]]
            await query.message.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        # Menu after all messages
        await query.message.reply_text("Choose what to do next:", reply_markup=navigation_buttons())

        # Save message dictionary for details lookup
        context.user_data["messages"] = {str(msg.get("id", msg.get("title", "")))[:50]: msg for msg in messages}

    except Exception as e:
        await query.message.reply_text(f"⚠️ Error: {e}", reply_markup=navigation_buttons())


async def show_message_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg_id = query.data.replace("details_", "")
    messages = context.user_data.get("messages", {})

    if msg_id not in messages:
        await query.message.reply_text("Message details not found.", reply_markup=navigation_buttons())
        return

    msg = messages[msg_id]
    title = html.escape(msg.get("title", "No title"))
    desc = msg.get("description", "")
    pub = msg.get("publication")
    pub_str = (
        datetime.datetime.fromtimestamp(pub / 1000, datetime.UTC).strftime("%d.%m.%Y %H:%M")
        if pub else "?"
    )

    text = f"📢 <b>{title}</b>\n🕓 {pub_str}\n\n{desc}"
    await query.message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await query.message.reply_text("Choose what to do next:", reply_markup=navigation_buttons())


async def show_departures_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Please enter the station name (e.g., *Erding*):", parse_mode="Markdown")
    context.user_data["expecting_station"] = True


async def handle_station_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("expecting_station"):
        return

    context.user_data["expecting_station"] = False
    station_name = update.message.text.strip()
    await update.message.reply_text(f"🔍 Searching departures for *{station_name}*...", parse_mode="Markdown")

    try:
        station_id = get_station_id(station_name)
        if not station_id:
            await update.message.reply_text("🚫 Station not found.", reply_markup=navigation_buttons())
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

        r = requests.get(url, headers=headers)
        if r.status_code != 200 or not r.text.strip():
            url = f"https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/fchg/{station_id}"
            r = requests.get(url, headers=headers)

        if r.status_code != 200:
            await update.message.reply_text(f"❌ Error fetching timetable ({r.status_code})",
                                            reply_markup=navigation_buttons())
            return

        root = ET.fromstring(r.text)
        rows = []
        for s in root.findall("s"):
            tl = s.find("tl")
            if tl is None:
                continue
            line_code = tl.attrib.get("c", "")
            if not line_code.startswith("S"):
                continue
            node = s.find("dp")
            if node is None:
                continue
            time_fmt = parse_db_time(node.attrib.get("pt", ""))
            path = node.attrib.get("ppth", "")
            destination = path.split("|")[-1] if path else "Unknown"
            rows.append((line_code, time_fmt, destination))

        if not rows:
            await update.message.reply_text("ℹ️ No departures right now.", reply_markup=navigation_buttons())
            return

        rows.sort(key=lambda x: x[1])
        text = f"<b>🚉 Departures from {station_name}</b>\n\n"
        for line, time, dest in rows[:10]:
            text += f"• {line} → {dest} at {time}\n"

        await update.message.reply_text(text, parse_mode="HTML")
        await update.message.reply_text("Choose what to do next:", reply_markup=navigation_buttons())

    except Exception as e:
        await update.message.reply_text(f"💥 Error: {html.escape(str(e))}", reply_markup=navigation_buttons())


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start(update, context)


# === MAIN ===
if __name__ == "__main__":
    print("🚀 Bot starting in polling mode...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(line_selected, pattern="^line_"))
    app.add_handler(CallbackQueryHandler(show_service_messages, pattern="^service_messages"))
    app.add_handler(CallbackQueryHandler(show_message_details, pattern="^details_"))
    app.add_handler(CallbackQueryHandler(show_departures_prompt, pattern="^departures"))
    app.add_handler(CallbackQueryHandler(go_back, pattern="^back_main"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_station_input))

    print("✅ Bot started successfully (polling).")
    app.run_polling()
