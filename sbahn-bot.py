import os
import re
import requests
import datetime
import html
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio

# ============================================================
# 🔧 Настройки
# ============================================================
TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен Telegram из Railway secrets
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"
CHAT_ID = os.getenv("CHAT_ID")  # твой Telegram ID
DEFAULT_LINE = os.getenv("DEFAULT_LINE", "S2")  # линия по умолчанию


# ============================================================
# 🧩 Функции
# ============================================================
def fetch_messages():
    """Получает JSON со всеми сообщениями MVG."""
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(URL, headers=headers)
    resp.raise_for_status()
    return resp.json()

def clean_unsupported_html(text):
    allowed = ['b', 'i', 'u', 's', 'a', 'code', 'pre']
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all():
        if tag.name not in allowed:
            tag.unwrap()
    return str(soup)

def is_active(incident_durations):
    """Проверяет, актуально ли сообщение сейчас."""
    if not incident_durations:
        return False
    now = datetime.datetime.utcnow().timestamp() * 1000
    for d in incident_durations:
        start = d.get("from")
        end = d.get("to")
        if start and end and start <= now <= end:
            return True
    return False

def filter_sbahn_messages(messages, line_label="S2"):
    """Фильтрует активные сообщения по линии S-Bahn (S1–S8)."""
    seen = {}
    for msg in messages:
        for line in msg.get("lines", []):
            if line.get("transportType") == "SBAHN" and line.get("label") == line_label:
                if is_active(msg.get("incidentDurations", [])):
                    title = msg.get("title", "").strip()
                    pub = msg.get("publication", 0)
                    if title in seen:
                        if pub > seen[title].get("publication", 0):
                            seen[title] = msg
                    else:
                        seen[title] = msg
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=True)

def format_message(messages, line_label="S2"):
    """Создает HTML-формат сообщений."""
    if not messages:
        return f"✅ <b>Keine aktuellen Meldungen für {line_label}.</b>"

    result = [f"<b>🚆 Aktuelle Betriebsmeldungen {line_label}:</b>\n"]
    for msg in messages:
        title = html.escape(msg.get("title", ""))
        desc = msg.get("description", "")
        desc=clean_unsupported_html(desc)
        pub = msg.get("publication", 0)
        pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"

        result.append(
            f"🟢 <b>{title}</b>\n"
            f"<i>({pub_str} UTC)</i>\n\n"
            f"{desc}\n\n"
            "───────────────\n"
        )
    return "\n".join(result)

# ============================================================
# 🤖 Telegram handlers
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"S{i}") for i in range(1, 5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"S{i}") for i in range(5, 9)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Wähle eine S-Bahn Linie für aktuelle Meldungen:",
        reply_markup=reply_markup
    )

async def handle_line_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    line_label = query.data
    try:
        data = fetch_messages()
        filtered = filter_sbahn_messages(data, line_label)
        message = format_message(filtered, line_label)
        await query.edit_message_text(
            text=message, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Fehler: {e}")

# ============================================================
# 🕒 Автоматическая отправка уведомлений
# ============================================================
async def send_daily_update(app):
    try:
        data = fetch_messages()
        filtered = filter_sbahn_messages(data, DEFAULT_LINE)
        message = format_message(filtered, DEFAULT_LINE)
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        print(f"✅ Daily update sent for {DEFAULT_LINE}")
    except Exception as e:
        print(f"❌ Error in daily update: {e}")

def start_scheduler(app):
    scheduler = BackgroundScheduler(timezone="Europe/Berlin")
    scheduler.add_job(lambda: asyncio.run(send_daily_update(app)), "cron", hour=7, minute=30)
    scheduler.start()
    print("🕒 Scheduler started (07:30 daily)")

# ============================================================
# 🚀 Запуск бота
# ============================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_line_selection))

    # Запуск планировщика
    start_scheduler(app)

    print("✅ Bot is running...")
    app.run_polling()
