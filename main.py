import base64
import datetime
import hashlib
import os
import re
import sqlite3
import time
import logging
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import requests
import telebot
import yt_dlp
from cryptography.fernet import Fernet
from telebot import types
from telebot.util import quick_markup
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError
from flask import Flask

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SECRET_KEY = os.environ.get("SECRET_KEY", "selg-secret-key-2024")
BOT_VERSION = "4.0"
BOT_NAME = "SELG"

# Настройки
MAX_FILESIZE = 50000000  # 50 MB
OUTPUT_FOLDER = "/tmp/selg_downloads"
ALLOWED_DOMAINS = [
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "youtube-nocookie.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
]

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ========== ШИФРОВАНИЕ COOKIES ==========
key = hashlib.sha256(SECRET_KEY.encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

# ========== БАЗА ДАННЫХ ==========
script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(script_dir, "selg_cookies.db")
db_conn = sqlite3.connect(db_path, check_same_thread=False)
db_cursor = db_conn.cursor()
db_cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_cookies (
        user_id INTEGER PRIMARY KEY,
        cookie_data TEXT NOT NULL
    )
""")
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

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = telebot.TeleBot(BOT_TOKEN)
last_edited = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def encrypt_cookie(cookie_data: str) -> str:
    return cipher.encrypt(cookie_data.encode()).decode()

def decrypt_cookie(encrypted_data: str) -> str:
    return cipher.decrypt(encrypted_data.encode()).decode()

def format_size(size_bytes):
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} ГБ"

def youtube_url_validation(url):
    youtube_regex = r"(https?://)?(www\.|m\.)?" \
                    r"(youtube|youtu|youtube-nocookie)\.(com|be)/" \
                    r"(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})"
    return re.match(youtube_regex, url)

def is_allowed_domain(url):
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()
        if ":" in domain:
            domain = domain.split(":")[0]
        return domain in ALLOWED_DOMAINS
    except (ValueError, AttributeError):
        return False

def filter_cookies_by_domain(cookie_data: str) -> str:
    lines = cookie_data.split("\n")
    filtered_lines = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            filtered_lines.append(line)
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lstrip(".")
        is_allowed = False
        for allowed_domain in ALLOWED_DOMAINS:
            if domain == allowed_domain or domain.endswith("." + allowed_domain):
                is_allowed = True
                break
        if is_allowed:
            filtered_lines.append(line)
    return "\n".join(filtered_lines)

def get_platform(url):
    """Определяет платформу по URL"""
    url_lower = url.lower()
    if 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'twitter'
    elif 'youtu.be' in url_lower or 'youtube.com' in url_lower:
        return 'youtube'
    return 'unknown'

def get_format_for_platform(platform, audio=False):
    """Возвращает оптимальные настройки формата для каждой платформы"""
    if audio:
        return 'bestaudio/best'
    
    if platform == 'tiktok':
        return 'best'  # TikTok лучше всего работает с форматом best
    elif platform == 'instagram':
        return 'best'  # Instagram тоже best
    elif platform == 'twitter':
        return 'best[ext=mp4]/best'
    elif platform == 'youtube':
        return 'best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best'
    else:
        return 'best'

# ========== ПРОГРЕСС ХУК ==========
def make_progress_hook(message, msg) -> Callable:
    def progress(d):
        if d["status"] != "downloading":
            return
        try:
            last = last_edited.get(f"{message.chat.id}-{msg.message_id}")
            if last and (datetime.datetime.now() - last).total_seconds() < 5:
                return
            
            downloaded_bytes = d.get("downloaded_bytes", 0)
            total_bytes = d.get("total_bytes", 1)
            
            if downloaded_bytes > MAX_FILESIZE:
                raise DownloadCancelled("File too large")
            
            if total_bytes > 0:
                perc = round(downloaded_bytes * 100 / total_bytes)
                bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=msg.message_id,
                    text=f"📥 Скачивание: {perc}%",
                    parse_mode="HTML",
                )
                last_edited[f"{message.chat.id}-{msg.message_id}"] = datetime.datetime.now()
        except DownloadCancelled:
            raise
        except Exception as e:
            logger.error(f"Progress hook error: {e}")
    return progress

# ========== ОТПРАВКА МЕДИА ==========
def send_media(message, info: Any, audio: bool = False) -> None:
    filepath = None
    
    # Пробуем найти файл
    if info.get("requested_downloads"):
        filepath = info["requested_downloads"][0].get("filepath")
    
    if not filepath and info.get("entries"):
        entries = info.get("entries")
        if entries and len(entries) > 0 and entries[0].get("requested_downloads"):
            filepath = entries[0]["requested_downloads"][0].get("filepath")
    
    if not filepath:
        for file in os.listdir(OUTPUT_FOLDER):
            if file.endswith(('.mp4', '.webm', '.mp3', '.jpg', '.png')):
                filepath = os.path.join(OUTPUT_FOLDER, file)
                break
    
    if not filepath or not os.path.exists(filepath):
        raise Exception("Не найден скачанный файл")
    
    file_size = os.path.getsize(filepath)
    
    with open(filepath, "rb") as f:
        if audio:
            bot.send_audio(message.chat.id, f, reply_to_message_id=message.message_id)
        elif filepath.endswith(('.jpg', '.png')):
            bot.send_photo(message.chat.id, f, reply_to_message_id=message.message_id)
        else:
            bot.send_video(
                message.chat.id, f, reply_to_message_id=message.message_id,
                caption=f"{BOT_NAME} Bot"
            )
    
    os.remove(filepath)
    logger.info(f"Файл отправлен и удален: {filepath} ({format_size(file_size)})")

def cleanup_old_files():
    try:
        now = time.time()
        for file in os.listdir(OUTPUT_FOLDER):
            file_path = os.path.join(OUTPUT_FOLDER, file)
            if os.path.isfile(file_path):
                if now - os.path.getmtime(file_path) > 3600:
                    os.remove(file_path)
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")

# ========== ОСНОВНАЯ ФУНКЦИЯ СКАЧИВАНИЯ ==========
def download_media(message, content, audio=False, format_id=None) -> None:
    match = re.search(r"https?://\S+", content)
    url = match.group(0) if match else content
    
    if not urlparse(url).scheme:
        bot.reply_to(message, "❌ Неверный URL")
        return
    
    if not is_allowed_domain(url):
        bot.reply_to(message, "❌ Неподдерживаемая платформа")
        return
    
    platform = get_platform(url)
    msg = bot.reply_to(message, f"📥 Скачиваю с {platform.upper()}...\n\nSELG Bot v{BOT_VERSION}", parse_mode="HTML")
    video_title = round(time.time() * 1000)
    
    # Выбираем формат для платформы
    if format_id:
        selected_format = format_id
    else:
        selected_format = get_format_for_platform(platform, audio)
    
    ydl_opts = {
        "format": selected_format,
        "outtmpl": f"{OUTPUT_FOLDER}/{video_title}.%(ext)s",
        "progress_hooks": [make_progress_hook(message, msg)],
        "max_filesize": MAX_FILESIZE,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }
    
    if audio:
        ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    
    cookie_file = None
    try:
        user_id = message.from_user.id
        db_cursor.execute("SELECT cookie_data FROM user_cookies WHERE user_id = ?", (user_id,))
        result = db_cursor.fetchone()
        
        if result:
            decrypted_data = decrypt_cookie(result[0])
            cookie_file = f"{OUTPUT_FOLDER}/cookies_{user_id}.txt"
            with open(cookie_file, "w") as f:
                f.write(decrypted_data)
            ydl_opts["cookiefile"] = cookie_file
            logger.info(f"Используются cookies для user {user_id}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            send_media(message, info, audio)
            bot.delete_message(message.chat.id, msg.message_id)
            
    except DownloadCancelled:
        bot.edit_message_text("❌ Файл слишком большой! Максимум 50 MB.", message.chat.id, msg.message_id)
    except (DownloadError, ExtractorError) as e:
        err = str(e).lower()
        logger.error(f"Ошибка: {err}")
        
        if "sign in" in err or "login required" in err:
            text = "⚠️ Требуется авторизация.\n\nИспользуйте /cookies и отправьте cookies файл"
        elif "rate-limit" in err:
            text = "⚠️ Слишком много запросов. Попробуйте через 10-15 минут"
        elif "tiktok" in platform and "block" in err:
            text = "⚠️ TikTok временно блокирует запросы. Попробуйте через 1-2 минуты"
        else:
            text = f"❌ Ошибка: {str(e)[:100]}\n\nПопробуйте еще раз через минуту"
        
        bot.edit_message_text(text, message.chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Download error: {e}")
        bot.edit_message_text(
            f"❌ Ошибка: {str(e)[:100]}\n\nПопробуйте еще раз или свяжитесь с @barbosick89",
            message.chat.id, msg.message_id
        )
    finally:
        if cookie_file and os.path.exists(cookie_file):
            os.remove(cookie_file)
        cleanup_old_files()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("📖 Инструкция", callback_data="help"),
        types.InlineKeyboardButton("❓ FAQ", callback_data="faq")
    )
    keyboard.add(
        types.InlineKeyboardButton("📱 Платформы", callback_data="platforms"),
        types.InlineKeyboardButton("🎵 Музыка", callback_data="music")
    )
    keyboard.add(
        types.InlineKeyboardButton("🍪 Cookies", callback_data="cookies_info"),
        types.InlineKeyboardButton("📞 Контакты", callback_data="contacts")
    )
    keyboard.add(
        types.InlineKeyboardButton("👤 О боте", callback_data="about")
    )
    return keyboard

def get_back_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu"))
    return keyboard

# ========== КОМАНДЫ ==========
@bot.message_handler(commands=["start", "help"])
def start_command(message):
    user = message.from_user
    welcome_text = f"""
🌟 <b>ДОБРО ПОЖАЛОВАТЬ, {user.first_name}!</b>

╔══════════════════════════════════════════════╗
║  🚀 <b>{BOT_NAME} - Universal Downloader Bot</b>  ║
║     <i>Ваш загрузчик контента</i>               ║
╚══════════════════════════════════════════════╝

✨ <b>Что умеет бот:</b>
• 🎵 TikTok
• 📸 Instagram
• ▶️ YouTube
• 🐦 Twitter/X
• 🎶 MP3 музыка

📞 <b>Контакты:</b> @barbosick89
"""
    bot.reply_to(message, welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())

@bot.message_handler(commands=["audio"])
def audio_command(message):
    text = message.text.replace("/audio", "").strip()
    if not text:
        bot.reply_to(message, "❌ Используйте: /audio https://youtu.be/...")
        return
    download_media(message, text, audio=True)

@bot.message_handler(commands=["cookies"])
def cookies_command(message):
    bot.reply_to(message, 
        "🍪 COOKIES ДЛЯ YouTube\n\n"
        "1. Установите расширение Get cookies.txt LOCALLY\n"
        "2. Войдите в YouTube в браузере\n"
        "3. Экспортируйте cookies в файл\n"
        "4. Отправьте файл боту\n\n"
        "Cookies хранятся в зашифрованном виде")

@bot.message_handler(content_types=["document"])
def handle_cookie_file(message):
    if not message.document:
        return
    
    user_id = message.from_user.id
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    try:
        cookie_data = downloaded_file.decode("utf-8")
        filtered_data = filter_cookies_by_domain(cookie_data)
        encrypted_data = encrypt_cookie(filtered_data)
        
        db_cursor.execute(
            "INSERT OR REPLACE INTO user_cookies (user_id, cookie_data) VALUES (?, ?)",
            (user_id, encrypted_data)
        )
        db_conn.commit()
        bot.reply_to(message, "✅ Cookies сохранены! YouTube будет работать лучше.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)[:100]}")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_url(message):
    text = message.text.strip()
    if re.match(r"https?://\S+", text):
        download_media(message, text, audio=False)

# ========== ОБРАБОТЧИК КНОПОК ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "help":
        help_text = """
📖 ИНСТРУКЦИЯ

Просто отправьте ссылку из:
• TikTok
• Instagram
• YouTube
• Twitter/X

Команды:
/audio - скачать MP3
/cookies - добавить cookies для YouTube
"""
        bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "faq":
        faq_text = f"""
❓ FAQ

❌ Не скачивается?
• Попробуйте еще раз через минуту
• Для YouTube используйте /cookies

🎵 MP3?
• /audio (ссылка)

📌 Версия: {BOT_NAME} v{BOT_VERSION}
"""
        bot.edit_message_text(faq_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "platforms":
        platforms_text = """
📱 ПЛАТФОРМЫ

✅ Поддерживаются:
• TikTok
• Instagram
• YouTube
• Twitter/X

Для YouTube используйте /cookies
"""
        bot.edit_message_text(platforms_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "music":
        music_text = """
🎵 MP3 АУДИО

/audio https://youtu.be/...

Формат: MP3
"""
        bot.edit_message_text(music_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "cookies_info":
        cookies_text = """
🍪 COOKIES

Зачем? YouTube блокирует скачивание.

Как получить?
1. Расширение "Get cookies.txt LOCALLY"
2. Войдите в YouTube
3. Экспортируйте cookies
4. Отправьте файл боту
"""
        bot.edit_message_text(cookies_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "contacts":
        contacts_text = f"""
📞 КОНТАКТЫ

🐛 Баги и вопросы:
@barbosick89

{BOT_NAME} v{BOT_VERSION}
"""
        bot.edit_message_text(contacts_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "about":
        about_text = f"""
👤 О БОТЕ

Название: {BOT_NAME}
Версия: {BOT_VERSION}
Разработчик: @barbosick89

Хостинг: Render
"""
        bot.edit_message_text(about_text, call.message.chat.id, call.message.message_id, reply_markup=get_back_keyboard())
    
    elif call.data == "main_menu":
        bot.edit_message_text(
            "🎯 ГЛАВНОЕ МЕНЮ\n\nОтправьте ссылку!",
            call.message.chat.id, call.message.message_id,
            reply_markup=get_main_keyboard()
        )
    
    bot.answer_callback_query(call.id)

# ========== ЗАПУСК ==========
def run_bot():
    logger.info(f"Запуск {BOT_NAME} v{BOT_VERSION}...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        time.sleep(10)
        run_bot()

if __name__ == "__main__":
    logger.info(f"ЗАПУСК {BOT_NAME} v{BOT_VERSION} НА RENDER")
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask сервер запущен")
    
    cleanup_old_files()
    run_bot()
