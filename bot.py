import asyncio
from datetime import datetime
import logging
import os
import threading
import time
import json
from app import database as db
from app.config import *
from app.services import locket, nextdns as nextdns_service
from flask import Flask, jsonify, request
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BotCommandScopeChat,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import pytz

logger = logging.getLogger(__name__)

telegram_app = None
main_event_loop = None

# --- HÀNG ĐỢI XỬ LÝ (QUEUE) ---
activation_queue = None  

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# --- DEFAULT VIP PACKAGES ---
VIP_PACKAGES_DEFAULT = {
    "vip1": {"name": "⚡ VIP NEW (1 Tuần)", "price": 15000, "memo_prefix": "VIP1", "spins": 10, "plan_name": "VIP NEW"},
    "vip2": {"name": "🔥 VIP GOLD (1 Tuần)", "price": 25000, "memo_prefix": "VIP2", "spins": 15, "plan_name": "VIP GOLD"},
    "vip3": {"name": "👑 VIP PREMIUM ULTIMATE", "price": 89000, "memo_prefix": "VIP3", "spins": 20, "plan_name": "VIP PREMIUM"},
}

def get_vip_packages():
    val = db.get_config("vip_packages")
    if val:
        try: return json.loads(val)
        except: pass
    return VIP_PACKAGES_DEFAULT

def save_vip_packages(pkgs):
    db.set_config("vip_packages", json.dumps(pkgs))

def get_vip_daily_limit(plan_name):
    if not plan_name: return 0
    p = plan_name.upper()
    if "PREMIUM" in p: return 10
    if "GOLD" in p: return 3
    if "NEW" in p or "VIP" in p: return 1
    return 0

# --- CƠ CHẾ CHỐNG SPAM ---
SPAM_MEMORY = {}

def check_spam(user_id):
    if user_id == ADMIN_ID: return 'ok'
    now = time.time()
    if user_id not in SPAM_MEMORY:
        SPAM_MEMORY[user_id] = {"times": [], "warnings": 0, "banned": False}
    
    rec = SPAM_MEMORY[user_id]
    if rec["banned"]: return 'banned' 
    
    rec["times"].append(now)
    rec["times"] = [t for t in rec["times"] if now - t <= 4.0] 
    
    if len(rec["times"]) >= 4: 
        rec["warnings"] += 1
        rec["times"] = [] 
        
        if rec["warnings"] >= 3:
            rec["banned"] = True
            return 'banned_now' 
        return f'warn_{rec["warnings"]}'
    
    return 'ok'


# --- NÂNG CẤP DATABASE ---
def upgrade_db_schema():
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("ALTER TABLE user_accounts ADD COLUMN IF NOT EXISTS extra_vip_spins INTEGER DEFAULT 0")
        c.execute("CREATE TABLE IF NOT EXISTS usage_logs (user_id BIGINT, date VARCHAR(50), count INTEGER, UNIQUE(user_id, date))")
        c.execute("CREATE TABLE IF NOT EXISTS force_channels (chat_id TEXT PRIMARY KEY, link TEXT)")
        conn.commit()
        c.close()
        conn.close()
    except Exception as e: 
        logger.error(f"Lỗi Upgrade DB Schema: {e}")

upgrade_db_schema()

# --- HÀM ÉP BUỘC VÀO NHÓM (FORCE SUBSCRIBE) ---
def get_force_channels():
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT chat_id, link FROM force_channels")
        rows = c.fetchall()
        conn.close()
        return [{"chat_id": r[0], "link": r[1]} for r in rows]
    except: return []

async def check_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID: return True
    
    channels = get_force_channels()
    if not channels: return True
    
    not_joined = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch["link"])
        except Exception as e:
            logger.error(f"Lỗi check Kênh {ch['chat_id']}: {e}")
            not_joined.append(ch["link"])
            
    if not_joined:
        keyboard = []
        for idx, link in enumerate(not_joined):
            keyboard.append([InlineKeyboardButton(f"👉 Tham gia Nhóm/Kênh {idx+1}", url=link)])
        keyboard.append([InlineKeyboardButton("✅ TÔI ĐÃ THAM GIA ĐỦ", callback_data="check_joined")])
        
        text = "⚠️ <b>YÊU CẦU BẮT BUỘC TỪ HỆ THỐNG</b>\n━━━━━━━━━━━━━━━━━━━\nĐể sử dụng Bot, bạn phải tham gia đủ các Nhóm/Kênh cộng đồng bên dưới.\n\n👇 <i>Sau khi tham gia xong, hãy bấm nút <b>ĐÃ THAM GIA ĐỦ</b> để tiếp tục!</i>"
        await send_clean_message(update.effective_chat.id, context, text, reply_markup=InlineKeyboardMarkup(keyboard), delete_user_msg=update.message)
        return False
    return True


# CÁC HÀM XỬ LÝ LƯỢT CƠ BẢN
def get_extra_vip_spins(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT extra_vip_spins FROM user_accounts WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        c.close()
        conn.close()
        return row[0] if row and row[0] else 0
    except: return 0

def add_extra_vip_spins(user_id, amount):
    conn = db.get_db_connection()
    c = conn.cursor()
    db._ensure_user_account(c, user_id)
    c.execute("UPDATE user_accounts SET extra_vip_spins = COALESCE(extra_vip_spins, 0) + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    c.close()
    conn.close()

def deduct_extra_vip_spin(user_id):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE user_accounts SET extra_vip_spins = GREATEST(0, COALESCE(extra_vip_spins, 0) - 1) WHERE user_id = %s", (user_id,))
    conn.commit()
    c.close()
    conn.close()

def deduct_spins_custom(user_id, amount):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT spins FROM user_accounts WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    current_spins = row[0] if row else 0
    new_spins = max(0, current_spins - int(amount))
    c.execute("UPDATE user_accounts SET spins = %s WHERE user_id = %s", (new_spins, user_id))
    conn.commit()
    c.close()
    conn.close()

def get_vip_tunnel_usage_today(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        today = datetime.now(VN_TZ).strftime("VIPTUN_%Y-%m-%d")
        c.execute("SELECT count FROM usage_logs WHERE user_id = %s AND date = %s", (user_id, today))
        row = c.fetchone()
        c.close()
        conn.close()
        return row[0] if row else 0
    except: return 0

def increment_vip_tunnel_usage_today(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        today = datetime.now(VN_TZ).strftime("VIPTUN_%Y-%m-%d")
        c.execute("INSERT INTO usage_logs (user_id, date, count) VALUES (%s, %s, 1) ON CONFLICT (user_id, date) DO UPDATE SET count = usage_logs.count + 1", (user_id, today))
        conn.commit()
        c.close()
        conn.close()
    except Exception as e: pass


# --- GIAO DIỆN CHUYÊN NGHIỆP ---
def get_welcome_text(user_name):
    return (
        "🎇 <b>HỆ THỐNG LOCKET GOLD PREMIUM</b> 🎇\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Chào mừng <b>{user_name}</b> đến với hệ thống!\n\n"
        "⚡ <i>Trung tâm phân phối & kích hoạt Locket Gold siêu tốc tự động, an toàn và bảo mật 100%.</i>\n\n"
        "👇 <i>Vui lòng lựa chọn các tính năng trên bàn phím điều khiển bên dưới:</i>"
    )

def get_bottom_reply_keyboard():
    keyboard = [
        [KeyboardButton("🚀 KÍCH HOẠT LOCKET"), KeyboardButton("👑 LUỒNG ĐẶC QUYỀN")],
        [KeyboardButton("💎 GÓI VIP"), KeyboardButton("🎟️ NẠP LƯỢT")],
        [KeyboardButton("🪪 TÀI KHOẢN"), KeyboardButton("🖼️ TÌM AVATAR")],
        [KeyboardButton("📖 HƯỚNG DẪN DNS"), KeyboardButton("💬 HỖ TRỢ")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_free_dns(): return db.get_config("dns_url", "https://dns.nextdns.io/785928") if hasattr(db, "get_config") else "https://dns.nextdns.io/785928"
def get_vip_dns(): return db.get_config("vip_dns_url", "Chưa cấu hình DNS VIP") if hasattr(db, "get_config") else "Chưa cấu hình DNS VIP"
def get_current_donate_info():
    bank = db.get_config("bank_code", "MB") if hasattr(db, "get_config") else "MB"
    acc = db.get_config("bank_acc", "0917921802") if hasattr(db, "get_config") else "0917921802"
    name = db.get_config("bank_name", "TRAN LAM TUONG HAI") if hasattr(db, "get_config") else "TRAN LAM TUONG HAI"
    return bank, acc, name

def get_guide_content():
    default_content = (
        "📖 <b>HƯỚNG DẪN CÀI ĐẶT DNS BẢO MẬT</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Hồ sơ:</b> <code>{display_name}</code>\n"
        "👑 <b>Cấp độ:</b> <code>{user_plan}</code>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🍎 <b>DÀNH CHO THIẾT BỊ IOS:</b>\n"
        "1️⃣ Mở liên kết cấu hình bên dưới bằng Safari.\n"
        "2️⃣ Bấm <i>Cho phép</i> tải về hồ sơ thiết bị.\n"
        "3️⃣ Vào <b>Cài đặt máy</b> ➔ <b>Đã tải về hồ sơ</b> ➔ <b>Cài đặt NextDNS</b>.\n\n"
        "🌐 <b>LINK TẢI DNS DÀNH CHO BẠN:</b>\n"
        "<a href='{dns_link}'>{dns_link}</a>\n\n"
        "🤖 <b>DÀNH CHO THIẾT BỊ ANDROID:</b>\n"
        "• Vào <b>Cài đặt</b> ➔ <b>Mạng</b> ➔ <b>DNS riêng tư</b> ➔ Dán dòng sau:\n"
        "<code>{nextdns_server_only}</code>"
    )
    return db.get_config("guide_text", default_content) if hasattr(db, "get_config") else default_content


# --- HÀM DỌN DẸP TIN NHẮN TỰ ĐỘNG ---
# --- HÀM DỌN DẸP TIN NHẮN TỰ ĐỘNG ---
async def send_clean_message(chat_id, context, text, reply_markup=None, photo=None, delete_user_msg=None):
    # 1. Xóa lệnh người dùng gõ vào cho sạch
    if delete_user_msg:
        try: await delete_user_msg.delete()
        except: pass

    # 2. Xóa tin nhắn cũ liền trước của Bot (BẬT LẠI ĐỂ DỌN RÁC)
    old_msg_id = context.user_data.get("last_bot_msg_id")
    if old_msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except: pass
    
    # 3. Gửi tin nhắn mới
    if photo:
        msg = await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    
    # 4. Ghi nhớ ID tin mới để lần sau dọn tiếp
    context.user_data["last_bot_msg_id"] = msg.message_id
    return msg

# --- LỆNH QUẢN LÝ CỦA ADMIN ---
async def admin_check(update: Update): return update.effective_user.id == ADMIN_ID

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        chat_id, link = context.args[0], context.args[1]
        try:
            await context.bot.get_chat_member(chat_id=chat_id, user_id=context.bot.id)
        except Exception as e:
            await context.bot.send_message(update.effective_chat.id, f"❌ <b>THÊM KÊNH THẤT BẠI!</b>\nBot không có quyền kiểm tra Kênh <code>{chat_id}</code>.\n\n<b>Nguyên nhân:</b>\n1. Sai ID (Group/Channel Private phải có chữ <b>-100</b> ở đầu, VD: <code>-10012345678</code>. Nếu kênh Public thì ghi <code>@TenKenh</code>).\n2. Bot chưa được cấp quyền <b>Admin</b> trong nhóm đó.\n\n<i>Lỗi: {e}</i>", parse_mode=ParseMode.HTML)
            return

        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO force_channels (chat_id, link) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET link = EXCLUDED.link", (chat_id, link))
        conn.commit()
        c.close(); conn.close()
        await context.bot.send_message(update.effective_chat.id, f"✅ Đã thêm Kênh Bắt Buộc thành công:\nID: {chat_id}\nLink: {link}", parse_mode=ParseMode.HTML)
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/addchannel [ID_Nhóm] [Link_Mời]</code>", parse_mode=ParseMode.HTML)

async def del_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM force_channels")
        conn.commit()
        c.close(); conn.close()
        await context.bot.send_message(update.effective_chat.id, "✅ <b>Đã xóa sạch tất cả các Kênh Bắt Buộc!</b>\nBây giờ người dùng có thể sử dụng Bot tự do bình thường.", parse_mode=ParseMode.HTML)
    except Exception as e: 
        await context.bot.send_message(update.effective_chat.id, f"❌ Lỗi khi xóa: {e}")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    channels = get_force_channels()
    if not channels:
        await context.bot.send_message(update.effective_chat.id, "📋 <b>DANH SÁCH KÊNH BẮT BUỘC:</b> Trống (Không yêu cầu gia nhập nhóm nào).", parse_mode=ParseMode.HTML)
        return
    msg = "📋 <b>DANH SÁCH KÊNH BẮT BUỘC HIỆN TẠI:</b>\n━━━━━━━━━━━━━━━━━━━\n"
    for idx, ch in enumerate(channels):
        msg += f"{idx+1}. ID: <code>{ch['chat_id']}</code>\n🔗 Link: {ch['link']}\n\n"
    msg += "🗑️ <i>Muốn xóa toàn bộ? Chỉ cần gõ lệnh: <code>/delchannel</code></i>"
    await context.bot.send_message(update.effective_chat.id, msg, parse_mode=ParseMode.HTML)

async def edit_vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        pkg_key = context.args[0].lower()
        price = int(context.args[1])
        spins = int(context.args[2])
        plan_name = " ".join(context.args[3:])
        
        if pkg_key not in ["vip1", "vip2", "vip3"]: raise ValueError()
        
        pkgs = get_vip_packages()
        pkgs[pkg_key]["price"] = price
        pkgs[pkg_key]["spins"] = spins
        pkgs[pkg_key]["name"] = f"🌟 {plan_name} ({price:,}đ)"
        pkgs[pkg_key]["plan_name"] = plan_name
        
        save_vip_packages(pkgs)
        await context.bot.send_message(update.effective_chat.id, f"✅ Cập nhật {pkg_key} thành công:\nGiá: {price}đ | Lượt: {spins}/ngày | Tên: {plan_name}")
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/editvip [vip1/vip2/vip3] [Giá] [Lượt] [Tên Gói Mới]</code>", parse_mode=ParseMode.HTML)

async def set_dns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, f"🌐 <b>DNS GÓI FREE HIỆN TẠI:</b>\n<code>{get_free_dns()}</code>\n\n⚠️ Đổi:\n<code>/setdns https://link-moi</code>", parse_mode=ParseMode.HTML)
        return
    if hasattr(db, "set_config"): db.set_config("dns_url", context.args[0])
    await context.bot.send_message(update.effective_chat.id, f"✅ <b>Đã cập nhật DNS FREE:</b>\n<code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)

async def set_nextdns_apikey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if not context.args:
        await context.bot.send_message(update.effective_chat.id, f"👑 <b>DNS GÓI VIP HIỆN TẠI:</b>\n<code>{get_vip_dns()}</code>\n\n⚠️ Đổi:\n<code>/setnextdnskey https://link-moi</code>", parse_mode=ParseMode.HTML)
        return
    if hasattr(db, "set_config"): db.set_config("vip_dns_url", context.args[0])
    await context.bot.send_message(update.effective_chat.id, f"✅ <b>Đã cập nhật DNS VIP:</b>\n<code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)

async def set_guide_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    new_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else " ".join(context.args)
    if not new_text:
        await context.bot.send_message(update.effective_chat.id, "📜 Cú pháp: <code>/setguide [Nội dung mới]</code>", parse_mode=ParseMode.HTML)
        return
    if hasattr(db, "set_config"): db.set_config("guide_text", new_text)
    await context.bot.send_message(update.effective_chat.id, "✅ <b>Đã cập nhật hướng dẫn!</b>", parse_mode=ParseMode.HTML)

async def set_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if len(context.args) < 3:
        bank, acc, name = get_current_donate_info()
        await context.bot.send_message(update.effective_chat.id, f"💳 <b>BANK:</b> <code>{bank}</code> - <code>{acc}</code> - <code>{name}</code>\nCú pháp: <code>/setdonate MB 123 TEN</code>", parse_mode=ParseMode.HTML)
        return
    if hasattr(db, "set_config"):
        db.set_config("bank_code", context.args[0])
        db.set_config("bank_acc", context.args[1])
        db.set_config("bank_name", " ".join(context.args[2:]))
    await context.bot.send_message(update.effective_chat.id, "✅ <b>Đã cập nhật thông tin ngân hàng!</b>", parse_mode=ParseMode.HTML)

async def set_success_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await context.bot.send_message(update.effective_chat.id, "⚠️ Vui lòng <b>reply</b> bức ảnh kèm lệnh <code>/setimg</code>", parse_mode=ParseMode.HTML)
        return
    if hasattr(db, "set_config"): db.set_config("success_image_id", update.message.reply_to_message.photo[-1].file_id)
    await context.bot.send_message(update.effective_chat.id, "✅ <b>Đã lưu ảnh thông báo!</b>", parse_mode=ParseMode.HTML)

async def set_guide_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    video_val = ""
    if update.message.reply_to_message:
        if update.message.reply_to_message.video: video_val = update.message.reply_to_message.video.file_id
        elif update.message.reply_to_message.document: video_val = update.message.reply_to_message.document.file_id
    elif context.args:
        video_val = None if context.args[0].lower() in ["none", "xóa", "off"] else context.args[0]
    if hasattr(db, "set_config"): db.set_config("guide_video_url", video_val)
    await context.bot.send_message(update.effective_chat.id, "✅ <b>Đã cập nhật video hướng dẫn!</b>", parse_mode=ParseMode.HTML)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    broadcast_msg = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else " ".join(context.args)
    if not broadcast_msg:
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Cú pháp: <code>/broadcast Nội dung</code>", parse_mode=ParseMode.HTML)
        return
    status_msg = await context.bot.send_message(chat_id=ADMIN_ID, text="⏳ <b>Đang phát thông báo...</b>", parse_mode=ParseMode.HTML)
    users = db.get_all_users() if hasattr(db, "get_all_users") else []
    success, fail = 0, 0
    formatted_msg = f"📢 <b>THÔNG BÁO TỪ HỆ THỐNG LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n\n{broadcast_msg}\n\n━━━━━━━━━━━━━━━━━━━"
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=formatted_msg, parse_mode=ParseMode.HTML)
            success += 1
            await asyncio.sleep(0.05)
        except: fail += 1
    await status_msg.edit_text(f"✅ <b>Hoàn tất Broadcast!</b>\nThành công: {success} | Lỗi: {fail}", parse_mode=ParseMode.HTML)

async def addspin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
        db.add_user_spins(uid, amount)
        await context.bot.send_message(update.effective_chat.id, f"✅ Đã cộng <b>{amount}</b> lượt cho ID <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/addspin [ID] [Số Lượt]</code>", parse_mode=ParseMode.HTML)

async def delspin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
        deduct_spins_custom(uid, amount)
        await context.bot.send_message(update.effective_chat.id, f"✅ Đã trừ <b>{amount}</b> lượt của ID <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/delspin [ID] [Số Lượt]</code>", parse_mode=ParseMode.HTML)

async def setplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        uid = int(context.args[0])
        if context.args[-1].lower() in ["ngày", "ngay", "days", "day"]:
            days = int(context.args[-2])
            plan = " ".join(context.args[1:-2])
        else:
            days = int(context.args[-1])
            plan = " ".join(context.args[1:-1])
        
        db.set_user_plan(uid, plan, days)
        await context.bot.send_message(update.effective_chat.id, f"✅ Đã gán gói <b>{plan}</b> ({days} ngày) cho ID <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/setplan [ID] [Tên Gói] [Số Ngày]</code>", parse_mode=ParseMode.HTML)

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        uid = int(context.args[0])
        if uid in SPAM_MEMORY:
            SPAM_MEMORY[uid] = {"times": [], "warnings": 0, "banned": False}
        await context.bot.send_message(update.effective_chat.id, f"✅ Đã mở khóa Anti-Spam cho ID <code>{uid}</code>", parse_mode=ParseMode.HTML)
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: <code>/unban [ID]</code>", parse_mode=ParseMode.HTML)


# --- FLASK WEBHOOK CHO TELEGRAM & SEPAY ---
flask_app = Flask(__name__)

_bot_thread_started = False
_bot_lock = threading.Lock()

def start_background_bot():
    global _bot_thread_started
    with _bot_lock:
        if not _bot_thread_started:
            bot_thread = threading.Thread(target=run_telegram_bot_background, daemon=True)
            bot_thread.start()
            _bot_thread_started = True

@flask_app.before_request
def ensure_bot():
    start_background_bot()

@flask_app.route("/", methods=["GET"])
def home(): return "Locket Gold System is Online!", 200

@flask_app.route("/telegram-webhook", methods=["POST", "GET"])
def telegram_webhook():
    global telegram_app, main_event_loop
    if request.method == "GET": return jsonify({"status": "Webhook Active"}), 200
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "No JSON"}), 400
    
    for _ in range(10):
        if telegram_app and main_event_loop:
            break
        time.sleep(0.5)

    if not telegram_app or not main_event_loop: 
        return jsonify({"error": "Initializing"}), 503
        
    try:
        update = Update.de_json(data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), main_event_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Telegram Webhook error: {str(e)}")
        return jsonify({"error": str(e)}), 500

def process_vip_payment(target_user_id, vip_code, transfer_amount):
    pkgs = get_vip_packages()
    pkg = pkgs.get(vip_code.lower())
    if not pkg or transfer_amount < pkg["price"]: return False, None
    if hasattr(db, "add_user_spins"): db.add_user_spins(target_user_id, pkg["spins"])
    if hasattr(db, "set_user_plan"): db.set_user_plan(target_user_id, pkg["plan_name"], days=7)
    if hasattr(db, "set_user_daily_quota"): db.set_user_daily_quota(target_user_id, pkg["spins"])
    return True, pkg

@flask_app.route("/sepay-webhook", methods=["POST", "GET"])
def sepay_webhook():
    if request.method == "GET": return jsonify({"status": "SePay Listening"}), 200
    try:
        data = request.get_json(force=True, silent=True)
        if not data: return jsonify({"success": False}), 400
        content = str(data.get("content", "")).upper()
        transfer_amount = float(data.get("transferAmount", 0))

        if "VIP" in content and "LUOT" not in content:
            vip_code, target_user_id = "", None
            for code in ["VIP1", "VIP2", "VIP3"]:
                if code in content:
                    vip_code = code.lower()
                    digits_only = "".join(filter(str.isdigit, content.replace(code, "").strip()))
                    if digits_only: target_user_id = int(digits_only[:10])
                    break
            if vip_code and target_user_id:
                success, result = process_vip_payment(target_user_id, vip_code, transfer_amount)
                if hasattr(db, "log_transaction"): db.log_transaction(target_user_id, transfer_amount, content)
                if success and telegram_app and main_event_loop:
                    success_noti = f"🎉 <b>GIAO DỊCH THÀNH CÔNG!</b>\n━━━━━━━━━━━━━━━━━━━\n👑 <b>Hạng:</b> {result['plan_name']} (7 Ngày)\n🎁 <b>Lượt/ngày:</b> +{result['spins']} Lượt\n💰 <b>Thực nhận:</b> <code>{transfer_amount:,.0f}đ</code>\n━━━━━━━━━━━━━━━━━━━\n🚀 <i>Cảm ơn bạn đã tin dùng dịch vụ chuyên nghiệp!</i>"
                    asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=target_user_id, text=success_noti, parse_mode=ParseMode.HTML), main_event_loop)
                return jsonify({"success": True}), 200

        elif "LUOT" in content and "VIPLUOT" not in content:
            target_user_id = None
            import re
            match = re.search(r"LUOT(\d+)(\d{4})", content)
            if match: target_user_id = int(match.group(1))
            if not target_user_id:
                for m in re.findall(r"\d+", content):
                    if len(m) >= 8: target_user_id = int(m[:10]); break
            if target_user_id and transfer_amount >= 2000:
                spins_added = int(transfer_amount // 2000)
                if spins_added > 0:
                    if hasattr(db, "add_user_spins"): db.add_user_spins(target_user_id, spins_added)
                    if hasattr(db, "log_transaction"): db.log_transaction(target_user_id, transfer_amount, content)
                    if telegram_app and main_event_loop:
                        success_noti = f"🎉 <b>NẠP LƯỢT THÀNH CÔNG!</b>\n━━━━━━━━━━━━━━━━━━━\n🎟️ <b>Đã cộng:</b> +{spins_added} Lượt\n💰 <b>Đã thanh toán:</b> <code>{transfer_amount:,.0f}đ</code>\n━━━━━━━━━━━━━━━━━━━\n🚀 <i>Hệ thống đã tự động cộng số lượt vào tài khoản của bạn!</i>"
                        asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=target_user_id, text=success_noti, parse_mode=ParseMode.HTML), main_event_loop)
                return jsonify({"success": True}), 200

        elif "VIPLUOT" in content:
            target_user_id = None
            import re
            match = re.search(r"VIPLUOT(\d+)(\d{4})", content)
            if match: target_user_id = int(match.group(1))
            if target_user_id and transfer_amount >= 10000:
                vip_spins_added = int(transfer_amount // 10000)
                if vip_spins_added > 0:
                    add_extra_vip_spins(target_user_id, vip_spins_added)
                    if hasattr(db, "log_transaction"): db.log_transaction(target_user_id, transfer_amount, content)
                    if telegram_app and main_event_loop:
                        success_noti = f"🎉 <b>MỞ KHÓA LUỒNG VIP THÀNH CÔNG!</b>\n━━━━━━━━━━━━━━━━━━━\n👑 <b>Đã cấp:</b> +{vip_spins_added} Lượt Đặc Quyền\n━━━━━━━━━━━━━━━━━━━\n<i>Vui lòng bấm vào Menu Luồng Đặc Quyền để sử dụng ngay!</i>"
                        asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=target_user_id, text=success_noti, parse_mode=ParseMode.HTML), main_event_loop)
                return jsonify({"success": True}), 200

        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 200

def _get_expiry_date(uid):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT expires_at FROM user_accounts WHERE user_id = %s", (uid,))
        row = c.fetchone()
        c.close()
        conn.close()
        return row[0] if row and row[0] else None
    except: return None


# --- XỬ LÝ SỰ KIỆN TỪ TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    spam_status = check_spam(user.id)
    if spam_status == 'banned': return
    if spam_status == 'banned_now':
        await context.bot.send_message(update.effective_chat.id, "🚫 <b>BẠN ĐÃ BỊ CHẶN HOÀN TOÀN!</b>\nBạn đã gửi lệnh quá mức cho phép. Hệ thống từ chối phục vụ bạn vĩnh viễn.", parse_mode=ParseMode.HTML)
        return
    if spam_status.startswith('warn_'):
        warn_count = spam_status.split('_')[1]
        await context.bot.send_message(update.effective_chat.id, f"⚠️ <b>CẢNH BÁO SPAM ({warn_count}/3)</b>\nBạn đang thao tác quá nhanh. Nếu đạt 3/3, bạn sẽ bị chặn vĩnh viễn!", parse_mode=ParseMode.HTML)
        return

    if not await check_force_sub(update, context): return
        
    context.user_data["active_flow"] = None
    loop = asyncio.get_running_loop()
    if hasattr(db, "check_and_reset_daily_spins"): await loop.run_in_executor(None, db.check_and_reset_daily_spins, user.id)
    
    await send_clean_message(update.effective_chat.id, context, get_welcome_text(user.full_name), reply_markup=get_bottom_reply_keyboard(), delete_user_msg=update.message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spam_status = check_spam(update.effective_user.id)
    if spam_status == 'banned': return
    
    user_id = update.effective_user.id
    loop = asyncio.get_running_loop()
    user_plan = await loop.run_in_executor(None, db.get_user_plan, user_id) if hasattr(db, "get_user_plan") else "Thường (Free)"
    
    is_vip_tier = "GOLD" in user_plan.upper() or "PREMIUM" in user_plan.upper() or "VIP" in user_plan.upper() or user_id == ADMIN_ID
    dns_link = get_vip_dns() if is_vip_tier else get_free_dns()
    guide_text = get_guide_content().format(display_name=update.effective_user.full_name, user_plan=user_plan, dns_link=dns_link, nextdns_server_only="dns.nextdns.io")
    
    await send_clean_message(update.effective_chat.id, context, guide_text, delete_user_msg=update.message)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    spam_status = check_spam(user_id)
    if spam_status == 'banned': return
    if spam_status == 'banned_now':
        await context.bot.send_message(update.effective_chat.id, "🚫 <b>BẠN ĐÃ BỊ CHẶN HOÀN TOÀN!</b>", parse_mode=ParseMode.HTML)
        return
    if spam_status.startswith('warn_'):
        warn_count = spam_status.split('_')[1]
        await context.bot.send_message(update.effective_chat.id, f"⚠️ <b>CẢNH BÁO SPAM ({warn_count}/3)</b>", parse_mode=ParseMode.HTML)
        return

    if not await check_force_sub(update, context): return

    current_flow = context.user_data.get("active_flow")
    text = update.message.text.strip()
    loop = asyncio.get_running_loop()
    
    menu_commands = ["🚀 KÍCH HOẠT LOCKET", "👑 LUỒNG ĐẶC QUYỀN", "💎 GÓI VIP", "🎟️ NẠP LƯỢT", "🪪 TÀI KHOẢN", "🖼️ TÌM AVATAR", "📖 HƯỚNG DẪN DNS", "💬 HỖ TRỢ"]
    if text in menu_commands:
        context.user_data["active_flow"] = None
        user_plan = await loop.run_in_executor(None, db.get_user_plan, user_id) if hasattr(db, "get_user_plan") else "Free"
        user_spins = await loop.run_in_executor(None, db.get_user_spins, user_id) if hasattr(db, "get_user_spins") else 0

        if text == "🚀 KÍCH HOẠT LOCKET":
            gold_text = f"⚙️ <b>TRUNG TÂM KÍCH HOẠT GOLD</b>\n━━━━━━━━━━━━━━━━━━━\n👑 <b>Hạng:</b> <code>{user_plan}</code> | 🎟️ <b>Số dư:</b> <code>{user_spins} Lượt</code>\n\n📊 <b>LỰA CHỌN PHÂN VÙNG:</b>\n🟢 <b>[1 Lượt] Luồng Cơ Bản:</b> Ổn định, cho mọi tài khoản.\n🔴 <b>[2 Lượt] Luồng Ưu Tiên:</b> VIP, Đóng băng huy hiệu.\n\n👇 <i>Vui lòng chọn luồng kích hoạt bên dưới:</i>"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Luồng Cơ Bản (1 Lượt)", callback_data="select_tunnel_1")], [InlineKeyboardButton("🔒 Luồng Ưu Tiên (2 Lượt)", callback_data="select_tunnel_2")]])
            await send_clean_message(update.effective_chat.id, context, gold_text, reply_markup=keyboard, delete_user_msg=update.message)
            return

        elif text == "👑 LUỒNG ĐẶC QUYỀN":
            if user_id != ADMIN_ID and "GOLD" not in user_plan.upper() and "PREMIUM" not in user_plan.upper() and "VIP" not in user_plan.upper():
                await send_clean_message(update.effective_chat.id, context, "❌ Tính năng này chỉ dành cho Tài khoản đã mua Gói VIP!", delete_user_msg=update.message)
                return
            
            used_today = await loop.run_in_executor(None, get_vip_tunnel_usage_today, user_id)
            extra_spins = await loop.run_in_executor(None, get_extra_vip_spins, user_id)
            vip_limit = get_vip_daily_limit(user_plan)
            
            if used_today >= vip_limit and extra_spins <= 0 and user_id != ADMIN_ID:
                lack_price = 10000
                memo_spin = f"VIPLUOT{user_id}{datetime.now().strftime('%M%S')}"
                bank_code, bank_acc, bank_name = get_current_donate_info()
                qr_lack_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={lack_price}&addInfo={memo_spin}&accountName={bank_name.replace(' ', '%20')}"
                lack_text = f"⚠️ <b>ĐÃ HẾT LƯỢT LUỒNG ĐẶC QUYỀN HÔM NAY</b> ⚠️\n━━━━━━━━━━━━━━━━━━━\nGói <b>{user_plan}</b> của bạn được sử dụng <b>{vip_limit} lần miễn phí / ngày.</b>\n\n💳 <b>MỞ THÊM 1 LƯỢT ĐẶC QUYỀN (10.000đ):</b>\n📝 Nội dung CK: <code>{memo_spin}</code>\n━━━━━━━━━━━━━━━━━━━\n👇 <i>Quét mã thanh toán rồi bấm nút Kiểm tra:</i>"
                keyboard_lack = InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 KIỂM TRA CK MUA 1 LƯỢT (10k)", callback_data=f"verify_vip_spin_10k|{memo_spin}")]])
                await send_clean_message(update.effective_chat.id, context, lack_text, reply_markup=keyboard_lack, photo=qr_lack_url, delete_user_msg=update.message)
                return

            step1_text = "👑 <b>LUỒNG ĐẶC QUYỀN · BƯỚC 1/4 (LÀM SẠCH)</b>\n━━━━━━━━━━━━━━━━━━━\n▫️ <b>1. Xóa cấu hình cũ:</b> Cài đặt máy -> VPN & Quản lý thiết bị -> Xóa Profile DNS cũ.\n▫️ <b>2. Xóa bộ đệm:</b> Xóa hoàn toàn app Locket khỏi máy.\n\n👇 <i>Xác nhận hoàn tất:</i>"
            await send_clean_message(update.effective_chat.id, context, step1_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ XONG, TIẾP TỤC ➔", callback_data="vup_step_2")]]), delete_user_msg=update.message)
            return

        elif text == "💎 GÓI VIP":
            pkgs = get_vip_packages()
            store_text = "💎 <b>CỬA HÀNG NÂNG CẤP VIP (7 NGÀY)</b>\n━━━━━━━━━━━━━━━━━━━\n"
            for k in ["vip1", "vip2", "vip3"]:
                store_text += f"{pkgs[k]['name']}: {pkgs[k]['spins']} lượt/ngày\n"
            store_text += "━━━━━━━━━━━━━━━━━━━\n👇 <i>Chọn gói nâng cấp tương ứng bên dưới:</i>"
            
            keyboard = [
                [InlineKeyboardButton("⚡ MUA VIP NEW (15k)", callback_data="buy_vip1")],
                [InlineKeyboardButton("🔥 MUA VIP GOLD (25k)", callback_data="buy_vip2")],
                [InlineKeyboardButton("👑 MUA PREMIUM (89k)", callback_data="buy_vip3")]
            ]
            await send_clean_message(update.effective_chat.id, context, store_text, reply_markup=InlineKeyboardMarkup(keyboard), delete_user_msg=update.message)
            return

        elif text == "🎟️ NẠP LƯỢT":
            context.user_data["active_flow"] = "custom_spin_input"
            await send_clean_message(update.effective_chat.id, context, "🎟️ <b>MUA LƯỢT LẺ TÙY CHỌN</b>\n━━━━━━━━━━━━━━━━━━━\n💬 Nhập <b>số lượng lượt</b> muốn mua (2.000đ/Lượt) vào khung chat:\n<i>Ví dụ: Nhập 10 để mua 10 lượt</i>", delete_user_msg=update.message)
            return

        elif text == "🪪 TÀI KHOẢN":
            expiry_raw = await loop.run_in_executor(None, _get_expiry_date, user_id)
            expiry_date = "Vô thời hạn" if user_plan == "Thường (Free)" else (expiry_raw if expiry_raw else "Không xác định")
            account_text = f"🪪 <b>THẺ TÀI KHOẢN CỦA BẠN</b>\n━━━━━━━━━━━━━━━━━━━\n👤 <b>Chủ tài khoản:</b> {update.effective_user.full_name}\n🆔 <b>ID Hệ thống:</b> <code>{user_id}</code>\n\n👑 <b>Hạng thành viên:</b> <code>{user_plan}</code>\n⏳ <b>Ngày hết hạn:</b> <code>{expiry_date}</code>\n🎟️ <b>Số dư hiện tại:</b> <b>{user_spins} Lượt</b>\n━━━━━━━━━━━━━━━━━━━\n💡 <i>Mẹo: Mua gói VIP để không cần lo lắng về lượt!</i>"
            await send_clean_message(update.effective_chat.id, context, account_text, delete_user_msg=update.message)
            return

        elif text == "🖼️ TÌM AVATAR":
            context.user_data["active_flow"] = "avt_input"
            await send_clean_message(update.effective_chat.id, context, "🖼️ <b>TRA CỨU AVATAR LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n💬 Gửi <b>Username</b> hoặc <b>Link Locket</b> vào khung chat để hệ thống trích xuất ảnh gốc chất lượng cao:", delete_user_msg=update.message)
            return

        elif text == "📖 HƯỚNG DẪN DNS":
            is_vip_tier = "GOLD" in user_plan.upper() or "PREMIUM" in user_plan.upper() or "VIP" in user_plan.upper() or user_id == ADMIN_ID
            dns_link = get_vip_dns() if is_vip_tier else get_free_dns()
            guide_text = get_guide_content().format(display_name=update.effective_user.full_name, user_plan=user_plan, dns_link=dns_link, nextdns_server_only="dns.nextdns.io")
            await send_clean_message(update.effective_chat.id, context, guide_text, delete_user_msg=update.message)
            return
            
        elif text == "💬 HỖ TRỢ":
            await send_clean_message(update.effective_chat.id, context, "💬 <b>LIÊN HỆ HỖ TRỢ</b>\n━━━━━━━━━━━━━━━━━━━\nMọi thắc mắc và hỗ trợ bảo hành vui lòng liên hệ Admin:\n👉 @TRANHAI_307", delete_user_msg=update.message)
            return

    if not current_flow: 
        try: await update.message.delete()
        except: pass
        # NẾU GÕ CHỮ BẤT KỲ MÀ CHƯA CÓ MENU, HIỆN LẠI MENU CHÍNH LUÔN
        await send_clean_message(update.effective_chat.id, context, get_welcome_text(update.effective_user.full_name), reply_markup=get_bottom_reply_keyboard())
        return

    if current_flow == "avt_input":
        username = text.split("locket.cam/")[-1].split("?")[0].replace("@", "") if "locket.cam/" in text else text.replace("@", "")
        msg = await send_clean_message(update.effective_chat.id, context, "🔍 <b>Đang trích xuất ảnh gốc từ máy chủ...</b>", delete_user_msg=update.message)
        try:
            uid = await locket.resolve_uid(username)
            if not uid:
                await send_clean_message(update.effective_chat.id, context, "❌ Không tìm thấy Username Locket!")
                return
            avatar_url = await locket.get_user_avatar(text)
            if avatar_url and avatar_url.startswith("http"):
                await send_clean_message(update.effective_chat.id, context, f"🖼️ <b>AVATAR LOCKET (BẢN GỐC)</b>\n━━━━━━━━━━━━━━━━━━━\n👤 <b>Tài khoản:</b> <code>{username}</code>\n🔗 <a href='{avatar_url}'>Mở ảnh chất lượng cao</a>", photo=avatar_url)
            else:
                await send_clean_message(update.effective_chat.id, context, "⚠️ Người dùng này chưa cài ảnh đại diện.")
        except Exception as e:
            await send_clean_message(update.effective_chat.id, context, f"❌ Xảy ra lỗi: {str(e)}")
        context.user_data["active_flow"] = None
        return

    if current_flow == "custom_spin_input":
        if not text.isdigit() or int(text) <= 0:
            await send_clean_message(update.effective_chat.id, context, "❌ Số lượng không hợp lệ. Vui lòng nhập số đếm (VD: 5).", delete_user_msg=update.message)
            return
        spin_count = int(text)
        total_price = spin_count * 2000
        memo = f"LUOT{user_id}{datetime.now().strftime('%M%S')}"
        bank_code, bank_acc, bank_name = get_current_donate_info()
        qr_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={total_price}&addInfo={memo}&accountName={bank_name.replace(' ', '%20')}"
        
        await send_clean_message(
            update.effective_chat.id, context,
            f"💳 <b>HÓA ĐƠN MUA {spin_count} LƯỢT CƠ BẢN</b>\n━━━━━━━━━━━━━━━━━━━\n💰 Tiền thanh toán: <code>{total_price:,} VNĐ</code>\n📝 Nội dung CK: <code>{memo}</code>\n━━━━━━━━━━━━━━━━━━━\n<i>Chuyển khoản xong hệ thống tự động cộng!</i>",
            photo=qr_url,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 KIỂM TRA CK TỰ ĐỘNG", callback_data=f"verify_spin|{spin_count}")]]),
            delete_user_msg=update.message
        )
        context.user_data["active_flow"] = None
        return

    if current_flow == "locket_input" or current_flow == "vip_premium_input":
        is_vip_flow = current_flow == "vip_premium_input"
        username = text.split("locket.cam/")[-1].split("?")[0].replace("@", "") if "locket.cam/" in text else text.replace("@", "")
        
        msg = await send_clean_message(update.effective_chat.id, context, "🔍 <b>Đang phân tích hồ sơ Locket...</b>", delete_user_msg=update.message)
        
        uid = await locket.resolve_uid(username)
        if not uid:
            await send_clean_message(update.effective_chat.id, context, "❌ Không tìm thấy Username Locket này!")
            return

        cost = context.user_data.get("tunnel_cost", 1) if not is_vip_flow else 0
        tunnel_name = "Luồng Cơ Bản / Ưu Tiên" if not is_vip_flow else "Luồng Đặc Quyền VIP"
        cost_text = f"{cost} Lượt Thường" if not is_vip_flow else "1 Lượt Đặc Quyền"
        callback_action = f"upg_vip|{uid}|{username}|{cost}" if is_vip_flow else f"upg|{uid}|{username}|{cost}"
        reinput_action = "reinput_vip" if is_vip_flow else f"reinput_locket_{cost}"

        is_active = False
        try:
            if hasattr(locket, "check_user_gold_status"): is_active = await locket.check_user_gold_status(uid, TOKEN_SETS[0])
            else:
                user_info = await locket.get_user_profile(uid, TOKEN_SETS[0])
                is_active = bool(user_info and user_info.get("is_gold", False))
        except: pass
        
        status_badge = "🟢 ACTIVE (ĐÃ CÓ GOLD)" if is_active else "🔴 CHƯA ACTIVE (FREE)"

        avatar_url = await locket.get_user_avatar(text)
        if avatar_url: context.user_data["last_locket_avatar_url"] = avatar_url

        confirm_msg = f"✅ <b>TÌM THẤY TÀI KHOẢN ĐÍCH:</b>\n━━━━━━━━━━━━━━━━━━━\n👤 <b>Username:</b> <code>{username}</code>\n🆔 <b>UID:</b> <code>{uid}</code>\n⚡ <b>Trạng thái:</b> {status_badge}\n\n🚀 <b>Phân vùng:</b> {tunnel_name}\n➖ <b>Sẽ tiêu hao:</b> -{cost_text}\n━━━━━━━━━━━━━━━━━━━\n👇 <i>Hãy bấm nút XÁC NHẬN để tiến hành Bơm Gold!</i>"
        
        keyboard_confirm = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ XÁC NHẬN BƠM GOLD", callback_data=callback_action)],
            [InlineKeyboardButton("🔄 NHẬP LẠI TÀI KHOẢN KHÁC", callback_data=reinput_action)]
        ])

        if avatar_url and avatar_url.startswith("http"):
            await send_clean_message(update.effective_chat.id, context, confirm_msg, photo=avatar_url, reply_markup=keyboard_confirm)
        else: 
            await send_clean_message(update.effective_chat.id, context, confirm_msg, reply_markup=keyboard_confirm)
        
        context.user_data["active_flow"] = None


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer()
    except: pass
    
    data, user_id = query.data, query.from_user.id
    spam_status = check_spam(user_id)
    if spam_status == 'banned': return
    
    loop = asyncio.get_running_loop()

    # THUẬT TOÁN QUÉT NGẦM MỚI (BÁO LỖI VĂN BẢN NẾU CHƯA THAM GIA)
    if data == "check_joined":
        channels = get_force_channels()
        not_joined_links = []
        for ch in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
                if member.status in ['left', 'kicked']: 
                    not_joined_links.append(ch["link"])
            except Exception as e:
                logger.error(f"Lỗi check_joined: {e}")
                not_joined_links.append(ch["link"])
        
        if not_joined_links:
            # Hiện thông báo văn bản rõ ràng nếu lừa Bot hoặc Bot chưa có quyền Admin
            keyboard = []
            for idx, link in enumerate(not_joined_links):
                keyboard.append([InlineKeyboardButton(f"👉 Tham gia Nhóm/Kênh {idx+1}", url=link)])
            keyboard.append([InlineKeyboardButton("✅ TÔI ĐÃ THAM GIA ĐỦ", callback_data="check_joined")])
            
            text = "❌ <b>HỆ THỐNG QUÉT: BẠN CHƯA THAM GIA NHÓM!</b>\n━━━━━━━━━━━━━━━━━━━\nHệ thống quét tự động không thấy bạn trong danh sách thành viên. Vui lòng kiểm tra lại:\n\n1. Bấm vào nút bên dưới để tham gia.\n2. Chắc chắn bạn đã nhấn nút <b>Join (Tham gia)</b> trong kênh đó.\n3. Nếu đã tham gia nhưng vẫn báo lỗi, vui lòng báo Admin kiểm tra lại quyền <b>Quản trị viên</b> của Bot trong nhóm!\n\n👇 <i>Nếu đã xử lý xong, hãy bấm lại nút ĐÃ THAM GIA ĐỦ!</i>"
            await send_clean_message(update.effective_chat.id, context, text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            # Nếu quét thành công, lập tức nhả Bảng Menu
            try: await context.bot.answer_callback_query(query.id, text="✅ Xác nhận thành công! Đang mở hệ thống...")
            except: pass
            await send_clean_message(update.effective_chat.id, context, get_welcome_text(query.from_user.full_name), reply_markup=get_bottom_reply_keyboard())
        return

    if data.startswith("reinput_locket_"):
        cost = int(data.split("_")[-1])
        tunnel_id = "1" if cost == 1 else "2"
        tunnel_name = "Luồng Cơ Bản" if tunnel_id == "1" else "Luồng Ưu Tiên"
        context.user_data["selected_tunnel"], context.user_data["tunnel_cost"], context.user_data["active_flow"] = tunnel_id, cost, "locket_input"
        input_prompt = f"⚙️ <b>NHẬP TÀI KHOẢN LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n🚀 Phân vùng: <b>{tunnel_name}</b> (-{cost} Lượt)\n\n💬 Vui lòng gửi <b>Username</b> hoặc <b>Đường link Locket</b> của bạn vào khung chat.\n<i>(Ví dụ: <code>trhai</code>)</i>\n\n⌨️ Gõ <code>hủy</code> nếu muốn quay lại."
        await send_clean_message(query.message.chat_id, context, input_prompt)
        return

    if data == "reinput_vip":
        context.user_data["active_flow"] = "vip_premium_input"
        step4_text = "👑 <b>QUY TRÌNH KÍCH VIP · BƯỚC 4/4 (HOÀN TẤT)</b>\n━━━━━━━━━━━━━━━━━━━\n🎉 Bạn đã thiết lập xong môi trường.\n💬 <b>Yêu cầu cuối:</b> Gửi ngay <b>Username Locket</b> vào khung chat để hệ thống đưa vào luồng VIP cấp cao."
        await send_clean_message(query.message.chat_id, context, step4_text)
        return

    if data.startswith("verify_vip_spin_10k|"):
        paid_ok = await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "VIPLUOT", 10000) if hasattr(db, "check_recent_transaction_by_prefix") else False
        if paid_ok:
            await loop.run_in_executor(None, add_extra_vip_spins, user_id, 1)
            await send_clean_message(query.message.chat_id, context, "🎉 <b>MỞ KHÓA LUỒNG ĐẶC QUYỀN THÀNH CÔNG!</b>\nBạn đã có thêm 1 lượt kích VIP. Hãy thao tác lại từ đầu trên Menu nhé.")
        else:
            try: await context.bot.answer_callback_query(callback_query_id=query.id, text="⏳ Chưa nhận được tiền. Hãy thử lại!", show_alert=True)
            except: pass
        return

    if data.startswith("vup_step_"):
        step_id = data.replace("vup_step_", "")
        if step_id == "2":
            step_text = "👑 <b>QUY TRÌNH KÍCH VIP · BƯỚC 2/4 (BẬT VPN)</b>\n━━━━━━━━━━━━━━━━━━━\n▫️ <b>1. Tải app:</b> Mở Appstore tải <b>VPNify</b>.\n▫️ <b>2. Đổi IP:</b> Bật VPN sang <b>UNITED STATES (Mỹ)</b>.\n\n👇 <i>Nhấn tiếp tục khi đã bật VPN:</i>"
            await send_clean_message(query.message.chat_id, context, step_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ BẬT VPN, TIẾP TỤC ➔", callback_data="vup_step_3")]]))
        elif step_id == "3":
            step_text = "👑 <b>QUY TRÌNH KÍCH VIP · BƯỚC 3/4 (CÀI ĐẶT LẠI)</b>\n━━━━━━━━━━━━━━━━━━━\n▫️ <b>1. Tải lại app:</b> Giữ nguyên VPN, tải lại Locket Widget.\n▫️ <b>2. Khởi tạo profile:</b> Đăng nhập, chụp 1 ảnh rác đúng <b>3 giây</b> rồi thoát app ngầm.\n\n👇 <i>Tiến hành kết nối máy chủ:</i>"
            await send_clean_message(query.message.chat_id, context, step_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ LÀM XONG, TỚI BƯỚC CUỐI ➔", callback_data="vup_step_4")]]))
        elif step_id == "4":
            context.user_data["active_flow"] = "vip_premium_input"
            step_text = "👑 <b>QUY TRÌNH KÍCH VIP · BƯỚC 4/4 (HOÀN TẤT)</b>\n━━━━━━━━━━━━━━━━━━━\n🎉 Bạn đã thiết lập xong môi trường.\n💬 <b>Yêu cầu cuối:</b> Gửi ngay <b>Username Locket</b> vào khung chat để hệ thống đưa vào luồng VIP cấp cao."
            await send_clean_message(query.message.chat_id, context, step_text)
        return

    if data.startswith("buy_vip"):
        pkgs = get_vip_packages()
        pkg_key = data.replace("buy_vip", "")
        pkg = pkgs.get(f"vip{pkg_key}")
        memo = f"{pkg['memo_prefix']}{user_id}{datetime.now().strftime('%M%S')}"
        bank_code, bank_acc, bank_name = get_current_donate_info()
        qr_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={pkg['price']}&addInfo={memo}&accountName={bank_name.replace(' ', '%20')}"
        buy_msg = f"💳 <b>HÓA ĐƠN THANH TOÁN TỰ ĐỘNG</b>\n━━━━━━━━━━━━━━━━━━━\n📦 <b>Gói dịch vụ:</b> {pkg['name']}\n💰 <b>Tổng thanh toán:</b> <code>{pkg['price']:,} VNĐ</code>\n📝 <b>Nội dung CK bắt buộc:</b> <code>{memo}</code>\n━━━━━━━━━━━━━━━━━━━\n⚠️ <i>Lưu ý: Mở app ngân hàng quét mã hoặc copy đúng nội dung CK!</i>"
        await send_clean_message(query.message.chat_id, context, buy_msg, photo=qr_url, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 TÔI ĐÃ CHUYỂN KHOẢN", callback_data=f"verify_{pkg_key}")]]))
        return

    if data.startswith("verify_"):
        if "verify_spin" in data:
            spin_count = int(data.split("|")[1])
            paid_ok = await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "LUOT", spin_count * 2000) if hasattr(db, "check_recent_transaction_by_prefix") else False
            if paid_ok:
                await loop.run_in_executor(None, db.add_user_spins, user_id, spin_count)
                await send_clean_message(query.message.chat_id, context, f"🎉 <b>ĐÃ NẠP THÀNH CÔNG +{spin_count} LƯỢT!</b>")
            else:
                try: await context.bot.answer_callback_query(callback_query_id=query.id, text="⏳ Giao dịch chưa hoàn tất!", show_alert=True)
                except: pass
            return

        pkgs = get_vip_packages()
        pkg_key = data.replace("verify_", "")
        pkg = pkgs.get(f"vip{pkg_key}")
        paid_success = await loop.run_in_executor(None, db.check_recent_transaction, user_id, pkg["memo_prefix"], pkg["price"]) if hasattr(db, "check_recent_transaction") else False
        
        if paid_success:
            await loop.run_in_executor(None, db.set_user_plan, user_id, pkg["plan_name"], 7)
            await loop.run_in_executor(None, db.add_user_spins, user_id, pkg["spins"])
            if hasattr(db, "set_user_daily_quota"): await loop.run_in_executor(None, db.set_user_daily_quota, user_id, pkg["spins"])
            await send_clean_message(query.message.chat_id, context, f"🎉 <b>NÂNG CẤP {pkg['plan_name']} THÀNH CÔNG!</b>\nChào mừng bạn đến với hệ sinh thái VIP.")
        else:
            try: await context.bot.answer_callback_query(callback_query_id=query.id, text="⏳ Hệ thống chưa quét thấy giao dịch. Vui lòng chờ 5 giây rồi bấm lại!", show_alert=True)
            except: pass
        return

    if data.startswith("buy_lack_qr|"):
        parts = data.split("|")
        spin_cnt, total_prc = int(parts[1]), int(parts[2])
        paid_ok = await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "LUOT", total_prc) if hasattr(db, "check_recent_transaction_by_prefix") else False

        if paid_ok:
            await loop.run_in_executor(None, db.add_user_spins, user_id, spin_cnt)
            await send_clean_message(query.message.chat_id, context, f"🎉 <b>ĐÃ CỘNG +{spin_cnt} LƯỢT THÀNH CÔNG!</b>\nVui lòng thao tác lại Kích hoạt trên Menu.")
        else:
            try: await context.bot.answer_callback_query(callback_query_id=query.id, text="⏳ Đang đợi tiền vào tài khoản. Hãy thử lại!", show_alert=True)
            except: pass
        return

    if data.startswith("select_tunnel_"):
        tunnel_id = data.replace("select_tunnel_", "")
        cost = 1 if tunnel_id == "1" else 2
        tunnel_name = "Luồng Cơ Bản" if tunnel_id == "1" else "Luồng Ưu Tiên"
        user_spins = await loop.run_in_executor(None, db.get_user_spins, user_id) if hasattr(db, "get_user_spins") else 0

        if user_id != ADMIN_ID and user_spins < cost:
            lack_price = (cost - user_spins) * 2000
            missing_spins = cost - user_spins
            memo_spin = f"LUOT{user_id}{datetime.now().strftime('%M%S')}"
            bank_code, bank_acc, bank_name = get_current_donate_info()
            qr_lack_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={lack_price}&addInfo={memo_spin}&accountName={bank_name.replace(' ', '%20')}"
            lack_text = f"⚠️ <b>SỐ DƯ LƯỢT KHÔNG ĐỦ</b> ⚠️\n━━━━━━━━━━━━━━━━━━━\n🎯 Luồng kích: <b>{tunnel_name}</b> (Cần {cost} lượt)\n🔻 Bạn đang thiếu: <b>{missing_spins} lượt</b>\n\n💳 <b>MUA NHANH ({missing_spins} lượt = {lack_price:,}đ):</b>\n📝 Nội dung CK: <code>{memo_spin}</code>\n━━━━━━━━━━━━━━━━━━━\n👇 <i>Sau khi quét mã, hãy bấm nút Kiểm tra:</i>"
            keyboard_lack = InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 KIỂM TRA CK MUA {missing_spins} LƯỢT", callback_data=f"buy_lack_qr|{missing_spins}|{lack_price}|{memo_spin}")]])
            await send_clean_message(query.message.chat_id, context, lack_text, photo=qr_lack_url, reply_markup=keyboard_lack)
            return

        context.user_data["selected_tunnel"], context.user_data["tunnel_cost"], context.user_data["active_flow"] = tunnel_id, cost, "locket_input"
        input_prompt = f"⚙️ <b>NHẬP TÀI KHOẢN LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n🚀 Phân vùng: <b>{tunnel_name}</b> (-{cost} Lượt)\n\n💬 Vui lòng gửi <b>Username</b> hoặc <b>Đường link Locket</b> của bạn vào khung chat.\n<i>(Ví dụ: <code>trhai</code>)</i>\n\n⌨️ Gõ <code>hủy</code> nếu muốn quay lại."
        await send_clean_message(query.message.chat_id, context, input_prompt)
        return

    if data.startswith("upg|") or data.startswith("upg_vip|"):
        is_vip_flow = data.startswith("upg_vip|")
        context.user_data["active_flow"] = None
        parts = data.split("|")
        uid, username, cost = parts[1], parts[2], int(parts[3])

        user_plan = await loop.run_in_executor(None, db.get_user_plan, user_id) if hasattr(db, "get_user_plan") else "Free"

        if is_vip_flow:
            if user_id != ADMIN_ID:
                used_today = await loop.run_in_executor(None, get_vip_tunnel_usage_today, user_id)
                extra_spins = await loop.run_in_executor(None, get_extra_vip_spins, user_id)
                vip_limit = get_vip_daily_limit(user_plan)
                if used_today >= vip_limit and extra_spins <= 0:
                    try: await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ Lỗi: Bạn đã hết lượt VIP hôm nay!", show_alert=True)
                    except: pass
                    return
        else:
            user_spins = await loop.run_in_executor(None, db.get_user_spins, user_id) if hasattr(db, "get_user_spins") else 0
            if user_id != ADMIN_ID and user_spins < cost:
                try: await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ Lỗi: Số dư lượt không đủ!", show_alert=True)
                except: pass
                return

        if user_id != ADMIN_ID: 
            if is_vip_flow:
                used_today = await loop.run_in_executor(None, get_vip_tunnel_usage_today, user_id)
                vip_limit = get_vip_daily_limit(user_plan)
                if used_today < vip_limit: await loop.run_in_executor(None, increment_vip_tunnel_usage_today, user_id)
                else: await loop.run_in_executor(None, deduct_extra_vip_spin, user_id)
            else:
                await loop.run_in_executor(None, deduct_spins_custom, user_id, cost)

        priority = 4
        if user_id == ADMIN_ID: priority = 0
        elif "PREMIUM" in user_plan.upper(): priority = 1
        elif "GOLD" in user_plan.upper(): priority = 2
        elif "VIP" in user_plan.upper(): priority = 3

        position = activation_queue.qsize() + 1
        queue_wait_text = f"⏳ <b>HÀNG CHỜ MÁY CHỦ LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n🚦 Vị trí của bạn: <b>{position}</b> (Ưu tiên hạng {priority})\n\n<i>Hệ thống tự động xử lý khi đến lượt. Vui lòng không thao tác thêm!</i>"
        
        msg_box = await send_clean_message(query.message.chat_id, context, queue_wait_text)

        avatar_url = context.user_data.get("last_locket_avatar_url")
        req_item = {
            "chat_id": msg_box.chat_id, 
            "message_id": msg_box.message_id, 
            "user_id": user_id, 
            "uid": uid, 
            "username": username, 
            "cost": cost, 
            "is_vip_flow": is_vip_flow, 
            "user_plan": user_plan, 
            "full_name": query.from_user.full_name, 
            "has_photo": False,  
            "avatar_url": avatar_url
        }
        await activation_queue.put((priority, time.time(), user_id, req_item))


async def queue_worker():
    global telegram_app, activation_queue
    while True:
        try:
            item = await activation_queue.get()
            try:
                priority, req_time, user_id, req_item = item
                chat_id = req_item["chat_id"]
                message_id = req_item["message_id"]
                uid = req_item["uid"]
                username = req_item["username"]
                cost = req_item["cost"]
                is_vip_flow = req_item["is_vip_flow"]
                user_plan = req_item["user_plan"]
                full_name = req_item["full_name"]
                avatar_url = req_item["avatar_url"]

                loop = asyncio.get_running_loop()
                success = False
                msg_result = ""
                injected_avatar = None
                
                try:
                    queue_steps = [
                        ("⏳ <b>[1/3] ĐANG XỬ LÝ DỮ LIỆU</b>", "🟢 KẾT NỐI MÁY CHỦ • [██████████] 100%\n🔄 Phân tích profile Locket..."),
                        ("🔥 <b>[2/3] BẢO MẬT & ĐÓNG BĂNG</b>", "🟢 XÁC THỰC UID • [██████████] 100%\n🔒 Thiết lập tường lửa chống quét..."),
                        ("⚡ <b>[3/3] HOÀN TẤT TIẾN TRÌNH</b>", "🟢 BƠM GOLD • [██████████] 100%\n🚀 Khởi tạo chứng chỉ số..."),
                    ]
                    for title, desc in queue_steps:
                        queue_ui = f"{title}\n─────────────────────────────\n{desc}\n─────────────────────────────\n🤖 <i>Hệ thống tự động hóa siêu tốc · TrHai</i>"
                        try: await telegram_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=queue_ui, parse_mode=ParseMode.HTML)
                        except: pass
                        await asyncio.sleep(0.6)

                    success, msg_result, injected_avatar = await asyncio.wait_for(locket.inject_gold(uid, TOKEN_SETS[0], lambda x: None), timeout=15.0)
                except Exception as e:
                    success, msg_result, injected_avatar = False, f"Lỗi Máy Chủ: {str(e)}", None

                final_avatar = injected_avatar or avatar_url
                if hasattr(db, "log_request"): await loop.run_in_executor(None, db.log_request, user_id, uid, "SUCCESS" if success else "FAIL")

                if success:
                    is_vip_tier = ("GOLD" in user_plan.upper()) or ("PREMIUM" in user_plan.upper()) or ("VIP" in user_plan.upper()) or (user_id == ADMIN_ID)
                    user_dns_link = get_vip_dns() if is_vip_flow else get_free_dns()
                    
                    current_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S · %d/%m/%Y")
                    tunnel_display = "Đặc Quyền VIP" if is_vip_flow else "Cơ Bản" if cost == 1 else "Ưu Tiên"
                    cost_text = "1 Lượt Đặc Quyền" if is_vip_flow else f"{cost} Lượt Cấp"

                    final_msg = (
                        "✅ <b>BIÊN LAI KÍCH HOẠT LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>Người dùng:</b> {full_name}\n"
                        f"🔗 <b>Username:</b> <code>{username}</code>\n"
                        f"🔑 <b>UID:</b> <code>{uid}</code>\n\n"
                        f"⚡ <b>Hầm kích hoạt:</b> {tunnel_display}\n"
                        f"🎟️ <b>Đã tiêu hao:</b> -{cost_text}\n"
                        f"⏳ <b>Lúc:</b> {current_time_str}\n━━━━━━━━━━━━━━━━━━━\n"
                        "📱 <b>HƯỚNG DẪN BẮT BUỘC SAU KÍCH HOẠT:</b>\n"
                        "1️⃣ Mở Safari, tải Cấu hình DNS bên dưới.\n"
                        "2️⃣ Vào <b>Cài đặt máy -> Đã tải về hồ sơ</b> để cài đặt.\n"
                        "3️⃣ Mở Locket, tận hưởng quyền lợi!\n\n"
                        "🌐 <b>LINK TẢI CẤU HÌNH DNS BẢO MẬT:</b>\n"
                        f"👉 <a href='{user_dns_link}'>BẤM VÀO ĐÂY ĐỂ CÀI DNS</a>"
                    )

                    try: await telegram_app.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except: pass
                    
                    success_photo = final_avatar if final_avatar and final_avatar.startswith("http") else (db.get_config("success_image_id", None) if hasattr(db, "get_config") else None)
                    
                    if success_photo:
                        try: await telegram_app.bot.send_photo(chat_id=chat_id, photo=success_photo, caption=final_msg, parse_mode=ParseMode.HTML)
                        except: await telegram_app.bot.send_message(chat_id=chat_id, text=final_msg, parse_mode=ParseMode.HTML)
                    else: await telegram_app.bot.send_message(chat_id=chat_id, text=final_msg, parse_mode=ParseMode.HTML)

                else:
                    if user_id != ADMIN_ID: 
                        if is_vip_flow: await loop.run_in_executor(None, add_extra_vip_spins, user_id, 1)
                        else: await loop.run_in_executor(None, db.add_user_spins, user_id, cost)
                    err_text = f"❌ <b>KÍCH HOẠT THẤT BẠI:</b> {msg_result}\n🔄 <i>Lượt kích hoạt đã được hoàn trả lại ví cho bạn.</i>"
                    try: await telegram_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=err_text, parse_mode=ParseMode.HTML)
                    except: pass
                
            except Exception as e:
                logger.error(f"Lỗi logic bên trong Worker: {e}")
            finally:
                activation_queue.task_done()
                
        except Exception as e:
            logger.error(f"Lỗi đọc Hàng Đợi: {e}")
            await asyncio.sleep(1)


def run_telegram_bot_background():
    global telegram_app, main_event_loop, activation_queue
    try:
        logging.basicConfig(format="%(message)s", level=logging.INFO)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        main_event_loop = loop
        
        activation_queue = asyncio.PriorityQueue()

        app = ApplicationBuilder().token(BOT_TOKEN).build()
        telegram_app = app

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        
        app.add_handler(CommandHandler("addchannel", add_channel_command))
        app.add_handler(CommandHandler("delchannel", del_channel_command)) 
        app.add_handler(CommandHandler("channels", list_channels_command))
        app.add_handler(CommandHandler("editvip", edit_vip_command))
        
        app.add_handler(CommandHandler("setdns", set_dns_command))
        app.add_handler(CommandHandler("setnextdnskey", set_nextdns_apikey_command))
        app.add_handler(CommandHandler("setdonate", set_donate_command))
        app.add_handler(CommandHandler("setimg", set_success_image_command))
        app.add_handler(CommandHandler("setguide", set_guide_text_command))
        app.add_handler(CommandHandler("setvideo", set_guide_video_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        app.add_handler(CommandHandler("addspin", addspin_command))
        app.add_handler(CommandHandler("delspin", delspin_command))
        app.add_handler(CommandHandler("setplan", setplan_command))
        app.add_handler(CommandHandler("unban", unban_command))

        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        async def startup():
            await app.initialize()
            await app.start()
            
            asyncio.create_task(queue_worker())
            
            try:
                await app.bot.set_my_commands([
                    BotCommand("start", "🏠 Mở Bảng Điều Khiển Locket"),
                    BotCommand("help", "📖 Xem Hướng Dẫn DNS Bảo Mật")
                ])
            except Exception: pass

            try:
                await app.bot.set_my_commands([
                    BotCommand("start", "🏠 Bảng Điều Khiển"),
                    BotCommand("broadcast", "📢 Phát thông báo"),
                    BotCommand("channels", "📋 Xem danh sách Kênh"),
                    BotCommand("delchannel", "🗑️ Xóa sạch tất cả Kênh"),
                    BotCommand("addchannel", "➕ Thêm Kênh Yêu Cầu"),
                    BotCommand("editvip", "👑 Sửa Giá/Tên Gói VIP"),
                    BotCommand("addspin", "➕ Cộng lượt (ID + Lượt)"),
                    BotCommand("delspin", "➖ Trừ lượt (ID + Lượt)"),
                    BotCommand("setplan", "👑 Chỉnh gói (ID + Gói + Ngày)"),
                    BotCommand("unban", "🔓 Mở khóa Spam"),
                    BotCommand("setdns", "🌐 Cài DNS Free"),
                    BotCommand("setnextdnskey", "👑 Cài DNS VIP"),
                    BotCommand("setdonate", "💳 Cài Ngân Hàng"),
                    BotCommand("setimg", "🖼️ Cài Ảnh Biên Lai"),
                    BotCommand("setguide", "📖 Cài HD Text"),
                ], scope=BotCommandScopeChat(ADMIN_ID))
            except Exception as e: pass

            render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
            if render_url: 
                await app.bot.set_webhook(url=f"{render_url}/telegram-webhook")

        loop.run_until_complete(startup())
        loop.run_forever()
    except Exception as e:
        logger.error(f"LỖI NGHIÊM TRỌNG TRONG LUỒNG BOT: {e}")
        print(f"🔥 LỖI NGHIÊM TRỌNG: {e}")

def run_bot():
    start_background_bot()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

if __name__ == "__main__":
    run_bot()
