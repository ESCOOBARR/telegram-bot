import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

# ==================== إعدادات ====================
TOKEN = 8922197031:AAGyO-wkhXHCYavJeNCfWTNXvWrL16Kan6A  # ← حط التوكن الجديد هنا
GROUP_IDS = [-1002520349216, -1002134124297]   # ← حط ID الجروب هنا (رقم سالب مثلاً -1001234567890)
ADMIN_IDS = 7146124025  # ← حط الـ ID بتاعك هنا
SUBSCRIPTION_DAYS = 29
# =================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== قاعدة البيانات ====================
def init_db():
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            join_date TEXT,
            expiry_date TEXT,
            warned INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def add_subscriber(user_id, username, full_name):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    join_date = datetime.now()
    expiry_date = join_date + timedelta(days=SUBSCRIPTION_DAYS)
    c.execute("""
        INSERT OR REPLACE INTO subscribers (user_id, username, full_name, join_date, expiry_date, warned)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (user_id, username, full_name, join_date.isoformat(), expiry_date.isoformat()))
    conn.commit()
    conn.close()
    return expiry_date

def get_all_subscribers():
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("SELECT * FROM subscribers")
    rows = c.fetchall()
    conn.close()
    return rows

def remove_subscriber(user_id):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def mark_warned(user_id):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("UPDATE subscribers SET warned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ==================== أوامر الأدمين ====================
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ مش عندك صلاحية.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /add <user_id> <الاسم>")
        return

    try:
        user_id = int(context.args[0])
        full_name = " ".join(context.args[1:]) if len(context.args) > 1 else "غير معروف"
        expiry = add_subscriber(user_id, "", full_name)
        await update.message.reply_text(
            f"✅ تم إضافة المشترك!\n"
            f"👤 {full_name}\n"
            f"🆔 {user_id}\n"
            f"📅 ينتهي الاشتراك: {expiry.strftime('%Y-%m-%d')}"
        )
    except ValueError:
        await update.message.reply_text("❌ الـ user_id لازم يكون رقم.")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ مش عندك صلاحية.")
        return

    subs = get_all_subscribers()
    if not subs:
        await update.message.reply_text("📭 مفيش مشتركين حالياً.")
        return

    now = datetime.now()
    msg = "📋 قائمة المشتركين:\n\n"
    for s in subs:
        user_id, username, full_name, join_date, expiry_date, warned = s
        expiry = datetime.fromisoformat(expiry_date)
        days_left = (expiry - now).days
        status = "✅" if days_left > 3 else "⚠️" if days_left > 0 else "❌"
        msg += f"{status} {full_name} | ID: {user_id}\n"
        msg += f"   باقي: {days_left} يوم | ينتهي: {expiry.strftime('%Y-%m-%d')}\n\n"

    await update.message.reply_text(msg)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ مش عندك صلاحية.")
        return

    if not context.args:
        await update.message.reply_text("الاستخدام: /remove <user_id>")
        return

    try:
        user_id = int(context.args[0])
        try:
            await context.bot.ban_chat_member(GROUP_ID, user_id)
            await context.bot.unban_chat_member(GROUP_ID, user_id)
        except Exception:
            pass
        remove_subscriber(user_id)
        await update.message.reply_text(f"✅ تم إزالة المشترك {user_id}.")
    except ValueError:
        await update.message.reply_text("❌ الـ user_id لازم يكون رقم.")

# ==================== الفحص اليومي ====================
async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    subs = get_all_subscribers()
    now = datetime.now()

    for s in subs:
        user_id, username, full_name, join_date, expiry_date, warned = s
        expiry = datetime.fromisoformat(expiry_date)
        days_left = (expiry - now).days

        # تحذير قبل 3 أيام
        if days_left <= 3 and days_left > 0 and not warned:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ {full_name}، اشتراكك هينتهي بعد {days_left} يوم!\n"
                         f"جدد اشتراكك عشان متتطردش من الجروب."
                )
                mark_warned(user_id)
            except Exception as e:
                logger.warning(f"مقدرتش ابعت تحذير لـ {user_id}: {e}")

        # طرد بعد انتهاء الاشتراك
        elif days_left <= 0:
            try:
                await context.bot.ban_chat_member(GROUP_ID, user_id)
                await context.bot.unban_chat_member(GROUP_ID, user_id)  # بان مؤقت = طرد بدون حظر
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ {full_name}، انتهى اشتراكك وتم إخراجك من الجروب.\n"
                         f"تقدر تجدد اشتراكك وترجع تاني!"
                )
                logger.info(f"تم طرد {full_name} ({user_id})")
            except Exception as e:
                logger.warning(f"مقدرتش أطرد {user_id}: {e}")
            finally:
                remove_subscriber(user_id)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً! أنا بوت إدارة الاشتراكات.\n\n"
        "📌 أوامر الأدمين:\n"
        "/add <user_id> <الاسم> - إضافة مشترك\n"
        "/list - عرض كل المشتركين\n"
        "/remove <user_id> - حذف مشترك يدوياً"
    )

# ==================== التشغيل ====================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("remove", remove_command))

    # الفحص اليومي كل 24 ساعة
    app.job_queue.run_repeating(daily_check, interval=86400, first=10)

    logger.info("البوت شغال! ✅")
    app.run_polling()

if __name__ == "__main__":
    main()
