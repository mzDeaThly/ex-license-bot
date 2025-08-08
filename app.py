import os
import json
import uuid
from datetime import datetime, date, timedelta
import qrcode
import io
import base64
from flask import Flask, request, jsonify, Response, abort
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView
from apscheduler.schedulers.background import BackgroundScheduler
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    PushMessageRequest, TextMessage, MulticastRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


# --- [แก้ไข] ฟังก์ชันสำหรับสร้าง PromptPay Payload ให้รองรับทุกรูปแบบ ---
def generate_promptpay_payload(account_id, amount=None):
    """Generates a standard-compliant PromptPay payload string for various ID types."""

    def crc16_ccitt_false(data: bytes):
        crc = 0xFFFF
        poly = 0x1021
        for b in data:
            crc ^= (b << 8)
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ poly
                else:
                    crc <<= 1
        return crc & 0xFFFF

    target = account_id.replace('-', '').strip()

    # Field 00, 01
    payload = "000201"
    payload += "010212" if amount else "010211"

    # Field 29: Merchant Account Information
    merchant_guid = "0016A000000677010111"

    if len(target) == 10 and target.startswith('0'): # Phone Number
        biller_id_value = '0066' + target[1:]
        biller_id = f"0113{biller_id_value}"
    elif len(target) == 13: # National ID / Tax ID
        biller_id = f"0113{target}"
    elif len(target) == 15: # e-Wallet ID
        biller_id = f"0215{target}"
    else:
        # Return empty string if account_id format is invalid
        return ""

    merchant_info = f"{merchant_guid}{biller_id}"
    payload += f"29{len(merchant_info):02}{merchant_info}"

    # Field 58, 53
    payload += "5802TH"
    payload += "5303764"

    # Field 54
    if amount:
        amount_str = f"{amount:.2f}"
        payload += f"54{len(amount_str):02}{amount_str}"

    # Field 63: CRC
    payload_for_crc = payload + "6304"
    checksum = crc16_ccitt_false(payload_for_crc.encode('utf-8'))

    return f"{payload_for_crc}{checksum:04X}"


# --- 1. Basic Setup ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# --- API Key Setup ---
CAPSOLVER_API_KEY = os.environ.get('CAPSOLVER_API_KEY')
PROMPTPAY_ID = os.environ.get('PROMPTPAY_ID')

# --- LINE Bot Setup ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_ADMIN_USER_IDS_STR = os.environ.get('LINE_ADMIN_USER_ID', '')
LINE_ADMIN_USER_IDS = [uid.strip() for uid in LINE_ADMIN_USER_IDS_STR.split(',') if uid.strip()]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Database Setup ---
DISK_STORAGE_PATH = '/var/data'
DATABASE_FILE = 'licenses.db'
DATABASE_PATH = os.path.join(DISK_STORAGE_PATH, DATABASE_FILE)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + DATABASE_PATH
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key-for-local-dev')

db = SQLAlchemy(app)

# --- 2. Database Model ---
class License(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    expires_on = db.Column(db.Date, nullable=False)
    api_key = db.Column(db.String(120), nullable=False)
    tier = db.Column(db.String(50), nullable=False)
    max_sessions = db.Column(db.Integer, default=1)
    active_sessions = db.Column(db.Text, default='[]')
    status = db.Column(db.String(20), default='pending', nullable=False)

    def __repr__(self):
        if self.id:
            return f'<License id={self.id} key="{self.key}">'
        else:
            return '<License (new)>'

# --- 3. & 4. Admin Panel ---
def check_auth(username, password):
    admin_user = os.environ.get('ADMIN_USERNAME')
    admin_pass = os.environ.get('ADMIN_PASSWORD')
    return username == admin_user and password == admin_pass

def authenticate():
    return Response(
    'Could not verify your access level for that URL.\n'
    'You have to login with proper credentials', 401,
    {'WWW-Authenticate': 'Basic realm="Login Required"'})

class ProtectedAdminIndexView(AdminIndexView):
    def is_accessible(self):
        auth = request.authorization
        return auth and check_auth(auth.username, auth.password)
    def inaccessible_callback(self, name, **kwargs):
        return authenticate()

class ProtectedModelView(ModelView):
    def is_accessible(self):
        auth = request.authorization
        return auth and check_auth(auth.username, auth.password)
    def inaccessible_callback(self, name, **kwargs):
        return authenticate()

admin = Admin(app, name='License Manager', template_mode='bootstrap4', index_view=ProtectedAdminIndexView())
admin.add_view(ProtectedModelView(License, db.session))

with app.app_context():
    db.create_all()

# --- Helper Function for LINE Message ---
LINE_GROUP_ID = os.environ.get('LINE_GROUP_ID')
def send_line_message(message_text):
    if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_GROUP_ID]):
        print("🚨 [LINE] Missing LINE config for Group")
        return
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(to=LINE_GROUP_ID, messages=[TextMessage(text=message_text)])
            )
    except Exception as e:
        print(f"🚨 [LINE] Error sending group message: {e}")

# --- 5. API Endpoints ---
TIER_CONFIG = {
    'basic': {'price_satang': 30000, 'duration_days': 30, 'max_sessions': 1},
    'basic3': {'price_satang': 65000, 'duration_days': 30, 'max_sessions': 3},
    'pro': {'price_satang': 75000, 'duration_days': 90, 'max_sessions': 1},
    'pro3': {'price_satang': 150000, 'duration_days': 90, 'max_sessions': 3}
}

@app.route('/')
def index():
    return "API Server is running."

@app.route('/create-charge-with-tier', methods=['POST'])
def create_charge_with_tier():
    if not PROMPTPAY_ID:
        return jsonify({'message': 'ผู้ดูแลระบบยังไม่ได้ตั้งค่า PromptPay ID'}), 500
    try:
        data = request.get_json()
        email, user_key, tier = data.get('email'), data.get('licenseKey'), data.get('tier')

        if not all([email, user_key, tier]) or tier not in TIER_CONFIG:
            return jsonify({'message': 'ข้อมูลไม่ครบถ้วนหรือไม่ถูกต้อง'}), 400

        if License.query.filter_by(key=user_key).first():
                return jsonify({'message': 'License Key นี้มีอยู่ในระบบแล้ว'}), 409

        tier_info = TIER_CONFIG[tier]
        amount_thb = tier_info['price_satang'] / 100.0

        payload = generate_promptpay_payload(PROMPTPAY_ID, amount=amount_thb)
        img = qrcode.make(payload)
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        qr_code_data_uri = f"data:image/png;base64,{img_str}"

        pending_id = uuid.uuid4().hex
        new_license = License(
            key=user_key,
            expires_on=date.today(),
            api_key=pending_id,
            tier=tier,
            status='pending',
            max_sessions=0
        )
        db.session.add(new_license)
        db.session.commit()

        message_to_admin = (
            f"⏳ มีรายการรอชำระเงินใหม่\n"
            f"Key: {user_key}\n"
            f"แพ็กเกจ: {tier}\n"
            f"ยอดเงิน: {amount_thb} บาท\n\n"
            f"ใช้คำสั่ง `activate {user_key}` เพื่อเปิดใช้งาน"
        )
        send_line_message(message_to_admin)

        return jsonify({'chargeId': pending_id, 'qrCodeUrl': qr_code_data_uri})
    except Exception as e:
        print(f"Error in create_charge_with_tier: {e}")
        return jsonify({'message': str(e)}), 500

@app.route('/check-charge-status')
def check_charge_status():
    charge_id = request.args.get('charge_id')
    license_entry = License.query.filter_by(api_key=charge_id).first()
    if not license_entry: return jsonify({'status': 'error', 'message': 'ไม่พบรายการ'})
    if license_entry.status == 'active': return jsonify({'status': 'successful', 'license_key': license_entry.key})
    else: return jsonify({'status': 'pending', 'message': 'รอการยืนยัน'})

@app.route('/verify-license', methods=['POST'])
def verify_license():
    try:
        data = request.get_json()
        key = data.get('licenseKey')
        if not key: return jsonify({'isValid': False, 'message': 'กรุณากรอก License Key'}), 400

        license_entry = License.query.filter_by(key=key).first()

        if not license_entry or license_entry.status == 'pending':
            return jsonify({'isValid': False, 'message': 'ไม่พบ License Key หรือยังไม่เปิดใช้งาน'}), 404
        if license_entry.expires_on < date.today():
            return jsonify({'isValid': False, 'message': 'License นี้หมดอายุแล้ว'}), 403

        session_token = uuid.uuid4().hex
        try:
            active_sessions = json.loads(license_entry.active_sessions)
        except (json.JSONDecodeError, TypeError):
            active_sessions = []
        active_sessions.append(session_token)
        max_sessions_int = int(license_entry.max_sessions or 1)
        if len(active_sessions) > max_sessions_int:
            active_sessions.pop(0)
            send_line_message(f"⚠️ Session เกินกำหนด!\nKey: {license_entry.key}\nมีการเข้าสู่ระบบใหม่ ทำให้ Session เก่าถูกตัด")

        license_entry.active_sessions = json.dumps(active_sessions)
        db.session.commit()

        return jsonify({
            'isValid': True,
            'message': 'License ใช้งานได้',
            'apiKey': license_entry.api_key,
            'sessionToken': session_token,
            'expiresOn': license_entry.expires_on.strftime('%Y-%m-%d'),
            'activeSessionsCount': len(active_sessions),
            'maxSessions': license_entry.max_sessions
        })
    except Exception as e:
        return jsonify({'isValid': False, 'message': f'เกิดข้อผิดพลาดบนเซิร์ฟเวอร์: {str(e)}'}), 500

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    return jsonify({'status': 'ok'}), 200

@app.route("/line-webhook", methods=['POST'])
def line_webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    # --- ตรวจสอบว่าข้อความมาจากกลุ่มที่ถูกต้องหรือไม่ ---
    source_group_id = ""
    if event.source.type == 'group':
        source_group_id = event.source.group_id

    # ถ้าไม่ได้มาจากกลุ่มที่กำหนดไว้ ให้ออกจากฟังก์ชันทันที
    if source_group_id != LINE_GROUP_ID:
        return 'OK'

    # --- ถ้ามาจากกลุ่มที่ถูกต้อง ให้ประมวลผลคำสั่งต่อ ---
    text = event.message.text.strip()
    user_id = event.source.user_id # ID ของผู้ที่พิมพ์คำสั่ง
    reply_token = event.reply_token

    parts = text.split(' ')
    command = parts[0].lower()

    admin_commands = ['activate', 'ban', 'check', 'notify']
    reply_text = ""

    # ตรวจสอบสิทธิ์แอดมินจาก User ID เหมือนเดิม
    if user_id not in LINE_ADMIN_USER_IDS:
        # ไม่ต้องตอบกลับถ้าคนพิมพ์ไม่ใช่แอดมิน
        pass

    # ถ้าเป็นแอดมินและใช้คำสั่ง
    elif command in admin_commands:
        if command == 'activate' and len(parts) == 2:
            key_to_activate = parts[1]
            license_to_activate = License.query.filter_by(key=key_to_activate, status='pending').first()
            if license_to_activate:
                tier = license_to_activate.tier
                tier_info = TIER_CONFIG[tier]
                license_to_activate.status = 'active'
                license_to_activate.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
                license_to_activate.max_sessions = tier_info['max_sessions']
                license_to_activate.api_key = CAPSOLVER_API_KEY
                db.session.commit()

                activation_message = f"✅ เปิดใช้งาน License '{key_to_activate}' สำเร็จ!\n- Tier: {tier}"
                reply_text = activation_message
                send_line_message(activation_message)
            else:
                reply_text = f"ไม่พบ License Key '{key_to_activate}' ที่รอการเปิดใช้งาน"

        elif command == 'ban' and len(parts) == 2:
            key_to_ban = parts[1]
            license_to_ban = License.query.filter_by(key=key_to_ban).first()
            if license_to_ban:
                license_to_ban.status = 'banned'
                license_to_ban.expires_on = date.today() - timedelta(days=1)
                db.session.commit()
                reply_text = f"🚫 แบน License '{key_to_ban}' เรียบร้อยแล้ว"
            else:
                reply_text = f"ไม่พบ License Key '{key_to_ban}'"

        elif command == 'notify' and len(parts) == 2:
            key_to_notify = parts[1]
            license_to_notify = License.query.filter_by(key=key_to_notify).first()
            if license_to_notify:
                status_message = (
                    f"🔔 สถานะ License\n"
                    f"Key: {license_to_notify.key}\n"
                    f"Tier: {license_to_notify.tier}\n"
                    f"Status: {license_to_notify.status.capitalize()}\n"
                    f"Max Sessions: {license_to_notify.max_sessions}\n"
                    f"หมดอายุ: {license_to_notify.expires_on.strftime('%Y-%m-%d')}"
                )
                send_line_message(status_message)
                reply_text = f"✅ ส่งข้อมูลของ '{key_to_notify}' ให้แอดมินทุกคนแล้ว"
            else:
                reply_text = f"ไม่พบ License Key '{key_to_notify}'"

        elif command == 'check':
            all_licenses = License.query.order_by(License.id).all()
            if not all_licenses:
                reply_text = "ℹ️ ไม่มีข้อมูล License ในระบบ"
            else:
                details = [f"📋 License ทั้งหมด: {len(all_licenses)} รายการ\n"]
                for lic in all_licenses:
                    exp_date = lic.expires_on.strftime('%Y-%m-%d')
                    status_text = lic.status.capitalize()
                    lic_info = (
                        f"Key: `{lic.key}`\n"
                        f"สถานะ: **{status_text}**\n"
                        f"หมดอายุ: {exp_date}\n"
                        f"Max Sessions: {lic.max_sessions}"
                    )
                    details.append(lic_info)
                reply_text = "\n\n".join(details)
                if len(reply_text) > 4800:
                    reply_text = f"พบข้อมูล {len(all_licenses)} รายการ แต่ข้อความยาวเกินไปที่จะแสดงผลทั้งหมด"

        else:
            reply_text = "รูปแบบคำสั่งแอดมินไม่ถูกต้อง"


    if reply_text:
        try:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=reply_text)])
                )
        except Exception as e:
            print(f"🚨 [LINE BOT] ไม่สามารถตอบกลับได้: {e}")

# --- 7. Scheduled Job ---
def clear_all_sessions():
    with app.app_context():
        try:
            num_updated = License.query.update({License.active_sessions: '[]'})
            db.session.commit()
            print(f"✅ [CRON JOB] ล้างข้อมูล Session ทั้งหมด {num_updated} รายการสำเร็จ")
        except Exception as e:
            db.session.rollback()
            print(f"🚨 [CRON JOB] เกิดข้อผิดพลาดในการล้างข้อมูล Session: {str(e)}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(clear_all_sessions, 'interval', minutes=15)
scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
