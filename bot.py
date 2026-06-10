import logging
import sqlite3
import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ChatMemberHandler
)
from telegram import ChatMember

# ==================== إعدادات ====================
TOKEN = os.environ.get("TOKEN")
GROUP_IDS = [int(x) for x in os.environ.get("GROUP_IDS", "").split(",") if x]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
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

def add_subscriber(user_id, username, full_name, join_date=None):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    if join_date is None:
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

def is_subscribed(user_id):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM subscribers WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

# ==================== تسجيل أوتوماتيك ====================
async def member_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result.chat.id not in GROUP_IDS:
        return

    new_member = result.new_chat_member
    old_member = result.old_chat_member

    if (old_member.status in [ChatMember.LEFT, ChatMember.BANNED] and
            new_member.status == ChatMember.MEMBER):

        user = new_member.user
        if user.is_bot:
            return

        if not is_subscribed(user.id):
            full_name = f"{user.first_name} {user.last_name or ''}".strip()
            expiry = add_subscriber(user.id, user.username or "", full_name)
            logger.info(f"تم تسجيل {full_name} ({user.id}) أوتوماتيك")

            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"👋 أهلاً {full_name}!\n\n"
                         f"✅ تم تسجيل اشتراكك أوتوماتيك.\n"
                         f"📅 اشتراكك ينتهي في: {expiry.strftime('%Y-%m-%d')}\n\n"
                         f"⚠️ هيتبعتلك تحذير قبل الانتهاء بـ 3 أيام."
                )
            except Exception as e:
                logger.warning(f"مقدرتش ابعت رسالة ترحيب لـ {user.id}: {e}")

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

async def adddate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إضافة مشترك بتاريخ قديم: /adddate <user_id> <الاسم> <YYYY-MM-DD>"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ مش عندك صلاحية.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("الاستخدام: /adddate <user_id> <الاسم> <YYYY-MM-DD>\nمثال: /adddate 123456789 أحمد 2026-05-21")
        return

    try:
        user_id = int(context.args[0])
        date_str = context.args[-1]
        full_name = " ".join(context.args[1:-1])
        join_date = datetime.strptime(date_str, "%Y-%m-%d")
        expiry = add_subscriber(user_id, "", full_name, join_date)
        days_left = (expiry - datetime.now()).days
        await update.message.reply_text(
            f"✅ تم إضافة المشترك بتاريخ قديم!\n"
            f"👤 {full_name}\n"
            f"🆔 {user_id}\n"
            f"📅 تاريخ الانضمام: {join_date.strftime('%Y-%m-%d')}\n"
            f"📅 ينتهي الاشتراك: {expiry.strftime('%Y-%m-%d')}\n"
            f"⏳ باقي: {days_left} يوم"
        )
    except ValueError:
        await update.message.reply_text("❌ التاريخ غلط! لازم يكون بالشكل ده: YYYY-MM-DD\nمثال: 2026-05-21")

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
        for gid in GROUP_IDS:
            try:
                await context.bot.ban_chat_member(gid, user_id)
                await context.bot.unban_chat_member(gid, user_id)
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

        # تحذير قبل 3 أيام - في الخاص وفي الجروبين
        if days_left <= 3 and days_left > 0 and not warned:
            warning_msg = (
                f"⚠️ تنبيه لـ {full_name}\n\n"
                f"اشتراكك هينتهي بعد {days_left} يوم!\n"
                f"جدد اشتراكك عشان متتطردش من الجروب. 🙏"
            )
            # ابعت في الخاص
            try:
                await context.bot.send_message(chat_id=user_id, text=warning_msg)
            except Exception as e:
                logger.warning(f"مقدرتش ابعت خاص لـ {user_id}: {e}")

            # ابعت في كل الجروبات
            for gid in GROUP_IDS:
                try:
                    await context.bot.send_message(chat_id=gid, text=warning_msg)
                except Exception as e:
                    logger.warning(f"مقدرتش ابعت في الجروب {gid}: {e}")

            mark_warned(user_id)

        # طرد بعد انتهاء الاشتراك
        elif days_left <= 0:
            for gid in GROUP_IDS:
                try:
                    await context.bot.ban_chat_member(gid, user_id)
                    await context.bot.unban_chat_member(gid, user_id)
                except Exception as e:
                    logger.warning(f"مقدرتش أطرد {user_id} من {gid}: {e}")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ {full_name}، انتهى اشتراكك وتم إخراجك من الجروب.\n"
                         f"تقدر تجدد اشتراكك وترجع تاني!"
                )
            except Exception:
                pass
            remove_subscriber(user_id)
            logger.info(f"تم طرد {full_name} ({user_id})")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً! أنا بوت إدارة الاشتراكات.\n\n"
        "📌 أوامر الأدمين:\n"
        "/add <user_id> <الاسم> - إضافة مشترك من النهارده\n"
        "/adddate <user_id> <الاسم> <YYYY-MM-DD> - إضافة بتاريخ قديم\n"
        "/list - عرض كل المشتركين\n"
        "/remove <user_id> - حذف مشترك يدوياً"
    )

# ==================== التشغيل ====================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("adddate", adddate_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(ChatMemberHandler(member_joined, ChatMemberHandler.CHAT_MEMBER))

    app.job_queue.run_repeating(daily_check, interval=86400, first=10)

    logger.info("البوت شغال! ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
