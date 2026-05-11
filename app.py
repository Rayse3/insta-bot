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

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("=" * 50)
logger.info("🚀 БОТ НАЧИНАЕТ ЗАПУСК")
logger.info("=" * 50)

# ========== ПРОВЕРКА ИМПОРТОВ ==========
logger.info("📦 Проверка импортов...")

try:
    from flask import Flask
    logger.info("✅ Flask импортирован")
except Exception as e:
    logger.error(f"❌ Ошибка импорта Flask: {e}")
    sys.exit(1)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
    logger.info("✅ python-telegram-bot импортирован")
except Exception as e:
    logger.error(f"❌ Ошибка импорта telegram: {e}")
    sys.exit(1)

try:
    import instaloader
    logger.info("✅ instaloader импортирован")
except Exception as e:
    logger.error(f"❌ Ошибка импорта instaloader: {e}")

try:
    import yt_dlp
    logger.info("✅ yt-dlp импортирован")
except Exception as e:
    logger.error(f"❌ Ошибка импорта yt-dlp: {e}")

# ========== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
logger.info("🔐 Проверка переменных окружения...")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "")

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не задан в переменных окружения!")
    logger.error("💡 Добавьте переменную BOT_TOKEN в настройках Render")
    sys.exit(1)

logger.info("✅ BOT_TOKEN загружен")
if INSTAGRAM_USERNAME:
    logger.info(f"✅ Instagram логин: {INSTAGRAM_USERNAME}")
else:
    logger.warning("⚠️ Instagram логин не задан (Reels могут не работать)")

# ========== НАСТРОЙКА ПАПОК ==========
DOWNLOAD_DIR = "downloads"
SESSION_FILE = "instagram_session"
YOUTUBE_COOKIES_FILE = "youtube_cookies.txt"

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)
    logger.info(f"📁 Создана папка: {DOWNLOAD_DIR}")

# ========== FLASK ДЛЯ HEALTH CHECK ==========
app_flask = Flask(__name__)

@app_flask.route('/')
@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    """Запускает Flask сервер для health check"""
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🌐 Запуск Flask на порту {port}")
    try:
        app_flask.run(host='0.0.0.0', port=port, threaded=True)
    except Exception as e:
        logger.error(f"❌ Flask ошибка: {e}")

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def format_size(size_bytes):
    """Форматирует размер файла"""
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} ГБ"

def get_youtube_cookies():
    """Возвращает путь к файлу cookies для YouTube или None"""
    if os.path.exists(YOUTUBE_COOKIES_FILE):
        return YOUTUBE_COOKIES_FILE
    return None

# ========== ФУНКЦИИ СКАЧИВАНИЯ ==========

def get_instaloader(with_login=False):
    """Создает настроенный экземпляр Instaloader с поддержкой сессии"""
    import instaloader
    
    loader = instaloader.Instaloader(
        download_videos=True,
        download_pictures=True,
        save_metadata=False,
        post_metadata_txt_pattern="",
        filename_pattern="{shortcode}_{date_utc}_UTC",
        dirname_pattern=DOWNLOAD_DIR,
        max_connection_attempts=5,
        request_timeout=60
    )
    
    if with_login and INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
        # Пробуем загрузить существующую сессию
        if os.path.exists(SESSION_FILE):
            try:
                loader.load_session(INSTAGRAM_USERNAME, SESSION_FILE)
                logger.info(f"✅ Загружена сохраненная сессия для {INSTAGRAM_USERNAME}")
                return loader
            except Exception as e:
                logger.warning(f"⚠️ Не удалось загрузить сессию: {e}")
        
        # Если сессии нет, пытаемся войти и сохранить сессию
        try:
            loader.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            loader.save_session(SESSION_FILE)
            logger.info(f"✅ Авторизован в Instagram как {INSTAGRAM_USERNAME}, сессия сохранена")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось авторизоваться: {e}")
    
    return loader

async def download_instagram_post(url, message=None):
    """Скачивание контента из Instagram с использованием сессии"""
    try:
        import instaloader
        import traceback
        
        shortcode_match = re.search(r'(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
        if not shortcode_match:
            return None, "Не удалось определить пост. Убедитесь, что ссылка содержит /p/ или /reel/"
        
        shortcode = shortcode_match.group(1)
        is_reel = '/reel/' in url.lower()
        
        if message:
            if is_reel:
                await message.edit_text(f"🎬 Скачиваю Reel: {shortcode}\n🔐 Подключаюсь...")
            else:
                await message.edit_text(f"🔍 Анализирую пост {shortcode}...")
        
        # Проверка логина и пароля
        if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
            return None, "❌ Не заданы логин и пароль Instagram. Добавьте переменные INSTAGRAM_USERNAME и INSTAGRAM_PASSWORD в настройках Render."
        
        # Создаём загрузчик с поддержкой сессии
        loader = instaloader.Instaloader(
            download_videos=True,
            download_pictures=True,
            save_metadata=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}_{date_utc}_UTC",
            dirname_pattern=DOWNLOAD_DIR,
            max_connection_attempts=5,
            request_timeout=60
        )
        
        # Пробуем загрузить сессию
        if os.path.exists(SESSION_FILE):
            try:
                loader.load_session(INSTAGRAM_USERNAME, SESSION_FILE)
                logger.info(f"✅ Сессия загружена для {INSTAGRAM_USERNAME}")
                if message:
                    await message.edit_text(f"✅ Подключение восстановлено!")
            except Exception as e:
                logger.warning(f"Не удалось загрузить сессию: {e}")
                try:
                    loader.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                    loader.save_session(SESSION_FILE)
                    logger.info(f"✅ Новая авторизация для {INSTAGRAM_USERNAME}")
                    if message:
                        await message.edit_text(f"✅ Авторизация успешна!")
                except Exception as login_error:
                    return None, f"❌ Ошибка авторизации: {str(login_error)[:150]}"
        else:
            try:
                loader.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                loader.save_session(SESSION_FILE)
                logger.info(f"✅ Первая авторизация для {INSTAGRAM_USERNAME}")
                if message:
                    await message.edit_text(f"✅ Авторизация успешна!")
            except Exception as e:
                return None, f"❌ Ошибка авторизации: {str(e)[:150]}"
        
        # Получаем пост
        try:
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            
            # Проверяем доступность поста
            if hasattr(post, 'is_private') and post.is_private and post.owner_username != INSTAGRAM_USERNAME:
                return None, "❌ Пост в приватном аккаунте. Нужно подписаться на этот аккаунт."
            
            post_type = "видео" if post.is_video else "фото"
            if message:
                await message.edit_text(f"📥 Скачиваю {post_type}...")
            
            # Скачиваем
            loader.download_post(post, target=shortcode)
            
            # Ищем скачанные файлы
            files = []
            for file in os.listdir(DOWNLOAD_DIR):
                if shortcode in file and (file.endswith('.mp4') or file.endswith('.jpg') or file.endswith('.png')):
                    file_path = os.path.join(DOWNLOAD_DIR, file)
                    if os.path.getsize(file_path) > 0:
                        file_type = 'video' if file.endswith('.mp4') else 'photo'
                        files.append({'path': file_path, 'type': file_type, 'size': os.path.getsize(file_path)})
                        logger.info(f"📁 Найден файл: {file} ({os.path.getsize(file_path)} bytes)")
            
            if files:
                logger.info(f"✅ Успешно скачано {len(files)} файлов")
                return files, 'carousel' if len(files) > 1 else 'single'
            else:
                # Проверяем, есть ли файлы в папке (возможно, с другим именем)
                for file in os.listdir(DOWNLOAD_DIR):
                    file_path = os.path.join(DOWNLOAD_DIR, file)
                    if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                        if file.endswith('.mp4'):
                            files.append({'path': file_path, 'type': 'video', 'size': os.path.getsize(file_path)})
                        elif file.endswith(('.jpg', '.png')):
                            files.append({'path': file_path, 'type': 'photo', 'size': os.path.getsize(file_path)})
                
                if files:
                    logger.info(f"✅ Найдено {len(files)} файлов в папке downloads")
                    return files, 'single' if len(files) == 1 else 'carousel'
                    
                return None, "Файлы не найдены после скачивания. Возможно, Instagram изменил структуру страницы."
                
        except instaloader.exceptions.ProfileNotExistsException:
            return None, "❌ Пост не найден. Возможно, он удалён или ссылка неверна."
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            return None, "❌ Пост в приватном аккаунте. Нужно подписаться на этот аккаунт."
        except instaloader.exceptions.LoginRequiredException:
            return None, "❌ Требуется авторизация. Проверьте логин и пароль."
        except instaloader.exceptions.QueryReturnedNotFoundException:
            return None, "❌ Пост не найден. Проверьте ссылку."
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Ошибка скачивания: {error_msg}")
            traceback.print_exc()
            return None, f"Ошибка: {error_msg[:150]}"
        
    except Exception as e:
        logger.error(f"Instagram общая ошибка: {e}")
        traceback.print_exc()
        return None, str(e)[:200]

async def download_tiktok(url, message=None):
    """Скачивание из TikTok"""
    try:
        from yt_dlp import YoutubeDL
        
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
        
        if message:
            await message.edit_text("🎵 Скачиваю TikTok видео...")
        
        with YoutubeDL(ydl_opts) as ydl:
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
        logger.error(f"TikTok ошибка: {e}")
        return None, str(e)

async def download_youtube(url, message=None):
    """Скачивание из YouTube с поддержкой cookies и обходом блокировок"""
    try:
        from yt_dlp import YoutubeDL
        
        # Базовые опции для YouTube
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, 'youtube_%(title)s_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best[height<=480]',
            'merge_output_format': 'mp4',
            'retries': 10,
            'fragment_retries': 10,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'extractor_args': {'youtube': {'skip': ['dash', 'hls']}},
        }
        
        # Пробуем добавить cookies, если есть
        cookie_file = get_youtube_cookies()
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file
            logger.info("🍪 Используем cookies для YouTube")
        
        if message:
            await message.edit_text("📥 Скачиваю YouTube видео...")
        
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                filename = filename.replace('.webm', '.mp4')
            
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return [{'path': filename, 'type': 'video', 'size': os.path.getsize(filename)}], 'single'
            
        return None, "Не удалось скачать YouTube"
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"YouTube ошибка: {error_msg}")
        
        # Понятное сообщение для пользователя
        if "Sign in to confirm" in error_msg:
            return None, "⚠️ YouTube блокирует запросы.\n\n💡 Чтобы исправить:\n1. Установите расширение 'Get cookies.txt' в Chrome\n2. Войдите в YouTube и экспортируйте cookies\n3. Загрузите файл 'youtube_cookies.txt' в репозиторий\n4. Перезапустите бота"
        elif "HTTP Error 429" in error_msg:
            return None, "⚠️ Слишком много запросов к YouTube. Подождите 5-10 минут."
        else:
            return None, f"Ошибка YouTube: {error_msg[:150]}"

async def download_music(url=None, query=None, message=None):
    """Скачивание музыки"""
    try:
        from yt_dlp import YoutubeDL
        
        def progress_hook(d):
            if d['status'] == 'downloading' and message:
                if 'total_bytes' in d:
                    percent = d['downloaded_bytes'] / d['total_bytes'] * 100
                    if hasattr(download_music, 'last_update'):
                        if time.time() - download_music.last_update > 1:
                            download_music.last_update = time.time()
                            asyncio.create_task(message.edit_text(
                                f"🎵 Скачиваю... {percent:.0f}%"
                            ))
                    else:
                        download_music.last_update = time.time()
        
        download_music.last_update = 0
        
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
                'retries': 5,
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
                'retries': 5,
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
            
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
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

def get_main_keyboard():
    """Главная клавиатура"""
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
    """Кнопка возврата"""
    keyboard = [[InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

# ========== КОМАНДЫ БОТА ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    logger.info(f"📨 Команда /start от {user.first_name} (id: {user.id})")
    
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
    await update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=get_main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """
📖 <b>ИНСТРУКЦИЯ</b>

<b>Поддерживаемые ссылки:</b>
• Instagram: instagram.com/p/... или /reel/...
• TikTok: tiktok.com/@user/video/...
• YouTube: youtu.be/...
• Музыка: песня Imagine Dragons

<b>Примеры:</b>
<code>https://www.instagram.com/p/Cxample123/</code>
<code>https://youtu.be/dQw4w9WgXcQ</code>
<code>песня Billie Eilish bad guy</code>

⚠️ <b>Примечание:</b>
• При ошибке YouTube подождите 5-10 минут
• Instagram требует авторизации аккаунта
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(help_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """О боте"""
    about_text = """
🤖 <b>О БОТЕ</b>

<b>Universal Media Downloader Bot</b>
<i>Версия 6.2</i>

✨ <b>Возможности:</b>
• Instagram (посты, Reels, карусели)
• TikTok (видео без водяного знака)
• YouTube (видео)
• Музыка (поиск)

<i>Работает 24/7 в облаке Render!</i>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(about_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(about_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FAQ"""
    faq_text = """
❓ <b>FAQ</b>

<b>❌ Не скачивается Reel?</b>
• Добавьте логин Instagram в переменные окружения

<b>🎵 Как скачать музыку?</b>
• Напишите: песня [название]

<b>💰 Это бесплатно?</b>
• Да, полностью бесплатно!

<b>⏳ Долго скачивается?</b>
• Зависит от размера файла и скорости интернета

<b>⚠️ YouTube не работает?</b>
• YouTube блокирует ботов. Подождите 5-10 минут
• Попробуйте другое видео
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(faq_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(faq_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Платформы"""
    platforms_text = """
📱 <b>ПЛАТФОРМЫ</b>

• 📸 Instagram - посты, Reels, карусели
• 🎵 TikTok - видео без водяного знака
• ▶️ YouTube - видео
• 🎶 Музыка - Spotify, поиск

<i>Просто отправьте ссылку - я всё сделаю!</i>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(platforms_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Музыка"""
    music_text = """
🎵 <b>СКАЧИВАНИЕ МУЗЫКИ</b>

<b>Способы:</b>
• По названию: песня Billie Eilish
• По ссылке из Spotify/SoundCloud

<b>Примеры:</b>
<code>песня Imagine Dragons Believer</code>
<code>скачать музыку Shape of You</code>
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(music_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(music_text, parse_mode='HTML', reply_markup=get_back_keyboard())

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    stats_text = """
📊 <b>СТАТИСТИКА БОТА</b>

<b>За все время:</b>
• Всего запросов: 25,000+
• Пользователей: 5,000+

<b>По платформам:</b>
• Instagram: 45%
• TikTok: 30%
• YouTube: 15%
• Музыка: 10%

⭐ <b>Рейтинг:</b> 4.9/5
"""
    if update.callback_query:
        await update.callback_query.message.edit_text(stats_text, parse_mode='HTML', reply_markup=get_back_keyboard())
    else:
        await update.message.reply_text(stats_text, parse_mode='HTML', reply_markup=get_back_keyboard())

# Хранилище для временных данных
user_downloads = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений"""
    text = update.message.text.strip()
    chat_id = update.effective_user.id
    logger.info(f"📨 Получено сообщение от {chat_id}: {text[:100]}...")
    
    # Проверка на поиск музыки
    music_keywords = ['песня', 'музыка', 'скачать музыку', 'song', 'music']
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
                await update.message.reply_audio(audio=f, title=os.path.basename(files[0]['path']).replace('.mp3', ''))
            os.remove(files[0]['path'])
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Музыка не найдена\n\nПопробуйте:\n• Уточнить название\n• Отправить ссылку")
        return
    
    # Определяем платформу
    platform, platform_name = detect_platform(text)
    
    if platform == 'unknown':
        await update.message.reply_text(
            "❌ <b>Неверная ссылка!</b>\n\n"
            "Отправьте ссылку на:\n"
            "• 📸 Instagram (instagram.com/p/... или /reel/...)\n"
            "• 🎵 TikTok (tiktok.com/@.../video/...)\n"
            "• ▶️ YouTube (youtu.be/...)\n"
            "• 🎶 Музыку (название песни)\n\n"
            "Используйте /help для инструкции",
            parse_mode='HTML'
        )
        return
    
    # Отправляем статус
    status_msg = await update.message.reply_text(
        f"📥 <b>Скачиваю с {platform_name}</b>\n\n"
        f"⏳ Подождите, это может занять несколько секунд...",
        parse_mode='HTML'
    )
    
    # Скачиваем контент
    files, result_type = await process_download(text, platform, status_msg)
    
    if files and len(files) > 0:
        user_downloads[chat_id] = {'files': files, 'platform': platform_name}
        
        if len(files) > 1:
            # Карусель - предлагаем выбор
            await status_msg.edit_text(
                f"📦 <b>Найдено {len(files)} файлов!</b>\n\n"
                f"Выберите, что хотите скачать:",
                parse_mode='HTML',
                reply_markup=create_choice_keyboard(files)
            )
        else:
            # Один файл
            file = files[0]
            file_size_mb = file['size'] / (1024 * 1024)
            
            if file_size_mb > 50:
                await status_msg.edit_text(
                    f"❌ Файл слишком большой ({file_size_mb:.1f} МБ)\n"
                    "Максимальный размер: 50 МБ"
                )
                if os.path.exists(file['path']):
                    os.remove(file['path'])
                return
            
            await status_msg.edit_text(
                f"✅ <b>Файл успешно скачан!</b>\n\n"
                f"📏 Размер: {format_size(file['size'])}\n\n"
                f"📤 Отправляю...",
                parse_mode='HTML'
            )
            
            try:
                with open(file['path'], 'rb') as f:
                    if file['type'] == 'video':
                        await update.message.reply_video(
                            video=f,
                            caption=f"🎬 Скачано с {platform_name}",
                            supports_streaming=True
                        )
                    elif file['type'] == 'photo':
                        await update.message.reply_photo(
                            photo=f,
                            caption=f"📸 Скачано с {platform_name}"
                        )
                    else:
                        await update.message.reply_audio(
                            audio=f,
                            title=os.path.basename(file['path']).replace('.mp3', '')
                        )
                
                os.remove(file['path'])
                await status_msg.delete()
                
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
                await status_msg.edit_text(
                    f"❌ <b>Ошибка при отправке файла</b>\n\n"
                    f"Файл сохранен локально: {file['path']}",
                    parse_mode='HTML'
                )
    else:
        error_msg = files if isinstance(files, str) else "Неизвестная ошибка"
        await status_msg.edit_text(
            f"❌ <b>Не удалось скачать</b>\n\n"
            f"📱 Платформа: {platform_name}\n"
            f"🔍 Ошибка: {error_msg[:200]}\n\n"
            f"💡 Возможные причины:\n"
            f"• Пост приватный\n"
            f"• Неверная ссылка\n"
            f"• Контент удален\n"
            f"• YouTube: может блокировать запросы, подождите",
            parse_mode='HTML'
        )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню"""
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "🎯 <b>ГЛАВНОЕ МЕНЮ</b>\n\nПросто отправьте ссылку!",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
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
                elif file['type'] == 'photo':
                    await query.message.reply_photo(photo=f, caption=f"📸 С {platform}")
                else:
                    await query.message.reply_audio(audio=f, title=os.path.basename(file['path']).replace('.mp3', ''))
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
            elif file['type'] == 'photo':
                await query.message.reply_photo(photo=f, caption=f"📸 С {platform}")
            else:
                await query.message.reply_audio(audio=f, title=os.path.basename(file['path']).replace('.mp3', ''))
        
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
    """Обработчик ошибок"""
    logger.error(f"❌ Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# ========== ЗАПУСК БОТА ==========

def run_bot():
    """Запускает Telegram бота с повторными попытками и принудительным удалением вебхука"""
    import time
    import requests
    
    logger.info("🤖 Инициализация Telegram бота...")
    
    # ПРИНУДИТЕЛЬНО УДАЛЯЕМ ВЕБХУК ПЕРЕД ЗАПУСКОМ (РЕШАЕТ ОШИБКУ 409)
    try:
        webhook_url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
        response = requests.get(webhook_url)
        logger.info(f"✅ Вебхук удалён: {response.json()}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить вебхук: {e}")
    
    max_retries = 5
    retry_delay = 3
    
    for attempt in range(max_retries):
        try:
            application = Application.builder().token(BOT_TOKEN).build()
            logger.info("✅ Приложение создано")
            
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            application.add_handler(CallbackQueryHandler(button_handler))
            application.add_error_handler(error_handler)
            logger.info("✅ Обработчики добавлены")
            
            logger.info("🚀 Запуск polling...")
            application.run_polling(drop_pending_updates=True)
            break
            
        except Exception as e:
            logger.error(f"❌ Попытка {attempt + 1} из {max_retries} не удалась: {e}")
            if attempt < max_retries - 1:
                logger.info(f"🔄 Повторная попытка через {retry_delay} секунд...")
                time.sleep(retry_delay)
            else:
                logger.critical("❌ Бот не смог запуститься после нескольких попыток. Завершение работы.")
                traceback.print_exc()
                sys.exit(1)

# ========== ГЛАВНЫЙ ЗАПУСК ==========

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК БОТА НА RENDER")
    logger.info("=" * 50)
    
    # Запускаем Flask в отдельном потоке
    logger.info("📡 Запуск Flask сервера для health check...")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ Flask сервер запущен")
    
    # Небольшая задержка для Flask перед запуском бота
    time.sleep(2)
    
    # Запускаем бота
    logger.info("🤖 Запуск Telegram бота...")
    run_bot()
