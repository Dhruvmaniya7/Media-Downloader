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

# --- ✨ ANIMATIONS & UI ✨ ---
SPINNER_FRAMES = ["⢿", "⣻", "⣽", "⣾", "⣷", "⣯", "⣟", "⡿"]

# --- 💡 CONVERSATION STATES 💡 ---
CHOOSE_FORMAT, CHOOSE_QUALITY, ASK_RENAME, GET_NEW_NAME = range(4)

# --- Bot Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- UI & Helper Functions ---
def format_time(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

def generate_progress_bar(percent):
    filled_length = int(10 * percent // 100)
    return '█' * filled_length + '░' * (10 - filled_length)

async def update_status_message(context, message, status_text, start_time, percent=0, speed="", eta=""):
    now = time.monotonic()
    if now - context.bot_data.get('last_update_time', 0) < 1.5 and percent < 99: return
    elapsed_time = format_time(now - start_time)
    spinner = SPINNER_FRAMES[int(now * 10) % len(SPINNER_FRAMES)]
    text = f"`{spinner}` *{status_text}*\n\n"
    if percent > 0:
        bar = generate_progress_bar(percent)
        text += f"`[{bar}] {percent:.1f}%`\n`Speed:` {speed}\n`ETA:` {eta}\n"
    text += f"`Time:` {elapsed_time}"
    try:
        await context.bot.edit_message_text(text=text, chat_id=message.chat_id,
                                            message_id=message.message_id, parse_mode=ParseMode.MARKDOWN)
        context.bot_data['last_update_time'] = now
    except: pass

async def upload_to_0x0(file_path):
    try:
        with open(file_path, 'rb') as f:
            response = requests.post('https://0x0.st', files={'file': f}, timeout=60)
        return response.text.strip() if response.status_code == 200 else None
    except: return None

async def upload_to_gofile(file_path):
    try:
        server = requests.get('https://api.gofile.io/getServer', timeout=10).json().get('data', {}).get('server')
        if not server: return None
        with open(file_path, 'rb') as f:
            response = requests.post(f'https://{server}.gofile.io/uploadFile', files={'file': f}, timeout=60).json()
        return response.get('data', {}).get('downloadPage')
    except: return None

# --- Main Download Logic ---
async def download_media(chat_id, url, format_choice, quality_id, custom_filename, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.monotonic()
    status_message = await context.bot.send_message(chat_id=chat_id, text="`⢿` *Initializing...*")

    def progress_hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').replace('%','').strip()
            speed = d.get('_speed_str', 'N/A').strip()
            eta = d.get('_eta_str', 'N/A').strip()
            asyncio.ensure_future(update_status_message(
                context, status_message, "Downloading", start_time, float(percent), speed, eta))
    
    ydl_opts = {'noplaylist': True, 'logger': logger, 'progress_hooks': [progress_hook],
                'outtmpl': f"{custom_filename}.%(ext)s" if custom_filename else '%(title)s.%(ext)s'}

    if format_choice == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
    else:
        ydl_opts['format'] = f'{quality_id}+bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]

    file_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            original_file_path = ydl.prepare_filename(info_dict)
            file_path = os.path.splitext(original_file_path)[0] + f'.{format_choice}'
            if not os.path.exists(file_path) and os.path.exists(original_file_path):
                os.rename(original_file_path, file_path)

        await update_status_message(context, status_message, "Processing File", start_time, percent=100)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        total_time = format_time(time.monotonic() - start_time)
        
        # --- LOGIC FIX STARTS HERE ---
        if file_size_mb < MAX_FILE_SIZE_MB:
            await update_status_message(context, status_message, "Uploading to Telegram", start_time, percent=100)
            if format_choice == 'mp3':
                with open(file_path, 'rb') as f: await context.bot.send_audio(chat_id=chat_id, audio=f, title=info_dict.get('title'))
            else:
                with open(file_path, 'rb') as f: await context.bot.send_video(chat_id=chat_id, video=f, caption=info_dict.get('title'))
            
            # For small files, delete the status message and send a NEW summary message
            await status_message.delete()
            final_summary = (f"✅ **Task Complete!**\n\n"
                             f"📄 `{os.path.basename(file_path)}`\n"
                             f"📦 `Size: {file_size_mb:.2f} MB`\n"
                             f"⏱️ `Total Time: {total_time}`\n\n"
                             f"Connect with *{CREATOR_NAME}* here: {CONNECT_LINK}")
            await context.bot.send_message(chat_id=chat_id, text=final_summary, parse_mode=ParseMode.MARKDOWN)

        else:
            await update_status_message(context, status_message, "Uploading to Cloud Host", start_time, percent=100)
            download_link = await upload_to_0x0(file_path) or await upload_to_gofile(file_path)
            
            if download_link:
                # For large files, build the summary and EDIT the status message with the link
                final_summary = (f"✅ **Task Complete!**\n\n"
                                 f"File was too large for Telegram.\n"
                                 f"📦 `Size: {file_size_mb:.2f} MB`\n"
                                 f"⏱️ `Total Time: {total_time}`\n\n"
                                 f"🔗 **Download Link:** {download_link}")
                await status_message.edit_text(text=final_summary, parse_mode=ParseMode.MARKDOWN)
            else:
                await status_message.edit_text(text="❌ Sorry, the upload to both primary and backup hosts failed.", parse_mode=ParseMode.MARKDOWN)
        # --- LOGIC FIX ENDS HERE ---

    except Exception as e:
        await status_message.edit_text(f"❌ **An error occurred**\n\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# --- Conversation Handlers ---
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    url = update.message.text
    pre_check_message = await update.message.reply_text("🔍 Checking link...")
    try:
        with yt_dlp.YoutubeDL({'noplaylist': True, 'quiet': True}) as ydl:
            info_dict = ydl.extract_info(url, download=False)
        context.user_data.update({'url': url, 'info_dict': info_dict})
        keyboard = [[InlineKeyboardButton("🎬 Video (MP4)", callback_data='mp4'),
                     InlineKeyboardButton("🎵 Audio (MP3)", callback_data='mp3')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        duration_str = time.strftime('%H:%M:%S', time.gmtime(info_dict.get('duration', 0)))
        preview_text = (f"**Title:** {info_dict.get('title')}\n"
                        f"**Duration:** {duration_str}\n\n"
                        "Please choose your desired format:")
        await pre_check_message.edit_text(text=preview_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return CHOOSE_FORMAT
    except:
        await pre_check_message.edit_text(f"❌ Could not process the link.")
        return ConversationHandler.END

async def choose_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    query = update.callback_query
    await query.answer()
    context.user_data['format_choice'] = query.data
    info_dict = context.user_data.get('info_dict')
    if query.data == 'mp3':
        context.user_data['quality_id'] = 'bestaudio'
        keyboard = [[InlineKeyboardButton("✅ Keep Original Title", callback_data='keep'),
                     InlineKeyboardButton("✏️ Rename File", callback_data='rename')]]
        await query.edit_message_text(text=f"Default name: `{info_dict.get('title')}`. Do you want to rename it?", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return ASK_RENAME
    
    formats, quality_buttons, unique_qualities = info_dict.get('formats', []), [], {}
    for f in formats:
        if f.get('vcodec') != 'none' and f.get('acodec') == 'none':
            height, format_id = f.get('height'), f.get('format_id')
            if height and height not in unique_qualities: unique_qualities[height] = format_id
    quality_buttons.append([InlineKeyboardButton("Best Available (up to 720p)", callback_data="best|720")])
    for height, format_id in sorted(unique_qualities.items(), reverse=True):
        label = f"{height}p"
        if height >= 1080: label += " (HD)"
        if height >= 2160: label += " (4K)"
        quality_buttons.append([InlineKeyboardButton(label, callback_data=f"{format_id}|{height}")])
    
    await query.edit_message_text("Please choose a video quality:", reply_markup=InlineKeyboardMarkup(quality_buttons))
    return CHOOSE_QUALITY

async def choose_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    query = update.callback_query
    await query.answer()
    context.user_data['quality_id'] = query.data.split('|')[0]
    info_dict = context.user_data.get('info_dict')
    keyboard = [[InlineKeyboardButton("✅ Keep Original Title", callback_data='keep'),
                 InlineKeyboardButton("✏️ Rename File", callback_data='rename')]]
    await query.edit_message_text(text=f"Default name: `{info_dict.get('title')}`. Do you want to rename it?", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ASK_RENAME

async def ask_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    query = update.callback_query
    await query.answer()
    if query.data == 'keep':
        await query.edit_message_text("Perfect! Starting download.")
        await download_media(query.message.chat_id, context.user_data['url'], context.user_data['format_choice'], 
                             context.user_data['quality_id'], None, context)
        return ConversationHandler.END
    else: # rename
        await query.edit_message_text("Okay, please send me the new name for the file.")
        return GET_NEW_NAME

async def get_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    #... (This function is unchanged)
    custom_filename = re.sub(r'[\\/*?:"<>|]', "", update.message.text.strip())
    await update.message.reply_text(f"Got it! I'll name the file: `{custom_filename}`", parse_mode=ParseMode.MARKDOWN)
    await download_media(update.message.chat_id, context.user_data['url'], context.user_data['format_choice'], 
                         context.user_data['quality_id'], custom_filename, context)
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
    help_text = ("**How to use this bot:**\n\n"
                 "1. Send a link from a supported site.\n"
                 "2. Choose your format (Video/Audio) and quality.\n"
                 "3. Decide if you want to rename the file.\n"
                 "4. I'll download it, showing a live progress bar. Large files will be sent as a cloud link.\n\n"
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
            ASK_RENAME: [CallbackQueryHandler(ask_rename_callback)],
            GET_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_name)],
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
