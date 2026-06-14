import logging
import os
import io
import json
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, ReplyKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ChatMemberHandler, MessageHandler, filters
)

# ==================== إعدادات ====================
TOKEN = os.environ.get("TOKEN")
GROUP_IDS = [int(x) for x in os.environ.get("GROUP_IDS", "").split(",") if x]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
SUBSCRIPTION_DAYS = 29
DATABASE_URL = os.environ.get("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== قاعدة البيانات ====================
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
        join_date TEXT, expiry_date TEXT, warned INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS receipts (
        id SERIAL PRIMARY KEY, user_id BIGINT, file_id TEXT, date TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS visitors (
        user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
        first_seen TEXT, last_seen TEXT)""")
    conn.commit()
    conn.close()

def add_subscriber(user_id, username, full_name, join_date=None):
    conn = get_conn()
    c = conn.cursor()
    if join_date is None:
        join_date = datetime.now()
    expiry_date = join_date + timedelta(days=SUBSCRIPTION_DAYS)
    c.execute("""INSERT INTO subscribers (user_id, username, full_name, join_date, expiry_date, warned)
        VALUES (%s,%s,%s,%s,%s,0)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username,
        full_name=EXCLUDED.full_name, join_date=EXCLUDED.join_date,
        expiry_date=EXCLUDED.expiry_date, warned=0""",
        (user_id, username, full_name, join_date.isoformat(), expiry_date.isoformat()))
    conn.commit()
    conn.close()
    return expiry_date

def add_subscriber_with_expiry(user_id, username, full_name, expiry_date):
    conn = get_conn()
    c = conn.cursor()
    join_date = datetime.now()
    c.execute("""INSERT INTO subscribers (user_id, username, full_name, join_date, expiry_date, warned)
        VALUES (%s,%s,%s,%s,%s,0)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username,
        full_name=EXCLUDED.full_name, join_date=EXCLUDED.join_date,
        expiry_date=EXCLUDED.expiry_date, warned=0""",
        (user_id, username, full_name, join_date.isoformat(), expiry_date.isoformat()))
    conn.commit()
    conn.close()

def get_all_subscribers():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM subscribers")
    rows = c.fetchall()
    conn.close()
    return rows

def remove_subscriber(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM subscribers WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

def mark_warned(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE subscribers SET warned = 1 WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

def is_subscribed(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM subscribers WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def save_receipt(user_id, file_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO receipts (user_id, file_id, date) VALUES (%s,%s,%s)",
              (user_id, file_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_receipts(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT file_id, date FROM receipts WHERE user_id = %s ORDER BY date DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def save_visitor(user_id, username, full_name):
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""INSERT INTO visitors (user_id, username, full_name, first_seen, last_seen)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username,
        full_name=EXCLUDED.full_name, last_seen=EXCLUDED.last_seen""",
        (user_id, username, full_name, now, now))
    conn.commit()
    conn.close()

def get_all_visitors():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM visitors ORDER BY last_seen DESC")
    rows = c.fetchall()
    conn.close()
    return rows

# ==================== الأزرار ====================
def main_keyboard():
    keyboard = [
        ["➕ إضافة مشترك", "📅 إضافة بتاريخ قديم"],
        ["📅 إضافة بتاريخ انتهاء معين"],
        ["📋 قائمة المشتركين", "❌ حذف مشترك"],
        ["🧾 إيصالات الدفع", "🔗 إنشاء رابط"],
        ["🔄 تجديد اشتراك", "💳 Pay"],
        ["🔍 كشف مشترك", "👀 الزوار"],
        ["📥 رفع نسخة", "📤 نسخة احتياطية"],
        ["🚫 إلغاء"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def cancel_keyboard():
    return ReplyKeyboardMarkup([["🚫 إلغاء"]], resize_keyboard=True)

def cancel_with_skip_keyboard():
    return ReplyKeyboardMarkup([["-", "🚫 إلغاء"]], resize_keyboard=True)

# ==================== State Management ====================
def set_state(context, state, **kwargs):
    context.user_data.clear()
    context.user_data['state'] = state
    context.user_data.update(kwargs)

def get_state(context):
    return context.user_data.get('state', None)

def clear_state(context):
    context.user_data.clear()

# ==================== إشعار الأدمين ====================
async def notify_admin(context, user, message_text=None):
    username = f"@{user.username}" if user.username else "مفيش يوزر"
    full_name = f"{user.first_name} {user.last_name or ''}".strip()
    profile_link = f'<a href="tg://user?id={user.id}">{full_name}</a>'
    text = (f"👀 شخص فتح البوت أو بعت رسالة!\n\n"
            f"👤 الاسم: {profile_link}\n"
            f"🆔 ID: {user.id}\n"
            f"📛 يوزر: {username}\n")
    if message_text:
        text += f"💬 الرسالة: {message_text}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
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
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"👋 أهلاً {full_name}!\n\n✅ تم تسجيل اشتراكك.\n"
                         f"📅 ينتهي في: {expiry.strftime('%Y-%m-%d')}\n\n"
                         f"⚠️ هيتبعتلك تحذير قبل الانتهاء بـ 3 أيام.")
            except Exception:
                pass

# ==================== START ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    full_name = f"{user.first_name} {user.last_name or ''}".strip()
    save_visitor(user.id, user.username or "", full_name)
    clear_state(context)
    if user.id not in ADMIN_IDS:
        await notify_admin(context, user, "/start")
        await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")
        return
    await update.message.reply_text("👋 أهلاً! أنا بوت إدارة الاشتراكات.\n\nاختر من الأزرار:",
                                     reply_markup=main_keyboard())

# ==================== Handler رئيسي للأزرار ====================
async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return

    # لو مش أدمين
    if user.id not in ADMIN_IDS:
        if update.effective_chat.type == "private":
            full_name = f"{user.first_name} {user.last_name or ''}".strip()
            save_visitor(user.id, user.username or "", full_name)
            msg = update.message.text or update.message.caption or "📎 ملف أو صورة"
            await notify_admin(context, user, msg)
            await update.message.reply_text("⛔ عفواً، أنت لست المطور الخاص بهذا البوت!")
        return

    text = update.message.text or ""
    state = get_state(context)

    # ==================== إلغاء في أي وقت ====================
    if text == "🚫 إلغاء":
        clear_state(context)
        await update.message.reply_text("🚫 تم الإلغاء.", reply_markup=main_keyboard())
        return

    # ==================== الأزرار الرئيسية ====================
    if state is None:
        if text == "➕ إضافة مشترك":
            set_state(context, "ADD_ID")
            await update.message.reply_text(
                "📲 الخطوة 1/4: ابعتلي الـ ID بتاع المشترك:\n\n"
                "👉 اكتب الـ ID مباشرة\n"
                "👉 أو وجّه رسالة منه\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "📅 إضافة بتاريخ قديم":
            set_state(context, "ADDDATE_ID")
            await update.message.reply_text(
                "📲 الخطوة 1/5: ابعتلي الـ ID:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "📅 إضافة بتاريخ انتهاء معين":
            set_state(context, "EXPIRY_ID")
            await update.message.reply_text(
                "📲 الخطوة 1/5: ابعتلي الـ ID:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "📋 قائمة المشتركين":
            await list_command(update, context)

        elif text == "❌ حذف مشترك":
            set_state(context, "REMOVE_ID")
            await update.message.reply_text(
                "🆔 ابعتلي الـ ID بتاع المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "🧾 إيصالات الدفع":
            set_state(context, "RECEIPT_ID")
            await update.message.reply_text(
                "🆔 ابعتلي الـ ID بتاع المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "🔗 إنشاء رابط":
            await create_link(update, context)

        elif text == "💳 Pay":
            await pay_command(update, context)

        elif text == "🔄 تجديد اشتراك":
            set_state(context, "RENEW_ID")
            await update.message.reply_text(
                "🆔 ابعتلي الـ ID بتاع المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "🔍 كشف مشترك":
            set_state(context, "SEARCH_ID")
            await update.message.reply_text(
                "🆔 ابعتلي الـ ID بتاع المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif text == "👀 الزوار":
            await list_visitors(update, context)

        elif text == "📤 نسخة احتياطية":
            await export_backup(update, context)

        elif text == "📥 رفع نسخة":
            set_state(context, "IMPORT_FILE")
            await update.message.reply_text(
                "📎 ابعتلي ملف الـ JSON:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())

        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            await update.message.reply_text(f"file_id: {file_id}")

        return

    # ==================== معالجة الـ States ====================

    # --- ADD ---
    if state == "ADD_ID":
        user_id = None
        if update.message.forward_origin:
            try:
                origin = update.message.forward_origin
                if hasattr(origin, "sender_user") and origin.sender_user:
                    user_id = origin.sender_user.id
            except Exception:
                pass
        if user_id is None:
            try:
                user_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")
                return
        # تحقق لو متسجل بالفعل
        if is_subscribed(user_id):
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT full_name, expiry_date FROM subscribers WHERE user_id = %s", (user_id,))
            sub = c.fetchone()
            conn.close()
            full_name, expiry_date = sub
            expiry = datetime.fromisoformat(expiry_date)
            days_left = (expiry - datetime.now()).days
            await update.message.reply_text(
                f"⚠️ المشترك ده متسجل بالفعل!\n\n"
                f"👤 الاسم: {full_name}\n"
                f"🆔 ID: {user_id}\n"
                f"📅 ينتهي: {expiry.strftime('%Y-%m-%d')}\n"
                f"⏳ باقي: {days_left} يوم\n\n"
                f"عايز تجدد اشتراكه؟ استخدم 🔄 تجديد اشتراك",
                reply_markup=main_keyboard()
            )
            clear_state(context)
            return
        context.user_data['add_id'] = user_id
        context.user_data['state'] = "ADD_NAME"
        await update.message.reply_text(
            f"✅ ID: {user_id}\n\n📝 الخطوة 2/4: اسم المشترك:\n\nأو 🚫 إلغاء",
            reply_markup=cancel_keyboard())

    elif state == "ADD_NAME":
        context.user_data['add_name'] = text.strip()
        context.user_data['state'] = "ADD_USERNAME"
        await update.message.reply_text(
            "📛 الخطوة 3/4: اليوزر:\nمثال: @username\nأو ( - ) لو مفيش\n\nأو 🚫 إلغاء",
            reply_markup=cancel_with_skip_keyboard())

    elif state == "ADD_USERNAME":
        username = text.strip().replace("@", "")
        if username == "-":
            username = ""
        context.user_data['add_username'] = username
        context.user_data['state'] = "ADD_RECEIPT"
        await update.message.reply_text(
            "🖼 الخطوة 4/4: ابعت صورة الإيصال:\n\nأو 🚫 إلغاء",
            reply_markup=cancel_keyboard())

    elif state == "ADD_RECEIPT":
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            await update.message.reply_text("❌ ابعت صورة الإيصال:")
            return
        uid = context.user_data['add_id']
        name = context.user_data['add_name']
        uname = context.user_data['add_username']
        expiry = add_subscriber(uid, uname, name)
        save_receipt(uid, file_id)
        clear_state(context)
        await update.message.reply_text(
            f"✅ تم التسجيل!\n👤 {name}\n📛 @{uname if uname else 'مفيش'}\n🆔 {uid}\n📅 ينتهي: {expiry.strftime('%Y-%m-%d')}",
            reply_markup=main_keyboard())

    # --- ADDDATE ---
    elif state == "ADDDATE_ID":
        try:
            uid = int(text.strip())
            context.user_data['adddate_id'] = uid
            context.user_data['state'] = "ADDDATE_NAME"
            await update.message.reply_text(
                f"✅ ID: {uid}\n\n📝 الخطوة 2/5: اسم المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    elif state == "ADDDATE_NAME":
        context.user_data['adddate_name'] = text.strip()
        context.user_data['state'] = "ADDDATE_USERNAME"
        await update.message.reply_text(
            "📛 الخطوة 3/5: اليوزر:\nأو ( - ) لو مفيش\n\nأو 🚫 إلغاء",
            reply_markup=cancel_with_skip_keyboard())

    elif state == "ADDDATE_USERNAME":
        username = text.strip().replace("@", "")
        if username == "-":
            username = ""
        context.user_data['adddate_username'] = username
        context.user_data['state'] = "ADDDATE_DATE"
        await update.message.reply_text(
            "📅 الخطوة 4/5: تاريخ الانضمام:\nYYYY-MM-DD\nمثال: 2026-05-20\n\nأو 🚫 إلغاء",
            reply_markup=cancel_keyboard())

    elif state == "ADDDATE_DATE":
        try:
            join_date = datetime.strptime(text.strip(), "%Y-%m-%d")
            context.user_data['adddate_date'] = join_date
            context.user_data['state'] = "ADDDATE_RECEIPT"
            await update.message.reply_text(
                "🖼 الخطوة 5/5: ابعت صورة الإيصال:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())
        except ValueError:
            await update.message.reply_text("❌ التاريخ غلط! لازم: YYYY-MM-DD\nمثال: 2026-05-20")

    elif state == "ADDDATE_RECEIPT":
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            await update.message.reply_text("❌ ابعت صورة الإيصال:")
            return
        uid = context.user_data['adddate_id']
        name = context.user_data['adddate_name']
        uname = context.user_data['adddate_username']
        join_date = context.user_data['adddate_date']
        expiry = add_subscriber(uid, uname, name, join_date)
        save_receipt(uid, file_id)
        days_left = (expiry - datetime.now()).days
        clear_state(context)
        await update.message.reply_text(
            f"✅ تم!\n👤 {name}\n📛 @{uname if uname else 'مفيش'}\n🆔 {uid}\n"
            f"📅 انضم: {join_date.strftime('%Y-%m-%d')}\n📅 ينتهي: {expiry.strftime('%Y-%m-%d')}\n⏳ باقي: {days_left} يوم",
            reply_markup=main_keyboard())

    # --- EXPIRY ---
    elif state == "EXPIRY_ID":
        try:
            uid = int(text.strip())
            context.user_data['expiry_id'] = uid
            context.user_data['state'] = "EXPIRY_NAME"
            await update.message.reply_text(
                f"✅ ID: {uid}\n\n📝 الخطوة 2/5: اسم المشترك:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    elif state == "EXPIRY_NAME":
        context.user_data['expiry_name'] = text.strip()
        context.user_data['state'] = "EXPIRY_USERNAME"
        await update.message.reply_text(
            "📛 الخطوة 3/5: اليوزر:\nأو ( - ) لو مفيش\n\nأو 🚫 إلغاء",
            reply_markup=cancel_with_skip_keyboard())

    elif state == "EXPIRY_USERNAME":
        username = text.strip().replace("@", "")
        if username == "-":
            username = ""
        context.user_data['expiry_username'] = username
        context.user_data['state'] = "EXPIRY_DATE"
        await update.message.reply_text(
            "📅 الخطوة 4/5: تاريخ الانتهاء:\nYYYY-MM-DD\nمثال: 2026-12-31\n\nأو 🚫 إلغاء",
            reply_markup=cancel_keyboard())

    elif state == "EXPIRY_DATE":
        try:
            expiry_date = datetime.strptime(text.strip(), "%Y-%m-%d")
            context.user_data['expiry_date'] = expiry_date
            context.user_data['state'] = "EXPIRY_RECEIPT"
            await update.message.reply_text(
                "🖼 الخطوة 5/5: ابعت صورة الإيصال:\n\nأو 🚫 إلغاء",
                reply_markup=cancel_keyboard())
        except ValueError:
            await update.message.reply_text("❌ التاريخ غلط! لازم: YYYY-MM-DD")

    elif state == "EXPIRY_RECEIPT":
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        else:
            await update.message.reply_text("❌ ابعت صورة الإيصال:")
            return
        uid = context.user_data['expiry_id']
        name = context.user_data['expiry_name']
        uname = context.user_data['expiry_username']
        expiry_date = context.user_data['expiry_date']
        add_subscriber_with_expiry(uid, uname, name, expiry_date)
        save_receipt(uid, file_id)
        days_left = (expiry_date - datetime.now()).days
        clear_state(context)
        await update.message.reply_text(
            f"✅ تم!\n👤 {name}\n📛 @{uname if uname else 'مفيش'}\n🆔 {uid}\n"
            f"📅 ينتهي: {expiry_date.strftime('%Y-%m-%d')}\n⏳ باقي: {days_left} يوم",
            reply_markup=main_keyboard())

    # --- REMOVE ---
    elif state == "REMOVE_ID":
        try:
            uid = int(text.strip())
            for gid in GROUP_IDS:
                try:
                    await context.bot.ban_chat_member(gid, uid)
                    await context.bot.unban_chat_member(gid, uid)
                except Exception:
                    pass
            remove_subscriber(uid)
            clear_state(context)
            await update.message.reply_text(f"✅ تم إزالة المشترك {uid}.", reply_markup=main_keyboard())
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    # --- RECEIPT ---
    elif state == "RECEIPT_ID":
        try:
            uid = int(text.strip())
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT * FROM subscribers WHERE user_id = %s", (uid,))
            sub = c.fetchone()
            conn.close()
            if sub:
                _, username, full_name, join_date, expiry_date, warned = sub
                expiry = datetime.fromisoformat(expiry_date)
                days_left = (expiry - datetime.now()).days
                status = "✅ نشط" if days_left > 3 else "⚠️ قارب على الانتهاء" if days_left > 0 else "❌ منتهي"
                uname = f"@{username}" if username else "مفيش يوزر"
                profile_link = f'<a href="tg://user?id={uid}">{full_name}</a>'
                await update.message.reply_text(
                    f"📋 تفاصيل المشترك:\n\n👤 {profile_link}\n📛 {uname}\n🆔 {uid}\n"
                    f"📅 انضم: {datetime.fromisoformat(join_date).strftime('%Y-%m-%d')}\n"
                    f"📅 ينتهي: {expiry.strftime('%Y-%m-%d')}\n⏳ باقي: {days_left} يوم\n📊 {status}",
                    parse_mode="HTML")
            else:
                await update.message.reply_text(f"⚠️ المشترك {uid} مش موجود.")
            receipts = get_receipts(uid)
            if receipts:
                await update.message.reply_text(f"🧾 الإيصالات ({len(receipts)}):")
                for fid, date in receipts:
                    date_fmt = datetime.fromisoformat(date).strftime('%Y-%m-%d %H:%M')
                    await update.message.reply_photo(photo=fid, caption=f"📅 {date_fmt}")
            else:
                await update.message.reply_text("📭 مفيش إيصالات.")
            clear_state(context)
            await update.message.reply_text("✅ تم.", reply_markup=main_keyboard())
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    # --- RENEW ---
    elif state == "RENEW_ID":
        try:
            uid = int(text.strip())
            join_date = datetime.now()
            expiry_date = join_date + timedelta(days=SUBSCRIPTION_DAYS)
            conn = get_conn()
            c = conn.cursor()
            c.execute("UPDATE subscribers SET join_date=%s, expiry_date=%s, warned=0 WHERE user_id=%s",
                      (join_date.isoformat(), expiry_date.isoformat(), uid))
            updated = c.rowcount
            conn.commit()
            conn.close()
            if updated == 0:
                add_subscriber(uid, "", str(uid))
            clear_state(context)
            await update.message.reply_text(
                f"✅ تم التجديد!\n🆔 {uid}\n📅 ينتهي: {expiry_date.strftime('%Y-%m-%d')}",
                reply_markup=main_keyboard())
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    # --- SEARCH ---
    elif state == "SEARCH_ID":
        try:
            uid = int(text.strip())
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT * FROM subscribers WHERE user_id = %s", (uid,))
            sub = c.fetchone()
            conn.close()
            if not sub:
                clear_state(context)
                await update.message.reply_text(f"❌ المشترك {uid} مش موجود.", reply_markup=main_keyboard())
                return
            _, username, full_name, join_date, expiry_date, warned = sub
            expiry = datetime.fromisoformat(expiry_date)
            days_left = (expiry - datetime.now()).days
            status = "✅ نشط" if days_left > 3 else "⚠️ قارب على الانتهاء" if days_left > 0 else "❌ منتهي"
            uname = f"@{username}" if username else "مفيش يوزر"
            profile_link = f'<a href="tg://user?id={uid}">{full_name}</a>'
            clear_state(context)
            await update.message.reply_text(
                f"🔍 تفاصيل المشترك:\n\n👤 {profile_link}\n📛 {uname}\n🆔 {uid}\n"
                f"📅 انضم: {datetime.fromisoformat(join_date).strftime('%Y-%m-%d')}\n"
                f"📅 ينتهي: {expiry.strftime('%Y-%m-%d')}\n⏳ باقي: {days_left} يوم\n📊 {status}",
                reply_markup=main_keyboard(), parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("❌ الـ ID لازم يكون رقم! جرب تاني:")

    # --- IMPORT ---
    elif state == "IMPORT_FILE":
        if update.message.document:
            try:
                file = await context.bot.get_file(update.message.document.file_id)
                file_bytes = await file.download_as_bytearray()
                data = json.loads(file_bytes.decode('utf-8'))
                subs = data.get('subscribers', [])
                receipts = data.get('receipts', [])
                visitors = data.get('visitors', [])
                conn = get_conn()
                c = conn.cursor()
                c.execute("DELETE FROM receipts")
                c.execute("DELETE FROM subscribers")
                c.execute("DELETE FROM visitors")
                for s in subs:
                    c.execute("""INSERT INTO subscribers (user_id, username, full_name, join_date, expiry_date, warned)
                        VALUES (%s,%s,%s,%s,%s,%s)""",
                        (s['user_id'], s['username'], s['full_name'], s['join_date'], s['expiry_date'], s.get('warned', 0)))
                for r in receipts:
                    c.execute("INSERT INTO receipts (user_id, file_id, date) VALUES (%s,%s,%s)",
                              (r['user_id'], r['file_id'], r['date']))
                for v in visitors:
                    c.execute("""INSERT INTO visitors (user_id, username, full_name, first_seen, last_seen)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING""",
                        (v['user_id'], v['username'], v['full_name'], v['first_seen'], v['last_seen']))
                conn.commit()
                conn.close()
                clear_state(context)
                await update.message.reply_text(
                    f"✅ تم الاستعادة!\n👥 {len(subs)} مشترك\n🧾 {len(receipts)} إيصال\n👀 {len(visitors)} زائر",
                    reply_markup=main_keyboard())
            except Exception as e:
                await update.message.reply_text(f"❌ حصل خطأ: {e}")
        else:
            await update.message.reply_text("❌ ابعت ملف JSON.")

# ==================== قائمة المشتركين ====================
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = get_all_subscribers()
    if not subs:
        await update.message.reply_text("📭 مفيش مشتركين.")
        return
    now = datetime.now()
    msg = f"📋 قائمة المشتركين ({len(subs)})\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"
    numbers = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, s in enumerate(subs):
        uid, username, full_name, join_date, expiry_date, warned = s
        expiry = datetime.fromisoformat(expiry_date)
        days_left = (expiry - now).days
        if days_left > 3:
            status = "✅ نشط"
        elif days_left > 0:
            status = "⚠️ قارب على الانتهاء"
        else:
            status = "❌ منتهي"
        profile_link = f'<a href="tg://user?id={uid}">{full_name}</a>'
        uname = f"@{username}" if username else "مفيش يوزر"
        num = numbers[i] if i < len(numbers) else f"{i+1}."
        msg += f"{num} {profile_link}\n"
        msg += f"   📛 {uname} | 🆔 {uid}\n"
        msg += f"   📅 ينتهي: {expiry.strftime('%Y-%m-%d')} | ⏳ {days_left} يوم\n"
        msg += f"   📊 {status}\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# ==================== قائمة الزوار ====================
async def list_visitors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    visitors = get_all_visitors()
    if not visitors:
        await update.message.reply_text("📭 مفيش زوار.")
        return
    msg = f"👀 قائمة الزوار ({len(visitors)}):\n\n"
    for v in visitors:
        uid, username, full_name, first_seen, last_seen = v
        uname = f"@{username}" if username else "مفيش يوزر"
        profile_link = f'<a href="tg://user?id={uid}">{full_name}</a>'
        first = datetime.fromisoformat(first_seen).strftime('%Y-%m-%d %H:%M')
        last = datetime.fromisoformat(last_seen).strftime('%Y-%m-%d %H:%M')
        msg += f"👤 {profile_link} | {uname}\n🆔 {uid} | أول مرة: {first} | آخر مرة: {last}\n\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# ==================== إنشاء رابط ====================
async def create_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_IDS:
        await update.message.reply_text("❌ مفيش جروبات.")
        return
    msg = "🔗 روابط الدعوة (مرة واحدة):\n\n"
    for gid in GROUP_IDS:
        try:
            link = await context.bot.create_chat_invite_link(chat_id=gid, member_limit=1, name="رابط اشتراك")
            chat = await context.bot.get_chat(gid)
            msg += f"📌 {chat.title}:\n{link.invite_link}\n\n"
        except Exception as e:
            msg += f"❌ مقدرتش أعمل رابط للجروب {gid}: {e}\n\n"
    await update.message.reply_text(msg, reply_markup=main_keyboard())

# ==================== Pay ====================
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(
        photo="AgACAgEAAxkBAAIBZGoqkpsPmkOtXLVYIc94UziqQrRtAALRC2sbIFVZRTl2q1dDx9CRAQADAgADeQADOwQ",
        caption="💳 طرق الدفع:\n\n1️⃣ 01080151847 (Vodafone Cash - Wallet)\n2️⃣ Binance number will be created soon...\n3️⃣ عمـــيل باينـــس فى انتظـار الدفع"
    )

# ==================== نسخة احتياطية ====================
async def export_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM subscribers")
        subs = c.fetchall()
        c.execute("SELECT * FROM receipts")
        receipts = c.fetchall()
        c.execute("SELECT * FROM visitors")
        visitors = c.fetchall()
        conn.close()
        backup = {
            "subscribers": [dict(s) for s in subs],
            "receipts": [dict(r) for r in receipts],
            "visitors": [dict(v) for v in visitors],
            "exported_at": datetime.now().isoformat()
        }
        backup_json = json.dumps(backup, ensure_ascii=False, indent=2)
        file_obj = io.BytesIO(backup_json.encode('utf-8'))
        file_obj.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await update.message.reply_document(
            document=file_obj,
            caption=f"📦 نسخة احتياطية\n👥 {len(subs)} مشترك\n🧾 {len(receipts)} إيصال\n👀 {len(visitors)} زائر\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        await update.message.reply_text(f"❌ حصل خطأ: {e}")

# ==================== الفحص اليومي ====================
async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    subs = get_all_subscribers()
    now = datetime.now()
    for s in subs:
        uid, username, full_name, join_date, expiry_date, warned = s
        expiry = datetime.fromisoformat(expiry_date)
        # حساب دقيق بالساعات مش الأيام بس
        diff = expiry - now
        days_left = diff.days
        total_hours = diff.total_seconds() / 3600
        if days_left <= 3 and days_left > 0 and not warned:
            warning_msg = (f"⚠️ تنبيه لـ {full_name}\n\nاشتراكك هينتهي بعد {days_left} يوم!\nجدد اشتراكك عشان متتطردش. 🙏")
            try:
                await context.bot.send_message(chat_id=uid, text=warning_msg)
            except Exception:
                pass
            mark_warned(uid)
        elif total_hours <= 0:
            # طرد من الجروبين
            for gid in GROUP_IDS:
                try:
                    await context.bot.ban_chat_member(gid, uid)
                    await context.bot.unban_chat_member(gid, uid)
                except Exception as e:
                    logger.warning(f"مقدرتش أطرد {uid} من {gid}: {e}")
            # ابعت رسالة في الخاص بس
            try:
                await context.bot.send_message(chat_id=uid,
                    text=f"❌ {full_name}، انتهى اشتراكك وتم إخراجك من الجروب.\nتقدر تجدد وترجع تاني!")
            except Exception:
                pass
            remove_subscriber(uid)
            logger.info(f"تم طرد {full_name} ({uid})")

# ==================== التشغيل ====================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        main_handler
    ))
    app.add_handler(ChatMemberHandler(member_joined, ChatMemberHandler.CHAT_MEMBER))
    app.job_queue.run_repeating(daily_check, interval=86400, first=86400)

    logger.info("البوت شغال! ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
