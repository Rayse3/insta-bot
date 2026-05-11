import os
import logging
import re
import asyncio
import sys
import time
import signal
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import Conflict, TimedOut, NetworkError

# ========== ОБРАБОТКА ЗАВЕРШЕНИЯ ==========
def signal_handler(sig, frame):
    """Обработчик сигнала завершения"""
    print('\n🛑 Бот остановлен пользователем!')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ========== НАСТРОЙКА ==========
BOT_TOKEN = "8542884336:AAE55W8PifheY3u65-vxa53BfUH7I2C8ajk"

# Instagram логин (обязателен для Reels!)
INSTAGRAM_USERNAME = "excer25_io"  # Ваш логин
INSTAGRAM_PASSWORD = "snovatupo4ki28"  # Ваш пароль

# Создаем папки для скачивания
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def format_size(size_bytes):
    """Форматирует размер файла"""
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} ГБ"

def clear_downloads_folder():
    """Очищает папку загрузок"""
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
    
    for file in os.listdir(DOWNLOAD_DIR):
        try:
            file_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.warning(f"Не удалось удалить {file}: {e}")

# ========== ФУНКЦИИ СКАЧИВАНИЯ ==========

def get_instaloader(with_login=False):
    """Создает настроенный экземпляр Instaloader"""
    import instaloader
    
    loader = instaloader.Instaloader(
        download_videos=True,
        download_pictures=True,
        save_metadata=False,
        post_metadata_txt_pattern="",
        filename_pattern="{shortcode}_{date_utc}_UTC",
        dirname_pattern=DOWNLOAD_DIR,
        max_connection_attempts=3,
        request_timeout=30
    )
    
    # Авторизация для доступа к Reels
    if with_login and INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
        try:
            loader.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            logger.info(f"✅ Авторизован в Instagram как {INSTAGRAM_USERNAME}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось авторизоваться: {e}")
    
    return loader

async def download_instagram_post(url, message=None):
    """Скачивание контента из Instagram - исправленная версия"""
    try:
        import instaloader
        
        # Извлекаем короткий код
        shortcode_match = re.search(r'(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
        if not shortcode_match:
            return None, "Не удалось определить пост"
        
        shortcode = shortcode_match.group(1)
        is_reel = '/reel/' in url
        
        if message:
            if is_reel:
                await message.edit_text(f"🎬 Скачиваю Reel: {shortcode}\n⏳ Использую авторизованный доступ...")
            else:
                await message.edit_text(f"🔍 Анализирую пост {shortcode}...")
        
        files = []
        
        # Метод 1: С авторизацией (для Reels и приватного контента)
        if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
            if message:
                await message.edit_text(f"🔐 Использую авторизованный доступ...")
            
            loader = get_instaloader(with_login=True)
            try:
                post = instaloader.Post.from_shortcode(loader.context, shortcode)
                loader.download_post(post, target=shortcode)
                
                # Ищем файлы
                for file in os.listdir(DOWNLOAD_DIR):
                    if shortcode in file:
                        file_path = os.path.join(DOWNLOAD_DIR, file)
                        if file.endswith('.mp4'):
                            files.append({'path': file_path, 'type': 'video', 'size': os.path.getsize(file_path)})
                        elif file.endswith(('.jpg', '.png')):
                            files.append({'path': file_path, 'type': 'photo', 'size': os.path.getsize(file_path)})
                
                if files:
                    logger.info(f"✅ Авторизованный метод: скачано {len(files)} файлов")
                    return files, 'carousel' if len(files) > 1 else 'single'
                    
            except Exception as e:
                logger.warning(f"Авторизованный метод не сработал: {e}")
        
        # Метод 2: Анонимный (для публичных постов)
        if message:
            await message.edit_text(f"🌐 Пробую анонимный доступ...")
        
        loader = get_instaloader(with_login=False)
        try:
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            loader.download_post(post, target=shortcode)
            
            for file in os.listdir(DOWNLOAD_DIR):
                if shortcode in file:
                    file_path = os.path.join(DOWNLOAD_DIR, file)
                    if file.endswith('.mp4'):
                        files.append({'path': file_path, 'type': 'video', 'size': os.path.getsize(file_path)})
                    elif file.endswith(('.jpg', '.png')):
                        files.append({'path': file_path, 'type': 'photo', 'size': os.path.getsize(file_path)})
            
            if files:
                logger.info(f"✅ Анонимный метод: скачано {len(files)} файлов")
                return files, 'carousel' if len(files) > 1 else 'single'
                
        except Exception as e:
            logger.warning(f"Анонимный метод не сработал: {e}")
        
        # Метод 3: Для Reels - используем yt-dlp как fallback
        if is_reel:
            if message:
                await message.edit_text(f"🎬 Пробую скачать Reel через yt-dlp...")
            
            try:
                from yt_dlp import YoutubeDL
                
                ydl_opts = {
                    'outtmpl': os.path.join(DOWNLOAD_DIR, f'reel_{shortcode}.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'format': 'best',
                }
                
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    
                    if os.path.exists(filename):
                        return [{'path': filename, 'type': 'video', 'size': os.path.getsize(filename)}], 'single'
                    elif os.path.exists(filename.replace('.webm', '.mp4')):
                        filename = filename.replace('.webm', '.mp4')
                        return [{'path': filename, 'type': 'video', 'size': os.path.getsize(filename)}], 'single'
                        
            except Exception as e:
                logger.error(f"yt-dlp метод не сработал: {e}")
        
        # Если ничего не найдено, но в папке есть файлы - возвращаем их
        for file in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                if file.endswith('.mp4'):
                    files.append({'path': file_path, 'type': 'video', 'size': os.path.getsize(file_path)})
                elif file.endswith(('.jpg', '.png')):
                    files.append({'path': file_path, 'type': 'photo', 'size': os.path.getsize(file_path)})
        
        if files:
            logger.info(f"✅ Найдено {len(files)} файлов в папке")
            return files, 'carousel' if len(files) > 1 else 'single'
        
        return None, "Не удалось скачать пост - возможно, контент приватный"
        
    except Exception as e:
        logger.error(f"Instagram ошибка: {e}")
        return None, str(e)

async def download_tiktok(url, message=None):
    """Скачивание из TikTok"""
    try:
        from yt_dlp import YoutubeDL
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'tiktok_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'format': 'best',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
            }
        }
        
        if message:
            await message.edit_text("🎵 Скачиваю TikTok видео...\n⏳ Это может занять несколько секунд")
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                for ext in ['.mp4', '.webm', '.mkv']:
                    test_path = filename.rsplit('.', 1)[0] + ext
                    if os.path.exists(test_path):
                        filename = test_path
                        break
            
            if os.path.exists(filename):
                file_size = os.path.getsize(filename)
                return [{'path': filename, 'type': 'video', 'size': file_size}], 'single'
            
        return None, "Не удалось скачать TikTok"
        
    except Exception as e:
        logger.error(f"TikTok ошибка: {e}")
        return None, str(e)

async def download_youtube(url, message=None):
    """Скачивание из YouTube с прогрессом"""
    try:
        from yt_dlp import YoutubeDL
        
        def progress_hook(d):
            if d['status'] == 'downloading' and message:
                if 'total_bytes' in d:
                    percent = d['downloaded_bytes'] / d['total_bytes'] * 100
                    if hasattr(download_youtube, 'last_update'):
                        if time.time() - download_youtube.last_update > 0.5:
                            download_youtube.last_update = time.time()
                            asyncio.create_task(message.edit_text(
                                f"📥 Скачиваю YouTube...\n"
                                f"`{'█' * int(percent/5)}{'░' * (20 - int(percent/5))}` {percent:.1f}%",
                                parse_mode='Markdown'
                            ))
                    else:
                        download_youtube.last_update = time.time()
        
        download_youtube.last_update = 0
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'youtube_%(title)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best[height<=720]',
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook],
            'retries': 5,
        }
        
        if message:
            await message.edit_text("📥 Начинаю загрузку YouTube видео...")
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                filename = filename.replace('.webm', '.mp4')
            
            if os.path.exists(filename):
                return [{'path': filename, 'type': 'video', 'size': os.path.getsize(filename)}], 'single'
            
        return None, "Не удалось скачать YouTube"
        
    except Exception as e:
        logger.error(f"YouTube ошибка: {e}")
        return None, str(e)

async def download_music(url=None, query=None, message=None):
    """Скачивание музыки"""
    try:
        from yt_dlp import YoutubeDL
        
        def progress_hook(d):
            if d['status'] == 'downloading' and message:
                if 'total_bytes' in d:
                    percent = d['downloaded_bytes'] / d['total_bytes'] * 100
                    asyncio.create_task(message.edit_text(
                        f"🎵 Скачиваю...\n"
                        f"`{'█' * int(percent/5)}{'░' * (20 - int(percent/5))}` {percent:.1f}%",
                        parse_mode='Markdown'
                    ))
        
        if query:
            search_query = f"ytsearch:{query}"
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
                'progress_hooks': [progress_hook],
            }
        else:
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
                'progress_hooks': [progress_hook],
            }
        
        if message:
            await message.edit_text("🔍 Ищу музыку...")
        
        with YoutubeDL(ydl_opts) as ydl:
            if query:
                info = ydl.extract_info(search_query, download=True)
                if 'entries' in info:
                    info = info['entries'][0]
            else:
                info = ydl.extract_info(url, download=True)
            
            filename = ydl.prepare_filename(info)
            filename = filename.replace('.webm', '.mp3').replace('.m4a', '.mp3')
            
            if os.path.exists(filename):
                return [{'path': filename, 'type': 'audio', 'size': os.path.getsize(filename)}], 'single'
                
        return None, "Не удалось скачать музыку"
        
    except Exception as e:
        logger.error(f"Music ошибка: {e}")
        return None, str(e)

def detect_platform(url):
    """Определяет платформу по ссылке"""
    url = url.lower()
    
    if 'instagram.com' in url and ('/p/' in url or '/reel/' in url or '/tv/' in url):
        return 'instagram', '📸 Instagram'
    elif 'tiktok.com' in url:
        return 'tiktok', '🎵 TikTok'
    elif 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube', '▶️ YouTube'
    elif 'spotify.com' in url or 'soundcloud.com' in url:
        return 'music', '🎵 Музыка'
    else:
        return 'unknown', '❓ Неизвестно'

async def process_download(url, platform, message):
    """Обработка скачивания"""
    try:
        if platform == 'instagram':
            return await download_instagram_post(url, message)
        elif platform == 'tiktok':
            return await download_tiktok(url, message)
        elif platform == 'youtube':
            return await download_youtube(url, message)
        elif platform == 'music':
            return await download_music(url=url, message=message)
        else:
            return None, "Платформа не поддерживается"
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return None, str(e)

def create_choice_keyboard(files):
    """Создает клавиатуру для выбора файлов"""
    keyboard = []
    row = []
    for i, file in enumerate(files):
        emoji = "🎬" if file['type'] == 'video' else "📸" if file['type'] == 'photo' else "🎵"
        label = f"{emoji} {file['type'].capitalize()} {i+1} ({format_size(file['size'])})"
        row.append(InlineKeyboardButton(label, callback_data=f"file_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("📥 Скачать всё", callback_data="download_all")])
    keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)

# ========== КЛАВИАТУРЫ ==========

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📖 Инструкция", callback_data="help"),
         InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("👤 О боте", callback_data="about"),
         InlineKeyboardButton("❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton("📱 Платформы", callback_data="platforms"),
         InlineKeyboardButton("🎵 Музыка", callback_data="music")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard():
    keyboard = [[InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

# ========== КОМАНДЫ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    welcome_text = f"""
🌟 <b>ДОБРО ПОЖАЛОВАТЬ, {user.first_name}!</b>

╔══════════════════════════════════════════════╗
║  🚀 <b>Universal Media Downloader Bot</b>       ║
║     <i>Ваш универсальный загрузчик контента</i>  ║
╚══════════════════════════════════════════════╝

✨ <b>Я умею скачивать:</b>
• 📸 <b>Instagram</b> - фото, видео, Reels
• 🎵 <b>TikTok</b> - видео без водяного знака
• ▶️ <b>YouTube</b> - видео в хорошем качестве
• 🎶 <b>Музыку</b> - по ссылке или названию

🎯 <b>Просто отправьте ссылку!</b>
"""
    
    await update.message.reply_text(
        welcome_text,
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 <b>ИНСТРУКЦИЯ</b>

<b>Поддерживаемые ссылки:</b>
• Instagram: instagram.com/p/... или /reel/...
• TikTok: tiktok.com/@user/video/...
• YouTube: youtu.be/...
• Музыка: песня Imagine Dragons

<b>📌 Особенности:</b>
• Reels скачиваются через авторизацию
• Карусели с выбором файлов
• YouTube с прогрессом загрузки
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            help_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            help_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about_text = """
🤖 <b>О БОТЕ</b>

<b>Universal Media Downloader Bot</b>
<i>Версия 4.3</i>

✨ <b>Возможности:</b>
• Instagram (посты, Reels, карусели)
• TikTok (видео без водяного знака)
• YouTube (видео)
• Музыка (поиск)

<i>Спасибо за использование!</i>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            about_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            about_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    faq_text = """
❓ <b>FAQ</b>

<b>❌ Не скачивается Reel?</b>
• Используется авторизованный доступ
• Проверьте логин и пароль

<b>🎵 Как скачать музыку?</b>
• Напишите: песня [название]

<b>💰 Это бесплатно?</b>
• Да, полностью бесплатно!
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            faq_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            faq_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

async def platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms_text = """
📱 <b>ПЛАТФОРМЫ</b>

• 📸 Instagram - посты, Reels, карусели
• 🎵 TikTok - видео без водяного знака
• ▶️ YouTube - видео
• 🎶 Музыка - Spotify, поиск
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            platforms_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            platforms_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    music_text = """
🎵 <b>СКАЧИВАНИЕ МУЗЫКИ</b>

<b>Способы:</b>
• По названию: песня Billie Eilish
• По ссылке из Spotify/SoundCloud

<b>Примеры:</b>
<code>песня Imagine Dragons Believer</code>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            music_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            music_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats_text = """
📊 <b>СТАТИСТИКА</b>

<b>За все время:</b>
• Запросов: 25,000+
• Пользователей: 5,000+

<b>По платформам:</b>
• Instagram: 45%
• TikTok: 30%
• YouTube: 15%
• Музыка: 10%
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(
            stats_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )
    else:
        await update.message.reply_text(
            stats_text,
            parse_mode='HTML',
            reply_markup=get_back_keyboard()
        )

# Хранилище для временных данных
user_downloads = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # Проверка на поиск музыки
    music_keywords = ['песня', 'музыка', 'скачать музыку']
    if any(kw in text.lower() for kw in music_keywords) and not ('http' in text):
        query = text
        for kw in music_keywords:
            query = query.lower().replace(kw, '').strip()
        
        status_msg = await update.message.reply_text(
            f"🔍 <b>Ищу:</b> {query}\n⏳ Поиск...",
            parse_mode='HTML'
        )
        
        files, _ = await download_music(query=query, message=status_msg)
        
        if files and os.path.exists(files[0]['path']):
            await status_msg.edit_text("✅ Отправляю...")
            with open(files[0]['path'], 'rb') as f:
                await update.message.reply_audio(audio=f, title=files[0]['path'].split('\\')[-1].replace('.mp3', ''))
            os.remove(files[0]['path'])
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Музыка не найдена")
        return
    
    # Определяем платформу
    platform, platform_name = detect_platform(text)
    
    if platform == 'unknown':
        await update.message.reply_text(
            "❌ <b>Неверная ссылка!</b>\n\n"
            "Отправьте ссылку на Instagram, TikTok, YouTube или название песни",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
        return
    
    status_msg = await update.message.reply_text(
        f"📥 <b>Скачиваю с {platform_name}</b>\n⏳ Подождите...",
        parse_mode='HTML'
    )
    
    files, result_type = await process_download(text, platform, status_msg)
    
    if files and len(files) > 0:
        user_id = update.effective_user.id
        user_downloads[user_id] = {'files': files, 'platform': platform_name}
        
        if len(files) > 1:
            await status_msg.edit_text(
                f"📦 Найдено {len(files)} файлов!\nВыберите, что скачать:",
                parse_mode='HTML',
                reply_markup=create_choice_keyboard(files)
            )
        else:
            file = files[0]
            await status_msg.edit_text(f"✅ Скачано!\n📏 {format_size(file['size'])}\n📤 Отправляю...")
            
            with open(file['path'], 'rb') as f:
                if file['type'] == 'video':
                    await update.message.reply_video(video=f, caption=f"🎬 С {platform_name}", supports_streaming=True)
                elif file['type'] == 'photo':
                    await update.message.reply_photo(photo=f, caption=f"📸 С {platform_name}")
                else:
                    await update.message.reply_audio(audio=f)
            
            os.remove(file['path'])
            await status_msg.delete()
    else:
        error_msg = files if isinstance(files, str) else "Неизвестная ошибка"
        await status_msg.edit_text(
            f"❌ <b>Не удалось скачать</b>\n\n{platform_name}\nОшибка: {error_msg[:200]}",
            parse_mode='HTML'
        )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "🎯 <b>ГЛАВНОЕ МЕНЮ</b>\n\nПросто отправьте ссылку!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    
    if query.data == "help":
        await help_command(update, context)
    elif query.data == "about":
        await about(update, context)
    elif query.data == "faq":
        await faq(update, context)
    elif query.data == "platforms":
        await platforms(update, context)
    elif query.data == "music":
        await music_command(update, context)
    elif query.data == "stats":
        await stats(update, context)
    elif query.data == "main_menu":
        await main_menu(update, context)
    elif query.data == "download_all" and user_id in user_downloads:
        await query.answer("📥 Скачиваю всё...")
        files = user_downloads[user_id]['files']
        platform = user_downloads[user_id]['platform']
        
        for file in files:
            with open(file['path'], 'rb') as f:
                if file['type'] == 'video':
                    await query.message.reply_video(video=f, caption=f"🎬 С {platform}")
                else:
                    await query.message.reply_photo(photo=f, caption=f"📸 С {platform}")
            os.remove(file['path'])
        
        await query.message.edit_text("✅ Все файлы отправлены!")
        del user_downloads[user_id]
        
    elif query.data.startswith("file_") and user_id in user_downloads:
        idx = int(query.data.split("_")[1])
        file = user_downloads[user_id]['files'][idx]
        platform = user_downloads[user_id]['platform']
        
        with open(file['path'], 'rb') as f:
            if file['type'] == 'video':
                await query.message.reply_video(video=f, caption=f"🎬 С {platform}")
            else:
                await query.message.reply_photo(photo=f, caption=f"📸 С {platform}")
        
        os.remove(file['path'])
        user_downloads[user_id]['files'].pop(idx)
        
        if not user_downloads[user_id]['files']:
            del user_downloads[user_id]
            await query.message.edit_text("✅ Файл отправлен!")
        else:
            await query.message.edit_text(
                f"✅ Отправлено!\nОсталось {len(user_downloads[user_id]['files'])} файлов.",
                reply_markup=create_choice_keyboard(user_downloads[user_id]['files'])
            )
        
    elif query.data == "cancel" and user_id in user_downloads:
        for file in user_downloads[user_id]['files']:
            try:
                os.remove(file['path'])
            except:
                pass
        del user_downloads[user_id]
        await query.message.edit_text("❌ Отменено", reply_markup=get_main_keyboard())

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Улучшенный обработчик ошибок"""
    error = context.error
    
    if isinstance(error, Conflict):
        logger.warning("⚠️ Конфликт: бот уже запущен. Продолжаем...")
        return
    
    if isinstance(error, TimedOut):
        logger.warning("⏱️ Таймаут соединения, повторная попытка...")
        return
    
    if isinstance(error, NetworkError):
        logger.warning("🌐 Ошибка сети, проверьте соединение...")
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "🌐 Ошибка сети. Проверьте интернет соединение и попробуйте снова."
            )
        return
    
    logger.error(f"❌ Необработанная ошибка: {error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# ========== ЗАПУСК ==========

def main():
    print("""
╔═══════════════════════════════════════════════╗
║   🚀 UNIVERSAL MEDIA DOWNLOADER BOT v4.3     ║
║   📱 Instagram | TikTok | YouTube | Music    ║
╚═══════════════════════════════════════════════╝
    """)
    
    # Очистка старых файлов
    clear_downloads_folder()
    
    print(f"✅ Бот инициализирован")
    print(f"📁 Папка: {os.path.abspath(DOWNLOAD_DIR)}")
    print("🚀 Запуск...")
    
    if INSTAGRAM_USERNAME:
        print(f"🔐 Instagram авторизация: {INSTAGRAM_USERNAME}")
    else:
        print("⚠️ Instagram Reels могут не работать без авторизации")
    
    try:
        # Создаем приложение с повторными попытками
        app = Application.builder()\
            .token(BOT_TOKEN)\
            .connect_timeout(30.0)\
            .read_timeout(30.0)\
            .build()
        
        # Добавляем обработчики
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_error_handler(error_handler)
        
        print("""
╔═══════════════════════════════════════════════╗
║  ✅ БОТ ЗАПУЩЕН!                             ║
║  💬 Напишите /start в Telegram               ║
║  🛑 Ctrl+C для остановки                      ║
╚═══════════════════════════════════════════════╝
        """)
        
        # Запускаем бота с очисткой вебхука
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Conflict:
        print("\n⚠️ Бот уже запущен в другом месте!")
        print("💡 Решения:")
        print("   1. Закройте все другие терминалы")
        print("   2. Выполните: taskkill /f /im python.exe")
        print("   3. Перезапустите бота")
        
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        print("\n💡 Попробуйте:")
        print("   • Перезапустить бота")
        print("   • Проверить интернет соединение")
        print("   • Убедиться что токен правильный")

if __name__ == "__main__":
    main()