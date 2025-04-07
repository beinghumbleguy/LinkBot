from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
import re
import asyncio
import logging
import os
import aiogram

# Enable logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("BOT_TOKEN not set!")
    raise ValueError("BOT_TOKEN is required")

# Initialize Bot and Dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Store the text to search for (in memory for simplicity)
search_text = None

# Command to set the text to search for
@dp.message(Command(commands=["settext"]))
async def set_search_text(message: types.Message):
    global search_text
    text = message.text.replace('/settext', '').strip()
    
    if not text:
        await message.answer("Usage: /settext <text>\nExample: /settext Early")
        logger.info("No text provided for /settext command")
        return
    
    search_text = text
    await message.answer(f"Search text set to: {search_text} ✅")
    logger.info(f"Search text set to: {search_text}")

# Function to process messages (used for both groups and channels)
async def process_message_with_buttons(message: types.Message):
    global search_text
    
    # Skip if no search text is set
    if not search_text:
        return
    
    text = message.text
    # Check if the search text is present in the message (case-insensitive)
    if search_text.lower() in text.lower():
        # Extract CA from the message (assuming it's a 44-character Solana address)
        ca_match = re.search(r'[A-Za-z0-9]{44}', text)
        if not ca_match:
            logger.info(f"Search text '{search_text}' found, but no CA in message: {text}")
            return
        ca = ca_match.group(0)
        output_text = f"🔗 CA: `{ca}`"  # New text with "Button"
        # Create buttons
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Axiom", url=f"https://axiom.trade/t/{ca}/@lucidswan")
            ]
        ])
        
        # Reply with the message text and buttons
        # For channels, we can't use reply_to_message_id directly, so we send a new message
        if message.chat.type == "channel":
            await bot.send_message(
                chat_id=message.chat.id,
                text=output_text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            await message.reply(
                text=output_text,
                reply_markup=keyboard,
                parse_mode="Markdown",
                reply_to_message_id=message.message_id
            )
        logger.info(f"Added buttons for message containing '{search_text}' and CA: {ca}")

# Handler for messages in groups (message updates)
@dp.message(F.text)
async def add_buttons_if_text_found(message: types.Message):
    await process_message_with_buttons(message)

# Handler for messages in channels (channel_post updates)
@dp.channel_post(F.text)
async def add_buttons_if_text_found_in_channel(channel_post: types.Message):
    await process_message_with_buttons(channel_post)

# Startup function
async def on_startup():
    logger.info("Button Bot started")

# Main function to start the bot
async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
