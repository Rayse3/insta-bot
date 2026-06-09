import os
import re
import time
import logging
import threading
import asyncio
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
from flask import Flask

# ========== НАСТРОЙКА ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_VERSION = "6.5"
BOT_NAME = "SELG"

MAX_FILESIZE = 50000000
OUTPUT_FOLDER = "downloads"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ========== FLASK ==========
app_flask = Flask(__name__)

@app_flask.route('/')
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port, threaded=True)

# ========== ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ ==========
def get_platform(url: str) -> str:
    url_lower = url.lower()
    if 'instagram.com' in url_lower:
        return 'instagram'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    return 'unknown'

# ========== СКАЧИВАНИЕ ==========
async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_audio: bool = False):
    message = update.effective_message
    platform = get_platform(url)

    if platform == 'unknown':
        await message.reply_text("❌ Неподдерживаемая платформа\n\nПоддерживаются: Instagram, TikTok, YouTube")
        return

    status_msg = await message.reply_text(f"📥 Скачиваю с {platform.upper()}...")

    video_id = round(time.time() * 1000)
    ydl_opts = {
        'outtmpl': str(Path(OUTPUT_FOLDER) / f"{video_id}.%(ext)s"),
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'max_filesize': MAX_FILESIZE,
        'format': 'best',
    }

    if is_audio:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        downloaded_file = None
        for f in Path(OUTPUT_FOLDER).iterdir():
            if f.is_file() and f.name.startswith(str(video_id)):
                downloaded_file = f
                break

        if not downloaded_file:
            for f in Path(OUTPUT_FOLDER).iterdir():
                if f.is_file() and (time.time() - f.stat().st_mtime) < 30:
                    if not is_audio and f.suffix in ['.mp4', '.webm', '.mkv', '.jpg', '.png']:
                        downloaded_file = f
                        break
                    elif is_audio and f.suffix == '.mp3':
                        downloaded_file = f
                        break

        if not downloaded_file or downloaded_file.stat().st_size == 0:
            await status_msg.edit_text("❌ Не удалось найти скачанный файл")
            return

        await status_msg.edit_text("📤 Отправляю...")

        with open(downloaded_file, 'rb') as f:
            if is_audio:
                await message.reply_audio(audio=f, title=downloaded_file.stem)
            elif downloaded_file.suffix in ['.jpg', '.png']:
                await message.reply_photo(photo=f, caption=f"📸 Скачано с {platform.upper()}")
            else:
                await message.reply_video(video=f, caption=f"🎬 Скачано с {platform.upper()}")

        await status_msg.delete()
        downloaded_file.unlink()

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Ошибка: {error_msg}")
        await status_msg.edit_text(f"❌ Ошибка: {error_msg[:150]}\n\nСвяжитесь: @barbosick89")
    finally:
        for f in Path(OUTPUT_FOLDER).iterdir():
            if f.is_file() and (time.time() - f.stat().st_mtime) > 3600:
                f.unlink()

# ========== КОМАНДЫ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🌟 SELG Bot v{BOT_VERSION}\n\n"
        "📱 Поддерживаемые платформы:\n"
        "• 📸 Instagram (публичные посты, Reels)\n"
        "• 🎵 TikTok\n"
        "• ▶️ YouTube\n\n"
        "🎯 /audio ссылка - скачать MP3\n\n"
        "📞 @barbosick89"
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.replace('/audio', '').strip()
    if not url or 'http' not in url:
        await update.message.reply_text("❌ Используйте: /audio https://youtu.be/...")
        return
    await download_media(update, context, url, is_audio=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if re.match(r'https?://\S+', text):
        await download_media(update, context, text, is_audio=False)

# ========== ЗАПУСК ==========
def run_bot():
    logger.info(f"Запуск {BOT_NAME} v{BOT_VERSION}")
    
    # Создаём приложение
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Запускаем бота
    app.run_polling()

if __name__ == "__main__":
    logger.info(f"ЗАПУСК {BOT_NAME} v{BOT_VERSION} НА RENDER")
    
    # Запускаем Flask в потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    run_bot()
