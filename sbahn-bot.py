import os
import requests
import datetime
import html
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ============================================================
# 🔧 Настройки
# ============================================================
TOKEN = os.getenv("TELEGRAM_TOKEN")  # токен Telegram из Railway secrets
CHAT_ID = os.getenv("CHAT_ID", None)
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"

# ============================================================
# 🧩 Вспомогательные функции
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
    now = datetime.datetime.utcnow().timestamp() * 1000  # миллисекунды
    for d in incident_durations:
        start = d.get("from")
        end = d.get("to")
        if start and end and start <= now <= end:
            return True
    return False

def filter_s2_messages(messages):
    """Фильтрует активные сообщения S2, убирает дубликаты и сортирует по времени."""
    seen = {}
    for msg in messages:
        for line in msg.get("lines", []):
            if line.get("transportType") == "SBAHN" and line.get("label") == "S2":
                if is_active(msg.get("incidentDurations", [])):
                    title = msg.get("title", "").strip()
                    pub = msg.get("publication", 0)
                    # Сохраняем только самое свежее сообщение с данным заголовком
                    if title in seen:
                        if pub > seen[title].get("publication", 0):
                            seen[title] = msg
                    else:
                        seen[title] = msg
    # сортируем по времени публикации
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=True)

def format_message(messages):
    """Создает HTML-формат для Telegram."""
    if not messages:
        return "✅ <b>Keine aktuellen Meldungen für S2.</b>"

    result = ["<b>🚆 Aktuelle Betriebsmeldungen S2:</b>\n"]
    for msg in messages:
        title = html.escape(msg.get("title", ""))
        desc = msg.get("description", "")
        pub = msg.get("publication", 0)
        pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"

        # ограничиваем длину preview, добавляем кнопку (или подсказку)
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
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /check — показывает текущие сообщения."""
    try:
        data = fetch_messages()
        s2_msgs = filter_s2_messages(data)
        message = format_message(s2_msgs)
        await update.message.reply_text(message, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при загрузке данных: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Напиши /check, чтобы увидеть актуальные сообщения S2.")

# ============================================================
# 🚀 Запуск бота
# ============================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    print("✅ Bot is running...")
    app.run_polling()
