import logging
import os
import re
import requests
import asyncio
import time
import yt_dlp
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes,
    ConversationHandler, CallbackQueryHandler
)

# --- ⚙️ CONFIGURATION & CONSTANTS ⚙️ ---
CREATOR_NAME = "shadow maniya"
CONNECT_LINK = "https://www.linkedin.com/in/dhruv-maniya-shadow03"
WELCOME_IMAGE_URL = "https://i.ibb.co/bMNj87bT/download.jpg"
MAX_DURATION = 900  # Safety limit: 15 minutes (in seconds)
MAX_FILE_SIZE_MB = 49.5 # Safety limit for Telegram uploads

# --- ✨ ANIMATIONS ✨ ---
PROCESSING_ANIMATION = ["⚙️ Processing", "⚙️⚙️ Processing.", "⚙️⚙️⚙️ Processing..", "⚙️⚙️⚙️⚙️ Processing..."]

# --- 💡 CONVERSATION STATES 💡 ---
CHOOSE_FORMAT = range(1)

# --- Bot Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
async def upload_to_0x0(file_path):
    """Uploads a file to 0x0.st and returns the download link."""
    try:
        with open(file_path, 'rb') as f:
            response = requests.post('https://0x0.st', files={'file': f})
        if response.status_code == 200:
            return response.text.strip()
        else:
            logger.error(f"0x0.st upload failed with status {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"0x0.st upload failed: {e}")
        return None

async def update_progress_message(d, context: ContextTypes.DEFAULT_TYPE, message):
    """The progress hook function for yt-dlp to show real-time progress."""
    if d['status'] == 'downloading':
        now = time.time()
        # Update every 2 seconds to avoid spamming Telegram API
        if now - context.bot_data.get('last_update', 0) < 2:
            return
        
        progress_text = (f"Downloading...\n"
                         f"📈 **Progress**: `{d['_percent_str']}`\n"
                         f"💨 **Speed**: `{d['_speed_str']}`\n"
                         f"⏳ **ETA**: `{d['_eta_str']}`")
        try:
            await context.bot.edit_message_text(
                text=progress_text, chat_id=message.chat_id,
                message_id=message.message_id, parse_mode=ParseMode.MARKDOWN)
            context.bot_data['last_update'] = now
        except:  # Ignore errors like "message not modified"
            pass
    elif d['status'] == 'finished':
        # Animate the "processing" message after download finishes
        for frame in PROCESSING_ANIMATION:
            try:
                await context.bot.edit_message_text(text=frame, chat_id=message.chat_id, message_id=message.message_id)
                await asyncio.sleep(0.5)
            except:
                break

# --- Main Download Logic ---
async def download_media(chat_id, url, format_choice, context: ContextTypes.DEFAULT_TYPE):
    processing_message = await context.bot.send_message(chat_id=chat_id, text="🔄 Preparing download...")
    
    progress_hook = lambda d: asyncio.ensure_future(update_progress_message(d, context, processing_message))
    
    ydl_opts = {
        'noplaylist': True,
        'logger': logger,
        'outtmpl': '%(title)s.%(ext)s',
        'progress_hooks': [progress_hook] # <-- ADDED PROGRESS HOOK BACK
    }

    if format_choice == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
    else:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            original_file_path = ydl.prepare_filename(info_dict)
            
            if format_choice == 'mp3':
                file_path = os.path.splitext(original_file_path)[0] + '.mp3'
            else:
                file_path = original_file_path
        
        await processing_message.edit_text("✅ Download complete. Checking file size...")
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        if file_size_mb < MAX_FILE_SIZE_MB:
            await processing_message.edit_text(f"⬆️ File is {file_size_mb:.2f} MB. Uploading to Telegram...")
            if format_choice == 'mp3':
                with open(file_path, 'rb') as audio_file:
                    await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=info_dict.get('title'))
            else:
                with open(file_path, 'rb') as video_file:
                    await context.bot.send_video(chat_id=chat_id, video=video_file, caption=info_dict.get('title'))
        else:
            await processing_message.edit_text(f"⏳ File is {file_size_mb:.2f} MB. Uploading to temporary host...")
            download_link = await upload_to_0x0(file_path)
            if download_link:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Upload complete! Download your large file here:\n\n{download_link}")
            else:
                await context.bot.send_message(chat_id=chat_id, text="❌ Sorry, the upload to the temporary host failed.")
        
        await processing_message.delete()
        await context.bot.send_message(chat_id=chat_id, text=f"✅ Task complete! Connect with *{CREATOR_NAME}* here: {CONNECT_LINK}", parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        error_message = f"❌ **An error occurred**\n\n`{str(e)}`"
        await processing_message.edit_text(error_message, parse_mode=ParseMode.MARKDOWN)
        logger.error(f"Error processing link: {e}", exc_info=True)
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# --- Conversation Handlers ---
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    pre_check_message = await update.message.reply_text("🔍 Checking link...")

    try:
        with yt_dlp.YoutubeDL({'noplaylist': True, 'quiet': True}) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            duration = info_dict.get('duration', 0)

        # ADDED DURATION CHECK BACK
        if duration > MAX_DURATION:
            error_message = f"❌ *Video is too long!* This bot's limit is {MAX_DURATION // 60} minutes to keep things running smoothly."
            await pre_check_message.edit_text(text=error_message, parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END
        
        context.user_data['url'] = url
        keyboard = [[InlineKeyboardButton("🎬 Video (MP4)", callback_data='mp4'),
                     InlineKeyboardButton("🎵 Audio (MP3)", callback_data='mp3')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        duration_str = time.strftime('%H:%M:%S', time.gmtime(duration))
        preview_text = (f"**Title:** {info_dict.get('title')}\n"
                        f"**Duration:** {duration_str}\n\n"
                        "Please choose your desired format:")
        
        await pre_check_message.edit_text(text=preview_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return CHOOSE_FORMAT
        
    except Exception as e:
        await pre_check_message.edit_text(f"❌ Could not process the link. It might be private or from an unsupported site.")
        logger.error(f"Pre-check failed for {url}: {e}")
        return ConversationHandler.END

async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = context.user_data.get('url')
    format_choice = query.data
    
    if not url:
        await query.edit_message_text("Sorry, something went wrong. Please send the link again.")
        return ConversationHandler.END
        
    await query.edit_message_text(f"Great! Preparing to get the **{format_choice.upper()}** for you.")
    await download_media(query.message.chat_id, url, format_choice, context)
    return ConversationHandler.END

# --- Standard Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_caption = (f"👋 Hello, {user_name}!\n\nI am the **Ultimate Media Downloader**, created by *{CREATOR_NAME}*.\n\n"
                       "Send me a link from YouTube, TikTok, Instagram, X (Twitter), and more to begin.")
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=WELCOME_IMAGE_URL, caption=welcome_caption,
                                 parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ("**How to use this bot:**\n\n"
                 "1. Send a link from a supported site.\n"
                 "2. I'll show you a preview.\n"
                 "3. Choose your format (Video/Audio).\n"
                 "4. I'll download it. If it's too big, I'll send a link.\n\n"
                 "Use /cancel to stop any operation.")
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data: context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# --- Main Bot Execution ---
def main():
    if not BOT_TOKEN:
        print("FATAL ERROR: BOT_TOKEN environment variable not found.")
        return
    
    application = ApplicationBuilder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        conversation_timeout=600 # 10 minute timeout
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    
    print("🚀 Ultimate Media Downloader is up and running!")
    application.run_polling()

if __name__ == '__main__':
    main()