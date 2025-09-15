#!/usr/bin/env python3
"""
Ultimate Media Downloader Bot - FINAL FULL VERSION
Author: Dhruv Maniya (shadow maniya)

Features:
- yt-dlp for downloads
- aiohttp for uploads (0x0.st & gofile)
- progress updates (rate-limited)
- optional rename (inline or /skip)
- video quality selection
- per-user queue with JSON persistence (queue.json)
- global concurrency limit (Semaphore)
- PicklePersistence for conversation/user_data persistence
- Supports mobile/desktop YouTube & youtu.be links (auto-normalized)
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

QUEUE_FILE = Path("queue.json")
PERSISTENCE_FILE = "bot_persistence.pkl"

SUPPORTED_SITES_LINK = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
CREATOR_NAME = "shadow maniya"
CONNECT_LINK = "https://www.linkedin.com/in/dhruv-maniya-shadow03"

WELCOME_IMAGE_URL = "https://i.ibb.co/bMNj87bT/download.jpg"

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
    return re.sub(r'[\/*?:"<>|]', "_", name or "").strip()

def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def generate_progress_text(status_text: str, percent=None, speed=None, eta=None, elapsed=None) -> str:
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
    url = url.strip()
    url = url.replace("m.youtube.com", "youtube.com")
    url = url.replace("music.youtube.com", "youtube.com")
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        return f"https://youtube.com/watch?v={video_id}"
    return url

# ---------------- Queue Persistence ----------------
def save_queue_to_disk():
    try:
        with QUEUE_FILE.open("w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in DOWNLOAD_QUEUE.items()}, f, indent=2)
    except Exception: logger.exception("Failed to save queue")

def load_queue_from_disk():
    global DOWNLOAD_QUEUE
    if QUEUE_FILE.exists():
        try:
            with QUEUE_FILE.open("r", encoding="utf-8") as f:
                DOWNLOAD_QUEUE = {str(k): v for k, v in json.load(f).items()}
        except Exception: logger.exception("Failed to load queue")

# ---------------- Upload helpers ----------------
async def upload_to_gofile(file_path: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post("https://store1.gofile.io/uploadFile", data=data) as resp:
                    js = await resp.json()
                    return js.get("data", {}).get("downloadPage")
    except Exception: logger.exception("Gofile upload failed")

async def upload_to_0x0(file_path: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name)
                async with session.post("https://0x0.st", data=data) as resp:
                    if resp.status == 200: return (await resp.text()).strip()
    except Exception: logger.exception("0x0.st upload failed")

# ---------------- Queue ops ----------------
async def process_queue_for_user(user_id: str, app_context):
    while DOWNLOAD_QUEUE.get(user_id):
        task = DOWNLOAD_QUEUE[user_id].pop(0)
        save_queue_to_disk()
        try:
            async with DOWNLOAD_SEMAPHORE:
                await download_media(
                    task["chat_id"], task["url"],
                    task["format_choice"], task["quality_id"],
                    task.get("custom_filename"), app_context
                )
        except Exception: logger.exception("Task error")
        await asyncio.sleep(0.5)

async def queue_download(chat_id, user_id, url, fmt, qid, cname, app_context):
    uid = str(user_id)
    DOWNLOAD_QUEUE.setdefault(uid, []).append({
        "chat_id": chat_id, "url": url,
        "format_choice": fmt, "quality_id": qid,
        "custom_filename": cname
    })
    save_queue_to_disk()
    if len(DOWNLOAD_QUEUE[uid]) == 1:
        asyncio.create_task(process_queue_for_user(uid, app_context))

# ---------------- Handlers ----------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (f"👋 Hello {update.effective_user.first_name}!\n"
               "Send a link to download media.\n\n"
               "/audio <url>\n/video <url>\n/sites\n/cancel")
    if WELCOME_IMAGE_URL:
        await update.message.reply_photo(photo=WELCOME_IMAGE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_markdown(caption)

async def help_handler(update, context): await update.message.reply_text("Send link → choose format → choose quality → rename or /skip")

async def sites_handler(update, context): await update.message.reply_text(f"Supported sites: {SUPPORTED_SITES_LINK}")

async def handle_link(update, context):
    msg, url = update.message, normalize_url(update.message.text)
    status = await msg.reply_text("🔍 Fetching info...")
    try:
        info = await to_thread(lambda: yt_dlp.YoutubeDL({'quiet': True, 'noplaylist': True}).extract_info(url, download=False))
        context.user_data.update({'url': url, 'info': info})
        title = info.get('title', 'Unknown')
        preview = f"*{title}*\n\nChoose format:"
        buttons = [[InlineKeyboardButton("🎬 Video", callback_data='format|mp4'),
                    InlineKeyboardButton("🎵 Audio", callback_data='format|mp3')]]
        await status.delete()
        await msg.reply_markdown(preview, reply_markup=InlineKeyboardMarkup(buttons))
        return CHOOSE_FORMAT
    except: await status.edit_text("❌ Failed."); return ConversationHandler.END

async def choose_format_callback(update, context):
    q = update.callback_query; await q.answer()
    fmt = q.data.split("|")[1]; context.user_data["format_choice"] = fmt
    info = context.user_data.get("info", {})
    formats = info.get("formats", [])
    if fmt == "mp4":
        buttons = [[InlineKeyboardButton(f"{f.get('format_note','')} ({round(f.get('filesize',0)/1024/1024,1)} MB)", callback_data=f"quality|{f['format_id']}")] for f in formats if f.get("ext")=="mp4" and f.get("filesize")]
    else:
        buttons = [[InlineKeyboardButton(f"{f.get('abr','?')} kbps", callback_data=f"quality|{f['format_id']}")] for f in formats if f.get("ext") in ["mp3","m4a"]]
    if not buttons: buttons=[[InlineKeyboardButton("Best",callback_data="quality|best")]]
    await q.edit_message_text("Select quality:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_QUALITY

async def choose_quality_callback(update, context):
    q = update.callback_query; await q.answer()
    context.user_data["quality_id"]=q.data.split("|")[1]
    buttons=[[InlineKeyboardButton("✏️ Rename",callback_data="rename_choice|yes"),
              InlineKeyboardButton("➡️ Skip",callback_data="rename_choice|no")]]
    await q.edit_message_text("Rename file?",reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_RENAME

async def ask_rename_inline_callback(update, context):
    q = update.callback_query; await q.answer()
    if q.data.endswith("yes"): await q.edit_message_text("Send new filename:"); return GET_NEW_NAME
    await q.edit_message_text("➡️ Added to queue")
    await queue_download(q.message.chat_id,q.from_user.id,context.user_data["url"],context.user_data["format_choice"],context.user_data["quality_id"],None,context.application)
    return ConversationHandler.END

async def get_new_name_handler(update, context):
    name = sanitize_filename(update.message.text)
    await update.message.reply_text(f"➡️ Added to queue as {name}")
    await queue_download(update.message.chat_id,update.effective_user.id,context.user_data["url"],context.user_data["format_choice"],context.user_data["quality_id"],name,context.application)
    return ConversationHandler.END

async def skip_rename_handler(update, context):
    await update.message.reply_text("➡️ Added to queue")
    await queue_download(update.message.chat_id,update.effective_user.id,context.user_data["url"],context.user_data["format_choice"],context.user_data["quality_id"],None,context.application)
    return ConversationHandler.END

async def cancel_handler(update, context):
    DOWNLOAD_QUEUE[str(update.effective_user.id)] = []
    save_queue_to_disk()
    await update.message.reply_text("❌ Queue cleared")

async def audio_command(update, context):
    if not context.args: return await update.message.reply_text("Usage: /audio <url>")
    url=normalize_url(context.args[0])
    await queue_download(update.message.chat_id,update.effective_user.id,url,"mp3","bestaudio",None,context.application)
    await update.message.reply_text("➡️ Audio queued")

async def video_command(update, context):
    if not context.args: return await update.message.reply_text("Usage: /video <url>")
    url=normalize_url(context.args[0])
    await queue_download(update.message.chat_id,update.effective_user.id,url,"mp4","best",None,context.application)
    await update.message.reply_text("➡️ Video queued")

# ---------------- Download core ----------------
async def download_media(chat_id, url, fmt, qid, cname, app_context):
    safe_name = sanitize_filename(cname or "")
    output = str(DOWNLOAD_DIR/("%(title)s.%(ext)s" if not safe_name else safe_name+".%(ext)s"))
    status = await app_context.bot.send_message(chat_id,"⬇️ Downloading...")
    def hook(d):
        if d["status"]=="downloading":
            p=d.get("_percent_str","").strip("%")
            try: p=float(p)
            except: p=None
            text=generate_progress_text("Downloading",p,d.get("_speed_str"),d.get("_eta_str"),d.get("_elapsed_str"))
            try: asyncio.create_task(status.edit_text(text,parse_mode=ParseMode.MARKDOWN))
            except: pass
    ydl_opts={"format":qid,"outtmpl":output,"progress_hooks":[hook]}
    if fmt=="mp3": ydl_opts.update({"format":"bestaudio","postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]})
    file_path=None
    try:
        info=await to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url,download=True))
        file_path=Path(yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info))
        if fmt=="mp3": file_path=file_path.with_suffix(".mp3")
    except Exception: await status.edit_text("❌ Download failed"); return
    if not file_path or not file_path.exists(): return await status.edit_text("❌ File missing")
    if file_path.stat().st_size<=TELEGRAM_SAFE_MAX_BYTES:
        with open(file_path,"rb") as f: await app_context.bot.send_document(chat_id,f,caption=f"✅ {file_path.name}")
    else:
        await status.edit_text("📤 Uploading...")
        link=await upload_to_gofile(str(file_path)) or await upload_to_0x0(str(file_path))
        if link: await app_context.bot.send_message(chat_id,f"✅ Uploaded: {link}")
        else: await app_context.bot.send_message(chat_id,"❌ Upload failed")
    file_path.unlink(missing_ok=True)
    await status.delete()

# ---------------- Bootstrap ----------------
def main():
    load_queue_from_disk()
    persistence=PicklePersistence(filepath=PERSISTENCE_FILE)
    app=Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    conv=ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT:[CallbackQueryHandler(choose_format_callback,pattern=r"^format\|")],
            CHOOSE_QUALITY:[CallbackQueryHandler(choose_quality_callback,pattern=r"^quality\|")],
            ASK_RENAME:[CallbackQueryHandler(ask_rename_inline_callback,pattern=r"^rename_choice\|")],
            GET_NEW_NAME:[
                MessageHandler(filters.TEXT & ~filters.COMMAND,get_new_name_handler),
                CommandHandler("skip",skip_rename_handler)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],conversation_timeout=600)
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("sites", sites_handler))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(CommandHandler("video", video_command))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(conv)
    async def startup(application): [asyncio.create_task(process_queue_for_user(uid,application)) for uid in DOWNLOAD_QUEUE if DOWNLOAD_QUEUE[uid]]
    app.post_init=startup
    logger.info("🚀 Bot running"); app.run_polling()

if __name__=="__main__": main()
