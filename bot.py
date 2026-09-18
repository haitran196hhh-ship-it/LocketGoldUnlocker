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
activation_queue = None  

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# ĐẶT GÓI 89K THÀNH NO DNS 1 NĂM
VIP_PACKAGES_DEFAULT = {
    "vip1": {"name": "⚡ VIP NEW (1 Tuần)", "price": 10000, "memo_prefix": "VIP1", "spins": 10, "plan_name": "VIP NEW"},
    "vip2": {"name": "🔥 VIP GOLD (1 Tuần)", "price": 15000, "memo_prefix": "VIP2", "spins": 15, "plan_name": "VIP GOLD"},
    "vip3": {"name": "👑 LOCKET GOLD NO DNS 1 NĂM", "price": 89000, "memo_prefix": "VIP3", "spins": 20, "plan_name": "VIP PREMIUM"},
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
    if "PREMIUM" in p or "NO DNS" in p: return 10
    if "GOLD" in p: return 3
    if "NEW" in p or "VIP" in p: return 1
    return 0

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

def upgrade_db_schema():
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("ALTER TABLE user_accounts ADD COLUMN IF NOT EXISTS extra_vip_spins INTEGER DEFAULT 0")
        c.execute("CREATE TABLE IF NOT EXISTS usage_logs (user_id BIGINT, date VARCHAR(50), count INTEGER, UNIQUE(user_id, date))")
        c.execute("CREATE TABLE IF NOT EXISTS force_channels (chat_id TEXT PRIMARY KEY, link TEXT)")
        conn.commit()
        c.close(); conn.close()
    except Exception: pass

upgrade_db_schema()

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
            if member.status in ['left', 'kicked']: not_joined.append(ch["link"])
        except: not_joined.append(ch["link"])
            
    if not_joined:
        keyboard = []
        for idx, link in enumerate(not_joined):
            keyboard.append([InlineKeyboardButton(f"👉 Tham gia Nhóm/Kênh {idx+1}", url=link)])
        keyboard.append([InlineKeyboardButton("✅ TÔI ĐÃ THAM GIA ĐỦ", callback_data="check_joined")])
        text = "⚠️ <b>YÊU CẦU BẮT BUỘC TỪ HỆ THỐNG</b>\n━━━━━━━━━━━━━━━━━━━\nĐể sử dụng Bot, bạn phải tham gia đủ các Nhóm/Kênh cộng đồng bên dưới.\n\n👇 <i>Sau khi tham gia xong, hãy bấm nút <b>ĐÃ THAM GIA ĐỦ</b> để tiếp tục!</i>"
        await send_clean_message(update.effective_chat.id, context, text, reply_markup=InlineKeyboardMarkup(keyboard), delete_user_msg=update.message)
        return False
    return True

def get_extra_vip_spins(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT extra_vip_spins FROM user_accounts WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        c.close(); conn.close()
        return row[0] if row and row[0] else 0
    except: return 0

def add_extra_vip_spins(user_id, amount):
    conn = db.get_db_connection()
    c = conn.cursor()
    db._ensure_user_account(c, user_id)
    c.execute("UPDATE user_accounts SET extra_vip_spins = COALESCE(extra_vip_spins, 0) + %s WHERE user_id = %s", (amount, user_id))
    conn.commit()
    c.close(); conn.close()

def deduct_extra_vip_spin(user_id):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE user_accounts SET extra_vip_spins = GREATEST(0, COALESCE(extra_vip_spins, 0) - 1) WHERE user_id = %s", (user_id,))
    conn.commit()
    c.close(); conn.close()

def deduct_spins_custom(user_id, amount):
    conn = db.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT spins FROM user_accounts WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    current_spins = row[0] if row else 0
    c.execute("UPDATE user_accounts SET spins = %s WHERE user_id = %s", (max(0, current_spins - int(amount)), user_id))
    conn.commit()
    c.close(); conn.close()

def get_vip_tunnel_usage_today(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        today = datetime.now(VN_TZ).strftime("VIPTUN_%Y-%m-%d")
        c.execute("SELECT count FROM usage_logs WHERE user_id = %s AND date = %s", (user_id, today))
        row = c.fetchone()
        c.close(); conn.close()
        return row[0] if row else 0
    except: return 0

def increment_vip_tunnel_usage_today(user_id):
    try:
        conn = db.get_db_connection()
        c = conn.cursor()
        today = datetime.now(VN_TZ).strftime("VIPTUN_%Y-%m-%d")
        c.execute("INSERT INTO usage_logs (user_id, date, count) VALUES (%s, %s, 1) ON CONFLICT (user_id, date) DO UPDATE SET count = usage_logs.count + 1", (user_id, today))
        conn.commit()
        c.close(); conn.close()
    except: pass

def get_welcome_text(user_name):
    return (
        "🎇 <b>HỆ THỐNG LOCKET GOLD CỦA ANH HẢI</b> 🎇\n"
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
        "📖 <b>HƯỚNG DẪN SỬ DỤNG LOCKET GOLD</b>\n"
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

async def send_clean_message(chat_id, context, text, reply_markup=None, photo=None, delete_user_msg=None):
    if delete_user_msg:
        try: await delete_user_msg.delete()
        except: pass
    old_msg_id = context.user_data.get("last_bot_msg_id")
    if old_msg_id:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except: pass
    if photo: msg = await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else: msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    context.user_data["last_bot_msg_id"] = msg.message_id
    return msg

async def admin_check(update: Update): return update.effective_user.id == ADMIN_ID

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        chat_id, link = context.args[0], context.args[1]
        try: await context.bot.get_chat_member(chat_id=chat_id, user_id=context.bot.id)
        except Exception as e:
            await context.bot.send_message(update.effective_chat.id, f"❌ Lỗi Bot chưa được làm Admin nhóm: {e}")
            return
        conn = db.get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO force_channels (chat_id, link) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET link = EXCLUDED.link", (chat_id, link))
        conn.commit(); c.close(); conn.close()
        await context.bot.send_message(update.effective_chat.id, f"✅ Thêm kênh thành công!")
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: /addchannel [ID_Nhóm] [Link]")

async def del_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    conn = db.get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM force_channels")
    conn.commit(); c.close(); conn.close()
    await context.bot.send_message(update.effective_chat.id, "✅ Đã xóa toàn bộ Kênh bắt buộc!")

async def list_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    channels = get_force_channels()
    if not channels: await context.bot.send_message(update.effective_chat.id, "📋 Trống.")
    else: await context.bot.send_message(update.effective_chat.id, f"📋 Đang có {len(channels)} Kênh bắt buộc.")

async def edit_vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    try:
        pkg_key, price, spins = context.args[0].lower(), int(context.args[1]), int(context.args[2])
        plan_name = " ".join(context.args[3:])
        if pkg_key not in ["vip1", "vip2", "vip3"]: raise ValueError()
        pkgs = get_vip_packages()
        pkgs[pkg_key].update({"price": price, "spins": spins, "name": f"🌟 {plan_name} ({price:,}đ)", "plan_name": plan_name})
        save_vip_packages(pkgs)
        await context.bot.send_message(update.effective_chat.id, f"✅ Sửa {pkg_key} thành công!")
    except: await context.bot.send_message(update.effective_chat.id, "⚠️ Cú pháp: /editvip [vip1/vip2/vip3] [Giá] [Lượt] [Tên Gói Mới]")

async def set_dns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if hasattr(db, "set_config"): db.set_config("dns_url", context.args[0])
    await context.bot.send_message(update.effective_chat.id, f"✅ Đổi DNS Free xong!")

async def set_nextdns_apikey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if hasattr(db, "set_config"): db.set_config("vip_dns_url", context.args[0])
    await context.bot.send_message(update.effective_chat.id, f"✅ Đổi DNS VIP xong!")

async def set_guide_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    new_text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if hasattr(db, "set_config"): db.set_config("guide_text", new_text)
    await context.bot.send_message(update.effective_chat.id, "✅ Lưu HD Text xong!")

async def set_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if hasattr(db, "set_config"):
        db.set_config("bank_code", context.args[0])
        db.set_config("bank_acc", context.args[1])
        db.set_config("bank_name", " ".join(context.args[2:]))
    await context.bot.send_message(update.effective_chat.id, "✅ Lưu ngân hàng xong!")

async def set_success_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    if hasattr(db, "set_config"): db.set_config("success_image_id", update.message.reply_to_message.photo[-1].file_id)
    await context.bot.send_message(update.effective_chat.id, "✅ Lưu ảnh thông báo xong!")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    broadcast_msg = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    users = db.get_all_users() if hasattr(db, "get_all_users") else []
    for uid in users:
        try: await context.bot.send_message(chat_id=uid, text=broadcast_msg, parse_mode=ParseMode.HTML)
        except: pass

async def addspin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    uid, amount = int(context.args[0]), int(context.args[1])
    db.add_user_spins(uid, amount)
    await context.bot.send_message(update.effective_chat.id, f"✅ Đã cộng {amount} lượt cho {uid}")

async def delspin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    uid, amount = int(context.args[0]), int(context.args[1])
    deduct_spins_custom(uid, amount)
    await context.bot.send_message(update.effective_chat.id, f"✅ Đã trừ {amount} lượt cho {uid}")

async def setplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    uid, days = int(context.args[0]), int(context.args[-1])
    plan = " ".join(context.args[1:-1])
    db.set_user_plan(uid, plan, days)
    await context.bot.send_message(update.effective_chat.id, f"✅ Đã gán gói {plan} ({days} ngày) cho {uid}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update): return
    try: await update.message.delete()
    except: pass
    uid = int(context.args[0])
    if uid in SPAM_MEMORY: SPAM_MEMORY[uid] = {"times": [], "warnings": 0, "banned": False}
    await context.bot.send_message(update.effective_chat.id, f"✅ Đã unban ID: {uid}")

flask_app = Flask(__name__)
_bot_thread_started = False
_bot_lock = threading.Lock()

def start_background_bot():
    global _bot_thread_started
    with _bot_lock:
        if not _bot_thread_started:
            threading.Thread(target=run_telegram_bot_background, daemon=True).start()
            _bot_thread_started = True

@flask_app.before_request
def ensure_bot(): start_background_bot()

@flask_app.route("/", methods=["GET"])
def home(): return "Locket Gold System is Online!", 200

@flask_app.route("/telegram-webhook", methods=["POST", "GET"])
def telegram_webhook():
    global telegram_app, main_event_loop
    if request.method == "GET": return jsonify({"status": "Webhook Active"}), 200
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "No JSON"}), 400
    for _ in range(10):
        if telegram_app and main_event_loop: break
        time.sleep(0.5)
    if not telegram_app or not main_event_loop: return jsonify({"error": "Initializing"}), 503
    try:
        update = Update.de_json(data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), main_event_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

def process_vip_payment(target_user_id, vip_code, transfer_amount):
    pkgs = get_vip_packages()
    pkg = pkgs.get(vip_code.lower())
    if not pkg or transfer_amount < pkg["price"]: return False, None
    if hasattr(db, "add_user_spins"): db.add_user_spins(target_user_id, pkg["spins"])
    # NẾU MUA GÓI VIP3 (89K) -> CẤP LUÔN HẠN 365 NGÀY (1 NĂM)
    days = 365 if vip_code.lower() == "vip3" else 7
    if hasattr(db, "set_user_plan"): db.set_user_plan(target_user_id, pkg["plan_name"], days=days)
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
                    success_noti = f"🎉 <b>GIAO DỊCH THÀNH CÔNG!</b>\n━━━━━━━━━━━━━━━━━━━\n👑 <b>Hạng:</b> {result['plan_name']}\n🎁 <b>Lượt/ngày:</b> +{result['spins']} Lượt\n💰 <b>Thực nhận:</b> <code>{transfer_amount:,.0f}đ</code>\n━━━━━━━━━━━━━━━━━━━\n🚀 <i>Hệ thống đã kích hoạt thành công!</i>"
                    asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=target_user_id, text=success_noti, parse_mode=ParseMode.HTML), main_event_loop)
                return jsonify({"success": True}), 200

        elif "LUOT" in content and "VIPLUOT" not in content:
            target_user_id = None
            import re
            match = re.search(r"LUOT(\d+)(\d{4})", content)
            if match: target_user_id = int(match.group(1))
            if target_user_id and transfer_amount >= 2000:
                spins_added = int(transfer_amount // 2000)
                if spins_added > 0:
                    if hasattr(db, "add_user_spins"): db.add_user_spins(target_user_id, spins_added)
                    if hasattr(db, "log_transaction"): db.log_transaction(target_user_id, transfer_amount, content)
                    if telegram_app and main_event_loop:
                        success_noti = f"🎉 <b>NẠP LƯỢT THÀNH CÔNG!</b>\n🎟️ <b>Đã cộng:</b> +{spins_added} Lượt"
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
                        success_noti = f"🎉 <b>MỞ KHÓA LUỒNG VIP THÀNH CÔNG!</b>\n👑 <b>Đã cấp:</b> +{vip_spins_added} Lượt Đặc Quyền"
                        asyncio.run_coroutine_threadsafe(telegram_app.bot.send_message(chat_id=target_user_id, text=success_noti, parse_mode=ParseMode.HTML), main_event_loop)
                return jsonify({"success": True}), 200
        return jsonify({"success": True}), 200
    except: return jsonify({"success": False}), 200

def _get_expiry_date(uid):
    try:
        conn = db.get_db_connection(); c = conn.cursor()
        c.execute("SELECT expires_at FROM user_accounts WHERE user_id = %s", (uid,))
        row = c.fetchone()
        c.close(); conn.close()
        return row[0] if row and row[0] else None
    except: return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    spam_status = check_spam(user.id)
    if spam_status == 'banned': return
    if spam_status == 'banned_now':
        await context.bot.send_message(update.effective_chat.id, "🚫 <b>BẠN ĐÃ BỊ CHẶN HOÀN TOÀN!</b>", parse_mode=ParseMode.HTML)
        return
    if spam_status.startswith('warn_'):
        await context.bot.send_message(update.effective_chat.id, f"⚠️ <b>CẢNH BÁO SPAM ({spam_status.split('_')[1]}/3)</b>", parse_mode=ParseMode.HTML)
        return

    if not await check_force_sub(update, context): return
        
    context.user_data["active_flow"] = None
    loop = asyncio.get_running_loop()
    if hasattr(db, "check_and_reset_daily_spins"): await loop.run_in_executor(None, db.check_and_reset_daily_spins, user.id)
    await send_clean_message(update.effective_chat.id, context, get_welcome_text(user.full_name), reply_markup=get_bottom_reply_keyboard(), delete_user_msg=update.message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id) == 'banned': return
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
    if spam_status == 'banned_now': return
    if spam_status.startswith('warn_'): return
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
                lack_text = f"⚠️ <b>ĐÃ HẾT LƯỢT LUỒNG ĐẶC QUYỀN HÔM NAY</b> ⚠️\n━━━━━━━━━━━━━━━━━━━\nGói <b>{user_plan}</b> của bạn được sử dụng <b>{vip_limit} lần miễn phí / ngày.</b>\n💳 <b>MỞ THÊM 1 LƯỢT (10.000đ):</b>\n📝 Nội dung CK: <code>{memo_spin}</code>"
                keyboard_lack = InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 KIỂM TRA CK MUA 1 LƯỢT", callback_data=f"verify_vip_spin_10k|{memo_spin}")]])
                await send_clean_message(update.effective_chat.id, context, lack_text, reply_markup=keyboard_lack, photo=qr_lack_url, delete_user_msg=update.message)
                return

            # NẾU MUA GÓI 89K (PREMIUM ULTIMATE) HOẶC ADMIN -> BỎ QUA TẤT CẢ CÁC BƯỚC DNS, GỬI USERNAME LÀ XONG!
            if "PREMIUM" in user_plan.upper() or user_id == ADMIN_ID:
                step1_text = "👑 <b>LUỒNG ĐẶC QUYỀN: LOCKET GOLD NO DNS 1 NĂM</b>\n━━━━━━━━━━━━━━━━━━━\n▫️ Bản quyền kích hoạt trực tiếp từ máy chủ RevenueCat.\n▫️ <b>Không cần cài DNS, Không cần VPN</b>, giữ nguyên ứng dụng gốc.\n\n👇 <i>Bấm nút bên dưới để tiến hành:</i>"
                await send_clean_message(update.effective_chat.id, context, step1_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ KÍCH HOẠT NO DNS NGAY ➔", callback_data="vup_step_4")]]), delete_user_msg=update.message)
                return
            else:
                step1_text = "👑 <b>LUỒNG ĐẶC QUYỀN · BƯỚC 1/4 (LÀM SẠCH)</b>\n━━━━━━━━━━━━━━━━━━━\n▫️ <b>1. Xóa cấu hình cũ:</b> Cài đặt máy -> VPN & Quản lý thiết bị -> Xóa Profile DNS cũ.\n▫️ <b>2. Xóa bộ đệm:</b> Xóa hoàn toàn app Locket khỏi máy.\n\n👇 <i>Xác nhận hoàn tất:</i>"
                await send_clean_message(update.effective_chat.id, context, step1_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ XONG, TIẾP TỤC ➔", callback_data="vup_step_2")]]), delete_user_msg=update.message)
                return

        elif text == "💎 GÓI VIP":
            pkgs = get_vip_packages()
            store_text = "💎 <b>CỬA HÀNG NÂNG CẤP VIP</b>\n━━━━━━━━━━━━━━━━━━━\n"
            for k in ["vip1", "vip2", "vip3"]:
                store_text += f"{pkgs[k]['name']}: {pkgs[k]['spins']} lượt/ngày\n"
            store_text += "━━━━━━━━━━━━━━━━━━━\n👇 <i>Chọn gói nâng cấp tương ứng bên dưới:</i>"
            keyboard = [
                [InlineKeyboardButton(pkgs["vip1"]["name"], callback_data="buy_vip1")],
                [InlineKeyboardButton(pkgs["vip2"]["name"], callback_data="buy_vip2")],
                [InlineKeyboardButton(pkgs["vip3"]["name"], callback_data="buy_vip3")]
            ]
            await send_clean_message(update.effective_chat.id, context, store_text, reply_markup=InlineKeyboardMarkup(keyboard), delete_user_msg=update.message)
            return

        elif text == "🎟️ NẠP LƯỢT":
            context.user_data["active_flow"] = "custom_spin_input"
            await send_clean_message(update.effective_chat.id, context, "🎟️ Nhập <b>số lượng lượt</b> muốn mua (2.000đ/Lượt):", delete_user_msg=update.message)
            return

        elif text == "🪪 TÀI KHOẢN":
            expiry_raw = await loop.run_in_executor(None, _get_expiry_date, user_id)
            expiry_date = "Vô thời hạn" if user_plan == "Thường (Free)" else (expiry_raw if expiry_raw else "Không xác định")
            account_text = f"🪪 <b>THẺ TÀI KHOẢN CỦA BẠN</b>\n👤 <b>Chủ TK:</b> {update.effective_user.full_name}\n🆔 <b>ID:</b> <code>{user_id}</code>\n👑 <b>Hạng:</b> <code>{user_plan}</code>\n⏳ <b>Hạn:</b> <code>{expiry_date}</code>\n🎟️ <b>Số dư:</b> <b>{user_spins} Lượt</b>"
            await send_clean_message(update.effective_chat.id, context, account_text, delete_user_msg=update.message)
            return

        elif text == "🖼️ TÌM AVATAR":
            context.user_data["active_flow"] = "avt_input"
            await send_clean_message(update.effective_chat.id, context, "🖼️ Gửi <b>Username Locket</b> để tra ảnh gốc:", delete_user_msg=update.message)
            return

        elif text == "📖 HƯỚNG DẪN DNS":
            is_vip_tier = "GOLD" in user_plan.upper() or "PREMIUM" in user_plan.upper() or "VIP" in user_plan.upper() or user_id == ADMIN_ID
            dns_link = get_vip_dns() if is_vip_tier else get_free_dns()
            guide_text = get_guide_content().format(display_name=update.effective_user.full_name, user_plan=user_plan, dns_link=dns_link, nextdns_server_only="dns.nextdns.io")
            await send_clean_message(update.effective_chat.id, context, guide_text, delete_user_msg=update.message)
            return
            
        elif text == "💬 HỖ TRỢ":
            await send_clean_message(update.effective_chat.id, context, "💬 Mọi thắc mắc liên hệ Admin: 👉 @TRANHAI_307", delete_user_msg=update.message)
            return

    if not current_flow: 
        try: await update.message.delete()
        except: pass
        await send_clean_message(update.effective_chat.id, context, get_welcome_text(update.effective_user.full_name), reply_markup=get_bottom_reply_keyboard())
        return

    if current_flow == "avt_input":
        username = text.split("locket.cam/")[-1].split("?")[0].replace("@", "") if "locket.cam/" in text else text.replace("@", "")
        await send_clean_message(update.effective_chat.id, context, "🔍 Đang trích xuất ảnh gốc...", delete_user_msg=update.message)
        try:
            uid = await locket.resolve_uid(username)
            avatar_url = await locket.get_user_avatar(text) if uid else None
            if avatar_url: await send_clean_message(update.effective_chat.id, context, f"🖼️ <b>AVATAR BẢN GỐC:</b> <code>{username}</code>\n🔗 <a href='{avatar_url}'>Mở ảnh chất lượng cao</a>", photo=avatar_url)
            else: await send_clean_message(update.effective_chat.id, context, "❌ Lỗi: User không tồn tại hoặc chưa cài avatar.")
        except Exception as e: await send_clean_message(update.effective_chat.id, context, f"❌ Lỗi: {e}")
        context.user_data["active_flow"] = None
        return

    if current_flow == "custom_spin_input":
        if not text.isdigit() or int(text) <= 0:
            await send_clean_message(update.effective_chat.id, context, "❌ Vui lòng nhập số đếm (VD: 5).", delete_user_msg=update.message)
            return
        spin_count = int(text)
        total_price = spin_count * 2000
        memo = f"LUOT{user_id}{datetime.now().strftime('%M%S')}"
        bank_code, bank_acc, bank_name = get_current_donate_info()
        qr_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={total_price}&addInfo={memo}&accountName={bank_name.replace(' ', '%20')}"
        await send_clean_message(update.effective_chat.id, context, f"💳 <b>HÓA ĐƠN {spin_count} LƯỢT</b>\n💰 Tiền: <code>{total_price:,} VNĐ</code>\n📝 ND: <code>{memo}</code>", photo=qr_url, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 KIỂM TRA CK TỰ ĐỘNG", callback_data=f"verify_spin|{spin_count}")]]), delete_user_msg=update.message)
        context.user_data["active_flow"] = None
        return

    if current_flow == "locket_input" or current_flow == "vip_premium_input":
        is_vip_flow = current_flow == "vip_premium_input"
        username = text.split("locket.cam/")[-1].split("?")[0].replace("@", "") if "locket.cam/" in text else text.replace("@", "")
        await send_clean_message(update.effective_chat.id, context, "🔍 Đang phân tích hồ sơ Locket...", delete_user_msg=update.message)
        uid = await locket.resolve_uid(username)
        if not uid:
            await send_clean_message(update.effective_chat.id, context, "❌ Không tìm thấy Username Locket này!")
            return

        cost = context.user_data.get("tunnel_cost", 1) if not is_vip_flow else 0
        tunnel_name = "Luồng Cơ Bản / Ưu Tiên" if not is_vip_flow else "Luồng Đặc Quyền (NO DNS 1 NĂM)"
        cost_text = f"{cost} Lượt Thường" if not is_vip_flow else "1 Lượt Đặc Quyền"
        callback_action = f"upg_vip|{uid}|{username}|{cost}" if is_vip_flow else f"upg|{uid}|{username}|{cost}"
        reinput_action = "reinput_vip" if is_vip_flow else f"reinput_locket_{cost}"

        is_active = await locket.check_user_gold_status(uid, TOKEN_SETS[0])
        status_badge = "🟢 ACTIVE (ĐÃ CÓ GOLD)" if is_active else "🔴 CHƯA ACTIVE (FREE)"
        avatar_url = await locket.get_user_avatar(text)
        if avatar_url: context.user_data["last_locket_avatar_url"] = avatar_url

        confirm_msg = f"✅ <b>TÌM THẤY TÀI KHOẢN ĐÍCH:</b>\n👤 <b>User:</b> <code>{username}</code>\n⚡ <b>Trạng thái:</b> {status_badge}\n🚀 <b>Phân vùng:</b> {tunnel_name}\n➖ <b>Sẽ tiêu:</b> -{cost_text}\n\n👇 <i>Bấm XÁC NHẬN để Bơm Gold!</i>"
        keyboard_confirm = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ XÁC NHẬN BƠM GOLD", callback_data=callback_action)], [InlineKeyboardButton("🔄 ĐỔI TÀI KHOẢN KHÁC", callback_data=reinput_action)]])
        
        if avatar_url and avatar_url.startswith("http"): await send_clean_message(update.effective_chat.id, context, confirm_msg, photo=avatar_url, reply_markup=keyboard_confirm)
        else: await send_clean_message(update.effective_chat.id, context, confirm_msg, reply_markup=keyboard_confirm)
        context.user_data["active_flow"] = None

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer()
    except: pass
    data, user_id = query.data, query.from_user.id
    if check_spam(user_id) == 'banned': return
    loop = asyncio.get_running_loop()

    if data == "check_joined":
        channels = get_force_channels()
        not_joined_links = []
        for ch in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
                if member.status in ['left', 'kicked']: not_joined_links.append(ch["link"])
            except: not_joined_links.append(ch["link"])
        if not_joined_links:
            keyboard = [[InlineKeyboardButton(f"👉 Tham gia Nhóm {idx+1}", url=link)] for idx, link in enumerate(not_joined_links)]
            keyboard.append([InlineKeyboardButton("✅ TÔI ĐÃ THAM GIA ĐỦ", callback_data="check_joined")])
            await send_clean_message(update.effective_chat.id, context, "❌ <b>BẠN CHƯA THAM GIA ĐỦ NHÓM YÊU CẦU!</b>", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await send_clean_message(update.effective_chat.id, context, get_welcome_text(query.from_user.full_name), reply_markup=get_bottom_reply_keyboard())
        return

    if data.startswith("reinput_locket_"):
        cost = int(data.split("_")[-1])
        tunnel_id = "1" if cost == 1 else "2"
        context.user_data.update({"selected_tunnel": tunnel_id, "tunnel_cost": cost, "active_flow": "locket_input"})
        await send_clean_message(query.message.chat_id, context, f"⚙️ <b>NHẬP TÀI KHOẢN LOCKET</b> (-{cost} Lượt)\nGửi Username hoặc Link Locket của bạn:")
        return

    if data == "reinput_vip":
        context.user_data["active_flow"] = "vip_premium_input"
        await send_clean_message(query.message.chat_id, context, "👑 <b>GỬI USERNAME LOCKET CẦN BƠM NO DNS:</b>")
        return

    if data.startswith("verify_vip_spin_10k|"):
        if await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "VIPLUOT", 10000):
            await loop.run_in_executor(None, add_extra_vip_spins, user_id, 1)
            await send_clean_message(query.message.chat_id, context, "🎉 <b>MỞ KHÓA LUỒNG VIP THÀNH CÔNG!</b>")
        return

    if data.startswith("vup_step_"):
        step_id = data.replace("vup_step_", "")
        if step_id == "2": await send_clean_message(query.message.chat_id, context, "👑 <b>BƯỚC 2/4 (BẬT VPN Mỹ)</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ BẬT VPN, TIẾP TỤC ➔", callback_data="vup_step_3")]]))
        elif step_id == "3": await send_clean_message(query.message.chat_id, context, "👑 <b>BƯỚC 3/4 (CÀI LẠI APP)</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ ĐÃ LÀM XONG, TIẾP ➔", callback_data="vup_step_4")]]))
        elif step_id == "4":
            context.user_data["active_flow"] = "vip_premium_input"
            await send_clean_message(query.message.chat_id, context, "👑 <b>HÃY GỬI USERNAME LOCKET CỦA BẠN ĐỂ TIẾN HÀNH BƠM GOLD NO DNS:</b>")
        return

    if data.startswith("buy_vip"):
        pkgs, pkg_key = get_vip_packages(), data.replace("buy_vip", "")
        pkg = pkgs.get(f"vip{pkg_key}")
        memo, bank_code, bank_acc, bank_name = f"{pkg['memo_prefix']}{user_id}{datetime.now().strftime('%M%S')}", *get_current_donate_info()
        qr_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={pkg['price']}&addInfo={memo}&accountName={bank_name.replace(' ', '%20')}"
        await send_clean_message(query.message.chat_id, context, f"💳 <b>MUA {pkg['name']}</b>\n💰 Tiền: <code>{pkg['price']:,} VNĐ</code>\n📝 ND: <code>{memo}</code>", photo=qr_url, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ĐÃ CHUYỂN KHOẢN", callback_data=f"verify_{pkg_key}")]]))
        return

    if data.startswith("verify_"):
        if "verify_spin" in data:
            spin_count = int(data.split("|")[1])
            if await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "LUOT", spin_count * 2000):
                await loop.run_in_executor(None, db.add_user_spins, user_id, spin_count)
                await send_clean_message(query.message.chat_id, context, f"🎉 <b>ĐÃ NẠP THÀNH CÔNG +{spin_count} LƯỢT!</b>")
            return
        pkgs, pkg_key = get_vip_packages(), data.replace("verify_", "")
        pkg = pkgs.get(f"vip{pkg_key}")
        if await loop.run_in_executor(None, db.check_recent_transaction, user_id, pkg["memo_prefix"], pkg["price"]):
            days = 365 if pkg_key == "3" else 7
            await loop.run_in_executor(None, db.set_user_plan, user_id, pkg["plan_name"], days)
            await loop.run_in_executor(None, db.add_user_spins, user_id, pkg["spins"])
            if hasattr(db, "set_user_daily_quota"): await loop.run_in_executor(None, db.set_user_daily_quota, user_id, pkg["spins"])
            await send_clean_message(query.message.chat_id, context, f"🎉 <b>NÂNG CẤP {pkg['plan_name']} THÀNH CÔNG!</b>")
        return

    if data.startswith("buy_lack_qr|"):
        spin_cnt, total_prc = int(data.split("|")[1]), int(data.split("|")[2])
        if await loop.run_in_executor(None, db.check_recent_transaction_by_prefix, user_id, "LUOT", total_prc):
            await loop.run_in_executor(None, db.add_user_spins, user_id, spin_cnt)
            await send_clean_message(query.message.chat_id, context, f"🎉 <b>ĐÃ CỘNG +{spin_cnt} LƯỢT THÀNH CÔNG!</b>")
        return

    if data.startswith("select_tunnel_"):
        tunnel_id = data.replace("select_tunnel_", "")
        cost, tunnel_name = (1, "Luồng Cơ Bản") if tunnel_id == "1" else (2, "Luồng Ưu Tiên")
        user_spins = await loop.run_in_executor(None, db.get_user_spins, user_id) if hasattr(db, "get_user_spins") else 0
        if user_id != ADMIN_ID and user_spins < cost:
            lack_price, missing_spins = (cost - user_spins) * 2000, cost - user_spins
            memo_spin = f"LUOT{user_id}{datetime.now().strftime('%M%S')}"
            bank_code, bank_acc, bank_name = get_current_donate_info()
            qr_lack_url = f"https://img.vietqr.io/image/{bank_code}-{bank_acc}-compact2.png?amount={lack_price}&addInfo={memo_spin}&accountName={bank_name.replace(' ', '%20')}"
            await send_clean_message(query.message.chat_id, context, f"⚠️ <b>THIẾU {missing_spins} LƯỢT</b>\n💳 MUA NHANH ({lack_price:,}đ):\n📝 ND: <code>{memo_spin}</code>", photo=qr_lack_url, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 KIỂM TRA CK MUA THÊM LƯỢT", callback_data=f"buy_lack_qr|{missing_spins}|{lack_price}|{memo_spin}")]]))
            return
        context.user_data.update({"selected_tunnel": tunnel_id, "tunnel_cost": cost, "active_flow": "locket_input"})
        await send_clean_message(query.message.chat_id, context, f"⚙️ <b>NHẬP TÀI KHOẢN LOCKET</b> (-{cost} Lượt)\nGửi Username hoặc Link Locket của bạn:")
        return

    if data.startswith("upg|") or data.startswith("upg_vip|"):
        is_vip_flow = data.startswith("upg_vip|")
        context.user_data["active_flow"] = None
        parts = data.split("|")
        uid, username, cost = parts[1], parts[2], int(parts[3])
        user_plan = await loop.run_in_executor(None, db.get_user_plan, user_id) if hasattr(db, "get_user_plan") else "Free"

        if is_vip_flow:
            if user_id != ADMIN_ID:
                if await loop.run_in_executor(None, get_vip_tunnel_usage_today, user_id) >= get_vip_daily_limit(user_plan) and await loop.run_in_executor(None, get_extra_vip_spins, user_id) <= 0: return
        else:
            if user_id != ADMIN_ID and (await loop.run_in_executor(None, db.get_user_spins, user_id) if hasattr(db, "get_user_spins") else 0) < cost: return

        if user_id != ADMIN_ID: 
            if is_vip_flow:
                if await loop.run_in_executor(None, get_vip_tunnel_usage_today, user_id) < get_vip_daily_limit(user_plan): await loop.run_in_executor(None, increment_vip_tunnel_usage_today, user_id)
                else: await loop.run_in_executor(None, deduct_extra_vip_spin, user_id)
            else: await loop.run_in_executor(None, deduct_spins_custom, user_id, cost)

        priority = 0 if user_id == ADMIN_ID else (1 if "PREMIUM" in user_plan.upper() else (2 if "GOLD" in user_plan.upper() else (3 if "VIP" in user_plan.upper() else 4)))
        msg_box = await send_clean_message(query.message.chat_id, context, f"⏳ <b>ĐANG ĐẨY VÀO MÁY CHỦ...</b>\n🚦 Vị trí chờ: {activation_queue.qsize() + 1} (Ưu tiên hạng {priority})")
        
        req_item = {
            "chat_id": msg_box.chat_id, "message_id": msg_box.message_id, "user_id": user_id, "uid": uid,
            "username": username, "cost": cost, "is_vip_flow": is_vip_flow, "user_plan": user_plan,
            "full_name": query.from_user.full_name, "avatar_url": context.user_data.get("last_locket_avatar_url")
        }
        await activation_queue.put((priority, time.time(), user_id, req_item))

async def queue_worker():
    global telegram_app, activation_queue
    while True:
        try:
            item = await activation_queue.get()
            try:
                priority, req_time, user_id, req_item = item
                chat_id, message_id, uid, username, cost, is_vip_flow, user_plan, full_name, avatar_url = req_item.values()
                loop = asyncio.get_running_loop()
                success, msg_result, injected_avatar = False, "", None
                
                try:
                    queue_steps = [
                        ("⏳ <b>[1/3] ĐANG XỬ LÝ DỮ LIỆU</b>", "🟢 KẾT NỐI MÁY CHỦ • [██████████] 100%\n🔄 Phân tích profile Locket..."),
                        ("🔥 <b>[2/3] BẢO MẬT & ĐÓNG BĂNG</b>", "🟢 XÁC THỰC UID • [██████████] 100%\n🔒 Thiết lập tường lửa chống quét..."),
                        ("⚡ <b>[3/3] HOÀN TẤT TIẾN TRÌNH</b>", "🟢 BƠM GOLD • [██████████] 100%\n🚀 Khởi tạo chứng chỉ số..."),
                    ]
                    for title, desc in queue_steps:
                        try: await telegram_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"{title}\n────────────────\n{desc}", parse_mode=ParseMode.HTML)
                        except: pass
                        await asyncio.sleep(0.6)

                    # PHÂN LUỒNG MÁY BƠM: NẾU LÀ GÓI PREMIUM/NO DNS HOẶC ADMIN THÌ CHẠY INJECT_GOLD_NO_DNS
                    if is_vip_flow and ("PREMIUM" in user_plan.upper() or user_id == ADMIN_ID):
                        success, msg_result, injected_avatar = await asyncio.wait_for(locket.inject_gold_no_dns(uid, None, lambda x: None), timeout=15.0)
                    else:
                        success, msg_result, injected_avatar = await asyncio.wait_for(locket.inject_gold(uid, TOKEN_SETS[0], lambda x: None), timeout=15.0)
                except Exception as e:
                    success, msg_result, injected_avatar = False, f"Lỗi Máy Chủ: {str(e)}", None

                final_avatar = injected_avatar or avatar_url
                if hasattr(db, "log_request"): await loop.run_in_executor(None, db.log_request, user_id, uid, "SUCCESS" if success else "FAIL")

                if success:
                    current_time_str = datetime.now(VN_TZ).strftime("%H:%M:%S · %d/%m/%Y")
                    is_no_dns = is_vip_flow and ("PREMIUM" in user_plan.upper() or user_id == ADMIN_ID)
                    tunnel_display = "Đặc Quyền (NO DNS 1 NĂM)" if is_no_dns else ("Ưu Tiên VIP (Cần DNS)" if is_vip_flow else ("Cơ Bản" if cost == 1 else "Ưu Tiên"))
                    cost_text = "1 Lượt Đặc Quyền" if is_vip_flow else f"{cost} Lượt Cấp"

                    if is_no_dns:
                        final_msg = (
                            "✅ <b>BIÊN LAI KÍCH HOẠT LOCKET NO DNS 1 NĂM</b>\n━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 <b>Người dùng:</b> {full_name}\n🔗 <b>Username:</b> <code>{username}</code>\n🔑 <b>UID:</b> <code>{uid}</code>\n"
                            f"⚡ <b>Hầm kích:</b> {tunnel_display}\n🎟️ <b>Đã tiêu hao:</b> -{cost_text}\n⏳ <b>Lúc:</b> {current_time_str}\n━━━━━━━━━━━━━━━━━━━\n"
                            "👑 <b>ĐẶC QUYỀN GÓI 1 NĂM KHÔNG CẦN DNS:</b>\nTài khoản của bạn đã được cấp quyền trực tiếp trên máy chủ. <b>Chỉ cần mở App Locket lên là dùng ngay.</b> Không cần tải VPN hay cài bất kỳ cấu hình DNS nào!\n\n"
                            "<i>(Nếu chưa thấy huy hiệu Gold, hãy vuốt tắt hẳn app Locket chạy ngầm rồi mở lại).</i>"
                        )
                    else:
                        user_dns_link = get_vip_dns() if ("GOLD" in user_plan.upper() or "PREMIUM" in user_plan.upper()) else get_free_dns()
                        final_msg = (
                            "✅ <b>BIÊN LAI KÍCH HOẠT LOCKET</b>\n━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 <b>Người dùng:</b> {full_name}\n🔗 <b>Username:</b> <code>{username}</code>\n🔑 <b>UID:</b> <code>{uid}</code>\n"
                            f"⚡ <b>Hầm kích hoạt:</b> {tunnel_display}\n🎟️ <b>Đã tiêu hao:</b> -{cost_text}\n⏳ <b>Lúc:</b> {current_time_str}\n━━━━━━━━━━━━━━━━━━━\n"
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
                    try: await telegram_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"❌ <b>KÍCH HOẠT THẤT BẠI:</b> {msg_result}\n🔄 <i>Lượt kích hoạt đã được hoàn trả.</i>", parse_mode=ParseMode.HTML)
                    except: pass
            except Exception as e: logger.error(f"Lỗi logic Worker: {e}")
            finally: activation_queue.task_done()
        except: await asyncio.sleep(1)

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
            await app.initialize(); await app.start()
            asyncio.create_task(queue_worker())
            try: await app.bot.set_my_commands([BotCommand("start", "🏠 Bảng Điều Khiển"), BotCommand("help", "📖 HD DNS")])
            except: pass
            render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
            if render_url: await app.bot.set_webhook(url=f"{render_url}/telegram-webhook")

        loop.run_until_complete(startup())
        loop.run_forever()
    except Exception as e: logger.error(f"LỖI BOT: {e}")

def run_bot():
    start_background_bot()
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

if __name__ == "__main__": run_bot()
