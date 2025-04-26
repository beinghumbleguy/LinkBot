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

# Enable detailed logging
logging.basicConfig(
    level=logging.DEBUG,
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

# Store forwarded CAs to prevent duplicates
forwarded_cas = set()

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

# Debug handler for all messages in Lucid Labs VIP
@dp.message(F.chat.id == -2419720617)
async def debug_lucid_labs_messages(message: types.Message):
    user = message.from_user
    username = user.username or "Unknown"
    text = message.text or "No text"
    message_type = message.content_type
    logger.debug(f"Debug: Message in Lucid Labs VIP (chat {message.chat.id}) from user ID {user.id} (@{username}), Type: {message_type}, Content: {text}")

# Forward messages from specific user with CA
@dp.message(F.text, F.chat.id == -2419720617, F.from_user.id == 6199899344)
async def forward_user_message_with_ca(message: types.Message):
    """
    Monitors messages in Lucid Labs VIP (chat ID -2419720617) from user @X_500SOL (ID 6199899344).
    If a Solana CA is found and the message doesn't start with /pnl, forwards to the target group (chat ID -4757751231).
    Skips forwarding if the CA has already been forwarded.
    """
    try:
        text = message.text.strip()
        user = message.from_user
        username = user.username or "Unknown"
        message_type = message.content_type
        logger.debug(f"Received message in chat {message.chat.id} from user ID {user.id} (@{username}), Type: {message_type}, Content: {text}")

        # Skip messages starting with /pnl
        if text.lower().startswith('/pnl'):
            logger.info(f"Skipping message starting with /pnl from @{username} (ID {user.id}) in chat {message.chat.id}: {text}")
            return

        # Extract CA from the message (43 or 44-character Solana address)
        ca_match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{43,44}\b', text)
        if not ca_match:
            logger.info(f"No CA found in message from @{username} (ID {user.id}) in chat {message.chat.id}: {text}")
            return

        ca = ca_match.group(0)
        logger.debug(f"Detected CA in message from @{username} (ID {user.id}): {ca}")

        # Check for duplicate CA
        if ca in forwarded_cas:
            logger.info(f"CA {ca} already forwarded, skipping duplicate from @{username} (ID {user.id})")
            await message.reply(
                text=f"CA `{ca}` has already been forwarded.",
                parse_mode="Markdown"
            )
            return

        logger.info(f"Found CA {ca} in message from @X_500SOL, forwarding to target group")

        # Forward the message to the target group (chat ID -4757751231)
        forwarded_message = await bot.forward_message(
            chat_id=-4757751231,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        logger.info(f"Successfully forwarded message with CA {ca} to group -4757751231 (Message ID: {forwarded_message.message_id})")

        # Add CA to forwarded set
        forwarded_cas.add(ca)

        # Send confirmation to the source group (Lucid Labs VIP)
        await message.reply(
            text=f"Message with CA `{ca}` forwarded to the target group.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error in forward_user_message_with_ca for message: {text}, CA: {ca if ca_match else 'None'}: {str(e)}")
        await message.reply(
            text=f"⚠️ Error forwarding message with CA `{ca if ca_match else 'None'}`: {str(e)}",
            parse_mode="Markdown"
        )

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
