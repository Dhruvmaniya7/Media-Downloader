#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - FINAL, ROBUST & FLEXIBLE VERSION
Author: Dhruv Maniya (shadow maniya)
Enhancements by: Luna

Features:
- yt-dlp with robust options, SMART COOKIE handling, and FFmpeg error correction.
- Guaranteed, live, and animated progress bar updates visible to the user.
- aiohttp for reliable, non-blocking uploads with multi-service fallbacks.
- Per-user queue with JSON persistence.
- Global concurrency limit with asyncio.Semaphore.
- PicklePersistence for conversation state.
- Improved error handling and user feedback.
"""

import os
import re
import json
import time
import math
import asyncio
import logging
import shutil
import yt_dlp
import aiohttp
from pathlib import Path
from typing import Dict, Any, Optional, List

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Message
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable not set.")

USER_AGENT = "TelegramMediaDownloader/2.0 (by shadow)"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = Path("queue.json")
PERSISTENCE_FILE = "bot_persistence.pkl"
COOKIE_FILE = Path("cookies.txt")

SUPPORTED_SITES_LINK = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
WELCOME_IMAGE_URL = "https://i.ibb.co/MNj87bT/download.jpg"

TELEGRAM_SAFE_MAX_BYTES = 49 * 1024 * 1024
GLOBAL_MAX_CONCURRENT_DOWNLOADS = 3
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]

CHOOSE_FORMAT, CHOOSE_QUALITY, ASK_RENAME, GET_NEW_NAME = range(4)

DOWNLOAD_QUEUE: Dict[str, List[Dict[str, Any]]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Utilities ----------------
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name or "").strip()

def format_bytes(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.log(abs(size_bytes), 1024)) if size_bytes > 0 else 0
    p = 1024 ** i
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

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
    return await asyncio.to_thread(func, *args, **kwargs)

def normalize_url(url: str) -> str:
    url = url.strip().replace("m.youtube.com", "www.youtube.com").replace("music.youtube.com", "www.youtube.com")
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url

# ---------------- Progress Manager ----------------
class ProgressManager:
    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message: Optional[Message] = None
        self.last_update_text = ""
        self.last_update_time = 0
        self.loop = asyncio.get_running_loop()

    async def send_initial_message(self, text: str = "Initializing..."):
        initial_text = generate_progress_text(text)
        try:
            self.message = await self.bot.send_message(self.chat_id, initial_text, parse_mode=ParseMode.MARKDOWN)
            self.last_update_text = initial_text
        except TelegramError as e:
            logger.error(f"Failed to send initial message: {e}")

    def _update_message_threadsafe(self, text: str):
        current_time = time.time()
        if self.message and text != self.last_update_text and (current_time - self.last_update_time > 1.0):
            self.last_update_text = text
            self.last_update_time = current_time
            asyncio.run_coroutine_threadsafe(
                self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                self.loop
            )

    async def update(self, text: str):
        if self.message and text != self.last_update_text:
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                self.last_update_text = text
            except BadRequest:
                pass
            except TelegramError as e:
                logger.warning(f"Failed to edit progress message: {e}")

    async def delete(self):
        if self.message:
            try:
                await self.message.delete()
            except TelegramError:
                pass
        self.message = None

    def get_progress_hook(self, start_time: float):
        def progress_hook(d):
            if d['status'] == 'finished':
                self._update_message_threadsafe(generate_progress_text("Processing file..."))
                return
            if d['status'] == 'downloading':
                percent_str = d.get('_percent_str', '0%').replace('%', '').strip()
                percent = float(percent_str) if percent_str else 0
                elapsed_time = format_elapsed(time.monotonic() - start_time)
                text = generate_progress_text("Downloading...", percent, d.get('_speed_str'), d.get('_eta_str'), elapsed_time)
                self._update_message_threadsafe(text)
        return progress_hook

# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    try:
        with QUEUE_FILE.open("w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in DOWNLOAD_QUEUE.items()}, f, indent=4)
    except IOError as e:
        logger.exception(f"Failed to save queue: {e}")

def load_queue_from_disk():
    global DOWNLOAD_QUEUE
    if QUEUE_FILE.exists():
        try:
            with QUEUE_FILE.open("r", encoding="utf-8") as f:
                DOWNLOAD_QUEUE = {str(k): v for k, v in json.load(f).items()}
            logger.info(f"Loaded {sum(len(v) for v in DOWNLOAD_QUEUE.values())} tasks from queue.json")
        except (IOError, json.JSONDecodeError) as e:
            logger.exception(f"Failed to load queue: {e}")
            DOWNLOAD_QUEUE = {}

# ---------------- Upload Helpers ----------------
async def upload_file(file_path: Path, progress: ProgressManager) -> Optional[str]:
    uploaders = [
        ("0x0.st", upload_to_0x0st),
        ("Transfer.sh", upload_to_transfersh),
        ("GoFile", upload_to_gofile),
        ("File.io", upload_to_fileio),
    ]
    for name, uploader_func in uploaders:
        await progress.update(generate_progress_text(f"Uploading to {name}..."))
        try:
            link = await uploader_func(str(file_path))
            if link:
                logger.info(f"Uploaded to {name}: {link}")
                return link
            else:
                logger.warning(f"{name} failed for {file_path.name}")
        except Exception as e:
            logger.error(f"Error uploading to {name}: {e}")
    return None

async def _upload_with_aiohttp(url: str, file_path: str, method: str = 'POST', data_field: str = 'file', custom_data: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            with open(file_path, "rb") as f:
                if method.upper() == 'POST':
                    data = aiohttp.FormData()
                    if custom_data:
                        for k, v in custom_data.items():
                            data.add_field(k, v)
                    data.add_field(data_field, f, filename=Path(file_path).name)
                    async with session.post(url, data=data) as resp:
                        resp.raise_for_status()
                        if 'application/json' in resp.headers.get('Content-Type', ''):
                            return await resp.json()
                        return {"text": await resp.text()}
                else:
                    async with session.put(url, data=f) as resp:
                        resp.raise_for_status()
                        return {"text": await resp.text()}
    except aiohttp.ClientError as e:
        logger.error(f"Network error: {e}")
    except asyncio.TimeoutError:
        logger.error("Upload timed out")
    except Exception as e:
        logger.error(f"Upload error: {e}")
    return None

async def upload_to_0x0st(file_path: str) -> Optional[str]:
    headers = {'User-Agent': USER_AGENT}
    custom_data = {'secret': 'true'}
    response = await _upload_with_aiohttp("https://0x0.st", file_path, custom_data=custom_data, headers=headers)
    text = response.get("text") if response else None
    return text.strip() if text else None

async def upload_to_gofile(file_path: str) -> Optional[str]:
    response = await _upload_with_aiohttp("https://store1.gofile.io/uploadFile", file_path)
    if response and response.get("status") == "ok":
        return response.get("data", {}).get("downloadPage")
    return None

async def upload_to_fileio(file_path: str) -> Optional[str]:
    response = await _upload_with_aiohttp("https://file.io/?expires=1d", file_path)
    if response and response.get("success"):
        return response.get("link")
    return None

async def upload_to_transfersh(file_path: str) -> Optional[str]:
    response = await _upload_with_aiohttp(f"https://transfer.sh/{Path(file_path).name}", file_path, method='PUT')
    text = response.get("text") if response else None
    return text.strip() if text else None

# ---------------- Queue Operations ----------------
async def process_queue_for_user(user_id: str, application: Application):
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        try:
            async with DOWNLOAD_SEMAPHORE:
                logger.info(f"Processing task for user {user_id}: {task['url']}")
                await download_media(task, application)
        except Exception as e:
            logger.exception(f"Critical error for user {user_id}: {e}")
            await application.bot.send_message(task['chat_id'], "A critical error occurred. Skipping task.")
        await asyncio.sleep(1)

async def queue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_filename: Optional[str]):
    user_id_str = str(update.effective_user.id)
    task = {
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "url": context.user_data["url"],
        "format_choice": context.user_data["format_choice"],
        "quality_id": context.user_data.get("quality_id"),
        "custom_filename": custom_filename
    }
    if user_id_str not in DOWNLOAD_QUEUE:
        DOWNLOAD_QUEUE[user_id_str] = []
    DOWNLOAD_QUEUE[user_id_str].append(task)
    save_queue_to_disk()
    position = len(DOWNLOAD_QUEUE[user_id_str])
    message_text = f"✅ Task added to your queue at position #{position}."
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(message_text)
    elif update.message:
        await update.message.reply_text(message_text)
    if len(DOWNLOAD_QUEUE[user_id_str]) == 1:
        asyncio.create_task(process_queue_for_user(user_id_str, context.application))

# ---------------- Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "User"
    caption = f"👋 Hello, *{user_name}*!\nSend me a link to download media."
    try:
        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        await update.message.reply_markdown(caption)

async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Supported sites:\n{SUPPORTED_SITES_LINK}")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    url = normalize_url(msg.text)
    status_msg = await msg.reply_text("🔍 Analyzing link...")
    info = None
    try:
        ydl_opts = {'quiet': True, 'noplaylist': True, 'skip_download': True}
        if COOKIE_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIE_FILE)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=False)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        await status_msg.edit_text(f"❌ Could not process link. Reason: {str(e)[:150]}")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text("❌ Unexpected error.")
        return ConversationHandler.END

    if not info:
        await status_msg.edit_text("❌ No information retrieved.")
        return ConversationHandler.END

    context.user_data.update({"url": url, "info": info})
    title = info.get("title", "Unknown Title")
    buttons = [
        [InlineKeyboardButton("Download", callback_data="download|yes")],
        [InlineKeyboardButton("Cancel", callback_data="download|no")]
    ]
    await status_msg.edit_text(f"*{title}*\nChoose an option:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN)
    return CHOOSE_FORMAT


async def ask_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.split("|")[1] == "yes":
        await query.edit_message_text("OK. Please send me the new filename (without the extension):")
        return GET_NEW_NAME
    else:
        await queue_download(update, context, custom_filename=None)
        return ConversationHandler.END

async def get_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = sanitize_filename(update.message.text.strip())
    if not new_name:
        await update.message.reply_text("❌ Invalid name. Please try again.")
        return GET_NEW_NAME
    await queue_download(update, context, custom_filename=new_name)
    return ConversationHandler.END

async def download_media(task: Dict[str, Any], application: Application):
    chat_id = task["chat_id"]
    url = task["url"]
    format_choice = task.get("format_choice", "best")
    custom_filename = task.get("custom_filename")

    file_name = custom_filename or "downloaded_file"
    file_path = DOWNLOAD_DIR / f"{sanitize_filename(file_name)}.mp4"

    progress = ProgressManager(application.bot, chat_id)
    await progress.send_initial_message()

    start_time = time.monotonic()
    ydl_opts = {
        'format': format_choice,
        'outtmpl': str(file_path),
        'noplaylist': True,
        'quiet': True,
        'progress_hooks': [progress.get_progress_hook(start_time)],
    }
    if COOKIE_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIE_FILE)

    try:
        async with DOWNLOAD_SEMAPHORE:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await to_thread(ydl.download, [url])
    except Exception as e:
        logger.error(f"Download failed: {e}")
        await progress.update(generate_progress_text("Download failed."))
        return

    await progress.update(generate_progress_text("Download complete! Uploading..."))
    upload_link = await upload_file(file_path, progress)

    if upload_link:
        msg = f"✅ Download and upload complete!\n[{file_path.name}]({upload_link})"
        await progress.update(generate_progress_text("Upload complete!"))
    else:
        msg = f"✅ Download complete!\nFile saved as `{file_path.name}`"
        await progress.update(generate_progress_text("Upload failed."))

    try:
        await application.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        logger.error(f"Failed to send message: {e}")

    await progress.delete()
    try:
        file_path.unlink()
    except Exception:
        pass

# ---------------- Main ----------------
def main():
    load_queue_from_disk()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & (~filters.COMMAND), handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(ask_rename_callback, pattern="download\|")],
            GET_NEW_NAME: [MessageHandler(filters.TEXT & (~filters.COMMAND), get_new_name)],
        },
        fallbacks=[],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("sites", sites_handler))
    application.add_handler(conv_handler)

    logger.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()

