import os
import re
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
TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен Telegram из Railway secrets
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

def clean_unsupported_html(text):
    # Удаляем теги <p> и </p>, а также другие неподдерживаемые теги.
    text = re.sub(r"</?p>", "", text)
    # Можно добавить ещё удаление других тегов при необходимости
    return text


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

def format_message_titles(messages, line_label="S2"):
    if not messages:
        return f"✅ <b>Keine aktuellen Meldungen für {line_label}.</b>", None

    result = [f"<b>🚆 Aktuelle Betriebsmeldungen {line_label}:</b>\n"]
    buttons = []
    for i, msg in enumerate(messages):
        title = html.escape(msg.get("title", ""))
        pub = msg.get("publication", 0)
        pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"
        result.append(f"🟢 <b>{title}</b>\n<i>({pub_str} UTC)</i>\n")
        buttons.append([InlineKeyboardButton("Details", callback_data=f"details_{i}")])
    return "\n".join(result), InlineKeyboardMarkup(buttons)

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

# Модификация обработчика выбора линии:
async def handle_line_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    line_label = query.data
    context.user_data["line_label"] = line_label
    try:
        data = fetch_messages()
        filtered = filter_sbahn_messages(data, line_label)
        message, keyboard = format_message_titles(filtered, line_label)
        await query.edit_message_text(
            text=message, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Fehler: {e}")

# --- Обработчик кнопки “Подробнее” ---
async def handle_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Получаем индекс сообщения из callback_data, например details_0
    idx = int(query.data.split("_")[1])
    data = fetch_messages()
    filtered = filter_sbahn_messages(data, context.user_data.get("line_label", "S2"))
    msg = filtered[idx]

    title = html.escape(msg.get("title", ""))
    desc = clean_unsupported_html(msg.get("description", ""))

    pub = msg.get("publication", 0)
    pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"

    text = (
        f"🟢 <b>{title}</b>\n"
        f"<i>({pub_str} UTC)</i>\n\n"
        f"{desc}\n\n"
        "───────────────"
    )

    # Добавляем кнопку "Back"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data="back")]]
    )

    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)

async def handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    line_label = context.user_data.get("line_label", "S2")
    data = fetch_messages()
    filtered = filter_sbahn_messages(data, line_label)
    message, keyboard = format_message_titles(filtered, line_label)
    await query.edit_message_text(text=message, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


# ============================================================
# 🚀 Запуск бота
# ============================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_line_selection))
    app.add_handler(CallbackQueryHandler(handle_details_callback, pattern=r"^details_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_back_callback, pattern="back"))
    print("✅ Bot is running...")
    app.run_polling()
