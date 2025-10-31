import os
import requests
import datetime
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# 🔑 Настройки
TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен из переменных окружения
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"


# --- Вспомогательные функции ---
def fetch_messages():
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(URL, headers=headers)
    resp.raise_for_status()
    return resp.json()


def is_active(incident_durations):
    """Проверяет, активно ли сообщение в данный момент"""
    if not incident_durations:
        return False
    now = datetime.datetime.utcnow().timestamp() * 1000
    for d in incident_durations:
        start = d.get("from")
        end = d.get("to")
        if start and end and start <= now <= end:
            return True
    return False


def filter_s2_messages(messages):
    """Фильтрует только активные сообщения по линии S2"""
    result = []
    for msg in messages:
        for line in msg.get("lines", []):
            if line.get("transportType") == "SBAHN" and line.get("label") == "S2":
                if is_active(msg.get("incidentDurations", [])):
                    result.append(msg)
    result.sort(key=lambda m: m.get("publication", 0), reverse=True)
    return result


def html_to_plain_text(html_content):
    """Преобразует HTML в чистый текст"""
    if not html_content:
        return ""
    text = BeautifulSoup(html_content, "html.parser").get_text(separator="\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text


# --- Команда /check ---
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = fetch_messages()
        s2_msgs = filter_s2_messages(data)

        if not s2_msgs:
            await update.message.reply_text("✅ Keine aktuellen Störungen auf der S2.")
            return

        # сохраняем сообщения в контексте
        context.user_data["s2_msgs"] = s2_msgs

        for i, msg in enumerate(s2_msgs):
            title = msg.get("title", "Без заголовка")
            mtype = msg.get("type", "")
            pub = msg.get("publication")
            pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"

            keyboard = [[InlineKeyboardButton("📄 Подробнее", callback_data=f"details_{i}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            preview_text = f"🚆 {title}\n{mtype}\n🕓 {pub_str} UTC\n(Нажмите «Подробнее» для полного текста.)"

            await update.message.reply_text(preview_text, reply_markup=reply_markup)

    except Exception as e:
        await update.message.reply_text(f"❗Ошибка при загрузке данных: {e}")


# --- Обработчик кнопки "Подробнее" ---
async def show_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    msg_index = int(query.data.split("_")[1])
    s2_msgs = context.user_data.get("s2_msgs", [])

    if msg_index >= len(s2_msgs):
        await query.edit_message_text("Сообщение не найдено.")
        return

    msg = s2_msgs[msg_index]
    title = msg.get("title", "Без заголовка")
    raw_desc = msg.get("description", "")
    desc_text = html_to_plain_text(raw_desc)

    durations = msg.get("incidentDurations", [])
    time_str = ""
    if durations:
        d = durations[0]
        start = datetime.datetime.utcfromtimestamp(d.get("from") / 1000).strftime("%d.%m.%Y %H:%M")
        end = datetime.datetime.utcfromtimestamp(d.get("to") / 1000).strftime("%d.%m.%Y %H:%M")
        time_str = f"{start} – {end} UTC"

    full_text = f"{title}\n\n{desc_text}\n\n🕓 Gültig: {time_str}"

    MAX_LEN = 3900  # лимит Telegram
    if len(full_text) > MAX_LEN:
        full_text = full_text[:MAX_LEN] + "\n\n...[truncated]"

    await query.edit_message_text(full_text, disable_web_page_preview=True)


# --- Основной запуск ---
if __name__ == "__main__":
    print("🤖 Bot started...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CallbackQueryHandler(show_details))
    app.run_polling()
