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
BOT_VERSION = "3.0"
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
JS_RUNTIME = {"node": {"path": "node"}}

# Создаем папку для скачиваний
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ========== ШИФРОВАНИЕ COOKIES ==========
key = hashlib.sha256(SECRET_KEY.encode()).digest()
cipher = Fernet(base64.urlsafe_b64encode(key))

# ========== БАЗА ДАННЫХ ДЛЯ COOKIES ==========
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

# ========== FLASK ДЛЯ HEALTH CHECK (RENDER) ==========
app_flask = Flask(__name__)

@app_flask.route('/')
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port, threaded=True)

# ========== ИНИЦИАЛИЗАЦИЯ ТЕЛЕГРАМ БОТА ==========
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
            if downloaded_bytes > MAX_FILESIZE:
                raise DownloadCancelled("File too large")
            
            perc = round(d["downloaded_bytes"] * 100 / d["total_bytes"])
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg.message_id,
                text=f"📥 Скачивание: {d.get('info_dict', {}).get('title', 'файла')[:50]}...\n\n{perc}%\n\n<i>SELG Bot v{BOT_VERSION}</i>",
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
    downloads = info.get("requested_downloads") or None
    if not downloads:
        if info.get("entries") is not None and len(info.get("entries")) > 0:
            downloads = info.get("entries")[0].get("requested_downloads") or None
    if not downloads or len(downloads) == 0:
        raise Exception("No requested downloads found")
    
    filepath = downloads[0]["filepath"]
    file_size = os.path.getsize(filepath)
    
    with open(filepath, "rb") as f:
        if audio:
            bot.send_audio(message.chat.id, f, reply_to_message_id=message.message_id)
        else:
            bot.send_video(
                message.chat.id, f, reply_to_message_id=message.message_id,
                width=downloads[0].get("width", 0), height=downloads[0].get("height", 0),
                caption=f"🎬 Скачано с помощью {BOT_NAME} Bot"
            )
    
    os.remove(filepath)
    logger.info(f"Файл отправлен и удален: {filepath} ({format_size(file_size)})")

# ========== ОЧИСТКА ==========
def cleanup_file(video_title: int) -> None:
    for file in os.listdir(OUTPUT_FOLDER):
        if file.startswith(str(video_title)):
            os.remove(os.path.join(OUTPUT_FOLDER, file))

# ========== ОСНОВНАЯ ФУНКЦИЯ СКАЧИВАНИЯ ==========
def download_media(message, content, audio=False, format_id=None) -> None:
    # Проверка URL
    match = re.search(r"https?://\S+", content)
    url = match.group(0) if match else content
    
    if not urlparse(url).scheme:
        bot.reply_to(message, "❌ Неверный URL")
        return
    
    if not is_allowed_domain(url):
        bot.reply_to(message, "❌ Неподдерживаемая платформа.\n\nПоддерживаются: YouTube, TikTok, Instagram, Twitter")
        return
    
    if urlparse(url).netloc in {"www.youtube.com", "youtube.com", "youtu.be", "m.youtube.com", "youtube-nocookie.com"}:
        if not youtube_url_validation(url):
            bot.reply_to(message, "❌ Неверная ссылка YouTube")
            return
    
    # Статусное сообщение (ИСПРАВЛЕНО)
    msg = bot.reply_to(message, f"📥 Начинаю скачивание...\n\nSELG Bot v{BOT_VERSION}", parse_mode="HTML")
    video_title = round(time.time() * 1000)
    
    # Настройки yt-dlp
    ydl_opts = {
        "format": format_id if format_id else "bestvideo+bestaudio/best",
        "outtmpl": f"{OUTPUT_FOLDER}/{video_title}.%(ext)s",
        "progress_hooks": [make_progress_hook(message, msg)],
        "max_filesize": MAX_FILESIZE,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}] if audio else [],
    }
    
    # Добавляем JS runtime для YouTube
    if JS_RUNTIME:
        ydl_opts["js_runtimes"] = JS_RUNTIME
    
    # Добавляем cookies если есть
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
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=msg.message_id,
                text="📤 Отправляю файл в Telegram..."
            )
            send_media(message, info, audio)
            bot.delete_message(message.chat.id, msg.message_id)
            
    except DownloadCancelled:
        bot.edit_message_text("❌ Файл слишком большой!", message.chat.id, msg.message_id)
    except (DownloadError, ExtractorError) as e:
        err = str(e).lower()
        if "sign in" in err or "login required" in err:
            text = "⚠️ YouTube требует авторизации.\n\n💡 Используйте команду /cookies и отправьте cookies.txt файл из браузера"
        elif "rate-limit" in err:
            text = "⚠️ Слишком много запросов. Попробуйте через 10-15 минут"
        else:
            text = f"❌ Ошибка при скачивании: {str(e)[:100]}"
        bot.edit_message_text(text, message.chat.id, msg.message_id)
    except Exception as e:
        logger.error(f"Download error: {e}")
        bot.edit_message_text(
            f"❌ Не удалось скачать. Убедитесь, что файл меньше {round(MAX_FILESIZE / 1_000_000)}MB",
            message.chat.id, msg.message_id
        )
    finally:
        if cookie_file and os.path.exists(cookie_file):
            os.remove(cookie_file)
        cleanup_file(video_title)

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

# ========== КОМАНДЫ БОТА ==========
@bot.message_handler(commands=["start", "help"])
def start_command(message):
    user = message.from_user
    welcome_text = f"""
🌟 <b>ДОБРО ПОЖАЛОВАТЬ, {user.first_name}!</b>

╔══════════════════════════════════════════════╗
║  🚀 <b>{BOT_NAME} - Universal Downloader Bot</b>  ║
║     <i>Ваш универсальный загрузчик контента</i>  ║
╚══════════════════════════════════════════════╝

✨ <b>Что умеет бот:</b>
• ▶️ <b>YouTube</b> - видео (включая Shorts)
• 🎵 <b>TikTok</b> - видео без водяного знака
• 📸 <b>Instagram</b> - публичные посты и Reels
• 🐦 <b>Twitter/X</b> - видео
• 🎶 <b>Музыка</b> - команда /audio <ссылка>

🎯 <b>Просто отправьте ссылку или используйте команды:</b>
• /audio <url> - скачать аудио MP3
• /custom <url> - выбрать формат
• /cookies - добавить cookies (для YouTube)

📞 <b>Контакты:</b> @barbosick89
"""
    bot.reply_to(message, welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())

@bot.message_handler(commands=["download"])
def download_command(message):
    text = message.text.replace("/download", "").strip()
    if not text:
        bot.reply_to(message, "❌ Используйте: `/download https://youtu.be/...`", parse_mode="Markdown")
        return
    download_media(message, text, audio=False)

@bot.message_handler(commands=["audio"])
def audio_command(message):
    text = message.text.replace("/audio", "").strip()
    if not text:
        bot.reply_to(message, "❌ Используйте: `/audio https://youtu.be/...`", parse_mode="Markdown")
        return
    download_media(message, text, audio=True)

@bot.message_handler(commands=["custom"])
def custom_command(message):
    text = message.text.replace("/custom", "").strip()
    if not text:
        bot.reply_to(message, "❌ Используйте: `/custom https://youtu.be/...`", parse_mode="Markdown")
        return
    
    msg = bot.reply_to(message, "🔍 Получаю доступные форматы...")
    try:
        with yt_dlp.YoutubeDL() as ydl:
            info = ydl.extract_info(text, download=False)
        
        formats = info.get("formats") or []
        data = {}
        for f in formats:
            if f.get("vcodec") != "none":
                resolution = f.get("resolution", "unknown")
                ext = f.get("ext", "mp4")
                label = f"{resolution}.{ext}"
                data[label] = {"callback_data": f"format_{f['format_id']}"}
        
        if not data:
            bot.edit_message_text("❌ Не найдено доступных форматов", message.chat.id, msg.message_id)
        else:
            markup = quick_markup(data, row_width=2)
            bot.delete_message(msg.chat.id, msg.message_id)
            bot.reply_to(message, "🎬 Выберите формат:", reply_markup=markup)
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка: {str(e)[:100]}", message.chat.id, msg.message_id)

@bot.message_handler(commands=["cookies"])
def cookies_command(message):
    bot.reply_to(message, 
        "🍪 **Cookies для YouTube**\n\n"
        "1. Установите расширение [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
        "2. Войдите в YouTube в браузере\n"
        "3. Нажмите на иконку расширения и экспортируйте cookies\n"
        "4. Отправьте полученный файл с командой /cookies\n\n"
        "После этого бот сможет обходить блокировки YouTube!",
        parse_mode="Markdown", disable_web_page_preview=True)

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
        bot.reply_to(message, "✅ Cookies успешно сохранены! Теперь YouTube будет работать лучше.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при сохранении cookies: {str(e)[:100]}")

@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"Chat ID: `{message.chat.id}`", parse_mode="Markdown")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_url(message):
    text = message.text.strip()
    if re.match(r"https?://\S+", text):
        download_media(message, text, audio=False)

# ========== ОБРАБОТЧИК КНОПОК ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "help":
        help_text = f"""
📖 <b>ИНСТРУКЦИЯ</b>

<b>🟢 Команды:</b>
• /download <url> - скачать видео
• /audio <url> - скачать аудио MP3
• /custom <url> - выбрать формат
• /cookies - добавить cookies для YouTube

<b>Поддерживаемые платформы:</b>
• YouTube, TikTok, Instagram, Twitter/X

⚙️ <b>YouTube не работает?</b>
Используйте команду /cookies и отправьте cookies файл
"""
        bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "faq":
        faq_text = f"""
❓ <b>FAQ</b>

<b>❌ YouTube не скачивается?</b>
• Используйте /cookies и отправьте cookies из браузера
• Подождите 10-15 минут (временная блокировка)

<b>🎵 Как скачать аудио?</b>
• /audio https://youtu.be/...

<b>💰 Бесплатно?</b>
• Да, бот полностью бесплатный!

<b>🐛 Нашёл баг?</b>
• @barbosick89

<b>📌 Версия:</b> {BOT_NAME} v{BOT_VERSION}
"""
        bot.edit_message_text(faq_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "platforms":
        platforms_text = """
📱 <b>ПЛАТФОРМЫ</b>

✅ <b>Поддерживаются:</b>
• ▶️ YouTube (с cookies)
• 🎵 TikTok
• 📸 Instagram (публичные)
• 🐦 Twitter/X

💡 <b>Совет:</b>
Для YouTube используйте /cookies для обхода блокировок
"""
        bot.edit_message_text(platforms_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "music":
        music_text = """
🎵 <b>МУЗЫКА</b>

<b>Как скачать MP3:</b>
<code>/audio https://youtu.be/...</code>

Поддерживаются любые видео с YouTube, TikTok, Instagram

📌 Формат: MP3, 192 kbps
"""
        bot.edit_message_text(music_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "cookies_info":
        cookies_text = """
🍪 <b>COOKIES ДЛЯ YOUTUBE</b>

<b>Зачем нужны?</b>
YouTube блокирует скачивание из облака. Cookies решают эту проблему.

<b>Как получить:</b>
1. Установите расширение "Get cookies.txt LOCALLY"
2. Войдите в YouTube в браузере
3. Экспортируйте cookies в файл
4. Отправьте файл боту командой /cookies

<b>Безопасно?</b>
Cookies хранятся в зашифрованном виде
"""
        bot.edit_message_text(cookies_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "contacts":
        contacts_text = f"""
📞 <b>КОНТАКТЫ</b>

🐛 <b>По вопросам и багам:</b>
• Telegram: <b>@barbosick89</b>

💡 <b>По любым вопросам:</b>
• Ошибки в работе
• Предложения

⏰ <b>Время ответа:</b>
Обычно в течение 24 часов

📌 <b>{BOT_NAME} v{BOT_VERSION}</b>
"""
        bot.edit_message_text(contacts_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "about":
        about_text = f"""
👤 <b>О БОТЕ</b>

<b>Название:</b> {BOT_NAME}
<b>Версия:</b> {BOT_VERSION}
<b>Разработчик:</b> @barbosick89

✨ <b>Возможности:</b>
• YouTube (с cookies)
• TikTok
• Instagram
• Twitter/X

📡 <b>Хостинг:</b> Render
🍪 <b>Cookies:</b> зашифрованы

🐛 <b>Нашли баг?</b> @barbosick89
"""
        bot.edit_message_text(about_text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=get_back_keyboard())
    
    elif call.data == "main_menu":
        bot.edit_message_text(
            "🎯 <b>ГЛАВНОЕ МЕНЮ</b>\n\nОтправьте ссылку или используйте команды!",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=get_main_keyboard()
        )
    
    elif call.data.startswith("format_"):
        format_id = call.data.replace("format_", "")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        download_media(call.message.reply_to_message, call.message.reply_to_message.text, format_id=format_id)
    
    bot.answer_callback_query(call.id)

# ========== ЗАПУСК БОТА ==========
def run_bot():
    logger.info(f"🤖 Запуск {BOT_NAME} v{BOT_VERSION}...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        time.sleep(10)
        run_bot()

# ========== MAIN ==========
if __name__ == "__main__":
    logger.info(f"🚀 ЗАПУСК {BOT_NAME} v{BOT_VERSION} НА RENDER")
    
    # Запускаем Flask в потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ Flask сервер запущен")
    
    # Запускаем бота
    run_bot()
