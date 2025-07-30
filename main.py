#!/usr/bin/env python3
import os, sys, asyncio, logging, json, time, random, aiohttp, websockets, collections
from telethon import TelegramClient
from telethon.sessions import StringSession
from aiohttp import web
from typing import Set, Dict, Any, Optional, List, Tuple
from functools import lru_cache
from datetime import datetime, timedelta
import traceback

# === PARAMETERS TO EDIT ===
ULTRA_MIN_LIQ = 8
ULTRA_BUY_AMOUNT = 0.07
ULTRA_TP_X = 2.0
ULTRA_SL_X = 0.7
ULTRA_MIN_RISES = 2
ULTRA_AGE_MAX_S = 120

SCALPER_BUY_AMOUNT = 0.10
SCALPER_MIN_LIQ = 8
SCALPER_TP_X = 2.0
SCALPER_SL_X = 0.7
SCALPER_TRAIL = 0.2
SCALPER_MAX_POOLAGE = 20*60

COMMUNITY_BUY_AMOUNT = 0.04
COMM_HOLDER_THRESHOLD = 250
COMM_MAX_CONC = 0.10
COMM_TP1_MULT = 2.0
COMM_SL_PCT = 0.6
COMM_TRAIL = 0.4
COMM_HOLD_SECONDS = 2*24*60*60
COMM_MIN_SIGNALS = 2

ANTI_SNIPE_DELAY = 2
ML_MIN_SCORE = 60

# Performance settings
CACHE_TTL = 5 # seconds
MAX_CONCURRENT_REQUESTS = 10
API_RETRY_COUNT = 3
API_RETRY_DELAY = 1
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 60

# === ENV VARS ===
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_STRING_SESSION = os.environ["TELEGRAM_STRING_SESSION"]
TOXIBOT_USERNAME = os.environ.get("TOXIBOT_USERNAME", "@toxi_solana_bot")
RUGCHECK_API = os.environ.get("RUGCHECK_API", "")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.environ.get("HELIUS_RPC_URL", "")
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS", "")
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")
BITQUERY_API_KEY = os.environ.get("BITQUERY_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("toxibot")

# Global state
blacklisted_tokens: Set[str] = set()
blacklisted_devs: Set[str] = set()
positions: Dict[str, Dict[str, Any]] = {}
activity_log: collections.deque = collections.deque(maxlen=1000)
exposure: float = 0.0
daily_loss: float = 0.0
runtime_status: str = "Starting..."
current_wallet_balance: float = 0.0

# Performance tracking
api_failures: Dict[str, int] = collections.defaultdict(int)
api_circuit_breakers: Dict[str, float] = {}
price_cache: Dict[str, Tuple[float, float]] = {}

session_pool: Optional[aiohttp.ClientSession] = None

community_signal_votes = collections.defaultdict(lambda: {"sources": set(), "first_seen": time.time()})
community_token_queue = asyncio.Queue()

# ==== PERFORMANCE UTILITIES ====

async def get_session() -> aiohttp.ClientSession:
    global session_pool
    if not session_pool or session_pool.closed:
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        session_pool = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return session_pool

def is_circuit_broken(api_name: str) -> bool:
    if api_name in api_circuit_breakers:
        if time.time() < api_circuit_breakers[api_name]:
            return True
        else:
            del api_circuit_breakers[api_name]
            api_failures[api_name] = 0
    return False

def trip_circuit_breaker(api_name: str):
    api_failures[api_name] += 1
    if api_failures[api_name] >= CIRCUIT_BREAKER_THRESHOLD:
        api_circuit_breakers[api_name] = time.time() + CIRCUIT_BREAKER_TIMEOUT
        logger.warning(f"Circuit breaker tripped for {api_name}")

async def retry_with_backoff(func, *args, **kwargs):
    for attempt in range(API_RETRY_COUNT):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == API_RETRY_COUNT - 1:
                raise
            await asyncio.sleep(API_RETRY_DELAY * (2 ** attempt))

@lru_cache(maxsize=1000)
def get_cached_price(token: str) -> Optional[float]:
    if token in price_cache:
        price, timestamp = price_cache[token]
        if time.time() - timestamp < CACHE_TTL:
            return price
    return None

def set_cached_price(token: str, price: float):
    price_cache[token] = (price, time.time())

# ==== UTILITIES ====

def get_total_pl():
    return sum([pos.get("pl", 0) for pos in positions.values()])

async def fetch_token_price(token: str) -> Optional[float]:
    cached = get_cached_price(token)
    if cached is not None:
        return cached
    if is_circuit_broken("dexscreener"):
        return None
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("baseToken", {}).get("address", "") == token and "priceNative" in pair:
                        price = float(pair["priceNative"])
                        set_cached_price(token, price)
                        return price
                return None
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"DEXScreener price error: {e}")
        trip_circuit_breaker("dexscreener")
        return None

async def fetch_pool_age(token: str) -> Optional[float]:
    if is_circuit_broken("dexscreener"):
        return None
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("baseToken", {}).get("address") == token:
                        ts = pair.get("createdAtTimestamp") or pair.get("pairCreatedAt")
                        if ts:
                            age_sec = time.time() - (int(ts)//1000 if len(str(ts)) > 10 else int(ts))
                            return age_sec
                return None
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"Pool age fetch error: {e}")
        trip_circuit_breaker("dexscreener")
        return None

async def fetch_volumes(token: str) -> dict:
    if is_circuit_broken("dexscreener"):
        return {"liq":0,"vol_1h":0,"vol_6h":0,"base_liq":0}
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("baseToken", {}).get("address") == token:
                        return {
                            "liq": float(pair.get("liquidity", {}).get("base", 0)),
                            "vol_1h": float(pair.get("volume", {}).get("h1", 0)),
                            "vol_6h": float(pair.get("volume", {}).get("h6", 0)),
                            "base_liq": float(pair.get("liquidity", {}).get("base", 0)),
                        }
                return {"liq":0,"vol_1h":0,"vol_6h":0,"base_liq":0}
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"Volume fetch error: {e}")
        trip_circuit_breaker("dexscreener")
        return {"liq":0,"vol_1h":0,"vol_6h":0,"base_liq":0}

def estimate_short_vs_long_volume(vol_1h, vol_6h):
    avg_15min = vol_6h / 24 if vol_6h else 0.01
    return vol_1h > 2 * avg_15min if avg_15min else False

async def fetch_holders_and_conc(token: str) -> dict:
    if is_circuit_broken("dexscreener"):
        return {"holders": 0, "max_holder_pct": 99.}
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("baseToken", {}).get("address") == token:
                        holders = int(pair.get("holders", 0) or 0)
                        maxconc = float(pair.get("holderConcentration", 0.0) or 0)
                        return {"holders": holders, "max_holder_pct": maxconc}
                return {"holders": 0, "max_holder_pct": 99.}
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"Holders fetch error: {e}")
        trip_circuit_breaker("dexscreener")
        return {"holders": 0, "max_holder_pct": 99.}

async def fetch_liquidity_and_buyers(token: str) -> dict:
    if is_circuit_broken("dexscreener"):
        return {"liq": 0.0, "buyers": 0, "holders": 0}
    result = {"liq": 0.0, "buyers": 0, "holders": 0}
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("baseToken", {}).get("address", "") == token:
                        result["liq"] = float(pair.get("liquidity", {}).get("base", 0.0))
                        result["buyers"] = int(pair.get("buyTxns", 0) or 0)
                        return result
                return result
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"[UltraEarly] Error fetching liq/buyers: {e}")
        trip_circuit_breaker("dexscreener")
        return result

async def fetch_wallet_balance():
    if not WALLET_ADDRESS or not HELIUS_API_KEY:
        return 0.0
    if is_circuit_broken("helius"):
        return current_wallet_balance
    try:
        url = f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}"
        req = [{"jsonrpc":"2.0","id":1,"method":"getBalance","params":[WALLET_ADDRESS]}]
        session = await get_session()
        async def _fetch():
            async with session.post(url, json=req) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                res = await resp.json()
                lamports = res[0].get("result",{}).get("value",0)
                return lamports/1e9
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.warning(f"Helius Wallet getBalance error: {e}")
        trip_circuit_breaker("helius")
        return current_wallet_balance

# ==== FEEDS ====

async def pumpfun_newtoken_feed(callback):
    uri = "wss://pumpportal.fun/api/data"
    retry_count = 0
    max_retries = 10
    while retry_count < max_retries:
        try:
            async with websockets.connect(uri) as ws:
                payload = {"method": "subscribeNewToken"}
                await ws.send(json.dumps(payload))
                retry_count = 0
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        token = data.get("params", {}).get("mintAddress") or data.get("params", {}).get("coinAddress")
                        if token:
                            await callback(token, "pumpfun")
                    except asyncio.TimeoutError:
                        logger.warning("Pump.fun WS timeout, sending ping")
                        await ws.ping()
        except Exception as e:
            retry_count += 1
            wait_time = min(60, 2 ** retry_count)
            logger.error(f"Pump.fun connection error (retry {retry_count}/{max_retries}): {e}")
            await asyncio.sleep(wait_time)

async def moralis_trending_feed(callback):
    if not MORALIS_API_KEY:
        logger.warning("Moralis feed not enabled (no API key).")
        return
    url = "https://solana-gateway.moralis.io/account/mainnet/trending"
    while True:
        if not is_circuit_broken("moralis"):
            try:
                session = await get_session()
                async def _fetch():
                    async with session.get(url, headers={"X-API-Key": MORALIS_API_KEY}) as resp:
                        if resp.status != 200:
                            raise Exception(f"HTTP {resp.status}")
                        trend = await resp.json()
                        for item in trend.get("result", []):
                            if "mint" in item:
                                await callback(item["mint"], "moralis")
                await retry_with_backoff(_fetch)
            except Exception as e:
                logger.error(f"Moralis feed error: {e}")
                trip_circuit_breaker("moralis")
            await asyncio.sleep(120)

async def bitquery_trending_feed(callback):
    if not BITQUERY_API_KEY:
        logger.warning("Bitquery feed not enabled (no API key).")
        return
    url = "https://streaming.bitquery.io/graphql"
    query = """
    {
      Solana {
        DEXTrades(limit: 10, orderBy: {descending: Block_Time}, tradeAmountUsd: {gt: 100}) {
          transaction { txFrom }
          baseCurrency { address }
          quoteCurrency { symbol }
          tradeAmount
          exchange { fullName }
          Block_Time
        }
      }
    }
    """
    payload = {"query": query}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {BITQUERY_API_KEY}"
    }
    while True:
        if not is_circuit_broken("bitquery"):
            session = await get_session()
            try:
                async def _fetch():
                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            raise Exception(f"HTTP {resp.status}: {text}")
                        data = await resp.json()
                        if (
                            not data or
                            "data" not in data or
                            "Solana" not in data["data"] or
                            "DEXTrades" not in data["data"]["Solana"]
                        ):
                            logger.error(f"Bitquery unexpected data shape: {data}")
                            return
                        for trade in data["data"]["Solana"]["DEXTrades"]:
                            addr = trade.get("baseCurrency", {}).get("address", "")
                            if addr:
                                await callback(addr, "bitquery")
                await retry_with_backoff(_fetch)
            except Exception as e:
                logger.error(f"Bitquery feed error: {e}")
                trip_circuit_breaker("bitquery")
            await asyncio.sleep(180)

# ==== COMMUNITY PERSONALITY VOTE AGGREGATOR ====

async def community_candidate_callback(token, src):
    now = time.time()
    if src and token:
        rec = community_signal_votes[token]
        rec["sources"].add(src)
        if "first_seen" not in rec:
            rec["first_seen"] = now
        voted = len(rec["sources"])
        logger.info(f"[CommunityBot] {token} in {rec['sources']} ({voted}/{COMM_MIN_SIGNALS})")
        if voted >= COMM_MIN_SIGNALS:
            await community_token_queue.put(token)

# ==== TOXIBOT/TELEGRAM ====

class ToxiBotClient:
    def __init__(self, api_id, api_hash, session_id, username):
        self._client = TelegramClient(StringSession(session_id), api_id, api_hash, connection_retries=5)
        self.bot_username = username
        self.send_lock = asyncio.Lock()
    async def connect(self):
        await self._client.start()
        logger.info("Connected to ToxiBot (Telegram).")
    async def send_buy(self, mint: str, amount: float, price_limit=None):
        async with self.send_lock:
            cmd = f"/buy {mint} {amount}".strip()
            if price_limit:
                cmd += f" limit {price_limit:.7f}"
            logger.info(f"Sending to ToxiBot: {cmd}")
            try:
                return await self._client.send_message(self.bot_username, cmd)
            except Exception as e:
                logger.error(f"Failed to send buy command: {e}")
                raise
    async def send_sell(self, mint: str, perc: int = 100):
        async with self.send_lock:
            cmd = f"/sell {mint} {perc}%"
            logger.info(f"Sending to ToxiBot: {cmd}")
            try:
                return await self._client.send_message(self.bot_username, cmd)
            except Exception as e:
                logger.error(f"Failed to send sell command: {e}")
                raise

# ==== RUGCHECK & ML ====

async def rugcheck(token_addr: str) -> Dict[str, Any]:
    if is_circuit_broken("rugcheck"):
        return {}
    url = f"https://rugcheck.xyz/api/check/{token_addr}"
    try:
        session = await get_session()
        async def _fetch():
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                if resp.headers.get('content-type','').startswith('application/json'):
                    data = await resp.json()
                else:
                    logger.warning(f"Rugcheck returned HTML for {token_addr}")
                    data = {}
                logger.info(f"Rugcheck {token_addr}: {data}")
                return data
        return await retry_with_backoff(_fetch)
    except Exception as e:
        logger.error(f"Rugcheck error for {token_addr}: {e}")
        trip_circuit_breaker("rugcheck")
        return {}

def rug_gate(rug: Dict[str, Any]) -> Optional[str]:
    if rug.get("label") != "Good":
        return "rugcheck not Good"
    if "bundled" in rug.get("supply_type", "").lower():
        if rug.get("mint"):
            blacklisted_tokens.add(rug["mint"])
        if rug.get("authority"):
            blacklisted_devs.add(rug["authority"])
        return "supply bundled"
    if rug.get("max_holder_pct", 0) > 25:
        return "too concentrated"
    return None

def is_blacklisted(token: str, dev: str = "") -> bool:
    return token in blacklisted_tokens or (dev and dev in blacklisted_devs)

def ml_score_token(meta: Dict[str, Any]) -> float:
    # Placeholder - integrate your actual ML model here
    random.seed(meta.get("mint", random.random()))
    base_score = random.uniform(60, 90)
    if meta.get("liq", 0) > 50:
        base_score += 5
    if meta.get("holders", 0) > 500:
        base_score += 3
    if meta.get("vol_1h", 0) > 10000:
        base_score += 2
    return min(97, base_score)

# ==== TRADING STRATEGIES ====

async def ultra_early_handler(token, toxibot):
    if is_blacklisted(token):
        return
    rug = await rugcheck(token)
    if rug_gate(rug):
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} UltraEarly: Rug gated.")
        return
    if token in positions:
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} UltraEarly: Already traded, skipping.")
        return
    rises, last_liq, last_buyers = 0, 0, 0
    for i in range(3):
        stats = await fetch_liquidity_and_buyers(token)
        if stats['liq'] >= ULTRA_MIN_LIQ and stats['liq'] > last_liq:
            rises += 1
        last_liq, last_buyers = stats['liq'], stats['buyers']
        await asyncio.sleep(2)
    if rises < ULTRA_MIN_RISES:
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} UltraEarly: Liquidity not rapidly rising, skipping.")
        return
    entry_price = await fetch_token_price(token) or 0.01
    try:
        await toxibot.send_buy(token, ULTRA_BUY_AMOUNT)
        positions[token] = {
            "src": "pumpfun",
            "buy_time": time.time(),
            "size": ULTRA_BUY_AMOUNT,
            "ml_score": ml_score_token({"mint": token, "liq": last_liq}),
            "entry_price": entry_price,
            "last_price": entry_price,
            "phase": "filled",
            "pl": 0.0,
            "local_high": entry_price,
            "hard_sl": entry_price * ULTRA_SL_X,
            "runner_trail": 0.3,
            "dev": rug.get("authority")
        }
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} UltraEarly: BUY {ULTRA_BUY_AMOUNT} @ {entry_price:.5f}")
    except Exception as e:
        logger.error(f"Failed to execute UltraEarly buy: {e}")

async def scalper_handler(token, src, toxibot):
    if is_blacklisted(token):
        return
    if token in positions:
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Scalper] Already traded. Skipping.")
        return
    pool_stats = await fetch_volumes(token)
    pool_age = await fetch_pool_age(token) or 9999
    liq_ok = pool_stats["liq"] >= SCALPER_MIN_LIQ
    vol_ok = estimate_short_vs_long_volume(pool_stats["vol_1h"], pool_stats["vol_6h"])
    age_ok = 0 <= pool_age < SCALPER_MAX_POOLAGE
    if not (liq_ok and age_ok and vol_ok):
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Scalper] Entry FAIL: Liq:{liq_ok}, Age:{age_ok}, Vol:{vol_ok}")
        return
    rug = await rugcheck(token)
    if rug_gate(rug):
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Scalper] Rug gated.")
        return
    entry_price = await fetch_token_price(token) or 0.01
    limit_price = entry_price * 0.97
    try:
        await toxibot.send_buy(token, SCALPER_BUY_AMOUNT, price_limit=limit_price)
        positions[token] = {
            "src": src,
            "buy_time": time.time(),
            "size": SCALPER_BUY_AMOUNT,
            "ml_score": ml_score_token({
                "mint": token,
                "liq": pool_stats["liq"],
                "vol_1h": pool_stats["vol_1h"]
            }),
            "entry_price": limit_price,
            "last_price": limit_price,
            "phase": "waiting_fill",
            "pl": 0.0,
            "local_high": limit_price,
            "hard_sl": limit_price * SCALPER_SL_X,
            "liq_ref": pool_stats["base_liq"],
            "dev": rug.get("authority"),
        }
        activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} Scalper: limit-buy {SCALPER_BUY_AMOUNT} @ {limit_price:.5f}")
    except Exception as e:
        logger.error(f"Failed to execute Scalper buy: {e}")

# ==== COMMUNITY/WHALE STRATEGY ====

recent_rugdevs = set()

async def community_trade_manager(toxibot):
    while True:
        try:
            token = await community_token_queue.get()
            if is_blacklisted(token):
                continue
            rug = await rugcheck(token)
            dev = rug.get("authority")
            if rug_gate(rug) or (dev and dev in recent_rugdevs):
                activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Community] rejected: Ruggate or rugdev.")
                continue
            holders_data = await fetch_holders_and_conc(token)
            if holders_data["holders"] < COMM_HOLDER_THRESHOLD or holders_data["max_holder_pct"] > COMM_MAX_CONC:
                activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Community] fails holder/distribution screen.")
                continue
            if token in positions:
                activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Community] position open. No averaging down.")
                continue
            entry_price = await fetch_token_price(token) or 0.01
            try:
                await toxibot.send_buy(token, COMMUNITY_BUY_AMOUNT)
                now = time.time()
                positions[token] = {
                    "src": "community",
                    "buy_time": now,
                    "size": COMMUNITY_BUY_AMOUNT,
                    "ml_score": ml_score_token({
                        "mint": token,
                        "holders": holders_data["holders"]
                    }),
                    "entry_price": entry_price,
                    "last_price": entry_price,
                    "phase": "filled",
                    "pl": 0.0,
                    "local_high": entry_price,
                    "hard_sl": entry_price * COMM_SL_PCT,
                    "dev": dev,
                    "hold_until": now + COMM_HOLD_SECONDS
                }
                activity_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {token} [Community] Buy {COMMUNITY_BUY_AMOUNT} @ {entry_price:.6f}")
            except Exception as e:
                logger.error(f"Failed to execute Community buy: {e}")
        except Exception as e:
            logger.error(f"Community trade manager error: {e}")
            await asyncio.sleep(5)

# ==== PROCESS TOKEN ====

async def process_token(token, src):
    if src == "pumpfun":
        await ultra_early_handler(token, toxibot)
    elif src in ("moralis", "bitquery"):
        await scalper_handler(token, src, toxibot)

# ==== POSITION MANAGEMENT ====

async def update_position_prices_and_wallet():
    global positions, current_wallet_balance, daily_loss, exposure
    while True:
        try:
            active_tokens = [token for token, pos in positions.items() if pos.get('size', 0) > 0]
            price_tasks = [fetch_token_price(token) for token in active_tokens]
            prices = await asyncio.gather(*price_tasks, return_exceptions=True)
            for token, price in zip(active_tokens, prices):
                if isinstance(price, Exception):
                    logger.warning(f"Price update failed for {token}: {price}")
                    continue
                if price and token in positions:
                    pos = positions[token]
                    pos['last_price'] = price
                    pos['local_high'] = max(pos.get("local_high", price), price)
                    pl = (price - pos['entry_price']) * pos['size']
                    pos['pl'] = pl
                    await handle_position_exit(token, pos, price)
            to_remove = [k for k, v in positions.items() if v.get('size', 0) == 0]
            for k in to_remove:
                daily_loss += positions[k].get('pl', 0)
                del positions[k]
            bal = await fetch_wallet_balance()
            if bal:
                current_wallet_balance = bal
            exposure = sum(pos.get('size', 0) * pos.get('last_price', 0) for pos in positions.values())
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"Position update error: {e}")
            await asyncio.sleep(30)

async def handle_position_exit(token: str, pos: Dict[str, Any], last_price: float):
    try:
        # (Omitted for brevity, use your detailed code here; position exit logic unchanged)
        pass
    except Exception as e:
        logger.error(f"Position exit handler error for {token}: {e}")

import asyncio
import json
from aiohttp import web

# ==== DASHBOARD ====
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TOXIBOT v2 | TRON INTERFACE</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Share+Tech+Mono&display=swap');
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            background: #000;
            color: #00ffff;
            font-family: 'Orbitron', monospace;
            overflow-x: hidden;
            position: relative;
        }
        
        /* Animated grid background */
        body::before {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-image: 
                linear-gradient(rgba(0, 255, 255, 0.1) 1px, transparent 1px),
                linear-gradient(90deg, rgba(0, 255, 255, 0.1) 1px, transparent 1px);
            background-size: 50px 50px;
            animation: grid-move 10s linear infinite;
            z-index: -2;
        }
        
        @keyframes grid-move {
            0% { transform: translate(0, 0); }
            100% { transform: translate(50px, 50px); }
        }
        
        /* Scanlines effect */
        body::after {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: repeating-linear-gradient(
                0deg,
                transparent,
                transparent 2px,
                rgba(0, 255, 255, 0.03) 2px,
                rgba(0, 255, 255, 0.03) 4px
            );
            pointer-events: none;
            z-index: 1;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            position: relative;
            z-index: 2;
        }
        
        /* Header */
        .header {
            text-align: center;
            margin-bottom: 30px;
            position: relative;
            padding: 30px 0;
        }
        
        .header::before {
            content: "";
            position: absolute;
            top: 0;
            left: -50%;
            right: -50%;
            height: 1px;
            background: linear-gradient(90deg, transparent, #00ffff, transparent);
            animation: scan 3s linear infinite;
        }
        
        @keyframes scan {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }
        
        h1 {
            font-size: 4em;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            text-shadow: 
                0 0 10px #00ffff,
                0 0 20px #00ffff,
                0 0 30px #00ffff,
                0 0 40px #0088ff;
            animation: pulse-glow 2s ease-in-out infinite;
        }
        
        @keyframes pulse-glow {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }
        
        .status-indicator {
            display: inline-block;
            padding: 10px 30px;
            margin-top: 20px;
            border: 2px solid #00ff00;
            background: rgba(0, 255, 0, 0.1);
            font-weight: 700;
            text-transform: uppercase;
            position: relative;
            overflow: hidden;
        }
        
        .status-indicator.active {
            color: #00ff00;
            text-shadow: 0 0 10px #00ff00;
        }
        
        .status-indicator.inactive {
            border-color: #ff0066;
            background: rgba(255, 0, 102, 0.1);
            color: #ff0066;
            text-shadow: 0 0 10px #ff0066;
        }
        
        .status-indicator::before {
            content: "";
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
            animation: sweep 3s linear infinite;
        }
        
        @keyframes sweep {
            0% { left: -100%; }
            100% { left: 100%; }
        }
        
        /* Metrics Grid */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .metric-card {
            background: rgba(0, 20, 40, 0.8);
            border: 1px solid #00ffff;
            padding: 20px;
            position: relative;
            overflow: hidden;
            transition: all 0.3s ease;
        }
        
        .metric-card::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, #00ffff, transparent);
            animation: slide 2s linear infinite;
        }
        
        .metric-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0, 255, 255, 0.3);
            border-color: #00ff00;
        }
        
        .metric-label {
            font-size: 0.9em;
            color: #0088ff;
            text-transform: uppercase;
            margin-bottom: 10px;
            letter-spacing: 0.1em;
        }
        
        .metric-value {
            font-size: 2em;
            font-weight: 700;
            font-family: 'Share Tech Mono', monospace;
            text-shadow: 0 0 10px currentColor;
        }
        
        .positive { color: #00ff00; }
        .negative { color: #ff0066; }
        
        /* Bot Cards */
        .bots-section {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .bot-card {
            background: linear-gradient(135deg, rgba(0, 50, 100, 0.3), rgba(0, 20, 40, 0.3));
            border: 2px solid #0088ff;
            padding: 25px;
            position: relative;
            clip-path: polygon(0 0, calc(100% - 20px) 0, 100% 20px, 100% 100%, 20px 100%, 0 calc(100% - 20px));
        }
        
        .bot-card::before {
            content: "";
            position: absolute;
            top: -2px;
            left: -2px;
            right: -2px;
            bottom: -2px;
            background: linear-gradient(45deg, #00ffff, #ff00ff, #00ffff);
            z-index: -1;
            opacity: 0;
            transition: opacity 0.3s ease;
            clip-path: polygon(0 0, calc(100% - 20px) 0, 100% 20px, 100% 100%, 20px 100%, 0 calc(100% - 20px));
        }
        
        .bot-card:hover::before {
            opacity: 1;
            animation: rainbow 2s linear infinite;
        }
        
        @keyframes rainbow {
            0% { filter: hue-rotate(0deg); }
            100% { filter: hue-rotate(360deg); }
        }
        
        .bot-name {
            font-size: 1.5em;
            font-weight: 700;
            margin-bottom: 20px;
            text-transform: uppercase;
            color: #00ffff;
            text-shadow: 0 0 10px #00ffff;
        }
        
        .bot-stats {
            display: grid;
            gap: 10px;
        }
        
        .stat-row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            border-bottom: 1px solid rgba(0, 255, 255, 0.2);
        }
        
        /* Positions Table */
        .positions-section {
            margin-bottom: 30px;
        }
        
        .section-title {
            font-size: 2em;
            margin-bottom: 20px;
            text-transform: uppercase;
            position: relative;
            padding-left: 20px;
        }
        
        .section-title::before {
            content: "▶";
            position: absolute;
            left: 0;
            animation: blink 1s ease-in-out infinite;
        }
        
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        .positions-table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(0, 20, 40, 0.5);
        }
        
        .positions-table th,
        .positions-table td {
            padding: 15px;
            text-align: left;
            border: 1px solid rgba(0, 255, 255, 0.2);
        }
        
        .positions-table th {
            background: rgba(0, 100, 200, 0.3);
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }
        
        .positions-table tr:hover td {
            background: rgba(0, 255, 255, 0.1);
        }
        
        /* Activity Log */
        .log-container {
            background: rgba(0, 0, 0, 0.8);
            border: 1px solid #00ffff;
            padding: 20px;
            height: 300px;
            overflow-y: auto;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.9em;
            position: relative;
        }
        
        .log-container::before {
            content: "SYSTEM LOG";
            position: absolute;
            top: -15px;
            left: 20px;
            background: #000;
            padding: 0 10px;
            color: #00ffff;
            font-weight: 700;
        }
        
        .log-entry {
            margin-bottom: 5px;
            padding: 5px;
            border-left: 2px solid #0088ff;
            padding-left: 10px;
            opacity: 0;
            animation: fade-in 0.5s ease forwards;
        }
        
        @keyframes fade-in {
            to { opacity: 1; }
        }
        
        .log-entry.success { border-color: #00ff00; }
        .log-entry.error { border-color: #ff0066; }
        .log-entry.warning { border-color: #ffaa00; }
        
        /* Scrollbar styling */
        ::-webkit-scrollbar {
            width: 10px;
        }
        
        ::-webkit-scrollbar-track {
            background: rgba(0, 20, 40, 0.5);
        }
        
        ::-webkit-scrollbar-thumb {
            background: #00ffff;
            border-radius: 5px;
        }
        
        ::-webkit-scrollbar-thumb:hover {
            background: #00ff00;
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            h1 { font-size: 2.5em; }
            .metric-value { font-size: 1.5em; }
            .container { padding: 10px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>TOXIBOT v2.0</h1>
            <div id="status" class="status-indicator active">SYSTEM ACTIVE</div>
        </div>
        
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Wallet Balance</div>
                <div class="metric-value positive" id="wallet">0.00 SOL</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Total P/L</div>
                <div class="metric-value" id="total-pl">+0.000</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Win Rate</div>
                <div class="metric-value" id="winrate">0.0%</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Active Positions</div>
                <div class="metric-value" id="positions-count">0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Exposure</div>
                <div class="metric-value" id="exposure">0.000 SOL</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Daily Loss</div>
                <div class="metric-value negative" id="daily-loss">0.000 SOL</div>
            </div>
        </div>
        
        <div class="bots-section">
            <div class="bot-card">
                <div class="bot-name">Ultra-Early Discovery</div>
                <div class="bot-stats">
                    <div class="stat-row">
                        <span>Trades</span>
                        <span id="ultra-trades">0/0</span>
                    </div>
                    <div class="stat-row">
                        <span>Win Rate</span>
                        <span id="ultra-winrate">0%</span>
                    </div>
                    <div class="stat-row">
                        <span>P/L</span>
                        <span id="ultra-pl">+0.000</span>
                    </div>
                </div>
            </div>
            
            <div class="bot-card">
                <div class="bot-name">2-Minute Scalper</div>
                <div class="bot-stats">
                    <div class="stat-row">
                        <span>Trades</span>
                        <span id="scalper-trades">0/0</span>
                    </div>
                    <div class="stat-row">
                        <span>Win Rate</span>
                        <span id="scalper-winrate">0%</span>
                    </div>
                    <div class="stat-row">
                        <span>P/L</span>
                        <span id="scalper-pl">+0.000</span>
                    </div>
                </div>
            </div>
            
            <div class="bot-card">
                <div class="bot-name">Community/Whale</div>
                <div class="bot-stats">
                    <div class="stat-row">
                        <span>Trades</span>
                        <span id="community-trades">0/0</span>
                    </div>
                    <div class="stat-row">
                        <span>Win Rate</span>
                        <span id="community-winrate">0%</span>
                    </div>
                    <div class="stat-row">
                        <span>P/L</span>
                        <span id="community-pl">+0.000</span>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="positions-section">
            <h2 class="section-title">Active Positions</h2>
            <table class="positions-table">
                <thead>
                    <tr>
                        <th>Token</th>
                        <th>Source</th>
                        <th>Size</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>P/L</th>
                        <th>P/L %</th>
                        <th>Phase</th>
                        <th>Age</th>
                    </tr>
                </thead>
                <tbody id="positions-tbody">
                    <!-- Positions will be populated here -->
                </tbody>
            </table>
        </div>
        
        <div class="log-section">
            <h2 class="section-title">System Activity</h2>
            <div class="log-container" id="log-container">
                <!-- Log entries will be populated here -->
            </div>
        </div>
    </div>
    
    <script>
        // WebSocket connection
        const ws = new WebSocket(`ws://${location.host}/ws`);
        
        // Format numbers with appropriate precision
        function formatNumber(num, decimals = 3) {
            return parseFloat(num || 0).toFixed(decimals);
        }
        
        // Format age from seconds
        function formatAge(seconds) {
            if (!seconds) return '';
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            
            const parts = [];
            if (d) parts.push(`${d}d`);
            if (h) parts.push(`${h}h`);
            if (m) parts.push(`${m}m`);
            if (s && !d && !h) parts.push(`${s}s`);
            
            return parts.join(' ') || '0s';
        }
        
        // Update UI with data from WebSocket
        ws.onmessage = function(event) {
            const data = JSON.parse(event.data);
            
            // Update status
            const statusEl = document.getElementById('status');
            const isActive = data.status && data.status.toLowerCase().includes('live');
            statusEl.className = `status-indicator ${isActive ? 'active' : 'inactive'}`;
            statusEl.textContent = isActive ? 'SYSTEM ACTIVE' : 'SYSTEM OFFLINE';
            
            // Update metrics
            document.getElementById('wallet').textContent = `${formatNumber(data.wallet_balance, 2)} SOL`;
            document.getElementById('total-pl').textContent = `${data.pl >= 0 ? '+' : ''}${formatNumber(data.pl)}`;
            document.getElementById('total-pl').className = `metric-value ${data.pl >= 0 ? 'positive' : 'negative'}`;
            document.getElementById('winrate').textContent = `${formatNumber(data.winrate, 1)}%`;
            document.getElementById('positions-count').textContent = Object.keys(data.positions || {}).length;
            document.getElementById('exposure').textContent = `${formatNumber(data.exposure)} SOL`;
            document.getElementById('daily-loss').textContent = `${formatNumber(data.daily_loss)} SOL`;
            
            // Update bot stats
            document.getElementById('ultra-trades').textContent = `${data.ultra_wins}/${data.ultra_total}`;
            document.getElementById('ultra-winrate').textContent = `${data.ultra_total ? formatNumber(100 * data.ultra_wins / data.ultra_total, 1) : 0}%`;
            document.getElementById('ultra-pl').textContent = `${data.ultra_pl >= 0 ? '+' : ''}${formatNumber(data.ultra_pl)}`;
            document.getElementById('ultra-pl').className = data.ultra_pl >= 0 ? 'positive' : 'negative';
            
            document.getElementById('scalper-trades').textContent = `${data.scalper_wins}/${data.scalper_total}`;
            document.getElementById('scalper-winrate').textContent = `${data.scalper_total ? formatNumber(100 * data.scalper_wins / data.scalper_total, 1) : 0}%`;
            document.getElementById('scalper-pl').textContent = `${data.scalper_pl >= 0 ? '+' : ''}${formatNumber(data.scalper_pl)}`;
            document.getElementById('scalper-pl').className = data.scalper_pl >= 0 ? 'positive' : 'negative';
            
            document.getElementById('community-trades').textContent = `${data.community_wins}/${data.community_total}`;
            document.getElementById('community-winrate').textContent = `${data.community_total ? formatNumber(100 * data.community_wins / data.community_total, 1) : 0}%`;
            document.getElementById('community-pl').textContent = `${data.community_pl >= 0 ? '+' : ''}${formatNumber(data.community_pl)}`;
            document.getElementById('community-pl').className = data.community_pl >= 0 ? 'positive' : 'negative';
            
            // Update positions table
            const tbody = document.getElementById('positions-tbody');
            tbody.innerHTML = '';
            
            const now = Date.now() / 1000;
            Object.entries(data.positions || {}).forEach(([token, pos]) => {
                const entry = parseFloat(pos.entry_price || 0);
                const last = parseFloat(pos.last_price || entry);
                const size = parseFloat(pos.size || 0);
                const pl = (last - entry) * size;
                const plPct = entry ? 100 * (last - entry) / entry : 0;
                const age = now - (pos.buy_time || now);
                
                const row = tbody.insertRow();
                row.innerHTML = `
                    <td style="color: #00ffff">${token.slice(0, 6)}...${token.slice(-4)}</td>
                    <td>${pos.src || ''}</td>
                    <td>${formatNumber(size)}</td>
                    <td>${formatNumber(entry, 6)}</td>
                    <td>${formatNumber(last, 6)}</td>
                    <td class="${pl >= 0 ? 'positive' : 'negative'}">${formatNumber(pl, 4)}</td>
                    <td class="${plPct >= 0 ? 'positive' : 'negative'}">${formatNumber(plPct, 2)}%</td>
                    <td>${pos.phase || ''}</td>
                    <td>${formatAge(age)}</td>
                `;
            });
            
            // Update activity log
            const logContainer = document.getElementById('log-container');
            const logEntries = (data.log || []).slice(-50);
            logContainer.innerHTML = logEntries.map(entry => {
                let className = 'log-entry';
                if (entry.includes('BUY') || entry.includes('Sold')) className += ' success';
                else if (entry.includes('SL') || entry.includes('blacklist')) className += ' error';
                else if (entry.includes('skipping') || entry.includes('FAIL')) className += ' warning';
                
                return `<div class="${className}">${entry}</div>`;
            }).join('');
            logContainer.scrollTop = logContainer.scrollHeight;
        };
        
        // Reconnect on close
        ws.onclose = function() {
            setTimeout(() => {
                location.reload();
            }, 5000);
        };
    </script>
</body>
</html>
"""
async def html_handler(request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")
    
    async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    while True:
         data = {
            "wallet_balance": current_wallet_balance,
            "pl": get_total_pl(),
            "winrate": calc_winrate(),
            "positions": positions,
            "exposure": exposure,
            "daily_loss": daily_loss,
            "log": list(activity_log)[-40:],
            "ultra_wins": ultra_wins,
            "ultra_total": ultra_total,
            "ultra_pl": ultra_pl,
            "scalper_wins": scalper_wins,
            "scalper_total": scalper_total,
            "scalper_pl": scalper_pl,
            "community_wins": community_wins,
            "community_total": community_total,
            "community_pl": community_pl,
        }
        await ws.send_str(json.dumps(data))
        await asyncio.sleep(2)

app = web.Application()
app.router.add_get('/', html_handler)
app.router.add_get('/ws', ws_handler)

if __name__ == "__main__":
    web.run_app(app, port=8080)

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    while True:
        data = {
            'wallet_balance': current_wallet_balance,
            'pl': get_total_pl(),
            'winrate': calc_winrate(),
            'positions': positions,
            'exposure': exposure,
            'daily_loss': daily_loss,
            'log': list(activity_log)[-40:],  # last 40 log entries
            # Unpack bot stats:
            **bot_stats
        }
        await ws.send_str(json.dumps(data))
        await asyncio.sleep(2)   # Update every 2 seconds
    # (Never reached)
    # await ws.close()
    # return ws

async def html_handler(request):
    return web.FileResponse(DASHBOARD_FILE)

async def run_dashboard_server(port=8080):
    app = web.Application()
    app.router.add_get('/', html_handler)
    app.router.add_get('/ws', ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, port=port)
    await site.start()
    print(f"Dashboard up at http://0.0.0.0:{port}")

# ---- To run in your asyncio main() style app ----

if __name__ == "__main__":
    import sys

    # Set this to your desired port or get from env
    PORT = int(os.environ.get("PORT", "8080"))

    loop = asyncio.get_event_loop()
    loop.create_task(run_dashboard_server(PORT))
    # Also kick off your trading bot main here:
    # loop.create_task(main())

    loop.run_forever()

# ==== MAIN ====

async def main():
    global toxibot
    toxibot = ToxiBotClient(
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        TELEGRAM_STRING_SESSION,
        TOXIBOT_USERNAME
    )
    await toxibot.connect()

    feeds = [
        pumpfun_newtoken_feed(lambda token, src: asyncio.create_task(process_token(token, src))),
        moralis_trending_feed(lambda token, src: asyncio.create_task(process_token(token, src))),
        bitquery_trending_feed(lambda token, src: asyncio.create_task(process_token(token, src))),
        community_trade_manager(toxibot),
        update_position_prices_and_wallet()
    ]

    await asyncio.gather(*feeds)

if __name__ == "__main__":
    asyncio.run(main())
