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

# Enable detailed logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create data directory
os.makedirs("/app/data", exist_ok=True)

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

# Growth check variables (matching Humble bot)
growth_notifications_enabled = True
GROWTH_THRESHOLD = 1.5  # Notify when growth reaches 1.5x
INCREMENT_THRESHOLD = 1.0  # Notify at increments of 1.0 (e.g., 1.5x, 2.5x, 3.5x)
CHECK_INTERVAL = 30  # Seconds between growth checks
MONITORING_DURATION = 21600  # 6 hours in seconds
monitored_tokens = {}  # Format: {key: {"mint_address": str, "chat_id": int, "initial_mc": float, "timestamp": float, "token_name": str, "message_id": int}}
last_growth_ratios = {}  # Tracks last notified growth ratio per CA

# Define channel IDs
VIP_CHAT_ID = -1002625241334  # Lucid Labs VIP channel ID
CSV_PATH = "/app/data/monitored_tokens.csv"  # Path to store monitored tokens

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
    """Load monitored tokens from CSV on startup."""
    global monitored_tokens, last_growth_ratios
    if os.path.exists(CSV_PATH):
        try:
            df = pd.read_csv(CSV_PATH)
            for _, row in df.iterrows():
                key = f"{row['mint_address']}:{row['chat_id']}"
                monitored_tokens[key] = {
                    "mint_address": row["mint_address"],
                    "chat_id": int(row["chat_id"]),
                    "initial_mc": float(row["initial_mc"]),
                    "timestamp": float(row["timestamp"]),
                    "token_name": row["token_name"],
                    "message_id": int(row["message_id"])
                }
                last_growth_ratios[key] = float(row.get("last_growth_ratio", 1.0))
            logger.info(f"Loaded {len(monitored_tokens)} tokens from {CSV_PATH}")
        except Exception as e:
            logger.error(f"Failed to load monitored tokens from CSV: {str(e)}")
    else:
        logger.info(f"No existing monitored tokens CSV found at {CSV_PATH}")

def save_monitored_tokens():
    """Save monitored tokens to CSV."""
    try:
        df = pd.DataFrame([
            {
                "mint_address": data["mint_address"],
                "chat_id": data["chat_id"],
                "initial_mc": data["initial_mc"],
                "timestamp": data["timestamp"],
                "token_name": data["token_name"],
                "message_id": data["message_id"],
                "last_growth_ratio": last_growth_ratios.get(key, 1.0)
            }
            for key, data in monitored_tokens.items()
        ])
        df.to_csv(CSV_PATH, index=False)
        logger.info(f"Saved {len(monitored_tokens)} tokens to {CSV_PATH}")
    except Exception as e:
        logger.error(f"Failed to save monitored tokens to CSV: {str(e)}")

async def add_to_monitored_tokens(mint_address: str, chat_id: int, initial_mc: float, token_name: str, message_id: int):
    """Add a CA to monitored tokens and save to CSV."""
    key = f"{mint_address}:{chat_id}"
    if key not in monitored_tokens:
        monitored_tokens[key] = {
            "mint_address": mint_address,
            "chat_id": chat_id,
            "initial_mc": initial_mc,
            "timestamp": time.time(),
            "token_name": token_name,
            "message_id": message_id
        }
        last_growth_ratios[key] = 1.0  # Initialize last notified ratio
        save_monitored_tokens()
        logger.info(f"Added CA {mint_address} to monitored_tokens for chat {chat_id}")
    else:
        logger.debug(f"CA {mint_address} already in monitored_tokens for chat {chat_id}")

async def growthcheck():
    """Periodically check market cap growth and notify for milestones (1.5x, 2.5x, etc.) in VIP channel."""
    while True:
        if not monitored_tokens:
            logger.debug("No tokens to monitor, skipping growth check")
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        logger.debug(f"Starting growthcheck with monitored_tokens: {len(monitored_tokens)} tokens")
        to_remove = []

        for key, data in monitored_tokens.items():
            ca = data["mint_address"]
            chat_id = data["chat_id"]
            initial_mc = data["initial_mc"]
            token_name = data["token_name"]
            message_id = data["message_id"]
            timestamp = data["timestamp"]

            # Only process VIP channel
            if chat_id != VIP_CHAT_ID:
                logger.debug(f"Skipping CA {ca} in chat {chat_id} (not VIP)")
                continue

            # Check if monitoring duration exceeded
            current_time = datetime.now(pytz.timezone('America/New_York'))
            token_time = datetime.fromtimestamp(timestamp, pytz.timezone('America/New_York'))
            time_diff = (current_time - token_time).total_seconds()
            if time_diff > MONITORING_DURATION:
                logger.info(f"Removing CA {ca} from monitoring: exceeded 6 hours")
                to_remove.append(key)
                continue

            # Fetch current market cap
            token_data = await get_gmgn_token_data(ca)
            if "error" in token_data:
                logger.debug(f"Skipping CA {ca} due to API error: {token_data['error']}")
                to_remove.append(key)
                continue

            current_mc = token_data.get("market_cap", 0)
            if current_mc == 0:
                logger.debug(f"Skipping CA {ca} due to invalid current_mc: {current_mc}")
                to_remove.append(key)
                continue

            # Calculate growth ratio
            growth_ratio = current_mc / initial_mc if initial_mc != 0 else 0

            # Define market cap strings for debug logging
            initial_mc_str = f"{initial_mc / 1000:.1f}K" if initial_mc < 1_000_000 else f"{initial_mc / 1_000_000:.1f}M"
            current_mc_str = f"{current_mc / 1000:.1f}K" if current_mc < 1_000_000 else f"{current_mc / 1_000_000:.1f}M"

            # Check notification threshold
            last_ratio = last_growth_ratios.get(key, 1.0)
            next_threshold = int(last_ratio) + INCREMENT_THRESHOLD

            if growth_notifications_enabled and growth_ratio >= GROWTH_THRESHOLD and growth_ratio >= next_threshold:
                last_growth_ratios[key] = growth_ratio
                time_since_added = calculate_time_since(timestamp)
                initial_mc_str_md = f"**{initial_mc / 1000:.1f}K**" if initial_mc < 1_000_000 else f"**{initial_mc / 1_000_000:.1f}M**"
                current_mc_str_md = f"**{current_mc / 1000:.1f}K**" if current_mc < 1_000_000 else f"**{current_mc / 1_000_000:.1f}M**"
                emoji = "🚀" if 2 <= growth_ratio < 5 else "🔥" if 5 <= growth_ratio < 10 else "🌙"
                growth_str = f"**{growth_ratio:.1f}x**"

                growth_message = (
                    f"{emoji} {growth_str} | "
                    f"💹From {initial_mc_str_md} ↗️ **{current_mc_str_md}** within **{time_since_added}**"
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

            # Log growth for debugging
            profit_percent = ((current_mc - initial_mc) / initial_mc) * 100 if initial_mc != 0 else 0
            logger.debug(f"CA {ca}: Initial MC={initial_mc_str}, Current MC={current_mc_str}, Growth={profit_percent:.2f}%")

        # Remove expired or errored tokens
        for key in to_remove:
            monitored_tokens.pop(key, None)
            last_growth_ratios.pop(key, None)
        if to_remove:
            save_monitored_tokens()
            logger.info(f"Removed {len(to_remove)} expired tokens")

        await asyncio.sleep(CHECK_INTERVAL)

# API Session Manager for fetching token data
class APISessionManager:
    def __init__(self):
        self.session = None
        self._session_created_at = 0
        self._session_requests = 0
        self._session_max_age = 3600  # 1 hour
        self._session_max_requests = 100
        self.max_retries = 5
        self.retry_delay = 2
        self.base_url = "https://gmgn.ai/defi/quotation/v1/tokens/sol/search"
        
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.ua = UserAgent()

        self.headers_dict = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": "https://gmgn.ai",
            "Referer": "https://gmgn.ai/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        self.proxy_list = [
            {
                "host": "residential.birdproxies.com",
                "port": 7777,
                "username": "pool-p1-cc-us",
                "password": "sf3lefz1yj3zwjvy"
            } for _ in range(9)
        ]
        self.current_proxy_index = 0
        logger.info(f"Initialized proxy list with {len(self.proxy_list)} proxies")

    async def get_proxy(self):
        if not self.proxy_list:
            logger.warning("No proxies available in the proxy list")
            return None
        proxy = self.proxy_list[self.current_proxy_index]
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.proxy_list)
        proxy_url = f"http://{proxy['username']}:{proxy['password']}@{proxy['host']}:{proxy['port']}"
        logger.debug(f"Selected proxy: {proxy_url}")
        return {"http": proxy_url, "https": proxy_url}

    async def randomize_session(self, force: bool = False, use_proxy: bool = True):
        current_time = time.time()
        session_expired = (current_time - self._session_created_at) > self._session_max_age
        too_many_requests = self._session_requests >= self._session_max_requests
        
        if self.session is None or force or session_expired or too_many_requests:
            self.session = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'mobile': False
                }
            )
            user_agent = self.ua.random
            self.headers_dict["User-Agent"] = user_agent
            self.session.headers.update(self.headers_dict)
            logger.debug(f"Randomized User-Agent: {user_agent}")
            
            if use_proxy and self.proxy_list:
                proxy_dict = await self.get_proxy()
                if proxy_dict:
                    self.session.proxies = proxy_dict
                    logger.debug(f"Successfully configured proxy {proxy_dict}")
                else:
                    logger.warning("No proxy available, proceeding without proxy.")
            else:
                self.session.proxies = None
                logger.debug("Proceeding without proxy as per request.")
            
            self._session_created_at = current_time
            self._session_requests = 0
            logger.debug("Created new cloudscraper session")

    async def _run_in_executor(self, func, *args, **kwargs):
        return await asyncio.get_event_loop().run_in_executor(
            self._executor, 
            lambda: func(*args, **kwargs)
        )

    async def fetch_token_data(self, mint_address):
        logger.debug(f"Fetching data for mint_address: {mint_address}")
        await self.randomize_session()
        if not self.session:
            logger.error("Cloudscraper session not initialized")
            return {"error": "Cloudscraper session not initialized"}
        
        self._session_requests += 1
        url = f"{self.base_url}?q={mint_address}"
        
        for attempt in range(self.max_retries):
            try:
                response = await self._run_in_executor(
                    self.session.get,
                    url,
                    headers=self.headers_dict,
                    timeout=15
                )
                headers_log = {k: v for k, v in response.headers.items()}
                logger.debug(f"Attempt {attempt + 1} for CA {mint_address} - Status: {response.status_code}, Response length: {len(response.text)}, Headers: {headers_log}")
                
                if response.status_code == 200:
                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' not in content_type.lower():
                        logger.warning(f"Non-JSON response for CA {mint_address}: Content-Type={content_type}, Response: {response.text[:500]}")
                        await self.randomize_session(force=True, use_proxy=True)
                        return {"error": f"Unexpected Content-Type: {content_type}"}
                    
                    content_length = int(response.headers.get('Content-Length', -1))
                    if content_length == 0 or not response.text.strip():
                        logger.error(f"Empty response for CA {mint_address}: Content-Length={content_length}, Response: {response.text[:500]}")
                        await self.randomize_session(force=True, use_proxy=True)
                        return {"error": "Empty response from API"}
                    
                    try:
                        json_data = response.json()
                        logger.debug(f"Raw response for CA {mint_address} (first 500 chars): {response.text[:500]}")
                        
                        if json_data.get("code") != 0:
                            logger.error(f"API error for CA {mint_address}: code={json_data.get('code')}, msg={json_data.get('msg')}")
                            return {"error": f"API error: code={json_data.get('code')}, msg={json_data.get('msg')}"}
                        
                        return json_data
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error for CA {mint_address}: {str(e)}, Response: {response.text[:500]}, Headers: {headers_log}")
                        await self.randomize_session(force=True, use_proxy=True)
                        if attempt < self.max_retries - 1:
                            logger.debug(f"Retrying due to JSON decode error for CA {mint_address}")
                            delay = self.retry_delay * (2 ** attempt)
                            await asyncio.sleep(delay)
                            continue
                        return {"error": f"Invalid JSON response for CA {mint_address}: {str(e)}"}
                
                elif response.status_code == 403:
                    logger.warning(f"Attempt {attempt + 1} for CA {mint_address} failed with 403: {response.text[:100]}, Headers: {headers_log}")
                    if "Just a moment" in response.text:
                        logger.warning(f"Cloudflare challenge detected for CA {mint_address}, rotating proxy")
                        await self.randomize_session(force=True, use_proxy=True)
                elif response.status_code == 429:
                    logger.warning(f"Attempt {attempt + 1} for CA {mint_address} failed with 429: Rate limited, Headers: {headers_log}")
                    delay = self.retry_delay * (2 ** attempt) * 2
                    logger.debug(f"Backing off for {delay}s before retry {attempt + 2} for CA {mint_address}")
                    await asyncio.sleep(delay)
                else:
                    logger.warning(f"Attempt {attempt + 1} for CA {mint_address} CASE1 failed with status {response.status_code}: {response.text[:100]}, Headers: {headers_log}")
            
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} for CA {mint_address} failed: {str(e)}")
            
            if attempt < self.max_retries - 1:
                delay = self.retry_delay * (2 ** attempt)
                logger.debug(f"Backing off for {delay}s before retry {attempt + 2} for CA {mint_address}")
                await asyncio.sleep(delay)
        
        logger.info(f"All proxy attempts failed for CA {mint_address}, trying without proxy")
        await self.randomize_session(force=True, use_proxy=False)
        try:
            response = await self._run_in_executor(
                self.session.get,
                url,
                headers=self.headers_dict,
                timeout=15
            )
            headers_log = {k: v for k, v in response.headers.items()}
            logger.debug(f"Fallback for CA {mint_address} - Status: {response.status_code}, Response length: {len(response.text)}, Headers: {headers_log}")
            
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' not in content_type.lower():
                    logger.warning(f"Fallback non-JSON response for CA {mint_address}: Content-Type={content_type}, Response: {response.text[:500]}")
                    return {"error": f"Unexpected Content-Type: {content_type}"}
                
                content_length = int(response.headers.get('Content-Length', -1))
                if content_length == 0 or not response.text.strip():
                    logger.error(f"Fallback empty response for CA {mint_address}: Content-Length={content_length}, Response: {response.text[:500]}")
                    return {"error": "Empty response from API"}
                
                try:
                    json_data = response.json()
                    logger.debug(f"Fallback raw response for CA {mint_address} (first 500 chars): {response.text[:500]}")
                    
                    if json_data.get("code") != 0:
                        logger.error(f"Fallback API error for CA {mint_address}: code={json_data.get('code')}, msg={json_data.get('msg')}")
                        return {"error": f"API error: code={json_data.get('code')}, msg={json_data.get('msg')}"}
                    
                    return json_data
                except json.JSONDecodeError as e:
                    logger.error(f"Fallback JSON decode error for CA {mint_address}: {str(e)}, Response: {response.text[:500]}, Headers: {headers_log}")
                    return {"error": f"Invalid JSON response for CA {mint_address}: {str(e)}"}
            
            logger.warning(f"Fallback for CA {mint_address} failed with status {response.status_code}: {response.text[:100]}, Headers: {headers_log}")
        
        except Exception as e:
            logger.error(f"Final attempt without proxy failed for CA {mint_address}: {str(e)}")
        
        logger.error(f"Failed to fetch data for CA {mint_address} after {self.max_retries} attempts")
        return {"error": f"Failed to fetch data for CA {mint_address} after retries"}

# Initialize the API session manager
api_session_manager = APISessionManager()

# Helper function to format market cap
def format_market_cap(market_cap: float) -> str:
    if market_cap >= 1_000_000:
        return f"{market_cap / 1_000_000:.2f}M"
    elif market_cap >= 1_000:
        return f"{market_cap / 1_000:.2f}K"
    elif market_cap > 0:
        return f"{market_cap:.2f}"
    return "N/A"

# Helper function to format price
def format_price(price: float) -> str:
    if price == 0:
        return "N/A"
    if price < 0.0001:
        return f"{price:.7f}".rstrip('0').rstrip('.')
    return f"{price:.6f}".rstrip('0').rstrip('.')

# Helper function to format volume
def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"{volume / 1_000:.2f}K"
    elif volume > 0:
        return f"{volume:.2f}"
    return "N/A"

# Helper function to calculate percentage change
def calculate_percentage_change(current: float, previous: float) -> str:
    if previous == 0 or current == 0:
        return "N/A"
    change = ((current - previous) / previous) * 100
    return f"{change:+.2f}%"  # + for positive, - for negative

# Function to fetch token data
async def get_gmgn_token_data(mint_address):
    token_data_raw = await api_session_manager.fetch_token_data(mint_address)
    logger.debug(f"Received raw token data for CA {mint_address}: {token_data_raw}")
    if "error" in token_data_raw:
        logger.error(f"Error from fetch_token_data for CA {mint_address}: {token_data_raw['error']}")
        return {"error": token_data_raw["error"]}

    try:
        token_data = {}
        
        if not token_data_raw or "data" not in token_data_raw or "tokens" not in token_data_raw["data"] or len(token_data_raw["data"]["tokens"]) == 0:
            logger.warning(f"No valid token data in response for CA {mint_address}: {token_data_raw}")
            return {"error": f"No token data returned from API for CA {mint_address}"}
        
        token_info = token_data_raw["data"]["tokens"][0]
        logger.debug(f"Token info for CA {mint_address}: {token_info}")
        
        price = float(token_info.get("price", 0))
        token_data["price"] = price
        token_data["price_1h"] = float(token_info.get("price_1h", 0))
        token_data["price_24h"] = float(token_info.get("price_24h", 0))
        total_supply = float(token_info.get("total_supply", 0))
        token_data["market_cap"] = price * total_supply
        token_data["market_cap_str"] = format_market_cap(token_data["market_cap"])
        token_data["liquidity"] = token_info.get("liquidity", "0")
        token_data["volume_24h"] = float(token_info.get("volume_24h", 0))
        token_data["swaps_24h"] = token_info.get("swaps_24h", 0)
        token_data["top_10_holder_rate"] = float(token_info.get("top_10_holder_rate", 0)) * 100
        token_data["renounced_mint"] = bool(token_info.get("renounced_mint", 0))
        token_data["renounced_freeze_account"] = bool(token_info.get("renounced_freeze_account", 0))
        token_data["contract"] = mint_address
        token_data["name"] = token_info.get("name", "Unknown")

        logger.debug(f"Processed token data for CA {mint_address}: {token_data}")
        return token_data

    except Exception as e:
        logger.error(f"Error processing API response for CA {mint_address}: {str(e)}")
        return {"error": f"Network or parsing error for CA {mint_address}: {str(e)}"}

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

# Command to test API fetch for a specific CA
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
        output_text = (
            f"**Token Data**\n\n"
            f"🔖 Token Name: {token_data.get('name', 'Unknown')}\n"
            f"📍 CA: `{ca}`\n"
            f"📈 Market Cap: ${token_data.get('market_cap_str', 'N/A')}"
        )
        await message.answer(output_text, parse_mode="Markdown")
    logger.info(f"Tested API fetch for CA: {ca}")

# Command to debug chat and user IDs
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

# Function to process messages (used for both groups and channels)
async def process_message_with_buttons(message: types.Message):
    global search_text
    
    if not search_text:
        return
    
    text = message.text.strip()
    logger.debug(f"Processing message: {text}")
    if search_text.lower() in text.lower():
        ca_match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{43,44}\b', text)
        if not ca_match:
            logger.info(f"Search text '{search_text}' found, but no CA in message: {text}")
            return
        ca = ca_match.group(0)
        logger.debug(f"Detected CA: {ca}")
        
        token_data = await get_gmgn_token_data(ca)
        if "error" in token_data:
            output_text = f"🔗 CA: `{ca}`\n⚠️ Error fetching token data: {token_data['error']}"
        else:
            price = token_data.get('price', 0)
            price_display = format_price(price) if price != 0 else "N/A"
            price_change_1h = calculate_percentage_change(price, token_data.get('price_1h', 0))
            price_change_24h = calculate_percentage_change(price, token_data.get('price_24h', 0))
            volume_24h = format_volume(token_data.get('volume_24h', 0))
            security_status = (
                f"✅ Mint Renounced\n"
                f"✅ Freeze Renounced"
                if token_data.get('renounced_mint') and token_data.get('renounced_freeze_account')
                else "⚠️ Check Security"
            )
            output_text = (
                f"**Token Data**\n\n"
                f"🔖 Token Name: {token_data.get('name', 'Unknown')}\n"
                f"📍 CA: `{ca}`\n"
                f"📈 Market Cap: ${token_data.get('market_cap_str', 'N/A')}\n"
                f"💧 Liquidity: ${float(token_data.get('liquidity', '0')):.2f}\n"
                f"💰 Price: ${price_display}\n"
                f"📉 Price Change (1h/24h): {price_change_1h} / {price_change_24h}\n"
                f"🔄 Swaps (24h): {token_data.get('swaps_24h', 'N/A')}\n"
                f"💸 Volume (24h): ${volume_24h}\n"
                f"👥 Top 10 Holders: {token_data.get('top_10_holder_rate', 0):.2f}%\n"
                f"🔒 Security: {security_status}"
            )

            # Add to monitored tokens if in VIP channel
            if message.chat.id == VIP_CHAT_ID:
                initial_mc = token_data.get("market_cap", 0)
                if initial_mc > 0:
                    await add_to_monitored_tokens(
                        mint_address=ca,
                        chat_id=message.chat.id,
                        initial_mc=initial_mc,
                        token_name=token_data.get("name", "Unknown"),
                        message_id=message.message_id
                    )
                else:
                    logger.warning(f"Skipping CA {ca} for monitoring: initial market cap is 0")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Axiom", url=f"https://axiom.trade/t/{ca}/@lucidswan")
            ]
        ])
        
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
        logger.info(f"Added buttons and token info for message containing '{search_text}' and CA: {ca}")

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
    load_monitored_tokens()  # Load existing tokens
    asyncio.create_task(growthcheck())  # Start growth check
    logger.info("Button Bot started")

# Main function to start the bot
async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
