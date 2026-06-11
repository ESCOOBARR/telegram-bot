import logging
import sqlite3
import os
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, ChatMember
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ChatMemberHandler, MessageHandler, filters,
    ConversationHandler
)

# ==================== إعدادات ====================
TOKEN = os.environ.get("TOKEN")
GROUP_IDS = [int(x) for x in os.environ.get("GROUP_IDS", "").split(",") if x]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
SUBSCRIPTION_DAYS = 29

# مراحل المحادثة
WAIT_ID, WAIT_RECEIPT, WAIT_DATE_ID, WAIT_DATE, WAIT_REMOVE_ID, WAIT_GETRECEIPT_ID = range(6)
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_id TEXT,
            date TEXT
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

def save_receipt(user_id, file_id):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("INSERT INTO receipts (user_id, file_id, date) VALUES (?, ?, ?)",
              (user_id, file_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_receipts(user_id):
    conn = sqlite3.connect("subscribers.db")
    c = conn.cursor()
    c.execute("SELECT file_id, date FROM receipts WHERE user_id = ? ORDER BY date DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# ==================== إشعار الأدمين ====================
async def notify_admin(context, user, message_text=None):
    username = f"@{user.username}" if user.username else "مفيش يوزر"
    full_name = f"{user.first_name} {user.last_name or ''}".strip()
    
    text = (
        f"👀 شخص فتح البوت أو بعت رسالة!\n\n"
        f"👤 الاسم: {full_name}\n"
        f"🆔 ID: {user.id}\n"
        f"📛 يوزر: {username}\n"
    )
    if message_text:
        text += f"💬 الرسالة: {message_text}"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logger.warning(f"مقدرتش ابعت نوتيفيكيشن للأدمين {admin_id}: {e}")

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

# ==================== الأزرار الرئيسية ====================
def main_keyboard():
    keyboard = [
        ["➕ إضافة مشترك", "📅 إضافة بتاريخ قديم"],
        ["📋 قائمة المشتركين", "❌ حذف مشترك"],
        ["🧾 إيصالات الدفع", "🔗 إنشاء رابط"],
        ["🚫 إلغاء"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ==================== إنشاء رابط دعوة ====================
async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")
        return

    if not GROUP_IDS:
        await update.message.reply_text("❌ مفيش جروبات مضافة.")
        return

    msg = "🔗 روابط الدعوة (استخدام مرة واحدة):\n\n"
    for gid in GROUP_IDS:
        try:
            link = await context.bot.create_chat_invite_link(
                chat_id=gid,
                member_limit=1,
                name="رابط اشتراك"
            )
            chat = await context.bot.get_chat(gid)
            msg += f"📌 {chat.title}:\n{link.invite_link}\n\n"
        except Exception as e:
            msg += f"❌ مقدرتش أعمل رابط للجروب {gid}: {e}\n\n"

    await update.message.reply_text(msg, reply_markup=main_keyboard())

# ==================== START ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        # ابعت نوتيفيكيشن للأدمين
        await notify_admin(context, user, "/start")
        await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")
        return
    await update.message.reply_text(
        "👋 أهلاً! أنا بوت إدارة الاشتراكات.\n\nاختر من الأزرار:",
        reply_markup=main_keyboard()
    )

# ==================== إشعار لما حد غريب يبعت رسالة ====================
async def unknown_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # بس في الخاص مش في الجروب
    if update.effective_chat.type != "private":
        return
    if user.id not in ADMIN_IDS:
        msg = update.message.text or update.message.caption or "📎 ملف أو صورة"
        await notify_admin(context, user, msg)
        await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")

# ==================== إلغاء ====================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("🚫 تم الإلغاء.", reply_markup=main_keyboard())
    return ConversationHandler.END

# ==================== ADD (خطوة بخطوة) ====================
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "📲 ابعتلي الـ ID بتاع المشترك:\n\nأو اضغط 🚫 إلغاء للرجوع.",
        reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
    )
    return WAIT_ID

async def add_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    try:
        user_id = int(update.message.text.strip())
        context.user_data["new_user_id"] = user_id
        await update.message.reply_text(
            "🖼 تمام! دلوقتي ابعتلي صورة الإيصال:\n\nأو اضغط 🚫 إلغاء للرجوع.",
            reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
        )
        return WAIT_RECEIPT
    except ValueError:
        await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")
        return WAIT_ID

async def add_got_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    user_id = context.user_data.get("new_user_id")
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("❌ ابعت صورة الإيصال:")
        return WAIT_RECEIPT
    expiry = add_subscriber(user_id, "", str(user_id))
    save_receipt(user_id, file_id)
    await update.message.reply_text(
        f"✅ تم تسجيل المشترك وحفظ الإيصال!\n"
        f"🆔 ID: {user_id}\n"
        f"📅 ينتهي الاشتراك: {expiry.strftime('%Y-%m-%d')}",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END

# ==================== ADD DATE (خطوة بخطوة) ====================
async def adddate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "📲 ابعتلي الـ ID بتاع المشترك:\n\nأو اضغط 🚫 إلغاء للرجوع.",
        reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
    )
    return WAIT_DATE_ID

async def adddate_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    try:
        user_id = int(update.message.text.strip())
        context.user_data["date_user_id"] = user_id
        await update.message.reply_text(
            "📅 ابعتلي تاريخ الانضمام:\nYYYY-MM-DD\nمثال: 2026-05-20\n\nأو اضغط 🚫 إلغاء للرجوع.",
            reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
        )
        return WAIT_DATE
    except ValueError:
        await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")
        return WAIT_DATE_ID

async def adddate_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    user_id = context.user_data.get("date_user_id")
    try:
        join_date = datetime.strptime(update.message.text.strip(), "%Y-%m-%d")
        expiry = add_subscriber(user_id, "", str(user_id), join_date)
        days_left = (expiry - datetime.now()).days
        await update.message.reply_text(
            f"✅ تم إضافة المشترك بتاريخ قديم!\n"
            f"🆔 ID: {user_id}\n"
            f"📅 تاريخ الانضمام: {join_date.strftime('%Y-%m-%d')}\n"
            f"📅 ينتهي الاشتراك: {expiry.strftime('%Y-%m-%d')}\n"
            f"⏳ باقي: {days_left} يوم",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ التاريخ غلط! لازم يكون:\nYYYY-MM-DD\nمثال: 2026-05-20")
        return WAIT_DATE

# ==================== REMOVE (خطوة بخطوة) ====================
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "🆔 ابعتلي الـ ID بتاع المشترك اللي عايز تشيله:\n\nأو اضغط 🚫 إلغاء للرجوع.",
        reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
    )
    return WAIT_REMOVE_ID

async def remove_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    try:
        user_id = int(update.message.text.strip())
        for gid in GROUP_IDS:
            try:
                await context.bot.ban_chat_member(gid, user_id)
                await context.bot.unban_chat_member(gid, user_id)
            except Exception:
                pass
        remove_subscriber(user_id)
        await update.message.reply_text(
            f"✅ تم إزالة المشترك {user_id} من الجروبين.",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")
        return WAIT_REMOVE_ID

# ==================== GET RECEIPT (خطوة بخطوة) ====================
async def getreceipt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "🆔 ابعتلي الـ ID بتاع المشترك:\n\nأو اضغط 🚫 إلغاء للرجوع.",
        reply_markup=ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)
    )
    return WAIT_GETRECEIPT_ID

async def getreceipt_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🚫 إلغاء":
        return await cancel(update, context)
    try:
        user_id = int(update.message.text.strip())

        # تفاصيل الاشتراك
        conn = sqlite3.connect("subscribers.db")
        c = conn.cursor()
        c.execute("SELECT * FROM subscribers WHERE user_id = ?", (user_id,))
        sub = c.fetchone()
        conn.close()

        if sub:
            _, username, full_name, join_date, expiry_date, warned = sub
            expiry = datetime.fromisoformat(expiry_date)
            days_left = (expiry - datetime.now()).days
            status = "✅ نشط" if days_left > 3 else "⚠️ قارب على الانتهاء" if days_left > 0 else "❌ منتهي"
            await update.message.reply_text(
                f"📋 تفاصيل المشترك:\n\n"
                f"🆔 ID: {user_id}\n"
                f"📅 تاريخ الانضمام: {datetime.fromisoformat(join_date).strftime('%Y-%m-%d')}\n"
                f"📅 ينتهي في: {expiry.strftime('%Y-%m-%d')}\n"
                f"⏳ باقي: {days_left} يوم\n"
                f"📊 الحالة: {status}"
            )
        else:
            await update.message.reply_text(f"⚠️ المشترك {user_id} مش موجود في القاعدة.")

        # الإيصالات
        receipts = get_receipts(user_id)
        if receipts:
            await update.message.reply_text(f"🧾 الإيصالات ({len(receipts)}):")
            for file_id, date in receipts:
                date_fmt = datetime.fromisoformat(date).strftime('%Y-%m-%d %H:%M')
                await update.message.reply_photo(
                    photo=file_id,
                    caption=f"📅 تاريخ الحفظ: {date_fmt}"
                )
        else:
            await update.message.reply_text("📭 مفيش إيصالات محفوظة لهذا المشترك.")

        await update.message.reply_text("✅ تم.", reply_markup=main_keyboard())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")
        return WAIT_GETRECEIPT_ID

# ==================== LIST ====================
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
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

# ==================== الفحص اليومي ====================
async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    subs = get_all_subscribers()
    now = datetime.now()
    for s in subs:
        user_id, username, full_name, join_date, expiry_date, warned = s
        expiry = datetime.fromisoformat(expiry_date)
        days_left = (expiry - now).days
        if days_left <= 3 and days_left > 0 and not warned:
            warning_msg = (
                f"⚠️ تنبيه لـ {full_name}\n\n"
                f"اشتراكك هينتهي بعد {days_left} يوم!\n"
                f"جدد اشتراكك عشان متتطردش من الجروب. 🙏"
            )
            try:
                await context.bot.send_message(chat_id=user_id, text=warning_msg)
            except Exception as e:
                logger.warning(f"مقدرتش ابعت خاص لـ {user_id}: {e}")
            for gid in GROUP_IDS:
                try:
                    await context.bot.send_message(chat_id=gid, text=warning_msg)
                except Exception as e:
                    logger.warning(f"مقدرتش ابعت في الجروب {gid}: {e}")
            mark_warned(user_id)
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

# ==================== التشغيل ====================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex("^➕ إضافة مشترك$"), add_start)
        ],
        states={
            WAIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_id)],
            WAIT_RECEIPT: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, add_got_receipt),
                MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel)
        ],
    )

    adddate_conv = ConversationHandler(
        entry_points=[
            CommandHandler("adddate", adddate_start),
            MessageHandler(filters.Regex("^📅 إضافة بتاريخ قديم$"), adddate_start)
        ],
        states={
            WAIT_DATE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddate_got_id)],
            WAIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adddate_got_date)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel)
        ],
    )

    remove_conv = ConversationHandler(
        entry_points=[
            CommandHandler("remove", remove_start),
            MessageHandler(filters.Regex("^❌ حذف مشترك$"), remove_start)
        ],
        states={
            WAIT_REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_got_id)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel)
        ],
    )

    getreceipt_conv = ConversationHandler(
        entry_points=[
            CommandHandler("getreceipt", getreceipt_start),
            MessageHandler(filters.Regex("^🧾 إيصالات الدفع$"), getreceipt_start)
        ],
        states={
            WAIT_GETRECEIPT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, getreceipt_got_id)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel)
        ],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Regex("^📋 قائمة المشتركين$"), list_command))
    app.add_handler(MessageHandler(filters.Regex("^🔗 إنشاء رابط$"), create_link))
    app.add_handler(MessageHandler(filters.Regex("^🚫 إلغاء$"), cancel))
    app.add_handler(add_conv)
    app.add_handler(adddate_conv)
    app.add_handler(remove_conv)
    app.add_handler(getreceipt_conv)
    app.add_handler(ChatMemberHandler(member_joined, ChatMemberHandler.CHAT_MEMBER))

    # إشعار الأدمين لما حد غريب يبعت رسالة
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        unknown_user_message
    ))

    app.job_queue.run_repeating(daily_check, interval=86400, first=10)

    logger.info("البوت شغال! ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
