import os
import requests
import datetime
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ============================================================
# 🔧 Настройки
# ============================================================
TOKEN = os.getenv("BOT_TOKEN")  # токен Telegram из Railway secrets
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"

# ============================================================
# 🧩 Функции
# ============================================================
def fetch_messages():
    """Получает JSON со всеми сообщениями MVG."""
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(URL, headers=headers)
    resp.raise_for_status()
    return resp.json()

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
    """Показывает кнопки выбора линии."""
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
    """Обработка выбора линии пользователем."""
    query = update.callback_query
    await query.answer()

    line_label = query.data  # например "S2"
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
# 🚀 Запуск бота
# ============================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_line_selection))
    print("✅ Bot is running...")
    app.run_polling()
