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
MAX_FILE_SIZE_MB = 49.5

# --- 💡 CONVERSATION STATES 💡 ---
CHOOSE_FORMAT, CHOOSE_QUALITY = range(2)

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
        if response.status_code == 200: return response.text.strip()
        else: return None
    except Exception as e:
        logger.error(f"0x0.st upload failed: {e}")
        return None

# --- Main Download Logic ---
async def download_media(chat_id, url, format_choice, quality_id, context: ContextTypes.DEFAULT_TYPE):
    processing_message = await context.bot.send_message(chat_id=chat_id, text="🔄 Preparing to download...")
    
    ydl_opts = {'noplaylist': True, 'logger': logger, 'outtmpl': '%(title)s.%(ext)s'}

    if format_choice == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
    else: # mp4
        # Use the specific quality ID the user selected
        ydl_opts['format'] = f'{quality_id}+bestaudio[ext=m4a]/best[ext=mp4]/best'

    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await processing_message.edit_text("⏳ Downloading media... This may take a moment.")
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
        error_message = f"❌ **An error occurred during download**\n\n`{str(e)}`"
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
        
        context.user_data['url'] = url
        context.user_data['info_dict'] = info_dict  # Store the whole info dict

        keyboard = [[InlineKeyboardButton("🎬 Video (MP4)", callback_data='mp4'),
                     InlineKeyboardButton("🎵 Audio (MP3)", callback_data='mp3')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        duration_str = time.strftime('%H:%M:%S', time.gmtime(info_dict.get('duration', 0)))
        preview_text = (f"**Title:** {info_dict.get('title')}\n"
                        f"**Duration:** {duration_str}\n\n"
                        "Please choose your desired format:")
        
        await pre_check_message.edit_text(text=preview_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return CHOOSE_FORMAT
        
    except Exception as e:
        await pre_check_message.edit_text(f"❌ Could not process the link. It might be private or from an unsupported site.")
        return ConversationHandler.END

async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data['format_choice'] = query.data
    info_dict = context.user_data.get('info_dict')

    if query.data == 'mp3':
        # For audio, we don't need to ask for quality, just get the best
        await query.edit_message_text(text="Great! Preparing to get the best quality **MP3** for you.")
        await download_media(query.message.chat_id, context.user_data['url'], 'mp3', 'bestaudio', context)
        return ConversationHandler.END
    
    # For video, create quality selection buttons
    formats = info_dict.get('formats', [])
    quality_buttons = []
    unique_qualities = {}
    
    for f in formats:
        # Filter for mp4 files that contain both video and audio
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('ext') == 'mp4':
            height = f.get('height')
            format_id = f.get('format_id')
            # Use a dictionary to only get one button per resolution
            if height and height not in unique_qualities:
                unique_qualities[height] = format_id
    
    # Sort qualities from high to low
    sorted_qualities = sorted(unique_qualities.items(), key=lambda item: item[0], reverse=True)
    
    for height, format_id in sorted_qualities:
        quality_buttons.append([InlineKeyboardButton(f"{height}p", callback_data=format_id)])

    if not quality_buttons:
        await query.edit_message_text("Sorry, I couldn't find any standard MP4 video formats. Please try another link.")
        return ConversationHandler.END

    reply_markup = InlineKeyboardMarkup(quality_buttons)
    await query.edit_message_text("Please choose your desired video quality:", reply_markup=reply_markup)
    return CHOOSE_QUALITY

async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    quality_id = query.data
    url = context.user_data.get('url')
    format_choice = context.user_data.get('format_choice')

    if not url or not format_choice:
        await query.edit_message_text("Sorry, something went wrong. Please send the link again.")
        return ConversationHandler.END

    await query.edit_message_text(f"Perfect! Getting the video for you.")
    await download_media(query.message.chat_id, url, format_choice, quality_id, context)
    return ConversationHandler.END

# --- Standard Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (start command code remains the same)
    user_name = update.effective_user.first_name
    welcome_caption = (f"👋 Hello, {user_name}!\n\nI am the **Ultimate Media Downloader**, created by *{CREATOR_NAME}*.\n\n"
                       "Send me a link from YouTube, TikTok, Instagram, X (Twitter), and more to begin.")
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=WELCOME_IMAGE_URL, caption=welcome_caption,
                                 parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (help command code remains the same)
    help_text = ("**How to use this bot:**\n\n"
                 "1. Send me a link to a video.\n"
                 "2. I will show you a preview.\n"
                 "3. Choose your format (Video/Audio).\n"
                 "4. If you chose video, select a quality.\n"
                 "5. I'll download it. If it's too big, I'll send a link.\n\n"
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
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback)],
            CHOOSE_QUALITY: [CallbackQueryHandler(choose_quality_callback)], # NEW STATE
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
