#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - Final Version
Features:
- yt-dlp for downloads
- aiohttp for non-blocking uploads (0x0.st & gofile)
- progress updates (rate-limited)
- optional rename (user types new name or /skip)
- video quality selection
- per-user queue with JSON persistence (queue.json)
- global concurrency limit (Semaphore)
- PicklePersistence for conversation/user_data persistence
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
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters, PicklePersistence
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_FILE = Path("queue.json")           # persistent queue store
PERSISTENCE_FILE = "bot_persistence.pkl"  # PicklePersistence file for user_data

SUPPORTED_SITES_LINK = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
CREATOR_NAME = "shadow maniya"
CONNECT_LINK = "https://www.linkedin.com/in/dhruv-maniya-shadow03"

# Limits
TELEGRAM_SAFE_MAX_BYTES = 49 * 1024 * 1024  # 49 MB (safe)
GLOBAL_MAX_CONCURRENT_DOWNLOADS = 3         # semaphore limit

# Spinner frames & UI
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]

# Conversation states
CHOOSE_FORMAT, CHOOSE_QUALITY, ASK_RENAME, GET_NEW_NAME = range(4)

# In-memory queue: user_id -> list[task]
DOWNLOAD_QUEUE: Dict[str, List[Dict[str, Any]]] = {}

# Global semaphore
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT_DOWNLOADS)

# Logging
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Utilities ----------------
def sanitize_filename(name: str) -> str:
    """Replace unsafe file characters and trim whitespace."""
    if not name:
        return ""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def generate_progress_text(status_text: str, percent: Optional[float] = None,
                           speed: Optional[str] = None, eta: Optional[str] = None,
                           elapsed: Optional[str] = None) -> str:
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
    """Convenience wrapper for running blocking functions in a thread."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))

# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    try:
        # Convert keys to str for JSON safety (user_id ints -> strings)
        serializable = {str(k): v for k, v in DOWNLOAD_QUEUE.items()}
        with QUEUE_FILE.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save queue to disk")

def load_queue_from_disk():
    global DOWNLOAD_QUEUE
    if not QUEUE_FILE.exists():
        return
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # restore keys as str (we use str user_id consistently)
        DOWNLOAD_QUEUE = {str(k): v for k, v in data.items()}
        logger.info("Loaded queue from disk")
    except Exception:
        logger.exception("Failed to load queue from disk")

# ---------------- Upload helpers (aiohttp) ----------------
async def upload_to_gofile(file_path: str) -> Optional[str]:
    url = "https://store1.gofile.io/uploadFile"
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post(url, data=data, timeout=300) as resp:
                    resp_json = await resp.json()
                    return resp_json.get("data", {}).get("downloadPage")
    except Exception:
        logger.exception("Gofile upload failed")
        return None

async def upload_to_0x0(file_path: str) -> Optional[str]:
    url = "https://0x0.st"
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post(url, data=data, timeout=120) as resp:
                    text = await resp.text()
                    if resp.status == 200 and text.strip():
                        return text.strip()
                    return None
    except Exception:
        logger.exception("0x0.st upload failed")
        return None

# ---------------- Queue operations ----------------
async def process_queue_for_user(user_id: str, app_context: ContextTypes.DEFAULT_TYPE):
    """Process queued downloads for a user sequentially. Uses global semaphore."""
    # Guard: ensure user_id in DOWNLOAD_QUEUE
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        chat_id = task["chat_id"]
        url = task["url"]
        format_choice = task["format_choice"]
        quality_id = task.get("quality_id", "best")
        custom_filename = task.get("custom_filename")
        try:
            async with DOWNLOAD_SEMAPHORE:
                logger.info(f"Starting download for user {user_id}: {url}")
                await download_media(chat_id, url, format_choice, quality_id, custom_filename, app_context)
        except Exception:
            logger.exception("Error while processing queued task")
        # small yield to the loop
        await asyncio.sleep(0.5)

async def queue_download(chat_id: int, user_id: int, url: str,
                         format_choice: str, quality_id: str,
                         custom_filename: Optional[str], app_context: ContextTypes.DEFAULT_TYPE):
    """Add task to queue and start processing if idle."""
    uid = str(user_id)
    DOWNLOAD_QUEUE.setdefault(uid, []).append({
        "chat_id": chat_id,
        "url": url,
        "format_choice": format_choice,
        "quality_id": quality_id,
        "custom_filename": custom_filename
    })
    save_queue_to_disk()
    # If this is the only task, spawn a background task to process queue
    if len(DOWNLOAD_QUEUE[uid]) == 1:
        # start background task without awaiting
        asyncio.create_task(process_queue_for_user(uid, app_context))

# ---------------- Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"👋 Hello {user_name}!\n"
        "Send a supported media link (YouTube, TikTok, etc.) to begin.\n\n"
        "Shortcuts:\n"
        "/audio <url> - Direct audio\n"
        "/video <url> - Direct video\n"
        "/sites - Supported sites\n"
        "/cancel - Cancel your queued downloads"
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use:\n1. Send a link.\n2. Choose format & quality.\n3. Rename or /skip.\n\n"
        "Shortcuts:\n/audio <url>\n/video <url>\n/sites\n/cancel"
    )

async def sites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Supported sites (via yt-dlp): {SUPPORTED_SITES_LINK}")

# Handle link: fetch info and present format buttons
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    url = msg.text.strip()
    status_msg = await msg.reply_text("🔍 Checking link and fetching metadata...")
    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL({'noplaylist': True, 'quiet': True}) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        context.user_data['url'] = url
        context.user_data['info'] = info

        # Prepare preview
        title = info.get('title', 'Unknown title')
        uploader = info.get('uploader', 'Unknown uploader')
        duration = time.strftime('%H:%M:%S', time.gmtime(info.get('duration', 0)))
        thumbnail = info.get('thumbnail')

        preview = f"*{title}*\n_by:_ {uploader}\nDuration: `{duration}`\n\nChoose format:"
        buttons = [
            [InlineKeyboardButton("🎬 Video (MP4)", callback_data='format|mp4'),
             InlineKeyboardButton("🎵 Audio (MP3)", callback_data='format|mp3')]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        await status_msg.delete()
        if thumbnail:
            await msg.reply_photo(photo=thumbnail, caption=preview, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        else:
            await msg.reply_markdown(preview, reply_markup=reply_markup)
        return CHOOSE_FORMAT
    except Exception as e:
        logger.exception("Error in handle_link")
        await status_msg.edit_text("❌ Could not process the link. Maybe it's private or unsupported.")
        return ConversationHandler.END

# When user picks format (mp3 or mp4)
async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    payload = query.data  # e.g., 'format|mp4' or 'format|mp3'
    _, fmt = payload.split("|", 1)
    context.user_data['format_choice'] = fmt

    if fmt == 'mp3':
        # skip quality selection — audio uses best audio
        context.user_data['quality_id'] = 'bestaudio'
        # ask rename or keep
        buttons = [[InlineKeyboardButton("✅ Keep Default Name", callback_data='rename_choice|keep'),
                    InlineKeyboardButton("✏️ Rename File", callback_data='rename_choice|rename')]]
        text = f"Default filename: `{sanitize_filename(context.user_data.get('info', {}).get('title','file'))}`\nDo you want to rename it?"
        try:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return ASK_RENAME
    else:
        # Build quality list from info['formats']
        info = context.user_data.get('info', {})
        formats = info.get('formats', [])
        # Filter formats that have video (vcodec != none) and audio (acodec != none) or simply prefer mp4
        candidates = []
        seen_heights = set()
        for f in formats:
            vcodec = f.get('vcodec')
            acodec = f.get('acodec')
            height = f.get('height') or 0
            format_id = f.get('format_id')
            ext = f.get('ext')
            # prefer formats that contain both video & audio; but if not available include video-only
            if vcodec and vcodec != 'none':
                # choose only one format per resolution to avoid duplicates
                if height not in seen_heights:
                    seen_heights.add(height)
                    filesize = f.get('filesize') or f.get('filesize_approx') or 0
                    candidates.append({
                        'height': height,
                        'format_id': format_id,
                        'ext': ext,
                        'filesize': filesize
                    })
        # Fallback if no candidates
        if not candidates:
            buttons = [[InlineKeyboardButton("Best available", callback_data='quality|best')]]
            await query.edit_message_text("No quality list available — proceeding with Best available.", reply_markup=InlineKeyboardMarkup(buttons))
            return CHOOSE_QUALITY

        # Create buttons sorted by height desc
        buttons = []
        # Add Best option
        buttons.append([InlineKeyboardButton("Best available", callback_data='quality|best')])
        for c in sorted(candidates, key=lambda x: (x['height'] or 0), reverse=True):
            label = f"{c['height']}p" if c['height'] else "Unknown"
            if c['filesize']:
                label += f" (~{c['filesize'] / (1024*1024):.1f}MB)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"quality|{c['format_id']}")])
        await query.edit_message_text("Choose a video quality:", reply_markup=InlineKeyboardMarkup(buttons))
        return CHOOSE_QUALITY

# Quality chosen handler
async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    payload = query.data  # 'quality|best' or 'quality|<format_id>'
    _, quality = payload.split("|", 1)
    context.user_data['quality_id'] = quality
    # ask rename or keep
    buttons = [[InlineKeyboardButton("✅ Keep Default Name", callback_data='rename_choice|keep'),
                InlineKeyboardButton("✏️ Rename File", callback_data='rename_choice|rename')]]
    text = f"Default filename: `{sanitize_filename(context.user_data.get('info', {}).get('title','file'))}`\nDo you want to rename it?"
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_RENAME

# Rename choice (inline)
async def ask_rename_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, choice = query.data.split("|", 1)
    if choice == 'keep':
        context.user_data['custom_filename'] = None
        await query.edit_message_text("Using default filename. Queuing download...")
        await queue_download(query.message.chat_id, query.from_user.id,
                             context.user_data['url'], context.user_data['format_choice'],
                             context.user_data.get('quality_id', 'best'), None, context)
        return ConversationHandler.END
    else:
        await query.edit_message_text("Send the new filename (no extension). Or type /skip to keep default.")
        return GET_NEW_NAME

# Receive a typed filename
async def get_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    sanitized = sanitize_filename(raw)
    if not sanitized:
        await update.message.reply_text("Invalid filename. Send another name or /skip.")
        return GET_NEW_NAME
    context.user_data['custom_filename'] = sanitized
    await update.message.reply_text(f"Filename set to `{sanitized}`", parse_mode=ParseMode.MARKDOWN)
    # queue download
    await queue_download(update.message.chat_id, update.effective_user.id,
                         context.user_data['url'], context.user_data['format_choice'],
                         context.user_data.get('quality_id', 'best'), sanitized, context)
    return ConversationHandler.END

# Skip rename
async def skip_rename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['custom_filename'] = None
    await update.message.reply_text("Keeping default filename.")
    await queue_download(update.message.chat_id, update.effective_user.id,
                         context.user_data['url'], context.user_data['format_choice'],
                         context.user_data.get('quality_id', 'best'), None, context)
    return ConversationHandler.END

# Shortcuts
async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /audio <url>")
    url = context.args[0]
    await queue_download(update.effective_chat.id, update.effective_user.id, url, 'mp3', 'bestaudio', None, context)
    await update.message.reply_text("🎵 Audio queued.")
    return ConversationHandler.END

async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /video <url>")
    url = context.args[0]
    await queue_download(update.effective_chat.id, update.effective_user.id, url, 'mp4', 'best', None, context)
    await update.message.reply_text("🎬 Video queued.")
    return ConversationHandler.END

# Cancel handler - clears user's queue
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if DOWNLOAD_QUEUE.get(uid):
        DOWNLOAD_QUEUE[uid].clear()
        save_queue_to_disk()
        await update.message.reply_text("🛑 Your queued downloads were cancelled.")
    else:
        await update.message.reply_text("You have no queued downloads.")
    return ConversationHandler.END

# ---------------- Core download logic ----------------
async def download_media(chat_id: int, url: str, format_choice: str,
                         quality_id: str, custom_filename: Optional[str],
                         context: ContextTypes.DEFAULT_TYPE):
    """Download via yt-dlp (in executor), show progress, upload to Telegram or cloud."""
    # send initial status message
    status_msg = await context.bot.send_message(chat_id=chat_id, text=generate_progress_text("Initializing..."), parse_mode=ParseMode.MARKDOWN)
    start_time = time.monotonic()
    last_update_ts = 0

    # progress hook runs in thread, schedule edits on event loop
    def progress_hook(d):
        nonlocal last_update_ts
        try:
            status = d.get('status')
            now = time.time()
            # rate-limit updates to avoid flooding
            if status == 'downloading' and now - last_update_ts >= 2:
                last_update_ts = now
                percent_str = d.get('_percent_str', '0%').replace('%', '').strip()
                try:
                    percent = float(percent_str)
                except Exception:
                    percent = 0.0
                speed = d.get('_speed_str', '')
                eta = d.get('_eta_str', '')
                elapsed = format_elapsed(time.monotonic() - start_time)
                text = generate_progress_text("Downloading", percent=percent, speed=speed, eta=eta, elapsed=elapsed)
                loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)
            elif status == 'finished':
                loop = asyncio.get_event_loop()
                text = generate_progress_text("Download finished. Processing...")
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN), loop)
        except Exception:
            logger.exception("progress_hook error")

    # build yt-dlp options
    def build_opts():
        opts = {
            'noplaylist': True,
            'quiet': True,
            'progress_hooks': [progress_hook],
            'outtmpl': str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            'retries': 1,
        }
        if format_choice == 'mp3':
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            if quality_id and quality_id != 'best':
                opts['format'] = f"{quality_id}+bestaudio/best"
            else:
                opts['format'] = "best"
            opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
        return opts

    final_path = None
    try:
        loop = asyncio.get_event_loop()
        ydl_opts = build_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            original_path = Path(ydl.prepare_filename(info))
            # extension handling
            if format_choice == 'mp3':
                original_path = original_path.with_suffix('.mp3')
        final_path = original_path

        # rename if requested (use to_thread)
        if custom_filename:
            ext = final_path.suffix or ('.mp3' if format_choice == 'mp3' else '')
            new_path = DOWNLOAD_DIR / f"{sanitize_filename(custom_filename)}{ext}"
            await to_thread(final_path.replace, new_path)
            final_path = new_path

        # Upload phase
        elapsed = format_elapsed(time.monotonic() - start_time)
        await status_msg.edit_text(generate_progress_text("Uploading", percent=100, elapsed=elapsed), parse_mode=ParseMode.MARKDOWN)

        # get file size using to_thread
        stat = await to_thread(final_path.stat)
        size_bytes = stat.st_size

        # if small enough -> upload to Telegram, else upload cloud
        if size_bytes <= TELEGRAM_SAFE_MAX_BYTES:
            try:
                async with final_path.open("rb") as fh:
                    await context.bot.send_document(chat_id=chat_id, document=fh, filename=final_path.name)
                await status_msg.delete()
                summary = (f"✅ **Task Complete!**\n\n"
                           f"📄 `{final_path.name}`\n"
                           f"📦 `{size_bytes/(1024*1024):.2f} MB`\n"
                           f"⏱️ `{elapsed}`\n\n"
                           f"Connect with *{CREATOR_NAME}*: {CONNECT_LINK}")
                await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                logger.exception("Telegram upload failed, falling back to cloud")
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Telegram upload failed, uploading to cloud...")
                link = await upload_to_0x0(str(final_path)) or await upload_to_gofile(str(final_path))
                if link:
                    await context.bot.send_message(chat_id=chat_id, text=f"🔗 Download link: {link}")
                else:
                    await context.bot.send_message(chat_id=chat_id, text="❌ Upload to cloud failed.")
                await status_msg.delete()
        else:
            # too large for safe telegram upload
            await context.bot.send_message(chat_id=chat_id, text="⚠️ File too large for Telegram. Uploading to cloud...")
            link = await upload_to_0x0(str(final_path)) or await upload_to_gofile(str(final_path))
            if link:
                await context.bot.send_message(chat_id=chat_id, text=f"🔗 Download link: {link}")
            else:
                await context.bot.send_message(chat_id=chat_id, text="❌ Upload to cloud failed.")
            await status_msg.delete()

    except Exception as e:
        logger.exception("download_media error")
        try:
            await status_msg.edit_text(f"❌ Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # Cleanup downloaded file
        try:
            if final_path and final_path.exists():
                await to_thread(final_path.unlink)
        except Exception:
            logger.exception("Failed to remove temp file")

# ---------------- Application bootstrap ----------------
def main():
    # load persistent queue
    load_queue_from_disk()

    # PicklePersistence for user_data and conversation_data
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    # Conversation handler for link flow
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback, pattern=r"^format\|")],
            CHOOSE_QUALITY: [CallbackQueryHandler(choose_quality_callback, pattern=r"^quality\|")],
            ASK_RENAME: [CallbackQueryHandler(ask_rename_inline_callback, pattern=r"^rename_choice\|")],
            GET_NEW_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_name_handler),
                CommandHandler("skip", skip_rename_handler)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        conversation_timeout=600
    )

    # Add handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("sites", sites_handler))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(CommandHandler("video", video_command))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(conv_handler)

    # On startup: if queue has pending tasks, spawn processing tasks
    async def startup_tasks(app):
        # start processing pending queues
        for user_id in list(DOWNLOAD_QUEUE.keys()):
            if DOWNLOAD_QUEUE.get(user_id):
                # spawn background tasks to process each user's queue
                asyncio.create_task(process_queue_for_user(user_id, app.bot))
        logger.info("Startup tasks scheduled (if any).")

    app.post_init = startup_tasks

    logger.info("🚀 Starting Ultimate Media Downloader Bot")
    app.run_polling()

if __name__ == "__main__":
    main()

