import os
import requests
import datetime
import html
import xml.etree.ElementTree as ET
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ========== Configuration ==========
TOKEN = os.getenv("BOT_TOKEN")
MVG_URL = "https://www.mvg.de/api/bgw-pt/v3/messages"
DB_CLIENT_ID = os.getenv("DB_CLIENT_ID")
DB_API_KEY = os.getenv("DB_API_KEY")

# ========== Utility Functions ==========
def fetch_mvg():
    r = requests.get(MVG_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    return r.json()

def is_active(inc):
    now = datetime.datetime.now(datetime.UTC).timestamp() * 1000
    for d in inc or []:
        if d.get("from") and d.get("to") and d["from"] <= now <= d["to"]:
            return True
    return False

def normalize_label(l): 
    return "".join((l or "").split()).upper()

def filter_mvg(messages, label="S2"):
    label = normalize_label(label)
    seen = {}
    for m in messages:
        for line in m.get("lines", []):
            if normalize_label(line.get("label")) == label and line.get("transportType") in ("SBAHN", "S"):
                if is_active(m.get("incidentDurations", [])):
                    title = m.get("title", "").strip()
                    pub = m.get("publication", 0)
                    if title in seen and pub < seen[title].get("publication", 0):
                        continue
                    seen[title] = m
    return sorted(seen.values(), key=lambda m: m.get("publication", 0), reverse=True)

def format_mvg(messages, label="S2"):
    if not messages:
        return f"✅ No current service messages for {label}."
    out = [f"<b>🚆 Current service alerts for {label}:</b>\n"]
    for m in messages:
        title = html.escape(m.get("title", ""))
        pub = datetime.datetime.utcfromtimestamp(m["publication"]/1000).strftime("%d.%m.%Y %H:%M")
        out.append(f"• <b>{title}</b>\n🕓 {pub} UTC\n")
    return "\n".join(out)

# -------- Deutsche Bahn Timetables ----------
def get_station_id(station_name):
    url = "https://apis.deutschebahn.com/db-api-marketplace/apis/station-data/v2/stations"
    h = {"Accept": "application/json","DB-Client-Id": DB_CLIENT_ID,"DB-Api-Key": DB_API_KEY}
    r = requests.get(url, headers=h, params={"searchstring": station_name})
    r.raise_for_status()
    data = r.json().get("result")
    if not data: return None
    return data[0]["evaNumbers"][0]["number"], data[0]["name"]

def get_departures(station_id, line_label):
    now = datetime.datetime.now()
    date = now.strftime("%y%m%d")
    hour = now.strftime("%H")
    url = f"https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan/{station_id}/{date}/{hour}"
    h = {"Accept": "application/xml","DB-Client-Id": DB_CLIENT_ID,"DB-Api-Key": DB_API_KEY}
    r = requests.get(url, headers=h)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    rows=[]
    for s in root.findall("s"):
        tl=s.find("tl"); 
        if not tl: continue
        if not tl.attrib.get("c","").startswith("S"): continue
        if normalize_label(tl.attrib.get("c")) != normalize_label(line_label): continue
        dp=s.find("dp")
        if not dp: continue
        raw=dp.attrib.get("pt","")
        if len(raw)<10: continue
        time=f"{2000+int(raw[0:2]):04d}-{int(raw[2:4]):02d}-{int(raw[4:6]):02d} {int(raw[6:8]):02d}:{int(raw[8:10]):02d}"
        path=dp.attrib.get("ppth","")
        dest=path.split("|")[-1] if path else ""
        rows.append((time,dest))
    rows.sort(key=lambda x:x[0])
    return rows[:10]

# ========== Telegram Bot Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(1,5)],
        [InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(5,9)]]
    await update.message.reply_text("🚆 Choose an S-Bahn line:", reply_markup=InlineKeyboardMarkup(kb))

async def choose_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    _, label=q.data.split("|",1)
    context.user_data["line"]=label
    kb=[
        [InlineKeyboardButton("📢 Service Messages",callback_data=f"MSG|{label}")],
        [InlineKeyboardButton("🕓 Departures",callback_data=f"DEP|{label}")],
        [InlineKeyboardButton("⬅ Back to Lines",callback_data="BACK|LINES")]
    ]
    await q.edit_message_text(f"Line {label} — choose what to view:", reply_markup=InlineKeyboardMarkup(kb))

async def show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    _, label=q.data.split("|",1)
    try:
        data=fetch_mvg()
        msgs=filter_mvg(data,label)
        text=format_mvg(msgs,label)
    except Exception as e:
        text=f"❌ Error loading data: {e}"
    kb=[[InlineKeyboardButton("⬅ Back", callback_data=f"LINE|{label}")]]
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def ask_station(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    _, label=q.data.split("|",1)
    context.user_data["line"]=label
    context.user_data["await_station"]=True
    kb=[[InlineKeyboardButton("⬅ Back", callback_data=f"LINE|{label}")]]
    await q.edit_message_text(f"Please enter a station name for {label} (e.g., Erding):", reply_markup=InlineKeyboardMarkup(kb))

async def handle_station_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_station"): return
    name=update.message.text.strip()
    label=context.user_data.get("line","S2")
    context.user_data["await_station"]=False
    try:
        sid,fullname=get_station_id(name)
        if not sid:
            await update.message.reply_text(f"🚫 Station '{name}' not found.")
            return
        deps=get_departures(sid,label)
        if not deps:
            await update.message.reply_text(f"No departures for {label} at {fullname}.")
            return
        out=f"🕓 <b>Next {label} departures from {fullname}</b>\n\n"
        for t,dest in deps:
            out+=f"• {t} → {dest}\n"
        kb=[[InlineKeyboardButton("⬅ Back", callback_data=f"LINE|{label}")]]
        await update.message.reply_text(out, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    _, target = q.data.split("|",1)
    if target == "LINES":
        kb=[[InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(1,5)],
            [InlineKeyboardButton(f"S{i}", callback_data=f"LINE|S{i}") for i in range(5,9)]]
        await q.edit_message_text("🚆 Choose an S-Bahn line:", reply_markup=InlineKeyboardMarkup(kb))

# ========== Run ==========
if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(choose_action, pattern="^LINE\\|"))
    app.add_handler(CallbackQueryHandler(show_messages, pattern="^MSG\\|"))
    app.add_handler(CallbackQueryHandler(ask_station, pattern="^DEP\\|"))
    app.add_handler(CallbackQueryHandler(handle_back, pattern="^BACK\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_station_name))
    print("✅ Bot is running...")
    app.run_polling()
