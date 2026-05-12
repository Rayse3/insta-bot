import base64
import datetime
import hashlib
import os
import re
import sqlite3
import time
import logging
import threading
from urllib.parse import urlparse
from pathlib import Path

import telebot
import yt_dlp
from cryptography.fernet import Fernet
from telebot import types
from flask import Flask

# ========== НАСТРОЙКА ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SECRET_KEY = os.environ.get("SECRET_KEY", "selg-secret-key-2024")
BOT_VERSION = "5.0"
BOT_NAME = "SELG"

MAX_FILESIZE = 50000000
OUTPUT_FOLDER = "/tmp/selg_downloads"
ALLOWED_DOMAINS = [
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "youtube-nocookie.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
]

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ========== ШИФРОВАНИЕ ==========
key = hashlib.sha256(SECRET_KEY.encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

# ========== БАЗА ДАННЫХ ==========
db_path = Path(__file__).parent / "selg_cookies.db"
db_conn = sqlite3.connect(db_path, check_same_thread=False)
db_cursor = db_conn.cursor()
db_cursor.execute("CREATE TABLE IF NOT EXISTS user_cookies (user_id INTEGER PRIMARY KEY, cookie_data TEXT NOT NULL)")
db_conn.commit()

# ========== FLASK ==========
app_flask = Flask(__name__)

@app_flask.route('/')
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port, threaded=True)

# ========== БОТ ==========
bot = telebot.TeleBot(BOT_TOKEN)
last_edited = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def encrypt_cookie(data: str) -> str:
    return cipher.encrypt(data.encode()).decode()

def decrypt_cookie(data: str) -> str:
    return cipher.decrypt(data.encode()).decode()

def format_size(size_bytes):
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} ГБ"

def get_platform(url):
    url_lower = url.lower()
    if 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'youtu.be' in url_lower or 'youtube.com' in url_lower:
        return 'youtube'
    return 'unknown'

def is_allowed_domain(url):
    try:
        domain = urlparse(url).netloc.lower().split(':')[0]
        return domain in ALLOWED_DOMAINS
    except:
        return False

def filter_cookies_by_domain(cookie_data: str) -> str:
    lines = cookie_data.split("\n")
    filtered = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            filtered.append(line)
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lstrip(".")
        if domain in ["youtube.com", ".youtube.com", "www.youtube.com"]:
            filtered.append(line)
    return "\n".join(filtered)

def clean_old_files():
    try:
        now = time.time()
        for f in Path(OUTPUT_FOLDER).iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > 3600:
                f.unlink()
                logger.info(f"Удален старый файл: {f.name}")
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")

# ========== СКАЧИВАНИЕ ==========
def download_media(message, url, is_audio=False):
    """Скачивание видео или аудио"""
    msg = bot.reply_to(message, f"📥 Начинаю скачивание...\n\n{BOT_NAME} v{BOT_VERSION}")
    video_id = round(time.time() * 1000)
    
    # Получаем cookies пользователя
    user_id = message.from_user.id
    cookie_file = None
    db_cursor.execute("SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,))
    result = db_cursor.fetchone()
    
    if result:
        try:
            cookie_data = decrypt_cookie(result[0])
            cookie_file = Path(OUTPUT_FOLDER) / f"cookies_{user_id}.txt"
            cookie_file.write_text(cookie_data)
        except Exception as e:
            logger.error(f"Ошибка cookies: {e}")
    
    # Настройки yt-dlp
    ydl_opts = {
        'format': 'best[height<=480]/best' if not is_audio else 'bestaudio/best',
        'outtmpl': str(Path(OUTPUT_FOLDER) / f"{video_id}.%(ext)s"),
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'max_filesize': MAX_FILESIZE,
    }
    
    if is_audio:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    
    if cookie_file and cookie_file.exists():
        ydl_opts['cookiefile'] = str(cookie_file)
        logger.info(f"Используем cookies для user {user_id}")
    
    try:
        # Скачивание
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        
        # Поиск скачанного файла
        downloaded_file = None
        for f in Path(OUTPUT_FOLDER).iterdir():
            if f.is_file() and f.name.startswith(str(video_id)):
                downloaded_file = f
                break
        
        # Если не нашли по ID, ищем свежий файл
        if not downloaded_file:
            for f in Path(OUTPUT_FOLDER).iterdir():
                if f.is_file() and (time.time() - f.stat().st_mtime) < 30:
                    if not is_audio and f.suffix in ['.mp4', '.webm', '.mkv']:
                        downloaded_file = f
                        break
                    elif is_audio and f.suffix == '.mp3':
                        downloaded_file = f
                        break
        
        if not downloaded_file or downloaded_file.stat().st_size == 0:
            bot.edit_message_text("❌ Не удалось найти скачанный файл", message.chat.id, msg.message_id)
            return
        
        # Отправка
        bot.edit_message_text("📤 Отправляю файл...", message.chat.id, msg.message_id)
        
        with open(downloaded_file, 'rb') as f:
            if is_audio:
                bot.send_audio(message.chat.id, f, reply_to_message_id=message.message_id)
            else:
                bot.send_video(message.chat.id, f, reply_to_message_id=message.message_id, caption=f"{BOT_NAME} Bot")
        
        bot.delete_message(message.chat.id, msg.message_id)
        downloaded_file.unlink()
        logger.info(f"Файл отправлен: {downloaded_file.name}")
        
    except yt_dlp.utils.DownloadCancelled:
        bot.edit_message_text("❌ Файл слишком большой (максимум 50 MB)", message.chat.id, msg.message_id)
    except yt_dlp.utils.DownloadError as e:
        error = str(e).lower()
        logger.error(f"DownloadError: {error}")
        
        if "sign in" in error or "login required" in error:
            text = "⚠️ YouTube требует авторизации\n\nОтправьте cookies файл командой /cookies"
        elif "rate-limit" in error:
            text = "⚠️ Слишком много запросов\nПопробуйте через 10-15 минут"
        elif "video not available" in error:
            text = "❌ Видео недоступно\nВозможно, оно удалено или приватное"
        else:
            text = f"❌ Ошибка: {str(e)[:100]}\n\nПопробуйте еще раз"
        
        bot.edit_message_text(text, message.chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Error: {e}")
        bot.edit_message_text(f"❌ Ошибка: {str(e)[:100]}\n\nСвяжитесь: @barbosick89", message.chat.id, msg.message_id)
    finally:
        if cookie_file and cookie_file.exists():
            cookie_file.unlink()
        clean_old_files()

# ========== КЛАВИАТУРЫ ==========
def main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📖 Помощь", callback_data="help"),
        types.InlineKeyboardButton("❓ FAQ", callback_data="faq"),
        types.InlineKeyboardButton("🍪 Cookies", callback_data="cookies"),
        types.InlineKeyboardButton("📞 Контакты", callback_data="contacts"),
        types.InlineKeyboardButton("👤 О боте", callback_data="about")
    )
    return kb

def back_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="back"))
    return kb

# ========== КОМАНДЫ ==========
@bot.message_handler(commands=['start', 'help'])
def send_help(message):
    bot.reply_to(message,
        f"🌟 SELG Bot v{BOT_VERSION}\n\n"
        "Отправьте ссылку из:\n"
        "• YouTube\n"
        "• TikTok\n\n"
        "/audio ссылка - скачать MP3\n"
        "/cookies - инструкция по cookies\n\n"
        f"📞 @barbosick89",
        reply_markup=main_keyboard())

@bot.message_handler(commands=['audio'])
def audio_cmd(message):
    url = message.text.replace('/audio', '').strip()
    if not url or 'http' not in url:
        bot.reply_to(message, "❌ Используйте: /audio https://youtu.be/...")
        return
    download_media(message, url, is_audio=True)

@bot.message_handler(commands=['cookies'])
def cookies_cmd(message):
    bot.reply_to(message,
        "🍪 Как получить cookies для YouTube:\n\n"
        "1. Установите расширение Get cookies.txt LOCALLY\n"
        "2. Войдите в YouTube\n"
        "3. Экспортируйте cookies в файл\n"
        "4. Отправьте файл этим ботом\n\n"
        "После этого YouTube будет работать лучше",
        reply_markup=back_keyboard())

@bot.message_handler(content_types=['document'])
def handle_cookie_file(message):
    if not message.document or not message.document.file_name.endswith('.txt'):
        bot.reply_to(message, "❌ Отправьте файл cookies.txt")
        return
    
    file_info = bot.get_file(message.document.file_id)
    file_data = bot.download_file(file_info.file_path)
    
    try:
        cookie_str = file_data.decode('utf-8')
        filtered = filter_cookies_by_domain(cookie_str)
        
        if len(filtered) < 100:
            bot.reply_to(message, "❌ Не удалось извлечь cookies для YouTube\nПопробуйте другой файл")
            return
        
        encrypted = encrypt_cookie(filtered)
        user_id = message.from_user.id
        db_cursor.execute("INSERT OR REPLACE INTO user_cookies VALUES (?, ?)", (user_id, encrypted))
        db_conn.commit()
        
        bot.reply_to(message, "✅ Cookies сохранены! YouTube будет работать лучше")
        logger.info(f"Cookies сохранены для user {user_id}")
    except Exception as e:
        logger.error(f"Cookie error: {e}")
        bot.reply_to(message, f"❌ Ошибка: {str(e)[:100]}")

@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_url(message):
    text = message.text.strip()
    if re.match(r'https?://\S+', text) and is_allowed_domain(text):
        download_media(message, text, is_audio=False)
    elif 'http' in text:
        bot.reply_to(message, "❌ Неподдерживаемая платформа\n\nПоддерживаются: YouTube, TikTok")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "help":
        bot.edit_message_text(
            "📖 ПОМОЩЬ\n\nПросто отправьте ссылку из YouTube или TikTok\n\n/audio ссылка - скачать MP3\n/cookies - добавить cookies для YouTube",
            call.message.chat.id, call.message.message_id, reply_markup=back_keyboard())
    elif call.data == "faq":
        bot.edit_message_text(
            "❓ FAQ\n\n❌ YouTube не работает? Отправьте /cookies и добавьте файл cookies\n🎵 MP3: /audio ссылка\n🐛 Баги: @barbosick89",
            call.message.chat.id, call.message.message_id, reply_markup=back_keyboard())
    elif call.data == "cookies":
        bot.edit_message_text(
            "🍪 COOKIES\n\n1. Расширение Get cookies.txt LOCALLY\n2. Войдите в YouTube\n3. Экспортируйте cookies\n4. Отправьте файл боту",
            call.message.chat.id, call.message.message_id, reply_markup=back_keyboard())
    elif call.data == "contacts":
        bot.edit_message_text(f"📞 КОНТАКТЫ\n\n🐛 Баги и вопросы: @barbosick89\n\n{BOT_NAME} v{BOT_VERSION}", call.message.chat.id, call.message.message_id, reply_markup=back_keyboard())
    elif call.data == "about":
        bot.edit_message_text(f"👤 О БОТЕ\n\nНазвание: {BOT_NAME}\nВерсия: {BOT_VERSION}\nРазработчик: @barbosick89", call.message.chat.id, call.message.message_id, reply_markup=back_keyboard())
    elif call.data == "back":
        bot.edit_message_text(f"🌟 SELG Bot v{BOT_VERSION}\n\nОтправьте ссылку из YouTube или TikTok\n\n/audio скачать MP3\n/cookies для YouTube", call.message.chat.id, call.message.message_id, reply_markup=main_keyboard())
    
    bot.answer_callback_query(call.id)

# ========== ЗАПУСК ==========
def run_bot():
    logger.info(f"Запуск {BOT_NAME} v{BOT_VERSION}")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            time.sleep(15)

if __name__ == "__main__":
    logger.info(f"ЗАПУСК {BOT_NAME} v{BOT_VERSION} НА RENDER")
    threading.Thread(target=run_flask, daemon=True).start()
    clean_old_files()
    run_bot()
