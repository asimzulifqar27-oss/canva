import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import sqlite3
import time
import re
import os
import threading
import html
import atexit
import json
import io
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from premium_emojis import EMOJI_MAP

load_dotenv()

TOKEN = '8798428200:AAE2CgI7WwCQRSDbD1Z0yj-JUnbnSKEezbw'
ADMIN_ID = 7604473724
OWNER_IDS = {ADMIN_ID, 5937217262, 6330429432}
ADMIN_IDS = set(OWNER_IDS)
CHANNELS = ["@siddmethodsgiveway"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "canva_bot.db")
CANVA_STORAGE = os.getenv("CANVA_STORAGE_STATE", os.path.join(BASE_DIR, "canva_storage_state.json"))
DUOLINGO_ACCOUNTS_PATH = os.getenv("DUOLINGO_ACCOUNTS_PATH", os.path.join(BASE_DIR, "accounts.txt"))
DUOLINGO_LEGACY_ACCOUNTS_PATH = os.path.join(BASE_DIR, "account.txt")
SURFSHARK_STORAGE = os.getenv("SURFSHARK_STORAGE_STATE", os.path.join(BASE_DIR, "storage_state.json"))
SURFSHARK_CODE_URL = os.getenv("SURFSHARK_CODE_URL", "https://my.surfshark.com/account/login-code")
SURFSHARK_CODE_RE = re.compile(r"\b([A-Za-z0-9]{6})\b")
CANVA_CREDIT_COST = 1
SURFSHARK_CREDIT_COST = 2
CANVA_BUSINESS_COOLDOWN = 24 * 60 * 60
CANVA_BUSINESS_WEEK_LIMIT = 3
PRO_REQUEST_COOLDOWN = 12 * 60 * 60
SURFSHARK_CODE_COOLDOWN = 60
SURFSHARK_FAIL_LIMIT = 5
SURFSHARK_FAIL_WINDOW = 60 * 60
REFERRAL_REWARD_DELAY = 5 * 60
REFERRAL_VELOCITY_LIMIT = 5
REFERRAL_VELOCITY_WINDOW = 10 * 60
surfshark_lock = threading.Lock()
surfshark_playwright = None
surfshark_browser = None
surfshark_context = None

bot = telebot.TeleBot(TOKEN, threaded=False)

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
    text = re.sub(r'`([^`]*?)`', lambda m: f"<code>{html.escape(m.group(1))}</code>", text, flags=re.DOTALL)
    text = re.sub(r'\*(.*?)\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'_(.*?)_', r'<i>\1</i>', text, flags=re.DOTALL)
    return premium_emoji_html(text)

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

original_inline_btn_init = InlineKeyboardButton.__init__
original_inline_btn_to_dict = InlineKeyboardButton.to_dict
def custom_inline_btn_init(self, text, *args, **kwargs):
    original_text = text
    forced_icon_id = kwargs.pop('icon_custom_emoji_id', None)
    matched_id = None
    for emoji, eid in PREMIUM_EMOJI_ITEMS:
        if text.startswith(emoji):
            text = text[len(emoji):].strip()
            matched_id = str(eid)
            break
    original_inline_btn_init(self, text if matched_id else original_text, *args, **kwargs)
    self._icon_custom_emoji_id = forced_icon_id or matched_id

def custom_inline_btn_to_dict(self):
    d = original_inline_btn_to_dict(self)
    icon_id = getattr(self, '_icon_custom_emoji_id', None)
    if icon_id:
        d['icon_custom_emoji_id'] = str(icon_id)
    return d

InlineKeyboardButton.__init__ = custom_inline_btn_init
InlineKeyboardButton.to_dict = custom_inline_btn_to_dict

original_keyboard_btn_init = KeyboardButton.__init__
original_keyboard_btn_to_dict = KeyboardButton.to_dict
def custom_keyboard_btn_init(self, text, *args, **kwargs):
    original_text = text
    forced_icon_id = kwargs.pop('icon_custom_emoji_id', None)
    matched_id = None
    if not forced_icon_id:
        for emoji, eid in PREMIUM_EMOJI_ITEMS:
            if text.startswith(emoji):
                text = text[len(emoji):].strip()
                matched_id = str(eid)
                break
    try:
        original_keyboard_btn_init(self, text if matched_id else original_text, *args, **kwargs)
    except TypeError:
        original_keyboard_btn_init(self, original_text, *args, **kwargs)
    self._icon_custom_emoji_id = forced_icon_id or matched_id

def custom_keyboard_btn_to_dict(self):
    d = original_keyboard_btn_to_dict(self)
    icon_id = getattr(self, '_icon_custom_emoji_id', None)
    if icon_id:
        d['icon_custom_emoji_id'] = str(icon_id)
    return d

KeyboardButton.__init__ = custom_keyboard_btn_init
KeyboardButton.to_dict = custom_keyboard_btn_to_dict


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
    c.execute('''CREATE TABLE IF NOT EXISTS abuse_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata TEXT DEFAULT '',
                    created_at INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS abuse_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    severity TEXT DEFAULT 'medium',
                    status TEXT DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL)''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_abuse_events_user_type_time ON abuse_events (user_id, event_type, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_abuse_flags_status_time ON abuse_flags (status, created_at)")
    conn.commit()
    conn.close()

def upgrade_db():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE users ADD COLUMN referrals INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN surfshark_credits INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN referral_rewarded INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
        
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, 
                    value TEXT)''')
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
    c.execute('''CREATE TABLE IF NOT EXISTS abuse_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    metadata TEXT DEFAULT '',
                    created_at INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS abuse_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    severity TEXT DEFAULT 'medium',
                    status TEXT DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL)''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_abuse_events_user_type_time ON abuse_events (user_id, event_type, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_abuse_flags_status_time ON abuse_flags (status, created_at)")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('pro_status', 'enabled')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_status', 'disabled')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('canva_mode', 'link')")
    conn.commit()
    conn.close()

init_db()
upgrade_db()

def load_admins_from_db():
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        conn.close()
        for row in rows:
            ADMIN_IDS.add(row['user_id'])
    except Exception:
        pass

load_admins_from_db()

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_owner(user_id):
    return user_id in OWNER_IDS

def add_admin_to_db(user_id, username, added_by):
    ADMIN_IDS.add(user_id)
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
        (user_id, username, added_by)
    )
    conn.commit()
    conn.close()

def remove_admin_from_db(user_id):
    ADMIN_IDS.discard(user_id)
    conn = get_db_connection()
    conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def notify_admins(text, **kwargs):
    for admin_id in list(ADMIN_IDS):
        try:
            bot.send_message(admin_id, text, **kwargs)
        except:
            pass

def now_ts():
    return int(time.time())

def format_wait(seconds):
    seconds = max(1, int(seconds))
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"

def log_abuse_event(user_id, event_type, metadata=''):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO abuse_events (user_id, event_type, metadata, created_at) VALUES (?, ?, ?, ?)",
        (user_id, event_type, str(metadata or ''), now_ts())
    )
    conn.commit()
    conn.close()

def count_recent_events(user_id, event_type, window_seconds):
    since = now_ts() - window_seconds
    conn = get_db_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM abuse_events WHERE user_id=? AND event_type=? AND created_at>=?",
        (user_id, event_type, since)
    ).fetchone()[0]
    conn.close()
    return count

def last_event_age(user_id, event_type):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT created_at FROM abuse_events WHERE user_id=? AND event_type=? ORDER BY created_at DESC LIMIT 1",
        (user_id, event_type)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return now_ts() - row['created_at']

def cooldown_remaining(user_id, event_type, cooldown_seconds):
    age = last_event_age(user_id, event_type)
    if age is None or age >= cooldown_seconds:
        return 0
    return cooldown_seconds - age

def create_abuse_flag(user_id, reason, severity='medium', notify=True):
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id FROM abuse_flags WHERE user_id=? AND reason=? AND status='open' LIMIT 1",
        (user_id, reason)
    ).fetchone()
    ts = now_ts()
    if existing:
        conn.execute("UPDATE abuse_flags SET updated_at=?, severity=? WHERE id=?", (ts, severity, existing['id']))
        flag_id = existing['id']
        is_new = False
    else:
        cur = conn.execute(
            "INSERT INTO abuse_flags (user_id, reason, severity, status, created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?)",
            (user_id, reason, severity, ts, ts)
        )
        flag_id = cur.lastrowid
        is_new = True
    conn.commit()
    conn.close()
    if notify and is_new:
        notify_admins(
            "⚠️ *Abuse Flag Created*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 User: `{user_id}`\n"
            f"🚦 Severity: `{severity}`\n"
            f"📝 Reason: {reason}\n"
            f"🆔 Flag: `{flag_id}`",
            parse_mode="Markdown"
        )
    return flag_id

def has_critical_abuse_flag(user_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM abuse_flags WHERE user_id=? AND status='open' AND severity IN ('high', 'critical') LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    return row is not None

def get_open_abuse_flags(limit=20):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM abuse_flags WHERE status='open' ORDER BY updated_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows

def resolve_abuse_flag(flag_id, resolved_by):
    conn = get_db_connection()
    conn.execute(
        "UPDATE abuse_flags SET status='resolved', updated_at=? WHERE id=?",
        (now_ts(), flag_id)
    )
    conn.commit()
    conn.close()
    log_abuse_event(resolved_by, "abuse_flag_resolved", f"flag={flag_id}")

def user_age_seconds(user_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT CAST((julianday('now') - julianday(joined_date)) * 86400 AS INTEGER) AS age FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row['age'] if row and row['age'] is not None else 0

def is_action_locked(user_id):
    return (not is_admin(user_id)) and (is_banned(user_id) or has_critical_abuse_flag(user_id))

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
    row = conn.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

def get_banned_list():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM banned_users ORDER BY banned_date DESC").fetchall()
    conn.close()
    return rows

def count_canva_ids(id_type='pro'):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM canva_ids WHERE type=? AND used=0",
        (id_type,)
    ).fetchone()
    conn.close()
    return row['cnt'] if row else 0

def close_canva_browser():
    return None

def count_duolingo_accounts():
    total = 0
    for path in (DUOLINGO_ACCOUNTS_PATH, DUOLINGO_LEGACY_ACCOUNTS_PATH):
        try:
            with open(path, "r", encoding="utf-8") as f:
                total += sum(1 for line in f if line.strip())
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return total

def format_duolingo_stock():
    return (
        "📦 *Duolingo Stock*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🦉 Available accounts: `{count_duolingo_accounts()}`"
    )

def restore_canva_link(link):
    conn = get_db_connection()
    conn.execute("INSERT INTO links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()

def send_claimed_canva_link(chat_id, text_msg, markup, user_id, charged, link):
    try:
        bot.send_message(chat_id, text_msg, parse_mode="Markdown", reply_markup=markup)
        return True
    except Exception:
        if charged:
            update_credits(user_id, charged)
        restore_canva_link(link)
        log_abuse_event(user_id, "canva_business_delivery_fail", "link_restored")
        bot.send_message(
            chat_id,
            "❌ *Could not deliver the Canva link.*\n\nYour credit was refunded. Please try again.",
            parse_mode="Markdown"
        )
        return False

def send_abuse_flags(chat_id):
    flags = get_open_abuse_flags()
    if not flags:
        bot.send_message(chat_id, "✅ *No open abuse flags.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    for flag in flags:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("🚫 Ban", callback_data=f"abuse_ban_{flag['id']}"),
            InlineKeyboardButton("🔄 Reset", callback_data=f"abuse_reset_{flag['id']}"),
            InlineKeyboardButton("✅ Clear", callback_data=f"abuse_clear_{flag['id']}")
        )
        bot.send_message(
            chat_id,
            "⚠️ *Open Abuse Flag*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Flag: `{flag['id']}`\n"
            f"👤 User: `{flag['user_id']}`\n"
            f"🚦 Severity: `{flag['severity']}`\n"
            f"📝 Reason: {flag['reason']}",
            parse_mode="Markdown",
            reply_markup=markup
        )

def get_flag(flag_id):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM abuse_flags WHERE id=?", (flag_id,)).fetchone()
    conn.close()
    return row

def get_setting(key):
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row:
        return row['value']
    return None

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
        "⚠️ *Maintenance Mode*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "The bot is getting a quick upgrade right now.\n"
        "Please try again in a little while."
    )

def block_member_during_maintenance(message):
    if not is_admin(message.from_user.id) and is_maintenance_enabled():
        send_maintenance_message(message.chat.id)
        return True
    return False

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

def set_pending_referral(user_id, referrer_id):
    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET referred_by=?, referral_rewarded=0 WHERE user_id=? AND referred_by IS NULL AND referral_rewarded=0",
        (referrer_id, user_id)
    )
    conn.commit()
    conn.close()

def reward_referral_if_eligible(user_id):
    user = get_user(user_id)
    if not user or user['referral_rewarded'] or not user['referred_by']:
        return False
    referrer_id = int(user['referred_by'])
    if (
        referrer_id == user_id
        or not get_user(referrer_id)
        or is_banned(user_id)
        or is_banned(referrer_id)
        or has_critical_abuse_flag(user_id)
        or has_critical_abuse_flag(referrer_id)
        or not check_channels(user_id)
        or user_age_seconds(user_id) < REFERRAL_REWARD_DELAY
    ):
        return False

    conn = get_db_connection()
    conn.execute(
        "UPDATE users SET referral_rewarded=1 WHERE user_id=? AND referral_rewarded=0",
        (user_id,)
    )
    changed = conn.total_changes
    if changed:
        conn.execute("UPDATE users SET referrals = referrals + 1, credits = credits + 1, surfshark_credits = surfshark_credits + 1 WHERE user_id=?", (referrer_id,))
        conn.commit()
    conn.close()

    if changed:
        log_abuse_event(referrer_id, "referral_reward", f"referred={user_id}")
        recent_referrals = count_recent_events(referrer_id, "referral_reward", REFERRAL_VELOCITY_WINDOW)
        if recent_referrals >= REFERRAL_VELOCITY_LIMIT:
            create_abuse_flag(
                referrer_id,
                f"Referral velocity: {recent_referrals} rewards in {REFERRAL_VELOCITY_WINDOW // 60} minutes",
                "high"
            )
        try:
            bot.send_message(
                referrer_id,
                "🎉 *Referral Reward Unlocked*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "👤 Someone joined using your invite link and joined the channel.\n"
                "🎁 You earned *1 Canva credit* 🪙\n"
                "🦈 You earned *1 Surfshark login credit*",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return True
    return False

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

def short_error(error, limit=900):
    text = html.escape(str(error))
    if len(text) > limit:
        text = text[:limit] + "\n\n...error trimmed..."
    return text

def block_heavy_surfshark_assets(route):
    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
    else:
        route.continue_()

def get_active_surfshark_storage_state():
    conn = get_db_connection()
    row = conn.execute("SELECT cookies FROM surfshark_cookies WHERE status='active' ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()
    if row:
        return json.loads(row['cookies'])
    if Path(SURFSHARK_STORAGE).exists():
        return SURFSHARK_STORAGE
    raise RuntimeError("No Surfshark cookies found. Add cookies from the admin panel or run login.py first.")

def get_surfshark_context():
    global surfshark_playwright, surfshark_browser, surfshark_context

    if surfshark_context is not None:
        return surfshark_context

    storage_state = get_active_surfshark_storage_state()
    surfshark_playwright = sync_playwright().start()
    surfshark_browser = surfshark_playwright.chromium.launch(
        headless=True,
        args=["--disable-background-networking", "--disable-dev-shm-usage", "--disable-extensions", "--disable-sync", "--no-first-run"],
    )
    surfshark_context = surfshark_browser.new_context(storage_state=storage_state)
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

def warm_surfshark_browser():
    try:
        get_surfshark_context()
        return True
    except Exception as e:
        print(f"Surfshark warmup skipped: {e}")
        return False

atexit.register(close_surfshark_browser)

def submit_surfshark_code(code):
    timings = {}
    started = time.monotonic()
    with surfshark_lock:
        context = get_surfshark_context()
        timings["browser"] = time.monotonic() - started
        page = context.new_page()
        try:
            step_started = time.monotonic()
            page.goto(SURFSHARK_CODE_URL, wait_until="commit", timeout=15000)

            if "log-in" in page.url and "login-code" not in page.url:
                timings["page"] = time.monotonic() - step_started
                return False, (
                    "🔒 <b>Session expired on the server.</b>\n"
                    "An admin needs to run <code>login.py</code> again."
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
                        "🎉 <b>You're signed in</b>\n\n"
                        "Open the Surfshark app now. Your account should be ready to use."
                    ), timings
                checks += 1
                if checks % 4 == 0:
                    body = page.inner_text("body").lower()
                    if any(word in body for word in ("invalid", "expired", "incorrect", "wrong")):
                        timings["confirm"] = time.monotonic() - step_started
                        return False, (
                            "🚫 <b>Code rejected</b>\n\n"
                            "This code may be invalid or expired. Please generate a fresh code in the Surfshark app and send it here."
                        ), timings
                page.wait_for_timeout(100)

            timings["confirm"] = time.monotonic() - step_started
            return False, (
                "⏳ <b>Submitted, but not confirmed</b>\n\n"
                "Surfshark did not return a clear confirmation. Please open the app and check your login status."
            ), timings
        except Exception as e:
            return False, (
                "💥 <b>Something went wrong inside the Surfshark login helper.</b>\n\n"
                f"<code>{short_error(e)}</code>"
            ), timings
        finally:
            page.close()

def handle_surfshark_code(message, raw_text):
    match = SURFSHARK_CODE_RE.search(raw_text)
    if not match:
        return False

    user = get_user(message.from_user.id)
    if is_action_locked(message.from_user.id):
        bot.send_message(message.chat.id, "🚫 *Your account is locked.*", parse_mode="Markdown")
        return True
    remaining = cooldown_remaining(message.from_user.id, "surfshark_attempt", SURFSHARK_CODE_COOLDOWN)
    if not is_admin(message.from_user.id) and remaining:
        bot.send_message(
            message.chat.id,
            f"⏳ *Slow down a bit.*\n\nTry another Surfshark code in `{format_wait(remaining)}`.",
            parse_mode="Markdown"
        )
        return True
    if not is_admin(message.from_user.id) and (not user or user['surfshark_credits'] < SURFSHARK_CREDIT_COST):
        bot.send_message(
            message.chat.id,
            "🦈 *Surfshark Locked*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔢 Send your invite link to a new user first.\n\n"
            "Old Canva credits cannot be used for Surfshark.\n"
            f"Surfshark login costs `{SURFSHARK_CREDIT_COST}` credits.",
            parse_mode="Markdown",
            reply_markup=main_menu(message.from_user.id)
        )
        return True

    code = match.group(1).upper()
    status = bot.send_message(
        message.chat.id,
        f"🦈 <b>Checking your Surfshark code...</b>\n\n<code>{code}</code>"
    )
    charged = 0 if is_admin(message.from_user.id) else SURFSHARK_CREDIT_COST
    if charged:
        update_surfshark_credits(message.from_user.id, -charged)
    log_abuse_event(message.from_user.id, "surfshark_attempt", code)
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

    if not ok and charged:
        update_surfshark_credits(message.from_user.id, charged)
        reply += f"\n\n🪙 <b>{charged} Surfshark credits refunded.</b>"
    if ok:
        log_abuse_event(message.from_user.id, "surfshark_success", code)
    else:
        log_abuse_event(message.from_user.id, "surfshark_fail", code)
        recent_fails = count_recent_events(message.from_user.id, "surfshark_fail", SURFSHARK_FAIL_WINDOW)
        if recent_fails >= SURFSHARK_FAIL_LIMIT and not is_admin(message.from_user.id):
            create_abuse_flag(
                message.from_user.id,
                f"{recent_fails} failed Surfshark codes in {SURFSHARK_FAIL_WINDOW // 60} minutes",
                "critical"
            )
            ban_user(message.from_user.id, "Repeated failed Surfshark login codes", 0)
            reply += "\n\n🚫 <b>Your account was locked for repeated failed codes.</b>"

    elapsed = time.monotonic() - started_at
    header = "✅ <b>Success</b>\n\n" if ok else "❌ <b>Could not sign in</b>\n\n"
    timing_text = (
        f"\n\n⏱ <i>Completed in {elapsed:.1f} seconds.</i>"
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
        bot.send_message(message.chat.id, result_text)
    return True

def main_menu(user_id=None):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    
    pro_status = get_setting('pro_status')
    if pro_status == 'enabled':
        markup.row(KeyboardButton("💼 Canva Business"), KeyboardButton("👑 Canva Pro"))
    else:
        markup.row(KeyboardButton("💼 Canva Business"))
        
    markup.row(KeyboardButton("🦈 Surfshark Login"))
    markup.row(KeyboardButton("🛒 Buy Premium / Panel"), KeyboardButton("👤 My Account"))
    markup.row(KeyboardButton("🔗 Refer & Earn"), KeyboardButton("📞 Support"))
    if user_id and is_admin(user_id):
        markup.row(KeyboardButton("🛠 Maintenance"), KeyboardButton("⚙️ Admin Panel"))
    return markup

def admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("➕ Add Link"), KeyboardButton("📋 View Links"), KeyboardButton("🗑 Delete Link"))
    markup.row(KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Statistics"), KeyboardButton("🔰 Leaderboard"))
    markup.row(KeyboardButton("💰 Give Credits"), KeyboardButton("🦈 Give Surf Credits"), KeyboardButton("🎯 Set Credits"))
    markup.row(KeyboardButton("👤 User Info"), KeyboardButton("👥 All Users"), KeyboardButton("🎁 Global Gift"))
    markup.row(KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User"), KeyboardButton("📋 Banned List"))
    markup.row(KeyboardButton("👑 Pro Requests"), KeyboardButton("🔄 Reset User"), KeyboardButton("✉️ Message User"))
    markup.row(KeyboardButton("⚙️ Toggle Pro"), KeyboardButton("🛠 Maintenance"), KeyboardButton("📂 Backup DB"))
    markup.row(KeyboardButton("🦈 Surf Global Gift"), KeyboardButton("📤 Export Users"), KeyboardButton("🍪 Update Cookies"))
    markup.row(KeyboardButton("➕ Add Surf Cookie"), KeyboardButton("📋 View Surf Cookies"), KeyboardButton("🗑 Delete Surf Cookie"))
    markup.row(KeyboardButton("📦 Duolingo Stock"), KeyboardButton("⚠️ Abuse Flags"))
    markup.row(KeyboardButton("🔄 Toggle Canva Mode"), KeyboardButton("🍪 Canva Cookies"), KeyboardButton("🔃 Refresh Canva"))
    markup.row(KeyboardButton("🔙 Back to Main"))
    return markup

def welcome_text():
    return (
        "💎 *SIDD SAGA CANVA BOT*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👋 Welcome to your premium Canva access hub.\n\n"
        "👨‍💻 *Owner:* sidsaga\n"
        "🛠 *Developer:* Asim\n\n"
        "💼 *Canva Business*\n"
        "└ Instant team invite access\n\n"
        "👑 *Canva Pro*\n"
        "└ Secure manual activation\n\n"
        "🦈 *Surfshark Login*\n"
        "└ Requires a fresh invite credit\n\n"
        "👇 Choose an option from the menu below."
    )

def join_required_text():
    return (
        "💎 *SIDD SAGA CANVA BOT*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👨‍💻 *Owner:* sidsaga\n"
        "🛠 *Developer:* Asim\n\n"
        "🔒 Join our official updates channel to unlock the bot.\n\n"
        "✅ Free Canva Business access\n"
        "✅ Private Canva Pro upgrades\n"
        "✅ Fast support and updates\n\n"
        "👇 Tap below, join, then come back."
    )

def admin_panel_text():
    return (
        "⚙️ *Admin Command Center*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 Manage users, links, credits, broadcasts, bans, cookies, exports, and backups from the menu below."
    )

def cancel_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("❌ Cancel"))
    return markup

def join_channels_markup():
    markup = InlineKeyboardMarkup()
    for ch in CHANNELS:
        markup.add(InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{ch[1:]}"))
    markup.add(InlineKeyboardButton("✅ I have joined", callback_data="check_join"))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    parts = message.text.split()
    referrer_id = None
    if len(parts) > 1 and parts[1].isdigit():
        referrer_id = int(parts[1])

    user_id = message.from_user.id
    
    # Process New User
    user = get_user(user_id)
    if not user:
        add_user(user_id, message.from_user.username)
        
        # Process referral logic
        if referrer_id and referrer_id != user_id:
            set_pending_referral(user_id, referrer_id)
            referrer = get_user(referrer_id)
            if False and referrer:
                conn = get_db_connection()
                conn.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (referrer_id,))
                conn.commit()
                updated_referrer = conn.execute("SELECT referrals FROM users WHERE user_id=?", (referrer_id,)).fetchone()
                conn.close()
                
                if False and updated_referrer:
                    update_credits(referrer_id, 1)
                    update_surfshark_credits(referrer_id, 1)
                    try:
                        bot.send_message(
                            referrer_id,
                            "🎉 *Referral Reward Unlocked*\n"
                            "━━━━━━━━━━━━━━━━━━━━\n\n"
                            "👤 Someone joined using your invite link.\n"
                            "🎁 You earned *1 Canva credit* 🪙\n"
                            "🦈 You earned *1 Surfshark login credit*",
                            parse_mode="Markdown"
                        )
                    except:
                        pass

    if not is_admin(user_id) and is_maintenance_enabled():
        send_maintenance_message(message.chat.id)
        return

    if is_action_locked(user_id):
        bot.send_message(message.chat.id, "🚫 *Your account is locked.*", parse_mode="Markdown")
        return

    if not check_channels(message.from_user.id):
        text = join_required_text()
        bot.send_message(message.chat.id, text, reply_markup=join_channels_markup())
    else:
        reward_referral_if_eligible(user_id)
        text = welcome_text()
        bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=main_menu(user_id))

@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def callback_check_join(call):
    if not is_admin(call.from_user.id) and is_maintenance_enabled():
        bot.answer_callback_query(call.id, "Bot is under maintenance.", show_alert=True)
        send_maintenance_message(call.message.chat.id)
        return

    if is_action_locked(call.from_user.id):
        bot.answer_callback_query(call.id, "Your account is locked.", show_alert=True)
        return

    if check_channels(call.from_user.id):
        bot.answer_callback_query(call.id, "Thank you for joining!")
        reward_referral_if_eligible(call.from_user.id)
        text = welcome_text()
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown", reply_markup=main_menu(call.from_user.id))
    else:
        bot.answer_callback_query(call.id, "You haven't joined yet!", show_alert=True)

@bot.message_handler(commands=['addadmin'])
def cmd_add_admin(message):
    if not is_owner(message.from_user.id):
        bot.send_message(message.chat.id, "❌ *Only owners can add admins.*", parse_mode="Markdown")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "❌ *Usage:* `/addadmin <user_id>`", parse_mode="Markdown")
        return
    new_admin_id = int(parts[1])
    username = None
    try:
        chat = bot.get_chat(new_admin_id)
        username = chat.username
    except Exception:
        pass
    add_admin_to_db(new_admin_id, username, message.from_user.id)
    bot.send_message(message.chat.id, f"✅ *Admin added.*\n\n👤 ID: `{new_admin_id}`", parse_mode="Markdown")
    try:
        bot.send_message(new_admin_id, "🛠 *You are now an admin.*\n\nUse /admin to open the admin panel.", parse_mode="Markdown")
    except Exception:
        pass

@bot.message_handler(commands=['removeadmin'])
def cmd_remove_admin(message):
    if not is_owner(message.from_user.id):
        bot.send_message(message.chat.id, "❌ *Only owners can remove admins.*", parse_mode="Markdown")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "❌ *Usage:* `/removeadmin <user_id>`", parse_mode="Markdown")
        return
    target_id = int(parts[1])
    if target_id in OWNER_IDS:
        bot.send_message(message.chat.id, "🚫 *Cannot remove an owner from admin.*", parse_mode="Markdown")
        return
    remove_admin_from_db(target_id)
    bot.send_message(message.chat.id, f"✅ *Admin removed.*\n\n👤 ID: `{target_id}`", parse_mode="Markdown")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, admin_panel_text(), reply_markup=admin_menu())
    elif is_maintenance_enabled():
        send_maintenance_message(message.chat.id)

@bot.message_handler(commands=['duostock', 'duolingo_stock'])
def cmd_duolingo_stock(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ *Only admins can check Duolingo stock.*", parse_mode="Markdown")
        return
    bot.send_message(message.chat.id, format_duolingo_stock(), parse_mode="Markdown", reply_markup=admin_menu())

@bot.callback_query_handler(func=lambda call: call.data.startswith("abuse_"))
def callback_abuse_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Admins only.", show_alert=True)
        return
    try:
        _, action, flag_id_text = call.data.split("_", 2)
        flag_id = int(flag_id_text)
        flag = get_flag(flag_id)
        if not flag:
            bot.answer_callback_query(call.id, "Flag not found.", show_alert=True)
            return
        target_id = int(flag['user_id'])
        if action == "ban":
            if target_id in ADMIN_IDS:
                bot.answer_callback_query(call.id, "Cannot ban an admin.", show_alert=True)
                return
            ban_user(target_id, f"Abuse flag #{flag_id}: {flag['reason']}", call.from_user.id)
            resolve_abuse_flag(flag_id, call.from_user.id)
            bot.answer_callback_query(call.id, "User banned.")
        elif action == "reset":
            conn = get_db_connection()
            conn.execute("UPDATE users SET credits=0, surfshark_credits=0, referrals=0 WHERE user_id=?", (target_id,))
            conn.commit()
            conn.close()
            log_abuse_event(target_id, "admin_reset_from_flag", f"flag={flag_id}")
            bot.answer_callback_query(call.id, "User reset.")
        elif action == "clear":
            resolve_abuse_flag(flag_id, call.from_user.id)
            bot.answer_callback_query(call.id, "Flag cleared.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Failed: {short_error(e, 120)}", show_alert=True)

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    raw_text = message.text.strip()
    text = raw_text
    
    known_commands = [
        "🔙 Back to Main", "🛒 Buy Premium / Panel", "👤 My Account", 
        "🔗 Refer & Earn", "📞 Support", "💼 Canva Business", "👑 Canva Pro", 
        "Surfshark Login", "🦈 Surfshark Login", "🏄‍♀️ Surfshark Login",
        "➕ Add Link", "📋 View Links",
        "📢 Broadcast", "📊 Statistics", 
        "💰 Give Credits", "🗑 Delete Link", "👤 User Info", "🎁 Global Gift", 
        "⚙️ Toggle Pro", "🛠 Maintenance", "⚙️ Admin Panel", "✉️ Message User", "📂 Backup DB",
        "🔰 Leaderboard", "🏆 Leaderboard",
        "🦈 Give Surf Credits", "Give Surf Credits", "🎯 Set Credits", "👥 All Users",
        "🚫 Ban User", "✅ Unban User", "📋 Banned List", "👑 Pro Requests", "🔄 Reset User",
        "🦈 Surf Global Gift", "Surf Global Gift", "📤 Export Users", "🍪 Update Cookies",
        "➕ Add Surf Cookie", "📋 View Surf Cookies", "🗑 Delete Surf Cookie",
        "📦 Duolingo Stock", "⚠️ Abuse Flags", "🔄 Toggle Canva Mode", "🍪 Canva Cookies", "🔃 Refresh Canva"
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

    if is_action_locked(user_id):
        bot.send_message(
            message.chat.id,
            "🚫 *Your account is locked.*\n\nContact support if you think this is a mistake.",
            parse_mode="Markdown"
        )
        return

    if text == "🔙 Back to Main":
        bot.send_message(message.chat.id, welcome_text(), reply_markup=main_menu(user_id))
        return

    if not is_admin(user_id) and not check_channels(user_id):
        bot.send_message(message.chat.id, join_required_text(), reply_markup=join_channels_markup())
        return
    reward_referral_if_eligible(user_id)

    if handle_surfshark_code(message, raw_text):
        return

    if text == "🛒 Buy Premium / Panel":
        msg = (
            "🛒 *Premium Deals & Panels*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "💎 Want your own *Canva Business admin panel* or a private *Canva Pro* account?\n\n"
            "💬 *Support:* @siddheshsaga\\_bot\n"
            "⚡ Fast setup, clean pricing, direct support."
        )
        bot.send_message(message.chat.id, msg, parse_mode="Markdown")

    elif text == "👤 My Account":
        credits = "∞" if is_admin(user_id) else user['credits']
        surfshark_credits = "∞" if is_admin(user_id) else user['surfshark_credits']
        username = f"@{user['username']}" if user['username'] else "Not set"
        bot.send_message(
            message.chat.id,
            "👤 *Your Account*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 *ID:* `{user['user_id']}`\n"
            f"👨‍💻 *Username:* {username}\n"
            f"🪙 *Credits:* `{credits}`\n"
            f"🦈 *Surfshark Credits:* `{surfshark_credits}`\n"
            f"👥 *Referrals:* `{user['referrals']}`",
            parse_mode="Markdown"
        )

    elif text == "🔗 Refer & Earn":
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        msg = (
            "🔗 *Refer & Earn*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👥 Invite friends and collect free credits.\n\n"
            "🎁 *Reward:* `1` Canva credit per successful referral\n"
            "🦈 *Surfshark:* `1` fresh login credit per new referral\n\n"
            "Old Canva credits cannot be used for Surfshark.\n"
            f"📊 *Your Referrals:* `{user['referrals']}`\n\n"
            f"🔗 *Your Link:*\n`{ref_link}`"
        )
        bot.send_message(message.chat.id, msg, parse_mode="Markdown")

    elif text == "📞 Support":
        bot.send_message(
            message.chat.id,
            "📞 *Support & Updates*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "👨‍💻 *Owner:* sidsaga\n"
            "🛠 *Developer:* Asim\n"
            "🤖 *Support Bot:* @siddheshsaga\\_bot\n"
            "🌐 *Updates:* @siddmethodsgiveway",
            parse_mode="Markdown"
        )

    elif text in ["Surfshark Login", "🦈 Surfshark Login", "🏄 Surfshark Login", "🏄‍♀️ Surfshark Login"]:
        bot.send_message(
            message.chat.id,
            "🦈 *Surfshark Quick Login*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔢 Send your 6-character Surfshark login code.\n\n"
            f"🪙 This deducts `{SURFSHARK_CREDIT_COST}` Surfshark credits.\n"
            "🔗 Need credit? Invite a new user with your referral link."
            
        )

    elif text == "💼 Canva Business":
        if is_admin(user_id) or user['credits'] >= CANVA_CREDIT_COST:
            remaining = cooldown_remaining(user_id, "canva_business_claim", CANVA_BUSINESS_COOLDOWN)
            if not is_admin(user_id) and remaining:
                bot.send_message(
                    message.chat.id,
                    f"⏳ *Canva claim cooldown active.*\n\nTry again in `{format_wait(remaining)}`.",
                    parse_mode="Markdown"
                )
                return
            weekly_claims = count_recent_events(user_id, "canva_business_claim", 7 * 24 * 60 * 60)
            if not is_admin(user_id) and weekly_claims >= CANVA_BUSINESS_WEEK_LIMIT:
                create_abuse_flag(user_id, f"Canva weekly claim limit reached: {weekly_claims} claims", "medium")
                bot.send_message(
                    message.chat.id,
                    "⚠️ *Weekly Canva limit reached.*\n\nPlease wait before claiming more Canva Business links.",
                    parse_mode="Markdown"
                )
                return
            charged = 0 if is_admin(user_id) else CANVA_CREDIT_COST
            if charged:
                update_credits(user_id, -charged)

            conn = get_db_connection()
            link_row = conn.execute("SELECT * FROM links ORDER BY RANDOM() LIMIT 1").fetchone()
            if link_row:
                conn.execute(
                    "INSERT INTO link_claims (user_id, link_id, link) VALUES (?, ?, ?)",
                    (user_id, link_row['id'], link_row['link'])
                )
                conn.execute("DELETE FROM links WHERE id=?", (link_row['id'],))
                conn.commit()
            conn.close()
            
            if link_row:
                text_msg = (
                    "🎉 *Canva Business Unlocked*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "💼 Your team invite is ready.\n"
                    "💡 Please do not share this link with others.\n\n"
                    f"🪙 *{CANVA_CREDIT_COST} credit deducted.*"
                )
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("🔗 Join Canva Business Team", url=link_row['link']))
                if send_claimed_canva_link(message.chat.id, text_msg, markup, user_id, charged, link_row['link']):
                    log_abuse_event(user_id, "canva_business_claim", f"link_id={link_row['id']}")
            else:
                if charged:
                    update_credits(user_id, charged)
                bot.send_message(message.chat.id, "❌ *No links available right now.*\n\nNo credit was deducted. Please check again later.", parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, "❌ *Not enough credits.*\n\nUse 🔗 *Refer & Earn* or contact support to get more.", parse_mode="Markdown")

    elif text == "👑 Canva Pro":
        if get_setting('pro_status') != 'enabled':
            bot.send_message(message.chat.id, "❌ *Canva Pro is currently disabled.*\n\nPlease check back later.", parse_mode="Markdown", reply_markup=main_menu(user_id))
            return
            
        if is_admin(user_id) or user['credits'] > 0:
            if not is_admin(user_id):
                conn = get_db_connection()
                pending = conn.execute("SELECT id FROM pro_requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
                conn.close()
                if pending:
                    log_abuse_event(user_id, "blocked_duplicate_pro_request", f"pending={pending['id']}")
                    bot.send_message(message.chat.id, "⏳ *You already have an active Canva Pro request.*\n\nPlease wait for the current request to finish.", parse_mode="Markdown")
                    return
                remaining = cooldown_remaining(user_id, "canva_pro_request", PRO_REQUEST_COOLDOWN)
                if remaining:
                    bot.send_message(message.chat.id, f"⏳ *Canva Pro cooldown active.*\n\nTry again in `{format_wait(remaining)}`.", parse_mode="Markdown")
                    return
            msg = bot.send_message(
                message.chat.id,
                "👑 *Canva Pro Activation*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📩 Send the email address you want upgraded.",
                reply_markup=cancel_menu()
            )
            bot.register_next_step_handler(msg, process_pro_email)
        else:
            bot.send_message(message.chat.id, "❌ *Not enough credits.*\n\nUse 🔗 *Refer & Earn* or contact support to get more.", parse_mode="Markdown")

    elif is_admin(user_id):
        if text == "⚙️ Admin Panel":
            bot.send_message(message.chat.id, admin_panel_text(), reply_markup=admin_menu())
        elif text == "➕ Add Link":
            msg = bot.send_message(message.chat.id, "➕ *Add Canva Business Link*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the invite link you want to store.", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_add_link)
        elif text == "📋 View Links":
            conn = get_db_connection()
            links = conn.execute("SELECT * FROM links").fetchall()
            conn.close()
            if links:
                res = "📋 *Stored Canva Links*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for l in links:
                    res += f"🆔 `{l['id']}`\n🔗 {l['link']}\n\n"
                bot.send_message(message.chat.id, res)
            else:
                bot.send_message(message.chat.id, "📭 *No links saved yet.*")
        elif text == "📢 Broadcast":
            msg = bot.send_message(message.chat.id, "📢 *Broadcast Message*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the text, photo, video, or file you want delivered to all users.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_broadcast)
        elif text == "📊 Statistics":
            conn = get_db_connection()
            users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            pro_reqs = conn.execute("SELECT COUNT(*) FROM pro_requests").fetchone()[0]
            canva_pro_ids = count_canva_ids('pro')
            canva_biz_ids = count_canva_ids('business')
            admins_count = len(ADMIN_IDS)
            open_flags = conn.execute("SELECT COUNT(*) FROM abuse_flags WHERE status='open'").fetchone()[0]
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
                f"⚙️ *Total Admins:* `{admins_count}`\n"
                f"⚠️ *Open Abuse Flags:* `{open_flags}`",
                parse_mode="Markdown"
            )
        elif text == "💰 Give Credits":
            msg = bot.send_message(message.chat.id, "💰 *Give Credits*\n━━━━━━━━━━━━━━━━━━━━\n\nSend user ID and amount.\n\nExample: `123456789 5`", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_give_credits)
        elif text == "🗑 Delete Link":
            msg = bot.send_message(message.chat.id, "🗑 *Delete Link*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the link ID you want removed.", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_delete_link)
        elif text == "👤 User Info":
            msg = bot.send_message(message.chat.id, "👤 *User Lookup*\n━━━━━━━━━━━━━━━━━━━━\n\nSend a user ID or username.", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_user_info)
        elif text == "🎁 Global Gift":
            msg = bot.send_message(message.chat.id, "🎁 *Global Gift*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the amount of credits to give every user.", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_global_gift)
        elif text == "⚙️ Toggle Pro":
            current = get_setting('pro_status')
            new_status = 'disabled' if current == 'enabled' else 'enabled'
            update_setting('pro_status', new_status)
            bot.send_message(message.chat.id, f"✅ *Canva Pro is now {new_status.upper()}.*", parse_mode="Markdown", reply_markup=main_menu(user_id))
            bot.send_message(message.chat.id, admin_panel_text(), reply_markup=admin_menu())
        elif text == "🛠 Maintenance":
            current = get_setting('maintenance_status')
            new_status = 'disabled' if current == 'enabled' else 'enabled'
            update_setting('maintenance_status', new_status)
            bot.send_message(message.chat.id, f"🛠 *Maintenance mode is now {new_status.upper()}.*", parse_mode="Markdown", reply_markup=admin_menu())
        elif text in ["🔰 Leaderboard", "🏆 Leaderboard"]:
            conn = get_db_connection()
            top_users = conn.execute("SELECT user_id, username, referrals FROM users ORDER BY referrals DESC LIMIT 10").fetchall()
            conn.close()
            if top_users:
                res = "🏆 *Top 10 Referrers*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for i, u in enumerate(top_users):
                    uname = f"@{u['username']}" if u['username'] else f"`{u['user_id']}`"
                    res += f"{i+1}. {uname}  •  👥 `{u['referrals']}` referrals\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, "📭 *No leaderboard data yet.*")
        elif text == "📂 Backup DB":
            try:
                with open(DB_PATH, "rb") as doc:
                    bot.send_document(message.chat.id, doc, caption="📂 *Database Backup Ready*", parse_mode="Markdown")
            except Exception as e:
                bot.send_message(message.chat.id, "❌ *Database backup failed.*")
        elif text == "✉️ Message User":
            msg = bot.send_message(message.chat.id, "✉️ *Message User*\n━━━━━━━━━━━━━━━━━━━━\n\nSend user ID and message.\n\nExample: `123456789 Hello bro!`", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_admin_message)
        elif text in ["🦈 Give Surf Credits", "Give Surf Credits"]:
            msg = bot.send_message(message.chat.id, "🦈 *Give Surfshark Credits*\n━━━━━━━━━━━━━━━━━━━━\n\nSend user ID and amount.\n\nExample: `123456789 3`", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_give_surf_credits)
        elif text == "🎯 Set Credits":
            msg = bot.send_message(message.chat.id, "🎯 *Set Credits*\n━━━━━━━━━━━━━━━━━━━━\n\nFormat: `USER_ID CANVA_CREDITS SURFSHARK_CREDITS`\n\nExample: `123456789 10 5`", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_set_credits)
        elif text == "👥 All Users":
            conn = get_db_connection()
            users_list = conn.execute("SELECT user_id, username, credits, surfshark_credits, referrals FROM users ORDER BY joined_date DESC LIMIT 50").fetchall()
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            conn.close()
            if users_list:
                res = f"👥 *All Users* (latest 50 of {total})\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for u in users_list:
                    uname = f"@{u['username']}" if u['username'] else "-"
                    banned_tag = " 🚫" if is_banned(u['user_id']) else ""
                    admin_tag = " 👑" if u['user_id'] in ADMIN_IDS else ""
                    res += f"`{u['user_id']}` {uname}{admin_tag}{banned_tag}\n  🪙 `{u['credits']}` • 🦈 `{u['surfshark_credits']}` • 👥 `{u['referrals']}`\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "📭 *No users yet.*", parse_mode="Markdown", reply_markup=admin_menu())
        elif text == "🚫 Ban User":
            msg = bot.send_message(message.chat.id, "🚫 *Ban User*\n━━━━━━━━━━━━━━━━━━━━\n\nFormat: `USER_ID REASON`\n\nReason is optional.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_ban_user)
        elif text == "✅ Unban User":
            msg = bot.send_message(message.chat.id, "✅ *Unban User*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the user ID to unban.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_unban_user)
        elif text == "📋 Banned List":
            banned = get_banned_list()
            if banned:
                res = "🚫 *Banned Users*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for b in banned:
                    res += f"🚫 `{b['user_id']}` — _{b['reason'] or 'No reason'}_\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "✅ *No banned users.*", parse_mode="Markdown", reply_markup=admin_menu())
        elif text == "👑 Pro Requests":
            conn = get_db_connection()
            reqs = conn.execute("SELECT * FROM pro_requests ORDER BY id DESC LIMIT 20").fetchall()
            conn.close()
            if reqs:
                res = "👑 *Pro Requests (Last 20)*\n━━━━━━━━━━━━━━━━━━━━\n\n"
                for r in reqs:
                    status_icon = "✅" if r['status'] == 'done' else "⏳"
                    res += f"{status_icon} `[{r['id']}]`\n  👤 `{r['user_id']}`\n  📩 `{r['email']}`\n  📌 Status: `{r['status']}`\n\n"
                bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())
            else:
                bot.send_message(message.chat.id, "📭 *No pro requests yet.*", parse_mode="Markdown", reply_markup=admin_menu())
        elif text == "🔄 Reset User":
            msg = bot.send_message(message.chat.id, "🔄 *Reset User*\n━━━━━━━━━━━━━━━━━━━━\n\nThis resets Canva credits, Surfshark credits, and referrals to 0.\n\nSend the user ID.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_reset_user)
        elif text in ["🦈 Surf Global Gift", "Surf Global Gift"]:
            msg = bot.send_message(message.chat.id, "🦈 *Surfshark Global Gift*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the amount of Surfshark credits to give every user.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_surf_global_gift)
        elif text == "📤 Export Users":
            export_users_csv(message)
        elif text == "🍪 Update Cookies":
            msg = bot.send_message(message.chat.id, "🍪 *Update Surfshark Cookies*\n━━━━━━━━━━━━━━━━━━━━\n\nUpload a `.json` / `.txt` file or paste the JSON cookies text directly.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_update_cookies)
        elif text == "➕ Add Surf Cookie":
            msg = bot.send_message(message.chat.id, "➕ *Add Surfshark Cookie*\n━━━━━━━━━━━━━━━━━━━━\n\nSend `AccountLabel` on the first line and JSON cookies after it, or upload a file with the label as caption.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_add_surf_cookie)
        elif text == "📋 View Surf Cookies":
            view_surf_cookies(message)
        elif text == "🗑 Delete Surf Cookie":
            msg = bot.send_message(message.chat.id, "🗑 *Delete Surfshark Cookie*\n━━━━━━━━━━━━━━━━━━━━\n\nSend the cookie ID to remove.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_delete_surf_cookie)
        elif text == "📦 Duolingo Stock":
            bot.send_message(message.chat.id, format_duolingo_stock(), parse_mode="Markdown", reply_markup=admin_menu())
        elif text == "⚠️ Abuse Flags":
            send_abuse_flags(message.chat.id)
        elif text == "🔄 Toggle Canva Mode":
            current = get_setting('canva_mode') or 'link'
            new_mode = 'auto' if current == 'link' else 'link'
            update_setting('canva_mode', new_mode)
            mode_label = "🤖 AUTO (Email Invite)" if new_mode == 'auto' else "🔗 LINK (Stored Links)"
            bot.send_message(message.chat.id, f"🔄 *Canva Business Mode Changed*\n━━━━━━━━━━━━━━━━━━━━\n\nMode: *{mode_label}*", parse_mode="Markdown", reply_markup=admin_menu())
        elif text == "🍪 Canva Cookies":
            msg = bot.send_message(message.chat.id, "🍪 *Update Canva Cookies*\n━━━━━━━━━━━━━━━━━━━━\n\nUpload a `.json` / `.txt` file or paste the JSON cookies text directly.", parse_mode="Markdown", reply_markup=cancel_menu())
            bot.register_next_step_handler(msg, process_update_canva_cookies)
        elif text == "🔃 Refresh Canva":
            close_canva_browser()
            bot.send_message(message.chat.id, "✅ *Canva Browser Refreshed*\n\nNext Canva action will use a fresh browser session.", parse_mode="Markdown", reply_markup=admin_menu())

def process_pro_email(message):
    if block_member_during_maintenance(message):
        return

    email = message.text
    user_id = message.from_user.id

    if email == "❌ Cancel":
        bot.send_message(message.chat.id, "❌ *Cancelled.*\n\nNo credit was deducted.", reply_markup=main_menu(user_id))
        return

    if "@" not in email:
        bot.send_message(message.chat.id, "❌ *Invalid email.*\n\nPlease start again with a valid email address.", reply_markup=main_menu(user_id))
        return

    user = get_user(user_id)
    if not is_admin(user_id) and (not user or user['credits'] < CANVA_CREDIT_COST):
        bot.send_message(message.chat.id, "❌ *Not enough Canva credits.*\n\nNo request was started.", parse_mode="Markdown", reply_markup=main_menu(user_id))
        return
    if not is_admin(user_id):
        conn = get_db_connection()
        pending = conn.execute("SELECT id FROM pro_requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        conn.close()
        if pending:
            log_abuse_event(user_id, "blocked_duplicate_pro_request", f"pending={pending['id']}")
            bot.send_message(message.chat.id, "⏳ *You already have an active Canva Pro request.*", parse_mode="Markdown", reply_markup=main_menu(user_id))
            return
        remaining = cooldown_remaining(user_id, "canva_pro_request", PRO_REQUEST_COOLDOWN)
        if remaining:
            bot.send_message(message.chat.id, f"⏳ *Canva Pro cooldown active.*\n\nTry again in `{format_wait(remaining)}`.", parse_mode="Markdown", reply_markup=main_menu(user_id))
            return
    
    update_credits(user_id, -CANVA_CREDIT_COST)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO pro_requests (user_id, email) VALUES (?, ?)", (user_id, email))
    conn.commit()
    conn.close()
    log_abuse_event(user_id, "canva_pro_request", email)
    
    bot.send_message(
        message.chat.id,
        "✅ *Request Submitted*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👑 An admin will log in and activate Canva Pro for you.\n"
        "🔔 You will be asked for a login code when needed.",
        reply_markup=main_menu(user_id)
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔑 Ask User for Code", callback_data=f"ask_code_{user_id}"))
    notify_admins(
        "🔔 *New Canva Pro Request*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 *User:* `{user_id}`\n"
        f"📩 *Email:* `{email}`\n\n"
        "Tap below when you are ready to request the login code.",
        parse_mode="Markdown",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("ask_code_"))
def callback_ask_code(call):
    target_user_id = int(call.data.split("_")[2])
    
    conn = get_db_connection()
    req = conn.execute("SELECT email FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (target_user_id,)).fetchone()
    conn.close()
    email = req['email'] if req else "your email"

    try:
        msg = bot.send_message(
            target_user_id,
            "🔑 *Canva Login Code Needed*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📩 *Email:* `{email}`\n\n"
            "Check your inbox for the Canva login code and send it here.",
            parse_mode="Markdown",
            reply_markup=cancel_menu()
        )
        bot.register_next_step_handler(msg, lambda m: process_user_code(m, email))
        bot.answer_callback_query(call.id, "Message sent to user!")
        notify_admins(f"✅ Code request sent for `{email}`")
    except Exception as e:
        bot.answer_callback_query(call.id, "Failed to send message to user.", show_alert=True)

def process_user_code(message, email):
    if block_member_during_maintenance(message):
        return

    if message.text == "❌ Cancel":
        conn = get_db_connection()
        req = conn.execute("SELECT id, status FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (message.from_user.id,)).fetchone()
        if req and req['status'] == 'pending':
            conn.execute("UPDATE pro_requests SET status='refunded' WHERE id=?", (req['id'],))
            conn.commit()
            update_credits(message.from_user.id, CANVA_CREDIT_COST)
        conn.close()
        bot.send_message(message.chat.id, "❌ *Cancelled.*", reply_markup=main_menu(message.from_user.id))
        notify_admins(f"❌ User `{message.from_user.id}` cancelled code sharing for `{email}`.")
        return

    code = message.text
    bot.send_message(message.chat.id, "✅ *Code Sent*\n\nPlease wait while the admin completes activation.", reply_markup=main_menu(message.from_user.id))
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Login Successful", callback_data=f"pro_success_{message.from_user.id}"))
    markup.add(InlineKeyboardButton("❌ Ask for New Code", callback_data=f"pro_resend_{message.from_user.id}"))
    markup.add(InlineKeyboardButton("💸 Activation Failed + Refund", callback_data=f"pro_fail_{message.from_user.id}"))
    markup.add(InlineKeyboardButton("💬 Send Custom Msg", callback_data=f"pro_msg_{message.from_user.id}"))

    notify_admins(
        "🔑 *Canva Code Received*\n"
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
        conn = get_db_connection()
        conn.execute("UPDATE pro_requests SET status='done' WHERE user_id=? AND id=(SELECT id FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1)", (user_id, user_id))
        conn.commit()
        conn.close()
        bot.send_message(user_id, "🎉 *Canva Pro Activated*\n━━━━━━━━━━━━━━━━━━━━\n\nYour login was successful. Enjoy your premium access.", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Success message sent!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        bot.answer_callback_query(call.id, "Failed to send message.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_fail_"))
def callback_pro_fail(call):
    user_id = int(call.data.split("_")[2])
    try:
        conn = get_db_connection()
        req = conn.execute("SELECT id, status FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if req and req['status'] not in ("refunded", "done"):
            conn.execute("UPDATE pro_requests SET status='refunded' WHERE id=?", (req['id'],))
            conn.commit()
            update_credits(user_id, CANVA_CREDIT_COST)
            log_abuse_event(user_id, "canva_pro_refund", f"request={req['id']}")
            refunds = count_recent_events(user_id, "canva_pro_refund", 7 * 24 * 60 * 60)
            if refunds >= 3:
                create_abuse_flag(user_id, f"{refunds} Canva Pro refunds in 7 days", "medium")
        conn.close()
        bot.send_message(user_id, f"❌ *Canva Pro activation failed.*\n\n🪙 `{CANVA_CREDIT_COST}` Canva credit refunded.", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Refund sent.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        bot.answer_callback_query(call.id, "Failed to refund.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_resend_"))
def callback_pro_resend(call):
    user_id = int(call.data.split("_")[2])
    conn = get_db_connection()
    req = conn.execute("SELECT email FROM pro_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()
    email = req['email'] if req else "your email"

    try:
        msg = bot.send_message(user_id, "❌ *Login Code Failed*\n━━━━━━━━━━━━━━━━━━━━\n\nThe code was incorrect or expired. Please send the new Canva login code from your email.", parse_mode="Markdown", reply_markup=cancel_menu())
        bot.register_next_step_handler(msg, lambda m: process_user_code(m, email))
        bot.answer_callback_query(call.id, "Asked user for new code!")
    except:
        bot.answer_callback_query(call.id, "Failed to send message.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pro_msg_"))
def callback_pro_msg(call):
    user_id = call.data.split("_")[2]
    msg = bot.send_message(call.message.chat.id, "💬 *Custom Message*\n━━━━━━━━━━━━━━━━━━━━\n\nEnter the message you want to send to this user.", reply_markup=cancel_menu())
    bot.register_next_step_handler(msg, lambda m: process_custom_msg(m, user_id))

def process_custom_msg(message, target_user_id):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        bot.send_message(target_user_id, f"📩 *Message from Admin*\n━━━━━━━━━━━━━━━━━━━━\n\n{message.text}", parse_mode="Markdown")
        bot.send_message(message.chat.id, "✅ *Message sent successfully.*", reply_markup=admin_menu())
    except:
        bot.send_message(message.chat.id, "❌ *Failed to send message.*", reply_markup=admin_menu())


def process_give_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split()
        target_user_id = int(parts[0])
        amount = int(parts[1])
        update_credits(target_user_id, amount)
        bot.send_message(message.chat.id, f"✅ *Credits added.*\n\n👤 User: `{target_user_id}`\n🪙 Amount: `{amount}`", reply_markup=admin_menu())
        try:
            bot.send_message(target_user_id, f"🎉 *Credits Added*\n━━━━━━━━━━━━━━━━━━━━\n\n🪙 Admin gifted you `{amount}` credits.")
        except:
            pass
    except Exception as e:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID AMOUNT`", parse_mode="Markdown", reply_markup=admin_menu())

def process_add_link(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    link = message.text
    conn = get_db_connection()
    conn.execute("INSERT INTO links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, "✅ *Link added successfully.*", reply_markup=admin_menu())

def process_delete_link(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        link_id = int(message.text)
        conn = get_db_connection()
        conn.execute("DELETE FROM links WHERE id=?", (link_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *Link deleted.*\n\n🆔 Link ID: `{link_id}`", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, "❌ *Invalid link ID.*", reply_markup=admin_menu())

def process_user_info(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
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
            bot.send_message(
                message.chat.id,
                "👤 *User Info*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🆔 *ID:* `{user['user_id']}`\n"
                f"👨‍💻 *Username:* {username}\n"
                f"🪙 *Credits:* `{user['credits']}`\n"
                f"🦈 *Surfshark Credits:* `{user['surfshark_credits']}`\n"
                f"👥 *Referrals:* `{user['referrals']}`\n"
                f"📅 *Joined:* `{user['joined_date']}`",
                parse_mode="Markdown",
                reply_markup=admin_menu()
            )
        else:
            bot.send_message(message.chat.id, "❌ *User not found.*", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, "❌ *Error fetching user info.*", reply_markup=admin_menu())

def process_global_gift(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        amount = int(message.text)
        conn = get_db_connection()
        conn.execute("UPDATE users SET credits = credits + ?", (amount,))
        conn.commit()
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *Global gift applied.*\n\n🪙 Every user received `{amount}` credits.", reply_markup=admin_menu())
        bot.send_message(message.chat.id, "📢 *Broadcasting gift notice...*")
        
        success = 0
        for u in users:
            try:
                bot.send_message(u['user_id'], f"🎁 *Surprise Gift*\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 Owner @siddsaga gifted you `{amount}` free credits.\n\n🚀 Enjoy your premium access.", parse_mode="Markdown")
                success += 1
                time.sleep(0.05)
            except:
                pass
        bot.send_message(message.chat.id, f"✅ *Gift broadcast complete.*\n\n👥 Sent to `{success}` users.")
    except Exception as e:
        bot.send_message(message.chat.id, "❌ *Invalid amount.*", reply_markup=admin_menu())

def process_broadcast(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    conn = get_db_connection()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    success = 0
    bot.send_message(message.chat.id, "📢 *Broadcasting...*", reply_markup=admin_menu())
    for u in users:
        try:
            bot.copy_message(chat_id=u['user_id'], from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            time.sleep(0.05)
        except:
            pass
    bot.send_message(message.chat.id, f"✅ *Broadcast complete.*\n\n👥 Sent to `{success}` users.")

def process_admin_message(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split(" ", 1)
        uid = int(parts[0])
        text_msg = parts[1]
        bot.send_message(uid, f"📩 *Message from Admin*\n━━━━━━━━━━━━━━━━━━━━\n\n{text_msg}", parse_mode="Markdown")
        bot.send_message(message.chat.id, "✅ *Message sent successfully.*", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, "❌ *Invalid format or user blocked the bot.*", reply_markup=admin_menu())

def process_give_surf_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        target_user_id, amount = map(int, message.text.split()[:2])
        update_surfshark_credits(target_user_id, amount)
        bot.send_message(message.chat.id, f"✅ *Surfshark credits added.*\n\n👤 User: `{target_user_id}`\n🦈 Amount: `{amount}`", parse_mode="Markdown", reply_markup=admin_menu())
        try:
            bot.send_message(target_user_id, f"🦈 *Surfshark Credits Added*\n━━━━━━━━━━━━━━━━━━━━\n\nAdmin gifted you `{amount}` Surfshark credits.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID AMOUNT`", parse_mode="Markdown", reply_markup=admin_menu())

def process_set_credits(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split()
        target_user_id = int(parts[0])
        canva_credits = int(parts[1])
        surf_credits = int(parts[2]) if len(parts) > 2 else 0
        conn = get_db_connection()
        conn.execute("UPDATE users SET credits=?, surfshark_credits=? WHERE user_id=?", (canva_credits, surf_credits, target_user_id))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *Credits set.*\n\n👤 User: `{target_user_id}`\n🪙 Canva: `{canva_credits}`\n🦈 Surfshark: `{surf_credits}`", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID CANVA_CREDITS SURFSHARK_CREDITS`", parse_mode="Markdown", reply_markup=admin_menu())

def process_ban_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        parts = message.text.split(None, 1)
        target_id = int(parts[0])
        reason = parts[1] if len(parts) > 1 else ""
        if target_id in ADMIN_IDS:
            bot.send_message(message.chat.id, "🚫 *Cannot ban an admin.*", parse_mode="Markdown", reply_markup=admin_menu())
            return
        ban_user(target_id, reason, message.from_user.id)
        bot.send_message(message.chat.id, f"🚫 *User banned.*\n\n👤 ID: `{target_id}`\n📝 Reason: _{reason or 'No reason'}_", parse_mode="Markdown", reply_markup=admin_menu())
        try:
            bot.send_message(target_id, f"🚫 *You have been banned from this bot.*\n\n📝 Reason: _{reason or 'Not specified'}_", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid format.*\n\nUse: `USER_ID REASON`", parse_mode="Markdown", reply_markup=admin_menu())

def process_unban_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        target_id = int(message.text.strip())
        if not is_banned(target_id):
            bot.send_message(message.chat.id, "⚠️ *This user is not banned.*", parse_mode="Markdown", reply_markup=admin_menu())
            return
        unban_user(target_id)
        bot.send_message(message.chat.id, f"✅ *User unbanned.*\n\n👤 ID: `{target_id}`", parse_mode="Markdown", reply_markup=admin_menu())
        try:
            bot.send_message(target_id, "✅ *You have been unbanned.*\n\nSend /start to use the bot again.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid user ID.*", parse_mode="Markdown", reply_markup=admin_menu())

def process_reset_user(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        target_id = int(message.text.strip())
        if not get_user(target_id):
            bot.send_message(message.chat.id, "❌ *User not found.*", parse_mode="Markdown", reply_markup=admin_menu())
            return
        conn = get_db_connection()
        conn.execute("UPDATE users SET credits=0, surfshark_credits=0, referrals=0 WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *User reset.*\n\n👤 ID: `{target_id}`\n🪙 Credits: `0`\n🦈 Surfshark: `0`\n👥 Referrals: `0`", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid user ID.*", parse_mode="Markdown", reply_markup=admin_menu())

def process_surf_global_gift(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", reply_markup=admin_menu())
        return
    try:
        amount = int(message.text)
        conn = get_db_connection()
        conn.execute("UPDATE users SET surfshark_credits = surfshark_credits + ?", (amount,))
        conn.commit()
        users = conn.execute("SELECT user_id FROM users").fetchall()
        conn.close()
        bot.send_message(message.chat.id, f"✅ *Surfshark global gift applied.*\n\n🦈 Every user received `{amount}` Surfshark credits.", parse_mode="Markdown", reply_markup=admin_menu())
        success = 0
        for u in users:
            try:
                bot.send_message(u['user_id'], f"🦈 *Surfshark Gift*\n━━━━━━━━━━━━━━━━━━━━\n\nYou got `{amount}` free Surfshark credits.", parse_mode="Markdown")
                success += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.send_message(message.chat.id, f"✅ *Gift broadcast complete.*\n\n👥 Sent to `{success}` users.", parse_mode="Markdown")
    except Exception:
        bot.send_message(message.chat.id, "❌ *Invalid amount.*", parse_mode="Markdown", reply_markup=admin_menu())

def export_users_csv(message):
    conn = get_db_connection()
    users_list = conn.execute("SELECT user_id, username, credits, surfshark_credits, referrals, joined_date FROM users ORDER BY joined_date DESC").fetchall()
    conn.close()
    if not users_list:
        bot.send_message(message.chat.id, "📭 *No users to export.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    csv_text = "user_id,username,credits,surfshark_credits,referrals,joined_date\n"
    for u in users_list:
        csv_text += f"{u['user_id']},{u['username'] or ''},{u['credits']},{u['surfshark_credits']},{u['referrals']},{u['joined_date']}\n"
    file_obj = io.BytesIO(csv_text.encode('utf-8'))
    file_obj.name = "users_export.csv"
    bot.send_document(message.chat.id, file_obj, caption="📤 *Users Export Ready*", parse_mode="Markdown")

def normalize_cookies_json(cookies_json):
    data = json.loads(cookies_json)
    if isinstance(data, dict):
        raw_cookies = data.get("cookies", [])
    elif isinstance(data, list):
        raw_cookies = data
    else:
        raise ValueError("JSON must be a cookie array or a Playwright storage state object.")

    normalized = []
    for cookie in raw_cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if not name or value is None or not domain:
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        try:
            expires = float(expires) if expires is not None else None
        except (ValueError, TypeError):
            expires = None
        same_site_raw = str(cookie.get("sameSite", "Lax")).lower()
        same_site = "None" if "none" in same_site_raw or "no_restriction" in same_site_raw else ("Strict" if "strict" in same_site_raw else "Lax")
        item = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(cookie.get("path", "/")),
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", False)),
            "sameSite": same_site
        }
        if expires is not None:
            item["expires"] = expires
        normalized.append(item)
    if not normalized:
        raise ValueError("No valid cookies found.")
    return {"cookies": normalized, "origins": []}, len(normalized)

def read_cookies_from_message(message):
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        return bot.download_file(file_info.file_path).decode('utf-8')
    if message.text:
        return message.text
    raise ValueError("Send cookies as text or a file.")

def process_update_cookies(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        storage_state, count = normalize_cookies_json(read_cookies_from_message(message))
        with open(SURFSHARK_STORAGE, 'w', encoding='utf-8') as f:
            json.dump(storage_state, f, indent=2)
        close_surfshark_browser()
        bot.send_message(message.chat.id, f"✅ *Cookies updated.*\n\n🍪 Normalized `{count}` cookies.\n📂 Saved to `storage_state.json`.", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ *Failed to update cookies:* `{short_error(e)}`", parse_mode="Markdown", reply_markup=admin_menu())

def process_add_surf_cookie(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        label = message.document.file_name if message.document else f"Surf Account {int(time.time())}"
        cookies_json = read_cookies_from_message(message)
        if message.document and message.caption:
            label = message.caption.strip()
        elif not message.document:
            lines = cookies_json.strip().split("\n", 1)
            if len(lines) == 2 and not lines[0].lstrip().startswith(("[", "{")):
                label, cookies_json = lines[0].strip(), lines[1].strip()
        storage_state, count = normalize_cookies_json(cookies_json)
        conn = get_db_connection()
        conn.execute("INSERT INTO surfshark_cookies (name, cookies, status) VALUES (?, ?, 'active')", (label, json.dumps(storage_state)))
        conn.commit()
        conn.close()
        close_surfshark_browser()
        bot.send_message(message.chat.id, f"✅ *Surfshark cookie stored.*\n\n🏷 Label: `{label}`\n🍪 Cookies: `{count}`", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ *Failed to add Surfshark cookies:* `{short_error(e)}`", parse_mode="Markdown", reply_markup=admin_menu())

def view_surf_cookies(message):
    conn = get_db_connection()
    cookies = conn.execute("SELECT * FROM surfshark_cookies").fetchall()
    conn.close()
    if not cookies:
        bot.send_message(message.chat.id, "📭 *No Surfshark cookies saved yet.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    res = "📋 *Stored Surfshark Cookies*\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for c in cookies:
        status_icon = "🟢" if c['status'] == 'active' else "🔴"
        res += f"🆔 `{c['id']}` | {status_icon} *{c['name']}* ({c['status']})\n📅 Added: `{c['added_date']}`\n\n"
    bot.send_message(message.chat.id, res, parse_mode="Markdown", reply_markup=admin_menu())

def process_delete_surf_cookie(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        cookie_id = int(message.text.strip())
        conn = get_db_connection()
        row = conn.execute("SELECT name FROM surfshark_cookies WHERE id=?", (cookie_id,)).fetchone()
        if not row:
            conn.close()
            bot.send_message(message.chat.id, "❌ *Cookie ID not found.*", parse_mode="Markdown", reply_markup=admin_menu())
            return
        conn.execute("DELETE FROM surfshark_cookies WHERE id=?", (cookie_id,))
        conn.commit()
        conn.close()
        close_surfshark_browser()
        bot.send_message(message.chat.id, f"🗑 *Surfshark cookie deleted.*\n\n🆔 ID: `{cookie_id}`\n🏷 Label: `{row['name']}`", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ *Error deleting cookie:* `{short_error(e)}`", parse_mode="Markdown", reply_markup=admin_menu())

def process_update_canva_cookies(message):
    if message.text and message.text in ["❌ Cancel", "Cancel"]:
        bot.send_message(message.chat.id, "❌ *Action cancelled.*", parse_mode="Markdown", reply_markup=admin_menu())
        return
    try:
        cookies_json = read_cookies_from_message(message)
        json.loads(cookies_json)
        with open(CANVA_STORAGE, 'w', encoding='utf-8') as f:
            f.write(cookies_json)
        close_canva_browser()
        bot.send_message(message.chat.id, "✅ *Canva cookies updated.*\n\n🍪 `canva_storage_state.json` has been saved.", parse_mode="Markdown", reply_markup=admin_menu())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ *Failed to save Canva cookies:* `{short_error(e)}`", parse_mode="Markdown", reply_markup=admin_menu())

if __name__ == "__main__":
    print("Bot is running...")
    warm_surfshark_browser()
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
