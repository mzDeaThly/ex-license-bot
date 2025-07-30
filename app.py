import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView
import omise

# --- 1. Basic Setup ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# --- Omise API Key Setup ---
omise.api_version = '2019-05-29'
omise.secret_key = os.environ.get('OMISE_SECRET_KEY')

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
                license_entry.api_key = "CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1"
                license_entry.tier = tier
                license_entry.max_sessions = tier_info['max_sessions']
                db.session.commit()
            
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
            license_to_update.api_key = "CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1"
            license_to_update.tier = tier
            license_to_update.max_sessions = tier_info['max_sessions']
            
            db.session.commit()
            print(f"✅ Webhook: ชำระเงินสำเร็จ! เปิดใช้งาน License '{tier}': {requested_key}")

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
            
        print(f"[DEBUG] ตรวจสอบ License: {key}")
        print(f"[DEBUG] ค่า max_sessions ที่อ่านจาก DB: {license_entry.max_sessions} (ประเภท: {type(license_entry.max_sessions)})")

        session_token = uuid.uuid4().hex
        
        try:
            active_sessions = json.loads(license_entry.active_sessions)
        except (json.JSONDecodeError, TypeError):
            active_sessions = []

        active_sessions.append(session_token)
        
        print(f"[DEBUG] จำนวน session ปัจจุบัน (ก่อนลบ): {len(active_sessions)}")
        
        max_sessions_int = int(license_entry.max_sessions or 1)

        while len(active_sessions) > max_sessions_int:
            removed_session = active_sessions.pop(0)
            print(f"[DEBUG] Session เกินกำหนด! ลบ session เก่า: {removed_session}")
        
        license_entry.active_sessions = json.dumps(active_sessions)
        db.session.commit()
        
        print(f"[DEBUG] สร้าง Session ใหม่: {session_token}")
        print(f"[DEBUG] รายการ Session ที่ใช้งานได้: {license_entry.active_sessions}")
        
        return jsonify({
            'isValid': True,
            'message': 'License ใช้งานได้',
            'apiKey': license_entry.api_key,
            'sessionToken': session_token,
            'expiresOn': license_entry.expires_on.strftime('%Y-%m-%d')
        })

    except Exception as e:
        print(f"[ERROR] เกิดข้อผิดพลาดใน /verify-license: {str(e)}")
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
            print(f"🚨 [HEARTBEAT] Session ไม่ถูกต้องสำหรับ License '{key}'. อาจมีการใช้งานจากเครื่องอื่น")
            return jsonify({'message': 'Session ไม่ถูกต้อง อาจมีการใช้งานจากเครื่องอื่น'}), 403
        
        print(f"❤️ [HEARTBEAT] สำเร็จสำหรับ License '{key}'")
        
        return jsonify({'status': 'ok', 'message': 'Session ใช้งานได้'}), 200

    except Exception as e:
        print(f"[ERROR] เกิดข้อผิดพลาดใน /heartbeat: {str(e)}")
        return jsonify({'message': f'เกิดข้อผิดพลาดบนเซิร์ฟเวอร์: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
