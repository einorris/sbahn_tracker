import requests
import datetime
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- Настройки ---
TOKEN = "ТОКЕН_ОТ_BOTFATHER"
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"


# --- Функции логики ---
def fetch_messages():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(URL, headers=headers)
    response.raise_for_status()
    return response.json()

def is_active(incident_durations):
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
    result = []
    for msg in messages:
        for line in msg.get("lines", []):
            if line.get("transportType") == "SBAHN" and line.get("label") == "S2":
                if is_active(msg.get("incidentDurations", [])):
                    result.append(msg)
    result.sort(key=lambda m: m.get("publication", 0), reverse=True)
    return result


# --- Команда /check ---
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = fetch_messages()
        s2_msgs = filter_s2_messages(data)

        if not s2_msgs:
            await update.message.reply_text("✅ Keine aktuellen Störungen auf der S2.")
            return

        for i, msg in enumerate(s2_msgs):
            title = html.escape(msg.get("title", "Без заголовка"))
            mtype = html.escape(msg.get("type", ""))
            pub = msg.get("publication")
            pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"
            
            keyboard = [
                [InlineKeyboardButton("📄 Подробнее", callback_data=f"details_{i}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"🚆 *{title}*\n_{mtype}_\n🕓 {pub_str} UTC",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

        # сохраняем данные в контекст для обратного вызова
        context.user_data["s2_msgs"] = s2_msgs

    except Exception as e:
        await update.message.reply_text(f"❗Ошибка при загрузке данных: {e}")


# --- Обработчик кнопки “Подробнее” ---
async def show_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    msg_index = int(query.data.split("_")[1])
    s2_msgs = context.user_data.get("s2_msgs", [])

    if msg_index >= len(s2_msgs):
        await query.edit_message_text("Сообщение не найдено.")
        return

    msg = s2_msgs[msg_index]
    title = html.escape(msg.get("title", "Без заголовка"))
    desc = msg.get("description", "")
    durations = msg.get("incidentDurations", [])

    time_str = ""
    if durations:
        d = durations[0]
        start = datetime.datetime.utcfromtimestamp(d.get("from") / 1000).strftime("%d.%m.%Y %H:%M")
        end = datetime.datetime.utcfromtimestamp(d.get("to") / 1000).strftime("%d.%m.%Y %H:%M")
        time_str = f"{start} – {end} UTC"

    text = f"*{title}*\n\n{desc}\n\n🕓 *Gültig:* {time_str}"

    await query.edit_message_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# --- Запуск приложения ---
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("check", check))
app.add_handler(CallbackQueryHandler(show_details))

print("🤖 Bot started...")
app.run_polling()
