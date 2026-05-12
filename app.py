import os
import sys
import logging
import threading
import traceback
import asyncio
import re
import time
import requests
import json
import aiohttp
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import yt_dlp

# ========== НАСТРОЙКА ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = "downloads"

BOT_VERSION = "2.5"
BOT_NAME = "SELG"

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не задан!")
    sys.exit(1)

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ========== FLASK ДЛЯ HEALTH CHECK ==========
app_flask = Flask(__name__)

@app_flask.route('/')
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port, threaded=True)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def format_size(size_bytes):
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} ГБ"

# ========== YOUTUBE (В РАЗРАБОТКЕ) ==========
async def download_youtube(url, message=None):
    """Скачивание YouTube видео - временно недоступно"""
    if message:
        await message.edit_text("⚙️ **YouTube временно в разработке**\n\nСкоро функция будет восстановлена!\n\n✅ Доступно сейчас:\n• TikTok\n• Музыка по названию\n• Музыка по ссылке", parse_mode='Markdown')
    return None, "YouTube временно недоступен"

# ========== TIKTOK ==========
async def download_tiktok(url, message=None):
    """Скачивание TikTok видео"""
    try:
        if message:
            await message.edit_text("🎵 Скачиваю TikTok видео...")
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'tiktok_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extract_flat': False,
            'retries': 5,
            'fragment_retries': 5,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                for ext in ['.mp4', '.webm', '.mkv']:
                    test_path = filename.rsplit('.', 1)[0] + ext
                    if os.path.exists(test_path):
                        filename = test_path
                        break
            
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return [{'path': filename, 'type': 'video', 'size': os.path.getsize(filename)}], 'single'
            
        return None, "Не удалось скачать TikTok"
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"TikTok ошибка: {error_msg}")
        
        if "blocked" in error_msg.lower():
            return None, "⚠️ TikTok временно блокирует запросы. Попробуйте через 5-10 минут"
        else:
            return None, f"Ошибка: {error_msg[:150]}"

# ========== МУЗЫКА ПО НАЗВАНИЮ ==========
async def search_and_download_music(query, message=None):
    """Поиск и скачивание музыки по названию"""
    try:
        if message:
            await message.edit_text(f"🔍 Ищу музыку: {query}...")
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'music_%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'retries': 5,
            'default_search': 'ytsearch',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch:{query}", download=True)
            
            if 'entries' in info and len(info['entries']) > 0:
                video = info['entries'][0]
                filename = ydl.prepare_filename(video)
                filename = filename.replace('.webm', '.mp3').replace('.m4a', '.mp3')
                
                if os.path.exists(filename) and os.path.getsize(filename) > 0:
                    return [{'path': filename, 'type': 'audio', 'size': os.path.getsize(filename)}], 'single'
        
        return None, "Музыка не найдена"
        
    except Exception as e:
        logger.error(f"Music ошибка: {e}")
        return None, f"Ошибка: {str(e)[:150]}"

# ========== МУЗЫКА ПО ССЫЛКЕ ==========
async def download_music_from_url(url, message=None):
    """Скачивание музыки по ссылке (SoundCloud, Spotify)"""
    try:
        if message:
            await message.edit_text("🎵 Скачиваю музыку по ссылке...")
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'music_%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'retries': 5,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            filename = filename.replace('.webm', '.mp3').replace('.m4a', '.mp3')
            
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return [{'path': filename, 'type': 'audio', 'size': os.path.getsize(filename)}], 'single'
        
        return None, "Не удалось скачать музыку"
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Music URL ошибка: {error_msg}")
        
        if "404" in error_msg:
            return None, "❌ Ссылка недоступна. Попробуйте найти музыку по названию: песня Название"
        else:
            return None, f"Ошибка: {error_msg[:150]}"

# ========== ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ ==========
def detect_platform(url):
    url_lower = url.lower()
    
    if 'tiktok.com' in url_lower:
        return 'tiktok', '🎵 TikTok'
    elif 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube', '▶️ YouTube'
    elif 'soundcloud.com' in url_lower or 'spotify.com' in url_lower:
        return 'music_url', '🎶 Музыка по ссылке'
    else:
        return 'unknown', '❓ Неизвестно'

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📖 Инструкция", callback_data="help"),
         InlineKeyboardButton("❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton("📱 Поддерживаемые платформы", callback_data="platforms"),
         InlineKeyboardButton("🎵 Поиск музыки", callback_data="music")],
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts"),
         InlineKeyboardButton("👤 О боте", callback_data="about")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard():
    keyboard = [[InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

# ========== КОМАНДЫ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    welcome_text = f"""
🌟 <b>ДОБРО ПОЖАЛОВАТЬ, {user.first_name}!</b>

╔══════════════════════════════════════════════╗
║  🚀 <b>{BOT_NAME} - Universal Downloader Bot</b>  ║
║     <i>Ваш универсальный загрузчик контента</i>  ║
╚══════════════════════════════════════════════╝

✨ <b>Что умеет бот:</b>
• 🎵 <b>TikTok</b> - видео без водяного знака
• 🎶 <b>Музыка</b> - поиск по названию или по ссылке (SoundCloud, Spotify)

⚙️ <b>В разработке:</b>
• ▶️ YouTube - скоро будет доступен

🎯 <b>Просто отправьте ссылку TikTok или напишите "песня Название"</b>

📞 <b>Контакты:</b> @barbosick89
"""
    await update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=get_main_keyboard())

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about_text = f"""
👤 <b>О БОТЕ</b>

<b>Название:</b> {BOT_NAME}
<b>Версия:</b> {BOT_VERSION}
<b>Разработчик:</b> @barbosick89

✨ <b>Возможности:</b>
• 🎵 TikTok (работает)
• 🎶 Музыка по названию (работает)
• 🎧 SoundCloud/Spotify (работает)
• ▶️ YouTube (в разработке)

📡 <b>Хостинг:</b> Render
🌍 <b>Статус:</b> 24/7
💡 <b>Бесплатный бот</b> для скачивания контента

🐛 <b>Нашли баг?</b> Свяжитесь: @barbosick89
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(about_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(about_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = f"""
📖 <b>ИНСТРУКЦИЯ</b>

<b>🟢 Работает сейчас:</b>

• <b>TikTok</b> - видео
  Пример: <code>https://www.tiktok.com/@user/video/123</code>

• <b>Музыка по названию</b>
  Пример: <code>песня Imagine Dragons Believer</code>

• <b>Музыка по ссылке</b> - SoundCloud/Spotify
  Пример: <code>https://soundcloud.com/artist/track</code>

⚙️ <b>В разработке:</b>
• <b>YouTube</b> - временно недоступен

⚠️ <b>Важно:</b>
• TikTok иногда блокирует - попробуйте позже
• Для музыки пишите "песня Название"

📞 <b>Контакты:</b> @barbosick89
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def platforms_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms_text = f"""
📱 <b>ПОДДЕРЖИВАЕМЫЕ ПЛАТФОРМЫ</b>

✅ <b>Работают сейчас:</b>
• 🎵 <b>TikTok</b> - видео без водяного знака
• 🎶 <b>Музыка</b> - поиск по названию (YouTube Music)
• 🎵 <b>SoundCloud</b> - прямая ссылка
• 🎧 <b>Spotify</b> - прямая ссылка (трек)

⚙️ <b>В разработке:</b>
• ▶️ <b>YouTube</b> - скоро добавим
• 📸 <b>Instagram</b> - в планах

💡 <b>Как скачать музыку:</b>
Напишите: <code>песня Название песни</code>

📞 <b>Контакты:</b> @barbosick89
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    faq_text = f"""
❓ <b>FAQ - ЧАСТЫЕ ВОПРОСЫ</b>

<b>🎵 Как скачать музыку по названию?</b>
Напишите: <code>песня Название песни</code>

<b>🎵 Как скачать музыку по ссылке?</b>
Отправьте ссылку из SoundCloud или Spotify

<b>⏳ Почему долго скачивается?</b>
Зависит от размера файла и скорости интернета

<b>⚠️ TikTok выдаёт ошибку?</b>
TikTok иногда блокирует запросы. Подождите 5-10 минут

<b>💰 Это бесплатно?</b>
Да, бот полностью бесплатный!

<b>🐛 Нашёл ошибку?</b>
Свяжитесь: @barbosick89

<b>📌 Версия бота:</b> {BOT_NAME} v{BOT_VERSION}
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(faq_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(faq_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def music_search_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    music_text = """
🎵 <b>ПОИСК МУЗЫКИ</b>

<b>Способы скачать музыку:</b>

1️⃣ <b>По названию:</b>
   Напишите: <code>песня Название</code>
   Пример: <code>песня Billie Eilish bad guy</code>

2️⃣ <b>По ссылке:</b>
   Отправьте ссылку на трек:
   • SoundCloud
   • Spotify

📌 <b>Поддерживаемые форматы:</b> MP3 (192 kbps)

💡 <b>Совет:</b> Чем точнее название, тем лучше результат
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(music_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(music_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contacts_text = f"""
📞 <b>КОНТАКТНАЯ ИНФОРМАЦИЯ</b>

🐛 <b>По вопросам и багам:</b>
• Telegram: <b>@barbosick89</b>

💡 <b>Обращаться по любым вопросам:</b>
• Ошибки в работе бота
• Предложения по улучшению
• Новые идеи для функционала

⏰ <b>Время ответа:</b>
Обычно в течение 24 часов

📌 <b>{BOT_NAME} v{BOT_VERSION}</b>

🌟 <b>Спасибо, что пользуетесь ботом!</b>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(contacts_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(contacts_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        f"🎯 <b>ГЛАВНОЕ МЕНЮ</b>\n\nПросто отправьте ссылку TikTok или напишите 'песня Название'!\n\n{BOT_NAME} v{BOT_VERSION}",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info(f"📨 Получено сообщение: {text[:100]}...")
    
    # Проверка на поиск музыки
    music_keywords = ['песня', 'музыка', 'скачать музыку', 'song', 'music']
    if any(kw in text.lower() for kw in music_keywords) and not ('http' in text):
        query = text
        for kw in music_keywords:
            query = query.lower().replace(kw, '').strip()
        
        status_msg = await update.message.reply_text(
            f"🔍 <b>Ищу музыку:</b> {query}\n⏳ Подождите...",
            parse_mode='HTML'
        )
        
        files, _ = await search_and_download_music(query, status_msg)
        
        if files and os.path.exists(files[0]['path']):
            await status_msg.edit_text("✅ Отправляю...")
            with open(files[0]['path'], 'rb') as f:
                await update.message.reply_audio(
                    audio=f,
                    title=os.path.basename(files[0]['path']).replace('.mp3', ''),
                    performer=f"{BOT_NAME} Bot"
                )
            os.remove(files[0]['path'])
            await status_msg.delete()
        else:
            await status_msg.edit_text(
                "❌ <b>Музыка не найдена</b>\n\n"
                "Попробуйте:\n"
                "• Уточнить название\n"
                "• Отправить ссылку из SoundCloud/Spotify\n"
                "• Пример: песня Imagine Dragons Believer",
                parse_mode='HTML'
            )
        return
    
    # Определяем платформу для ссылок
    platform, platform_name = detect_platform(text)
    
    if platform == 'unknown':
        await update.message.reply_text(
            f"❌ <b>Неверная ссылка!</b>\n\n"
            f"Поддерживаются:\n"
            f"• 🎵 TikTok (tiktok.com/@.../video/...)\n"
            f"• 🎶 Музыка: напишите 'песня Название'\n"
            f"• 🎵 SoundCloud/Spotify ссылки\n\n"
            f"⚙️ YouTube временно в разработке\n\n"
            f"Используйте /help для инструкции\n\n"
            f"📌 {BOT_NAME} v{BOT_VERSION}",
            parse_mode='HTML'
        )
        return
    
    # Скачивание
    status_msg = await update.message.reply_text(
        f"📥 <b>Скачиваю с {platform_name}</b>\n\n⏳ Подождите...",
        parse_mode='HTML'
    )
    
    if platform == 'youtube':
        files, _ = await download_youtube(text, status_msg)
    elif platform == 'tiktok':
        files, _ = await download_tiktok(text, status_msg)
    elif platform == 'music_url':
        files, _ = await download_music_from_url(text, status_msg)
    else:
        files = None
    
    if files and len(files) > 0:
        file = files[0]
        file_size_mb = file['size'] / (1024 * 1024)
        
        if file_size_mb > 50:
            await status_msg.edit_text(f"❌ Файл слишком большой ({file_size_mb:.1f} МБ)\nМаксимум: 50 МБ")
            if os.path.exists(file['path']):
                os.remove(file['path'])
            return
        
        await status_msg.edit_text(f"✅ <b>Готово!</b>\n📏 Размер: {format_size(file['size'])}\n📤 Отправляю...", parse_mode='HTML')
        
        try:
            with open(file['path'], 'rb') as f:
                if file['type'] == 'video':
                    await update.message.reply_video(video=f, caption=f"🎬 Скачано с {platform_name}", supports_streaming=True)
                else:
                    await update.message.reply_audio(audio=f, title=os.path.basename(file['path']).replace('.mp3', ''))
            
            if os.path.exists(file['path']):
                os.remove(file['path'])
            await status_msg.delete()
            
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            await status_msg.edit_text(f"❌ Ошибка при отправке\n\nСвяжитесь с поддержкой: @barbosick89")
    else:
        error_msg = files if isinstance(files, str) else "Неизвестная ошибка"
        await status_msg.edit_text(
            f"❌ <b>Не удалось скачать</b>\n\n"
            f"📱 Платформа: {platform_name}\n"
            f"🔍 Ошибка: {error_msg[:200]}\n\n"
            f"💡 Если ошибка повторяется, свяжитесь с поддержкой:\n"
            f"📞 @barbosick89\n\n"
            f"📌 {BOT_NAME} v{BOT_VERSION}",
            parse_mode='HTML'
        )

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if query.data == "help":
        await help_command(update, context)
    elif query.data == "faq":
        await faq(update, context)
    elif query.data == "platforms":
        await platforms_info(update, context)
    elif query.data == "music":
        await music_search_info(update, context)
    elif query.data == "contacts":
        await contacts(update, context)
    elif query.data == "about":
        await about_command(update, context)
    elif query.data == "main_menu":
        await main_menu(update, context)
    
    await query.answer()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            f"⚠️ Произошла ошибка.\n\n"
            f"Пожалуйста, попробуйте позже или свяжитесь с поддержкой:\n"
            f"📞 @barbosick89\n\n"
            f"📌 {BOT_NAME} v{BOT_VERSION}"
        )

# ========== ЗАПУСК БОТА ==========
def run_bot():
    logger.info(f"🤖 Запуск {BOT_NAME} v{BOT_VERSION}...")
    
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_error_handler(error_handler)
        
        application.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        sys.exit(1)

# ========== ГЛАВНЫЙ ЗАПУСК ==========
if __name__ == "__main__":
    logger.info(f"🚀 ЗАПУСК {BOT_NAME} v{BOT_VERSION} НА RENDER")
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    time.sleep(2)
    run_bot()
