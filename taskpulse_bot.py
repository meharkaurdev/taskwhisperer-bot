import logging
import sqlite3
import time
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_NAME = "taskpulse.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            schedule TEXT DEFAULT '10:00,14:00,18:00',
            max_reminders INTEGER DEFAULT 3,
            stop_on_response BOOLEAN DEFAULT TRUE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            created_at TIMESTAMP,
            status TEXT DEFAULT 'active',  -- active, completed, skipped, stopped
            last_reminded TIMESTAMP NULL,
            reminder_count INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Helper functions
def get_user_prefs(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT schedule, max_reminders, stop_on_response FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'schedule': row[0].split(',') if row[0] else ['10:00', '14:00', '18:00'],
            'max_reminders': row[1],
            'stop_on_response': row[2]
        }
    return {'schedule': ['10:00', '14:00', '18:00'], 'max_reminders': 3, 'stop_on_response': True}

def add_user_if_missing(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_task(user_id, text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, created_at) VALUES (?, ?, ?)",
              (user_id, text.strip(), datetime.now()))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_active_tasks(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        SELECT id, text, reminder_count, last_reminded 
        FROM tasks 
        WHERE user_id = ? AND status = 'active'
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_task_status(task_id, status, new_reminder_count=None, new_last_reminded=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if new_reminder_count is not None:
        c.execute("UPDATE tasks SET reminder_count = ?, last_reminded = ? WHERE id = ?", 
                  (new_reminder_count, new_last_reminded, task_id))
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()

def get_task_text(task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT text FROM tasks WHERE id = ?", (task_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else ""

def get_tz_time(time_str, tz_name="UTC"):
    """Convert HH:MM string to timezone-aware datetime for today"""
    now = datetime.now(pytz.timezone(tz_name))
    hour, minute = map(int, time_str.split(':'))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: check and send reminders to all users"""
    users_to_remind = {}
    now = datetime.now(pytz.UTC)
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Get all active users and their schedules
    c.execute("SELECT user_id, schedule FROM users")
    for user_id, schedule_str in c.fetchall():
        schedule = schedule_str.split(',') if schedule_str else ['10:00', '14:00', '18:00']
        for timestr in schedule:
            try:
                remind_time = get_tz_time(timestr, "UTC")
                # If today's reminder time has passed, skip
                if remind_time > now:
                    continue
                # Check if we already sent a reminder today for this user+time
                # We'll check all tasks and see which ones are due for a reminder
                c.execute("""
                    SELECT id, text, reminder_count FROM tasks 
                    WHERE user_id = ? AND status = 'active' AND reminder_count < ?
                """, (user_id, 3))  # Max 3 reminders per task per day
                tasks = c.fetchall()
                if tasks:
                    if user_id not in users_to_remind:
                        users_to_remind[user_id] = []
                    users_to_remind[user_id].extend(tasks)
            except Exception as e:
                logger.error(f"Error parsing time {timestr}: {e}")
    
    conn.close()
    
    for user_id, tasks in users_to_remind.items():
        # Only send one reminder per user per scheduler run (to avoid spam)
        if tasks:
            task_id, task_text, count = tasks[0]  # Pick first task
            keyboard = [
                [InlineKeyboardButton("✅ Completed", callback_data=f"complete:{task_id}")],
                [InlineKeyboardButton("⏳ Skip for today", callback_data=f"skip:{task_id}")],
                [InlineKeyboardButton("⏱️ Delay 2h", callback_data=f"delay:{task_id}")],
                [InlineKeyboardButton("🛑 Stop Reminders Forever", callback_data=f"stopforever:{task_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Task Reminder: \"{task_text}\"\n\nWhat are you doing right now?",
                    reply_markup=reply_markup
                )
                # Log that we reminded this task
                update_task_status(task_id, 'active', count + 1, now.isoformat())
                logger.info(f"Sent reminder to {user_id} for task {task_id}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {user_id}: {e}")

# Handle button presses
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[0]
    task_id = int(parts[1])
    user_id = query.from_user.id

    task_text = get_task_text(task_id)

    if action == "complete":
        update_task_status(task_id, "completed")
        await query.edit_message_text(f"✅ Great job! Task '{task_text}' marked as completed.")
        
    elif action == "skip":
        update_task_status(task_id, "skipped")  # Will be reactivated tomorrow
        await query.edit_message_text(f"⏳ Skipped '{task_text}' for today. I'll remind you again tomorrow!")

    elif action == "delay":
        # Delay by 2 hours
        delay_until = datetime.now(pytz.UTC) + timedelta(hours=2)
        update_task_status(task_id, "active", 0, delay_until.isoformat())  # Reset counter
        await query.edit_message_text(f"⏱️ Delayed '{task_text}' until {delay_until.strftime('%H:%M')}.")

    elif action == "stopforever":
        update_task_status(task_id, "stopped")
        await query.edit_message_text(f"🛑 Permanently stopped reminders for '{task_text}'. You're free!")

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_missing(user_id)
    prefs = get_user_prefs(user_id)
    schedule_str = ", ".join(prefs['schedule'])
    max_reminders = prefs['max_reminders']
    stop_on_response = prefs['stop_on_response']

    await update.message.reply_text(
        f"👋 Welcome to @TaskPulseBot!\n\n"
        f"I'll remind you about your tasks intelligently.\n\n"
        f"📌 *Rules*:\n"
        f"- I'll remind you up to {max_reminders} times per task per day.\n"
        f"- Reminders are sent at: {schedule_str}\n"
        f"- If you respond to any option, I'll stop reminding you for today.\n"
        f"- 'Skip for today' → I'll remind you again tomorrow.\n"
        f"- 'Stop Reminders Forever' → Never bothered again.\n\n"
        f"✨ Commands:\n"
        f"/add [task] — Add a task\n"
        f"/schedule [times] — Set reminder times (e.g., /schedule 09:00,15:00)\n"
        f"/maxreminders N — Change max reminders per day (default: 3)\n"
        f"/stoponresponse [on/off] — Toggle auto-stop after response (default: on)\n"
        f"/list — View active tasks\n"
        f"/clear — Clear all tasks\n\n"
        f"Type /add to begin!",
        parse_mode="Markdown"
    )

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /add [your task here]")
        return
    task_text = " ".join(context.args)
    task_id = add_task(user_id, task_text)
    await update.message.reply_text(f"✅ Added: \"{task_text}\"")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_active_tasks(user_id)
    if not tasks:
        await update.message.reply_text("No active tasks. Use /add to create one!")
        return
    msg = "📝 Your active tasks:\n"
    for tid, text, count, _ in tasks:
        msg += f"\n• {text} ({count}/3 reminders used)"
    await update.message.reply_text(msg)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /schedule 09:00,14:00,18:00")
        return
    times_str = ",".join(context.args)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET schedule = ? WHERE user_id = ?", (times_str, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"📅 Reminder times set to: {times_str}")

async def maxreminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /maxreminders 3 (number between 1-5)")
        return
    max_rem = int(context.args[0])
    if max_rem < 1 or max_rem > 5:
        await update.message.reply_text("Please choose between 1 and 5 reminders per day.")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET max_reminders = ? WHERE user_id = ?", (max_rem, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Max reminders per task set to: {max_rem}")

async def stoponresponse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Usage: /stoponresponse on|off")
        return
    flag = context.args[0].lower() == 'on'
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET stop_on_response = ? WHERE user_id = ?", (flag, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🔄 Auto-stop after response: {'ON' if flag else 'OFF'}")

async def clear_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = 'cleared' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ All tasks cleared!")

# Main function
def main():
    # Initialize bot
    import logging
import sqlite3
import time
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_NAME = "taskpulse.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            schedule TEXT DEFAULT '10:00,14:00,18:00',
            max_reminders INTEGER DEFAULT 3,
            stop_on_response BOOLEAN DEFAULT TRUE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            created_at TIMESTAMP,
            status TEXT DEFAULT 'active',  -- active, completed, skipped, stopped
            last_reminded TIMESTAMP NULL,
            reminder_count INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Helper functions
def get_user_prefs(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT schedule, max_reminders, stop_on_response FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'schedule': row[0].split(',') if row[0] else ['10:00', '14:00', '18:00'],
            'max_reminders': row[1],
            'stop_on_response': row[2]
        }
    return {'schedule': ['10:00', '14:00', '18:00'], 'max_reminders': 3, 'stop_on_response': True}

def add_user_if_missing(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_task(user_id, text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, created_at) VALUES (?, ?, ?)",
              (user_id, text.strip(), datetime.now()))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_active_tasks(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        SELECT id, text, reminder_count, last_reminded 
        FROM tasks 
        WHERE user_id = ? AND status = 'active'
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_task_status(task_id, status, new_reminder_count=None, new_last_reminded=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if new_reminder_count is not None:
        c.execute("UPDATE tasks SET reminder_count = ?, last_reminded = ? WHERE id = ?", 
                  (new_reminder_count, new_last_reminded, task_id))
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()

def get_task_text(task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT text FROM tasks WHERE id = ?", (task_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else ""

def get_tz_time(time_str, tz_name="UTC"):
    """Convert HH:MM string to timezone-aware datetime for today"""
    now = datetime.now(pytz.timezone(tz_name))
    hour, minute = map(int, time_str.split(':'))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: check and send reminders to all users"""
    users_to_remind = {}
    now = datetime.now(pytz.UTC)
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Get all active users and their schedules
    c.execute("SELECT user_id, schedule FROM users")
    for user_id, schedule_str in c.fetchall():
        schedule = schedule_str.split(',') if schedule_str else ['10:00', '14:00', '18:00']
        for timestr in schedule:
            try:
                remind_time = get_tz_time(timestr, "UTC")
                # If today's reminder time has passed, skip
                if remind_time > now:
                    continue
                # Check if we already sent a reminder today for this user+time
                # We'll check all tasks and see which ones are due for a reminder
                c.execute("""
                    SELECT id, text, reminder_count FROM tasks 
                    WHERE user_id = ? AND status = 'active' AND reminder_count < ?
                """, (user_id, 3))  # Max 3 reminders per task per day
                tasks = c.fetchall()
                if tasks:
                    if user_id not in users_to_remind:
                        users_to_remind[user_id] = []
                    users_to_remind[user_id].extend(tasks)
            except Exception as e:
                logger.error(f"Error parsing time {timestr}: {e}")
    
    conn.close()
    
    for user_id, tasks in users_to_remind.items():
        # Only send one reminder per user per scheduler run (to avoid spam)
        if tasks:
            task_id, task_text, count = tasks[0]  # Pick first task
            keyboard = [
                [InlineKeyboardButton("✅ Completed", callback_data=f"complete:{task_id}")],
                [InlineKeyboardButton("⏳ Skip for today", callback_data=f"skip:{task_id}")],
                [InlineKeyboardButton("⏱️ Delay 2h", callback_data=f"delay:{task_id}")],
                [InlineKeyboardButton("🛑 Stop Reminders Forever", callback_data=f"stopforever:{task_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Task Reminder: \"{task_text}\"\n\nWhat are you doing right now?",
                    reply_markup=reply_markup
                )
                # Log that we reminded this task
                update_task_status(task_id, 'active', count + 1, now.isoformat())
                logger.info(f"Sent reminder to {user_id} for task {task_id}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {user_id}: {e}")

# Handle button presses
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[0]
    task_id = int(parts[1])
    user_id = query.from_user.id

    task_text = get_task_text(task_id)

    if action == "complete":
        update_task_status(task_id, "completed")
        await query.edit_message_text(f"✅ Great job! Task '{task_text}' marked as completed.")
        
    elif action == "skip":
        update_task_status(task_id, "skipped")  # Will be reactivated tomorrow
        await query.edit_message_text(f"⏳ Skipped '{task_text}' for today. I'll remind you again tomorrow!")

    elif action == "delay":
        # Delay by 2 hours
        delay_until = datetime.now(pytz.UTC) + timedelta(hours=2)
        update_task_status(task_id, "active", 0, delay_until.isoformat())  # Reset counter
        await query.edit_message_text(f"⏱️ Delayed '{task_text}' until {delay_until.strftime('%H:%M')}.")

    elif action == "stopforever":
        update_task_status(task_id, "stopped")
        await query.edit_message_text(f"🛑 Permanently stopped reminders for '{task_text}'. You're free!")

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_missing(user_id)
    prefs = get_user_prefs(user_id)
    schedule_str = ", ".join(prefs['schedule'])
    max_reminders = prefs['max_reminders']
    stop_on_response = prefs['stop_on_response']

    await update.message.reply_text(
        f"👋 Welcome to @TaskPulseBot!\n\n"
        f"I'll remind you about your tasks intelligently.\n\n"
        f"📌 *Rules*:\n"
        f"- I'll remind you up to {max_reminders} times per task per day.\n"
        f"- Reminders are sent at: {schedule_str}\n"
        f"- If you respond to any option, I'll stop reminding you for today.\n"
        f"- 'Skip for today' → I'll remind you again tomorrow.\n"
        f"- 'Stop Reminders Forever' → Never bothered again.\n\n"
        f"✨ Commands:\n"
        f"/add [task] — Add a task\n"
        f"/schedule [times] — Set reminder times (e.g., /schedule 09:00,15:00)\n"
        f"/maxreminders N — Change max reminders per day (default: 3)\n"
        f"/stoponresponse [on/off] — Toggle auto-stop after response (default: on)\n"
        f"/list — View active tasks\n"
        f"/clear — Clear all tasks\n\n"
        f"Type /add to begin!",
        parse_mode="Markdown"
    )

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /add [your task here]")
        return
    task_text = " ".join(context.args)
    task_id = add_task(user_id, task_text)
    await update.message.reply_text(f"✅ Added: \"{task_text}\"")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_active_tasks(user_id)
    if not tasks:
        await update.message.reply_text("No active tasks. Use /add to create one!")
        return
    msg = "📝 Your active tasks:\n"
    for tid, text, count, _ in tasks:
        msg += f"\n• {text} ({count}/3 reminders used)"
    await update.message.reply_text(msg)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /schedule 09:00,14:00,18:00")
        return
    times_str = ",".join(context.args)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET schedule = ? WHERE user_id = ?", (times_str, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"📅 Reminder times set to: {times_str}")

async def maxreminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /maxreminders 3 (number between 1-5)")
        return
    max_rem = int(context.args[0])
    if max_rem < 1 or max_rem > 5:
        await update.message.reply_text("Please choose between 1 and 5 reminders per day.")
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET max_reminders = ? WHERE user_id = ?", (max_rem, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Max reminders per task set to: {max_rem}")

async def stoponresponse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or context.args[0].lower() not in ['on', 'off']:
        await update.message.reply_text("Usage: /stoponresponse on|off")
        return
    flag = context.args[0].lower() == 'on'
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET stop_on_response = ? WHERE user_id = ?", (flag, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🔄 Auto-stop after response: {'ON' if flag else 'OFF'}")

async def clear_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = 'cleared' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ All tasks cleared!")



    # Main function
def main():
    # Initialize bot
    TOKEN = "YOUR_BOT_TOKEN_HERE"  # <-- DELETE THIS ENTIRE LINE
    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_task_command))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("maxreminders", maxreminders_command))
    application.add_handler(CommandHandler("stoponresponse", stoponresponse_command))
    application.add_handler(CommandHandler("clear", clear_tasks))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Schedule reminders every minute (to check if it's time to remind)
    scheduler = BackgroundScheduler(timezone=pytz.UTC)
    scheduler.add_job(send_reminders, 'interval', minutes=1, args=[application])
    scheduler.start()

    # Start bot
    print("🚀 TaskPulseBot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_task_command))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("maxreminders", maxreminders_command))
    application.add_handler(CommandHandler("stoponresponse", stoponresponse_command))
    application.add_handler(CommandHandler("clear", clear_tasks))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Schedule reminders every minute (to check if it's time to remind)
    scheduler = BackgroundScheduler(timezone=pytz.UTC)
    scheduler.add_job(send_reminders, 'interval', minutes=1, args=[application])
    scheduler.start()

    # Start bot
    print("🚀 TaskPulseBot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
