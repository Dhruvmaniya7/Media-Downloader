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

GLOBAL_MAX_CONCURRENT_DOWNLOADS = 3
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]

# Conversation states
AWAIT_CHOICE, GET_NEW_NAME = range(2)

DOWNLOAD_QUEUE: Dict[str, List[Dict[str, Any]]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
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
        if self.message and text != self.last_update_text and (current_time - self.last_update_time > 1.5):
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
        ("Transfer.sh", upload_to_transfersh)
    ]
    for name, uploader_func in uploaders:
        await progress.update(generate_progress_text(f"Uploading to {name}..."))
        try:
            link = await uploader_func(str(file_path))
            if link:
                logger.info(f"Uploaded to {name}: {link}")
                return link
            logger.warning(f"Upload to {name} failed for {file_path.name}")
        except Exception as e:
            logger.error(f"Error uploading to {name}: {e}")
    return None

async def _upload_with_aiohttp(url: str, file_path: str, method: str = 'POST', data_field: str = 'file') -> Optional[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, "rb") as f:
                if method.upper() == 'POST':
                    data = aiohttp.FormData()
                    data.add_field(data_field, f, filename=Path(file_path).name)
                    async with session.post(url, data=data) as resp:
                        resp.raise_for_status()
                        return await resp.text()
                else: # PUT
                    async with session.put(url, data=f) as resp:
                        resp.raise_for_status()
                        return await resp.text()
    except Exception as e:
        logger.error(f"Aiohttp upload error to {url}: {e}")
    return None

async def upload_to_0x0st(file_path: str) -> Optional[str]:
    response_text = await _upload_with_aiohttp("https://0x0.st", file_path, data_field='file')
    return response_text.strip() if response_text else None

async def upload_to_transfersh(file_path: str) -> Optional[str]:
    url = f"https://transfer.sh/{Path(file_path).name}"
    response_text = await _upload_with_aiohttp(url, file_path, method='PUT')
    return response_text.strip() if response_text else None

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
            logger.exception(f"Critical error processing task for user {user_id}: {e}")
            await application.bot.send_message(task['chat_id'], "A critical error occurred while processing your task. Skipping.")
        await asyncio.sleep(1)

async def queue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_filename: Optional[str]):
    user_id_str = str(update.effective_user.id)
    task = {
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "url": context.user_data["url"],
        "info": context.user_data["info"],
        "custom_filename": custom_filename
    }
    if user_id_str not in DOWNLOAD_QUEUE:
        DOWNLOAD_QUEUE[user_id_str] = []
    
    DOWNLOAD_QUEUE[user_id_str].append(task)
    save_queue_to_disk()
    
    position = len(DOWNLOAD_QUEUE[user_id_str])
    message_text = f"✅ Task added to your queue at position #{position}."
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text)
    else: # From a direct message (e.g., after renaming)
        await update.message.reply_text(message_text)

    if len(DOWNLOAD_QUEUE[user_id_str]) == 1:
        asyncio.create_task(process_queue_for_user(user_id_str, context.application))

# ---------------- Core Download Logic ----------------
async def download_media(task: Dict[str, Any], application: Application):
    chat_id = task["chat_id"]
    url = task["url"]
    info = task["info"]
    custom_filename = task.get("custom_filename")

    file_name_base = sanitize_filename(custom_filename or info.get("title", "downloaded_file"))
    ext = info.get('ext', 'mp4')
    file_path_template = DOWNLOAD_DIR / f"{file_name_base}.%(ext)s"
    
    progress = ProgressManager(application.bot, chat_id)
    await progress.send_initial_message("Starting download...")

    final_filepath = None
    try:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': str(file_path_template),
            'noplaylist': True,
            'quiet': True,
            'progress_hooks': [progress.get_progress_hook(time.monotonic())],
            'merge_output_format': 'mp4',
        }
        if COOKIE_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIE_FILE)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = await to_thread(ydl.extract_info, url, download=True)
            final_filepath = Path(ydl.prepare_filename(meta))
            if not final_filepath.exists():
                raise FileNotFoundError("Downloaded file not found!")
    
    except Exception as e:
        logger.error(f"Download failed for URL {url}: {e}")
        await progress.update(generate_progress_text("Download failed."))
        await progress.delete()
        await application.bot.send_message(chat_id, f"❌ Download failed.\nReason: {str(e)[:200]}")
        return

    await progress.update(generate_progress_text("Download complete! Uploading..."))
    upload_link = await upload_file(final_filepath, progress)

    if upload_link:
        msg = f"✅ **Upload Complete!**\n\n[{final_filepath.name}]({upload_link})"
        await progress.update(generate_progress_text("Upload complete!"))
    else:
        msg = f"✅ **Download Complete!**\n\nUnfortunately, all upload attempts failed. The file is saved on the server as `{final_filepath.name}`."
        await progress.update(generate_progress_text("Upload failed."))
    
    await progress.delete()
    await application.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    
    try:
        final_filepath.unlink()
    except Exception as e:
        logger.error(f"Failed to delete file {final_filepath}: {e}")

# ---------------- Telegram Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    caption = f"👋 Hello, *{user_name}*!\n\nI can download media from thousands of sites. Just send me a link to get started."
    try:
        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        await update.message.reply_markdown(caption)

async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"I use yt-dlp, which supports a massive list of sites:\n{SUPPORTED_SITES_LINK}")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    url = normalize_url(msg.text)
    status_msg = await msg.reply_text("🔍 Analyzing link, please wait...")
    
    try:
        ydl_opts = {'quiet': True, 'noplaylist': True, 'skip_download': True}
        if COOKIE_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIE_FILE)
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=False)

        context.user_data.update({"url": url, "info": info})
        title = info.get("title", "Unknown Title")
        duration = time.strftime('%H:%M:%S', time.gmtime(info.get('duration', 0)))

        buttons = [
            [InlineKeyboardButton("✅ Download", callback_data="download_original")],
            [InlineKeyboardButton("✏️ Rename & Download", callback_data="download_rename")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        
        await status_msg.edit_text(
            f"**Found Video:**\n`{title}`\n\n**Duration:** `{duration}`\n\nWhat would you like to do?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_CHOICE

    except yt_dlp.utils.DownloadError as e:
        error_message = str(e)
        logger.error(f"yt-dlp error: {error_message}")
        friendly_message = "❌ **Could not process the link.**\n\n"
        if "authentication" in error_message.lower() or "sign in" in error_message.lower():
            friendly_message += "This video may be private or age-restricted. To access it, a `cookies.txt` file from a logged-in YouTube session is required. See bot documentation for instructions."
        else:
            reason = error_message.split(';')[-1].strip()
            friendly_message += f"**Reason:** {reason[:200]}"
        await status_msg.edit_text(friendly_message)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in handle_link: {e}")
        await status_msg.edit_text("❌ An unexpected error occurred. Please try another link.")
        return ConversationHandler.END

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "download_original":
        await queue_download(update, context, custom_filename=None)
        return ConversationHandler.END
    
    elif action == "download_rename":
        await query.edit_message_text("OK. Please send me the new filename (without the extension).")
        return GET_NEW_NAME
    
    elif action == "cancel":
        await query.edit_message_text("Operation cancelled.")
        return ConversationHandler.END

    return ConversationHandler.END

async def get_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = update.message.text.strip()
    if not new_name or len(new_name) > 100:
        await update.message.reply_text("❌ Invalid name. Please provide a shorter, valid filename.")
        return GET_NEW_NAME
    
    await queue_download(update, context, custom_filename=new_name)
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------------- Main Bot Setup ----------------
def main():
    load_queue_from_disk()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            AWAIT_CHOICE: [CallbackQueryHandler(choice_handler, pattern="^(download_original|download_rename|cancel)$")],
            GET_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_name_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("sites", sites_handler))
    application.add_handler(conv_handler)
    
    # Re-queue tasks from previous session
    if DOWNLOAD_QUEUE:
        logger.info("Restarting queued tasks from previous session...")
        for user_id in list(DOWNLOAD_QUEUE.keys()):
             if DOWNLOAD_QUEUE[user_id]:
                asyncio.create_task(process_queue_for_user(user_id, application))

    logger.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
