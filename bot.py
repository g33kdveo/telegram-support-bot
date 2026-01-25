import json
import sqlite3
import os
import random
import string
import time
import asyncio
import copy
import uuid
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skipping .env load
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeChatAdministrators, __version__ as ptb_version
from telegram.error import ChatMigrated
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID") or 0)
REVIEW_CHANNEL_ID = os.getenv("REVIEW_CHANNEL_ID")
REVIEW_TOPIC_ID = int(os.getenv("REVIEW_TOPIC_ID") or 0)
DB_FILE = os.getenv("DB_FILE", "bot_database.db")
TICKET_TIMEOUT = 4 * 60 * 60  # 4 hours in seconds
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS") or 15)

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
            "id": "create_order",
            "name": "🛒 Create an Order",
            "type": "category",
            "visible": True,
            "message": "✅ You selected: 🛒 Create Order\n\n👇 Next, please choose from the options below:",
            "items": [
                {"id": "order_singles", "name": "📦 Singles (1-5 pieces)", "type": "service", "status": True, "visible": True, "response_message": "✅ You have chosen {service_name}\nYour ticket has been created! 🎉\n\nPlease have your order ready!\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀"},
                {"id": "order_bulk", "name": "🚛 Bulk (10+ pieces)", "type": "service", "status": True, "visible": True, "response_message": "✅ You have chosen {service_name}\nYour ticket has been created! 🎉\n\nPlease have your order ready!\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀"}
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
    # Config Table (Key-Value)
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    conn.commit()
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

def db_create_ticket(ticket_id, user_id, section):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO tickets (id, user_id, section, created_at, last_activity) VALUES (?, ?, ?, ?, ?)",
              (ticket_id, user_id, section, time.time(), time.time()))
    conn.commit()
    conn.close()

def db_update_ticket_activity(ticket_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tickets SET last_activity = ? WHERE id = ?", (time.time(), ticket_id))
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

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = global_config
    keyboard = []
    
    for item in config.get("menu", []):
        if item.get("visible", True):
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
        
        await create_new_ticket(update, context, item["name"], item.get("response_message"))
    elif item["type"] == "auto_response":
        # Automated response, no ticket
        await query.message.reply_text(item.get("response_message", "ℹ️ Info"))

async def create_new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, section_name: str, custom_msg=None):
    user = update.effective_user
    if db_is_user_banned(user.id):
        await update.callback_query.message.reply_text("⛔ You are blocked from creating tickets.")
        return

    ticket_id = generate_ticket_id()
    db_create_ticket(ticket_id, user.id, section_name)

    # Message to User
    if custom_msg:
        msg_text = custom_msg
    else:
        msg_text = global_config["texts"]["ticket_created"]
    
    msg_text = msg_text.replace("{ticket_id}", ticket_id).replace("{service_name}", section_name) if msg_text else "Ticket Created."
    await update.callback_query.message.edit_text(msg_text)

    # Message to Admin Group
    keyboard = [[InlineKeyboardButton("Reply to Ticket ✍️", callback_data=f"reply_{ticket_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_to_support_group(
        context.bot,
        text=f"🆕 <b>New Ticket Created!</b>\n"
             f"👤 User: {user.first_name} (@{user.username}) ({user.id})\n"
             f"🎫 Ticket ID: {ticket_id}\n"
             f"📂 Category: {section_name}",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    
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

# ===== TICKET STATUS COMMAND =====
async def ticket_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    
    ticket_id = context.user_data.get('reply_ticket_id')
    if not ticket_id:
        await update.message.reply_text("❗ You must be replying to a ticket to change status.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /ticketstatus <status>\nOptions: accepted, paid, package, shipped, delivered, complete")
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
        
        new_status = "Order Delivered"
        db_update_ticket_status(ticket_id, new_status)
        db_close_ticket(ticket_id)
        
        final_msg = "🎉 <b>Order Delivered!</b>\n\nThank you for shopping with GeekdHouse! We hope you had a great experience and we hope to see you back soon! Use /review your order and leave a review! Any feedback is appreciated!"
        await context.bot.send_message(chat_id=user_id, text=final_msg, parse_mode='HTML')
        await update.message.reply_text(f"✅ Status updated to Delivered. Ticket closed.")
        return
    elif status_key == "complete":
        # Singles Check
        if "singles" not in ticket['section'].lower():
            await update.message.reply_text("⚠️ Warning: This ticket does not seem to be Singles. Proceeding anyway.")
            
        new_status = "Order Complete"
        db_update_ticket_status(ticket_id, new_status)
        db_close_ticket(ticket_id)
        
        final_msg = "🎉 <b>Order Complete!</b>\n\nThank you for shopping with GeekdHouse! We hope you had a great experience and we hope to see you back soon! Use /review your order and leave a review! Any feedback is appreciated!"
        await context.bot.send_message(chat_id=user_id, text=final_msg, parse_mode='HTML')
        await update.message.reply_text(f"✅ Status updated to Complete. Ticket closed.")
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
            admin_text = (
                f"🌟 <b>New Review!</b>\n"
                f"👤 User: {user.first_name} (@{user.username})\n"
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
    
    await show_settings_menu(update, context)

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 Edit Texts", callback_data='settings_texts')],
        [InlineKeyboardButton("️ Manage Services", callback_data='settings_services')],
        [InlineKeyboardButton("❌ Close Menu", callback_data='settings_close')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "⚙️ <b>Admin Command Center</b>\nSelect a category to configure:"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if update.effective_user.id not in ADMIN_IDS:
        return

    if data == 'settings_close':
        await query.message.delete()
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

# ===== BACKGROUND JOBS =====
async def check_timeouts(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    
    # Get all active tickets
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE closed = 0")
    tickets = c.fetchall()
    conn.close()
    
    for t in tickets:
        if now - t['last_activity'] > TICKET_TIMEOUT:
            db_close_ticket(t['id'])
            
            # Notify
            await send_to_support_group(context.bot, text=f"⏳ Ticket {t['id']} closed due to inactivity.")
            try:
                await context.bot.send_message(chat_id=t['user_id'], text=f"⏳ Ticket {t['id']} has been closed due to inactivity.")
            except:
                pass

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
        BotCommand("mytickets", "View your active tickets"),
        BotCommand("close", "Close current ticket"),
        BotCommand("review", "Leave a review"),
        BotCommand("help", "Show available commands")
    ], scope=BotCommandScopeAllPrivateChats())

    # Set /reply for the support group only, and ONLY for admins
    try:
        await app.bot.set_my_commands([
            BotCommand("reply", "Reply to a ticket")
        ], scope=BotCommandScopeChatAdministrators(chat_id=SUPPORT_GROUP_ID))
    except ChatMigrated as e:
        print(f"⚠️ Group upgraded to Supergroup. Updating SUPPORT_GROUP_ID to {e.new_chat_id}")
        SUPPORT_GROUP_ID = e.new_chat_id
        # Retry with new ID
        await app.bot.set_my_commands([
            BotCommand("reply", "Reply to a ticket")
        ], scope=BotCommandScopeChatAdministrators(chat_id=SUPPORT_GROUP_ID))

def main():
    print(f"🚀 Bot is starting... (PTB Version: {ptb_version})")
    # Validation
    if not TOKEN:
        print("❌ Error: BOT_TOKEN is missing! Set it in your environment variables.")
        return
    if not SUPPORT_GROUP_ID:
        print("⚠️ Warning: SUPPORT_GROUP_ID is missing or 0. Messages to the admin group will fail.")
    print(f"ℹ️ Current Support Group ID: {SUPPORT_GROUP_ID}")
    if not ADMIN_IDS:
        print("⚠️ Warning: ADMIN_IDS is empty. No admins will be able to reply.")

    conn = init_db()
    migrate_json_to_db(conn)
    conn.close()
    load_config()
    
    app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reply", handle_reply_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("ticketstatus", ticket_status_command))
    app.add_handler(CommandHandler("done", stop_reply_command))
    app.add_handler(CommandHandler("close", close_ticket_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("review", review_command))
    app.add_handler(CommandHandler("mytickets", mytickets_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("help", help_command))
    
    app.add_handler(CallbackQueryHandler(handle_reply_selection, pattern=r"^reply_[\w-]+$"))
    app.add_handler(CallbackQueryHandler(handle_ping_selection, pattern=r"^ping_[\w-]+$"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^(settings_|set_text_|toggle_svc_|toggle_btn_|svc_|add_type_)"))
    app.add_handler(CallbackQueryHandler(handle_review_callback, pattern=r"^rev_star_"))
    app.add_handler(CallbackQueryHandler(handle_myticket_selection, pattern=r"^sel_ticket_"))
    
    # Menu handler (catch-all for dynamic IDs)
    app.add_handler(CallbackQueryHandler(handle_menu_callback))

    
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_chat_migration))
    # Admin handler must be registered BEFORE the general user handler
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
