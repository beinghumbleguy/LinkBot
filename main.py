from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
import re
import asyncio
import logging
import os
import cloudscraper
import json
import time
from fake_useragent import UserAgent
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from datetime import datetime
import pytz
import aiofiles
import math
import urllib.parse

# Enable the detailed logging now
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

## Create data directory
os.makedirs("/app/data", exist_ok=True)

# #Load environment variables
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("BOT_TOKEN not set!")
    raise ValueError("BOT_TOKEN is required")

# Initialize Bot and Dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Store the text to search for (in memory for simplicity)
search_text = None

# Growth check variables
growth_notifications_enabled = True
GROWTH_THRESHOLD = 1.5  # Notify when growth reaches 1.5x
INCREMENT_THRESHOLD = 1.0  # Notify at increments of 1.0 (e.g., 1.5x, 2.0x, 3.0x)
CHECK_INTERVAL = 45  # Seconds between growth checks
MONITORING_DURATION = 21600  # 6 hours in seconds
monitored_tokens = {}  # Format: {key: {"mint_address": str, "chat_id": int, "initial_mc": float, "timestamp": float, "token_name": str, "symbol": str, "message_id": int}}
last_growth_ratios = {}  # Tracks last notified growth ratio per CA
csv_lock = asyncio.Lock()  # Lock for CSV writes test

# Define channel IDs
VIP_CHAT_ID = -1002625241334  # Lucid Labs VIP channel ID
PUBLIC_CHANNEL_ID = -1002366446172  # Public channel ID
CSV_PATH = "/app/data/lucidswans_monitored_tokens.csv"  # Path to store monitored tokens

# Daily report scheduling variable
DAILY_REPORT_INTERVAL = 14400  # Seconds between reports (14400 = 4 hours)

def calculate_time_since(timestamp):
    """Format time difference since timestamp in seconds, minutes, or hours:minutes."""
    current_time = datetime.now(pytz.timezone('America/New_York'))
    token_time = datetime.fromtimestamp(timestamp, pytz.timezone('America/New_York'))
    diff_seconds = int((current_time - token_time).total_seconds())
    if diff_seconds < 60:
        return f"{diff_seconds}s"
    minutes = diff_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h:{remaining_minutes:02d}m"

def load_monitored_tokens():
    """Load monitored tokens from CSV on startup with validation."""
    global monitored_tokens, last_growth_ratios
    if os.path.exists(CSV_PATH):
        try:
            df = pd.read_csv(CSV_PATH)
            required_columns = ["mint_address", "chat_id", "initial_mc", "timestamp", "token_name", "message_id"]
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                logger.error(f"CSV missing required columns: {missing_columns}")
                return

            for idx, row in df.iterrows():
                if pd.isna(row["mint_address"]) or pd.isna(row["chat_id"]) or pd.isna(row["initial_mc"]) or pd.isna(row["timestamp"]) or pd.isna(row["message_id"]):
                    logger.warning(f"Skipping invalid CSV row {idx}: missing required fields {row.to_dict()}")
                    continue

                key = f"{row['mint_address']}:{row['chat_id']}"
                try:
                    monitored_tokens[key] = {
                        "mint_address": str(row["mint_address"]),
                        "chat_id": int(row["chat_id"]),
                        "initial_mc": float(row["initial_mc"]),
                        "timestamp": float(row["timestamp"]),
                        "token_name": str(row["token_name"]),
                        "symbol": str(row.get("symbol", "")),
                        "message_id": int(row["message_id"])
                    }
                    last_growth_ratios[key] = float(row.get("last_growth_ratio", 1.0))
                    logger.debug(f"Loaded token {row['mint_address']}: last_growth_ratio={last_growth_ratios[key]}")
                except (ValueError, TypeError) as e:
                    logger.warning(f"Skipping invalid CSV row {idx}: invalid data types {row.to_dict()}, error: {str(e)}")
                    continue
            logger.info(f"Loaded {len(monitored_tokens)} tokens from {CSV_PATH}")
        except Exception as e:
            logger.error(f"Failed to load monitored tokens from CSV: {str(e)}")
    else:
        logger.info(f"No existing monitored tokens CSV found at {CSV_PATH}")

async def save_monitored_tokens():
    """Save monitored tokens to CSV with lock to prevent concurrent writes."""
    async with csv_lock:
        try:
            df = pd.DataFrame([
                {
                    "mint_address": data["mint_address"],
                    "chat_id": data["chat_id"],
                    "initial_mc": data["initial_mc"],
                    "timestamp": data["timestamp"],
                    "token_name": data["token_name"],
                    "symbol": data["symbol"],
                    "message_id": data["message_id"],
                    "last_growth_ratio": last_growth_ratios.get(key, 1.0)
                }
                for key, data in monitored_tokens.items()
            ])
            logger.debug(f"Saving CSV with {len(df)} tokens: {df[['mint_address', 'symbol', 'last_growth_ratio']].to_dict('records')}")
            df.to_csv(CSV_PATH, index=False)
            logger.info(f"Saved {len(monitored_tokens)} tokens to {CSV_PATH}")
        except Exception as e:
            logger.error(f"Failed to save monitored tokens to CSV: {str(e)}")
            raise

async def add_to_monitored_tokens(mint_address: str, chat_id: int, initial_mc: float, token_name: str, symbol: str, message_id: int):
    """Add a CA to monitored tokens and save to CSV, validate initial_mc."""
    if initial_mc <= 0:
        logger.warning(f"Skipping CA {mint_address} for monitoring: invalid initial_mc={initial_mc}")
        return

    key = f"{mint_address}:{chat_id}"
    if key not in monitored_tokens:
        monitored_tokens[key] = {
            "mint_address": mint_address,
            "chat_id": chat_id,
            "initial_mc": initial_mc,
            "timestamp": time.time(),
            "token_name": token_name,
            "symbol": symbol,
            "message_id": message_id
        }
        last_growth_ratios[key] = 1.0  # Initialize last notified ratio
        await save_monitored_tokens()
        logger.info(f"Added CA {mint_address} to monitored_tokens for chat {chat_id}, initial_mc={initial_mc}")
    else:
        logger.debug(f"CA {mint_address} already in monitored_tokens for chat {chat_id}")

async def generate_growth_report(report_type: str = "daily"):
    """Generate a growth report for top 20 VIP tokens (> 2.0x growth) added today."""
    logger.info(f"Generating {report_type} growth report")
    current_time = datetime.now(pytz.timezone('America/New_York'))
    today_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ts = today_start.timestamp()
    date_str = current_time.strftime("%d/%m/%Y")
    logger.debug(f"Report for date {date_str}, today_start_ts={today_start_ts} ({today_start})")

    qualifying_tokens = []
    total_tokens_evaluated = 0
    logger.debug(f"Evaluating {len(monitored_tokens)} tokens in monitored_tokens")

    for key, data in monitored_tokens.items():
        ca = data["mint_address"]
        total_tokens_evaluated += 1
        token_time = datetime.fromtimestamp(data["timestamp"], pytz.timezone('America/New_York'))
        logger.debug(f"Evaluating CA {ca}: chat_id={data['chat_id']}, timestamp={data['timestamp']} ({token_time}), initial_mc={data['initial_mc']:.2f}")

        if data["chat_id"] != VIP_CHAT_ID:
            logger.debug(f"Skipping CA {ca}: not in VIP channel (chat_id={data['chat_id']} != {VIP_CHAT_ID})")
            continue

        if data["timestamp"] < today_start_ts:
            logger.debug(f"Skipping CA {ca}: not added today (timestamp={data['timestamp']} ({token_time}) < today_start_ts={today_start_ts} ({today_start}))")
            continue

        initial_mc = data["initial_mc"]
        if initial_mc <= 0:
            logger.warning(f"Skipping CA {ca}: invalid initial_mc={initial_mc}")
            continue

        token_data = None
        for attempt in range(5):
            token_data = await get_gmgn_token_data(ca)
            if "error" not in token_data:
                break
            logger.debug(f"Attempt {attempt + 1} failed for CA {ca}: {token_data['error']}")
            await asyncio.sleep(2 ** attempt)

        if "error" in token_data:
            logger.warning(f"Skipping CA {ca} after 5 attempts: {token_data['error']}, token_data={data}")
            continue

        current_mc = token_data.get("market_cap", 0)
        if current_mc == 0:
            logger.warning(f"Skipping CA {ca}: invalid current_mc={current_mc}, token_data={data}")
            continue

        growth_ratio = current_mc / initial_mc if initial_mc != 0 else 0
        last_ratio = last_growth_ratios.get(key, 1.0)
        # Use last_growth_ratio if higher (to reflect peak notified growth)
        growth_ratio = max(growth_ratio, last_ratio)
        logger.debug(f"CA {ca}: initial_mc={initial_mc:.2f}, current_mc={current_mc:.2f}, calculated_growth_ratio={current_mc/initial_mc:.2f}x, last_growth_ratio={last_ratio:.2f}x, reported_growth_ratio={growth_ratio:.2f}x")

        if growth_ratio > 2.0:
            qualifying_tokens.append({
                "symbol": data["symbol"] or "Unknown",
                "ca": ca,
                "growth_ratio": growth_ratio,
                "token_name": data["token_name"]
            })
            logger.debug(f"Added CA {ca} to report: symbol={data['symbol']}, growth_ratio={growth_ratio:.2f}x, token_name={data['token_name']}")
        else:
            logger.debug(f"Skipping CA {ca}: growth_ratio={growth_ratio:.2f}x <= 2.0")

    logger.info(f"Evaluated {total_tokens_evaluated} tokens, found {len(qualifying_tokens)} qualifying tokens for {report_type} report")

    qualifying_tokens.sort(key=lambda x: x["growth_ratio"], reverse=True)
    qualifying_tokens = qualifying_tokens[:20]
    logger.info(f"Selected top {len(qualifying_tokens)} qualifying tokens for {report_type} report")

    if not qualifying_tokens:
        logger.info(f"No qualifying tokens for {report_type} report, skipping")
        return None, None

    report_lines = []
    for idx, token in enumerate(qualifying_tokens, 1):
        symbol = f"${token['symbol']}" if token["symbol"] != "Unknown" else token["token_name"]
        ca = token["ca"]
        growth_ratio = token["growth_ratio"]
        pump_fun_url = f"https://pump.fun/coin/{ca}"
        rank_emoji = "🏆" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "🏅"
        prefix = "└" if idx == len(qualifying_tokens) else "├"
        report_lines.append(
            f"{prefix}{rank_emoji} 👀 | 🔗 | [{symbol}]({pump_fun_url}) | {growth_ratio:.1f}x"
        )

    report_text = (
        f"📈 **Top Performing VIP Tokens** 📈\n"
        f"📅 {date_str}\n\n"
        + "\n".join(report_lines) + "\n\n"
        f"Join our VIP channel for more gains! 💰"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
      ##  [InlineKeyboardButton(text="🌟 Join VIP 🌟", url="https://t.me/DegenSwansVIP_bot?start=start")]
    ])

    return report_text, keyboard

async def daily_summary_report():
    """Generate and post a daily report of top 20 VIP tokens (> 2.0x growth) added today."""
    logger.debug("Triggering daily summary report")
    report_text, keyboard = await generate_growth_report(report_type="daily")
    if not report_text:
        return

    try:
        message = await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=report_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=keyboard
        )
        logger.info(f"Posted daily summary report to public channel {PUBLIC_CHANNEL_ID}, message_id={message.message_id}")

        await bot.pin_chat_message(
            chat_id=PUBLIC_CHANNEL_ID,
            message_id=message.message_id,
            disable_notification=True
        )
        logger.info(f"Pinned daily summary report message {message.message_id} in public channel {PUBLIC_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Failed to post or pin daily summary report: {str(e)}")

async def schedule_daily_report():
    """Schedule the daily report to run every DAILY_REPORT_INTERVAL seconds."""
    logger.info(f"Scheduling daily report to run every {DAILY_REPORT_INTERVAL} seconds")
    while True:
        await daily_summary_report()
        await asyncio.sleep(DAILY_REPORT_INTERVAL)

@dp.message(Command(commands=["growthreport"]))
async def on_demand_growth_report(message: types.Message):
    """Handle /growthreport command to generate an on-demand growth report."""
    chat_id = message.chat.id
    user_id = message.from_user.id
    logger.info(f"Received /growthreport command from user {user_id} in chat {chat_id}")

    is_admin = False
    try:
        for target_chat_id in [VIP_CHAT_ID, PUBLIC_CHANNEL_ID]:
            admins = await bot.get_chat_administrators(target_chat_id)
            admin_ids = [admin.user.id for admin in admins]
            if user_id in admin_ids:
                is_admin = True
                logger.debug(f"User {user_id} is an admin in chat {target_chat_id}")
                break
    except Exception as e:
        logger.error(f"Failed to check admin status for user {user_id} in chats {VIP_CHAT_ID}, {PUBLIC_CHANNEL_ID}: {str(e)}")
        await message.answer("⚠️ Error checking admin status. Please try again later.")
        return

    if not is_admin:
        logger.info(f"User {user_id} is not an admin in VIP or public channel, rejecting /growthreport")
        await message.answer("⚠️ You must be an admin in the VIP or public channel to use this command.")
        return

    logger.debug("Triggering on-demand growth report")
    report_text, keyboard = await generate_growth_report(report_type="on-demand")
    if not report_text:
        await message.answer("No qualifying tokens found for the on-demand growth report.")
        logger.info("No qualifying tokens for on-demand report, notified user")
        return

    try:
        report_message = await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=report_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=keyboard
        )
        logger.info(f"Posted on-demand growth report to public channel {PUBLIC_CHANNEL_ID}, message_id={report_message.message_id}")

        await bot.pin_chat_message(
            chat_id=PUBLIC_CHANNEL_ID,
            message_id=report_message.message_id,
            disable_notification=True
        )
        logger.info(f"Pinned on-demand growth report message {report_message.message_id} in public channel {PUBLIC_CHANNEL_ID}")

        await message.answer("✅ On-demand growth report posted successfully!")
    except Exception as e:
        logger.error(f"Failed to post or pin on-demand growth report: {str(e)}")
        await message.answer(f"⚠️ Failed to post on-demand growth report: {str(e)}")

async def growthcheck():
    """Periodically check market cap growth and notify for milestones (1.5x, 2.0x, 3.0x, etc.) in VIP channel."""
    while True:
        if not monitored_tokens:
            logger.debug("No tokens to monitor, skipping growth check")
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        logger.debug(f"Starting growthcheck with monitored_tokens: {len(monitored_tokens)} tokens")
        to_remove = []
        updated_tokens = False

        for key in list(monitored_tokens.keys()):
            data = monitored_tokens.get(key)
            if not data:
                continue

            ca = data["mint_address"]
            chat_id = data["chat_id"]
            initial_mc = data["initial_mc"]
            token_name = data["token_name"]
            symbol = data["symbol"]
            message_id = data["message_id"]
            timestamp = data["timestamp"]

            if chat_id != VIP_CHAT_ID:
                logger.debug(f"Skipping CA {ca} in chat {chat_id} (not VIP)")
                continue

            current_time = datetime.now(pytz.timezone('America/New_York'))
            token_time = datetime.fromtimestamp(timestamp, pytz.timezone('America/New_York'))
            time_diff = (current_time - token_time).total_seconds()
            if time_diff > MONITORING_DURATION:
                logger.info(f"Removing CA {ca} from monitoring: exceeded 6 hours")
                to_remove.append(key)
                continue

            token_data = await get_gmgn_token_data(ca)
            if "error" in token_data:
                logger.debug(f"Removing CA {ca} due to API error: {token_data['error']}")
                to_remove.append(key)
                continue

            current_mc = token_data.get("market_cap", 0)
            if current_mc == 0:
                logger.debug(f"Skipping CA {ca} due to invalid current_mc: {current_mc}")
                to_remove.append(key)
                continue

            growth_ratio = current_mc / initial_mc if initial_mc != 0 else 0
            logger.debug(f"CA {ca}: initial_mc={initial_mc:.2f}, current_mc={current_mc:.2f}, growth_ratio={growth_ratio:.2f}x")

            initial_mc_str = f"{initial_mc / 1000:.1f}K" if initial_mc < 1_000_000 else f"{initial_mc / 1_000_000:.1f}M"
            current_mc_str = f"{current_mc / 1000:.1f}K" if current_mc < 1_000_000 else f"{current_mc / 1_000_000:.1f}M"

            last_ratio = last_growth_ratios.get(key, 1.0)
            next_increment = last_ratio + INCREMENT_THRESHOLD
            if (growth_notifications_enabled and 
                growth_ratio >= GROWTH_THRESHOLD and 
                growth_ratio >= next_increment and 
                math.floor(growth_ratio) > math.floor(last_ratio)):
                last_growth_ratios[key] = growth_ratio
                updated_tokens = True
                logger.info(f"Triggered notification for CA {ca}: growth_ratio={growth_ratio:.2f}x, last_ratio={last_ratio:.2f}x, next_increment={next_increment:.2f}")
                logger.info(f"Updated last_growth_ratio for CA {ca} to {growth_ratio:.2f}x (previous: {last_ratio:.2f}x) due to notification")
                time_since_added = calculate_time_since(timestamp)
                initial_mc_str_md = f"**{initial_mc / 1000:.1f}K**" if initial_mc < 1_000_000 else f"**{initial_mc / 1_000_000:.1f}M**"
                current_mc_str_md = f"**{current_mc / 1000:.1f}K**" if current_mc < 1_000_000 else f"**{current_mc / 1_000_000:.1f}M**"
                emoji = "🚀" if 2 <= growth_ratio < 5 else "🔥" if 5 <= growth_ratio < 10 else "🌙"
                growth_str = f"**{growth_ratio:.1f}x**"
                symbol_display = f" - ${symbol}" if symbol else ""

                growth_message = (
                    f"{emoji} {growth_str} | "
                    f"💹From {initial_mc_str_md} ↗️ {current_mc_str_md} within **{time_since_added}**{symbol_display}"
                )

                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=growth_message,
                        parse_mode="Markdown",
                        reply_to_message_id=message_id
                    )
                    logger.info(f"Sent growth notification for CA {ca} in chat {chat_id}: {growth_message}")
                except Exception as e:
                    logger.error(f"Failed to send growth notification for CA {ca} in chat {chat_id}: {e}")
            else:
                skip_reasons = []
                if not growth_notifications_enabled:
                    skip_reasons.append("notifications disabled")
                if growth_ratio < GROWTH_THRESHOLD:
                    skip_reasons.append(f"growth_ratio={growth_ratio:.2f} < threshold={GROWTH_THRESHOLD}")
                if growth_ratio < next_increment:
                    skip_reasons.append(f"growth_ratio={growth_ratio:.2f} < last_ratio={last_ratio:.2f} + increment={INCREMENT_THRESHOLD}")
                if math.floor(growth_ratio) <= math.floor(last_ratio):
                    skip_reasons.append(f"floor(growth_ratio)={math.floor(growth_ratio)} <= floor(last_ratio)={math.floor(last_ratio)}")
                logger.debug(f"Skipped notification for CA {ca}: {', '.join(skip_reasons)}, threshold={GROWTH_THRESHOLD}, next_increment={next_increment:.2f}")

            profit_percent = ((current_mc - initial_mc) / initial_mc) * 100 if initial_mc != 0 else 0
            logger.debug(f"CA {ca}: Initial MC={initial_mc_str}, Current MC={current_mc_str}, Growth={profit_percent:.2f}%")

        for key in to_remove:
            monitored_tokens.pop(key, None)
            last_growth_ratios.pop(key, None)
            updated_tokens = True
            logger.info(f"Removed token {key} from monitoring")
        if to_remove:
            logger.info(f"Removed {len(to_remove)} expired tokens")

        if updated_tokens:
            await save_monitored_tokens()
            logger.info(f"Saved CSV after updating last_growth_ratio or removing tokens")

        await asyncio.sleep(CHECK_INTERVAL)

class APISessionManager:
    def __init__(self):
        self.session = None
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.ua = UserAgent()
        self.max_retries = 5
        self.retry_delay = 2

        self.headers_dict = {
            "Accept": "application/json",
            "User-Agent": self.ua.random,
            #"x-cg-demo-api-key": os.getenv("COINGECKO_API_KEY", "CG-YOUR-API-KEY")
            "x-cg-pro-api-key": os.getenv("COINGECKO_API_KEY", "CG-YOUR-API-KEY")
        }

    async def _run_in_executor(self, func, *args, **kwargs):
        return await asyncio.get_event_loop().run_in_executor(
            self._executor,
            lambda: func(*args, **kwargs)
        )

    async def fetch_token_data(self, mint_address):
        #url = f"https://api.coingecko.com/api/v3/onchain/networks/solana/tokens/multi/{mint_address}"
        url = f"https://pro-api.coingecko.com/api/v3/onchain/networks/solana/tokens/multi/{mint_address}"
        for attempt in range(self.max_retries):
            try:
                if not self.session:
                    self.session = cloudscraper.create_scraper()
                response = await self._run_in_executor(
                    self.session.get,
                    url,
                    headers=self.headers_dict,
                    timeout=15
                )
                if response.status_code != 200:
                    logger.warning(f"Attempt {attempt+1} failed: {response.status_code} {response.text[:200]}")
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                    continue

                json_data = response.json()
                logger.debug(f"Raw CoinGecko response for {mint_address}: {str(json_data)[:500]}")

                if "data" not in json_data or not isinstance(json_data["data"], list) or not json_data["data"]:
                    return {"error": f"No token data returned for {mint_address}"}

                attributes = json_data["data"][0].get("attributes", {})
                if not attributes:
                    return {"error": f"No attributes for {mint_address}"}

                # Map fields
                price = float(attributes.get("price_usd") or 0)
                market_cap = float(attributes.get("market_cap_usd") or attributes.get("fdv_usd") or 0)
                liquidity = float(attributes.get("total_reserve_in_usd") or 0)

                # Handle volume_usd.h24 safely
                volume_usd = attributes.get("volume_usd", {})
                volume_24h = 0.0
                if isinstance(volume_usd, dict) and "h24" in volume_usd:
                    try:
                        volume_24h = float(volume_usd["h24"])
                    except (TypeError, ValueError):
                        logger.warning(f"Invalid h24 volume value for {mint_address}: {volume_usd['h24']}")

                token_data = {
                    "price": price,
                    "market_cap": market_cap,
                    "market_cap_str": format_market_cap(market_cap),
                    "liquidity": liquidity,
                    "liquidity_str": format_market_cap(liquidity),
                    "volume_24h": volume_24h,
                    # "swaps_24h": not available in CoinGecko response
                    # "top_10_holder_rate": not available in CoinGecko response
                    # "renounced_mint": not available in CoinGecko response
                    # "renounced_freeze_account": not available in CoinGecko response
                    "contract": attributes.get("address", mint_address),
                    "name": attributes.get("name", "Unknown"),
                    "symbol": attributes.get("symbol", ""),
                    # "hot_level": not available in CoinGecko response
                }
                return token_data

            except Exception as e:
                logger.error(f"Attempt {attempt+1} for {mint_address} failed: {str(e)}")
                await asyncio.sleep(self.retry_delay * (2 ** attempt))

        return {"error": f"Failed to fetch data for {mint_address} after retries"}


api_session_manager = APISessionManager()


def format_market_cap(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.2f}K"
    elif value > 0:
        return f"{value:.2f}"
    return "N/A"

def format_price(price: float) -> str:
    if price == 0:
        return "N/A"
    if price < 0.0001:
        return f"{price:.7f}".rstrip('0').rstrip('.')
    return f"{price:.6f}".rstrip('0').rstrip('.')

def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"{volume / 1_000:.2f}K"
    elif volume > 0:
        return f"{volume:.2f}"
    return "N/A"

def calculate_percentage_change(current: float, previous: float) -> str:
    if previous == 0 or current == 0:
        return "N/A"
    change = ((current - previous) / previous) * 100
    return f"{change:+.2f}%"

def get_hot_level_emoji(hot_level: int) -> str:
    """Return an emoji based on the hot_level value."""
    if hot_level == 0:
        return "🥶"
    elif hot_level == 1:
        return "😎"
    elif hot_level == 2:
        return "🔥"
    elif hot_level >= 3:
        return "🌋"
    return "❓"  # For invalid or negative values

async def get_gmgn_token_data(mint_address):
    token_data_raw = await api_session_manager.fetch_token_data(mint_address)
    logger.debug(f"Received raw token data for CA {mint_address}: {token_data_raw}")

    if "error" in token_data_raw:
        logger.error(f"Error from fetch_token_data for CA {mint_address}: {token_data_raw['error']}")
        return {"error": token_data_raw["error"]}

    # ✅ With CoinGecko integration, fetch_token_data already returns final dict
    return token_data_raw

    try:
        # #Handle Cloudflare challenge indicators
        if "cf-ray" in token_data_raw.get("headers", {}):
            logger.info(f"Cloudflare headers detected for CA {mint_address}, possible CAPTCHA challenge")

        # Check if response is a single token object or wrapped in 'data' with 'tokens' list
        token_info = None
        if isinstance(token_data_raw, dict) and "data" in token_data_raw and isinstance(token_data_raw["data"], dict) and "tokens" in token_data_raw["data"]:
            if not isinstance(token_data_raw["data"]["tokens"], list):
                logger.warning(f"Invalid 'tokens' key in response for CA {mint_address}: {token_data_raw}")
                return {"error": f"No valid token data returned from API for CA {mint_address}"}
            if len(token_data_raw["data"]["tokens"]) == 0:
                logger.warning(f"No tokens found in API response for CA {mint_address}: {token_data_raw}")
                return {"error": f"Token not found for CA {mint_address} on gmgn.ai. Please verify the contract address."}
            token_info = token_data_raw["data"]["tokens"][0]
        elif isinstance(token_data_raw, dict) and "address" in token_data_raw:
            token_info = token_data_raw  # Direct token object
        else:
            logger.warning(f"Invalid response structure for CA {mint_address}: {token_data_raw}")
            return {"error": f"Invalid API response for CA {mint_address}: expected token object or tokens list"}

        logger.debug(f"Token info for CA {mint_address}: {token_info}")
        
        token_data = {}
        price = float(token_info.get("price", 0))
        token_data["price"] = price
        token_data["price_1h"] = float(token_info.get("price_1h", 0))
        token_data["price_24h"] = float(token_info.get("price_24h", 0))
        total_supply = float(token_info.get("total_supply", 0))
        token_data["market_cap"] = price * total_supply
        token_data["market_cap_str"] = format_market_cap(token_data["market_cap"])
        token_data["liquidity"] = float(token_info.get("liquidity", 0))
        token_data["liquidity_str"] = format_market_cap(token_data["liquidity"])
        token_data["volume_24h"] = float(token_info.get("volume_24h", 0))
        token_data["swaps_24h"] = token_info.get("swaps_24h", 0)
        token_data["top_10_holder_rate"] = float(token_info.get("top_10_holder_rate", 0)) * 100
        token_data["renounced_mint"] = bool(token_info.get("renounced_mint", False))  # Default to False
        token_data["renounced_freeze_account"] = bool(token_info.get("renounced_freeze_account", False))  # Default to False
        token_data["contract"] = mint_address
        token_data["name"] = token_info.get("name", "Unknown")
        token_data["symbol"] = token_info.get("symbol", "")
        token_data["hot_level"] = int(token_info.get("hot_level", 0))

        logger.debug(f"Processed token data for CA {mint_address}: market_cap={token_data['market_cap']:.2f}, symbol={token_data['symbol']}, hot_level={token_data['hot_level']}")
        return token_data

    except Exception as e:
        logger.error(f"Error processing API response for CA {mint_address}: {str(e)}, Raw response: {token_data_raw}")
        return {"error": f"Failed to process API response for CA {mint_address}: {str(e)}"}

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

@dp.message(Command(commands=["testca"]))
async def test_ca(message: types.Message):
    ca = message.text.replace('/testca', '').strip()
    if not ca:
        await message.answer("Usage: /testca <CA>\nExample: /testca 3ZL3oMDSXiqhQo6c4tt2bTo9faEevnURttHKmsv4pump")
        logger.info("No CA provided for /testca command")
        return
    
    logger.debug(f"Testing API fetch for CA: {ca}")
    token_data = await get_gmgn_token_data(ca)
    if "error" in token_data:
        await message.answer(f"⚠️ Error fetching token data for CA `{ca}`: {token_data['error']}", parse_mode="Markdown")
    else:
        pump_fun_url = f"https://pump.fun/coin/{ca}"
        token_name = token_data.get('name', 'Unknown')
        symbol = token_data.get('symbol', '')
        output_text = (
            f"📊 [{token_name}]({pump_fun_url}) | ${symbol}\n"
            f"📍 CA: `{ca}`\n"
            f"📈 Market Cap: ${token_data.get('market_cap_str', 'N/A')}\n"
            f"🌡️ Hot Level: {token_data.get('hot_level', 'N/A')} {get_hot_level_emoji(token_data.get('hot_level', -1))}"
        )
        await message.answer(output_text, parse_mode="Markdown")
    logger.info(f"Tested API fetch for CA: {ca}")

@dp.message(Command(commands=["debug"]))
async def debug_info(message: types.Message):
    user = message.from_user
    username = user.username or "Unknown"
    chat_id = message.chat.id
    user_id = user.id
    logger.info(f"Debug command received - Chat ID: {chat_id}, User ID: {user_id}, Username: @{username}")
    await message.answer(
        f"**Debug Info**\n\nChat ID: `{chat_id}`\nUser ID: `{user_id}`\nUsername: `@{username}`",
        parse_mode="Markdown"
    )

@dp.message(Command(commands=["downloadcsv"]))
async def download_csv(message: types.Message):
    logger.info(f"Received /downloadcsv command from user {message.from_user.id} in chat {message.chat.id}")
    if not os.path.exists(CSV_PATH):
        await message.answer("⚠️ No monitored tokens CSV found. Try posting a CA in the VIP channel first.")
        logger.warning(f"CSV file not found at {CSV_PATH} for /downloadcsv command")
        return
    try:
        async with aiofiles.open(CSV_PATH, mode='rb') as file:
            await bot.send_document(
                chat_id=message.chat.id,
                document=types.FSInputFile(CSV_PATH, filename="lucidswans_monitored_tokens.csv"),
                caption="Here is the monitored tokens CSV file."
            )
        logger.info(f"Sent lucidswans_monitored_tokens.csv to chat {message.chat.id}")
    except Exception as e:
        await message.answer(f"⚠️ Error sending CSV file: {str(e)}")
        logger.error(f"Failed to send CSV file to chat {message.chat.id}: {str(e)}")

async def process_message_with_buttons(message: types.Message):
    global search_text
    
    text = message.text.strip()
    logger.debug(f"Processing message: {text}")
    
    ca_match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{43,44}\b', text)
    if not ca_match:
        logger.info(f"No CA found in message: {text}")
        return
    ca = ca_match.group(0)
    logger.debug(f"Detected CA: {ca}")
    
    token_data = await get_gmgn_token_data(ca)
    if "error" in token_data:
        output_text = f"🔗 CA: `{ca}`\n⚠️ Error fetching token data: {token_data['error']}"
    else:
        api_mc = token_data.get("market_cap", 0)
        api_liq = token_data.get("liquidity", 0)
        price = token_data.get('price', 0)
        price_display = format_price(price) if price != 0 else "N/A"
        price_change_1h = calculate_percentage_change(price, token_data.get('price_1h', 0))
        price_change_24h = calculate_percentage_change(price, token_data.get('price_24h', 0))
        volume_24h = format_volume(token_data.get('volume_24h', 0))
        mint_status = "✅" if token_data.get('renounced_mint') else "❌"
        freeze_status = "✅" if token_data.get('renounced_freeze_account') else "❌"
        security_status = f"🔒 Security: {mint_status} Mint {freeze_status} Freeze"
        token_name = token_data.get('name', 'Unknown')
        symbol = token_data.get('symbol', '')
        pump_fun_url = f"https://pump.fun/coin/{ca}"
        name_display = f"📊 [{token_name}]({pump_fun_url}) | ${symbol}"
        hot_level = token_data.get('hot_level', 0)
        liq_to_supply_ratio = (api_liq / api_mc * 100) if api_mc > 0 and api_liq > 0 else 0
        liq_to_supply_display = f"{liq_to_supply_ratio:.1f}%" if liq_to_supply_ratio > 0 else "N/A"

        volume_24h_raw = float(token_data.get('volume_24h', 0))
        logger.debug(f"Calculating PeakVol/MC for CA {ca}: volume_24h={volume_24h_raw}, market_cap={api_mc}")
        peak_vol_to_mc_ratio = (volume_24h_raw / api_mc * 100) if api_mc > 0 and volume_24h_raw >= 0 else 0
        peak_vol_to_mc_display = f"{peak_vol_to_mc_ratio:.2f}%" if peak_vol_to_mc_ratio > 0 else "N/A"
        
        output_text = (
            f"{name_display}\n"
            f"💎 MC: ${token_data.get('market_cap_str', 'N/A')}\n"
           # f"💧 Liquidity: ${token_data.get('liquidity_str', 'N/A')}\n"
           # f"🌊 Liq/Supply: {liq_to_supply_display}\n"
            f"💰 Price: ${price_display}\n"
           # f"📈 Price Change (1h): {price_change_1h}\n"
           # f"🔄 Swaps (24h): {token_data.get('swaps_24h', 'N/A')}\n"
            f"💸 Volume (24h): ${volume_24h}\n"
            # f"👥 Top 10 Holders: {token_data.get('top_10_holder_rate', 0):.2f}%\n"
            # f"{security_status}\n\n"
            f"`{ca}`\n"
        )
        logger.debug(f"Included hyperlinked token name '{token_name}', symbol '${symbol}', and hot_level {hot_level} in token data message for CA: {ca}")

        if message.chat.id == VIP_CHAT_ID:
            initial_mc = token_data.get("market_cap", 0)
            if initial_mc > 0:
                await add_to_monitored_tokens(
                    mint_address=ca,
                    chat_id=message.chat.id,
                    initial_mc=initial_mc,
                    token_name=token_data.get("name", "Unknown"),
                    symbol=token_data.get("symbol", ""),
                    message_id=message.message_id
                )
            else:
                logger.warning(f"Skipping CA {ca} for monitoring: initial market cap is 0")

    axiom_url = f"https://axiom.trade/t/{urllib.parse.quote(ca)}/@lucidswan"
    fasol_url = f"https://t.me/fasol_robot?start=ref_humbleguy_ca_{ca}"
    parsed_axiom_url = urllib.parse.urlparse(axiom_url)
    parsed_fasol_url = urllib.parse.urlparse(fasol_url)
    if not (parsed_axiom_url.scheme in ['http', 'https'] and parsed_axiom_url.netloc):
        logger.error(f"Invalid Axiom URL for CA {ca}: {axiom_url}")
        output_text += "\n⚠️ Axiom link unavailable due to invalid URL"
        keyboard = None
    elif not (parsed_fasol_url.scheme in ['http', 'https'] and parsed_fasol_url.netloc):
        logger.error(f"Invalid Fasol URL for CA {ca}: {fasol_url}")
        output_text += "\n⚠️ Fasol link unavailable due to invalid URL"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Axiom", url=axiom_url)]
        ])
    else:
        logger.debug(f"Valid Axiom URL for CA {ca}: {axiom_url}")
        logger.debug(f"Valid Fasol URL for CA {ca}: {fasol_url}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                # InlineKeyboardButton(text="Axiom", url=axiom_url),
                InlineKeyboardButton(text="Buy on Fasol - 25% Cashback", url=fasol_url),
                # InlineKeyboardButton(text="Bloom", url=f"https://t.me/BloomSolana_bot?start=ref_humbleguy_ca_{ca}")
            ]
        ])

    try:
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
        logger.info(f"Added buttons and token info for CA: {ca}")
    except Exception as e:
        logger.error(f"Failed to send message for CA {ca}: {str(e)}")
        output_text += f"\n⚠️ Error posting message: {str(e)}"
        await bot.send_message(
            chat_id=message.chat.id,
            text=output_text,
            parse_mode="Markdown"
        )

@dp.message(F.text)
async def add_buttons_if_text_found(message: types.Message):
    global search_text
    text = message.text.strip()
    logger.debug(f"Received message in group: {text}")

    ca_match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{43,44}\b', text)
    if not ca_match:
        logger.debug(f"No CA found in group message: {text}")
        return
    ca = ca_match.group(0)
    logger.debug(f"Detected CA in group message: {ca}")

    if search_text and search_text.lower() not in text.lower():
        logger.debug(f"Search text '{search_text}' not found in message with CA {ca}, skipping")
        return

    logger.debug(f"Waiting 3 seconds before processing CA {ca} in group")
    await asyncio.sleep(2)

    await process_message_with_buttons(message)

@dp.channel_post(F.text)
async def add_buttons_if_text_found_in_channel(channel_post: types.Message):
    global search_text
    text = channel_post.text.strip()
    logger.debug(f"Received message in channel: {text}")

    ca_match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{43,44}\b', text)
    if not ca_match:
        logger.debug(f"No CA found in channel message: {text}")
        return
    ca = ca_match.group(0)
    logger.debug(f"Detected CA in channel message: {ca}")

    if search_text and search_text.lower() not in text.lower():
        logger.debug(f"Search text '{search_text}' not found in message with CA {ca}, skipping")
        return

    logger.debug(f"Waiting 3 seconds before processing CA {ca} in channel")
    await asyncio.sleep(3)

    await process_message_with_buttons(channel_post)

async def on_startup():
    load_monitored_tokens()
    ## asyncio.create_task(growthcheck())
    ## asyncio.create_task(schedule_daily_report())
    logger.info("Button Bot started")


import os
import base64
import hashlib
import secrets

code_verifier = secrets.token_urlsafe(32)
print(f"code_verifier: {code_verifier}")
code_challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
code_challenge = base64.urlsafe_b64encode(code_challenge_bytes).rstrip(b'=').decode('utf-8')
print(f"code_challenge: {code_challenge}")
## os.environ['X_CODE_VERIFIER'] = code_verifier  # Store temporarily

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
