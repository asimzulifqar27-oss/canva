import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import sqlite3
import time
import re
import os
import threading
import html
import atexit
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from premium_emojis import EMOJI_MAP

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Bot Config ────────────────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")

# ── Persistent Admin System ──────────────────────────────────────
# Hardcoded owner IDs (cannot be removed via commands)
OWNER_IDS = {7604473724, 5937217262, 6330429432}
# Runtime admin set (loaded from DB at start + owners)
ADMIN_IDS = set(OWNER_IDS)

CHANNELS = ["@siddmethodsgiveway"]
DB_PATH = os.getenv("CANVA_DB_PATH", os.path.join(BASE_DIR, "canva_bot (1).db"))
DUOLINGO_ACCOUNTS_PATH = os.getenv("DUOLINGO_ACCOUNTS_PATH", os.path.join(BASE_DIR, "account.txt"))
SURFSHARK_STORAGE = os.getenv("SURFSHARK_STORAGE_STATE", os.path.join(BASE_DIR, "storage_state.json"))
SURFSHARK_CODE_URL = os.getenv("SURFSHARK_CODE_URL", "https://my.surfshark.com/account/login-code")
SURFSHARK_CODE_RE = re.compile(r"^([A-Za-z0-9]{6})$")
DUOLINGO_EMOJI_ID = "5796371348808799072"
surfshark_lock = threading.Lock()
surfshark_playwright = None
surfshark_browser = None
surfshark_context = None

# ── Canva Auto-Invite Config ─────────────────────────────────────
import json
CANVA_STORAGE = os.path.join(BASE_DIR, "canva_storage_state.json")
CANVA_PEOPLE_URL = os.getenv("CANVA_PEOPLE_URL", "https://www.canva.com/settings/people")
CANVA_CDP_URL = os.getenv("CANVA_CDP_URL", "http://127.0.0.1:9222")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
CANVA_INVITE_HOSTS = {"canva.com", "www.canva.com"}
COOLDOWNS = {"canva_business": 30, "canva_pro": 30, "surfshark": 20}
SURFSHARK_CREDIT_COST = 2
cooldown_hits = {}
canva_lock = threading.Lock()
duolingo_lock = threading.Lock()
canva_playwright = None
canva_browser = None
canva_context = None
canva_page = None
CANCEL_TEXTS = {"Cancel", "❌ Cancel"}
MENU_BUTTONS = {
    "🔙 Back to Main", "🛒 Buy Premium / Panel", "👤 My Account",
    "🔗 Refer & Earn", "📞 Support", "💼 Canva Business", "👑 Canva Pro",
    "🦈 Surfshark Login", "Duolingo", "🔫 Duolingo", "🦉 Duolingo", "⚙️ Admin Panel",
}

bot = telebot.TeleBot(TOKEN, threaded=False)

# ── Premium Emoji Engine ──────────────────────────────────────────
PREMIUM_EMOJI_ITEMS = sorted(EMOJI_MAP.items(), key=lambda item: len(item[0]), reverse=True)
HTML_PROTECTED_RE = re.compile(
    r"(<tg-emoji\b[^>]*>.*?</tg-emoji>|<code>.*?</code>|<pre>.*?</pre>|<[^>]+>)",
    re.DOTALL | re.IGNORECASE,
)

def premium_emoji_html(text):
    if not isinstance(text, str):
        return text
    parts = HTML_PROTECTED_RE.split(text)
    for index, part in enumerate(parts):
        if not part or HTML_PROTECTED_RE.fullmatch(part):
            continue
        for emoji, emoji_id in PREMIUM_EMOJI_ITEMS:
            part = part.replace(emoji, f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>')
        parts[index] = part
    return "".join(parts)

def format_html(text):
    if not isinstance(text, str):
        return text
    text = text.replace("\\_", "_")
    text = text.replace("\\*", "*")
    text = text.replace("\\`", "`")
    # 1. Replace backticks first to create code blocks
    text = re.sub(r'`([^`]*?)`', lambda m: f"<code>{html.escape(m.group(1))}</code>", text, flags=re.DOTALL)
    
    # 2. Protect HTML tags from further bold/italic formatting
    parts = HTML_PROTECTED_RE.split(text)
    for index, part in enumerate(parts):
        if not part or HTML_PROTECTED_RE.fullmatch(part):
            continue
        # Only format bold and italic outside of HTML tags
        part = re.sub(r'\*(.*?)\*', r'<b>\1</b>', part, flags=re.DOTALL)
        part = re.sub(r'_(.*?)_', r'<i>\1</i>', part, flags=re.DOTALL)
        parts[index] = part
        
    return premium_emoji_html("".join(parts))

# ── Monkey-patch telebot send methods for auto HTML + premium emoji ──
original_send_message = telebot.TeleBot.send_message
def custom_send_message(self, chat_id, text, *args, **kwargs):
    kwargs['parse_mode'] = 'HTML'
    text = format_html(text)
    return original_send_message(self, chat_id, text, *args, **kwargs)
telebot.TeleBot.send_message = custom_send_message

original_edit_message_text = telebot.TeleBot.edit_message_text
def custom_edit_message_text(self, text, *args, **kwargs):
    kwargs['parse_mode'] = 'HTML'
    text = format_html(text)
    return original_edit_message_text(self, text, *args, **kwargs)
telebot.TeleBot.edit_message_text = custom_edit_message_text

original_send_document = telebot.TeleBot.send_document
def custom_send_document(self, chat_id, document, *args, **kwargs):
    if isinstance(kwargs.get('caption'), str):
        kwargs['parse_mode'] = 'HTML'
        kwargs['caption'] = format_html(kwargs['caption'])
    return original_send_document(self, chat_id, document, *args, **kwargs)
telebot.TeleBot.send_document = custom_send_document

# ── Inline button: FORCE icon_custom_emoji_id support ─────────────
# pyTelegramBotAPI 4.24.0 does NOT include icon_custom_emoji_id in to_dict()
# We must patch BOTH __init__ (to store it) AND to_dict (to send it to API)
original_inline_btn_init = InlineKeyboardButton.__init__
original_inline_btn_to_dict = InlineKeyboardButton.to_dict

def custom_inline_btn_init(self, text, *args, **kwargs):
    # Extract icon_custom_emoji_id if passed directly
    forced_icon_id = kwargs.pop('icon_custom_emoji_id', None)
    original_text = text
    matched_emoji = None
    matched_id = None
    # Try to match emoji from EMOJI_MAP
    for emoji, eid in PREMIUM_EMOJI_ITEMS:
        if text.startswith(emoji):
            matched_emoji = emoji
            matched_id = str(eid)
            break
    if matched_emoji and matched_id:
        clean_text = text[len(matched_emoji):].strip()
        original_inline_btn_init(self, clean_text, *args, **kwargs)
        self._icon_custom_emoji_id = forced_icon_id or matched_id
    else:
        original_inline_btn_init(self, original_text, *args, **kwargs)
        self._icon_custom_emoji_id = forced_icon_id

def custom_inline_btn_to_dict(self):
    d = original_inline_btn_to_dict(self)
    icon_id = getattr(self, '_icon_custom_emoji_id', None)
    if icon_id:
        d['icon_custom_emoji_id'] = str(icon_id)
    return d

InlineKeyboardButton.__init__ = custom_inline_btn_init
InlineKeyboardButton.to_dict = custom_inline_btn_to_dict

# ── Reply keyboard button: FORCE icon_custom_emoji_id support ─────
original_keyboard_btn_init = KeyboardButton.__init__
original_keyboard_btn_to_dict = KeyboardButton.to_dict

def custom_keyboard_btn_init(self, text, *args, **kwargs):
    forced_icon_id = kwargs.pop('icon_custom_emoji_id', None)
    original_text = text
    matched_emoji = None
    matched_id = None
    for emoji, eid in PREMIUM_EMOJI_ITEMS:
        if text.startswith(emoji):
            matched_emoji = emoji
            matched_id = str(eid)
            break
    if matched_emoji and matched_id:
        clean_text = text[len(matched_emoji):].strip()
        original_keyboard_btn_init(self, clean_text, *args, **kwargs)
        self._icon_custom_emoji_id = forced_icon_id or matched_id
    else:
        original_keyboard_btn_init(self, original_text, *args, **kwargs)
        self._icon_custom_emoji_id = forced_icon_id

def custom_keyboard_btn_to_dict(self):
    d = original_keyboard_btn_to_dict(self)
    icon_id = getattr(self, '_icon_custom_emoji_id', None)
    if icon_id:
        d['icon_custom_emoji_id'] = str(icon_id)
    return d

KeyboardButton.__init__ = custom_keyboard_btn_init
KeyboardButton.to_dict = custom_keyboard_btn_to_dict

# ── DB Setup ──────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    credits INTEGER DEFAULT 1,
                    surfshark_credits INTEGER DEFAULT 0,
                    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS pro_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    email TEXT,
                    status TEXT DEFAULT 'pending')''')
    c.execute('''CREATE TABLE IF NOT EXISTS canva_ids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canva_id TEXT,
                    type TEXT DEFAULT 'pro',
                    used INTEGER DEFAULT 0,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    added_by INTEGER,
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
                    user_id INTEGER PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    banned_by INTEGER,
                    banned_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS surfshark_cookies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    cookies TEXT,
                    status TEXT DEFAULT 'active',
                    added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS link_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    link_id INTEGER NOT NULL,
                    link TEXT NOT NULL,
                    claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pro_status', 'enabled')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_status', 'disabled')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('canva_mode', 'auto')")
    conn.commit()
    conn.close()

def upgrade_db():
    conn = get_db_connection()
    c = conn.cursor()
    for col in [
        "ALTER TABLE users ADD COLUMN referrals INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN surfshark_credits INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN referral_rewarded INTEGER DEFAULT 0",
    ]:
        try:
            c.execute(col)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

def load_admins_from_db():
    """Load persisted admins from DB into ADMIN_IDS set."""
    global ADMIN_IDS
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        conn.close()
        for row in rows:
            ADMIN_IDS.add(row['user_id'])
    except Exception:
        pass

init_db()
upgrade_db()
load_admins_from_db()

# ── Admin Helpers ────────────────────────────────────────────────
def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_owner(user_id):
    return user_id in OWNER_IDS

def add_admin_to_db(user_id, username, added_by):
    global ADMIN_IDS
    ADMIN_IDS.add(user_id)
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
        (user_id, username, added_by)
    )
    conn.commit()
    conn.close()

def remove_admin_from_db(user_id):
    global ADMIN_IDS
    ADMIN_IDS.discard(user_id)
    conn = get_db_connection()
    conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def notify_admins(text, **kwargs):
    for admin_id in list(ADMIN_IDS):
        try:
            bot.send_message(admin_id, text, **kwargs)
        except Exception:
            pass

# ── Settings ─────────────────────────────────────────────────────
def get_setting(key):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else None

def update_setting(key, value):
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def is_maintenance_enabled():
    return get_setting('maintenance_status') == 'enabled'

def send_maintenance_message(chat_id):
    bot.send_message(
        chat_id,
        "🛠 *Bot Maintenance*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ The bot is being upgraded right now.\n"
        "🔄 Please try again in a little while.\n\n"
        "✨ _Good things take time!_"
    )

def block_member_during_maintenance(message):
    if not is_admin(message.from_user.id) and is_maintenance_enabled():
        send_maintenance_message(message.chat.id)
        return True
    return False

# ── User Helpers ─────────────────────────────────────────────────
def check_channels(user_id):
    for channel in CHANNELS:
        try:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

def get_user(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return user

def add_user(user_id, username):
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def update_credits(user_id, amount):
    if is_admin(user_id) and amount < 0:
        return
    conn = get_db_connection()
    conn.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def update_surfshark_credits(user_id, amount):
    if is_admin(user_id) and amount < 0:
        return
    conn = get_db_connection()
    conn.execute("UPDATE users SET surfshark_credits = surfshark_credits + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def set_pending_referral(user_id, referrer_id):
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET referred_by=?, referral_rewarded=0 WHERE user_id=? AND referred_by IS NULL",
        (referrer_id, user_id)
    )
    conn.commit()
    conn.close()

def grant_pending_referral(user_id):
    user = get_user(user_id)
    if not user or not user["referred_by"] or user["referral_rewarded"]:
        return False
    if not check_channels(user_id):
        return False

    referrer_id = user["referred_by"]
    referrer = get_user(referrer_id)
    if not referrer:
        return False

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh = conn.execute(
            "SELECT referred_by, referral_rewarded FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        if not fresh or not fresh["referred_by"] or fresh["referral_rewarded"]:
            conn.commit()
            return False
        conn.execute("UPDATE users SET referral_rewarded=1 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (referrer_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    update_credits(referrer_id, 1)
    update_surfshark_credits(referrer_id, 1)
    try:
        bot.send_message(
            referrer_id,
            "🎉 *Referral Reward Unlocked!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👤 Someone joined using your invite link and joined the channel!\n"
            "🪙 You earned *1 Canva credit* 🎁\n"
            "🦈 You earned *1 Surfshark login credit* ⚡\n\n"
            "🔗 Keep sharing to earn more!",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    return True

def short_error(error, limit=900):
    text = html.escape(str(error))
    if len(text) > limit:
        text = text[:limit] + "\n\n...error trimmed..."
    return text

# ── Canva ID Helpers ─────────────────────────────────────────────
def add_canva_id(canva_id, id_type='pro'):
    conn = get_db_connection()
    conn.execute("INSERT INTO canva_ids (canva_id, type) VALUES (?, ?)", (canva_id, id_type))
    conn.commit()
    conn.close()

def get_canva_id(id_type='pro'):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM canva_ids WHERE type=? AND used=0 ORDER BY RANDOM() LIMIT 1",
        (id_type,)
    ).fetchone()
    conn.close()
    return row

def mark_canva_id_used(cid):
    conn = get_db_connection()
    conn.execute("UPDATE canva_ids SET used=1 WHERE id=?", (cid,))
    conn.commit()
    conn.close()

def count_canva_ids(id_type='pro'):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM canva_ids WHERE type=? AND used=0",
        (id_type,)
    ).fetchone()
    conn.close()
    return row['cnt'] if row else 0

# ── Ban Helpers ──────────────────────────────────────────────────
def ban_user(user_id, reason='', banned_by=0):
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, reason, banned_by) VALUES (?, ?, ?)",
        (user_id, reason, banned_by)
    )
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

def get_banned_list():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM banned_users ORDER BY banned_date DESC").fetchall()
    conn.close()
    return rows

# ── Surfshark Browser ─────────────────────────────────────────────
def block_heavy_surfshark_assets(route):
    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
    else:
        route.continue_()

def get_surfshark_context():
    global surfshark_playwright, surfshark_browser, surfshark_context
    if not Path(SURFSHARK_STORAGE).exists():
        raise RuntimeError("storage_state.json not found. Run login.py first.")
    if surfshark_context is not None:
        return surfshark_context
    surfshark_playwright = sync_playwright().start()
    surfshark_browser = surfshark_playwright.chromium.launch(
        headless=True,
        args=["--disable-background-networking", "--disable-dev-shm-usage",
              "--disable-extensions", "--disable-sync", "--no-first-run"],
    )
    surfshark_context = surfshark_browser.new_context(storage_state=SURFSHARK_STORAGE)
    surfshark_context.route("**/*", block_heavy_surfshark_assets)
    return surfshark_context

def close_surfshark_browser():
    global surfshark_playwright, surfshark_browser, surfshark_context
    for obj in (surfshark_context, surfshark_browser, surfshark_playwright):
        if obj:
            try:
                obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
    surfshark_context = surfshark_browser = surfshark_playwright = None

atexit.register(close_surfshark_browser)

# ── Canva Helpers ────────────────────────────────────────────────
def normalize_email(raw):
    email = (raw or "").strip().split()[0] if raw else ""
    return email.lower()

def valid_email(email):
    return bool(EMAIL_RE.fullmatch(email or ""))

def check_cooldown(user_id, action):
    if is_admin(user_id):
        return 0
    now = time.monotonic()
    key = (user_id, action)
    wait = COOLDOWNS.get(action, 10) - (now - cooldown_hits.get(key, 0))
    if wait > 0:
        return int(wait) + 1
    cooldown_hits[key] = now
    return 0

def reserve_credit(user_id, column="credits"):
    if is_admin(user_id):
        return True
    if column not in {"credits", "surfshark_credits"}:
        raise ValueError("Invalid credit column")
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {column} FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row[column] <= 0:
            conn.commit()
            return False
        conn.execute(f"UPDATE users SET {column} = {column} - 1 WHERE user_id=?", (user_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def refund_credit(user_id, column="credits"):
    if not is_admin(user_id):
        if column == "credits":
            update_credits(user_id, 1)
        elif column == "surfshark_credits":
            update_surfshark_credits(user_id, 1)

def pop_duolingo_account():
    with duolingo_lock:
        if not os.path.exists(DUOLINGO_ACCOUNTS_PATH):
            return None

        with open(DUOLINGO_ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        account = None
        kept_lines = []
        for line in lines:
            stripped = line.strip()
            if account is None and stripped and not stripped.startswith("#"):
                account = stripped
                continue
            kept_lines.append(line)

        if account is None:
            return None

        with open(DUOLINGO_ACCOUNTS_PATH, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)
        return account

def format_duolingo_account(account):
    duolingo_icon = f'<tg-emoji emoji-id="{DUOLINGO_EMOJI_ID}">🔫</tg-emoji>'
    if ":" in account:
        login, password = account.split(":", 1)
        account_block = (
            f"🔑 *Login:*\n`{login.strip()}`\n\n"
            f"🔒 *Password:*\n`{password.strip()}`"
        )
    else:
        account_block = f"🔑 *Account:*\n`{account}`"

    return (
        f"{duolingo_icon}💎 *Duolingo Account Delivered!* 💎{duolingo_icon}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{account_block}\n\n"
        "✅ *Status:* Fresh from stock\n"
        "⚡ *Tip:* Login now and change the password if possible.\n\n"
        "🎉 Enjoy your account!"
    )

_SAMESITE_MAP = {
    "no_restriction": "None", "unspecified": "Lax", "lax": "Lax",
    "strict": "Strict", "none": "None",
}

def load_storage_state(path):
    """Load a Playwright storage_state file, accepting Playwright format
    or Chrome/EditThisCookie-style JSON array of cookies."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "cookies" in data:
        return data
    if not isinstance(data, list):
        raise ValueError(f"Unrecognized storage state format in {path}")
    cookies = []
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        same_site_raw = c.get("sameSite")
        same_site_key = (same_site_raw or "").lower() if isinstance(same_site_raw, str) else ""
        same_site = _SAMESITE_MAP.get(same_site_key, "Lax")
        domain = c.get("domain") or ""
        host_only = c.get("hostOnly")
        if host_only and domain.startswith("."):
            domain = domain.lstrip(".")
        elif host_only is False and domain and not domain.startswith("."):
            domain = "." + domain
        cookie = {
            "name": c["name"], "value": c["value"], "domain": domain,
            "path": c.get("path", "/"), "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)), "sameSite": same_site,
        }
        if c.get("session"):
            cookie["expires"] = -1
        elif "expirationDate" in c and c["expirationDate"] is not None:
            cookie["expires"] = int(c["expirationDate"])
        else:
            cookie["expires"] = -1
        cookies.append(cookie)
    return {"cookies": cookies, "origins": []}

# ── Canva Browser Management ─────────────────────────────────────
def block_heavy_canva_assets(route):
    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
    else:
        route.continue_()

def get_canva_context():
    global canva_playwright, canva_browser, canva_context
    if canva_context is not None:
        return canva_context
    
    canva_playwright = sync_playwright().start()
    
    # 1. Try connecting over CDP (perfect stealth if Chrome debugging instance is active)
    try:
        canva_browser = canva_playwright.chromium.connect_over_cdp(CANVA_CDP_URL)
        if canva_browser.contexts:
            canva_context = canva_browser.contexts[0]
        else:
            canva_context = canva_browser.new_context()
            
        # Pre-load cookies into the CDP context if we have them saved
        if os.path.exists(CANVA_STORAGE):
            try:
                with open(CANVA_STORAGE, 'r', encoding='utf-8') as f:
                    state = json.loads(f.read())
                if "cookies" in state:
                    canva_context.add_cookies(state["cookies"])
                    print("[+] Pre-loaded cookies from canva_storage_state.json into CDP browser context.")
            except Exception as cookie_err:
                print(f"[*] Warning: Could not pre-load cookies into CDP browser context: {cookie_err}")
                
        canva_context.route("**/*", block_heavy_canva_assets)
        print("[+] Connected to Canva Chrome instance over CDP.")
        return canva_context
    except Exception as cdp_err:
        print(f"[*] CDP connection failed (Chrome may not be running on {CANVA_CDP_URL}): {cdp_err}")
        print("[*] Falling back to standard cookies storage state with full stealth browser...")

    # 2. Fallback to launching standard browser in stealth mode using storage state
    if not Path(CANVA_STORAGE).exists():
        if canva_playwright:
            try:
                canva_playwright.stop()
            except Exception:
                pass
            canva_playwright = None
        raise RuntimeError(
            f"Canva session not found. Please connect to Chrome over CDP on port 9222, "
            f"or upload cookies via bot, or run canva_login.py. (CDP failed, and {CANVA_STORAGE} is missing)"
        )
    
    try:
        canva_browser = canva_playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking", 
                "--disable-dev-shm-usage",
                "--disable-extensions", 
                "--disable-sync", 
                "--no-first-run",
                "--disable-blink-features=AutomationControlled"
            ],
        )
        storage_data = load_storage_state(CANVA_STORAGE)
        canva_context = canva_browser.new_context(storage_state=storage_data)
        
        # Inject webdriver mask to bypass automation checks
        canva_context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        canva_context.route("**/*", block_heavy_canva_assets)
        print("[+] Launched standalone stealth Chromium browser with cookies state.")
        return canva_context
    except Exception as e:
        if canva_playwright:
            try:
                canva_playwright.stop()
            except Exception:
                pass
            canva_playwright = None
        raise e

def close_canva_browser():
    global canva_playwright, canva_browser, canva_context, canva_page
    for obj in (canva_context, canva_browser):
        if obj:
            try:
                obj.close()
            except Exception:
                pass
    if canva_playwright:
        try:
            canva_playwright.stop()
        except Exception:
            pass
    canva_page = None
    canva_context = canva_browser = canva_playwright = None

atexit.register(close_canva_browser)

def get_canva_page():
    """Return a persistent page parked on the Canva People URL."""
    global canva_page
    context = get_canva_context()
    page = canva_page
    if page is not None:
        try:
            if page.is_closed():
                page = None
        except Exception:
            page = None
    if page is None:
        page = context.new_page()
        canva_page = page
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "/team/people" not in url:
        page.goto(CANVA_PEOPLE_URL, wait_until="domcontentloaded", timeout=20000)
    return page

def _reset_canva_page():
    global canva_page
    if canva_page is not None:
        try:
            canva_page.close()
        except Exception:
            pass
    canva_page = None

def submit_canva_invite(email):
    """Automate sending a Canva Business team invite via Playwright."""
    timings = {}
    started = time.monotonic()
    with canva_lock:
        timings["browser"] = time.monotonic() - started
        step_started = time.monotonic()
        try:
            page = get_canva_page()
        except Exception as e:
            return False, (
                "💥 <b>Could not open Canva tab.</b>\n\n"
                f"<code>{short_error(e)}</code>"
            ), timings
        try:
            if "/login" in page.url:
                timings["page"] = time.monotonic() - step_started
                return False, (
                    "🔒 <b>Canva session expired on the server.</b>\n"
                    "An admin needs to run <code>canva_login.py</code> or update cookies."
                ), timings

            invite_btn = page.get_by_role("button", name=re.compile(r"invite people", re.I)).first
            invite_btn.wait_for(state="visible", timeout=10000)
            invite_btn.click()
            timings["page"] = time.monotonic() - step_started

            step_started = time.monotonic()
            email_input = page.get_by_placeholder(re.compile(r"enter email", re.I)).first
            try:
                email_input.wait_for(state="visible", timeout=8000)
            except Exception:
                email_input = page.locator('input[type="email"], input[placeholder*="email" i]').first
                email_input.wait_for(state="visible", timeout=4000)
            email_input.click()
            email_input.fill(email)
            page.keyboard.press("Tab")
            timings["type"] = time.monotonic() - step_started

            step_started = time.monotonic()
            confirm_btn = page.get_by_role("button", name=re.compile(r"confirm and invite", re.I)).first
            confirm_btn.wait_for(state="visible", timeout=5000)
            for _ in range(20):
                if confirm_btn.is_enabled():
                    break
                page.wait_for_timeout(100)
            confirm_btn.click(timeout=5000)
            timings["click"] = time.monotonic() - step_started

            step_started = time.monotonic()
            deadline = time.monotonic() + 12
            success = False
            error_reason = None
            
            # We want to wait for the "Done" button to appear as proof of success
            while time.monotonic() < deadline:
                # Check for success (Done button)
                try:
                    done_btn = page.get_by_role("button", name=re.compile(r"^\s*done\s*$", re.I)).first
                    if done_btn.is_visible():
                        success = True
                        timings["confirm"] = time.monotonic() - step_started
                        try:
                            done_btn.click(timeout=3000)
                        except Exception:
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                        break
                except Exception:
                    pass
                
                # Check for explicit error messages on the page
                try:
                    body = page.inner_text("body").lower()
                    if any(word in body for word in ("already", "invalid email", "couldn't send", "could not send", "error", "something went wrong", "security check", "failed security check", "rrs-")):
                        timings["confirm"] = time.monotonic() - step_started
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass
                        
                        if "security check" in body or "failed security check" in body or "rrs-" in body:
                            error_reason = (
                                "🔒 <b>Canva Security Check Blocked the Invite (Anti-Bot Check)</b>\n\n"
                                "Canva's anti-bot system (Arkose/reCAPTCHA) flagged the automated server browser (Error code: RRS).\n\n"
                                "👉 <b>How to Fix This (Choose One):</b>\n"
                                "1️⃣ <b>Switch to Link Mode (Highly Recommended):</b> Tap <code>⚙️ Admin Panel</code> -> <code>🔄 Toggle Canva Mode</code> to switch from 'auto' to 'link'. Then add your team invite link. This is 100% reliable and never gets blocked by anti-bot checks!\n"
                                "2️⃣ <b>CDP Remote Browser:</b> Start Google Chrome manually on your Windows Server VPS with remote debugging enabled on port 9222, log into Canva, and leave the Chrome window open. The bot will automatically connect to your real Chrome session and bypass anti-bot checks completely!"
                            )
                        elif "already" in body:
                            error_reason = "This email is already part of the team or has a pending invite."
                        elif "invalid email" in body:
                            error_reason = "Canva says this email address is invalid."
                        else:
                            error_reason = "Canva rejected the invite. The email might be temporarily blocked or invalid."
                        break
                except Exception:
                    pass
                
                page.wait_for_timeout(200)

            if success:
                shot_path = os.path.join(
                    BASE_DIR,
                    f"invite_{int(time.time())}_{re.sub(r'[^A-Za-z0-9]', '_', email)}.png",
                )
                try:
                    page.screenshot(path=shot_path, full_page=True)
                    timings["screenshot"] = shot_path
                except Exception:
                    pass
                    
                return True, (
                    "🎉 <b>Invite sent</b>\n\n"
                    f"📩 An invite was emailed to <code>{html.escape(email)}</code>.\n"
                    "Check the inbox (and spam) to accept the team invite."
                ), timings
                
            elif error_reason:
                shot_path = os.path.join(
                    BASE_DIR,
                    f"invite_error_{int(time.time())}_{re.sub(r'[^A-Za-z0-9]', '_', email)}.png",
                )
                try:
                    page.screenshot(path=shot_path, full_page=True)
                    timings["screenshot"] = shot_path
                except Exception:
                    pass
                return False, (
                    f"🚫 <b>Canva rejected the invite</b>\n\n"
                    f"{error_reason}"
                ), timings
                
            else:
                # Capture a screenshot to help admins diagnose
                shot_path = os.path.join(
                    BASE_DIR,
                    f"invite_timeout_{int(time.time())}_{re.sub(r'[^A-Za-z0-9]', '_', email)}.png",
                )
                try:
                    page.screenshot(path=shot_path, full_page=True)
                    timings["screenshot"] = shot_path
                except Exception:
                    pass
                    
                timings["confirm"] = time.monotonic() - step_started
                _reset_canva_page()  # Close the page to avoid being stuck in an invalid state
                return False, (
                    "⏳ <b>Invite submission timed out</b>\n\n"
                    "Canva did not return a confirmation. This usually means the browser got stuck in a loading state or was blocked by anti-bot checks. Please try again or check your cookies."
                ), timings

        except Exception as e:
            _reset_canva_page()
            return False, (
                "💥 <b>Something went wrong inside the Canva invite helper.</b>\n\n"
                f"<code>{short_error(e)}</code>"
            ), timings


def get_active_surfshark_cookie():
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM surfshark_cookies WHERE status='active' ORDER BY RANDOM() LIMIT 1").fetchone()
        conn.close()
        return row
    except Exception:
        return None

def mark_cookie_expired(cookie_id):
    try:
        conn = get_db_connection()
        conn.execute("UPDATE surfshark_cookies SET status='expired' WHERE id=?", (cookie_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_surfshark_storage_state():
    # Try to get from database pool
    row = get_active_surfshark_cookie()
    if row:
        try:
            import json
            return json.loads(row['cookies']), row['id']
        except Exception:
            pass
    # Fallback to storage_state.json
    if Path(SURFSHARK_STORAGE).exists():
        try:
            import json
            with open(SURFSHARK_STORAGE, 'r', encoding='utf-8') as f:
                return json.load(f), None
        except Exception:
            pass
    raise RuntimeError("No active Surfshark cookies in DB and storage_state.json not found.")

def submit_surfshark_code(code):
    timings = {}
    started = time.monotonic()
    with surfshark_lock:
        try:
            storage_state, cookie_id = get_surfshark_storage_state()
        except Exception as e:
            return False, (
                "🔒 <b>No active Surfshark session available.</b>\n\n"
                f"<code>{short_error(e)}</code>"
            ), timings

        global surfshark_playwright, surfshark_browser
        if surfshark_playwright is None:
            surfshark_playwright = sync_playwright().start()
        if surfshark_browser is None:
            surfshark_browser = surfshark_playwright.chromium.launch(
                headless=True,
                args=["--disable-background-networking", "--disable-dev-shm-usage",
                      "--disable-extensions", "--disable-sync", "--no-first-run"],
            )

        context = surfshark_browser.new_context(storage_state=storage_state)
        context.route("**/*", block_heavy_surfshark_assets)
        timings["browser"] = time.monotonic() - started

        page = context.new_page()
        try:
            step_started = time.monotonic()
            page.goto(SURFSHARK_CODE_URL, wait_until="commit", timeout=15000)
            if "log-in" in page.url and "login-code" not in page.url:
                timings["page"] = time.monotonic() - step_started
                if cookie_id:
                    mark_cookie_expired(cookie_id)
                return False, (
                    "🔒 <b>Session expired on the server.</b>\n"
                    "An admin needs to add a fresh cookie."
                ), timings
            inputs = page.locator('input[type="tel"], input[inputmode="numeric"], input[maxlength="1"]')
            try:
                inputs.first.wait_for(state="visible", timeout=8000)
            except Exception:
                page.locator("input").first.wait_for(state="visible", timeout=3000)
            timings["page"] = time.monotonic() - step_started
            step_started = time.monotonic()
            if inputs.count() >= 6:
                inputs.nth(0).click()
                page.keyboard.type(code)
            else:
                single = page.locator("input").first
                single.click()
                single.fill(code)
            timings["type"] = time.monotonic() - step_started
            step_started = time.monotonic()
            login_btn = page.locator("#loginSubmit, button[type='submit']").first
            login_btn.wait_for(state="visible", timeout=4000)
            login_btn.click(timeout=5000)
            timings["click"] = time.monotonic() - step_started
            step_started = time.monotonic()
            deadline = time.monotonic() + 3
            checks = 0
            while time.monotonic() < deadline:
                if "login-code/success" in page.url or "login-code" not in page.url:
                    timings["confirm"] = time.monotonic() - step_started
                    return True, (
                        "🎉 <b>You're signed in!</b>\n\n"
                        "🦈 Open the Surfshark app now.\n"
                        "✅ Your account should be ready to use."
                    ), timings
                checks += 1
                if checks % 4 == 0:
                    body = page.inner_text("body").lower()
                    if any(word in body for word in ("invalid", "expired", "incorrect", "wrong")):
                        timings["confirm"] = time.monotonic() - step_started
                        return False, (
                            "🚫 <b>Code rejected</b>\n\n"
                            "This code may be invalid or expired.\n"
                            "🔄 Please generate a fresh code in the Surfshark app and send it here."
                        ), timings
                page.wait_for_timeout(100)
            timings["confirm"] = time.monotonic() - step_started
            return False, (
                "⏳ <b>Submitted, but not confirmed</b>\n\n"
                "Surfshark did not return a clear confirmation.\n"
                "💡 Please open the app and check your login status."
            ), timings
        except Exception as e:
            return False, (
                "💥 <b>Something went wrong inside the Surfshark login helper.</b>\n\n"
                f"<code>{short_error(e)}</code>"
            ), timings
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

def handle_surfshark_code(message, raw_text):
    cleaned = raw_text.strip()
    # If the message matches known commands (e.g. with or without emojis)
    known_commands_upper = [
        "BACK TO MAIN", "BUY PREMIUM / PANEL", "MY ACCOUNT",
        "REFER & EARN", "SUPPORT", "CANVA BUSINESS", "CANVA PRO", "DUOLINGO",
        "SURFSHARK LOGIN", "ADD LINK", "VIEW LINKS",
        "BROADCAST", "STATISTICS", "GIVE CREDITS", "DELETE LINK",
        "USER INFO", "GLOBAL GIFT", "TOGGLE PRO", "MAINTENANCE",
        "ADMIN PANEL", "MESSAGE USER", "BACKUP DB", "LEADERBOARD",
        "GIVE SURF CREDITS", "SET CREDITS", "ALL USERS",
        "BAN USER", "UNBAN USER", "BANNED LIST", "PRO REQUESTS",
        "RESET USER", "SURF GLOBAL GIFT", "EXPORT USERS", "UPDATE COOKIES",
        "CANCEL"
    ]
    # Build a broad set of all individual uppercase words from known commands to avoid collision
    ignore_words = {"CANCEL", "BACKUP", "GLOBAL"}
    for cmd in known_commands_upper:
        words = re.sub(r'[^\w\s]', '', cmd).upper().split()
        ignore_words.update(words)

    # Remove emojis and strip for broad command comparison
    cmd_clean = re.sub(r'[^\w\s]', '', cleaned).strip().upper()
    if cmd_clean in ignore_words or cmd_clean in known_commands_upper:
        return False

    match = SURFSHARK_CODE_RE.match(cleaned)
    if not match:
        return False
    user = get_user(message.from_user.id)
    if not is_admin(message.from_user.id) and (not user or user['surfshark_credits'] < SURFSHARK_CREDIT_COST):
        bot.send_message(
            message.chat.id,
            "🦈 *Surfshark Locked*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🪙 You need *{SURFSHARK_CREDIT_COST} Surfshark credits* to use this.\n\n"
            "🔗 Invite a new user with your referral link to earn credits.\n"
            "⚡ One new referral = `1` Surfshark login credit.",
            parse_mode="Markdown",
            reply_markup=main_menu(message.from_user.id)
        )
        return True
    code = match.group(1).upper()
    status = bot.send_message(
        message.chat.id,
        f"🦈 <b>Checking your Surfshark code...</b>\n\n<code>{code}</code>\n\n⏱ <i>Please wait...</i>"
    )
    update_surfshark_credits(message.from_user.id, -SURFSHARK_CREDIT_COST)
    started_at = time.monotonic()
    timings = {}
    try:
        ok, reply, timings = submit_surfshark_code(code)
    except Exception as e:
        ok = False
        reply = (
            "💥 <b>Something went wrong inside the Surfshark login helper.</b>\n\n"
            f"<code>{short_error(e)}</code>"
        )
    elapsed = time.monotonic() - started_at
    header = "✅ <b>Success!</b>\n\n" if ok else "❌ <b>Could not sign in</b>\n\n"
    timing_text = (
        f"\n\n⏱ <i>Completed in {elapsed:.1f}s</i>"
        f"\n<i>Page {timings.get('page', 0):.1f}s · Type {timings.get('type', 0):.1f}s · "
        f"Click {timings.get('click', 0):.1f}s · Confirm {timings.get('confirm', 0):.1f}s</i>"
    )
    result_text = header + reply + timing_text
    if len(result_text) > 3500:
        result_text = result_text[:3500] + "\n\n<i>Message trimmed.</i>"
    try:
        bot.edit_message_text(
            result_text,
            chat_id=message.chat.id,
            message_id=status.message_id,
            parse_mode="HTML"
        )
    except Exception:
        bot.send_message(message.chat.id, result_text, parse_mode="HTML")
    bot.send_message(
        message.chat.id,
        "✅ Done. Main menu restored.",
        reply_markup=main_menu(message.from_user.id)
    )
    return True

# ── Menus ────────────────────────────────────────────────────────
def main_menu(user_id=None):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    pro_status = get_setting('pro_status')
    if pro_status == 'enabled':
        markup.row(KeyboardButton("💼 Canva Business"), KeyboardButton("👑 Canva Pro"))
    else:
        markup.row(KeyboardButton("💼 Canva Business"))
    markup.row(
        KeyboardButton("🦈 Surfshark Login"),
        KeyboardButton("Duolingo", icon_custom_emoji_id=DUOLINGO_EMOJI_ID)
    )
    markup.row(KeyboardButton("👤 My Account"), KeyboardButton("🔗 Refer & Earn"))
    markup.row(KeyboardButton("📞 Support"), KeyboardButton("🛒 Buy Premium / Panel"))
    if user_id and is_admin(user_id):
        markup.row(KeyboardButton("🛠 Maintenance"), KeyboardButton("⚙️ Admin Panel"))
    return markup

def admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    # Row 1: Links management
    markup.row(KeyboardButton("➕ Add Link"), KeyboardButton("📋 View Links"), KeyboardButton("🗑 Delete Link"))
    # Row 2: Broadcast & Stats
    markup.row(KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Statistics"), KeyboardButton("🔰 Leaderboard"))
    # Row 3: Credits gifting
    markup.row(KeyboardButton("💰 Give Credits"), KeyboardButton("🦈 Give Surf Credits"), KeyboardButton("🎯 Set Credits"))
    # Row 4: Users search & details
    markup.row(KeyboardButton("👤 User Info"), KeyboardButton("👥 All Users"), KeyboardButton("🎁 Global Gift"))
    # Row 5: Ban system
    markup.row(KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User"), KeyboardButton("📋 Banned List"))
    # Row 6: User Actions / Requests
    markup.row(KeyboardButton("👑 Pro Requests"), KeyboardButton("🔄 Reset User"), KeyboardButton("✉️ Message User"))
    # Row 7: Settings & Maintenance
    markup.row(KeyboardButton("🛠 Maintenance"), KeyboardButton("📂 Backup DB"))
    # Row 8: Surfshark & Exports
    markup.row(KeyboardButton("🦈 Surf Global Gift"), KeyboardButton("📤 Export Users"), KeyboardButton("🍪 Update Cookies"))
    # Row 9: Surfshark Cookies Pool
    markup.row(KeyboardButton("➕ Add Surf Cookie"), KeyboardButton("📋 View Surf Cookies"), KeyboardButton("🗑 Delete Surf Cookie"))
    # Row 10: Canva Management
    markup.row(KeyboardButton("🔄 Toggle Canva Mode"), KeyboardButton("🍪 Canva Cookies"), KeyboardButton("🔃 Refresh Canva"))
    # Row 11: Back Button
    markup.row(KeyboardButton("🔙 Back to Main"))
    return markup

def cancel_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("❌ Cancel"))
    return markup

def join_channels_markup():
    markup = InlineKeyboardMarkup()
    for ch in CHANNELS:
        markup.add(InlineKeyboardButton(f"📢 Join {ch}", url=f"https://t.me/{ch[1:]}"))
    markup.add(InlineKeyboardButton("✅ I have joined", callback_data="check_join"))
    return markup

# ── Text Templates ────────────────────────────────────────────────
def welcome_text():
    return (
        "👑✨ *SIDD SAGA BOT* ✨👑\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Welcome to your *premium access hub!*\n\n"
        "👨‍💻 *Owner:* @siddsaga\n"
        "🛠 *Developer:* @AsimVirus\n\n"
        "💼 *Canva Business* — Instant team invite\n"
        "👑 *Canva Pro* — Manual activation\n"
        "🦈 *Surfshark VPN* — Auto login via code\n"
        f'<tg-emoji emoji-id="{DUOLINGO_EMOJI_ID}">🔫</tg-emoji> *Duolingo* — Fresh account delivery\n\n'
        "🔗 *Refer friends* to earn free credits!\n"
        "👇 Choose an option below."
    )

def join_required_text():
    return (
        "👑✨ *SIDD SAGA BOT* ✨👑\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👨‍💻 *Owner:* @siddsaga\n"
        "🛠 *Developer:* @AsimVirus\n\n"
        "🔒 Join our channel to unlock the bot.\n\n"
        "✅ Free Canva Business access\n"
        "✅ Private Canva Pro upgrades\n"
        "✅ Surfshark VPN codes\n"
        f'✅ <tg-emoji emoji-id="{DUOLINGO_EMOJI_ID}">🔫</tg-emoji> Duolingo accounts\n'
        "✅ Fast support & updates\n\n"
        "👇 Tap below, join, then come back!"
    )

def admin_panel_text():
    return (
        "⚙️ *Admin Command Center*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔥 Full control over bot, users & content.\n\n"
        "📦 *Links* — Add, view, delete business links\n"
        "💰 *Credits* — Give, set, reset user credits\n"
        "🚫 *Ban System* — Ban/unban/list banned users\n"
        "👑 *Pro Requests* — View pending activations\n"
        "📢 *Broadcast* — Send messages to all users\n"
        "📊 *Stats* — Full bot analytics\n"
        "⚙️ *System* — Maintenance, toggle, backup\n"
        "✉️ *Messaging* — DM any user directly\n\n"
        "🍪 *Cookies* — Update Surfshark session cookies"
    )

# ── /start ────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def send_welcome(message):
    parts = message.text.split()
    referrer_id = None
    if len(parts) > 1 and parts[1].isdigit():
        referrer_id = int(parts[1])

    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        add_user(user_id, message.from_user.username)
        if referrer_id and referrer_id != user_id:
            referrer = get_user(referrer_id)
            if referrer:
                set_pending_referral(user_id, referrer_id)

    if not is_admin(user_id) and is_maintenance_enabled():
        send_maintenance_message(message.chat.id)
        return

    if not is_admin(user_id) and not check_channels(message.from_user.id):
        bot.send_message(message.chat.id, join_required_text(),
                         parse_mode="Markdown", reply_markup=join_channels_markup())
    else:
        grant_pending_referral(user_id)
        bot.send_message(message.chat.id, welcome_text(),
                         parse_mode="Markdown", reply_markup=main_menu(user_id))

# ── /addadmin <user_id> ───────────────────────────────────────────
@bot.message_handler(commands=['addadmin'])
def cmd_add_admin(message):
    if not is_owner(message.from_user.id):
        bot.send_message(message.chat.id, "❌ *Only owners can add admins.*", parse_mode="Markdown")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id,
                         "❌ *Usage:* `/addadmin <user_id>`\n\nExample: `/addadmin 123456789`",
                         parse_mode="Markdown")
        return
    new_admin_id = int(parts[1])
    username = parts[2] if len(parts) > 2 else "unknown"
    add_admin_to_db(new_admin_id, username, message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"✅ *Admin Added!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *ID:* `{new_admin_id}`\n"
        f"👨‍💻 *Username:* {username}\n"
        f"⚡ Added by: `{message.from_user.id}`",
        parse_mode="Markdown"
    )
    try:
        bot.send_message(
            new_admin_id,
            "🎉 *Congratulations!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚙️ You have been granted *Admin access* to Sidd Saga Bot!\n"
            "🛠 Use /admin to access the admin panel.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ── /removeadmin <user_id> ────────────────────────────────────────
@bot.message_handler(commands=['removeadmin'])
def cmd_remove_admin(message):
    if not is_owner(message.from_user.id):
        bot.send_message(message.chat.id, "❌ *Only owners can remove admins.*", parse_mode="Markdown")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id,
                         "❌ *Usage:* `/removeadmin <user_id>`",
                         parse_mode="Markdown")
        return
    target_id = int(parts[1])
    if is_owner(target_id):
        bot.send_message(message.chat.id, "🚫 *Cannot remove an owner from admin.*", parse_mode="Markdown")
        return
    remove_admin_from_db(target_id)
    bot.send_message(
        message.chat.id,
        f"✅ *Admin Removed*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *ID:* `{target_id}` has been removed from admins.",
        parse_mode="Markdown"
    )

# ── /admin ────────────────────────────────────────────────────────
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, admin_panel_text(),
                         parse_mode="Markdown", reply_markup=admin_menu())
    elif is_maintenance_enabled():
        send_maintenance_message(message.chat.id)

# ── Callback: check_join ─────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def callback_check_join(call):
    if not is_admin(call.from_user.id) and is_maintenance_enabled():
        bot.answer_callback_query(call.id, "Bot is under maintenance.", show_alert=True)
        send_maintenance_message(call.message.chat.id)
        return
    if check_channels(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Thank you for joining!")
        grant_pending_referral(call.from_user.id)
        bot.send_message(call.message.chat.id, welcome_text(),
                         parse_mode="Markdown", reply_markup=main_menu(call.from_user.id))
    else:
        bot.answer_callback_query(call.id, "❌ You haven't joined yet!", show_alert=True)

# ── Callback: Main Menu Buttons (InlineKeyboard) ─────────────────
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def callback_main_menu(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    user = get_user(user_id)
    if not user:
        add_user(user_id, call.from_user.username)
        user = get_user(user_id)

    if not is_admin(user_id) and is_banned(user_id):
        bot.send_message(chat_id, "🚫 *You are banned from this bot.*", parse_mode="Markdown")
        return

    action = call.data

    if action == "menu_back":
        bot.send_message(chat_id, welcome_text(),
                         parse_mode="Markdown", reply_markup=main_menu(user_id))

    elif action == "menu_buy":
        bot.send_message(chat_id,
            "🛒 *Premium Deals & Panels*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 Want your own *Canva Business admin panel* or a private *Canva Pro* account?\n\n"
            "🦈 Want a *Surfshark VPN* subscription?\n\n"
            "💬 *Contact:* @siddheshsaga\\_bot\n"
            "⚡ Fast setup · Clean pricing · Direct support.",
            parse_mode="Markdown"
        )

    elif action == "menu_account":
        credits = "∞" if is_admin(user_id) else user['credits']
        surfshark_credits = "∞" if is_admin(user_id) else user['surfshark_credits']
        username = f"@{user['username']}" if user['username'] else "Not set"
        admin_badge = "\n👑 *Role:* `Admin`" if is_admin(user_id) else ""
        bot.send_message(chat_id,
            "👤 *Your Account*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 *ID:* `{user['user_id']}`\n"
            f"👨‍💻 *Username:* {username}\n"
            f"🪙 *Canva Credits:* `{credits}`\n"
            f"🦈 *Surfshark Credits:* `{surfshark_credits}`\n"
            f"👥 *Referrals:* `{user['referrals']}`"
            f"{admin_badge}",
            parse_mode="Markdown"
        )

    elif action == "menu_refer":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        bot.send_message(chat_id,
            "🔗 *Refer & Earn*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👥 Invite friends and collect free credits!\n\n"
            "🎁 *Reward per referral:*\n"
            "  🪙 `1` Canva credit\n"
            "  🦈 `1` Surfshark login credit\n\n"
            f"📊 *Your Referrals:* `{user['referrals']}`\n\n"
            f"🔗 *Your Invite Link:*\n`{ref_link}`",
            parse_mode="Markdown"
        )

    elif action == "menu_support":
        bot.send_message(chat_id,
            "📞 *Support & Updates*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👨‍💻 *Owner:* @siddsaga\n"
            "🛠 *Developer:* @AsimVirus\n"
            "🤖 *Support Bot:* @siddheshsaga\\_bot\n"
            "🌐 *Updates Channel:* @siddmethodsgiveway\n\n"
            "⚡ _We respond fast!_",
            parse_mode="Markdown"
        )

    elif action == "menu_surfshark":
        user_credits = "∞" if is_admin(user_id) else (user['surfshark_credits'] if user else 0)
        bot.send_message(chat_id,
            "🦈 *Surfshark Quick Login*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 Send your *6-character Surfshark login code* in chat.\n\n"
            "💡 How to get your code:\n"
            "  1. Open Surfshark app\n"
            "  2. Go to Login with code\n"
            "  3. Copy the 6-digit code\n"
            "  4. Send it here!\n\n"
            f"🪙 *Cost:* `{SURFSHARK_CREDIT_COST}` Surfshark credits\n"
            f"🦈 *Your Surfshark Credits:* `{user_credits}`\n"
            "🔗 Need more? Invite friends with your referral link!",
            parse_mode="Markdown"
        )

    elif action == "menu_duolingo":
        account = pop_duolingo_account()
        if account:
            bot.send_message(chat_id,
                format_duolingo_account(account),
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )
        else:
            bot.send_message(chat_id,
                "❌ *No Duolingo accounts available right now.*\n\n"
                "Please check again later or contact support.",
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )



    elif action == "menu_canva_biz":
        canva_mode = get_setting('canva_mode') or 'auto'
        if is_admin(user_id) or user['credits'] > 0:
            if canva_mode == 'auto':
                # ── Auto mode: ask for email, send invite via Playwright ──
                msg = bot.send_message(chat_id,
                    "💼 *Canva Business Invite*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "📩 Send the email address you want invited to the team.\n\n"
                    "🪙 `1` credit is deducted only if the invite is sent successfully.",
                    parse_mode="Markdown",
                    reply_markup=cancel_menu()
                )
                bot.register_next_step_handler(msg, process_business_email)
            else:
                # ── Link mode: give stored link ──
                conn = get_db_connection()
                link_row = conn.execute("SELECT * FROM links ORDER BY RANDOM() LIMIT 1").fetchone()
                conn.close()
                if link_row:
                    update_credits(user_id, -1)
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("🔗 Join Canva Business Team", url=link_row['link']))
                    markup.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
                    bot.send_message(chat_id,
                        "🎉 *Canva Business Unlocked!*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "💼 Your team invite is ready!\n"
                        "💡 Please *do not share* this link with others.\n\n"
                        "🪙 *1 credit deducted.*",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                else:
                    bot.send_message(chat_id,
                        "❌ *No links available right now.*\n\n"
                        "🔔 Please check again later or contact support.",
                        parse_mode="Markdown"
                    )
        else:
            bot.send_message(chat_id,
                "❌ *Not enough credits!*\n\n"
                "🔗 Use *Refer & Earn* to get free credits.\n"
                "💬 Or contact support.",
                parse_mode="Markdown"
            )


    elif action == "menu_canva_pro":
        if get_setting('pro_status') != 'enabled':
            bot.send_message(chat_id,
                "❌ *Canva Pro is currently disabled.*\n\nPlease check back later.",
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )
            return
        if is_admin(user_id) or user['credits'] > 0:
            msg = bot.send_message(chat_id,
                "👑 *Canva Pro Activation*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📩 Send the *email address* you want upgraded to Pro.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_pro_email)
        else:
            bot.send_message(chat_id,
                "❌ *Not enough credits!*\n\n"
                "🔗 Use *Refer & Earn* to get free credits.",
                parse_mode="Markdown"
            )

    elif action == "menu_maintenance":
        if is_admin(user_id):
            current = get_setting('maintenance_status')
            new_status = 'disabled' if current == 'enabled' else 'enabled'
            update_setting('maintenance_status', new_status)
            icon = "🛠" if new_status == 'enabled' else "✅"
            bot.send_message(chat_id,
                f"{icon} *Maintenance mode is now {new_status.upper()}.*",
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )

    elif action == "menu_admin":
        if is_admin(user_id):
            bot.send_message(chat_id, admin_panel_text(),
                             parse_mode="Markdown", reply_markup=admin_menu())

# ── Main message router ───────────────────────────────────────────
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    raw_text = message.text.strip() if message.text else ""
    text = raw_text

    known_commands = [
        "🔙 Back to Main", "🛒 Buy Premium / Panel", "👤 My Account",
        "🔗 Refer & Earn", "📞 Support", "💼 Canva Business", "👑 Canva Pro",
        "Surfshark Login", "🦈 Surfshark Login", "🏄‍♀️ Surfshark Login", "Duolingo", "🔫 Duolingo", "🦉 Duolingo",
        "➕ Add Link", "📋 View Links",
        "📢 Broadcast", "📊 Statistics",
        "💰 Give Credits", "🗑 Delete Link", "👤 User Info", "🎁 Global Gift",
        "🛠 Maintenance", "⚙️ Admin Panel", "✉️ Message User",
        "📂 Backup DB", "🔰 Leaderboard", "🏆 Leaderboard",
        # New admin features
        "🦈 Give Surf Credits", "🎯 Set Credits", "👥 All Users",
        "🚫 Ban User", "✅ Unban User", "📋 Banned List",
        "👑 Pro Requests", "🔄 Reset User",
        "🦈 Surf Global Gift", "📤 Export Users", "🍪 Update Cookies",
        # Surfshark cookies pool management
        "➕ Add Surf Cookie", "📋 View Surf Cookies", "🗑 Delete Surf Cookie",
        # Canva management
        "🔄 Toggle Canva Mode", "🍪 Canva Cookies", "🔃 Refresh Canva",
    ]
    for cmd in known_commands:
        if " " in cmd:
            cmd_text = cmd.split(" ", 1)[1]
            if raw_text == cmd_text or raw_text == cmd:
                text = cmd
                break

    user = get_user(user_id)
    if not user:
        add_user(user_id, message.from_user.username)
        user = get_user(user_id)

    if block_member_during_maintenance(message):
        return

    # Block banned users
    if not is_admin(user_id) and is_banned(user_id):
        bot.send_message(
            message.chat.id,
            "🚫 *You are banned from this bot.*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Contact support if you think this is a mistake.\n"
            "📞 @siddheshsaga\\_bot",
            parse_mode="Markdown"
        )
        return

    if text == "🔙 Back to Main":
        bot.send_message(message.chat.id, welcome_text(),
                         parse_mode="Markdown", reply_markup=main_menu(user_id))
        return

    if not is_admin(user_id) and not check_channels(user_id):
        bot.send_message(message.chat.id, join_required_text(),
                         parse_mode="Markdown", reply_markup=join_channels_markup())
        return

    if handle_surfshark_code(message, raw_text):
        return

    # ── User Commands ──────────────────────────────────────────────
    if text == "🛒 Buy Premium / Panel":
        bot.send_message(
            message.chat.id,
            "🛒 *Premium Deals & Panels*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 Want your own *Canva Business admin panel* or a private *Canva Pro* account?\n\n"
            "🦈 Want a *Surfshark VPN* subscription?\n\n"
            "💬 *Contact:* @siddheshsaga\\_bot\n"
            "⚡ Fast setup · Clean pricing · Direct support.",
            parse_mode="Markdown"
        )

    elif text == "👤 My Account":
        credits = "∞" if is_admin(user_id) else user['credits']
        surfshark_credits = "∞" if is_admin(user_id) else user['surfshark_credits']
        username = f"@{user['username']}" if user['username'] else "Not set"
        admin_badge = "\n👑 *Role:* `Admin`" if is_admin(user_id) else ""
        bot.send_message(
            message.chat.id,
            "👤 *Your Account*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 *ID:* `{user['user_id']}`\n"
            f"👨‍💻 *Username:* {username}\n"
            f"🪙 *Canva Credits:* `{credits}`\n"
            f"🦈 *Surfshark Credits:* `{surfshark_credits}`\n"
            f"👥 *Referrals:* `{user['referrals']}`"
            f"{admin_badge}",
            parse_mode="Markdown"
        )

    elif text == "🔗 Refer & Earn":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        bot.send_message(
            message.chat.id,
            "🔗 *Refer & Earn*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👥 Invite friends and collect free credits!\n\n"
            "🎁 *Reward per referral:*\n"
            "  🪙 `1` Canva credit\n"
            "  🦈 `1` Surfshark login credit\n\n"
            f"📊 *Your Referrals:* `{user['referrals']}`\n\n"
            f"🔗 *Your Invite Link:*\n`{ref_link}`",
            parse_mode="Markdown"
        )

    elif text == "📞 Support":
        bot.send_message(
            message.chat.id,
            "📞 *Support & Updates*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👨‍💻 *Owner:* @siddsaga\n"
            "🛠 *Developer:* @AsimVirus\n"
            "🤖 *Support Bot:* @siddheshsaga\\_bot\n"
            "🌐 *Updates Channel:* @siddmethodsgiveway\n\n"
            "⚡ _We respond fast!_",
            parse_mode="Markdown"
        )

    elif text in ["Surfshark Login", "🦈 Surfshark Login", "🏄 Surfshark Login", "🏄‍♀️ Surfshark Login"]:
        user_credits = "∞" if is_admin(user_id) else (user['surfshark_credits'] if user else 0)
        bot.send_message(
            message.chat.id,
            "🦈 *Surfshark Quick Login*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 Send your *6-character Surfshark login code.*\n\n"
            "💡 How to get your code:\n"
            "  1. Open Surfshark app\n"
            "  2. Go to Login with code\n"
            "  3. Copy the 6-digit code\n"
            "  4. Send it here!\n\n"
            f"🪙 *Cost:* `{SURFSHARK_CREDIT_COST}` Surfshark credits\n"
            f"🦈 *Your Surfshark Credits:* `{user_credits}`\n"
            "🔗 Need more? Invite friends with your referral link!"
        )

    elif text in ["Duolingo", "🔫 Duolingo", "🦉 Duolingo"]:
        account = pop_duolingo_account()
        if account:
            bot.send_message(
                message.chat.id,
                format_duolingo_account(account),
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )
        else:
            bot.send_message(
                message.chat.id,
                "❌ *No Duolingo accounts available right now.*\n\n"
                "Please check again later or contact support.",
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )

    elif text == "💼 Canva Business":
        canva_mode = get_setting('canva_mode') or 'auto'
        if is_admin(user_id) or user['credits'] > 0:
            if canva_mode == 'auto':
                msg = bot.send_message(
                    message.chat.id,
                    "💼 *Canva Business Invite*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "📩 Send the email address you want invited to the team.\n\n"
                    "🪙 `1` credit is deducted only if the invite is sent successfully.",
                    parse_mode="Markdown",
                    reply_markup=cancel_menu()
                )
                bot.register_next_step_handler(msg, process_business_email)
            else:
                conn = get_db_connection()
                link_row = conn.execute("SELECT * FROM links ORDER BY RANDOM() LIMIT 1").fetchone()
                conn.close()
                if link_row:
                    update_credits(user_id, -1)
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("🔗 Join Canva Business Team", url=link_row['link']))
                    bot.send_message(
                        message.chat.id,
                        "🎉 *Canva Business Unlocked!*\n"
                        "━━━━━━━━━━━━━━━━━━━━\n\n"
                        "💼 Your team invite is ready!\n"
                        "💡 Please *do not share* this link with others.\n\n"
                        "🪙 *1 credit deducted.*",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                else:
                    bot.send_message(
                        message.chat.id,
                        "❌ *No links available right now.*\n\n"
                        "🔔 Please check again later or contact support.",
                        parse_mode="Markdown"
                    )
        else:
            bot.send_message(
                message.chat.id,
                "❌ *Not enough credits!*\n\n"
                "🔗 Use *Refer & Earn* to get free credits.\n"
                "💬 Or contact support.",
                parse_mode="Markdown"
            )


    elif text == "👑 Canva Pro":
        if get_setting('pro_status') != 'enabled':
            bot.send_message(
                message.chat.id,
                "❌ *Canva Pro is currently disabled.*\n\nPlease check back later.",
                parse_mode="Markdown",
                reply_markup=main_menu(user_id)
            )
            return
        if is_admin(user_id) or user['credits'] > 0:
            msg = bot.send_message(
                message.chat.id,
                "👑 *Canva Pro Activation*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📩 Send the *email address* you want upgraded to Pro.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_pro_email)
        else:
            bot.send_message(
                message.chat.id,
                "❌ *Not enough credits!*\n\n"
                "🔗 Use *Refer & Earn* to get free credits.",
                parse_mode="Markdown"
            )

    elif is_admin(user_id):
        # ── Admin-only keyboard commands ──────────────────────────
        if text == "⚙️ Admin Panel":
            bot.send_message(message.chat.id, admin_panel_text(),
                             parse_mode="Markdown", reply_markup=admin_menu())

        elif text == "➕ Add Link":
            msg = bot.send_message(
                message.chat.id,
                "➕ *Add Canva Business Link*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📋 Send the invite link you want to store.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_add_link)

        elif text == "📋 View Links":
            conn = get_db_connection()
            links = conn.execute("SELECT * FROM links").fetchall()
            conn.close()
            if links:
                res = "📋 *Stored Canva Links*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for l in links:
                    res += f"🆔 `{l['id']}`\n🔗 {l['link']}\n\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "📭 *No links saved yet.*", parse_mode="Markdown")

        elif text == "📢 Broadcast":
            msg = bot.send_message(
                message.chat.id,
                "📢 *Broadcast Message*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📨 Send the text, photo, video, or file to deliver to all users.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_broadcast)

        elif text == "📊 Statistics":
            conn = get_db_connection()
            users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            pro_reqs = conn.execute("SELECT COUNT(*) FROM pro_requests").fetchone()[0]
            canva_pro_ids = count_canva_ids('pro')
            canva_biz_ids = count_canva_ids('business')
            admins_count = len(ADMIN_IDS)
            conn.close()
            bot.send_message(
                message.chat.id,
                "📊 *Bot Statistics*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👥 *Total Users:* `{users_count}`\n"
                f"🔗 *Business Links:* `{links_count}`\n"
                f"👑 *Pro Requests:* `{pro_reqs}`\n"
                f"🎨 *Canva Pro IDs:* `{canva_pro_ids}`\n"
                f"💼 *Canva Biz IDs:* `{canva_biz_ids}`\n"
                f"⚙️ *Total Admins:* `{admins_count}`",
                parse_mode="Markdown"
            )

        elif text == "💰 Give Credits":
            msg = bot.send_message(
                message.chat.id,
                "💰 *Give Credits*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Format: `USER_ID AMOUNT`\n"
                "Example: `123456789 5`",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_give_credits)

        elif text == "🗑 Delete Link":
            msg = bot.send_message(
                message.chat.id,
                "🗑 *Delete Link*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send the *Link ID* you want removed.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_delete_link)

        elif text == "👤 User Info":
            msg = bot.send_message(
                message.chat.id,
                "👤 *User Lookup*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send a *user ID* or *username* to look up.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_user_info)

        elif text == "🎁 Global Gift":
            msg = bot.send_message(
                message.chat.id,
                "🎁 *Global Gift*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send the *amount of credits* to give every user.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_global_gift)

        # (Toggle Pro removed as per owner request)

        elif text == "🛠 Maintenance":
            current = get_setting('maintenance_status')
            new_status = 'disabled' if current == 'enabled' else 'enabled'
            update_setting('maintenance_status', new_status)
            icon = "🛠" if new_status == 'enabled' else "✅"
            bot.send_message(
                message.chat.id,
                f"{icon} *Maintenance mode is now {new_status.upper()}.*",
                parse_mode="Markdown",
                reply_markup=admin_menu()
            )

        elif text in ["🔰 Leaderboard", "🏆 Leaderboard"]:
            conn = get_db_connection()
            top_users = conn.execute(
                "SELECT user_id, username, referrals FROM users ORDER BY referrals DESC LIMIT 10"
            ).fetchall()
            conn.close()
            if top_users:
                medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
                res = "🏆 *Top 10 Referrers*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for i, u in enumerate(top_users):
                    uname = f"@{u['username']}" if u['username'] else f"`{u['user_id']}`"
                    res += f"{medals[i]} {uname}  •  👥 `{u['referrals']}` referrals\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "📭 *No leaderboard data yet.*", parse_mode="Markdown")

        elif text == "📂 Backup DB":
            try:
                with open(DB_PATH, "rb") as doc:
                    bot.send_document(message.chat.id, doc,
                                      caption="📂 *Database Backup Ready* ✅",
                                      parse_mode="Markdown")
            except Exception:
                bot.send_message(message.chat.id, "❌ *Database backup failed.*", parse_mode="Markdown")

        elif text == "✉️ Message User":
            msg = bot.send_message(
                message.chat.id,
                "✉️ *Message User*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Format: `USER_ID Your message here`\n"
                "Example: `123456789 Hey bro!`",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_admin_message)

        # ── NEW: Give Surfshark Credits ─────────────────────────
        elif text == "🦈 Give Surf Credits":
            msg = bot.send_message(
                message.chat.id,
                "🦈 *Give Surfshark Credits*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Format: `USER_ID AMOUNT`\n"
                "Example: `123456789 3`",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_give_surf_credits)

        # ── NEW: Set Credits (exact) ───────────────────────────
        elif text == "🎯 Set Credits":
            msg = bot.send_message(
                message.chat.id,
                "🎯 *Set Credits (Exact)*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Format: `USER_ID CANVA_CREDITS SURFSHARK_CREDITS`\n\n"
                "Example: `123456789 10 5`\n"
                "Sets 10 canva credits and 5 surfshark credits.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_set_credits)

        # ── NEW: All Users ───────────────────────────────────
        elif text == "👥 All Users":
            conn = get_db_connection()
            users_list = conn.execute(
                "SELECT user_id, username, credits, surfshark_credits, referrals FROM users ORDER BY joined_date DESC LIMIT 50"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            conn.close()
            if users_list:
                res = f"👥 *All Users* (showing latest 50 of {total})\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for u in users_list:
                    uname = f"@{u['username']}" if u['username'] else "-"
                    banned_tag = " 🚫" if is_banned(u['user_id']) else ""
                    admin_tag = " 👑" if u['user_id'] in ADMIN_IDS else ""
                    res += (f"`{u['user_id']}` {uname}{admin_tag}{banned_tag}\n"
                            f"  🪙 `{u['credits']}` • 🦈 `{u['surfshark_credits']}` • 👥 `{u['referrals']}`\n")
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "📭 *No users yet.*", parse_mode="Markdown")

        # ── NEW: Ban User ────────────────────────────────────
        elif text == "🚫 Ban User":
            msg = bot.send_message(
                message.chat.id,
                "🚫 *Ban User*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Format: `USER_ID REASON`\n"
                "Example: `123456789 Spamming`\n\n"
                "Reason is optional.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_ban_user)

        # ── NEW: Unban User ──────────────────────────────────
        elif text == "✅ Unban User":
            msg = bot.send_message(
                message.chat.id,
                "✅ *Unban User*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send the *User ID* to unban.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_unban_user)

        # ── NEW: Banned List ─────────────────────────────────
        elif text == "📋 Banned List":
            banned = get_banned_list()
            if banned:
                res = "🚫 *Banned Users*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for b in banned:
                    reason = b['reason'] if b['reason'] else "No reason"
                    res += f"🚫 `{b['user_id']}` — _{reason}_\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "✅ *No banned users.*", parse_mode="Markdown", reply_markup=admin_menu())

        # ── NEW: Pro Requests ────────────────────────────────
        elif text == "👑 Pro Requests":
            conn = get_db_connection()
            reqs = conn.execute(
                "SELECT * FROM pro_requests ORDER BY id DESC LIMIT 20"
            ).fetchall()
            conn.close()
            if reqs:
                res = "👑 *Pro Requests (Last 20)*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for r in reqs:
                    status_icon = "✅" if r['status'] == 'done' else "⏳"
                    res += (f"{status_icon} `[{r['id']}]`\n"
                            f"  👤 `{r['user_id']}`\n"
                            f"  📩 `{r['email']}`\n"
                            f"  📌 Status: `{r['status']}`\n\n")
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "📭 *No pro requests yet.*", parse_mode="Markdown", reply_markup=admin_menu())

        # ── NEW: Reset User ──────────────────────────────────
        elif text == "🔄 Reset User":
            msg = bot.send_message(
                message.chat.id,
                "🔄 *Reset User*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "This will reset a user's credits, surfshark credits, and referrals to 0.\n\n"
                "📝 Send the *User ID* to reset.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_reset_user)

        # ── NEW: Surf Global Gift ──────────────────────────────
        elif text == "🦈 Surf Global Gift":
            msg = bot.send_message(
                message.chat.id,
                "🦈 *Surfshark Global Gift*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send the *amount of Surfshark credits* to give every user.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_surf_global_gift)

        # ── NEW: Export Users ─────────────────────────────────
        elif text == "📤 Export Users":
            conn = get_db_connection()
            users_list = conn.execute(
                "SELECT user_id, username, credits, surfshark_credits, referrals, joined_date FROM users ORDER BY joined_date DESC"
            ).fetchall()
            conn.close()
            if users_list:
                import io
                csv_text = "user_id,username,credits,surfshark_credits,referrals,joined_date\n"
                for u in users_list:
                    csv_text += f"{u['user_id']},{u['username'] or ''},{u['credits']},{u['surfshark_credits']},{u['referrals']},{u['joined_date']}\n"
                file_obj = io.BytesIO(csv_text.encode('utf-8'))
                file_obj.name = "users_export.csv"
                bot.send_document(message.chat.id, file_obj, caption="📤 *Users Export Ready* ✅", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "📭 *No users to export.*", parse_mode="Markdown")

        # ── NEW: Update Cookies ──────────────────────────────
        elif text == "🍪 Update Cookies":
            msg = bot.send_message(
                message.chat.id,
                "🍪 *Update Surfshark Cookies*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the cookies in one of these ways:\n\n"
                "📄 *Option 1:* Upload a `.json` or `.txt` file\n"
                "📝 *Option 2:* Paste the JSON cookies text directly\n\n"
                "⚠️ This will replace `storage_state.json` and restart the Surfshark browser session.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_update_cookies)

        # ── NEW: Surfshark Cookies Pool Management ────────────
        elif text == "➕ Add Surf Cookie":
            msg = bot.send_message(
                message.chat.id,
                "➕ *Add Surfshark Cookie*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Format to send:\n"
                "📝 `AccountLabel\nJSON_COOKIES`\n\n"
                "Or simply send a `.json` or `.txt` file containing the cookies, and specify the account label as the caption.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_add_surf_cookie)

        elif text == "📋 View Surf Cookies":
            conn = get_db_connection()
            cookies = conn.execute("SELECT * FROM surfshark_cookies").fetchall()
            conn.close()
            if cookies:
                res = "📋 *Stored Surfshark Cookies*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for c in cookies:
                    status_icon = "🟢" if c['status'] == 'active' else "🔴"
                    res += f"🆔 `{c['id']}` | {status_icon} *{c['name']}* ({c['status']})\n📅 Added: `{c['added_date']}`\n\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "📭 *No Surfshark cookies saved yet.*", parse_mode="Markdown")

        elif text == "🗑 Delete Surf Cookie":
            msg = bot.send_message(
                message.chat.id,
                "🗑 *Delete Surfshark Cookie*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📝 Send the *Cookie ID* to remove from DB.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_delete_surf_cookie)

        # ── Canva Management ──────────────────────────────────
        elif text == "🔄 Toggle Canva Mode":
            current = get_setting('canva_mode') or 'auto'
            new_mode = 'link' if current == 'auto' else 'auto'
            update_setting('canva_mode', new_mode)
            mode_label = "🤖 AUTO (Email Invite)" if new_mode == 'auto' else "🔗 LINK (Stored Links)"
            bot.send_message(
                message.chat.id,
                f"🔄 *Canva Business Mode Changed*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Mode: *{mode_label}*\n\n"
                + ("Users will now enter their email → Bot sends invite via Playwright." if new_mode == 'auto'
                   else "Users will now get a random stored link. Make sure links are stocked."),
                parse_mode="Markdown",
                reply_markup=admin_menu()
            )

        elif text == "🍪 Canva Cookies":
            msg = bot.send_message(
                message.chat.id,
                "🍪 *Update Canva Cookies*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Send the cookies in one of these ways:\n\n"
                "📄 *Option 1:* Upload a `.json` or `.txt` file\n"
                "📝 *Option 2:* Paste the JSON cookies text directly\n\n"
                "⚠️ This will replace `canva_storage_state.json` and restart the Canva browser session.",
                parse_mode="Markdown",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_update_canva_cookies)

        elif text == "🔃 Refresh Canva":
            try:
                close_canva_browser()
                bot.send_message(
                    message.chat.id,
                    "✅ *Canva Browser Refreshed*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "🔄 Session closed. Next invite will open a fresh browser.\n"
                    "⚡ Cookies from `canva_storage_state.json` will be loaded.",
                    parse_mode="Markdown",
                    reply_markup=admin_menu()
                )
            except Exception as e:
                bot.send_message(
                    message.chat.id,
                    f"❌ *Failed to refresh:* `{short_error(e)}`",
                    parse_mode="Markdown",
                    reply_markup=admin_menu()
                )


# ── Canva Business Email Step (Auto-Invite) ───────────────────────
def process_business_email(message):
    if block_member_during_maintenance(message):
        return
    user_id = message.from_user.id
    raw = (message.text or "").strip()
    if raw in CANCEL_TEXTS or raw in MENU_BUTTONS:
        try:
            bot.clear_step_handler_by_chat_id(message.chat.id)
        except Exception:
            pass
        if raw in CANCEL_TEXTS:
            bot.send_message(message.chat.id, "❌ *Cancelled.*\n\nNo credit was deducted.", reply_markup=main_menu(user_id))
        else:
            handle_text(message)
        return

    wait = check_cooldown(user_id, "canva_business")
    if wait:
        bot.send_message(message.chat.id, f"Please wait {wait}s before sending another invite.", reply_markup=main_menu(user_id))
        return

    email = normalize_email(raw)
    if not valid_email(email):
        bot.send_message(message.chat.id, "❌ *Invalid email.*\n\nPlease tap 💼 Canva Business and try again.", reply_markup=main_menu(user_id))
        return

    if not reserve_credit(user_id):
        bot.send_message(message.chat.id, "❌ *Not enough credits.*", reply_markup=main_menu(user_id))
        return

    status = bot.send_message(
        message.chat.id,
        f"💼 *Sending Canva Business invite...*\n\n`{html.escape(email)}`"
    )
    started_at = time.monotonic()
    timings = {}
    try:
        ok, reply, timings = submit_canva_invite(email)
    except Exception as e:
        ok = False
        reply = (
            "💥 <b>Something went wrong inside the Canva invite helper.</b>\n\n"
            f"<code>{short_error(e)}</code>"
        )

    if not ok:
        refund_credit(user_id)

    elapsed = time.monotonic() - started_at
    header = "✅ *Success*\n\n" if ok else "❌ *Invite not sent*\n\n"
    timing_text = (
        f"\n\n⏱ _Completed in {elapsed:.1f} seconds._"
        f"\n_Page {timings.get('page', 0):.1f}s · Type {timings.get('type', 0):.1f}s · "
        f"Click {timings.get('click', 0):.1f}s · Confirm {timings.get('confirm', 0):.1f}s_"
    )
    credit_note = "\n\n🪙 _1 credit deducted._" if ok and not is_admin(user_id) else ("\n\n🪙 _No credit charged._" if not ok else "")
    result_text = header + reply + credit_note + timing_text
    if len(result_text) > 3500:
        result_text = result_text[:3500] + "\n\n_Message trimmed._"
    try:
        bot.edit_message_text(
            result_text,
            chat_id=message.chat.id,
            message_id=status.message_id,
        )
    except Exception:
        bot.send_message(message.chat.id, result_text)

    bot.send_message(
        message.chat.id,
        "✅ Done. Main menu restored.",
        reply_markup=main_menu(user_id)
    )



# ── Canva Pro Email Step ──────────────────────────────────────────
def process_pro_email(message):
    if block_member_during_maintenance(message):
        return
    email = message.text
    user_id = message.from_user.id
    if email == "❌ Cancel":
        bot.send_message(message.chat.id, "❌ *Cancelled.*\n\nNo credit was deducted.",
                         parse_mode="Markdown", reply_markup=main_menu(user_id))
        return
    if "@" not in email:
        bot.send_message(message.chat.id,
                         "❌ *Invalid email!*\n\nPlease start again with a valid email address.",
                         parse_mode="Markdown", reply_markup=main_menu(user_id))
        return
    update_credits(user_id, -1)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO pro_requests (user_id, email) VALUES (?, ?)", (user_id, email))
    conn.commit()
    conn.close()
    bot.send_message(
        message.chat.id,
        "✅ *Request Submitted!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👑 An admin will activate Canva Pro for you.\n"
        "🔔 You'll be asked for a login code when needed.\n\n"
        "_Please keep the bot open!_",
        parse_mode="Markdown",
        reply_markup=main_menu(user_id)
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔑 Ask User for Code", callback_data=f"ask_code_{user_id}"))
    notify_admins(
        "🔔 *New Canva Pro Request!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *User:* `{user_id}`\n"
        f"📩 *Email:* `{email}`\n\n"
        "Tap below when ready to request the login code.",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_code_"))
def callback_ask_code(call):
    target_user_id = int(call.data.split("_")[2])
    conn = get_db_connection()
    req = conn.execute(
        "SELECT email FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (target_user_id,)
    ).fetchone()
    conn.close()
    email = req['email'] if req else "your email"
    try:
        msg = bot.send_message(
            target_user_id,
            "🔑 *Canva Login Code Needed*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📩 *Email:* `{email}`\n\n"
            "📧 Check your inbox for the Canva login code and send it here.",
            parse_mode="Markdown",
            reply_markup=cancel_menu()
        )
        bot.register_next_step_handler(msg, lambda m: process_user_code(m, email))
        bot.answer_callback_query(call.id, "✅ Message sent to user!")
        notify_admins(f"✅ Code request sent for `{email}`")
    except Exception:
        bot.answer_callback_query(call.id, "Failed to send message to user.", show_alert=True)

def process_user_code(message, email):
    if block_member_during_maintenance(message):
        return
    if message.text == "❌ Cancel":
        bot.send_message(message.chat.id, "❌ *Cancelled.*",
                         parse_mode="Markdown", reply_markup=main_menu(message.from_user.id))
        notify_admins(f"❌ User `{message.from_user.id}` cancelled code sharing for `{email}`.")
        return
    code = message.text
    bot.send_message(
        message.chat.id,
        "✅ *Code Sent!*\n\n⏳ Please wait while the admin completes activation.",
        parse_mode="Markdown",
        reply_markup=main_menu(message.from_user.id)
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Login Successful", callback_data=f"pro_success_{message.from_user.id}"))
    markup.add(InlineKeyboardButton("❌ Ask for New Code", callback_data=f"pro_resend_{message.from_user.id}"))
    markup.add(InlineKeyboardButton("💬 Send Custom Msg", callback_data=f"pro_msg_{message.from_user.id}"))
    notify_admins(
        "🔑 *Canva Code Received!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📩 *Email:* `{email}`\n"
        f"🔐 *Code:* `{code}`",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_success_"))
def callback_pro_success(call):
    user_id = call.data.split("_")[2]
    try:
        bot.send_message(
            user_id,
            "🎉 *Canva Pro Activated!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ Your login was successful!\n"
            "👑 Enjoy your premium Canva Pro access.\n\n"
            "⭐ _Thank you for using Sidd Saga Bot!_",
            parse_mode="Markdown",
            reply_markup=main_menu(int(user_id))
        )
        bot.answer_callback_query(call.id, "✅ Success message sent!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        bot.answer_callback_query(call.id, "Failed to send message.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_resend_"))
def callback_pro_resend(call):
    user_id = int(call.data.split("_")[2])
    conn = get_db_connection()
    req = conn.execute(
        "SELECT email FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    email = req['email'] if req else "your email"
    try:
        msg = bot.send_message(
            user_id,
            "❌ *Login Code Failed!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "The code was incorrect or expired.\n"
            "📧 Please send the *new Canva login code* from your email.",
            parse_mode="Markdown",
            reply_markup=cancel_menu()
        )
        bot.register_next_step_handler(msg, lambda m: process_user_code(m, email))
        bot.answer_callback_query(call.id, "✅ Asked user for new code!")
    except Exception:
        bot.answer_callback_query(call.id, "Failed to send message.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_msg_"))
def callback_pro_msg(call):
    user_id = call.data.split("_")[2]
    msg = bot.send_message(
        call.message.chat.id,
        "💬 *Custom Message*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "Enter the message to send to this user.",
        parse_mode="Markdown",
        reply_markup=cancel_menu()
    )
    bot.register_next_step_handler(msg, lambda m: process_custom_msg(m, user_id))

def process_custom_msg(message, target_user_id):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        bot.send_message(
            target_user_id,
            f"📩 *Message from Admin*\n━━━━━━━━━━━━━━━━━━━━\n\n{message.text}",
            parse_mode="Markdown"
        )
        bot.send_message(message.chat.id, "✅ *Message sent successfully.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ *Failed to send message.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

# ── Admin action processors ───────────────────────────────────────
def process_give_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split()
        target_user_id = int(parts[0])
        amount = int(parts[1])
        update_credits(target_user_id, amount)
        bot.send_message(
            message.chat.id,
            f"✅ *Credits Added!*\n\n"
            f"👤 User: `{target_user_id}`\n"
            f"🪙 Amount: `{amount}`",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        try:
            bot.send_message(
                target_user_id,
                f"🎉 *Credits Added!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🪙 Admin gifted you `{amount}` credits!\n"
                f"🎁 Enjoy your premium access!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID AMOUNT`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_add_link(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    link = message.text
    conn = get_db_connection()
    conn.execute("INSERT INTO links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, "✅ *Link added successfully!*",
                     parse_mode="Markdown", reply_markup=admin_menu())



def process_delete_link(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        link_id = int(message.text)
        conn = get_db_connection()
        conn.execute("DELETE FROM links WHERE id=?", (link_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *Link deleted.*\n\n🆔 Link ID: `{link_id}`",
                         parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid link ID.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_user_info(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        query = message.text.strip()
        conn = get_db_connection()
        if query.isdigit():
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (int(query),)).fetchone()
        else:
            query = query.replace("@", "")
            user = conn.execute("SELECT * FROM users WHERE username LIKE ?", (f"%{query}%",)).fetchone()
        conn.close()
        if user:
            username = f"@{user['username']}" if user['username'] else "Not set"
            admin_badge = " 👑 Admin" if user['user_id'] in ADMIN_IDS else ""
            bot.send_message(
                message.chat.id,
                "👤 *User Info*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🆔 *ID:* `{user['user_id']}`{admin_badge}\n"
                f"👨‍💻 *Username:* {username}\n"
                f"🪙 *Credits:* `{user['credits']}`\n"
                f"🦈 *Surfshark Credits:* `{user['surfshark_credits']}`\n"
                f"👥 *Referrals:* `{user['referrals']}`\n"
                f"📅 *Joined:* `{user['joined_date']}`",
                parse_mode="Markdown",
                reply_markup=admin_menu()
            )
        else:
            bot.send_message(message.chat.id, "❌ *User not found.*",
                             parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ *Error fetching user info.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_global_gift(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        amount = int(message.text)
        conn = get_db_connection()
        conn.execute("UPDATE users SET credits = credits + ?", (amount,))
        conn.commit()
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ *Global gift applied!*\n\n🪙 Every user received `{amount}` credits.",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        bot.send_message(message.chat.id, "📢 *Broadcasting gift notice...*", parse_mode="Markdown")
        success = 0
        for u in users:
            try:
                bot.send_message(
                    u['user_id'],
                    f"🎁 *Surprise Gift from @siddsaga!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎉 You received `{amount}` free credits!\n"
                    f"🚀 Enjoy your premium access!",
                    parse_mode="Markdown"
                )
                success += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.send_message(message.chat.id,
                         f"✅ *Gift broadcast complete!*\n\n👥 Sent to `{success}` users.",
                         parse_mode="Markdown")
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid amount.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_broadcast(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    conn = get_db_connection()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    success = 0
    bot.send_message(message.chat.id, "📢 *Broadcasting...*", parse_mode="Markdown", reply_markup=admin_menu())
    for u in users:
        try:
            bot.copy_message(chat_id=u['user_id'], from_chat_id=message.chat.id,
                             message_id=message.message_id)
            success += 1
            time.sleep(0.05)
        except Exception:
            pass
    bot.send_message(message.chat.id,
                     f"✅ *Broadcast complete!*\n\n👥 Sent to `{success}` users.",
                     parse_mode="Markdown")

def process_admin_message(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split(" ", 1)
        uid = int(parts[0])
        text_msg = parts[1]
        bot.send_message(
            uid,
            f"📩 *Message from Admin*\n━━━━━━━━━━━━━━━━━━━━\n\n{text_msg}",
            parse_mode="Markdown"
        )
        bot.send_message(message.chat.id, "✅ *Message sent successfully.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id,
                         "❌ *Invalid format or user blocked the bot.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

# ── NEW Processors ────────────────────────────────────────────────
def process_give_surf_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split()
        target_user_id = int(parts[0])
        amount = int(parts[1])
        update_surfshark_credits(target_user_id, amount)
        bot.send_message(
            message.chat.id,
            f"✅ *Surfshark Credits Added!*\n\n"
            f"👤 User: `{target_user_id}`\n"
            f"🦈 Amount: `{amount}`",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        try:
            bot.send_message(
                target_user_id,
                f"🦈 *Surfshark Credits Added!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚡ Admin gifted you `{amount}` Surfshark credits!\n"
                f"🔑 Use them to login to Surfshark VPN.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID AMOUNT`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_set_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split()
        target_user_id = int(parts[0])
        canva_credits = int(parts[1])
        surf_credits = int(parts[2]) if len(parts) > 2 else 0
        conn = get_db_connection()
        conn.execute(
            "UPDATE users SET credits=?, surfshark_credits=? WHERE user_id=?",
            (canva_credits, surf_credits, target_user_id)
        )
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ *Credits Set!*\n\n"
            f"👤 User: `{target_user_id}`\n"
            f"🪙 Canva: `{canva_credits}`\n"
            f"🦈 Surfshark: `{surf_credits}`",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception:
        bot.send_message(message.chat.id,
                         "❌ *Invalid format.*\n\nUse: `USER_ID CANVA_CREDITS SURFSHARK_CREDITS`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_ban_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split(None, 1)
        target_id = int(parts[0])
        reason = parts[1] if len(parts) > 1 else ""
        if target_id in ADMIN_IDS:
            bot.send_message(message.chat.id, "🚫 *Cannot ban an admin!*",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
        ban_user(target_id, reason, message.from_user.id)
        bot.send_message(
            message.chat.id,
            f"🚫 *User Banned!*\n\n"
            f"👤 ID: `{target_id}`\n"
            f"📝 Reason: _{reason or 'No reason'}_",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        try:
            bot.send_message(
                target_id,
                "🚫 *You have been banned from this bot.*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 Reason: _{reason or 'Not specified'}_\n\n"
                "Contact support if you think this is a mistake.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID REASON`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_unban_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        target_id = int(message.text.strip())
        if not is_banned(target_id):
            bot.send_message(message.chat.id, "⚠️ *This user is not banned.*",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
        unban_user(target_id)
        bot.send_message(
            message.chat.id,
            f"✅ *User Unbanned!*\n\n👤 ID: `{target_id}`",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        try:
            bot.send_message(
                target_id,
                "✅ *You have been unbanned!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "🎉 You can use the bot again. Send /start to begin.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid User ID.*",
                         parse_mode="Markdown", reply_markup=admin_menu())



def process_reset_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        target_id = int(message.text.strip())
        user = get_user(target_id)
        if not user:
            bot.send_message(message.chat.id, "❌ *User not found.*",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
        conn = get_db_connection()
        conn.execute(
            "UPDATE users SET credits=0, surfshark_credits=0, referrals=0 WHERE user_id=?",
            (target_id,)
        )
        conn.commit()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ *User Reset!*\n\n"
            f"👤 ID: `{target_id}`\n"
            f"🪙 Credits: `0`\n"
            f"🦈 Surfshark: `0`\n"
            f"👥 Referrals: `0`",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid User ID.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_surf_global_gift(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        amount = int(message.text)
        conn = get_db_connection()
        conn.execute("UPDATE users SET surfshark_credits = surfshark_credits + ?", (amount,))
        conn.commit()
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        bot.send_message(
            message.chat.id,
            f"✅ *Surfshark Global Gift Applied!*\n\n🦈 Every user got `{amount}` Surfshark credits.",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
        bot.send_message(message.chat.id, "📢 *Broadcasting...*", parse_mode="Markdown")
        success = 0
        for u in users:
            try:
                bot.send_message(
                    u['user_id'],
                    f"🦈 *Surfshark Gift from @siddsaga!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🎉 You got `{amount}` free Surfshark credits!\n"
                    f"🔑 Use them to login to Surfshark VPN!",
                    parse_mode="Markdown"
                )
                success += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.send_message(message.chat.id,
                         f"✅ *Broadcast complete!*\n\n👥 Sent to `{success}` users.",
                         parse_mode="Markdown")
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid amount.*",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_update_cookies(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    import json as json_mod
    cookies_json = None
    # Handle file upload
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            cookies_json = downloaded.decode('utf-8')
        except Exception as e:
            bot.send_message(message.chat.id,
                             f"❌ *Failed to download file:* `{short_error(e)}`",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
    elif message.text:
        cookies_json = message.text
    else:
        bot.send_message(message.chat.id, "❌ *Send cookies as text or a file.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return

    # Parse and normalize JSON
    try:
        data = json_mod.loads(cookies_json)
        
        # If it's a dict and contains 'cookies' key (like standard Playwright storage state)
        if isinstance(data, dict):
            raw_cookies = data.get("cookies", [])
        elif isinstance(data, list):
            raw_cookies = data
        else:
            raise ValueError("JSON must be a cookie array or a Playwright storage state object.")

        normalized_cookies = []
        for cookie in raw_cookies:
            if not isinstance(cookie, dict):
                continue
            
            # Essential keys for Playwright
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            path = cookie.get("path", "/")
            
            if not name or value is None or not domain:
                continue  # Skip invalid cookies
                
            # Expiry conversion: Chrome uses expirationDate, Playwright uses expires
            expires = cookie.get("expires")
            if expires is None:
                expires = cookie.get("expirationDate")
            
            # Map expires to float or omit if null/session
            if expires is not None:
                try:
                    expires = float(expires)
                except (ValueError, TypeError):
                    expires = None
                    
            # HTTPOnly & Secure: force proper boolean
            http_only = bool(cookie.get("httpOnly", False))
            secure = bool(cookie.get("secure", False))
            
            # SameSite normalization
            # Playwright strictly supports: "None", "Lax", "Strict"
            same_site_raw = str(cookie.get("sameSite", "Lax")).lower()
            if "none" in same_site_raw or "no_restriction" in same_site_raw:
                same_site = "None"
            elif "strict" in same_site_raw:
                same_site = "Strict"
            else:
                same_site = "Lax"  # default standard
                
            norm_cookie = {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(path),
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": same_site
            }
            if expires is not None:
                norm_cookie["expires"] = expires
                
            normalized_cookies.append(norm_cookie)
            
        if not normalized_cookies:
            raise ValueError("No valid cookies found in the input data.")
            
        # Wrap into Playwright storage state format
        storage_state = {
            "cookies": normalized_cookies,
            "origins": []
        }
        
        # Overwrite the storage state file
        with open(SURFSHARK_STORAGE, 'w', encoding='utf-8') as f:
            json_mod.dump(storage_state, f, indent=2)
            
        # Refresh the session by closing current context/browser
        close_surfshark_browser()
        
        bot.send_message(message.chat.id,
            "✅ *Cookies Normalized & Updated!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🍪 Loaded and sanitized `{len(normalized_cookies)}` cookies.\n"
            f"📂 Overwrote `storage_state.json` successfully.\n"
            "🔄 Surfshark browser session re-initialized.\n\n"
            "⚡ Next quick login will run with fresh credentials!",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception as e:
        bot.send_message(message.chat.id,
                         f"❌ *Failed to update cookies:* `{short_error(e)}`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_add_surf_cookie(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
    import json as json_mod
    cookies_json = None
    label = "Surf Account"
    
    # Handle file upload
    if message.document:
        try:
            if message.caption:
                label = message.caption.strip()
            else:
                label = message.document.file_name or "Surf Account"
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            cookies_json = downloaded.decode('utf-8')
        except Exception as e:
            bot.send_message(message.chat.id,
                             f"❌ *Failed to download file:* `{short_error(e)}`",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
    elif message.text:
        # Check if it has a label on the first line
        lines = message.text.strip().split("\n", 1)
        if len(lines) == 2 and not lines[0].strip().startswith("[") and not lines[0].strip().startswith("{"):
            label = lines[0].strip()
            cookies_json = lines[1].strip()
        else:
            label = f"Surf Account {int(time.time())}"
            cookies_json = message.text.strip()
    else:
        bot.send_message(message.chat.id, "❌ *Send cookies as text or a file.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return

    # Parse and normalize JSON
    try:
        data = json_mod.loads(cookies_json)
        
        # If it's a dict and contains 'cookies' key (like standard Playwright storage state)
        if isinstance(data, dict):
            raw_cookies = data.get("cookies", [])
        elif isinstance(data, list):
            raw_cookies = data
        else:
            raise ValueError("JSON must be a cookie array or a Playwright storage state object.")

        normalized_cookies = []
        for cookie in raw_cookies:
            if not isinstance(cookie, dict):
                continue
            
            # Essential keys for Playwright
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            path = cookie.get("path", "/")
            
            if not name or value is None or not domain:
                continue  # Skip invalid cookies
                
            # Expiry conversion: Chrome uses expirationDate, Playwright uses expires
            expires = cookie.get("expires")
            if expires is None:
                expires = cookie.get("expirationDate")
            
            # Map expires to float or omit if null/session
            if expires is not None:
                try:
                    expires = float(expires)
                except (ValueError, TypeError):
                    expires = None
                    
            # HTTPOnly & Secure: force proper boolean
            http_only = bool(cookie.get("httpOnly", False))
            secure = bool(cookie.get("secure", False))
            
            # SameSite normalization
            same_site_raw = str(cookie.get("sameSite", "Lax")).lower()
            if "none" in same_site_raw or "no_restriction" in same_site_raw:
                same_site = "None"
            elif "strict" in same_site_raw:
                same_site = "Strict"
            else:
                same_site = "Lax"  # default standard
                
            norm_cookie = {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(path),
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": same_site
            }
            if expires is not None:
                norm_cookie["expires"] = expires
                
            normalized_cookies.append(norm_cookie)
            
        if not normalized_cookies:
            raise ValueError("No valid cookies found in the input data.")
            
        # Wrap into Playwright storage state format
        storage_state = {
            "cookies": normalized_cookies,
            "origins": []
        }
        
        # Save to sqlite db instead of overwriting storage_state.json directly
        storage_state_str = json_mod.dumps(storage_state)
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO surfshark_cookies (name, cookies, status) VALUES (?, ?, 'active')",
            (label, storage_state_str)
        )
        conn.commit()
        conn.close()
        
        # Also refresh current browser session
        close_surfshark_browser()
        
        bot.send_message(message.chat.id,
            "✅ *Surfshark Cookie Stored in DB!* 🎉\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏷 *Label:* `{label}`\n"
            f"🍪 Normalized `{len(normalized_cookies)}` cookies.\n"
            f"🔄 Surfshark browser session re-initialized.\n\n"
            "⚡ Next login will pick an active account from pool!",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception as e:
        bot.send_message(message.chat.id,
                         f"❌ *Failed to add Surfshark cookies:* `{short_error(e)}`",
                         parse_mode="Markdown", reply_markup=admin_menu())

def process_delete_surf_cookie(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        cookie_id = int(message.text.strip())
        conn = get_db_connection()
        row = conn.execute("SELECT name FROM surfshark_cookies WHERE id=?", (cookie_id,)).fetchone()
        if not row:
            conn.close()
            bot.send_message(message.chat.id, "❌ *Cookie ID not found in database.*",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
        conn.execute("DELETE FROM surfshark_cookies WHERE id=?", (cookie_id,))
        conn.commit()
        conn.close()
        
        # Refresh current session
        close_surfshark_browser()
        
        bot.send_message(message.chat.id,
            f"🗑 *Surfshark Cookie Deleted!*\n\n"
            f"🆔 ID: `{cookie_id}`\n"
            f"🏷 Label: `{row['name']}`\n\n"
            "Session context cleared.",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ *Error deleting cookie:* `{short_error(e)}`",
                         parse_mode="Markdown", reply_markup=admin_menu())

# ── Canva Cookies Update Step ─────────────────────────────────────
def process_update_canva_cookies(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    import json as json_mod
    cookies_json = None
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            cookies_json = downloaded.decode('utf-8')
        except Exception as e:
            bot.send_message(message.chat.id,
                             f"❌ *Failed to download file:* `{short_error(e)}`",
                             parse_mode="Markdown", reply_markup=admin_menu())
            return
    elif message.text:
        cookies_json = message.text
    else:
        bot.send_message(message.chat.id, "❌ *Send cookies as text or a file.*",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        json_mod.loads(cookies_json)
    except (json_mod.JSONDecodeError, ValueError):
        bot.send_message(message.chat.id,
                         "❌ *Invalid JSON format.*\n\nMake sure the cookies are valid JSON.",
                         parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        with open(CANVA_STORAGE, 'w', encoding='utf-8') as f:
            f.write(cookies_json)
        close_canva_browser()
        bot.send_message(message.chat.id,
            "✅ *Canva Cookies Updated!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🍪 `canva_storage_state.json` has been saved.\n"
            "🔄 Canva browser session refreshed.\n"
            "⚡ Next Canva Business invite will use these cookies.",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    except Exception as e:
        bot.send_message(message.chat.id,
                         f"❌ *Failed to save cookies:* `{short_error(e)}`",
                         parse_mode="Markdown", reply_markup=admin_menu())

# ── Run ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print("[*] Bot starting...")
    print(f"[+] Token: {TOKEN[:20]}...")
    print(f"[+] Admins loaded: {ADMIN_IDS}")
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
