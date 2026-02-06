import json
import sqlite3
import os
import random
import string
import time
import asyncio
import copy
import uuid
import re
import urllib.parse
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from manual_scraper import ManualScraper
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skipping .env load
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeChatAdministrators, WebAppInfo, __version__ as ptb_version
from telegram.error import ChatMigrated
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID") or 0)
WEBAPP_URL = os.getenv("WEBAPP_URL")

# Chadsflooring credentials for API access
CHADS_USERNAME = os.getenv("CHADS_USERNAME")
CHADS_PASSWORD = os.getenv("CHADS_PASSWORD")
CHADS_COOKIE = os.getenv("CHADS_COOKIE")

# Auto-configure WEBAPP_URL for Railway if not set manually
if not WEBAPP_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    WEBAPP_URL = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}/webapp.html"

REVIEW_CHANNEL_ID = os.getenv("REVIEW_CHANNEL_ID")
REVIEW_TOPIC_ID = int(os.getenv("REVIEW_TOPIC_ID") or 0)
DB_FILE = os.getenv("DB_FILE", "bot_database.db")
TICKET_TIMEOUT = 24 * 60 * 60  # 24 hours in seconds
REFERRAL_CHAT_ID = -1003786439934
REFERRAL_TOPIC_ID = 575
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS") or 15)
DELETE_TIMEOUT = 14 * 24 * 60 * 60 # 2 weeks (Hard limit for open inactive tickets)

# ===== GLOBAL STATE =====
# Data structure:
# {
#   "tickets": {
#       "user_id_str": {
#           "id": "ABCDEF-1",
#           "section": "support",
#           "created_at": timestamp,
#           "last_activity": timestamp
#       }
#   },
#   "user_started": [list of ids],
#   "counter": 0
# }

DEFAULT_CONFIG = {
    "texts": {
        "welcome": "👋 Hi! Thanks for reaching out to GeekdHouse Support Bot.\n\nWe want to help you as best as we can.\n\nPlease create one ticket per user at a time.\n\nChoose an option from the menu below:",
        "ticket_created": "✅ Your ticket has been created! 🎉\n\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀\n\nUse /mytickets to view your ticket.",
        "service_closed": "⛔ Sorry, this service is currently closed. Please choose another option."
    },
    "menu": [
        {
            "id": "shop_webapp",
            "name": "🛍️ Open Shop",
            "type": "web_app",
            "visible": True
        },
        {
            "id": "create_order",
            "name": "🛒 Create an Order",
            "type": "category",
            "visible": True,
            "message": "✅ You selected: 🛒 Create Order\n\n👇 Next, please choose from the options below:",
            "items": [
                {"id": "order_singles", "name": "📦 Singles (1-5 pieces)", "type": "service", "status": True, "visible": True, "response_message": "✅ You have chosen {service_name}\nYour ticket has been created! 🎉\n\nPlease have your order ready!\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀"},
                {"id": "order_bulk", "name": "🚛 Bulk (10+ pieces SHIPPED)", "type": "service", "status": True, "visible": True, "response_message": "✅ You have chosen {service_name}\nYour ticket has been created! 🎉\n\nPlease have your order ready!\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀"}
            ]
        },
        {
            "id": "support",
            "name": "❓ Support",
            "type": "service",
            "status": True,
            "visible": True
        }
    ]
}

# Global Config Cache (Menu and Texts are kept in memory for speed, Tickets/Users in DB)
global_config = copy.deepcopy(DEFAULT_CONFIG)

# ===== PERSISTENCE HELPERS =====
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Tickets Table
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        section TEXT,
        status TEXT DEFAULT 'Created',
        created_at REAL,
        last_activity REAL,
        closed INTEGER DEFAULT 0
    )''')
    # Users Table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        banned INTEGER DEFAULT 0,
        started INTEGER DEFAULT 0
    )''')
    # Referrals Table
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        code TEXT PRIMARY KEY,
        user_id INTEGER,
        created_at REAL
    )''')
    # Config Table (Key-Value)
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    conn.commit()
    
    # Migration for points in users table
    try:
        c.execute("ALTER TABLE users ADD COLUMN points INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass # Column likely exists
    
    # Migration for referral_code in tickets table
    try:
        c.execute("ALTER TABLE tickets ADD COLUMN referral_code TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass # Column likely exists

    # Migration for prompt tracking (inactivity)
    try:
        c.execute("ALTER TABLE tickets ADD COLUMN last_prompt_at REAL")
        c.execute("ALTER TABLE tickets ADD COLUMN snooze_until REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    return conn

def migrate_json_to_db(conn):
    json_file = "bot_data.json"
    if os.path.exists(json_file):
        print("📦 Found bot_data.json, migrating to SQLite...")
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
            
            c = conn.cursor()
            
            # Check if migration was already done to prevent overwriting persistent DB with old JSON from Git
            c.execute("SELECT value FROM config WHERE key = 'migration_done'")
            if c.fetchone():
                print("ℹ️ Migration already marked as done in DB. Skipping JSON import.")
                return
            
            # Safety check: If tickets table is not empty, skip migration to prevent overwrite
            c.execute("SELECT count(*) FROM tickets")
            if c.fetchone()[0] > 0:
                print("ℹ️ Tickets table is not empty. Skipping JSON migration to prevent overwrite.")
                c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("migration_done", "1"))
                conn.commit()
                return
            
            # Migrate Tickets
            tickets = data.get("tickets", {})
            for k, v in tickets.items():
                # Handle old format where key was user_id
                if k.isdigit():
                    t_id = v.get('id', f"OLD-{k}")
                    u_id = int(k)
                else:
                    t_id = v.get('id', k)
                    u_id = v.get('user_id', 0)
                
                c.execute("INSERT OR IGNORE INTO tickets (id, user_id, section, created_at, last_activity, closed) VALUES (?, ?, ?, ?, ?, ?)",
                          (t_id, u_id, v.get('section', 'Support'), v.get('created_at', time.time()), v.get('last_activity', time.time()), 0))
            
            # Migrate Users
            for uid in data.get("user_started", []):
                c.execute("INSERT OR IGNORE INTO users (user_id, started) VALUES (?, 1)", (uid,))
            
            # Migrate Config
            if "config" in data:
                c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("main_config", json.dumps(data["config"])))
            
            # Migrate Counter
            if "counter" in data:
                c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("ticket_counter", str(data["counter"])))
            
            # Mark migration as complete
            c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("migration_done", "1"))
            
            conn.commit()
            print("✅ Migration complete. Renaming bot_data.json to bot_data.json.bak")
            os.rename(json_file, json_file + ".bak")
        except Exception as e:
            print(f"❌ Migration failed: {e}")

def load_config():
    global global_config
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Load Config
    c.execute("SELECT value FROM config WHERE key = 'main_config'")
    row = c.fetchone()
    if row:
        loaded_conf = json.loads(row[0])
        # Merge with default to ensure new keys exist
        global_config.update(loaded_conf)
        # Deep merge texts
        for k, v in DEFAULT_CONFIG["texts"].items():
            if k not in global_config["texts"]:
                global_config["texts"][k] = v
    else:
        save_config() # Save defaults
    conn.close()

def save_config():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("main_config", json.dumps(global_config)))
    conn.commit()
    conn.close()

def get_next_counter():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'ticket_counter'")
    row = c.fetchone()
    count = int(row[0]) if row else 0
    count += 1
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("ticket_counter", str(count)))
    conn.commit()
    conn.close()
    return count

def generate_ticket_id():
    count = get_next_counter()
    
    # Generate suffix: 1-99, then A-Z, then AA-ZZ...
    if count < 100:
        suffix = str(count)
    else:
        def num_to_letters(n):
            res = ""
            while n > 0:
                n, remainder = divmod(n - 1, 26)
                res = chr(65 + remainder) + res
            return res
        suffix = num_to_letters(count - 99)

    random_chars = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{random_chars}-{suffix}"

# ===== DB HELPERS =====
def db_get_ticket(ticket_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_active_tickets(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE user_id = ? AND closed = 0 ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def db_create_ticket(ticket_id, user_id, section, referral_code=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO tickets (id, user_id, section, created_at, last_activity, referral_code) VALUES (?, ?, ?, ?, ?, ?)",
              (ticket_id, user_id, section, time.time(), time.time(), referral_code))
    conn.commit()
    conn.close()

def db_update_ticket_activity(ticket_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Update activity AND reset prompt timers so the 24h check starts over
    c.execute("UPDATE tickets SET last_activity = ?, last_prompt_at = NULL, snooze_until = NULL WHERE id = ?", (time.time(), ticket_id))
    conn.commit()
    conn.close()

def db_update_ticket_status(ticket_id, status):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
    conn.commit()
    conn.close()

def db_close_ticket(ticket_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tickets SET closed = 1 WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()

def db_is_user_banned(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row and row[0] == 1

def db_set_user_banned(user_id, banned):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, started) VALUES (?, 1)", (user_id,))
    c.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))
    conn.commit()
    conn.close()

def db_register_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, started) VALUES (?, 1)", (user_id,))
    conn.commit()
    conn.close()

def db_get_referral(code):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM referrals WHERE code = ?", (code,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def db_create_referral(code, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO referrals (code, user_id, created_at) VALUES (?, ?, ?)", (code, user_id, time.time()))
    conn.commit()
    conn.close()

def db_get_user_points(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def db_add_user_points(user_id, points):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Ensure user exists
    c.execute("INSERT OR IGNORE INTO users (user_id, started) VALUES (?, 1)", (user_id,))
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))
    conn.commit()
    conn.close()

def db_check_user_started(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT started FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

async def send_to_support_group(bot, text, photo=None, **kwargs):
    global SUPPORT_GROUP_ID
    if not SUPPORT_GROUP_ID:
        print(f"⚠️ Cannot send message to support group: ID is 0. Message: {text}")
        return
    try:
        if photo:
            await bot.send_photo(chat_id=SUPPORT_GROUP_ID, photo=photo, caption=text, **kwargs)
        else:
            await bot.send_message(chat_id=SUPPORT_GROUP_ID, text=text, **kwargs)
    except ChatMigrated as e:
        print(f"⚠️ Group upgraded to Supergroup. Updating SUPPORT_GROUP_ID to {e.new_chat_id}")
        SUPPORT_GROUP_ID = e.new_chat_id
        if photo:
            await bot.send_photo(chat_id=SUPPORT_GROUP_ID, photo=photo, caption=text, **kwargs)
        else:
            await bot.send_message(chat_id=SUPPORT_GROUP_ID, text=text, **kwargs)

async def handle_chat_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SUPPORT_GROUP_ID
    new_id = update.message.migrate_to_chat_id
    print(f"⚠️ Group upgraded to Supergroup (Event). Updating SUPPORT_GROUP_ID to {new_id}")
    SUPPORT_GROUP_ID = new_id

# ===== MENU HELPERS =====
def find_menu_item(menu, target_id):
    for i, item in enumerate(menu):
        if item['id'] == target_id:
            return item, menu, i
        if item.get('items'):
            found, parent, idx = find_menu_item(item['items'], target_id)
            if found:
                return found, parent, idx
    return None, None, None

# ===== ADMIN HELPERS =====
def get_admin_help_text(context: ContextTypes.DEFAULT_TYPE):
    # Check if in reply mode
    if context.user_data.get('reply_ticket_id'):
        cmds = ["/done", "/close", "/ticketstatus", "/ping", "/cancel", "/help"]
        header = "📝 <b>Reply Mode Commands:</b>"
    else:
        cmds = ["/reply", "/settings", "/status", "/block", "/unblock", "/help"]
        header = "🛠️ <b>Admin Commands:</b>"
    
    cmd_list = "\n".join(cmds)
    return f"\n\n{header}\n{cmd_list}"

# ===== WEBAPP HELPERS =====
def get_webapp_url(user_id, admin_mode=False):
    base_url = WEBAPP_URL
    if not base_url:
        return None
    
    # Get Settings
    settings = global_config.get("webapp_settings", {})
    settings_json = json.dumps(settings)
    settings_encoded = urllib.parse.quote(settings_json)
    
    is_admin = "1" if (admin_mode and user_id in ADMIN_IDS) else "0"
    
    return f"{base_url}?settings={settings_encoded}&admin={is_admin}"

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = global_config
    keyboard = []
    
    for item in config.get("menu", []):
        if item.get("visible", True):
            if item.get("type") == "web_app":
                url = get_webapp_url(update.effective_user.id)
                if url:
                    keyboard.append([InlineKeyboardButton(item["name"], web_app=WebAppInfo(url=url))])
            else:
                keyboard.append([InlineKeyboardButton(item["name"], callback_data=item["id"])])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = config["texts"]["welcome"]
    await update.message.reply_text(
        msg, 
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Don't answer yet, might be handled elsewhere if we didn't filter correctly, 
    # but here we assume it's a menu click.
    user = update.effective_user
    choice = query.data
    
    # Try to find the item in the menu
    item, _, _ = find_menu_item(global_config["menu"], choice)
    
    if not item:
        # Not a menu item (could be admin command handled by another handler, but if we got here, it wasn't handled)
        # Or it's a stale button.
        await query.answer("❌ Option not found.", show_alert=True)
        return

    await query.answer()
    db_register_user(user.id)
    
    if item["type"] == "category":
        keyboard = [
        ]
        for sub in item.get("items", []):
            if sub.get("visible", True):
                status_icon = ""
                if sub["type"] == "service":
                    status_icon = " 🟢" if sub.get("status", True) else " 🔴 (Closed)"
                
                if sub.get("type") == "web_app":
                    url = get_webapp_url(user.id)
                    if url:
                        keyboard.append([InlineKeyboardButton(f"{sub['name']}{status_icon}", web_app=WebAppInfo(url=url))])
                else:
                    keyboard.append([InlineKeyboardButton(f"{sub['name']}{status_icon}", callback_data=sub["id"])])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            item.get("message", "👇 Choose an option:"),
            reply_markup=reply_markup
        )
    elif item["type"] == "service":
        if not item.get("status", True):
            await query.answer(global_config["texts"]["service_closed"], show_alert=True)
            await query.message.reply_text(global_config["texts"]["service_closed"])
            return
        
        # Check if this service is under "Create an Order" category (id="create_order")
        requires_referral = False
        create_order_cat, _, _ = find_menu_item(global_config["menu"], "create_order")
        if create_order_cat and create_order_cat.get("items"):
            found, _, _ = find_menu_item(create_order_cat["items"], item["id"])
            if found:
                requires_referral = True

        if requires_referral:
            # Start Referral Check Flow instead of creating ticket immediately
            context.user_data['ticket_creation_state'] = {
                'section_name': item["name"],
                'response_message': item.get("response_message")
            }
            await query.message.edit_text("🔗 <b>Referral Code</b>\n\nDo you have a referral code from a friend?\n\nType the code below, or type <b>skip</b> to proceed.", parse_mode='HTML')
        else:
            await create_new_ticket(update, context, item["name"], item.get("response_message"))
    elif item["type"] == "auto_response":
        # Automated response, no ticket
        await query.message.reply_text(item.get("response_message", "ℹ️ Info"))

async def create_new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, section_name: str, custom_msg=None, referral_code=None):
    user = update.effective_user
    if db_is_user_banned(user.id):
        # Handle both callback and message updates
        if update.callback_query:
            await update.callback_query.message.reply_text("⛔ You are blocked from creating tickets.")
        else:
            await update.message.reply_text("⛔ You are blocked from creating tickets.")
        return

    ticket_id = generate_ticket_id()
    db_create_ticket(ticket_id, user.id, section_name, referral_code)
    
    # Handle Referral Logic
    referral_note = ""
    if referral_code:
        ref_data = db_get_referral(referral_code)
        if ref_data:
            creator_id = ref_data['user_id']
            # Log usage
            creator_display = f"ID {creator_id}"
            try:
                creator_info = await context.bot.get_chat(creator_id)
                creator_link = f'<a href="tg://user?id={creator_id}">{creator_info.first_name}</a>'
                if creator_info.username:
                    creator_display = f"{creator_link} (@{creator_info.username})"
                else:
                    creator_display = f'{creator_link} (<a href="tg://user?id={creator_id}">DM Link</a>)'
            except Exception as e:
                print(f"Could not fetch creator info for {creator_id}: {e}")
                creator_display = f"ID {creator_id}"
            
            referral_note = f"\n🔗 <b>Referral Used:</b> {referral_code} (By {creator_display})"
            
            user_display = user.mention_html()
            if user.username:
                user_display += f" (@{user.username})"
            else:
                user_display += f' (<a href="tg://user?id={user.id}">DM Link</a>)'
            # Log to Admin Topic (Exact format requested)
            log_msg = (
                f"Referral code Used!\n"
                f"Code: {referral_code}\n"
                f"Created by: {creator_display}\n"
                f"Used by: {user_display}"
            )
            try:
                await context.bot.send_message(chat_id=REFERRAL_CHAT_ID, message_thread_id=REFERRAL_TOPIC_ID, text=log_msg)
                await context.bot.send_message(chat_id=REFERRAL_CHAT_ID, message_thread_id=REFERRAL_TOPIC_ID, text=log_msg, parse_mode='HTML')
            except Exception as e:
                print(f"Failed to log referral usage: {e}")

    # Message to User
    if custom_msg:
        msg_text = custom_msg
    else:
        msg_text = global_config["texts"]["ticket_created"]
    
    msg_text = msg_text.replace("{ticket_id}", ticket_id).replace("{service_name}", section_name) if msg_text else "Ticket Created."
    
    if update.callback_query:
        await update.callback_query.message.edit_text(msg_text)
    else:
        await update.message.reply_text(msg_text)

    # Message to Admin Group
    keyboard = [[InlineKeyboardButton("Reply to Ticket ✍️", callback_data=f"reply_{ticket_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    user_display = user.mention_html()
    if user.username:
        user_display += f" (@{user.username})"
    else:
        user_display += f' (<a href="tg://user?id={user.id}">DM Link</a>)'
    await send_to_support_group(
        context.bot,
        text=f"🆕 <b>New Ticket Created!</b>\n"
             f"👤 User: {user_display} ({user.id})\n"
             f"🎫 Ticket ID: {ticket_id}\n"
             f"📂 Category: {section_name}{referral_note}",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except json.JSONDecodeError:
        return

    # Handle Admin Settings Save
    if data.get("action") == "save_settings":
        if user.id not in ADMIN_IDS:
            return
        
        new_settings = data.get("settings", {})
        global_config["webapp_settings"] = new_settings
        save_config()
        await update.message.reply_text("✅ <b>Shop Settings Saved!</b>\nChanges (hidden items, renames) are now live.", parse_mode='HTML')
        return

    if data.get("action") == "web_app_order":
        cart = data.get("cart", {})
        if not cart:
            return

        # Build Order Summary
        summary = "🛒 <b>New Web App Order</b>\n\n"
        total = 0.0
        for item in cart.values():
            line_total = item['price'] * item['qty']
            total += line_total
            summary += f"• {item['qty']}x {item['parentName']} ({item['name']}) - ${line_total:.2f}\n"
        
        summary += f"\n<b>Total: ${total:.2f}</b>"

        # Create Ticket
        ticket_id = generate_ticket_id()
        db_create_ticket(ticket_id, user.id, "Web App Order")
        
        # Notify User
        await update.message.reply_text(f"✅ Order received!\nTicket ID: {ticket_id}\n\n{summary}", parse_mode='HTML')
        
        # Notify Admin
        user_display = f"{user.first_name} (@{user.username})" if user.username else f"{user.first_name} ({user.id})"
        admin_msg = f"🆕 <b>Web App Order</b>\n👤 {user_display}\n🎫 Ticket: {ticket_id}\n\n{summary}"
        await send_to_support_group(context.bot, text=admin_msg, parse_mode='HTML')

# ===== DM HANDLER =====
async def handle_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or update.message.caption
    photo = update.message.photo[-1].file_id if update.message.photo else None

    # Ignore messages in the support group or any public group to prevent spam
    if update.effective_chat.id == SUPPORT_GROUP_ID or update.effective_chat.type != 'private':
        return

    # Check if user is banned
    if db_is_user_banned(user.id):
        return

    # Auto-start menu for first-time users
    if not db_check_user_started(user.id):
        db_register_user(user.id)
        await start(update, context)
        return

    # Check for Review State
    if 'review_state' in context.user_data:
        await handle_review_step(update, context)
        return

    # Check for Shipping Details State
    if 'ship_state' in context.user_data:
        await handle_shipping_step(update, context)
        return
        
    # Check for Ticket Creation (Referral) State
    if 'ticket_creation_state' in context.user_data:
        await handle_ticket_creation_step(update, context)
        return

    # Only handle message if user is in a session
    # Check if user has a selected active ticket in context, otherwise find latest
    active_tickets = db_get_active_tickets(user.id)
    
    if active_tickets:
        # Determine which ticket to reply to
        selected_ticket_id = context.user_data.get('current_ticket_id')
        ticket = None
        
        if selected_ticket_id:
            # Verify it's still active and belongs to user
            ticket = next((t for t in active_tickets if t['id'] == selected_ticket_id), None)
        
        if not ticket:
            # Default to latest
            ticket = active_tickets[0]
            context.user_data['current_ticket_id'] = ticket['id']
        
        # Update activity
        db_update_ticket_activity(ticket['id'])

        # Prepare content
        msg_content = f"📨 Message from ({user.id}) Ticket {ticket['id']}"
        if text:
            msg_content += f":\n{text}"

        # Forward message to admin group
        await send_to_support_group(
            context.bot,
            text=msg_content,
            photo=photo
        )
    else:
        # User hasn't selected anything yet
        await update.message.reply_text("❗ Please select an option from the menu to proceed! 📋")

async def handle_ticket_creation_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('ticket_creation_state')
    text = update.message.text.strip()
    
    referral_code = None
    
    if text.lower() != 'skip':
        # Validate Code
        ref = db_get_referral(text)
        if ref:
            if ref['user_id'] == update.effective_user.id:
                await update.message.reply_text("❌ You cannot use your own referral code. Type a different code or 'skip'.")
                return
            referral_code = text
            await update.message.reply_text("✅ Referral code applied!")
        else:
            await update.message.reply_text("❌ Invalid referral code. Please try again or type 'skip'.")
            return
            
    # Proceed to create ticket
    del context.user_data['ticket_creation_state']
    await create_new_ticket(update, context, state['section_name'], state['response_message'], referral_code)

# ===== MY REFERRALS COMMAND =====
async def myreferrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Admin Mode: /myreferrals <userid> addpoint/removepoint <amount>
    if user.id in ADMIN_IDS and context.args:
        if len(context.args) >= 3:
            try:
                target_id = int(context.args[0])
                action = context.args[1].lower()
                amount = int(context.args[2])
                
                if action == "addpoint":
                    db_add_user_points(target_id, amount)
                    await update.message.reply_text(f"✅ Added {amount} points to User {target_id}.")
                    try:
                        await context.bot.send_message(target_id, f"🎉 You have received {amount} referral points from an admin!")
                    except:
                        pass
                elif action == "removepoint":
                    db_add_user_points(target_id, -amount)
                    await update.message.reply_text(f"✅ Removed {amount} points from User {target_id}.")
                else:
                    await update.message.reply_text("Usage: /myreferrals <userid> addpoint/removepoint <amount>")
            except ValueError:
                await update.message.reply_text("Invalid format. Usage: /myreferrals <userid> addpoint <amount>")
        else:
            await update.message.reply_text("Usage: /myreferrals <userid> addpoint <amount>")
        return

    # User Mode
    points = db_get_user_points(user.id)
    ref_data = None
    # Find user's referral code
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT code FROM referrals WHERE user_id = ?", (user.id,))
    row = c.fetchone()
    conn.close()
    
    code_msg = f"Your Referral Code: <code>{row[0]}</code>" if row else "You don't have a referral code yet. Use /refer to generate one!"
    
    msg = (
        f"🏆 <b>My Referrals</b>\n\n"
        f"💰 Current Points: <b>{points}</b>\n\n"
        f"{code_msg}\n\n"
        f"<i>Share your code to earn more points!</i>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# ===== MY TICKETS COMMAND =====
async def mytickets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tickets = db_get_active_tickets(user.id)
    
    if not tickets:
        await update.message.reply_text("📭 You have no active tickets.")
        return

    keyboard = []
    for t in tickets:
        status = t.get('status', 'Created')
        keyboard.append([InlineKeyboardButton(f"Ticket {t['id']} - {status}", callback_data=f"sel_ticket_{t['id']}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🗂 <b>My Active Tickets</b>\nSelect a ticket to view/reply:", reply_markup=reply_markup, parse_mode='HTML')

async def handle_myticket_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ticket_id = query.data.replace("sel_ticket_", "")
    
    ticket = db_get_ticket(ticket_id)
    if not ticket or ticket['closed']:
        await query.message.edit_text("❌ This ticket is closed or invalid.")
        return
    
    context.user_data['current_ticket_id'] = ticket_id
    status = ticket.get('status', 'Created')
    section = ticket.get('section', 'Unknown')
    
    await query.message.edit_text(
        f"🎫 <b>Current Ticket: {ticket_id}</b>\n"
        f"📂 Section: {section}\n"
        f"📊 Status: {status}\n\n"
        f"👇 Any messages you send now will be sent to this ticket.",
        parse_mode='HTML'
    )

# ===== BLOCK/UNBLOCK COMMANDS =====
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args:
        await update.message.reply_text("Usage: /block <user_id>")
        return
    try:
        uid = int(context.args[0])
        db_set_user_banned(uid, True)
        await update.message.reply_text(f"⛔ User {uid} has been blocked.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <user_id>")
        return
    try:
        uid = int(context.args[0])
        db_set_user_banned(uid, False)
        await update.message.reply_text(f"✅ User {uid} has been unblocked.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

# ===== PING COMMAND =====
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    ticket_id = context.user_data.get('reply_ticket_id')
    if not ticket_id:
        await update.message.reply_text("❗ You must be replying to a ticket to ping the user.")
        return
    
    ticket = db_get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("❌ Ticket not found.")
        return
        
    try:
        await context.bot.send_message(chat_id=ticket['user_id'], text="🔔 <b>You have been pinged by the staff!</b>", parse_mode='HTML')
        await update.message.reply_text(f"✅ Ping sent to User {ticket['user_id']}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to ping: {e}")

# ===== HELP COMMAND =====
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        msg = get_admin_help_text(context)
    else:
        msg = (
            "📚 <b>Available Commands:</b>\n\n"
            "/start - Show the main menu\n"
            "/mytickets - View your active tickets\n"
            "/close - Close current ticket\n"
            "/review - Leave a review"
        )
    await update.message.reply_text(msg, parse_mode='HTML')

# ===== REPLY COMMAND (ADMIN ONLY) =====
async def handle_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return  # ignore non-admins

    # Allow direct reply: /reply <user_id> <message>
    if context.args:
        try:
            target_id = int(context.args[0])
            message = " ".join(context.args[1:])
            if not message:
                await update.message.reply_text("❗ Usage: /reply <user_id> <message>")
                return
            
            await context.bot.send_message(chat_id=target_id, text=f"💬 Staff: {message}")
            await update.message.reply_text(f"✅ Message sent to {target_id}!")
            return
        except ValueError:
            await update.message.reply_text("❗ Invalid User ID.")
            return

    # Get all open tickets from DB
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE closed = 0 ORDER BY last_activity DESC LIMIT 20") # Limit to avoid huge lists
    tickets = c.fetchall()
    conn.close()

    if not tickets:
        await update.message.reply_text("📭 No open tickets right now.")
        return

    # Build inline keyboard of open tickets
    keyboard = []
    for t in tickets:
        tid = t['id']
        section = t['section']
        keyboard.append([
            InlineKeyboardButton(f"Ticket {tid} ({section})", callback_data=f"reply_{tid}"),
            InlineKeyboardButton("Ping 🔔", callback_data=f"ping_{tid}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👇 Select a ticket to reply to:", reply_markup=reply_markup)

async def handle_reply_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_user = update.effective_user

    if admin_user.id not in ADMIN_IDS:
        return

    # Extract ticket_id from callback_data
    ticket_id = query.data.split("_")[1]
    context.user_data['reply_ticket_id'] = ticket_id
    
    # Get ticket info for display
    ticket = db_get_ticket(ticket_id)
    ticket_display = ticket['id'] if ticket else ticket_id
    target_user_id = ticket['user_id'] if ticket else "Unknown"
    
    help_text = get_admin_help_text(context)
    
    if update.effective_chat.type == 'private':
        await query.message.edit_text(f"✏️ Now reply to Ticket {ticket_display} (User {target_user_id}).\nType your message here:{help_text}", parse_mode='HTML')
    else:
        # In group chat, just notify via alert and don't edit the ticket message
        await query.answer(f"✏️ You are now replying to Ticket {ticket_display}", show_alert=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"ℹ️ {admin_user.first_name} is now replying to Ticket {ticket_display}.")

async def handle_ping_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id not in ADMIN_IDS:
        return

    # Extract ticket_id from callback_data
    ticket_id = query.data.split("_")[1]
    ticket = db_get_ticket(ticket_id)
    
    if ticket:
        target_id = ticket['user_id']
        try:
            await context.bot.send_message(chat_id=target_id, text="❗ You are currently being pinged by the staff!")
            await query.message.reply_text(f"✅ Ping sent to User {target_id} (Ticket {ticket_id})!")
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to ping {target_id}: {e}")
    else:
        await query.message.reply_text("❌ Ticket not found.")

async def stop_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    # Clear reply state
    if context.user_data.pop('reply_ticket_id', None):
        # Notify group only
        await send_to_support_group(
            context.bot,
            text=f"ℹ️ Admin {user.first_name} disconnected from the ticket."
        )
    
    # Show general commands
    help_text = get_admin_help_text(context)
    await update.message.reply_text(f"✅ Disconnected from ticket.{help_text}", parse_mode='HTML')

async def close_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Check if Admin
    if user.id in ADMIN_IDS:
        ticket_id = context.user_data.get('reply_ticket_id')
        if not ticket_id:
            await update.message.reply_text("❗ You must be replying to a ticket to close it. Use /reply first.")
            return
        
        # Close the ticket
        ticket = db_get_ticket(ticket_id)
        if ticket and not ticket['closed']:
            target_user_id = ticket['user_id']
            db_close_ticket(ticket_id)
            
            # Notify User
            try:
                await context.bot.send_message(chat_id=target_user_id, text=f"🔒 Ticket {ticket_id} has been closed.")
            except:
                pass
            
            # Notify Admin/Group
            await send_to_support_group(context.bot, text=f"🔒 Ticket {ticket_id} closed by admin.")
            context.user_data.pop('reply_ticket_id', None)
            
            help_text = get_admin_help_text(context)
            await update.message.reply_text(f"✅ Ticket closed.{help_text}", parse_mode='HTML')
        else:
            await update.message.reply_text("❗ Ticket already closed or not found.")
            
    else:
        # User closing their own ticket
        # Find active ticket(s) for user
        user_tickets = db_get_active_tickets(user.id)
        if user_tickets:
            # Close all or just latest? Usually users want to close "the" session.
            # Let's close the latest one.
            ticket = user_tickets[0]
            ticket_id = ticket['id']
            
            db_close_ticket(ticket_id)

            await update.message.reply_text(f"🔒 Ticket {ticket_id} has been closed.")
            await send_to_support_group(
                context.bot,
                text=f"🔒 Ticket {ticket_id} closed by user {user.first_name}."
            )
        else:
            # If user has no ticket, ignore or inform
            await update.message.reply_text("❗ You do not have an open ticket.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    cancelled = False
    if context.user_data.get('editing_text'):
        del context.user_data['editing_text']
        cancelled = True
    
    if context.user_data.get('admin_state'):
        del context.user_data['admin_state']
        cancelled = True
        
    if cancelled:
        await update.message.reply_text("🚫 Action cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active action to cancel.")

# ===== TICKET INFO COMMAND =====
async def ticketinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    ticket_id = context.user_data.get('reply_ticket_id')
    if not ticket_id:
        await update.message.reply_text("❗ You must be replying to a ticket to view info.")
        return
    
    ticket = db_get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("❌ Ticket not found.")
        return

    # Format timestamps
    created_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ticket['created_at']))
    last_act_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ticket['last_activity']))
    
    # User details
    user_id = ticket['user_id']
    user_name = f"Unknown (ID: {user_id})"
    try:
        chat_member = await context.bot.get_chat(user_id)
        user_link = f'<a href="tg://user?id={user_id}">{chat_member.first_name}</a>'
        if chat_member.username:
            user_name = f"{user_link} (@{chat_member.username})"
        else:
            user_name = f'{user_link} (<a href="tg://user?id={user_id}">DM Link</a>)'
    except Exception:
        pass

    points = db_get_user_points(user_id)
    referral = ticket.get('referral_code') or "None"
    
    info_text = (
        f"📋 <b>Ticket Information</b>\n"
        f"-----------------------------\n"
        f"🎫 <b>Ticket ID:</b> {ticket['id']}\n"
        f"👤 <b>User:</b> {user_name}\n"
        f"🆔 <b>User ID:</b> {user_id}\n"
        f"💰 <b>User Points:</b> {points}\n"
        f"📂 <b>Section:</b> {ticket['section']}\n"
        f"📊 <b>Status:</b> {ticket['status']}\n"
        f"🔗 <b>Referral Code Used:</b> {referral}\n"
        f"📅 <b>Created:</b> {created_str}\n"
        f"⏱ <b>Last Activity:</b> {last_act_str}\n"
        f"🔒 <b>Closed:</b> {'Yes' if ticket['closed'] else 'No'}"
    )
    
    await update.message.reply_text(info_text, parse_mode='HTML')

# ===== TICKET STATUS COMMAND =====
async def ticket_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    ticket_id = context.user_data.get('reply_ticket_id')
    if not ticket_id:
        await update.message.reply_text("❗ You must be replying to a ticket to change status.")
        return
    
    valid_options = ["accepted", "shipdetails", "paid", "package", "shipped", "delivered", "complete"]
    
    if not context.args:
        options_str = ", ".join(valid_options)
        await update.message.reply_text(f"Usage: /ticketstatus <status>\nOptions: {options_str}")
        return
    
    status_key = context.args[0].lower()
    ticket = db_get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("❌ Ticket not found.")
        return

    user_id = ticket['user_id']
    
    # Status Logic
    if status_key == "accepted":
        new_status = "Order Accepted"
        msg = "✅ Your order has been accepted!"
    elif status_key == "paid":
        new_status = "Order Paid"
        msg = "💰 Payment received! Your order is marked as paid."
    elif status_key == "package":
        new_status = "Packaging Order"
        msg = "📦 We are currently packaging your order."
    elif status_key == "shipped":
        # Special Flow
        context.user_data['admin_state'] = {'action': 'waiting_tracking', 'ticket_id': ticket_id}
        await update.message.reply_text("🚚 <b>Shipping Order</b>\nPlease enter the USPS tracking code:", parse_mode='HTML')
        return
    elif status_key == "delivered":
        # Bulk Only Check
        if "bulk" not in ticket['section'].lower():
            await update.message.reply_text("⚠️ Warning: This ticket does not seem to be Bulk. Proceeding anyway.")
        
        # Award Point if Referral
        if ticket.get('referral_code'):
            ref_code = ticket['referral_code']
            ref_data = db_get_referral(ref_code)
            if ref_data:
                referrer_id = ref_data['user_id']
                db_add_user_points(referrer_id, 1)
                try:
                    await context.bot.send_message(chat_id=referrer_id, text=f"🎉 <b>Referral Bonus!</b>\n\nA user you referred has completed an order! You have received 1 referral point.\nUse /myreferrals to check your balance.", parse_mode='HTML')
                except Exception as e:
                    print(f"Failed to notify referrer {referrer_id}: {e}")

        new_status = "Order Delivered"
        db_update_ticket_status(ticket_id, new_status)
        db_close_ticket(ticket_id)
        
        final_msg = "🎉 <b>Order Delivered!</b>\n\nThank you for shopping with GeekdHouse! We hope you had a great experience and we hope to see you back soon! Use /review your order and leave a review! Any feedback is appreciated!\n\nAdditionally, use /refer to generate a referral code that gets you future discounts on your orders!"
        await context.bot.send_message(chat_id=user_id, text=final_msg, parse_mode='HTML')
        await update.message.reply_text(f"✅ Status updated to Delivered. Ticket closed.")
        return
    elif status_key == "complete":
        # Singles Check
        if "singles" not in ticket['section'].lower():
            await update.message.reply_text("⚠️ Warning: This ticket does not seem to be Singles. Proceeding anyway.")
            
        # Award Point if Referral
        if ticket.get('referral_code'):
            ref_code = ticket['referral_code']
            ref_data = db_get_referral(ref_code)
            if ref_data:
                referrer_id = ref_data['user_id']
                db_add_user_points(referrer_id, 1)
                try:
                    await context.bot.send_message(chat_id=referrer_id, text=f"🎉 <b>Referral Bonus!</b>\n\nA user you referred has completed an order! You have received 1 referral point.\nUse /myreferrals to check your balance.", parse_mode='HTML')
                except Exception as e:
                    print(f"Failed to notify referrer {referrer_id}: {e}")

        new_status = "Order Complete"
        db_update_ticket_status(ticket_id, new_status)
        db_close_ticket(ticket_id)
        
        final_msg = "🎉 <b>Order Complete!</b>\n\nThank you for shopping with GeekdHouse! We hope you had a great experience and we hope to see you back soon! Use /review your order and leave a review! Any feedback is appreciated!\n\nAdditionally, use /refer to generate a referral code that gets you future discounts on your orders!"
        await context.bot.send_message(chat_id=user_id, text=final_msg, parse_mode='HTML')
        await update.message.reply_text(f"✅ Status updated to Complete. Ticket closed.")
        return
    elif status_key == "shipdetails":
        # Bulk Only Check
        if "bulk" not in ticket['section'].lower():
            await update.message.reply_text("⚠️ Warning: This ticket does not seem to be Bulk. Proceeding anyway.")
        
        # Send options to user
        keyboard = [
            [InlineKeyboardButton("📦 Ship to Me", callback_data=f"ship_opt_ship_{ticket_id}")],
            [InlineKeyboardButton("🏃 Pick Up from Staff", callback_data=f"ship_opt_pickup_{ticket_id}")]
        ]
        try:
            await context.bot.send_message(chat_id=user_id, text="🚚 <b>Shipping Options</b>\n\nHow would you like to receive your order?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            await update.message.reply_text(f"✅ Sent shipping options to User {user_id}.")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send to user: {e}")
        return
    else:
        await update.message.reply_text("❌ Unknown status.")
        return

    db_update_ticket_status(ticket_id, new_status)
    await context.bot.send_message(chat_id=user_id, text=f"ℹ️ Status Update: <b>{new_status}</b>\n{msg}", parse_mode='HTML')
    await update.message.reply_text(f"✅ Status updated to: {new_status}")

# ===== REVIEW SYSTEM =====
async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Start review flow
    keyboard = [
        [InlineKeyboardButton("1 ⭐", callback_data="rev_star_1"), InlineKeyboardButton("2 ⭐", callback_data="rev_star_2"), InlineKeyboardButton("3 ⭐", callback_data="rev_star_3"), InlineKeyboardButton("4 ⭐", callback_data="rev_star_4"), InlineKeyboardButton("5 ⭐", callback_data="rev_star_5")],
        [InlineKeyboardButton("1.5", callback_data="rev_star_1.5"), InlineKeyboardButton("2.5", callback_data="rev_star_2.5"), InlineKeyboardButton("3.5", callback_data="rev_star_3.5"), InlineKeyboardButton("4.5", callback_data="rev_star_4.5")]
    ]
    await update.message.reply_text("🌟 <b>Leave a Review</b>\n\nPlease rate your experience:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    context.user_data['review_state'] = {'step': 'stars', 'data': {}}

async def handle_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not context.user_data.get('review_state'):
        await query.message.edit_text("❌ Review session expired. Type /review again.")
        return

    if query.data.startswith("rev_star_"):
        stars = query.data.replace("rev_star_", "")
        context.user_data['review_state']['data']['stars'] = stars
        context.user_data['review_state']['step'] = 'text'
        await query.message.edit_text(f"⭐ You selected <b>{stars} Stars</b>.\n\n✍️ Now, please write your review of the order:", parse_mode='HTML')

async def handle_review_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('review_state')
    step = state['step']
    data = state['data']
    user = update.effective_user
    
    if step == 'text':
        data['text'] = update.message.text
        state['step'] = 'photos'
        data['photos'] = []
        await update.message.reply_text("📸 <b>Upload Photos</b>\n\nPlease upload photos of your order.\nType <b>done</b> when finished, or <b>skip</b> to skip photos.", parse_mode='HTML')
        return

    if step == 'photos':
        msg_text = (update.message.text or "").lower()
        
        if msg_text == 'skip' or msg_text == 'done':
            # Finish Review
            stars = data['stars']
            review_text = data['text']
            photos = data['photos']
            
            # Send to Admin
            user_display = user.mention_html()
            if user.username:
                user_display += f" (@{user.username})"
            else:
                user_display += f' (<a href="tg://user?id={user.id}">DM Link</a>)'
            admin_text = (
                f"🌟 <b>New Review!</b>\n"
                f"👤 User: {user.first_name} (@{user.username})\n"
                f"👤 User: {user_display}\n"
                f"⭐ Rating: {stars}/5\n"
                f"💬 Review: {review_text}"
            )
            
            # Determine target chat and thread
            target_chat_id = SUPPORT_GROUP_ID
            target_thread_id = None

            if REVIEW_CHANNEL_ID:
                target_chat_id = REVIEW_CHANNEL_ID
            elif REVIEW_TOPIC_ID:
                target_thread_id = REVIEW_TOPIC_ID
            
            try:
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=target_thread_id, text=admin_text, parse_mode='HTML')
            
                # Send photos separately
                if photos:
                    for photo_id in photos:
                        await context.bot.send_photo(chat_id=target_chat_id, message_thread_id=target_thread_id, photo=photo_id, caption="📷 Review Photo")
            except Exception as e:
                print(f"❌ Failed to send review to {target_chat_id} (Topic {target_thread_id}): {e}")
            
            await update.message.reply_text("✅ <b>Thank you for your review!</b>", parse_mode='HTML')
            del context.user_data['review_state']
            return
        
        if update.message.photo:
            photo_id = update.message.photo[-1].file_id
            data['photos'].append(photo_id)
            await update.message.reply_text(f"✅ Photo received ({len(data['photos'])} total). Send more or type <b>done</b>.", parse_mode='HTML')
        else:
            await update.message.reply_text("❗ Please send a photo or type 'done'/'skip'.")

# ===== SHIPPING DETAILS FLOW =====
async def handle_shipping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    
    # Format: ship_opt_pickup_{ticket_id} OR ship_meth_std_{ticket_id}
    parts = data.split("_", 3)
    action = parts[1] # opt or meth
    sub_action = parts[2] # pickup, ship, std, prio
    ticket_id = parts[3]

    if action == "opt":
        if sub_action == "pickup":
            # Start Pickup Flow (Skip name/address, go to method)
            context.user_data['ship_state'] = {'step': 'method', 'ticket_id': ticket_id, 'data': {'type': 'pickup'}}
            keyboard = [
                [InlineKeyboardButton("Standard ($20, 3-7 days)", callback_data=f"ship_meth_std_{ticket_id}")],
                [InlineKeyboardButton("Priority ($35, 2-4 days)", callback_data=f"ship_meth_prio_{ticket_id}")]
            ]
            await query.message.edit_text("🏃 <b>Pickup Selected</b>\n\nPlease choose a processing speed:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
        elif sub_action == "ship":
            # Start Form
            context.user_data['ship_state'] = {'step': 'name', 'ticket_id': ticket_id, 'data': {'type': 'ship'}}
            await query.message.edit_text("📝 <b>Shipping Details</b>\n\nPlease enter your <b>Full Name</b>:", parse_mode='HTML')

    elif action == "meth":
        # Final Step
        state = context.user_data.get('ship_state')
        if not state or state['ticket_id'] != ticket_id:
            await query.message.edit_text("❌ Session expired.")
            return
        
        method_name = "Standard Shipping ($20, 3-7 days)" if sub_action == "std" else "Priority Shipping ($35, 2-4 days)"
        state['data']['method'] = method_name
        
        # Compile Info
        d = state['data']
        ship_type = d.get('type', 'ship')
        
        user_display = user.mention_html()
        if user.username:
            user_display += f" (@{user.username})"
        else:
            user_display += f' (<a href="tg://user?id={user.id}">DM Link</a>)'
        
        if ship_type == 'pickup':
            summary = (
                f"🏃 <b>Pickup Request</b>\n"
                f"🎫 Ticket: {ticket_id}\n"
                f"👤 User: {user_display}\n\n"
                f"⚡ Speed: {method_name}"
            )
            msg_user = "✅ <b>Thank you!</b>\n\nYour pickup request has been sent to the staff."
        else:
            summary = (
                f"📦 <b>Shipping Details Received</b>\n"
                f"🎫 Ticket: {ticket_id}\n"
                f"👤 User: {user_display}\n\n"
                f"📛 Name: {d.get('name')}\n"
                f"🏠 Address: {d.get('address')}\n"
                f"🚚 Method: {method_name}"
            )
            msg_user = "✅ <b>Thank you!</b>\n\nYour shipping details have been sent to the staff."
        
        await send_to_support_group(context.bot, text=summary, parse_mode='HTML')
        await query.message.edit_text(msg_user, parse_mode='HTML')
        
        # Clear state
        del context.user_data['ship_state']

async def handle_shipping_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('ship_state')
    step = state['step']
    ticket_id = state['ticket_id']
    text = update.message.text
    
    if step == 'name':
        state['data']['name'] = text
        state['step'] = 'address'
        await update.message.reply_text("✅ Name saved.\n\n📍 Now, please enter your <b>Full Shipping Address</b>:", parse_mode='HTML')
    
    elif step == 'address':
        # Address Validation
        # Format: Street, City, State Zip
        if not re.search(r"\d+\s+.+,\s*.+,\s*[A-Za-z]{2}\s+\d{5}", text):
            await update.message.reply_text(
                "❌ <b>Invalid Address Format</b>\n\n"
                "Please use the format:\n"
                "<code>Street Address, City, State ZipCode</code>\n\n"
                "Example: <i>123 Main St, New York, NY 10001</i>\n\n"
                "Please try again:",
                parse_mode='HTML'
            )
            return
            
        state['data']['address'] = text
        state['step'] = 'method'
        
        # Show Buttons
        keyboard = [
            [InlineKeyboardButton("Standard ($20, 3-7 days)", callback_data=f"ship_meth_std_{ticket_id}")],
            [InlineKeyboardButton("Priority ($35, 2-4 days)", callback_data=f"ship_meth_prio_{ticket_id}")]
        ]
        await update.message.reply_text("✅ Address saved.\n\n🚚 Please choose a shipping method:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===== REFERRAL SYSTEM =====
async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "- GEEKDHOUSE REFERRALS -\n\n"
        "Generate a referral code unique to you that you can share with your friends.\n\n"
        "Each time someone successfully orders using your referral code, you will gain referral points. "
        "Each point is equivelant to $5 off your order. You are limited to using 2 points per order, "
        "unless your order is $200+ in which case you can use 3 points. your points are stored and "
        "remembered for as long as you are a customer with us!\n\n"
        "Please note that anyone who is referred will also have to be a member of the main channel.\n\n"
        "Do you agree to the terms and wish to generate a referral link?"
    )
    
    keyboard = [
        [InlineKeyboardButton("Yes", callback_data="refer_yes"), InlineKeyboardButton("No", callback_data="refer_no")]
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_referral_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    
    if data == "refer_no":
        await query.message.edit_text("Referral cancelled, have a good day!")
        return
    
    if data == "refer_yes":
        # Generate Code
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        # Ensure uniqueness
        while db_get_referral(code):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        db_create_referral(code, user.id)
        
        # Log to Admin Topic (Exact format requested)
        user_display = user.mention_html()
        if user.username:
            user_display += f" (@{user.username})"
        else:
            user_display += f' (<a href="tg://user?id={user.id}">DM Link</a>)'
        log_msg = (
            f"Referral code created!\n"
            f"Code: {code}\n"
            f"User info: {user.first_name} (@{user.username}) ID: {user.id}"
            f"User info: {user_display} ID: {user.id}"
        )
        try:
            await context.bot.send_message(chat_id=REFERRAL_CHAT_ID, message_thread_id=REFERRAL_TOPIC_ID, text=log_msg)
            await context.bot.send_message(chat_id=REFERRAL_CHAT_ID, message_thread_id=REFERRAL_TOPIC_ID, text=log_msg, parse_mode='HTML')
        except Exception as e:
            print(f"Failed to log referral creation: {e}")

        response = (
            f"Thank you for using the GeekdHouse Referral Program! Here is your unique code:\n\n"
            f"<code>{code}</code>\n\n"
            f"Remember, please make sure to have your friends join the main channel! "
            f"Points are applied once a successful order is placed using the referal code.\n\n"
            f"Use /myreferrals to keep track of your points!"
        )
        await query.message.edit_text(response, parse_mode='HTML')

# ===== ADMIN REPLY MESSAGES =====
async def handle_admin_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Note: Filter in main() ensures this is only called for admins
    
    # Check for Tracking Code State
    if context.user_data.get('admin_state'):
        state = context.user_data['admin_state']
        if state['action'] == 'waiting_tracking':
            tracking_code = update.message.text
            ticket_id = state['ticket_id']
            ticket = db_get_ticket(ticket_id)
            
            if ticket:
                # Update Status
                db_update_ticket_status(ticket_id, "Order Shipped")
                
                # Send Message to User
                msg = (
                    f"🚚 <b>Your order has been shipped!</b>\n\n"
                    f"Here is your tracking code:\n<code>{tracking_code}</code>\n\n"
                    f"You can look at the status of your delivery by entering this code in the search bar at usps.com\n\n"
                    f"We recommend you to scroll down on the tracking site and sign your phone number up for text updates rather than checking the site over and over.\n\n"
                    f"Additionally, you can download 17track and input the tracking code there to have the app send you notification updates."
                )
                try:
                    await context.bot.send_message(chat_id=ticket['user_id'], text=msg, parse_mode='HTML')
                    await update.message.reply_text("✅ Tracking sent and status updated to Shipped!")
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to send to user: {e}")
            else:
                await update.message.reply_text("❌ Ticket not found.")
            
            del context.user_data['admin_state']
            return

    # Check for text editing mode
    if context.user_data.get('editing_text'):
        key = context.user_data['editing_text']
        
        # Handle Service/Category Message Edit
        if key.startswith("svc_msg_"):
            svc_id = key.replace("svc_msg_", "")
            item, _, _ = find_menu_item(global_config["menu"], svc_id)
            if item:
                target_field = "response_message" if item["type"] in ["service", "auto_response"] else "message"
                item[target_field] = update.message.text
                save_config()
                await update.message.reply_text(f"✅ Message for '{item['name']}' updated!")
            del context.user_data['editing_text']
            return

        new_text = update.message.text
        if new_text:
            global_config['texts'][key] = new_text
            save_config()
            del context.user_data['editing_text']
            await update.message.reply_text(f"✅ Text for '{key}' has been updated!")
            return
            
    # Check for Service Adding State
    if context.user_data.get('admin_state'):
        state = context.user_data['admin_state']
        if state['action'] == 'add_svc_name':
            name = update.message.text
            parent_id = state['parent_id']
            # Generate ID
            new_id = str(uuid.uuid4())[:8]
            
            # Ask for type
            context.user_data['admin_state'] = {'action': 'add_svc_type', 'name': name, 'id': new_id, 'parent_id': parent_id}
            keyboard = [
                [InlineKeyboardButton("📂 Category (Sub-menu)", callback_data='add_type_category')],
                [InlineKeyboardButton("🎫 Service (Ticket)", callback_data='add_type_service')],
                [InlineKeyboardButton("🤖 Automated Response", callback_data='add_type_auto_response')]
            ]
            await update.message.reply_text(f"Select type for '{name}':", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        # Other states...

    # Check for Review State
    if 'review_state' in context.user_data:
        await handle_review_step(update, context)
        return

    # Check for Shipping Details State (Admin testing as user)
    if 'ship_state' in context.user_data:
        await handle_shipping_step(update, context)
        return

    # Check for Ticket Creation (Referral) State (Admin testing as user)
    if 'ticket_creation_state' in context.user_data:
        await handle_ticket_creation_step(update, context)
        return

    ticket_id = context.user_data.get('reply_ticket_id')
    
    # If in the support group, ignore messages unless replying to a ticket
    if update.effective_chat.id == SUPPORT_GROUP_ID and not ticket_id:
        return

    if not ticket_id:
        # If not replying to a ticket, treat admin as a normal user (e.g. testing the bot)
        await handle_dm(update, context)
        return

    text = update.message.text or update.message.caption
    photo = update.message.photo[-1].file_id if update.message.photo else None
    
    # Update ticket activity
    ticket = db_get_ticket(ticket_id)
    if ticket and not ticket['closed']:
        db_update_ticket_activity(ticket_id)
        target_user_id = ticket['user_id']

        if photo:
            caption = f"💬 Staff: {text}" if text else "💬 Staff"
            await context.bot.send_photo(chat_id=target_user_id, photo=photo, caption=caption)
        else:
            await context.bot.send_message(chat_id=target_user_id, text=f"💬 Staff: {text}")
            
        await update.message.reply_text("✅ Message sent to user!")
    else:
        await update.message.reply_text("❌ Ticket not found (might be closed).")

# ===== SETTINGS / ADMIN COMMAND CENTER =====
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    print(f"⚙️ Settings command triggered by {user.id} in chat {update.effective_chat.id}")
    
    await show_settings_menu(update, context)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 Edit Texts", callback_data='settings_texts')],
        [InlineKeyboardButton("️ Manage Services", callback_data='settings_services')],
        [InlineKeyboardButton("❌ Close Menu", callback_data='settings_close')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "⚙️ <b>Admin Command Center</b>\nSelect a category to configure:"
    
    try:
        if update.callback_query:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        print(f"❌ Error showing settings menu: {e}")

async def appsettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    url = get_webapp_url(user.id, admin_mode=True)
    if not url:
        await update.message.reply_text("⚠️ Shop URL Not Set. Check WEBAPP_URL env var.")
        return

    keyboard = [[InlineKeyboardButton("🛍️ Manage Shop Settings", web_app=WebAppInfo(url=url))]]
    await update.message.reply_text("⚙️ <b>App Settings</b>\nClick below to manage the shop:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if update.effective_user.id not in ADMIN_IDS:
        return

    if data == 'settings_close':
        await query.message.delete()
        return
    
    if data == 'settings_no_url':
        await query.answer("⚠️ WEBAPP_URL is missing. Check bot logs.", show_alert=True)
        return
    
    if data == 'settings_menu':
        await show_settings_menu(update, context)
        return

    # Submenus
    if data == 'settings_texts':
        keyboard = []
        for key in global_config["texts"]:
            keyboard.append([InlineKeyboardButton(f"Edit: {key}", callback_data=f"set_text_{key}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='settings_menu')])
        await query.message.edit_text("📝 <b>Edit Texts</b>\nSelect a text to edit:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    # Actions
    if data.startswith("set_text_"):
        key = data.replace("set_text_", "")
        context.user_data['editing_text'] = key
        current_text = global_config["texts"].get(key, "N/A")
        
        placeholders = ""
        if key == "ticket_created":
            placeholders = "\nAvailable placeholders: {ticket_id}, {service_name}"
            
        msg_text = (
            f"📝 Editing <b>{key}</b>.\n\n"
            f"Current text:\n<pre>{current_text}</pre>\n\n"
            f"👇 Reply with the new text:{placeholders}"
        )
        
        await query.message.edit_text(msg_text, parse_mode='HTML')
        return

    # ===== MANAGE SERVICES =====
    if data == 'settings_services':
        await show_services_editor(update, context, global_config["menu"], "root")
        return

    if data.startswith("svc_open_"):
        svc_id = data.replace("svc_open_", "")
        item, _, _ = find_menu_item(global_config["menu"], svc_id)
        if item and item.get("items") is not None:
            await show_services_editor(update, context, item["items"], svc_id)
        return

    if data.startswith("svc_add_"):
        parent_id = data.replace("svc_add_", "")
        context.user_data['admin_state'] = {'action': 'add_svc_name', 'parent_id': parent_id}
        await query.message.edit_text("➕ <b>Add New Service</b>\n\nPlease reply with the <b>Name</b> of the new service/category:")
        return

    if data.startswith("add_type_"):
        # Handle type selection
        state = context.user_data.get('admin_state')
        if not state or 'name' not in state:
            await query.message.edit_text("❌ Session expired.")
            return
        
        new_type = data.replace("add_type_", "")
        new_item = {
            "id": state['id'],
            "name": state['name'],
            "type": new_type,
            "visible": True
        }
        if new_type == "category":
            new_item["items"] = []
            new_item["message"] = f"👇 Options for {state['name']}:"
        elif new_type == "auto_response":
            new_item["response_message"] = f"ℹ️ Info for {state['name']}"
        else:
            new_item["status"] = True
        
        # Add to parent
        parent_id = state['parent_id']
        if parent_id == "root":
            global_config["menu"].append(new_item)
        else:
            parent, _, _ = find_menu_item(global_config["menu"], parent_id)
            if parent:
                if "items" not in parent: parent["items"] = []
                parent["items"].append(new_item)
        
        save_config()
        del context.user_data['admin_state']
        await query.message.edit_text(f"✅ Added '{state['name']}'!")
        return

    if data.startswith("svc_edit_"):
        svc_id = data.replace("svc_edit_", "")
        item, _, _ = find_menu_item(global_config["menu"], svc_id)
        if not item: return
        
        keyboard = []
        # Toggle Visible
        vis_icon = "👁️ Visible" if item.get("visible", True) else "🚫 Hidden"
        keyboard.append([InlineKeyboardButton(f"Visibility: {vis_icon}", callback_data=f"svc_tog_vis_{svc_id}")])
        
        if item["type"] == "service":
            # Toggle Status
            stat_icon = "🟢 Open" if item.get("status", True) else "🔴 Closed"
            keyboard.append([InlineKeyboardButton(f"Status: {stat_icon}", callback_data=f"svc_tog_stat_{svc_id}")])
        
        # Edit Message
        msg_label = "Edit Menu Text" if item["type"] == "category" else "Edit Response"
        keyboard.append([InlineKeyboardButton(f"📝 {msg_label}", callback_data=f"svc_set_msg_{svc_id}")])
        
        # Delete
        keyboard.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"svc_del_{svc_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='settings_services')])
        
        await query.message.edit_text(f"⚙️ Editing: <b>{item['name']}</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if data.startswith("svc_tog_vis_"):
        svc_id = data.replace("svc_tog_vis_", "")
        item, _, _ = find_menu_item(global_config["menu"], svc_id)
        if item:
            item["visible"] = not item.get("visible", True)
            save_config()
            # Re-render edit menu
            update.callback_query.data = f"svc_edit_{svc_id}"
            await handle_settings_callback(update, context)
        return

    if data.startswith("svc_tog_stat_"):
        svc_id = data.replace("svc_tog_stat_", "")
        item, _, _ = find_menu_item(global_config["menu"], svc_id)
        if item:
            item["status"] = not item.get("status", True)
            save_config()
            update.callback_query.data = f"svc_edit_{svc_id}"
            await handle_settings_callback(update, context)
        return

    if data.startswith("svc_del_"):
        svc_id = data.replace("svc_del_", "")
        item, parent_list, idx = find_menu_item(global_config["menu"], svc_id)
        if parent_list is not None:
            del parent_list[idx]
            save_config()
            await query.message.edit_text("🗑️ Item deleted.")
        return

    if data.startswith("svc_set_msg_"):
        svc_id = data.replace("svc_set_msg_", "")
        context.user_data['editing_text'] = f"svc_msg_{svc_id}"
        item, _, _ = find_menu_item(global_config["menu"], svc_id)
        target_field = "response_message" if item["type"] in ["service", "auto_response"] else "message"
        current = item.get(target_field, "N/A")
        await query.message.edit_text(f"📝 Edit Message for <b>{item['name']}</b>\n\nCurrent:\n<pre>{current}</pre>\n\n👇 Reply with new text:", parse_mode='HTML')
        return

async def show_services_editor(update: Update, context: ContextTypes.DEFAULT_TYPE, menu_list, parent_id):
    keyboard = []
    for item in menu_list:
        icon = "📂" if item["type"] == "category" else "🎫"
        if item["type"] == "auto_response": icon = "🤖"
        
        name = item["name"]
        if not item.get("visible", True): name += " (Hidden)"
        
        # Row: [Edit] [Open (if category)]
        row = [InlineKeyboardButton(f"{icon} {name}", callback_data=f"svc_edit_{item['id']}")]
        if item["type"] == "category":
            row.append(InlineKeyboardButton("Open ➡️", callback_data=f"svc_open_{item['id']}"))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("➕ Add New", callback_data=f"svc_add_{parent_id}")])
    
    back_cb = 'settings_services' if parent_id != 'root' else 'settings_menu'
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
    
    await update.callback_query.message.edit_text(f"🛠️ <b>Manage Services</b>\nLevel: {parent_id}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("❗ Usage: /status <service_name_or_id> <open|closed>")
        return
    
    target = context.args[0].lower()
    state = context.args[1].lower()
    
    if state not in ['open', 'closed']:
        await update.message.reply_text("❗ State must be 'open' or 'closed'.")
        return
    
    is_open = (state == 'open')
    
    # Find service
    def find_and_update(menu, target):
        for item in menu:
            if target in item['id'].lower() or target in item['name'].lower():
                if item['type'] == 'service':
                    item['status'] = is_open
                    return item
            if item.get('items'):
                found = find_and_update(item['items'], target)
                if found: return found
        return None

    found_item = find_and_update(global_config["menu"], target)
    
    if not found_item:
        await update.message.reply_text(f"❗ Service '{target}' not found.")
        return

    save_config()
    
    await update.message.reply_text(f"✅ Service <b>{found_item['name']}</b> is now <b>{state.upper()}</b>.", parse_mode='HTML')

# ===== MENU COMMAND =====
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = get_webapp_url(user.id)
    
    if not url:
        await update.message.reply_text("⚠️ Shop URL is not configured. Please contact admin.")
        return

    keyboard = [[InlineKeyboardButton("🛍️ Open Shop", web_app=WebAppInfo(url=url))]]
    await update.message.reply_text("👇 <b>Tap below to open the shop:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ===== REFRESH COMMAND =====
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    global PRODUCT_CACHE
    status_msg = await update.message.reply_text("🔄 Reloading product file...")
    
    try:
        scraper = ManualScraper()
        fresh_result = scraper.get_products()
        
        if fresh_result and isinstance(fresh_result.get('data'), list):
            if not fresh_result.get('imagePathPrefix'):
                fresh_result['imagePathPrefix'] = "/uploads/products/"
            
            PRODUCT_CACHE["data"] = fresh_result
            PRODUCT_CACHE["timestamp"] = time.time()
            PRODUCT_CACHE["last_attempt"] = time.time()
            
            await status_msg.edit_text(f"✅ <b>Cache Refreshed!</b>\nLoaded {len(fresh_result['data'])} products.\nNext auto-reload in 1 hour.", parse_mode='HTML')
        else:
            await status_msg.edit_text("❌ Failed to load products. Please check the JSON file format.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")

# ===== BACKGROUND JOBS =====
async def check_timeouts(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    
    # Get all active tickets
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE closed = 0")
    tickets = c.fetchall()
    
    for t in tickets:
        ticket_id = t['id']
        last_activity = t['last_activity']
        last_prompt_at = t['last_prompt_at']
        snooze_until = t['snooze_until']
        
        inactivity = now - last_activity
        
        # 1. Check for Hard Delete (2 Weeks)
        if inactivity > DELETE_TIMEOUT:
            db_close_ticket(ticket_id)
            await send_to_support_group(context.bot, text=f"⏳ Ticket {ticket_id} closed automatically (2 weeks inactivity).")
            try:
                await context.bot.send_message(chat_id=t['user_id'], text=f"⏳ Ticket {ticket_id} has been closed due to extended inactivity.")
            except:
                pass
            continue

        # 2. Check for Inactivity Prompt (24 Hours)
        if inactivity > TICKET_TIMEOUT:
            # Check if snoozed
            if snooze_until and now < snooze_until:
                continue
            
            should_prompt = False
            if not last_prompt_at:
                should_prompt = True
            elif snooze_until and now >= snooze_until:
                should_prompt = True
            
            if should_prompt:
                # Send Prompt
                keyboard = [
                    [InlineKeyboardButton("Yes (Close)", callback_data=f"inact_yes_{ticket_id}")],
                    [InlineKeyboardButton("No (Keep Open)", callback_data=f"inact_no_{ticket_id}")]
                ]
                await send_to_support_group(
                    context.bot,
                    text=f"⏳ <b>Inactivity Alert</b>\nTicket {ticket_id} has been inactive for over 24 hours.\nClose it?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
                # Update DB: Set last_prompt_at, Clear snooze_until
                c.execute("UPDATE tickets SET last_prompt_at = ?, snooze_until = NULL WHERE id = ?", (now, ticket_id))
                conn.commit()
    
    conn.close()

async def handle_inactivity_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    parts = data.split("_")
    action = parts[1]
    ticket_id = parts[2]
    
    if action == "yes":
        db_close_ticket(ticket_id)
        await query.message.edit_text(f"✅ Ticket {ticket_id} closed by admin.")
        ticket = db_get_ticket(ticket_id)
        if ticket:
            try:
                await context.bot.send_message(chat_id=ticket['user_id'], text=f"🔒 Ticket {ticket_id} has been closed.")
            except:
                pass
    elif action == "no":
        # Snooze for 4 hours
        snooze_time = time.time() + (4 * 60 * 60)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE tickets SET snooze_until = ? WHERE id = ?", (snooze_time, ticket_id))
        conn.commit()
        conn.close()
        await query.message.edit_text(f"✅ Ticket {ticket_id} kept open. Will ask again in 4 hours.")

async def cleanup_database(context: ContextTypes.DEFAULT_TYPE):
    """Deletes closed tickets older than RETENTION_DAYS and reclaims disk space."""
    cutoff = time.time() - (RETENTION_DAYS * 24 * 60 * 60)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Delete old closed tickets
    c.execute("DELETE FROM tickets WHERE closed = 1 AND last_activity < ?", (cutoff,))
    deleted = c.rowcount
    if deleted > 0:
        # VACUUM is required to actually shrink the .db file size on disk
        c.execute("VACUUM")
        print(f"🧹 Database cleanup: Removed {deleted} old tickets and reclaimed space.")
    conn.commit()
    conn.close()

# ===== SET BOT COMMANDS (ONLY VISIBLE TO ADMINS WHERE NEEDED) =====
async def set_commands(app):
    global SUPPORT_GROUP_ID
    # Clear default commands to prevent BotFather/Global commands from showing up where not wanted
    await app.bot.delete_my_commands()
    
    # Clear any specific commands that might be stuck for Admins in DMs
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass

    # Set /start for all private chats (DMs)
    await app.bot.set_my_commands([
        BotCommand("start", "Show the main menu"),
        BotCommand("menu", "Open the Shop"),
        BotCommand("mytickets", "View your active tickets"),
        BotCommand("close", "Close current ticket"),
        BotCommand("review", "Leave a review"),
        BotCommand("refer", "Get Referral Code"),
        BotCommand("myreferrals", "Check Referral Points"),
        BotCommand("refresh", "Reload Product File"),
        BotCommand("help", "Show available commands")
    ], scope=BotCommandScopeAllPrivateChats())

    # Set /reply for the support group only, and ONLY for admins
    try:
        await app.bot.set_my_commands([
            BotCommand("reply", "Reply to a ticket"),
            BotCommand("settings", "Admin Settings"),
            BotCommand("appsettings", "Manage Web App"),
            BotCommand("refresh", "Reload Product File"),
            BotCommand("help", "Admin Help")
        ], scope=BotCommandScopeChatAdministrators(chat_id=SUPPORT_GROUP_ID))
    except ChatMigrated as e:
        print(f"⚠️ Group upgraded to Supergroup. Updating SUPPORT_GROUP_ID to {e.new_chat_id}")
        SUPPORT_GROUP_ID = e.new_chat_id
        # Retry with new ID
        await app.bot.set_my_commands([
            BotCommand("reply", "Reply to a ticket"),
            BotCommand("settings", "Admin Settings"),
            BotCommand("appsettings", "Manage Web App"),
            BotCommand("refresh", "Reload Product File"),
            BotCommand("help", "Admin Help")
        ], scope=BotCommandScopeChatAdministrators(chat_id=SUPPORT_GROUP_ID))

# Global cache for products to prevent login spam
PRODUCT_CACHE = {
    "data": None,
    "timestamp": 0,
    "last_attempt": 0
}
CACHE_DURATION = 3600  # 1 hour
FAILURE_COOLDOWN = 30 # 30 seconds cooldown on failure
SCRAPE_LOCK = threading.Lock()

class BotRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        global PRODUCT_CACHE
        
        if self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
            
        if self.path.startswith('/api/products'):
            try:
                # If cache is fresh and not a forced refresh, serve it immediately.
                # We ignore client-side 'nocache' params to prevent login loops
                if PRODUCT_CACHE["data"] and (time.time() - PRODUCT_CACHE["timestamp"] < CACHE_DURATION):
                    print("✅ Serving products from cache")
                    self.send_json(PRODUCT_CACHE["data"])
                    return

                # Acquire lock to prevent multiple browsers from launching at once
                with SCRAPE_LOCK:
                    now = time.time()
                    
                    # Double-check cache inside lock (another thread might have just finished)
                    if PRODUCT_CACHE["data"] and (now - PRODUCT_CACHE["timestamp"] < CACHE_DURATION):
                        print("✅ Serving products from cache (after lock)")
                        self.send_json(PRODUCT_CACHE["data"])
                        return

                    # Check failure cooldown to prevent login spam
                    if not PRODUCT_CACHE["data"] and (now - PRODUCT_CACHE.get("last_attempt", 0) < FAILURE_COOLDOWN):
                        print("⏳ Recent scrape failed. Serving error/empty to prevent login spam.")
                        self.send_json({"data": [], "error": True, "message": "Scrape cooldown active"})
                        return

                    # Mark attempt start
                    PRODUCT_CACHE["last_attempt"] = now

                    # --- Manual Load (No Scraping) ---
                    print("📂 Loading products from manual file...")
                    scraper = ManualScraper() # Uses manual_products.json by default
                    fresh_result = scraper.get_products()
                    
                    # --- Decide what to do with the new data ---
                    if fresh_result and isinstance(fresh_result.get('data'), list):
                        print(f"✅ Loaded {len(fresh_result['data'])} products from file.")
                        
                        # Ensure image path prefix is set
                        if not fresh_result.get('imagePathPrefix'):
                            fresh_result['imagePathPrefix'] = "/uploads/products/"
                        
                        # Update cache
                        PRODUCT_CACHE = { "data": fresh_result, "timestamp": time.time() }
                        self.send_json(fresh_result)
                    else:
                        print("❌ Failed to load manual data or data is empty.")
                        # Fallback to existing cache if available
                        if PRODUCT_CACHE["data"]:
                             self.send_json(PRODUCT_CACHE["data"])
                        else:
                             self.send_json({"error": True, "message": "Could not load product data"})
                
            except Exception as e:
                print(f"❌ Critical error in API proxy: {str(e)}")
                import traceback
                traceback.print_exc()
                
                # On critical error, still try to serve from cache.
                if PRODUCT_CACHE["data"]:
                    print("⚠️ Serving stale cache due to critical error.")
                    self.send_json(PRODUCT_CACHE["data"])
                else:
                    error_data = {
                        "data": [], "error": True, "message": f"Error fetching products: {str(e)}"
                    }
                    self.send_json(error_data)
            return
        return super().do_GET()
        
    def send_json(self, data):
        """Helper to send JSON response"""
        response = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(response)

def run_simple_server():
    # Railway provides PORT, default to 8080 if not set
    port = int(os.getenv("PORT", 8080))
    print(f"🌍 Starting Web Server on port {port}...")
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, BotRequestHandler)
    httpd.serve_forever()

def main():
    print(f"🚀 Bot is starting... (PTB Version: {ptb_version})")
    # Validation
    if not TOKEN:
        print("❌ Error: BOT_TOKEN is missing! Set it in your environment variables.")
        return
    if not SUPPORT_GROUP_ID:
        print("⚠️ Warning: SUPPORT_GROUP_ID is missing or 0. Messages to the admin group will fail.")
    print(f"ℹ️ Current Support Group ID: {SUPPORT_GROUP_ID}")
    
    if not WEBAPP_URL:
        print("⚠️ Warning: WEBAPP_URL is missing. The /menu command and Shop button will not work.")
    else:
        print(f"ℹ️ Web App URL: {WEBAPP_URL}")

    if not ADMIN_IDS:
        print("⚠️ Warning: ADMIN_IDS is empty. No admins will be able to reply.")
    print(f"📂 Database File Path: {os.path.abspath(DB_FILE)}")
    
    if "RAILWAY_ENVIRONMENT" in os.environ and not os.path.isabs(DB_FILE):
        print("⚠️ WARNING: Running on Railway with a relative DB_FILE path. Ensure you have a Volume mounted and DB_FILE points to it, or data will be lost on restart!")

    conn = init_db()
    migrate_json_to_db(conn)
    conn.close()
    load_config()
    
    # Start Web Server in background thread if PORT is set (Railway)
    if os.getenv("PORT"):
        threading.Thread(target=run_simple_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("reply", handle_reply_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("appsettings", appsettings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("ticketstatus", ticket_status_command))
    app.add_handler(CommandHandler("ticketinfo", ticketinfo_command))
    app.add_handler(CommandHandler("done", stop_reply_command))
    app.add_handler(CommandHandler("close", close_ticket_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("review", review_command))
    app.add_handler(CommandHandler("mytickets", mytickets_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("refer", refer_command))
    app.add_handler(CommandHandler("myreferrals", myreferrals_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    
    app.add_handler(CallbackQueryHandler(handle_reply_selection, pattern=r"^reply_[\w-]+$"))
    app.add_handler(CallbackQueryHandler(handle_ping_selection, pattern=r"^ping_[\w-]+$"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^(settings_|set_text_|toggle_svc_|toggle_btn_|svc_|add_type_)"))
    app.add_handler(CallbackQueryHandler(handle_review_callback, pattern=r"^rev_star_"))
    app.add_handler(CallbackQueryHandler(handle_myticket_selection, pattern=r"^sel_ticket_"))
    app.add_handler(CallbackQueryHandler(handle_shipping_callback, pattern=r"^ship_"))
    app.add_handler(CallbackQueryHandler(handle_referral_callback, pattern=r"^refer_"))
    app.add_handler(CallbackQueryHandler(handle_inactivity_response, pattern=r"^inact_"))
    
    # Menu handler (catch-all for dynamic IDs)
    app.add_handler(CallbackQueryHandler(handle_menu_callback))

    
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_chat_migration))
    # Admin handler must be registered BEFORE the general user handler
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_admin_dm))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_dm))

    # Job Queue for Timeouts (runs every 60 seconds)
    app.job_queue.run_repeating(check_timeouts, interval=60, first=10)

    # Job Queue for Database Cleanup (runs every 24 hours)
    app.job_queue.run_repeating(cleanup_database, interval=86400, first=60)

    print("Bot is running...")
    app.run_polling()

# ===== MAIN =====
if __name__ == "__main__":
    main()
