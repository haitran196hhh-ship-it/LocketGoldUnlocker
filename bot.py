import asyncio
import html as html_lib
import json
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import aiohttp
from app.config import TOKEN_SETS, TOKEN_SETS_NO_DNS

HEADERS = {
    'Host': 'api.revenuecat.com',
    'Authorization': 'Bearer appl_JngFETzdodyLmCREOlwTUtXdQik',
    'Content-Type': 'application/json',
    'Accept': '*/*',
    'X-Platform': 'iOS',
    'X-Platform-Version': 'Version 26.2 (Build 23C55)',
    'X-Platform-Device': 'iPhone15,3',
    'X-Platform-Flavor': 'native',
    'X-Version': '5.41.0',
    'X-Client-Version': '2.32.2',
    'X-Client-Bundle-ID': 'com.locket.Locket',
    'X-Client-Build-Version': '3',
    'X-StoreKit2-Enabled': 'true',
    'X-StoreKit-Version': '2',
    'X-Observer-Mode-Enabled': 'false',
    'X-Storefront': 'VNM',
    'X-Apple-Device-Identifier': '39A73C25-1E05-4350-ADA7-5CD3FE1079E8',
    'X-Preferred-Locales': 'vi_KR,ko_KR,en_KR',
    'X-Nonce': 'w0Mlb6+AmV4WYuVv',
    'X-Is-Backgrounded': 'false',
    'X-Retry-Count': '0',
    'X-Is-Debug-Build': 'false',
    'User-Agent': 'Locket/3 CFNetwork/3860.300.31 Darwin/25.2.0',
    'Accept-Language': 'vi-VN,vi;q=0.9',
    'Connection': 'keep-alive',
    'Pragma': 'no-cache',
    'Cache-Control': 'no-cache',
    'X-RevenueCat-ETag': '',
}

class Clr:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

_UID_RE = re.compile(r'/invites/([A-Za-z0-9]{28})', re.IGNORECASE)
_INVITE_TOKEN_RE = re.compile(r'/invites/([A-Za-z0-9]{28,80})', re.IGNORECASE)
_PROFILE_IMAGE_RE = re.compile(
    r'<img[^>]*class=["\'][^"\']*profile-pic-img[^"\']*["\'][^>]*'
    r'src\s*=\s*(?:["\']([^"\']+)["\']|([^\s>]+))',
    re.IGNORECASE,
)
_ALLOWED_AVATAR_HOSTS = {'firebasestorage.googleapis.com', 'storage.googleapis.com'}
_PROFILE_CACHE = {}
_AVATAR_BY_UID = {}
_PROFILE_CACHE_TTL = 600

def _decode_repeatedly(value):
    result = str(value or '').strip()
    for _ in range(3):
        decoded = unquote(result)
        if decoded == result: break
        result = decoded
    return result

def _profile_page_url(value):
    raw = str(value or '').strip()
    if not raw: return None
    parsed = urlparse(raw if '://' in raw else '')
    host = (parsed.hostname or '').lower()
    if host == 'locket.page.link':
        nested = parse_qs(parsed.query).get('link', [None])[0]
        return _profile_page_url(nested) if nested else None
    if host in {'locket.camera', 'www.locket.camera'}:
        match = _INVITE_TOKEN_RE.search(parsed.path)
        if match: return f'https://locket.camera/invites/{match.group(1)}'
    if host in {'locket.cam', 'www.locket.cam'}:
        username = unquote(parsed.path).strip('/').split('/', 1)[0]
        if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', username): return f"https://locket.cam/{quote(username, safe='._-')}"
        return None
    decoded = _decode_repeatedly(raw)
    invite_match = _INVITE_TOKEN_RE.search(decoded)
    if invite_match: return f'https://locket.camera/invites/{invite_match.group(1)}'
    username = decoded.lstrip('@').split('?', 1)[0].strip().strip('/')
    if re.fullmatch(r'[A-Za-z0-9._-]{1,64}', username): return f"https://locket.cam/{quote(username, safe='._-')}"
    return None

def _extract_avatar_url(page_html):
    match = _PROFILE_IMAGE_RE.search(page_html or '')
    if not match: return None
    raw_url = next((value for value in match.groups() if value), None)
    if not raw_url: return None
    avatar_url = html_lib.unescape(raw_url).strip()
    if avatar_url.startswith('//'): avatar_url = 'https:' + avatar_url
    parsed = urlparse(avatar_url)
    hostname = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or hostname not in _ALLOWED_AVATAR_HOSTS: return None
    if 'token=' not in parsed.query: return None
    return avatar_url

async def _get_public_profile(username_or_link):
    page_url = _profile_page_url(username_or_link)
    if not page_url: return {'uid': None, 'avatar_url': None}
    now = time.monotonic()
    cached = _PROFILE_CACHE.get(page_url)
    if cached and now - cached[0] < _PROFILE_CACHE_TTL: return cached[1]
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml',
    }
    result = {'uid': None, 'avatar_url': None}
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(page_url, headers=headers, allow_redirects=True) as response:
                if response.status != 200: return result
                page_html = await response.text(errors='ignore')
                uid_source = _decode_repeatedly(f'{response.url}\n{page_html}')
                uid_match = _UID_RE.search(uid_source)
                result = {
                    'uid': uid_match.group(1) if uid_match else None,
                    'avatar_url': _extract_avatar_url(page_html),
                }
    except (aiohttp.ClientError, asyncio.TimeoutError): return result
    _PROFILE_CACHE[page_url] = (now, result)
    if result['uid']: _AVATAR_BY_UID[result['uid']] = (now, result['avatar_url'])
    return result

async def resolve_uid(username):
    if not username: return None
    profile = await _get_public_profile(username)
    if profile.get('uid'): return profile['uid']
    decoded = _decode_repeatedly(username)
    match_link = _UID_RE.search(decoded)
    return match_link.group(1) if match_link else None

async def get_user_avatar(username_or_uid_or_link):
    if not username_or_uid_or_link: return None
    value = str(username_or_uid_or_link).strip()
    now = time.monotonic()
    if re.fullmatch(r'[A-Za-z0-9]{28}', value):
        cached = _AVATAR_BY_UID.get(value)
        if cached and now - cached[0] < _PROFILE_CACHE_TTL: return cached[1]
        return None
    profile = await _get_public_profile(value)
    return profile.get('avatar_url')

async def check_status(uid):
    if not uid: return {'active': False}
    url = f'https://api.revenuecat.com/v1/subscribers/{uid}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=10) as res:
                if 200 <= res.status < 300:
                    data = await res.json()
                    entitlements = data.get('subscriber', {}).get('entitlements', {}).get('Gold', {})
                    if entitlements:
                        return {'active': True, 'expires': entitlements.get('expires_date')}
                return {'active': False}
    except: return None

async def check_user_gold_status(uid: str, token: str = None) -> bool:
    status = await check_status(uid)
    return bool(status and status.get('active'))


# === LUỒNG 1: INJECT DNS (CHO GÓI THƯỜNG / VIP NEW / VIP GOLD) ===
async def inject_gold(uid, token_config, log_callback=None):
    def log(msg):
        if log_callback: log_callback(msg)
    url = 'https://api.revenuecat.com/v1/receipts'
    fetch_token = token_config['fetch_token']
    app_transaction = token_config['app_transaction']
    is_sandbox = token_config.get('is_sandbox', True)
    product_id = token_config.get('product_id', 'locket_199_1m')

    body = {
        'product_id': product_id,
        'fetch_token': fetch_token,
        'app_transaction': app_transaction,
        'app_user_id': uid,
        'is_restore': True,
        'store_country': 'VNM',
        'currency': 'USD',
        'price': '1.99',
        'normal_duration': 'P1M',
        'subscription_group_id': '21419447',
        'initiation_source': 'restore',
        'attributes': {'$attConsentStatus': {'updated_at_ms': int(time.time() * 1000), 'value': 'notDetermined'}},
    }

    current_headers = HEADERS.copy()
    current_headers['Content-Length'] = str(len(json.dumps(body)))

    if token_config.get('hash_params'):
        current_headers['X-Post-Params-Hash: app_user_id,fetch_token,app_transaction:' + token_config['hash_params']] = token_config['hash_params']
    if token_config.get('hash_headers'):
        current_headers['X-Headers-Hash: X-Is-Sandbox:' + token_config['hash_headers']] = token_config['hash_headers']

    current_headers['X-Is-Sandbox'] = str(is_sandbox).lower()
    avatar_url = await get_user_avatar(uid)

    async with aiohttp.ClientSession() as session:
        for attempt in range(5):
            try:
                async with session.post(url, headers=current_headers, json=body, timeout=15) as res:
                    if res.status == 200:
                        status = await check_status(uid)
                        if status and status.get('active'): return True, 'SUCCESS', avatar_url
                        await asyncio.sleep(2)
                        status = await check_status(uid)
                        if status and status.get('active'): return True, 'SUCCESS', avatar_url
                        return False, 'Accepted but NO Gold (Expired?)', avatar_url
                    elif res.status == 529:
                        await asyncio.sleep(2)
                        continue
                    else:
                        resp_json = await res.json()
                        return False, f"Rejected: {resp_json.get('message', res.status)}", avatar_url
            except Exception as e:
                if attempt == 4: return False, f'Request Error: {str(e)}', avatar_url
                await asyncio.sleep(2)
    return False, 'Timeout / Failed after retries', avatar_url


# === LUỒNG 2: INJECT NO DNS TRỰC TIẾP (CHO GÓI 89K VIP PREMIUM ULTIMATE) ===
async def inject_gold_no_dns(uid, token_config=None, log_callback=None):
    def log(msg):
        if log_callback: log_callback(msg)

    cfg = token_config if token_config else (TOKEN_SETS_NO_DNS[0] if TOKEN_SETS_NO_DNS else None)
    if not cfg: return False, "Chưa cấu hình Token No DNS", None

    fetch_token = cfg.get('fetch_token', '')
    app_transaction = cfg.get('app_transaction', '')

    url = 'https://api.revenuecat.com/v1/receipts'
    is_sandbox = cfg.get('is_sandbox', False)
    product_id = cfg.get('product_id', 'locket_yearly_3600')

    body = {
        'product_id': product_id,
        'fetch_token': fetch_token,
        'app_transaction': app_transaction,
        'app_user_id': uid,
        'is_restore': True,
        'store_country': 'VNM',
        'currency': 'VND',
        'price': '35000',
        'normal_duration': 'P1Y',
        'subscription_group_id': '21419447',
        'initiation_source': 'restore',
        'attributes': {'$attConsentStatus': {'updated_at_ms': int(time.time() * 1000), 'value': 'notDetermined'}},
    }

    current_headers = HEADERS.copy()
    current_headers['Content-Length'] = str(len(json.dumps(body)))

    if cfg.get('hash_params'):
        current_headers['X-Post-Params-Hash: app_user_id,fetch_token,app_transaction:' + cfg['hash_params']] = cfg['hash_params']
    if cfg.get('hash_headers'):
        current_headers['X-Headers-Hash: X-Is-Sandbox:' + cfg['hash_headers']] = cfg['hash_headers']

    current_headers['X-Is-Sandbox'] = str(is_sandbox).lower()
    avatar_url = await get_user_avatar(uid)

    async with aiohttp.ClientSession() as session:
        for attempt in range(5):
            try:
                async with session.post(url, headers=current_headers, json=body, timeout=15) as res:
                    if res.status == 200:
                        status = await check_status(uid)
                        if status and status.get('active'): return True, 'SUCCESS_NO_DNS', avatar_url
                        await asyncio.sleep(2)
                        status = await check_status(uid)
                        if status and status.get('active'): return True, 'SUCCESS_NO_DNS', avatar_url
                        return False, 'Biên lai được chấp thuận nhưng chưa cấp cờ Gold.', avatar_url
                    elif res.status == 529:
                        await asyncio.sleep(2)
                        continue
                    else:
                        resp_json = await res.json()
                        return False, f"Rejected: {resp_json.get('message', res.status)}", avatar_url
            except Exception as e:
                if attempt == 4: return False, f'Request Error: {str(e)}', avatar_url
                await asyncio.sleep(2)
    return False, 'Timeout / Failed after retries', avatar_url
