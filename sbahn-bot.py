import os
import requests
import datetime
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ========== Настройки ==========
TOKEN = os.getenv("TELEGRAM_TOKEN")
URL = "https://www.mvg.de/api/bgw-pt/v3/messages"

# ========== Вспомогательные функции ==========
def fetch_messages():
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(URL, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

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

def normalize_label(label):
    """Нормализует label: убирает пробелы, делает верхний регистр, например 'S 2' -> 'S2'"""
    if not label:
        return ""
    return "".join(label.split()).upper()

def normalize_transport_type(tt):
    if not tt:
        return ""
    return tt.strip().upper()

def filter_sbahn_messages(messages, line_label="S2"):
    """
    Фильтрует активные сообщения по линии (например 'S2').
    Убирает дубликаты по title, оставляет самый свежий (по publication).
    """
    want = line_label.replace(" ", "").upper()  # e.g. "S2"
    seen = {}
    for msg in messages:
        for line in msg.get("lines", []):
            ttype = normalize_transport_type(line.get("transportType"))
            label = normalize_label(line.get("label"))
            # считаем запись относящейся к нужной линии, если transportType указывает на S-Bahn
            if (ttype in ("SBAHN", "S") ) and label == want:
                if is_active(msg.get("incidentDurations", [])):
                    title = (msg.get("title") or "").strip()
                    pub = msg.get("publication", 0) or 0
                    # keep most recent by title
                    if title in seen:
                        if pub > (seen[title].get("publication") or 0):
                            seen[title] = msg
                    else:
                        seen[title] = msg
    # sort by publication desc
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=True)

def html_to_plain_text(html_content):
    """Безопасно извлекает текст из HTML (удаляет теги)."""
    if not html_content:
        return ""
    text = BeautifulSoup(html_content, "html.parser").get_text(separator="\n")
    # нормализуем пробелы/пустые строки
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text

# ========== Форматирование/вывод ==========
def build_preview_text(msg):
    title = msg.get("title", "Без заголовка")
    pub = msg.get("publication")
    pub_str = datetime.datetime.utcfromtimestamp(pub / 1000).strftime("%d.%m.%Y %H:%M") if pub else "?"
    return f"🚆 {title}\n🕓 {pub_str} UTC"

def build_detail_text(msg):
    title = msg.get("title", "Без заголовка")
    raw_desc = msg.get("description", "")
    desc = html_to_plain_text(raw_desc)
    durations = msg.get("incidentDurations", [])
    time_str = ""
    if durations:
        d = durations[0]
        start = datetime.datetime.utcfromtimestamp(d.get("from") / 1000).strftime("%d.%m.%Y %H:%M")
        end = datetime.datetime.utcfromtimestamp(d.get("to") / 1000).strftime("%d.%m.%Y %H:%M")
        time_str = f"{start} – {end} UTC"
    full = f"{title}\n\n{desc}\n\n🕓 Valid: {time_str}"
    # Ограничиваем длину (Telegram limit)
    if len(full) > 3900:
        full = full[:3900] + "\n\n...[truncated]"
    return full

# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(1,5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(5,9)],
    ]
    await update.message.reply_text("Выберите линию S-Bahn:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_line_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data = "LINE|S2" (we use this scheme)
    try:
        _, line_label = query.data.split("|", 1)
    except Exception:
        await query.edit_message_text("Неправильный callback.")
        return

    try:
        data = fetch_messages()
        msgs = filter_sbahn_messages(data, line_label)
    except Exception as e:
        await query.edit_message_text(f"Ошибка при получении данных: {e}")
        return

    # сохраняем под ключом конкретной линии, чтобы callback 'details' мог достать
    key = f"msgs_{line_label}"
    context.user_data[key] = msgs

    if not msgs:
        await query.edit_message_text(f"✅ Нет актуальных сообщений для {line_label}.")
        return

    # отправляем превью для каждого сообщения и кнопку "Details"
    # используем callback_data: DETAILS|S2|index
    for i, m in enumerate(msgs):
        preview = build_preview_text(m)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📄 Details", callback_data=f"DETAILS|{line_label}|{i}")]])
        # отправляем новым сообщением (не edit), чтобы пользователю было удобнее
        await query.message.reply_text(preview, reply_markup=kb)
    # удаляем исходное сообщение с кнопками (чтобы не засорять), можно оставить
    try:
        await query.delete_message()
    except Exception:
        pass

async def handle_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data = "DETAILS|S2|0"
    try:
        _, line_label, idx_str = query.data.split("|", 2)
        idx = int(idx_str)
    except Exception:
        await query.edit_message_text("Неправильный callback.")
        return

    key = f"msgs_{line_label}"
    msgs = context.user_data.get(key, [])
    if not msgs or idx < 0 or idx >= len(msgs):
        await query.edit_message_text("Сообщение не найдено или устарело.")
        return

    detail_text = build_detail_text(msgs[idx])
    # редактируем текущее сообщение (можно и отправить новым)
    try:
        await query.edit_message_text(detail_text)
    except Exception:
        # fallback: отправим как новое сообщение
        await query.message.reply_text(detail_text)

# ========== Запуск ==========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_line_selection, pattern=r"^LINE\|"))
    app.add_handler(CallbackQueryHandler(handle_details, pattern=r"^DETAILS\|"))
    print("Bot started")
    app.run_polling()
