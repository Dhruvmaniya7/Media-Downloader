#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - FINAL, STABLE & GUARANTEED WORKING VERSION
Author: Dhruv Maniya (shadow maniya)

Features:
- yt-dlp with robust options, COOKIE SUPPORT, and FFmpeg error handling.
- Guaranteed, live, and animated progress bar updates visible to the user.
- aiohttp for reliable, non-blocking uploads.
- Per-user queue with JSON persistence.
- Global concurrency limit with asyncio.Semaphore.
- PicklePersistence for conversation state.
"""

import os
import re
import json
import time
import asyncio
import logging
import yt_dlp
import aiohttp
from pathlib import Path
from functools import partial
from typing import Dict, Any, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("FATAL ERROR: BOT_TOKEN environment variable not set.")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = Path("queue.json")
PERSISTENCE_FILE = "bot_persistence.pkl"
COOKIE_FILE = Path("cookies.txt")

SUPPORTED_SITES_LINK = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
WELCOME_IMAGE_URL = "https://i.ibb.co/bMNj87bT/download.jpg"

TELEGRAM_SAFE_MAX_BYTES = 49 * 1024 * 1024
GLOBAL_MAX_CONCURRENT_DOWNLOADS = 3
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⾾", "⣷", "⣯", "⣟", "⡿"]

CHOOSE_FORMAT, CHOOSE_QUALITY, ASK_RENAME, GET_NEW_NAME = range(4)

DOWNLOAD_QUEUE: Dict[str, List[Dict[str, Any]]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------- Utilities ----------------
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name or "").strip()

def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def generate_progress_text(status_text: str, percent: Optional[float] = None, speed: Optional[str] = None, eta: Optional[str] = None, elapsed: Optional[str] = None) -> str:
    spinner = SPINNER_FRAMES[int(time.time() * 10) % len(SPINNER_FRAMES)]
    text = f"`{spinner}` *{status_text}*\n\n"
    if percent is not None:
        filled = int(10 * (percent / 100))
        bar = "█" * filled + "░" * (10 - filled)
        text += f"`[{bar}] {percent:.1f}%`\n"
    if speed: text += f"`Speed:` {speed}\n"
    if eta: text += f"`ETA:` {eta}\n"
    if elapsed: text += f"`Time:` {elapsed}\n"
    return text

async def to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(partial(func, *args, **kwargs))

def normalize_url(url: str) -> str:
    url = url.strip().replace("m.youtube.com", "www.youtube.com").replace("music.youtube.com", "www.youtube.com")
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


# ---------------- Robust Progress Manager ----------------
class ProgressManager:
    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message: Optional[Message] = None
        self.last_update_text = ""
        self.last_update_time = 0
        self.loop = asyncio.get_running_loop()

    async def send_initial_message(self):
        initial_text = generate_progress_text("Initializing...")
        self.message = await self.bot.send_message(self.chat_id, initial_text, parse_mode=ParseMode.MARKDOWN)
        self.last_update_text = initial_text

    def _update_message_threadsafe(self, text: str):
        if self.message and text != self.last_update_text:
            self.last_update_text = text
            asyncio.run_coroutine_threadsafe(
                self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                self.loop
            )

    async def update(self, text: str):
        if self.message and text != self.last_update_text:
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                self.last_update_text = text
            except TelegramError as e:
                logger.warning(f"Failed to edit progress message: {e}")

    async def delete(self):
        if self.message:
            try: await self.message.delete()
            except TelegramError: pass
        self.message = None

    def get_progress_hook(self, start_time: float):
        def progress_hook(d):
            now = time.time()
            if d['status'] == 'downloading' and now - self.last_update_time > 2.0:
                self.last_update_time = now
                percent = float(d.get('_percent_str', '0%').replace('%', '').strip() or 0)
                text = generate_progress_text(
                    "Downloading...", percent, d.get('_speed_str'), d.get('_eta_str'), format_elapsed(now - start_time)
                )
                self._update_message_threadsafe(text)
            elif d['status'] == 'finished':
                text = generate_progress_text("Processing file...")
                self._update_message_threadsafe(text)
        return progress_hook

# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    try:
        with QUEUE_FILE.open("w", encoding="utf-8") as f: json.dump({str(k): v for k, v in DOWNLOAD_QUEUE.items()}, f, indent=2)
    except Exception: logger.exception("Failed to save queue")

def load_queue_from_disk():
    global DOWNLOAD_QUEUE
    if QUEUE_FILE.exists():
        try:
            with QUEUE_FILE.open("r", encoding="utf-8") as f: DOWNLOAD_QUEUE = {str(k): v for k, v in json.load(f).items()}
            logger.info(f"Loaded {sum(len(v) for v in DOWNLOAD_QUEUE.values())} tasks from queue.json")
        except Exception: logger.exception("Failed to load queue")

# ---------------- Upload Helpers ----------------
async def upload_file(file_path: Path) -> Optional[str]:
    logger.info(f"Uploading {file_path.name}...")
    link = await upload_to_gofile(str(file_path))
    if link: return link
    logger.warning("Gofile upload failed.")
    return None

async def upload_to_gofile(file_path: str) -> Optional[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post("https://store1.gofile.io/uploadFile", data=data) as resp:
                    resp.raise_for_status()
                    return (await resp.json()).get("data", {}).get("downloadPage")
    except Exception as e:
        logger.exception(f"Gofile upload failed: {e}")
        return None

# ---------------- Queue Operations ----------------
async def process_queue_for_user(user_id: str, application: Application):
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        try:
            async with DOWNLOAD_SEMAPHORE:
                logger.info(f"Starting download for user {user_id}: {task['url']}")
                await download_media(task=task, application=application)
        except Exception: logger.exception(f"Critical error in processing task for user {user_id}")
        await asyncio.sleep(1)

async def queue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_filename: Optional[str]):
    user_id_str = str(update.effective_user.id)
    task = {"chat_id": update.effective_chat.id, "url": context.user_data["url"], "format_choice": context.user_data["format_choice"], "quality_id": context.user_data["quality_id"], "custom_filename": custom_filename}
    DOWNLOAD_QUEUE.setdefault(user_id_str, []).append(task)
    save_queue_to_disk()
    if len(DOWNLOAD_QUEUE[user_id_str]) == 1:
        asyncio.create_task(process_queue_for_user(user_id_str, context.application))
    
    position = len(DOWNLOAD_QUEUE[user_id_str])
    message_text = f"✅ Task added to your queue at position #{position}."
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text)
    else:
        await update.message.reply_text(message_text)

# ---------------- Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "User"
    caption = (f"👋 Hello, *{user_name}*!\n\nSend me a link to get started.\n\n*Commands:*\n`/sites`\n`/cancel`")
    try: await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except Exception: await update.message.reply_markdown(caption)

async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Full list of supported sites:\n{SUPPORTED_SITES_LINK}")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    url = normalize_url(msg.text)
    status_msg = await msg.reply_text("🔍 Analyzing link...")
    
    ydl_opts = {'quiet': True, 'noplaylist': True, 'skip_download': True}
    if COOKIE_FILE.exists(): ydl_opts['cookiefile'] = str(COOKIE_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=False)

        context.user_data.update({'url': url, 'info': info})
        title = info.get('title', 'Unknown Title')
        buttons = [[InlineKeyboardButton("🎬 Video", callback_data='format|mp4'), InlineKeyboardButton("🎵 Audio", callback_data='format|mp3')]]
        await status_msg.delete()
        await msg.reply_markdown(f"*{title}*\n\nChoose your desired format:", reply_markup=InlineKeyboardMarkup(buttons))
        return CHOOSE_FORMAT
    except Exception as e:
        logger.error(f"Failed to handle link {url}: {e}")
        error_text = "❌ Error: Could not process link."
        if "confirm you’re not a bot" in str(e): error_text += "\n\nThis video may require a login. The bot's cookie file could be invalid."
        await status_msg.edit_text(error_text)
        return ConversationHandler.END

async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["format_choice"] = query.data.split("|")[1]
    
    if context.user_data["format_choice"] == 'mp3':
        context.user_data['quality_id'] = 'bestaudio'
        buttons = [[InlineKeyboardButton("✏️ Rename", callback_data='rename|yes'), InlineKeyboardButton("➡️ Keep Name", callback_data='rename|no')]]
        await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_RENAME

    info, buttons, seen_heights = context.user_data.get("info", {}), [], set()
    for f in info.get("formats", []):
        height = f.get('height')
        if height and height not in seen_heights and f.get('vcodec', 'none') != 'none':
            seen_heights.add(height)
            filesize = f.get('filesize') or f.get('filesize_approx')
            label = f"{height}p" + (f" (~{filesize / (1024*1024):.1f} MB)" if filesize else "")
            buttons.append([InlineKeyboardButton(label, callback_data=f"quality|{f['format_id']}")])

    if not buttons: buttons.append([InlineKeyboardButton("Best Available", callback_data="quality|best")])
    buttons.sort(key=lambda b: int(re.search(r'(\d+)p', b[0].text).group(1)) if re.search(r'(\d+)p', b[0].text) else 0, reverse=True)
    await query.edit_message_text("Please select a video quality:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_QUALITY

async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['quality_id'] = query.data.split("|")[1]
    buttons = [[InlineKeyboardButton("✏️ Rename", callback_data='rename|yes'), InlineKeyboardButton("➡️ Keep Name", callback_data='rename|no')]]
    await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_RENAME

async def ask_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.split("|")[1] == 'yes':
        await query.edit_message_text("Please send the new filename (without extension).")
        return GET_NEW_NAME
    else:
        await queue_download(update, context, custom_filename=None)
        return ConversationHandler.END

async def get_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await queue_download(update, context, custom_filename=sanitize_filename(update.message.text))
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id_str = str(update.effective_user.id)
    if DOWNLOAD_QUEUE.get(user_id_str):
        DOWNLOAD_QUEUE[user_id_str].clear()
        save_queue_to_disk()
        await update.message.reply_text("✅ Your download queue has been cleared.")
    else:
        await update.message.reply_text("You have no active downloads in your queue.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------------- Download Core Logic ----------------
async def download_media(task: Dict[str, Any], application: Application):
    chat_id, url = task['chat_id'], task['url']
    progress = ProgressManager(application.bot, chat_id)
    await progress.send_initial_message()
    
    final_path = None
    try:
        start_time = time.monotonic()
        ydl_opts = {
            'noplaylist': True, 'quiet': True, 'progress_hooks': [progress.get_progress_hook(start_time)],
            'outtmpl': str(DOWNLOAD_DIR / (f"{task['custom_filename']}.%(ext)s" if task['custom_filename'] else "%(title)s.%(ext)s")),
            'retries': 3, 'fragment_retries': 3,
            'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
            'postprocessor_hooks': [lambda d: logger.info(f"FFmpeg: {d.get('msg','').strip()}") if d['status'] == 'started' else None],
            'ignoreerrors': 'only_download', # CRITICAL FIX for FFmpeg "errors"
        }
        if COOKIE_FILE.exists(): ydl_opts['cookiefile'] = str(COOKIE_FILE)
        
        if task['format_choice'] == 'mp3':
            ydl_opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]})
        else:
            ydl_opts['format'] = f"{task['quality_id']}+bestaudio/best" if task['quality_id'] != 'best' else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            ydl_opts.setdefault('postprocessors', []).append({'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'})
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=True)
            final_path = Path(ydl.prepare_filename(info))
            if task['format_choice'] == 'mp3' and final_path.suffix != '.mp3':
                final_path = final_path.with_suffix('.mp3')

        if not final_path or not final_path.exists(): raise FileNotFoundError("Downloaded file not found.")

        await progress.update(generate_progress_text("Uploading..."))
        if final_path.stat().st_size <= TELEGRAM_SAFE_MAX_BYTES:
            with final_path.open("rb") as f:
                await application.bot.send_document(chat_id, document=f, filename=final_path.name)
        else:
            link = await upload_file(final_path)
            await application.bot.send_message(chat_id, f"✅ Upload complete!\n\nLink: {link}" if link else "❌ Upload failed.")
        await progress.delete()
    
    except Exception as e:
        error_message = f"❌ Download failed. Error: {str(e)[:200]}"
        logger.exception(f"Error for URL {url}")
        await progress.update(error_message)
    finally:
        if final_path and final_path.exists():
            await to_thread(final_path.unlink)
            logger.info(f"Cleaned up file: {final_path.name}")

# ---------------- Application Bootstrap ----------------
def main():
    load_queue_from_disk()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback, pattern=r"^format\|")],
            CHOOSE_QUALITY: [CallbackQueryHandler(choose_quality_callback, pattern=r"^quality\|")],
            ASK_RENAME: [CallbackQueryHandler(ask_rename_callback, pattern=r"^rename\|")],
            GET_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_name_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        conversation_timeout=600
    )
    
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("sites", sites_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(conv_handler)
    
    async def on_startup(app: Application):
        if any(DOWNLOAD_QUEUE.values()):
            logger.info(f"Resuming queues for users: {', '.join([uid for uid, tasks in DOWNLOAD_QUEUE.items() if tasks])}")
            for user_id in list(DOWNLOAD_QUEUE.keys()):
                if DOWNLOAD_QUEUE[user_id]:
                    asyncio.create_task(process_queue_for_user(user_id, app))
    
    application.post_init = on_startup
    
    logger.info("🚀 Bot is running!")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
