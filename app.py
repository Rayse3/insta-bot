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

# ========== INSTAGRAM ЧЕРЕЗ ПУБЛИЧНОЕ API ==========
async def download_instagram(url, message=None):
    """Скачивание Instagram через публичное API (без авторизации)"""
    try:
        # Извлекаем shortcode из URL
        shortcode_match = re.search(r'(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
        if not shortcode_match:
            return None, "Не удалось определить пост. Убедитесь, что ссылка содержит /p/ или /reel/"
        
        shortcode = shortcode_match.group(1)
        is_reel = '/reel/' in url.lower()
        
        if message:
            if is_reel:
                await message.edit_text(f"🎬 Скачиваю Instagram Reel...")
            else:
                await message.edit_text(f"📸 Скачиваю Instagram пост...")
        
        # Используем yt-dlp для Instagram (работает без авторизации для публичных постов)
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'instagram_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extract_flat': False,
            'retries': 5,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Проверяем существование файла
            if not os.path.exists(filename):
                for ext in ['.mp4', '.webm', '.mkv', '.jpg', '.png']:
                    test_path = filename.rsplit('.', 1)[0] + ext
                    if os.path.exists(test_path):
                        filename = test_path
                        break
            
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                file_type = 'video' if filename.endswith(('.mp4', '.webm', '.mkv')) else 'photo'
                return [{'path': filename, 'type': file_type, 'size': os.path.getsize(filename)}], 'single'
        
        return None, "Не удалось скачать Instagram контент. Убедитесь, что пост публичный."
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Instagram ошибка: {error_msg}")
        
        if "Private" in error_msg:
            return None, "❌ Этот аккаунт приватный. Instagram бот может скачивать только публичные посты."
        elif "login" in error_msg.lower():
            return None, "❌ Требуется авторизация. Сейчас поддерживаются только публичные посты."
        else:
            return None, f"Ошибка: {error_msg[:150]}"

# ========== СКАЧИВАНИЕ YOUTUBE ==========
async def download_youtube(url, message=None):
    """Скачивание YouTube видео (стабильно)"""
    try:
        if message:
            await message.edit_text("📥 Подготавливаю скачивание YouTube...")
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'youtube_%(title)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best[height<=720]',
            'merge_output_format': 'mp4',
            'retries': 10,
            'fragment_retries': 10,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extract_flat': False,
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
                
        return None, "Не удалось скачать видео"
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"YouTube ошибка: {error_msg}")
        
        if "Sign in to confirm" in error_msg:
            return None, "⚠️ YouTube требует подтверждения. Подождите 5-10 минут"
        elif "HTTP Error 429" in error_msg:
            return None, "⚠️ Слишком много запросов. Подождите 10-15 минут"
        else:
            return None, f"Ошибка: {error_msg[:150]}"

# ========== СКАЧИВАНИЕ TIKTOK ==========
async def download_tiktok(url, message=None):
    """Скачивание TikTok видео с обходом блокировки"""
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

# ========== ПОИСК И СКАЧИВАНИЕ МУЗЫКИ ==========
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
        logger.error(f"Music URL ошибка: {e}")
        return None, f"Ошибка: {str(e)[:150]}"

# ========== ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ ==========
def detect_platform(url):
    url_lower = url.lower()
    
    if 'instagram.com' in url_lower and ('/p/' in url_lower or '/reel/' in url_lower or '/tv/' in url_lower):
        return 'instagram', '📸 Instagram'
    elif 'tiktok.com' in url_lower:
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
        [InlineKeyboardButton("📞 Контакты", callback_data="contacts")]
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
║  🚀 <b>Universal Media Downloader Bot</b>       ║
║     <i>Ваш универсальный загрузчик контента</i>  ║
╚══════════════════════════════════════════════╝

✨ <b>Что умеет бот:</b>
• 📸 <b>Instagram</b> - публичные фото, видео, Reels
• ▶️ <b>YouTube</b> - видео (включая Shorts)
• 🎵 <b>TikTok</b> - видео без водяного знака
• 🎶 <b>Музыка</b> - поиск по названию или ссылка (SoundCloud, Spotify)

🎯 <b>Просто отправьте ссылку или напишите "песня Название"</b>

📞 <b>Контакты для связи:</b> @barbosick89
"""
    await update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=get_main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 <b>ИНСТРУКЦИЯ</b>

<b>🟢 Поддерживаемые платформы:</b>

• <b>Instagram</b> - публичные посты, Reels
  Пример: <code>https://www.instagram.com/p/xxxxx/</code>

• <b>YouTube</b> - любые видео
  Пример: <code>https://youtu.be/dQw4w9WgXcQ</code>

• <b>TikTok</b> - видео
  Пример: <code>https://www.tiktok.com/@user/video/123</code>

• <b>Музыка по названию</b>
  Пример: <code>песня Imagine Dragons Believer</code>

• <b>Музыка по ссылке</b> - SoundCloud/Spotify
  Пример: <code>https://soundcloud.com/artist/track</code>

⚠️ <b>Важно:</b>
• Instagram работает ТОЛЬКО с публичными аккаунтами
• При ошибке YouTube подождите 5-10 минут
• TikTok иногда блокирует - попробуйте позже
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def platforms_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms_text = """
📱 <b>ПОДДЕРЖИВАЕМЫЕ ПЛАТФОРМЫ</b>

✅ <b>Работают:</b>
• 📸 <b>Instagram</b> - публичные фото, видео, Reels
• ▶️ <b>YouTube</b> - все видео, включая Shorts
• 🎵 <b>TikTok</b> - видео без водяного знака
• 🎶 <b>Музыка</b> - поиск по названию (YouTube Music)
• 🎵 <b>SoundCloud</b> - прямая ссылка
• 🎧 <b>Spotify</b> - прямая ссылка (трек)

🔒 <b>Ограничения Instagram:</b>
• Только публичные аккаунты
• Приватные аккаунты не поддерживаются

💡 <b>Как скачать музыку:</b>
Напишите: <code>песня Название песни</code>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    faq_text = """
❓ <b>FAQ - ЧАСТЫЕ ВОПРОСЫ</b>

<b>❌ Почему не скачивается Instagram?</b>
• Убедитесь, что аккаунт публичный
• Проверьте правильность ссылки
• Instagram может блокировать запросы

<b>🎵 Как скачать музыку по названию?</b>
Напишите: <code>песня Название песни</code>

<b>⏳ Почему долго скачивается?</b>
Зависит от размера файла и скорости интернета

<b>⚠️ TikTok выдаёт ошибку?</b>
TikTok иногда блокирует запросы. Подождите 5-10 минут

<b>💰 Это бесплатно?</b>
Да, бот полностью бесплатный!

<b>🐛 Нашёл ошибку?</b>
Свяжитесь: @barbosick89
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
    contacts_text = """
📞 <b>КОНТАКТНАЯ ИНФОРМАЦИЯ</b>

🐛 <b>По вопросам и багам:</b>
• Telegram: <b>@barbosick89</b>

💡 <b>Обращаться по любым вопросам:</b>
• Ошибки в работе бота
• Предложения по улучшению
• Новые идеи для функционала

⏰ <b>Время ответа:</b>
Обычно в течение 24 часов

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
        "🎯 <b>ГЛАВНОЕ МЕНЮ</b>\n\nПросто отправьте ссылку или напишите 'песня Название'!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
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
                    performer="Universal Bot"
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
            "❌ <b>Неверная ссылка!</b>\n\n"
            "Поддерживаются:\n"
            "• 📸 Instagram (instagram.com/p/... или /reel/...)\n"
            "• ▶️ YouTube (youtu.be/... или youtube.com/...)\n"
            "• 🎵 TikTok (tiktok.com/@.../video/...)\n"
            "• 🎶 Музыка: напишите 'песня Название'\n"
            "• 🎵 SoundCloud/Spotify ссылки\n\n"
            "Используйте /help для инструкции",
            parse_mode='HTML'
        )
        return
    
    # Скачивание
    status_msg = await update.message.reply_text(
        f"📥 <b>Скачиваю с {platform_name}</b>\n\n⏳ Подождите...",
        parse_mode='HTML'
    )
    
    if platform == 'instagram':
        files, _ = await download_instagram(text, status_msg)
    elif platform == 'youtube':
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
                elif file['type'] == 'photo':
                    await update.message.reply_photo(photo=f, caption=f"📸 Скачано с {platform_name}")
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
            f"💡 Для Instagram работают только ПУБЛИЧНЫЕ аккаунты\n\n"
            f"📞 Свяжитесь с поддержкой: @barbosick89",
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
    elif query.data == "main_menu":
        await main_menu(update, context)
    
    await query.answer()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла ошибка.\n\n"
            "Пожалуйста, попробуйте позже или свяжитесь с поддержкой:\n"
            "📞 @barbosick89"
        )

# ========== ЗАПУСК БОТА ==========
def run_bot():
    logger.info("🤖 Запуск Telegram бота...")
    
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
    logger.info("🚀 ЗАПУСК БОТА НА RENDER")
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    time.sleep(2)
    run_bot()
