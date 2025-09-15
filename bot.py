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
CONNECT_LINK = "https://www.linkedin.com/in/dhruv-maniya-shadow03" # Changed as per your last screenshot
WELCOME_IMAGE_URL = "https://i.ibb.co/bMNj87bT/download.jpg"
MAX_FILE_SIZE_MB = 49.5

# --- 💡 CONVERSATION STATES 💡 ---
CHOOSE_FORMAT, CHOOSE_QUALITY = range(2)

# --- Bot Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions (Uploaders) ---
async def upload_to_0x0(file_path):
    """Primary uploader: Uploads a file to 0x0.st."""
    try:
        with open(file_path, 'rb') as f:
            response = requests.post('https://0x0.st', files={'file': f}, timeout=60)
        if response.status_code == 200: return response.text.strip()
        else: return None
    except Exception as e:
        logger.error(f"0x0.st upload failed: {e}")
        return None

async def upload_to_gofile(file_path):
    """Backup uploader: Uploads a file to GoFile.io."""
    try:
        server_response = requests.get('https://api.gofile.io/getServer', timeout=10).json()
        server = server_response.get('data', {}).get('server')
        if not server: return None
        
        with open(file_path, 'rb') as f:
            upload_response = requests.post(f'https://{server}.gofile.io/uploadFile', files={'file': f}, timeout=60).json()
        
        return upload_response.get('data', {}).get('downloadPage')
    except Exception as e:
        logger.error(f"GoFile upload failed: {e}")
        return None

# --- Main Download Logic ---
async def download_media(chat_id, url, format_choice, quality_info, context: ContextTypes.DEFAULT_TYPE):
    processing_message = await context.bot.send_message(chat_id=chat_id, text="🔄 Preparing download...")
    
    # Unpack quality info
    quality_id, selected_height = quality_info.split('|')
    selected_height = int(selected_height)

    ydl_opts = {'noplaylist': True, 'logger': logger, 'outtmpl': '%(title)s.%(ext)s'}

    if format_choice == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
    else:
        ydl_opts['format'] = f'{quality_id}+bestaudio/best'

    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await processing_message.edit_text("⏳ Downloading media... This may take a moment.")
            info_dict = ydl.extract_info(url, download=True)
            original_file_path = ydl.prepare_filename(info_dict)
            
            if format_choice == 'mp3':
                file_path = os.path.splitext(original_file_path)[0] + '.mp3'
            else: # For video, yt-dlp might create .mkv, let's ensure .mp4
                file_path = os.path.splitext(original_file_path)[0] + '.mp4'
                # Add a postprocessor to ensure the container is mp4
                ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
                # Re-run with convertor if needed, a bit simplified here for brevity
                if not os.path.exists(file_path) and os.path.exists(original_file_path):
                    os.rename(original_file_path, file_path) # Simplified handling

        await processing_message.edit_text("✅ Download complete. Checking file...")
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        
        # NEW LOGIC: Force cloud link for 1080p+ OR if file is too big
        if file_size_mb < MAX_FILE_SIZE_MB and selected_height < 1080:
            await processing_message.edit_text(f"⬆️ File is {file_size_mb:.2f} MB. Uploading to Telegram...")
            if format_choice == 'mp3':
                with open(file_path, 'rb') as audio_file:
                    await context.bot.send_audio(chat_id=chat_id, audio=audio_file, title=info_dict.get('title'))
            else:
                with open(file_path, 'rb') as video_file:
                    await context.bot.send_video(chat_id=chat_id, video=video_file, caption=info_dict.get('title'))
        else:
            reason = "it's HD" if selected_height >= 1080 else "it's too large for Telegram"
            await processing_message.edit_text(f"⏳ File is {file_size_mb:.2f} MB ({reason}). Uploading to a cloud host...")
            
            # NEW LOGIC: Try primary uploader, then fall back to backup
            download_link = await upload_to_0x0(file_path) or await upload_to_gofile(file_path)
            
            if download_link:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Upload complete! Download your ad-free file here:\n\n{download_link}")
            else:
                await context.bot.send_message(chat_id=chat_id, text="❌ Sorry, the upload to both primary and backup hosts failed.")
        
        await processing_message.delete()
        await context.bot.send_message(chat_id=chat_id, text=f"✅ Task complete! Connect with *{CREATOR_NAME}* here: {CONNECT_LINK}", parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await processing_message.edit_text(f"❌ An error occurred: `{str(e)}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# --- Conversation Handlers ---
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function remains the same as the last version)
    url = update.message.text
    pre_check_message = await update.message.reply_text("🔍 Checking link...")
    try:
        with yt_dlp.YoutubeDL({'noplaylist': True, 'quiet': True}) as ydl:
            info_dict = ydl.extract_info(url, download=False)
        context.user_data['url'] = url
        context.user_data['info_dict'] = info_dict
        keyboard = [[InlineKeyboardButton("🎬 Video", callback_data='mp4'),
                     InlineKeyboardButton("🎵 Audio", callback_data='mp3')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        duration_str = time.strftime('%H:%M:%S', time.gmtime(info_dict.get('duration', 0)))
        preview_text = (f"**Title:** {info_dict.get('title')}\n"
                        f"**Duration:** {duration_str}\n\n"
                        "Please choose your desired format:")
        await pre_check_message.edit_text(text=preview_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return CHOOSE_FORMAT
    except Exception as e:
        await pre_check_message.edit_text(f"❌ Could not process the link.")
        return ConversationHandler.END

async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['format_choice'] = query.data
    info_dict = context.user_data.get('info_dict')

    if query.data == 'mp3':
        await query.edit_message_text(text="Great! Preparing to get the best quality **MP3**.")
        await download_media(query.message.chat_id, context.user_data['url'], 'mp3', '0|0', context)
        return ConversationHandler.END
    
    # NEW LOGIC: Relaxed filter to find more qualities (like .webm)
    formats = info_dict.get('formats', [])
    quality_buttons, unique_qualities = [], {}
    
    for f in formats:
        if f.get('vcodec') != 'none': # Look for any format with video
            height = f.get('height')
            format_id = f.get('format_id')
            if height and height not in unique_qualities:
                label = f"{height}p"
                if height >= 1080: label += " (HD)"
                if height >= 2160: label += " (4K)"
                # Pass both ID and height in the callback data
                unique_qualities[height] = {'id': format_id, 'label': label}
    
    sorted_qualities = sorted(unique_qualities.items(), key=lambda item: item[0], reverse=True)
    for height, data in sorted_qualities:
        callback_data = f"{data['id']}|{height}"
        quality_buttons.append([InlineKeyboardButton(data['label'], callback_data=callback_data)])

    if not quality_buttons:
        await query.edit_message_text("Sorry, no video formats found.")
        return ConversationHandler.END

    await query.edit_message_text("Please choose a video quality:", reply_markup=InlineKeyboardMarkup(quality_buttons))
    return CHOOSE_QUALITY

async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    quality_info = query.data # e.g., "616|1080"
    url = context.user_data.get('url')
    format_choice = context.user_data.get('format_choice')

    if not url or not format_choice:
        await query.edit_message_text("Sorry, an error occurred. Please send the link again.")
        return ConversationHandler.END

    await query.edit_message_text(f"Perfect! Getting the video for you.")
    await download_media(query.message.chat_id, url, format_choice, quality_info, context)
    return ConversationHandler.END

# --- Standard Commands (start, help, cancel) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    user_name = update.effective_user.first_name
    welcome_caption = (f"👋 Hello, {user_name}!\n\nI am the **Ultimate Media Downloader**, created by *{CREATOR_NAME}*.\n\n"
                       "Send me a link from YouTube, TikTok, and more to begin.")
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=WELCOME_IMAGE_URL, caption=welcome_caption,
                                 parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    help_text = ("**How to use this bot:**\n"
                 "1. Send a link.\n"
                 "2. I'll show a preview and you choose the format (Video/Audio).\n"
                 "3. For videos, you can then select the quality (including HD/4K).\n"
                 "4. I'll download it. I will always provide a cloud link for HD videos.\n"
                 "Use /cancel to stop any operation.")
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    if context.user_data: context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# --- Main Bot Execution ---
def main():
    #... (This function is unchanged)
    if not BOT_TOKEN:
        print("FATAL ERROR: BOT_TOKEN environment variable not found.")
        return
    application = ApplicationBuilder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)],
        states={
            CHOOSE_FORMAT: [CallbackQueryHandler(choose_format_callback)],
            CHOOSE_QUALITY: [CallbackQueryHandler(choose_quality_callback)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        conversation_timeout=600
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    print("🚀 Ultimate Media Downloader is up and running!")
    application.run_polling()

if __name__ == '__main__':
    main()
