#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - FINAL FULL VERSION
Author: Dhruv Maniya (shadow maniya)

Features:
- yt-dlp for downloads
- aiohttp for non-blocking uploads (0x0.st & gofile)
- Robust, rate-limited progress updates
- Optional rename (inline or /skip)
- Clear video quality selection with file sizes
- Per-user queue with JSON persistence (queue.json)
- Global concurrency limit with asyncio.Semaphore
- PicklePersistence for conversation/user_data state
- Auto-normalization for various YouTube link formats
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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
# Load Bot Token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable not set. Please set it before running.")

# --- Directories and Files ---
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_FILE = Path("queue.json")
PERSISTENCE_FILE = "bot_persistence.pkl"

# --- Bot Customization ---
CREATOR_NAME = "shadow maniya"
CONNECT_LINK = "https://www.linkedin.com/in/dhruv-maniya-shadow03"
WELCOME_IMAGE_URL = "https://i.ibb.co/bMNj87bT/download.jpg"
SUPPORTED_SITES_LINK = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"

# --- Limits & Constants ---
TELEGRAM_SAFE_MAX_BYTES = 49 * 1024 * 1024  # 49 MB
GLOBAL_MAX_CONCURRENT_DOWNLOADS = 3
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]

# --- Conversation States ---
CHOOSE_FORMAT, CHOOSE_QUALITY, ASK_RENAME, GET_NEW_NAME = range(4)

# --- Global In-memory State ---
DOWNLOAD_QUEUE: Dict[str, List[Dict[str, Any]]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT_DOWNLOADS)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ---------------- Utilities ----------------
def sanitize_filename(name: str) -> str:
    """Replace unsafe file characters and trim whitespace."""
    if not name:
        return ""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def format_elapsed(seconds: float) -> str:
    """Formats seconds into a human-readable H:M:S string."""
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def generate_progress_text(
    status_text: str,
    percent: Optional[float] = None,
    speed: Optional[str] = None,
    eta: Optional[str] = None,
    elapsed: Optional[str] = None,
) -> str:
    """Generates a formatted progress message with a spinner and progress bar."""
    spinner = SPINNER_FRAMES[int(time.time() * 10) % len(SPINNER_FRAMES)]
    text = f"`{spinner}` *{status_text}*\n\n"
    if percent is not None:
        filled = int(10 * (percent / 100))
        bar = "█" * filled + "░" * (10 - filled)
        text += f"`[{bar}] {percent:.1f}%`\n"
    if speed:
        text += f"`Speed:` {speed}\n"
    if eta:
        text += f"`ETA:` {eta}\n"
    if elapsed:
        text += f"`Time:` {elapsed}\n"
    return text


async def to_thread(func, *args, **kwargs):
    """Convenience wrapper for running blocking functions in a separate thread."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))


def normalize_url(url: str) -> str:
    """Normalizes various YouTube URL formats to a standard one."""
    url = url.strip()
    # Replace mobile/music domains with standard desktop domain
    url = url.replace("m.youtube.com", "youtube.com")
    url = url.replace("music.youtube.com", "youtube.com")
    # Convert short youtu.be links to full watch links
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    """Saves the current download queue to a JSON file."""
    try:
        serializable = {str(k): v for k, v in DOWNLOAD_QUEUE.items()}
        with QUEUE_FILE.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save queue to disk")


def load_queue_from_disk():
    """Loads the download queue from a JSON file on startup."""
    global DOWNLOAD_QUEUE
    if not QUEUE_FILE.exists():
        return
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as f:
            DOWNLOAD_QUEUE = {str(k): v for k, v in json.load(f).items()}
        logger.info(f"Loaded {sum(len(v) for v in DOWNLOAD_QUEUE.values())} tasks from queue.json")
    except Exception:
        logger.exception("Failed to load queue from disk")


# ---------------- Upload Helpers (aiohttp) ----------------
async def upload_file(file_path: Path) -> Optional[str]:
    """Uploads a file to a hosting service, trying Gofile first, then 0x0."""
    await asyncio.sleep(0.5) # Small delay before upload
    logger.info(f"Uploading {file_path.name} to cloud host...")
    
    # Try Gofile first
    gofile_link = await upload_to_gofile(str(file_path))
    if gofile_link:
        return gofile_link
    
    # Fallback to 0x0.st
    logger.warning("Gofile failed, falling back to 0x0.st")
    return await upload_to_0x0(str(file_path))


async def upload_to_gofile(file_path: str) -> Optional[str]:
    """Uploads a file to gofile.io."""
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post("https://store1.gofile.io/uploadFile", data=data, timeout=300) as resp:
                    resp_json = await resp.json()
                    return resp_json.get("data", {}).get("downloadPage")
    except Exception:
        logger.exception("Gofile upload failed")
        return None


async def upload_to_0x0(file_path: str) -> Optional[str]:
    """Uploads a file to 0x0.st."""
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post("https://0x0.st", data=data, timeout=120) as resp:
                    if resp.status == 200:
                        return (await resp.text()).strip()
                    return None
    except Exception:
        logger.exception("0x0.st upload failed")
        return None

# ---------------- Queue Operations ----------------
async def process_queue_for_user(user_id: str, application: Application):
    """Processes all tasks in a user's queue sequentially."""
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        try:
            # Wait for a free slot from the global semaphore
            async with DOWNLOAD_SEMAPHORE:
                logger.info(f"Starting download for user {user_id}: {task['url']}")
                await download_media(
                    chat_id=task["chat_id"],
                    url=task["url"],
                    format_choice=task["format_choice"],
                    quality_id=task["quality_id"],
                    custom_filename=task.get("custom_filename"),
                    context=application, # Pass the whole application object
                )
        except Exception:
            logger.exception(f"Error processing task for user {user_id}")
        await asyncio.sleep(1) # 1-second delay between tasks for a user

async def queue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_filename: Optional[str] = None):
    """Finalizes and adds a task to the user's download queue."""
    user_id_str = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    task = {
        "chat_id": chat_id,
        "url": context.user_data["url"],
        "format_choice": context.user_data["format_choice"],
        "quality_id": context.user_data["quality_id"],
        "custom_filename": custom_filename,
    }

    DOWNLOAD_QUEUE.setdefault(user_id_str, []).append(task)
    save_queue_to_disk()

    # If the queue was empty for this user, start the processing task
    if len(DOWNLOAD_QUEUE[user_id_str]) == 1:
        asyncio.create_task(process_queue_for_user(user_id_str, context.application))
    
    position = len(DOWNLOAD_QUEUE[user_id_str])
    await update.callback_query.edit_message_text(f"✅ Task added to your queue at position #{position}.")


# ---------------- Command and Conversation Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command with a welcome image and message."""
    user_name = update.effective_user.first_name or "User"
    caption = (
        f"👋 Hello, *{user_name}*!\n\n"
        "I am the Ultimate Media Downloader bot.\n"
        "Just send me a link from a supported site to get started.\n\n"
        "*Commands:*\n"
        "`/audio <url>` - Quick audio download\n"
        "`/video <url>` - Quick video download\n"
        "`/sites` - See all supported sites\n"
        "`/cancel` - Clear your download queue"
    )
    if WELCOME_IMAGE_URL:
        try:
            await update.message.reply_photo(
                photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN
            )
            return
        except Exception:
            logger.warning("Failed to send welcome photo. Falling back to text.")
    await update.message.reply_markdown(caption)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /help command."""
    await update.message.reply_text(
        "How to use:\n1. Send me a link.\n2. Choose video or audio.\n3. Select the quality.\n4. Choose to rename the file or keep the original name."
    )


async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /sites command."""
    await update.message.reply_text(
        f"This bot uses yt-dlp, which supports hundreds of websites.\n\n"
        f"You can find the full list here:\n{SUPPORTED_SITES_LINK}"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the conversation, handles an incoming link."""
    msg = update.message
    url = normalize_url(msg.text)
    status_msg = await msg.reply_text("🔍 Analyzing link...")
    try:
        ydl_opts = {'quiet': True, 'noplaylist': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=False)

        context.user_data.update({'url': url, 'info': info})

        title = info.get('title', 'Unknown Title')
        preview = f"*{title}*\n\nChoose your desired format:"
        buttons = [
            [
                InlineKeyboardButton("🎬 Video", callback_data='format|mp4'),
                InlineKeyboardButton("🎵 Audio", callback_data='format|mp3')
            ]
        ]
        
        await status_msg.delete()
        await msg.reply_markdown(preview, reply_markup=InlineKeyboardMarkup(buttons))
        return CHOOSE_FORMAT
    except Exception as e:
        logger.error(f"Failed to handle link {url}: {e}")
        await status_msg.edit_text("❌ Error: Could not process this link. It may be private, invalid, or from an unsupported site.")
        return ConversationHandler.END


async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice of format (Video/Audio)."""
    query = update.callback_query
    await query.answer()

    format_choice = query.data.split("|")[1]
    context.user_data["format_choice"] = format_choice
    info = context.user_data.get("info", {})

    if format_choice == 'mp3':
        # For audio, we don't need to ask for quality, use best available
        context.user_data['quality_id'] = 'bestaudio'
        buttons = [
            [
                InlineKeyboardButton("✏️ Rename File", callback_data='rename|yes'),
                InlineKeyboardButton("➡️ Keep Original Name", callback_data='rename|no')
            ]
        ]
        await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_RENAME

    # For video, generate a list of available qualities
    formats = info.get("formats", [])
    buttons = []
    seen_heights = set()

    for f in formats:
        height = f.get('height')
        # We only want video formats with decent codecs and a listed height
        if height and height not in seen_heights and f.get('vcodec') not in ('none', 'avc1.4d401e'):
            seen_heights.add(height)
            filesize = f.get('filesize') or f.get('filesize_approx')
            label = f"{height}p"
            if filesize:
                label += f" (~{filesize / (1024*1024):.1f} MB)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"quality|{f['format_id']}")])

    if not buttons: # Fallback if no specific formats found
        buttons.append([InlineKeyboardButton("Best Available", callback_data="quality|best")])
    
    # Sort buttons from highest to lowest quality
    buttons.sort(key=lambda b: int(re.search(r'(\d+)p', b[0].text).group(1)) if re.search(r'(\d+)p', b[0].text) else 0, reverse=True)

    await query.edit_message_text("Please select a video quality:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_QUALITY


async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice of video quality."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['quality_id'] = query.data.split("|")[1]
    
    buttons = [
        [
            InlineKeyboardButton("✏️ Rename File", callback_data='rename|yes'),
            InlineKeyboardButton("➡️ Keep Original Name", callback_data='rename|no')
        ]
    ]
    await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_RENAME


async def ask_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice to rename the file or not."""
    query = update.callback_query
    await query.answer()

    choice = query.data.split("|")[1]
    if choice == 'yes':
        await query.edit_message_text("Please send the new filename (without extension).")
        return GET_NEW_NAME
    else:
        # User chose not to rename, queue the download immediately
        await queue_download(update, context, custom_filename=None)
        return ConversationHandler.END


async def get_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the new filename from the user."""
    sanitized_name = sanitize_filename(update.message.text)
    
    # We need to get the original callback_query message to edit it
    # This is a bit of a workaround as we're now in a MessageHandler
    # A cleaner way would involve storing the message ID, but for now we reply.
    await update.message.reply_text(f"Filename set to: `{sanitized_name}`", parse_mode=ParseMode.MARKDOWN)

    # To maintain flow, we'll manually create a mock update object for queue_download
    mock_query = type('MockQuery', (), {'message': update.message, 'from_user': update.effective_user, 'edit_message_text': (lambda text: update.message.reply_text(text))})
    mock_update = type('MockUpdate', (), {'effective_user': update.effective_user, 'effective_chat': update.effective_chat, 'callback_query': mock_query})
    
    await queue_download(mock_update, context, custom_filename=sanitized_name)
    return ConversationHandler.END


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation and clears the user's queue."""
    user_id_str = str(update.effective_user.id)
    if DOWNLOAD_QUEUE.get(user_id_str):
        DOWNLOAD_QUEUE[user_id_str].clear()
        save_queue_to_disk()
        await update.message.reply_text("✅ Your download queue has been cleared.")
    else:
        await update.message.reply_text("You have no active downloads in your queue.")
    return ConversationHandler.END


# ---------------- Download Core Logic ----------------
async def download_media(
    chat_id: int,
    url: str,
    format_choice: str,
    quality_id: str,
    custom_filename: Optional[str],
    context: Application,
):
    """The main download function, with progress hooks and error handling."""
    status_msg = await context.bot.send_message(
        chat_id=chat_id, text=generate_progress_text("Initializing..."), parse_mode=ParseMode.MARKDOWN
    )
    start_time = time.monotonic()
    last_update_time = 0
    final_path = None

    # This hook is called from a separate thread by yt-dlp
    def progress_hook(d):
        nonlocal last_update_time
        now = time.time()
        if d['status'] == 'downloading' and now - last_update_time > 2.5: # Rate-limit updates
            last_update_time = now
            percent_str = d.get('_percent_str', '0%').replace('%', '').strip()
            try:
                percent = float(percent_str)
            except (ValueError, TypeError):
                percent = 0.0

            text = generate_progress_text(
                "Downloading",
                percent=percent,
                speed=d.get('_speed_str', 'N/A'),
                eta=d.get('_eta_str', 'N/A'),
                elapsed=format_elapsed(time.monotonic() - start_time)
            )
            # Safely schedule the async edit_text call on the main event loop
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                context.loop
            )

    # --- YDL Options ---
    output_template = str(DOWNLOAD_DIR / (f"{custom_filename}.%(ext)s" if custom_filename else "%(title)s.%(ext)s"))
    ydl_opts = {
        'noplaylist': True,
        'quiet': True,
        'progress_hooks': [progress_hook],
        'outtmpl': output_template,
        'retries': 2,
    }
    if format_choice == 'mp3':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else: # mp4
        ydl_opts['format'] = f"{quality_id}+bestaudio/best" if quality_id != 'best' else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        ydl_opts.setdefault('postprocessors', []).append({'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'})
    
    try:
        # --- Download Phase ---
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=True)
            final_path_str = ydl.prepare_filename(info)
            final_path = Path(final_path_str)
            # yt-dlp might not add mp3 extension after postprocessing
            if format_choice == 'mp3' and final_path.suffix != '.mp3':
                final_path = final_path.with_suffix('.mp3')

        if not final_path or not final_path.exists():
            raise FileNotFoundError("Downloaded file could not be found.")

        # --- Upload Phase ---
        await status_msg.edit_text(generate_progress_text("Uploading..."), parse_mode=ParseMode.MARKDOWN)
        file_size = final_path.stat().st_size
        
        if file_size <= TELEGRAM_SAFE_MAX_BYTES:
            with final_path.open("rb") as f:
                await context.bot.send_document(chat_id, document=f, filename=final_path.name)
            await status_msg.delete()
        else:
            link = await upload_file(final_path)
            if link:
                await context.bot.send_message(chat_id, f"✅ Upload complete!\n\nYour link is: {link}")
            else:
                await context.bot.send_message(chat_id, "❌ Upload failed after multiple attempts.")
            await status_msg.delete()
    
    except Exception as e:
        logger.exception(f"An error occurred during download/upload for URL {url}")
        error_message = f"❌ An error occurred: {str(e)[:200]}"
        try:
            await status_msg.edit_text(error_message)
        except Exception:
            await context.bot.send_message(chat_id, error_message)
    finally:
        # --- Cleanup Phase ---
        if final_path and final_path.exists():
            await to_thread(final_path.unlink)
            logger.info(f"Cleaned up file: {final_path.name}")


# ---------------- Application Bootstrap ----------------
def main():
    """Initializes and runs the bot."""
    load_queue_from_disk()
    
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    
    # The main conversation handler for the download process
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback, pattern=r"^format\|")],
            CHOOSE_QUALITY: [CallbackQueryHandler(choose_quality_callback, pattern=r"^quality\|")],
            ASK_RENAME: [CallbackQueryHandler(ask_rename_callback, pattern=r"^rename\|")],
            GET_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_name_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        conversation_timeout=600  # 10 minutes
    )
    
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("sites", sites_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(conv_handler)
    
    async def on_startup(app: Application):
        """Resume any queued tasks when the bot starts."""
        active_queues = [uid for uid, tasks in DOWNLOAD_QUEUE.items() if tasks]
        if active_queues:
            logger.info(f"Resuming queues for users: {', '.join(active_queues)}")
            for user_id in active_queues:
                asyncio.create_task(process_queue_for_user(user_id, app))
    
    application.post_init = on_startup
    
    logger.info("🚀 Bot is running!")
    application.run_polling()

if __name__ == "__main__":
    main()
