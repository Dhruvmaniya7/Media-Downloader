#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - ENHANCED, FLEXIBLE & ROBUST VERSION
Author: Dhruv Maniya (shadow maniya)
Enhancements by: Luna

Features:
- yt-dlp with robust options, SMART COOKIE handling, and FFmpeg error correction.
- Guaranteed, live, and animated progress bar updates visible to the user.
- aiohttp for reliable, non-blocking uploads with multi-service fallbacks (GoFile, File.io, Transfer.sh).
- Per-user queue with JSON persistence.
- Global concurrency limit with asyncio.Semaphore.
- PicklePersistence for conversation state.
- **IMPROVED**: Granular error handling for better user feedback.
- **IMPROVED**: More flexible if/else logic for format selection and file processing.
"""

import os
import re
import json
import time
import asyncio
import logging
import shutil
import yt_dlp
import math
import aiohttp
from pathlib import Path
from functools import partial
from typing import Dict, Any, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
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
    """Removes invalid characters from a filename."""
    return re.sub(r'[\\/*?:"<>|]', "_", name or "").strip()

def format_bytes(size_bytes: int) -> str:
    """Formats bytes into a human-readable string (KB, MB, GB)."""
    if not size_bytes or size_bytes <= 0: # Added a check for zero/negative
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    # THE FIX IS HERE: Use math.log(number, base)
    i = int(math.log(abs(size_bytes), 1024))
    p = 1024 ** i
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"


def format_elapsed(seconds: float) -> str:
    """Formats elapsed seconds into a human-readable string (h, m, s)."""
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def generate_progress_text(status_text: str, percent: Optional[float] = None, speed: Optional[str] = None, eta: Optional[str] = None, elapsed: Optional[str] = None) -> str:
    """Generates a formatted progress string with a spinner."""
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
    """Runs a synchronous function in a separate thread to avoid blocking asyncio loop."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))

def normalize_url(url: str) -> str:
    """Normalizes common YouTube URL variations to a standard format."""
    url = url.strip().replace("m.youtube.com", "www.youtube.com").replace("music.youtube.com", "www.youtube.com")
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


# ---------------- Robust Progress Manager ----------------
class ProgressManager:
    """Manages sending and updating a progress message in Telegram."""
    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message: Optional[Message] = None
        self.last_update_text = ""
        self.last_update_time = 0
        self.loop = asyncio.get_running_loop()

    async def send_initial_message(self, text: str = "Initializing..."):
        """Sends the first progress message."""
        initial_text = generate_progress_text(text)
        try:
            self.message = await self.bot.send_message(self.chat_id, initial_text, parse_mode=ParseMode.MARKDOWN)
            self.last_update_text = initial_text
        except TelegramError as e:
            logger.error(f"Failed to send initial progress message: {e}")

    def _update_message_threadsafe(self, text: str):
        """Schedules a message update from a synchronous thread."""
        current_time = time.time()
        # Throttle updates to avoid hitting Telegram API limits
        if self.message and text != self.last_update_text and (current_time - self.last_update_time > 1.5):
            self.last_update_text = text
            self.last_update_time = current_time
            asyncio.run_coroutine_threadsafe(
                self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                self.loop
            ).result() # Using .result() can be blocking, but okay for quick updates.

    async def update(self, text: str):
        """Updates the progress message asynchronously."""
        if self.message and text != self.last_update_text:
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                self.last_update_text = text
            except BadRequest: # Message might be unchanged, ignore
                pass
            except TelegramError as e:
                logger.warning(f"Failed to edit progress message: {e}")

    async def delete(self):
        """Deletes the progress message."""
        if self.message:
            try:
                await self.message.delete()
            except TelegramError:
                pass
        self.message = None

    def get_progress_hook(self, start_time: float):
        """Returns a progress hook function for yt-dlp."""
        def progress_hook(d):
            if d['status'] == 'finished':
                self._update_message_threadsafe(generate_progress_text("Processing file..."))
                return

            if d['status'] == 'downloading':
                percent_str = d.get('_percent_str', '0%').replace('%', '').strip()
                percent = float(percent_str) if percent_str else 0
                text = generate_progress_text(
                    "Downloading...", percent, d.get('_speed_str'), d.get('_eta_str'), format_elapsed(time.time() - start_time)
                )
                self._update_message_threadsafe(text)
        return progress_hook

# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    """Saves the current download queue to a JSON file."""
    try:
        with QUEUE_FILE.open("w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in DOWNLOAD_QUEUE.items()}, f, indent=4)
    except IOError as e:
        logger.exception(f"Failed to save queue to disk: {e}")

def load_queue_from_disk():
    """Loads the download queue from a JSON file on startup."""
    global DOWNLOAD_QUEUE
    if QUEUE_FILE.exists():
        try:
            with QUEUE_FILE.open("r", encoding="utf-8") as f:
                DOWNLOAD_QUEUE = {str(k): v for k, v in json.load(f).items()}
            logger.info(f"Loaded {sum(len(v) for v in DOWNLOAD_QUEUE.values())} tasks from queue.json")
        except (IOError, json.JSONDecodeError) as e:
            logger.exception(f"Failed to load queue from disk: {e}")
            DOWNLOAD_QUEUE = {}


# ---------------- Upload Helpers (Multi-service with better error handling) ----------------
async def upload_file(file_path: Path, progress: ProgressManager) -> Optional[str]:
    """Tries to upload a file using a sequence of services, returning the first successful link."""
    uploaders = [
        ("GoFile", upload_to_gofile),
        ("File.io", upload_to_fileio),
        ("Transfer.sh", upload_to_transfersh),
    ]
    for name, uploader_func in uploaders:
        await progress.update(generate_progress_text(f"Uploading to {name}..."))
        logger.info(f"Attempting upload of {file_path.name} to {name}...")
        try:
            link = await uploader_func(str(file_path))
            if link:
                logger.info(f"Successfully uploaded to {name}: {link}")
                return link
            else:
                 logger.warning(f"{name} upload failed for {file_path.name}, trying next service.")
        except Exception as e:
            logger.error(f"An exception occurred during upload to {name}: {e}")
            
    logger.error(f"All upload services failed for {file_path.name}.")
    return None

async def _upload_with_aiohttp(url: str, file_path: str, method: str = 'POST') -> Optional[Dict[str, Any]]:
    """Generic aiohttp upload helper."""
    try:
        timeout = aiohttp.ClientTimeout(total=600) # 10 minute timeout for uploads
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                
                request_method = session.post if method.upper() == 'POST' else session.put
                
                async with request_method(url, data=data if method.upper() == 'POST' else f) as resp:
                    resp.raise_for_status()
                    # Check content type to avoid errors with non-JSON responses
                    if 'application/json' in resp.headers.get('Content-Type', ''):
                        return await resp.json()
                    return {"text": await resp.text()}
    except aiohttp.ClientError as e:
        logger.error(f"Network error during upload to {url}: {e}")
    except asyncio.TimeoutError:
        logger.error(f"Upload to {url} timed out.")
    except Exception as e:
        logger.error(f"Generic upload error for {url}: {e}")
    return None

async def upload_to_gofile(file_path: str) -> Optional[str]:
    """Uploads a file to GoFile.io and returns the download link."""
    response = await _upload_with_aiohttp("https://store1.gofile.io/uploadFile", file_path)
    if response and response.get("status") == "ok":
        return response.get("data", {}).get("downloadPage")
    return None

async def upload_to_fileio(file_path: str) -> Optional[str]:
    """Uploads a file to File.io and returns the download link."""
    response = await _upload_with_aiohttp("https://file.io/?expires=1d", file_path)
    if response and response.get("success"):
        return response.get("link")
    return None

async def upload_to_transfersh(file_path: str) -> Optional[str]:
    """Uploads a file to Transfer.sh and returns the download link."""
    response = await _upload_with_aiohttp(f"https://transfer.sh/{Path(file_path).name}", file_path, method='PUT')
    return response.get("text") if response else None


# ---------------- Queue Operations ----------------
async def process_queue_for_user(user_id: str, application: Application):
    """Continuously processes tasks from a specific user's queue."""
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        try:
            # Use a semaphore to limit concurrent global downloads
            async with DOWNLOAD_SEMAPHORE:
                logger.info(f"Processing task for user {user_id}: {task['url']}")
                await download_media(task=task, application=application)
        except Exception as e:
            logger.exception(f"Critical error in task processor for user {user_id}. Task: {task}. Error: {e}")
            await application.bot.send_message(task['chat_id'], f"A critical error occurred while processing your request for {task['url']}. The task has been skipped.")
        
        # A small delay to prevent rapid-fire processing in case of errors
        await asyncio.sleep(1)

async def queue_download(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_filename: Optional[str]):
    """Adds a new download task to the user's queue."""
    user_id_str = str(update.effective_user.id)
    task = {
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
    
    # Gracefully handle if the previous message was a callback or a regular message
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text)
    else:
        # If the user sent a filename, the original message is the link, reply to that
        # Otherwise, reply to the new filename message
        if update.message:
            await update.message.reply_text(message_text)

    # If the queue processor for this user is not running, start it.
    if len(DOWNLOAD_QUEUE[user_id_str]) == 1:
        asyncio.create_task(process_queue_for_user(user_id_str, context.application))


# ---------------- Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    user_name = update.effective_user.first_name or "User"
    caption = (f"👋 Hello, *{user_name}*!\n\nSend me a link to get started.\n\n"
               "*Commands:*\n`/sites` - See all supported websites\n`/queue` - View your current queue\n`/cancel` - Clear your queue")
    try:
        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        # Fallback to text if sending a photo fails
        await update.message.reply_markdown(caption)

async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /sites command."""
    await update.message.reply_text(f"Full list of supported sites:\n{SUPPORTED_SITES_LINK}")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the conversation, handles receiving a link."""
    msg = update.message
    url = normalize_url(msg.text)
    status_msg = await msg.reply_text("🔍 Analyzing link, please wait...")
    
    info = None
    try:
        ydl_opts = {
            'quiet': True,
            'noplaylist': True,
            'skip_download': True,
            'extract_flat': 'in_playlist' # Faster for playlists, though we disallow them
        }
        if COOKIE_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIE_FILE)
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await to_thread(ydl.extract_info, url, download=False)

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp download error for {url}: {e}")
        error_text = "❌ Error: Could not process the link."
        if "Unsupported URL" in str(e):
            error_text = "❌ Error: This website or link is not supported."
        elif "Video unavailable" in str(e):
            error_text = "❌ Error: This video is unavailable."
        elif "Private video" in str(e):
            error_text = "❌ Error: This video is private."
        elif "confirm you’re not a bot" in str(e):
            error_text += "\n\nThis video may require a login. The bot's cookie file could be invalid or expired."
        await status_msg.edit_text(error_text)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Generic error handling link {url}: {e}")
        await status_msg.edit_text("❌ An unexpected error occurred. Please try again later.")
        return ConversationHandler.END

    if not info:
        await status_msg.edit_text("❌ Error: Could not retrieve any information for this link.")
        return ConversationHandler.END

    context.user_data.update({'url': url, 'info': info})
    title = info.get('title', 'Unknown Title')
    buttons = [
        [InlineKeyboardButton("🎬 Video", callback_data='format|mp4'), InlineKeyboardButton("🎵 Audio", callback_data='format|mp3')]
    ]
    await status_msg.delete()
    await msg.reply_markdown(f"*{title}*\n\nChoose your desired format:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_FORMAT


async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice between video and audio."""
    query = update.callback_query
    await query.answer()
    context.user_data["format_choice"] = query.data.split("|")[1]
    
    if context.user_data["format_choice"] == 'mp3':
        context.user_data['quality_id'] = 'bestaudio'
        buttons = [[InlineKeyboardButton("✏️ Rename File", callback_data='rename|yes'), InlineKeyboardButton("➡️ Keep Original Name", callback_data='rename|no')]]
        await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_RENAME

    info = context.user_data.get("info", {})
    buttons, seen_heights = [], set()

    # Filter for formats that are actually video and have resolution info
    video_formats = [f for f in info.get("formats", []) if f.get('vcodec', 'none') != 'none' and f.get('height')]
    
    if not video_formats:
        # Fallback if no video streams found (e.g., soundcloud)
        await query.edit_message_text("No video formats found for this link. Please choose audio instead.", reply_markup=None)
        return ConversationHandler.END

    for f in video_formats:
        height = f.get('height')
        if height and height not in seen_heights:
            seen_heights.add(height)
            filesize = f.get('filesize') or f.get('filesize_approx')
            label = f"{height}p"
            if filesize:
                label += f" (~{format_bytes(filesize)})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"quality|{f['format_id']}")])

    # If after filtering we have no buttons, offer a generic best option
    if not buttons:
        buttons.append([InlineKeyboardButton("Best Available Quality", callback_data="quality|best")])
    
    # Sort buttons by resolution, descending
    buttons.sort(
        key=lambda b: int(re.search(r'(\d+)p', b[0].text).group(1)) if re.search(r'(\d+)p', b[0].text) else 0,
        reverse=True
    )
    
    await query.edit_message_text("Please select a video quality:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_QUALITY


async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice of video quality."""
    query = update.callback_query
    await query.answer()
    context.user_data['quality_id'] = query.data.split("|")[1]
    buttons = [[InlineKeyboardButton("✏️ Rename File", callback_data='rename|yes'), InlineKeyboardButton("➡️ Keep Original Name", callback_data='rename|no')]]
    await query.edit_message_text("Do you want to rename the file?", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_RENAME

async def ask_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the user's choice to rename the file or not."""
    query = update.callback_query
    await query.answer()
    if query.data.split("|")[1] == 'yes':
        await query.edit_message_text("OK. Please send me the new filename (without the file extension).")
        return GET_NEW_NAME
    else:
        # No rename, queue the download immediately
        await queue_download(update, context, custom_filename=None)
        return ConversationHandler.END

async def get_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives the new filename from the user and queues the download."""
    await queue_download(update, context, custom_filename=sanitize_filename(update.message.text))
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler for /cancel, clears the user's queue and ends conversations."""
    user_id_str = str(update.effective_user.id)
    if DOWNLOAD_QUEUE.get(user_id_str):
        DOWNLOAD_QUEUE[user_id_str].clear()
        save_queue_to_disk()
        await update.message.reply_text("✅ Your download queue has been cleared.")
    else:
        await update.message.reply_text("Your queue is already empty.")
        
    # Also provides an exit point for any active conversation
    if 'info' in context.user_data:
        context.user_data.clear()
        await update.message.reply_text("The current download operation has been cancelled.")
        return ConversationHandler.END

    return ConversationHandler.END


# ---------------- Download Core Logic ----------------
async def download_media(task: Dict[str, Any], application: Application):
    """The main download logic for a single task."""
    chat_id, url = task['chat_id'], task['url']
    progress = ProgressManager(application.bot, chat_id)
    await progress.send_initial_message("Preparing to download...")
    
    final_path = None
    try:
        start_time = time.monotonic()
        
        # Base yt-dlp options
        ydl_opts = {
            'noplaylist': True,
            'quiet': True,
            'progress_hooks': [progress.get_progress_hook(start_time)],
            'outtmpl': str(DOWNLOAD_DIR / (f"{task['custom_filename']}.%(ext)s" if task['custom_filename'] else "%(title)s.%(ext)s")),
            'retries': 5,
            'fragment_retries': 5,
            'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
            'ignoreerrors': True,
        }
        
        if COOKIE_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIE_FILE)
            
        # --- MORE FLEXIBLE FORMAT SELECTION LOGIC ---
        if task['format_choice'] == 'mp3':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
            })
        else: # mp4
            # This is a more robust format selector that handles cases where combined streams don't exist
            quality = task['quality_id']
            # Select best video with chosen quality ID + best audio, OR best video/audio if separate streams are not available.
            ydl_opts['format'] = f"{quality}+bestaudio/bestvideo[height<=?{quality}]+bestaudio/best"
            ydl_opts.setdefault('postprocessors', []).append({'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'})
            # Add metadata postprocessor for embedding thumbnail etc.
            ydl_opts.setdefault('postprocessors', []).append({'key': 'FFmpegMetadata'})

        # Download starts here
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await to_thread(ydl.extract_info, url, download=True)
            
            # Defensive check if download failed silently
            if not info_dict:
                raise ValueError("yt-dlp failed to return media information after download attempt.")
                
            final_path_str = ydl.prepare_filename(info_dict)
            if not final_path_str:
                raise FileNotFoundError("Could not determine final file path from yt-dlp.")
            
            # Correct the extension based on the requested format
            temp_path = Path(final_path_str)
            if task['format_choice'] == 'mp3':
                final_path = temp_path.with_suffix('.mp3')
            else:
                final_path = temp_path.with_suffix('.mp4')

            # yt-dlp sometimes creates the file with the original extension before post-processing.
            # We need to find the correct final file.
            if not final_path.exists() and temp_path.exists():
                # This means post-processing likely happened and the file was renamed
                # e.g., from .webm to .mp4. Let's find it.
                if final_path.exists():
                     pass # Already correct
                elif temp_path.with_suffix('.mp4').exists():
                    final_path = temp_path.with_suffix('.mp4')
                elif temp_path.with_suffix('.mp3').exists():
                    final_path = temp_path.with_suffix('.mp3')


        # --- MORE ROBUST FILE VALIDATION ---
        if not final_path or not final_path.exists():
            await asyncio.sleep(2) # Wait for filesystem to catch up
            if not final_path or not final_path.exists():
                raise FileNotFoundError(f"File not found at expected path after download: {final_path}")
        
        file_size = final_path.stat().st_size
        if file_size < 1024: # Less than 1 KB is suspicious
            raise ValueError(f"Downloaded file is suspiciously small ({format_bytes(file_size)}). Download likely failed.")

        # --- UPLOAD LOGIC ---
        if file_size <= TELEGRAM_SAFE_MAX_BYTES:
            await progress.update(generate_progress_text(f"Uploading {format_bytes(file_size)} to Telegram..."))
            with final_path.open("rb") as f:
                await application.bot.send_document(chat_id, document=f, filename=final_path.name)
        else:
            await progress.update(generate_progress_text(f"File is {format_bytes(file_size)}, using external host..."))
            link = await upload_file(final_path, progress)
            if link:
                await application.bot.send_message(chat_id, f"✅ Upload complete!\n\n**File:** `{final_path.name}`\n**Link:** {link}", parse_mode=ParseMode.MARKDOWN)
            else:
                await application.bot.send_message(chat_id, "❌ All upload services failed. Could not upload the file.")

        await progress.delete()
    
    except (yt_dlp.utils.DownloadError, ValueError, FileNotFoundError) as e:
        error_message = f"❌ Download failed. Reason: {str(e)[:200]}"
        logger.error(f"Download failure for URL {url}: {e}")
        try:
            await progress.update(error_message)
        except TelegramError: # If progress message was deleted or invalid
            await application.bot.send_message(chat_id, error_message)
    except Exception as e:
        error_message = "❌ An unexpected critical error occurred during download."
        logger.exception(f"CRITICAL FAILURE for URL {url}")
        try:
            await progress.update(error_message)
        except TelegramError:
            await application.bot.send_message(chat_id, error_message)
    finally:
        # Cleanup: ensure the downloaded file is deleted
        if final_path and final_path.exists():
            await to_thread(final_path.unlink)
            logger.info(f"Successfully cleaned up: {final_path.name}")


# ---------------- Application Bootstrap ----------------
def main():
    """Initializes and runs the bot."""
    if not shutil.which("ffmpeg"):
        logger.error("FATAL ERROR: FFmpeg is not installed or not in PATH. Please install it to enable format conversions.")
        return
    logger.info("FFmpeg found, proceeding with startup.")

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
        conversation_timeout=600, # 10 minutes
        persistent=True,
        name="download_conv"
    )
    
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("sites", sites_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler)) # A global cancel
    application.add_handler(conv_handler)
    
    async def on_startup(app: Application):
        """Resumes any queued downloads when the bot restarts."""
        if any(DOWNLOAD_QUEUE.values()):
            active_users = [uid for uid, tasks in DOWNLOAD_QUEUE.items() if tasks]
            logger.info(f"Resuming queues for users: {', '.join(active_users)}")
            for user_id in active_users:
                asyncio.create_task(process_queue_for_user(user_id, app))
    
    application.post_init = on_startup
    
    logger.info("🚀 Bot is running!")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

