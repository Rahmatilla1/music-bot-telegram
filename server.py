import telebot
from telebot import types
import yt_dlp
import os
import subprocess
import sqlite3
from datetime import datetime
import glob
import threading
import time
import warnings
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telebot import types, apihelper

load_dotenv()

# ================== SOZLAMALAR ==================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    print("❌ TOKEN topilmadi! .env faylni tekshiring:")
    print("1. cat .env → TOKEN ko'rinadimi?")
    print("2. pwd → server.py bilan bir joydami?")
    exit(1)

print(f"✅ Token yuklandi: {TOKEN[:10]}...")

CHANNELS = ["@efoouz"]
ADMINS = [5664207838]
warnings.filterwarnings("ignore")

user_search_cache = {}
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

apihelper.READ_TIMEOUT = 300
apihelper.WRITE_TIMEOUT = 300
apihelper.CONNECT_TIMEOUT = 300

bot = telebot.TeleBot(TOKEN, threaded=True)

# ================== DATABASE ==================
def get_db():
    conn = sqlite3.connect("bot.db", check_same_thread=False)
    return conn, conn.cursor()

conn, c = get_db()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    subscribed INTEGER,
    last_active TEXT
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS music_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    query TEXT,
    yt_url TEXT,
    created_at TEXT
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS bot_stats (
    date TEXT PRIMARY KEY,
    users_today INTEGER DEFAULT 0,
    requests_today INTEGER DEFAULT 0
)
""")
conn.commit()
conn.close()

# ================== ADMIN FUNCTIONS ==================
def clear_music_db():
    conn, c = get_db()
    c.execute("DELETE FROM music_requests")
    deleted_count = c.rowcount
    conn.commit()
    conn.close()
    return deleted_count

# ================== UTIL ==================
def is_admin(user_id):
    return user_id in ADMINS

def clear_downloads():
    for f in glob.glob(f"{DOWNLOAD_DIR}/*"):
        try:
            if f.endswith(('.mp4', '.mp3', '.m4a', '.webm')):
                os.remove(f)
        except:
            pass

def auto_clear_downloads(interval=300):
    while True:
        clear_downloads()
        time.sleep(interval)

threading.Thread(target=auto_clear_downloads, daemon=True).start()

# ================== STATS FUNKSIYALARI ==================
def update_daily_stats(user_id=None, is_request=False):
    today = datetime.now().strftime('%Y-%m-%d')
    conn, c = get_db()

    if user_id:
        c.execute("UPDATE bot_stats SET users_today = users_today + 1 WHERE date = ?", (today,))
        if c.rowcount == 0:
            c.execute("INSERT INTO bot_stats (date, users_today) VALUES (?, 1)", (today,))

    if is_request:
        c.execute("UPDATE bot_stats SET requests_today = requests_today + 1 WHERE date = ?", (today,))
        if c.rowcount == 0:
            c.execute("INSERT INTO bot_stats (date, requests_today) VALUES (?, 1)", (today,))

    conn.commit()
    conn.close()

def get_monthly_stats():
    conn, c = get_db()
    today = datetime.now().strftime('%Y-%m-%d')

    c.execute("""
        SELECT COALESCE(users_today, 0), COALESCE(requests_today, 0)
        FROM bot_stats WHERE date = ?
    """, (today,))
    today_row = c.fetchone()
    today_users = int(today_row[0]) if today_row else 0
    today_requests = int(today_row[1]) if today_row else 0

    c.execute("""
        SELECT COALESCE(SUM(users_today), 0), COALESCE(SUM(requests_today), 0)
        FROM bot_stats
        WHERE date >= date('now', '-30 days')
    """)
    month_row = c.fetchone()
    month_users = int(float(month_row[0])) if month_row[0] is not None else 0
    month_requests = int(float(month_row[1])) if month_row[1] is not None else 0

    conn.close()
    return today_users, month_users, today_requests, month_requests

def update_bot_description():
    try:
        today_users, month_users, today_requests, month_requests = get_monthly_stats()

        month_str = f"{int(month_users):,}"
        today_str = f"{int(today_users):,}"
        req_str = f"{int(today_requests):,}"

        short_desc = f"🎵 Musiqa | Oyda {month_str} foydalanuvchi"
        description = f"""🎵 Musiqa Bot

Bugun: {today_str} foydalanuvchi, {req_str} so'rov
Oyda: {month_str} foydalanuvchi

Qo'shiqchi nomi yozing → Top 10"""

        bot.set_my_short_description(short_desc)
        bot.set_my_description(description)
        print(f"✅ OK: Oyda {month_str}")

    except Exception as e:
        print(f"❌ ERROR: {e}")
        bot.set_my_short_description("🎵 Musiqa Bot | Statistika yuklanmoqda...")

def auto_update_stats():
    while True:
        update_bot_description()
        time.sleep(1800)

threading.Thread(target=auto_update_stats, daemon=True).start()

# ================== SUBSCRIBE CHECK ==================
def check_subscribe(user_id):
    for ch in CHANNELS:
        try:
            member = bot.get_chat_member(ch, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except:
            return False
    return True

def subscribe_markup():
    kb = types.InlineKeyboardMarkup()
    for ch in CHANNELS:
        kb.add(types.InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch[1:]}"))
    kb.add(types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub"))
    return kb

def save_user(user):
    conn, c = get_db()
    c.execute(
        "INSERT OR REPLACE INTO users VALUES (?,?,?,?,?)",
        (user.id, user.username, user.full_name, int(check_subscribe(user.id)), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def save_music(user_id, query, url):
    conn, c = get_db()
    c.execute(
        "INSERT INTO music_requests(user_id, query, yt_url, created_at) VALUES (?,?,?,?)",
        (user_id, query, url, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

# ================== YT-DLP OPTS (COOKIES YO‘Q) ==================
YTDLP_BASE_OPTS = {
    "quiet": False,
    "verbose": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "sleep_interval": 2,
    "max_sleep_interval": 5,
    "force_ipv4": True,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

# ================== MUSIC FUNCTIONS ==================
def search_artist_top10(artist_name):
    opts = {**YTDLP_BASE_OPTS, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{artist_name}", download=False)
        entries = info.get("entries") or []
        results = []
        for i, entry in enumerate(entries[:10], 1):
            results.append({
                "title": entry.get("title", f"Qo'shiq {i}"),
                "url": f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                "duration": entry.get("duration", 0),
                "number": i
            })
        return results

def download_instagram(url, timeout=60):
    opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "format": "mp4",
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": timeout
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

def extract_audio(video_path):
    audio_path = video_path.replace(".mp4", ".mp3")
    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-ab", "192k", audio_path, "-y"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        raise Exception("FFmpeg audio ajratishda xatolik")
    return audio_path

def download_mp3_from_url(yt_url, title, timeout=60):
    opts = {
        **YTDLP_BASE_OPTS,
        "format": "bestaudio/best",
        "outtmpl": f"{DOWNLOAD_DIR}/%(title).200s.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([yt_url])

    mp3_files = glob.glob(f"{DOWNLOAD_DIR}/*.mp3")
    if not mp3_files:
        raise Exception("MP3 topilmadi")
    mp3 = max(mp3_files, key=os.path.getctime)
    return mp3, yt_url, title

# ================== CALLBACKS ==================
@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_cb(call):
    if check_subscribe(call.from_user.id):
        bot.edit_message_text("✅ Obuna tasdiqlandi! Endi musiqa yuklash mumkin",
                              call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ Hali kanalga obuna bo'lmadingiz!", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("song_"))
def song_callback(call):
    if not check_subscribe(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Avval kanalga obuna bo'ling!", show_alert=True)
        return

    loading = bot.send_message(call.message.chat.id, "⏳ Qo'shiq yuklanmoqda...")
    update_daily_stats(call.from_user.id, is_request=True)

    try:
        index = int(call.data.split("_")[1])
        songs = user_search_cache.get(call.from_user.id)
        if not songs or index >= len(songs):
            raise Exception("Qo'shiq topilmadi")

        song = songs[index]
        mp3_path, url, title = download_mp3_from_url(song["url"], song["title"])

        with open(mp3_path, "rb") as audio:
            bot.send_audio(call.message.chat.id, audio, title=title)

        save_music(call.from_user.id, title, url)

    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Xatolik: {e}")

    finally:
        try:
            bot.delete_message(call.message.chat.id, loading.message_id)
        except:
            pass
        clear_downloads()

@bot.callback_query_handler(func=lambda c: c.data == "clear_music_db")
def clear_music_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Siz admin emassiz!", show_alert=True)
        return

    deleted_count = clear_music_db()
    bot.answer_callback_query(call.id, f"✅ Music DB tozalandi!\n🗑️ {deleted_count} ta yozuv o'chirildi", show_alert=True)

# ================== COMMANDS ==================
@bot.message_handler(commands=["start"])
def start(m):
    save_user(m.from_user)
    update_daily_stats(m.from_user.id)

    text = """👋 Assalomu alaykum!

🎵 Musiqa nomini yozing
🎤 Qo'shiqchi nomi yozsangiz - 10 ta qo'shiq
📱 Instagram/YouTube link yuboring"""

    if not check_subscribe(m.from_user.id):
        bot.send_message(m.chat.id, text + "\n\n📢 Avval kanalga obuna bo'ling:", reply_markup=subscribe_markup())
    else:
        bot.send_message(m.chat.id, text)

@bot.message_handler(commands=["stats"])
def stats(m):
    if not is_admin(m.from_user.id):
        return bot.send_message(m.chat.id, "⛔ Siz admin emassiz")

    today_users, month_users, today_requests, month_requests = get_monthly_stats()
    conn, c = get_db()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM music_requests")
    total_requests_db = c.fetchone()[0]
    conn.close()

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🗑️ Clear Music DB", callback_data="clear_music_db"))

    bot.send_message(m.chat.id, f"""📊 STATISTIKA

👥 JAMI Foydalanuvchilar: {total_users}
🎵 JAMI So'rovlar: {total_requests_db}

📅 BUGUN:
👤 Foydalanuvchilar: {today_users}
🎧 So'rovlar: {today_requests}

📈 OYDA (30 kun):
👥 Foydalanuvchilar: {month_users:,}
🎵 So'rovlar: {month_requests:,}""", reply_markup=kb)

# ================== MAIN HANDLER ==================
@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle(m):
    save_user(m.from_user)
    update_daily_stats(m.from_user.id, is_request=True)

    if not check_subscribe(m.from_user.id):
        bot.send_message(m.chat.id, "❗ Avval kanalga obuna bo'ling", reply_markup=subscribe_markup())
        return

    loading = bot.send_message(m.chat.id, "🔍 Qidirilmoqda...")

    try:
        results = search_artist_top10(m.text)
        if results:
            user_search_cache[m.from_user.id] = results
            text = f"🎤 <b>{m.text.upper()}</b> - Top 10:\n\n"
            kb = types.InlineKeyboardMarkup(row_width=2)

            buttons = []
            for song in results:
                dur = int(song.get("duration") or 0)
                duration = f" ({dur//60}:{dur%60:02d})" if dur else ""
                btn_text = f"{song['number']}. {song['title'][:35]}{duration}"[:50]
                buttons.append(types.InlineKeyboardButton(btn_text, callback_data=f"song_{song['number'] - 1}"))

            kb.add(*buttons)
            bot.delete_message(m.chat.id, loading.message_id)
            bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML")
            return

        if "instagram.com" in m.text:
            video_path = download_instagram(m.text)

            with open(video_path, "rb") as video:
                bot.send_video(m.chat.id, video, caption="🎥 Video + Original Musiqa")

            audio_path = extract_audio(video_path)
            with open(audio_path, "rb") as audio:
                bot.send_audio(m.chat.id, audio, title="🔊 Ovoz (Musiqasiz)")

            bot.delete_message(m.chat.id, loading.message_id)
            return

        # Oddiy qidiruv ham ishlashi uchun: topilgan birinchi video url dan yuklaymiz
        with yt_dlp.YoutubeDL({**YTDLP_BASE_OPTS, "extract_flat": True}) as ydl:
            info = ydl.extract_info(f"ytsearch1:{m.text}", download=False)
            entry = (info.get("entries") or [None])[0]
            if not entry:
                raise Exception("Natija topilmadi")
            yt_url = f"https://www.youtube.com/watch?v={entry.get('id')}"

        mp3_path, url, title = download_mp3_from_url(yt_url, m.text)

        with open(mp3_path, "rb") as audio:
            bot.send_audio(m.chat.id, audio, title=title)

        save_music(m.from_user.id, title, url)

    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Xatolik: {e}")

    finally:
        clear_downloads()
        try:
            bot.delete_message(m.chat.id, loading.message_id)
        except:
            pass

# ================== BOT COMMANDS ==================
def set_bot_commands():
    bot.set_my_commands([
        types.BotCommand("start", "🎵 Boshlash"),
        types.BotCommand("stats", "📊 Statistika (admin)")
    ])

set_bot_commands()

# ================== HEALTH SERVER (RENDER) ==================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()

if __name__ == "__main__":
    update_bot_description()
    print("🚀 Bot ishga tushdi - Stats FAOL!")
    bot.infinity_polling()