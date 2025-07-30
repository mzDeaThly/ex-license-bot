import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response, abort
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView
import omise
from apscheduler.schedulers.background import BackgroundScheduler

# --- [เพิ่ม] LINE SDK Imports ---
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    MulticastRequest
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

# --- 1. Basic Setup ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# --- API Key Setup from Environment Variables ---
omise.api_version = '2019-05-29'
omise.secret_key = os.environ.get('OMISE_SECRET_KEY')
CAPSOLVER_API_KEY = os.environ.get('CAPSOLVER_API_KEY') 

# --- [แก้ไข] LINE Bot Setup for Multiple Admins ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
# อ่านค่า Admin ID ทั้งหมดแล้วแยกด้วยจุลภาค (,)
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
    tier = db.Column(db.String(50), default='Basic')
    max_sessions = db.Column(db.Integer, default=1)
    active_sessions = db.Column(db.Text, default='[]')

    def __repr__(self):
        return f'<License {self.key}>'

# --- 3. Admin Panel Protection ---
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

# --- 4. Admin Panel Creation ---
admin = Admin(app, name='License Manager', template_mode='bootstrap4', index_view=ProtectedAdminIndexView())
admin.add_view(ProtectedModelView(License, db.session))

with app.app_context():
    db.create_all()

# --- [แก้ไข] Helper Function for Sending LINE Message to all Admins ---
def send_line_message(message_text):
    if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_ADMIN_USER_IDS]):
        print("🚨 [LINE] ไม่สามารถส่งข้อความได้: กรุณาตั้งค่า LINE_CHANNEL_ACCESS_TOKEN และ LINE_ADMIN_USER_ID")
        return

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            # ใช้ Multicast เพื่อส่งหา Admin ทุกคนในครั้งเดียว
            line_bot_api.multicast(
                MulticastRequest(
                    to=LINE_ADMIN_USER_IDS,
                    messages=[TextMessage(text=message_text)]
                )
            )
        print(f"✅ [LINE] ส่งข้อความแจ้งเตือนไปยัง Admin {len(LINE_ADMIN_USER_IDS)} คนสำเร็จ")
    except Exception as e:
        print(f"🚨 [LINE] เกิดข้อผิดพลาดในการส่งข้อความ: {e}")

# --- 5. API Endpoints ---
TIER_CONFIG = {
    'basic': {'price_satang': 120000, 'duration_days': 30, 'max_sessions': 1},
    'basic3': {'price_satang': 180000, 'duration_days': 90, 'max_sessions': 3},
    'pro': {'price_satang': 250000, 'duration_days': 30, 'max_sessions': 1},
    'pro3': {'price_satang': 450000, 'duration_days': 90, 'max_sessions': 3}
}

@app.route('/')
def index():
    return "API Server is running."

@app.route('/create-charge-with-tier', methods=['POST'])
def create_charge_with_tier():
    try:
        data = request.get_json()
        email = data.get('email')
        user_key = data.get('licenseKey')
        tier = data.get('tier')

        if not all([email, user_key, tier]) or tier not in TIER_CONFIG:
            return jsonify({'message': 'ข้อมูลไม่ครบถ้วนหรือไม่ถูกต้อง'}), 400

        if License.query.filter_by(key=user_key).first():
            return jsonify({'message': 'License Key นี้มีผู้ใช้งานแล้ว'}), 409

        tier_info = TIER_CONFIG[tier]
        amount = tier_info['price_satang']

        charge = omise.Charge.create(
            amount=amount,
            currency='thb',
            source={'type': 'promptpay'},
            metadata={'email': email, 'requested_key': user_key, 'tier': tier}
        )
        new_license = License(
            key=f"PENDING-{user_key}",
            expires_on=date.today(),
            api_key=charge.id,
            tier='Pending',
            max_sessions=0
        )
        db.session.add(new_license)
        db.session.commit()
        
        return jsonify({
            'chargeId': charge.id,
            'qrCodeUrl': charge.source['scannable_code']['image']['download_uri']
        })
    except Exception as e:
        return jsonify({'message': str(e)}), 500

@app.route('/check-charge-status')
def check_charge_status():
    charge_id = request.args.get('charge_id')
    if not charge_id:
        return jsonify({'status': 'ไม่พบข้อมูล', 'message': 'ไม่พบ charge_id'}), 400
        
    license_entry = License.query.filter_by(api_key=charge_id).first()
    
    if not license_entry:
        return jsonify({'status': 'ไม่พบข้อมูล'})
        
    try:
        charge = omise.Charge.retrieve(charge_id)
        if charge.paid:
            if license_entry.tier == 'Pending':
                metadata = charge['metadata']
                requested_key = metadata.get('requested_key')
                tier = metadata.get('tier')
                tier_info = TIER_CONFIG[tier]

                license_entry.key = requested_key
                license_entry.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
                license_entry.api_key = CAPSOLVER_API_KEY
                license_entry.tier = tier
                license_entry.max_sessions = tier_info['max_sessions']
                db.session.commit()

                message = (
                    f"🎉 มี License ใหม่!\n"
                    f"Key: {license_entry.key}\n"
                    f"Max Sessions: {license_entry.max_sessions}\n"
                    f"หมดอายุ: {license_entry.expires_on.strftime('%Y-%m-%d')}"
                )
                send_line_message(message)
            
            return jsonify({'status': 'successful', 'license_key': license_entry.key})
        else:
            return jsonify({'status': 'pending'})
    except Exception as e:
         return jsonify({'status': 'error', 'message': str(e)})

@app.route('/omise-webhook', methods=['POST'])
def omise_webhook():
    event = request.get_json()
    if event.get('key') == 'charge.complete' and event['data']['status'] == 'successful':
        charge_data = event['data']
        charge_id = charge_data['id']
        metadata = charge_data['metadata']
        requested_key = metadata.get('requested_key')
        tier = metadata.get('tier')

        if not all([requested_key, tier]) or tier not in TIER_CONFIG:
            return jsonify({'status': 'error', 'message': 'ข้อมูล Metadata ไม่ครบ'})

        license_to_update = License.query.filter_by(api_key=charge_id, tier='Pending').first()
        
        if license_to_update:
            tier_info = TIER_CONFIG[tier]
            license_to_update.key = requested_key
            license_to_update.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
            license_to_update.api_key = CAPSOLVER_API_KEY
            license_to_update.tier = tier
            license_to_update.max_sessions = tier_info['max_sessions']
            
            db.session.commit()
            print(f"✅ Webhook: ชำระเงินสำเร็จ! เปิดใช้งาน License '{tier}': {requested_key}")

            message = (
                f"🎉 มี License ใหม่! (Webhook)\n"
                f"Key: {license_to_update.key}\n"
                f"Max Sessions: {license_to_update.max_sessions}\n"
                f"หมดอายุ: {license_to_update.expires_on.strftime('%Y-%m-%d')}"
            )
            send_line_message(message)

    return jsonify({'status': 'ok'})
    
@app.route('/verify-license', methods=['POST'])
def verify_license():
    try:
        data = request.get_json()
        key = data.get('licenseKey')

        if not key:
            return jsonify({'isValid': False, 'message': 'กรุณากรอก License Key'}), 400

        license_entry = License.query.filter_by(key=key).first()

        if not license_entry or license_entry.tier == 'Pending':
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

        while len(active_sessions) > max_sessions_int:
            active_sessions.pop(0)
            message = f"⚠️ Session เกินกำหนด!\nKey: {license_entry.key}\nมีการพยายามเข้าสู่ระบบครั้งใหม่ ทำให้ Session เก่าถูกตัดการเชื่อมต่อ"
            send_line_message(message)
        
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
    try:
        data = request.get_json()
        key = data.get('licenseKey')
        session_token = data.get('sessionToken')

        if not key or not session_token:
            return jsonify({'message': 'ต้องการ License Key และ Session Token'}), 400

        license_entry = License.query.filter_by(key=key).first()

        if not license_entry:
            return jsonify({'message': 'ไม่พบ License'}), 404
        
        try:
            active_sessions = json.loads(license_entry.active_sessions)
        except (json.JSONDecodeError, TypeError):
            active_sessions = []
        
        if session_token not in active_sessions:
            return jsonify({'message': 'Session ไม่ถูกต้อง อาจมีการใช้งานจากเครื่องอื่น'}), 403
        
        return jsonify({'status': 'ok', 'message': 'Session ใช้งานได้'}), 200

    except Exception as e:
        return jsonify({'message': f'เกิดข้อผิดพลาดบนเซิร์ฟเวอร์: {str(e)}'}), 500

# --- 6. LINE Messaging API Webhook ---
@app.route("/line-webhook", methods=['POST'])
def line_webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    print(f"[LINE WEBHOOK] Request body: {body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("🚨 [LINE WEBHOOK] Invalid signature. โปรดตรวจสอบ Channel Secret ของคุณ")
        abort(400)
    except Exception as e:
        print(f"🚨 [LINE WEBHOOK] เกิดข้อผิดพลาดในการ Handle: {e}")
        abort(500)
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.lower().strip()
    user_id = event.source.user_id
    reply_token = event.reply_token

    reply_text = ""

    # ตรวจสอบก่อนว่าผู้ส่งเป็น Admin หรือไม่
    if user_id not in LINE_ADMIN_USER_IDS:
        reply_text = "⛔️ บัญชีนี้ไม่ได้รับสิทธิ์ใช้งานบอท\nกรุณาติดต่อผู้ดูแลระบบ"

    else:
        # เป็นแอดมิน → จัดการคำสั่ง
        if text.startswith('ban '):
            parts = text.split(' ')
            if len(parts) == 2:
                key_to_ban = parts[1]
                license_to_ban = License.query.filter_by(key=key_to_ban).first()
                if license_to_ban:
                    license_to_ban.expires_on = date.today() - timedelta(days=1)
                    license_to_ban.active_sessions = '[]'
                    db.session.commit()
                    reply_text = f"🚫 แบน License '{key_to_ban}' เรียบร้อยแล้ว"
                else:
                    reply_text = f"ไม่พบ License Key '{key_to_ban}'"
            else:
                reply_text = "❗️รูปแบบคำสั่งไม่ถูกต้อง\nตัวอย่าง: ban KEY-123"

        elif text == 'check':
            active_licenses = License.query.filter(License.expires_on >= date.today()).all()
            count = len(active_licenses)
            
            if count == 0:
                reply_text = "ℹ️ ไม่มี License Key ที่ใช้งานได้ในขณะนี้"
            else:
                details = []
                for lic in active_licenses:
                    details.append(f"- {lic.key} ({lic.max_sessions} sessions)")
                
                details_text = "\n".join(details)
                reply_text = f"📊 License ที่ใช้งานได้: {count} รายการ\n\n{details_text}"

        elif text.startswith('notify '):
            parts = text.split(' ')
            if len(parts) == 2:
                key_to_notify = parts[1]
                license_to_notify = License.query.filter_by(key=key_to_notify).first()

                if license_to_notify:
                    status_message = (
                        f"🔔 แจ้งเตือนสถานะ License\n"
                        f"Key: {license_to_notify.key}\n"
                        f"Max Sessions: {license_to_notify.max_sessions}\n"
                        f"หมดอายุ: {license_to_notify.expires_on.strftime('%Y-%m-%d')}"
                    )
                    send_line_message(status_message)
                    reply_text = f"✅ ส่งการแจ้งเตือนสำหรับ '{key_to_notify}' เรียบร้อย"
                else:
                    reply_text = f"ไม่พบ License Key '{key_to_notify}'"
            else:
                reply_text = "❗️รูปแบบคำสั่งไม่ถูกต้อง\nตัวอย่าง: notify KEY-123"

        else:
            reply_text = (
                "คำสั่งที่ใช้ได้:\n"
                "ban <key>\n"
                "check\n"
                "notify <key>"
                "<key> << ให้พิมพ์ user license key ได้เลย ไม่ต้องมี <>"
            )

    # ส่งข้อความตอบกลับ (ใช้ reply_token ได้แค่ครั้งเดียว)
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print(f"🚨 [LINE BOT] ตอบกลับไม่ได้: {e}")


# --- 7. Scheduled Job for Clearing Sessions ---
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
