import json
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

DEFAULT_CONFIG = {
    "texts": {
        "welcome": "👋 Hi! Thanks for reaching out to GeekdHouse Support Bot.\n\nWe want to help you as best as we can.\n\nPlease create one ticket per user at a time.\n\nChoose an option from the menu below:",
        "ticket_created": "✅ Your ticket has been created! 🎉\n\n🎫 Ticket {ticket_id} has been sent to our staff.\n⏳ They will be with you shortly! 🚀",
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

bot_data = {
    "tickets": {},
    "user_started": [],
    "counter": 0,
    "config": copy.deepcopy(DEFAULT_CONFIG)
}

# ===== PERSISTENCE HELPERS =====
def load_data():
    global bot_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                loaded_data = json.load(f)
                bot_data.update(loaded_data)
            # Convert user_started to set for faster lookup, but keep as list in json
            bot_data["user_started"] = list(set(bot_data.get("user_started", [])))
            
            # Ensure config exists and has defaults (for updates)
            if "config" not in bot_data:
                bot_data["config"] = copy.deepcopy(DEFAULT_CONFIG)
            else:
                # Ensure menu exists (migration)
                if "menu" not in bot_data["config"]:
                    bot_data["config"]["menu"] = copy.deepcopy(DEFAULT_CONFIG["menu"])
                # Ensure texts exist
                if "texts" not in bot_data["config"]:
                    bot_data["config"]["texts"] = copy.deepcopy(DEFAULT_CONFIG["texts"])
                else:
                    for k, v in DEFAULT_CONFIG["texts"].items():
                        if k not in bot_data["config"]["texts"]:
                            bot_data["config"]["texts"][k] = v
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

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = bot_data["config"]
    keyboard = []
    
    for item in config.get("menu", []):
        if item.get("visible", True):
            keyboard.append([InlineKeyboardButton(item["name"], callback_data=item["id"])])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        config["texts"]["welcome"], 
        reply_markup=reply_markup
    )

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Don't answer yet, might be handled elsewhere if we didn't filter correctly, 
    # but here we assume it's a menu click.
    user = update.effective_user
    choice = query.data
    
    # Try to find the item in the menu
    item, _, _ = find_menu_item(bot_data["config"]["menu"], choice)
    
    if not item:
        # Not a menu item (could be admin command handled by another handler, but if we got here, it wasn't handled)
        # Or it's a stale button.
        await query.answer("❌ Option not found.", show_alert=True)
        return

    await query.answer()

    if user.id not in bot_data["user_started"]:
        bot_data["user_started"].append(user.id)
    
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
            await query.answer(bot_data["config"]["texts"]["service_closed"], show_alert=True)
            await query.message.reply_text(bot_data["config"]["texts"]["service_closed"])
            return
        
        await create_new_ticket(update, context, item["name"], item.get("response_message"))

async def create_new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, section_name: str, custom_msg=None):
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
    if custom_msg:
        msg_text = custom_msg
    else:
        msg_text = bot_data["config"]["texts"]["ticket_created"]
    
    msg_text = msg_text.replace("{ticket_id}", ticket_id).replace("{service_name}", section_name)
    await update.callback_query.message.edit_text(msg_text)

    # Message to Admin Group
    keyboard = [[InlineKeyboardButton("Reply to Ticket ✍️", callback_data=f"reply_{user.id}")]]
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

# ===== ADMIN REPLY MESSAGES =====
async def handle_admin_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Note: Filter in main() ensures this is only called for admins
    
    # Check for text editing mode
    if context.user_data.get('editing_text'):
        key = context.user_data['editing_text']
        
        # Handle Service/Category Message Edit
        if key.startswith("svc_msg_"):
            svc_id = key.replace("svc_msg_", "")
            item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
            if item:
                target_field = "response_message" if item["type"] == "service" else "message"
                item[target_field] = update.message.text
                save_data()
                await update.message.reply_text(f"✅ Message for '{item['name']}' updated!")
            del context.user_data['editing_text']
            return

        new_text = update.message.text
        if new_text:
            bot_data['config']['texts'][key] = new_text
            save_data()
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
                [InlineKeyboardButton("🎫 Service (Ticket)", callback_data='add_type_service')]
            ]
            await update.message.reply_text(f"Select type for '{name}':", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        # Other states...

    target_id = context.user_data.get('reply_to')
    
    # If in the support group, ignore messages unless replying to a ticket
    if update.effective_chat.id == SUPPORT_GROUP_ID and not target_id:
        return

    if not target_id:
        # If not replying to a ticket, treat admin as a normal user (e.g. testing the bot)
        await handle_dm(update, context)
        return

    text = update.message.text or update.message.caption
    photo = update.message.photo[-1].file_id if update.message.photo else None
    
    # Update ticket activity
    if str(target_id) in bot_data["tickets"]:
        bot_data["tickets"][str(target_id)]["last_activity"] = time.time()
        save_data()

    if photo:
        caption = f"💬 Staff: {text}" if text else "💬 Staff"
        await context.bot.send_photo(chat_id=target_id, photo=photo, caption=caption)
    else:
        await context.bot.send_message(chat_id=target_id, text=f"💬 Staff: {text}")
        
    await update.message.reply_text("✅ Message sent to user!")

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
        for key in bot_data["config"]["texts"]:
            keyboard.append([InlineKeyboardButton(f"Edit: {key}", callback_data=f"set_text_{key}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='settings_menu')])
        await query.message.edit_text("📝 <b>Edit Texts</b>\nSelect a text to edit:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    # Actions
    if data.startswith("set_text_"):
        key = data.replace("set_text_", "")
        context.user_data['editing_text'] = key
        current_text = bot_data["config"]["texts"].get(key, "N/A")
        
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
        await show_services_editor(update, context, bot_data["config"]["menu"], "root")
        return

    if data.startswith("svc_open_"):
        svc_id = data.replace("svc_open_", "")
        item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
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
        else:
            new_item["status"] = True
        
        # Add to parent
        parent_id = state['parent_id']
        if parent_id == "root":
            bot_data["config"]["menu"].append(new_item)
        else:
            parent, _, _ = find_menu_item(bot_data["config"]["menu"], parent_id)
            if parent:
                if "items" not in parent: parent["items"] = []
                parent["items"].append(new_item)
        
        save_data()
        del context.user_data['admin_state']
        await query.message.edit_text(f"✅ Added '{state['name']}'!")
        return

    if data.startswith("svc_edit_"):
        svc_id = data.replace("svc_edit_", "")
        item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
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
        msg_label = "Edit Menu Text" if item["type"] == "category" else "Edit Ticket Response"
        keyboard.append([InlineKeyboardButton(f"📝 {msg_label}", callback_data=f"svc_set_msg_{svc_id}")])
        
        # Delete
        keyboard.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"svc_del_{svc_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='settings_services')])
        
        await query.message.edit_text(f"⚙️ Editing: <b>{item['name']}</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if data.startswith("svc_tog_vis_"):
        svc_id = data.replace("svc_tog_vis_", "")
        item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
        if item:
            item["visible"] = not item.get("visible", True)
            save_data()
            # Re-render edit menu
            update.callback_query.data = f"svc_edit_{svc_id}"
            await handle_settings_callback(update, context)
        return

    if data.startswith("svc_tog_stat_"):
        svc_id = data.replace("svc_tog_stat_", "")
        item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
        if item:
            item["status"] = not item.get("status", True)
            save_data()
            update.callback_query.data = f"svc_edit_{svc_id}"
            await handle_settings_callback(update, context)
        return

    if data.startswith("svc_del_"):
        svc_id = data.replace("svc_del_", "")
        item, parent_list, idx = find_menu_item(bot_data["config"]["menu"], svc_id)
        if parent_list is not None:
            del parent_list[idx]
            save_data()
            await query.message.edit_text("🗑️ Item deleted.")
        return

    if data.startswith("svc_set_msg_"):
        svc_id = data.replace("svc_set_msg_", "")
        context.user_data['editing_text'] = f"svc_msg_{svc_id}"
        item, _, _ = find_menu_item(bot_data["config"]["menu"], svc_id)
        target_field = "response_message" if item["type"] == "service" else "message"
        current = item.get(target_field, "N/A")
        await query.message.edit_text(f"📝 Edit Message for <b>{item['name']}</b>\n\nCurrent:\n<pre>{current}</pre>\n\n👇 Reply with new text:", parse_mode='HTML')
        return

async def show_services_editor(update: Update, context: ContextTypes.DEFAULT_TYPE, menu_list, parent_id):
    keyboard = []
    for item in menu_list:
        icon = "📂" if item["type"] == "category" else "🎫"
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

    found_item = find_and_update(bot_data["config"]["menu"], target)
    
    if not found_item:
        await update.message.reply_text(f"❗ Service '{target}' not found.")
        return

    save_data()
    
    await update.message.reply_text(f"✅ Service <b>{found_item['name']}</b> is now <b>{state.upper()}</b>.", parse_mode='HTML')

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

    load_data()
    app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reply", handle_reply_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("done", stop_reply_command))
    app.add_handler(CommandHandler("close", close_ticket_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(handle_reply_selection, pattern=r"^reply_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_ping_selection, pattern=r"^ping_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^(settings_|set_text_|toggle_svc_|toggle_btn_|svc_|add_type_)"))
    
    # Menu handler (catch-all for dynamic IDs)
    app.add_handler(CallbackQueryHandler(handle_menu_callback))

    
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_chat_migration))
    # Admin handler must be registered BEFORE the general user handler
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_admin_dm))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_dm))

    # Job Queue for Timeouts (runs every 60 seconds)
    app.job_queue.run_repeating(check_timeouts, interval=60, first=10)

    print("Bot is running...")
    app.run_polling()

# ===== MAIN =====
if __name__ == "__main__":
    main()
