import json
import os
import random
import string
import time
import asyncio
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skipping .env load
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeChatAdministrators
from telegram.error import ChatMigrated
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID") or 0)
DATA_FILE = "bot_data.json"
TICKET_TIMEOUT = 4 * 60 * 60  # 4 hours in seconds

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
bot_data = {
    "tickets": {},
    "user_started": [],
    "counter": 0
}

# ===== PERSISTENCE HELPERS =====
def load_data():
    global bot_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                bot_data = json.load(f)
            # Convert user_started to set for faster lookup, but keep as list in json
            bot_data["user_started"] = list(set(bot_data.get("user_started", [])))
        except Exception as e:
            print(f"Error loading data: {e}")

def save_data():
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(bot_data, f, indent=4)
    except Exception as e:
        print(f"Error saving data: {e}")

def generate_ticket_id():
    bot_data["counter"] += 1
    count = bot_data["counter"]
    
    # Generate suffix: 1-99, then A-Z, then AA-ZZ...
    if count < 100:
        suffix = str(count)
    else:
        # Simple conversion for 100+ to letters (A, B... Z, AA...)
        # This is a basic implementation.
        def num_to_letters(n):
            res = ""
            while n > 0:
                n, remainder = divmod(n - 1, 26)
                res = chr(65 + remainder) + res
            return res
        suffix = num_to_letters(count - 99)

    # 6 random chars
    random_chars = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{random_chars}-{suffix}"

async def send_to_support_group(bot, text, **kwargs):
    global SUPPORT_GROUP_ID
    if not SUPPORT_GROUP_ID:
        print(f"⚠️ Cannot send message to support group: ID is 0. Message: {text}")
        return
    try:
        await bot.send_message(chat_id=SUPPORT_GROUP_ID, text=text, **kwargs)
    except ChatMigrated as e:
        print(f"⚠️ Group upgraded to Supergroup. Updating SUPPORT_GROUP_ID to {e.new_chat_id}")
        SUPPORT_GROUP_ID = e.new_chat_id
        await bot.send_message(chat_id=SUPPORT_GROUP_ID, text=text, **kwargs)

async def handle_chat_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SUPPORT_GROUP_ID
    new_id = update.message.migrate_to_chat_id
    print(f"⚠️ Group upgraded to Supergroup (Event). Updating SUPPORT_GROUP_ID to {new_id}")
    SUPPORT_GROUP_ID = new_id

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛒 Create an Order", callback_data='create_order')],
        [InlineKeyboardButton("❓ Support", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Hi! Thanks for reaching out to GeekdHouse Support Bot.\n\n"
        "We want to help you as best as we can.\n\n"
        "Please create one ticket per user at a time.\n\n"
        "Choose an option from the menu below:", 
        reply_markup=reply_markup
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    choice = query.data
    
    if user.id not in bot_data["user_started"]:
        bot_data["user_started"].append(user.id)
    
    if choice == 'create_order':
        keyboard = [
            [InlineKeyboardButton("📦 Singles (1-5 pieces)", callback_data='order_singles')],
            [InlineKeyboardButton("🚛 Bulk (10+ pieces)", callback_data='order_bulk')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "✅ You selected: 🛒 Create Order\n\n"
            "👇 Next, please choose from the options below:",
            reply_markup=reply_markup
        )
        return

    # Determine section name based on choice
    section_name = ""
    if choice == 'support':
        section_name = "Support"
    elif choice == 'order_singles':
        section_name = "Create Order (Singles)"
    elif choice == 'order_bulk':
        section_name = "Create Order (Bulk)"
    
    if section_name:
        await create_new_ticket(update, context, section_name)

async def create_new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, section_name: str):
    user = update.effective_user
    ticket_id = generate_ticket_id()
    
    bot_data["tickets"][str(user.id)] = {
        "id": ticket_id,
        "section": section_name,
        "created_at": time.time(),
        "last_activity": time.time()
    }
    save_data()

    # Message to User
    await update.callback_query.message.edit_text(
        f"✅ Your ticket has been created! 🎉\n\n"
        f"🎫 Ticket {ticket_id} has been sent to our staff.\n"
        f"⏳ They will be with you shortly! 🚀"
    )

    # Message to Admin Group
    keyboard = [[InlineKeyboardButton("Reply to Ticket ✍️", callback_data=f"reply_{user.id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await send_to_support_group(
        context.bot,
        text=f"🆕 <b>New Ticket Created!</b>\n"
             f"👤 User: {user.first_name} ({user.id})\n"
             f"🎫 Ticket ID: {ticket_id}\n"
             f"📂 Category: {section_name}",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    
# ===== DM HANDLER =====
async def handle_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # Ignore messages in the support group to prevent spam
    if update.effective_chat.id == SUPPORT_GROUP_ID:
        return

    # Auto-start menu for first-time users
    if user.id not in bot_data["user_started"]:
        bot_data["user_started"].append(user.id)
        save_data()
        await start(update, context)
        return

    # Only handle message if user is in a session
    ticket = bot_data["tickets"].get(str(user.id))
    if ticket:
        # Update activity
        ticket["last_activity"] = time.time()
        save_data()

        # Forward message to admin group
        await send_to_support_group(
            context.bot,
            text=f"📨 Message from ({user.id}) Ticket {ticket['id']}:\n{text}"
        )
    else:
        # User hasn't selected anything yet
        await update.message.reply_text("❗ Please select an option from the menu to proceed! 📋")

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

    if not bot_data["tickets"]:
        await update.message.reply_text("📭 No open tickets right now.")
        return

    # Build inline keyboard of open tickets
    keyboard = []
    for uid, ticket_info in bot_data["tickets"].items():
        tid = ticket_info['id']
        section = ticket_info['section']
        keyboard.append([
            InlineKeyboardButton(f"Ticket {tid} ({section})", callback_data=f"reply_{uid}"),
            InlineKeyboardButton("Ping 🔔", callback_data=f"ping_{uid}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👇 Select a ticket to reply to:", reply_markup=reply_markup)

async def handle_reply_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_user = update.effective_user

    if admin_user.id not in ADMIN_IDS:
        return

    # Extract user_id from callback_data
    target_id = int(query.data.split("_")[1])
    context.user_data['reply_to'] = target_id
    
    # Get ticket info for display
    ticket = bot_data["tickets"].get(str(target_id))
    ticket_display = ticket['id'] if ticket else "Unknown"
    
    if update.effective_chat.type == 'private':
        await query.message.edit_text(f"✏️ Now reply to Ticket {ticket_display} (User {target_id}).\nType your message here:\n(Type /done to finish, /close to close ticket)")
    else:
        # In group chat, just notify via alert and don't edit the ticket message
        await query.answer(f"✏️ You are now replying to Ticket {ticket_display}", show_alert=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"ℹ️ {admin_user.first_name} is now replying to Ticket {ticket_display}.")

async def handle_ping_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id not in ADMIN_IDS:
        return

    # Extract user_id from callback_data
    target_id = int(query.data.split("_")[1])
    
    try:
        await context.bot.send_message(chat_id=target_id, text="❗ You are currently being pinged by the staff!")
        await query.message.reply_text(f"✅ Ping sent to {target_id}!")
    except Exception as e:
        await query.message.reply_text(f"❌ Failed to ping {target_id}: {e}")

async def stop_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    
    if context.user_data.pop('reply_to', None):
        # Notify group only
        await send_to_support_group(
            context.bot,
            text=f"ℹ️ Admin {user.first_name} disconnected from the ticket."
        )
        # No message to user
    else:
        await update.message.reply_text("ℹ️ You are not in reply mode.")

async def close_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Check if Admin
    if user.id in ADMIN_IDS:
        target_id = context.user_data.get('reply_to')
        if not target_id:
            await update.message.reply_text("❗ You must be replying to a ticket to close it. Use /reply first.")
            return
        
        # Close the ticket
        if str(target_id) in bot_data["tickets"]:
            ticket_id = bot_data["tickets"][str(target_id)]['id']
            del bot_data["tickets"][str(target_id)]
            save_data()
            
            # Notify User
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🔒 Ticket {ticket_id} has been closed.")
            except:
                pass
            
            # Notify Admin/Group
            await send_to_support_group(context.bot, text=f"🔒 Ticket {ticket_id} closed by admin.")
            context.user_data.pop('reply_to', None)
        else:
            await update.message.reply_text("❗ Ticket already closed or not found.")
            
    else:
        # User closing their own ticket
        if str(user.id) in bot_data["tickets"]:
            ticket_id = bot_data["tickets"][str(user.id)]['id']
            del bot_data["tickets"][str(user.id)]
            save_data()
            
            await update.message.reply_text(f"🔒 Ticket {ticket_id} has been closed.")
            await send_to_support_group(
                context.bot,
                text=f"🔒 Ticket {ticket_id} closed by user {user.first_name}."
            )
        else:
            # If user has no ticket, ignore or inform
            await update.message.reply_text("❗ You do not have an open ticket.")

# ===== ADMIN REPLY MESSAGES =====
async def handle_admin_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Note: Filter in main() ensures this is only called for admins

    target_id = context.user_data.get('reply_to')
    
    # If in the support group, ignore messages unless replying to a ticket
    if update.effective_chat.id == SUPPORT_GROUP_ID and not target_id:
        return

    if not target_id:
        # If not replying to a ticket, treat admin as a normal user (e.g. testing the bot)
        await handle_dm(update, context)
        return

    text = update.message.text
    
    # Update ticket activity
    if str(target_id) in bot_data["tickets"]:
        bot_data["tickets"][str(target_id)]["last_activity"] = time.time()
        save_data()

    await context.bot.send_message(chat_id=target_id, text=f"💬 Staff: {text}")
    await update.message.reply_text("✅ Message sent to user!")

# ===== BACKGROUND JOBS =====
async def check_timeouts(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    to_remove = []
    
    # Check for expired tickets
    for uid, ticket in bot_data["tickets"].items():
        if now - ticket["last_activity"] > TICKET_TIMEOUT:
            to_remove.append(uid)
            
    for uid in to_remove:
        ticket_id = bot_data["tickets"][uid]['id']
        del bot_data["tickets"][uid]
        save_data()
        
        # Notify
        await send_to_support_group(context.bot, text=f"⏳ Ticket {ticket_id} closed due to inactivity.")
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"⏳ Ticket {ticket_id} has been closed due to inactivity.")
        except:
            pass

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
        BotCommand("close", "Close current ticket")
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
    print("🚀 Bot is starting...")
    # Validation
    if not TOKEN:
        print("❌ Error: BOT_TOKEN is missing! Set it in your environment variables.")
        return
    if not SUPPORT_GROUP_ID:
        print("⚠️ Warning: SUPPORT_GROUP_ID is missing or 0. Messages to the admin group will fail.")
    print(f"ℹ️ Current Support Group ID: {SUPPORT_GROUP_ID}")
    if not ADMIN_IDS:
        print("⚠️ Warning: ADMIN_IDS is empty. No admins will be able to reply.")

    load_data()
    app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reply", handle_reply_command))
    app.add_handler(CommandHandler("done", stop_reply_command))
    app.add_handler(CommandHandler("close", close_ticket_command))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^(create_order|support|order_singles|order_bulk)$"))
    app.add_handler(CallbackQueryHandler(handle_reply_selection, pattern=r"^reply_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_ping_selection, pattern=r"^ping_\d+$"))
    
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_chat_migration))
    # Admin handler must be registered BEFORE the general user handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_admin_dm))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_dm))

    # Job Queue for Timeouts (runs every 60 seconds)
    app.job_queue.run_repeating(check_timeouts, interval=60, first=10)

    print("Bot is running...")
    app.run_polling()

# ===== MAIN =====
if __name__ == "__main__":
    main()
