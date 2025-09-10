import time
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
import cloudscraper
from fake_useragent import UserAgent

logger = logging.getLogger(__name__)

class APISessionManager:
    def __init__(self):
        self.session = None
        self._session_created_at = 0
        self._session_requests = 0
        self._session_max_age = 3600
        self._session_max_requests = 100
        self.max_retries = 5
        self.retry_delay = 2
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
            } for _ in range(50)
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
        url = "https://gmgn.ai/api/v1/mutil_window_token_info"
        payload = {"chain": "sol", "addresses": [mint_address]}

        for attempt in range(self.max_retries):
            try:
                response = await self._run_in_executor(
                    self.session.post,
                    url,
                    headers=self.headers_dict,
                    json=payload,
                    timeout=15
                )
                headers_log = {k: v for k, v in response.headers.items()}
                logger.debug(f"Attempt {attempt + 1} for CA {mint_address} - "
                             f"Status: {response.status_code}, Response length: {len(response.text)}, Headers: {headers_log}")

                if response.status_code != 200:
                    logger.warning(f"Attempt {attempt + 1} failed for CA {mint_address}: "
                                   f"HTTP {response.status_code}, Response: {response.text[:200]}")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(self.retry_delay * (2 ** attempt))
                    continue

                try:
                    json_data = response.json()
                    logger.debug(f"Raw response for CA {mint_address} (first 500 chars): {response.text[:500]}")
                    json_data["headers"] = headers_log

                    if isinstance(json_data, dict) and "data" in json_data and "tokens" in json_data["data"]:
                        return json_data
                    else:
                        logger.error(f"Unexpected response structure for CA {mint_address}: {response.text[:500]}")
                        return {"error": f"Unexpected response structure for CA {mint_address}"}

                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error for CA {mint_address}: {str(e)}")
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(self.retry_delay * (2 ** attempt))
                        continue
                    return {"error": f"Invalid JSON response for CA {mint_address}"}

            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} for CA {mint_address} failed: {str(e)}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))

        return {"error": f"Failed to fetch data for CA {mint_address} after retries"}

api_session_manager = APISessionManager()
