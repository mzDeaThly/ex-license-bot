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

# --- 1. ตั้งค่าพื้นฐาน ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app)

# --- ตั้งค่า Omise API Key จาก Environment Variables ---
omise.api_version = '2019-05-29'
omise.secret_key = os.environ.get('OMISE_SECRET_KEY')
YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN') 

# --- ตั้งค่าฐานข้อมูลสำหรับ Render Persistent Disk ---
DISK_STORAGE_PATH = '/var/data'
DATABASE_FILE = 'licenses.db'
DATABASE_PATH = os.path.join(DISK_STORAGE_PATH, DATABASE_FILE)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + DATABASE_PATH
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key-for-local-dev')

db = SQLAlchemy(app)

# --- 2. สร้าง Model สำหรับฐานข้อมูล ---
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

# --- 3. ส่วนของโค้ดสำหรับป้องกัน Admin Panel ---
def check_auth(username, password):
    """ตรวจสอบ Username และ Password กับค่าที่ตั้งไว้ใน Environment Variables"""
    admin_user = os.environ.get('ADMIN_USERNAME')
    admin_pass = os.environ.get('ADMIN_PASSWORD')
    return username == admin_user and password == admin_pass

def authenticate():
    """ส่ง Response 401 Unauthorized เพื่อให้เบราว์เซอร์แสดงหน้าต่าง Login"""
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

# --- 4. สร้างแผงควบคุมสำหรับ Admin โดยใช้ View ที่ป้องกันแล้ว ---
admin = Admin(app, name='License Manager', template_mode='bootstrap4', index_view=ProtectedAdminIndexView())
admin.add_view(ProtectedModelView(License, db.session))

# --- สร้าง Table ในฐานข้อมูล (จะทำงานเมื่อแอปเริ่ม) ---
with app.app_context():
    db.create_all()

# --- 5. API Endpoints ---
TIER_CONFIG = {
    'basic': {'price_satang': 120000, 'duration_days': 30, 'max_sessions': 1},
    'basic3': {'price_satang': 180000, 'duration_days': 30, 'max_sessions': 3},
    'pro': {'price_satang': 250000, 'duration_days': 30, 'max_sessions': 1},
    'pro3': {'price_satang': 450000, 'duration_days': 30, 'max_sessions': 3}
}
SESSION_TIMEOUT_MINUTES = 10

def get_active_sessions(license_obj):
    """ดึงและทำความสะอาด active sessions ที่หมดอายุแล้ว"""
    sessions = json.loads(license_obj.active_sessions)
    fresh_sessions = []
    now = datetime.utcnow()
    
    for session in sessions:
        last_seen = datetime.fromisoformat(session['last_seen'])
        if now - last_seen < timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            fresh_sessions.append(session)
            
    return fresh_sessions

@app.route('/')
def index():
    # Endpoint นี้อาจจะไม่จำเป็นถ้าคุณไม่ได้โฮสต์หน้าเว็บหลักที่นี่
    # แต่การมีไว้ก็ไม่เสียหาย
    return "License Server is running."

@app.route('/create-charge-with-tier', methods=['POST'])
def create_charge_with_tier():
    try:
        data = request.get_json()
        email = data.get('email')
        user_key = data.get('licenseKey')
        tier = data.get('tier')

        if not all([email, user_key, tier]) or tier not in TIER_CONFIG:
            return jsonify({'message': 'Missing or invalid information'}), 400

        if License.query.filter_by(key=user_key).first():
            return jsonify({'message': 'This License Key is already in use.'}), 409

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
        import traceback
        traceback.print_exc()
        return jsonify({'message': str(e)}), 500

@app.route('/check-charge-status')
def check_charge_status():
    charge_id = request.args.get('charge_id')
    license_entry = License.query.filter_by(api_key=charge_id).first()
    
    if not license_entry:
        return jsonify({'status': 'not_found'})
        
    if license_entry.tier == 'Pending':
        return jsonify({'status': 'pending'})
    else:
        return jsonify({'status': 'successful', 'license_key': license_entry.key})

@app.route('/omise-webhook', methods=['POST'])
def omise_webhook():
    event = request.get_json()
    # For production, it's better to verify the webhook signature
    # but for now, we'll trust the event based on the secret URL.
    if event.get('key') == 'charge.complete':
        charge_data = event['data']
        if charge_data.get('status') == 'successful':
            charge_id = charge_data['id']
            metadata = charge_data['metadata']
            requested_key = metadata.get('requested_key')
            tier = metadata.get('tier')

            if not all([requested_key, tier]) or tier not in TIER_CONFIG:
                return jsonify({'status': 'error', 'message': 'Missing metadata'})

            license_to_update = License.query.filter_by(api_key=charge_id, tier='Pending').first()
            
            if license_to_update:
                tier_info = TIER_CONFIG[tier]
                license_to_update.key = requested_key
                license_to_update.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
                license_to_update.api_key = "YOUR_DEFAULT_CAPSOLVER_API_KEY"
                license_to_update.tier = tier.capitalize()
                license_to_update.max_sessions = tier_info['max_sessions']
                
                db.session.commit()
                print(f"✅ Webhook: Payment successful! Activated '{tier}' license: {requested_key}")

    return jsonify({'status': 'ok'})

@app.route('/verify-license', methods=['POST'])
def verify_license_main(): # Renamed to avoid conflict with the original verify_license
    data = request.get_json()
    license_key = data.get('licenseKey')

    if not license_key:
        return jsonify({'isValid': False, 'message': 'กรุณาส่ง License Key'}), 400

    license_obj = License.query.filter_by(key=license_key).first()

    if not license_obj:
        return jsonify({'isValid': False, 'message': 'License Key ไม่ถูกต้อง'}), 401

    if license_obj.expires_on < date.today():
        return jsonify({'isValid': False, 'message': f'License Key หมดอายุแล้วเมื่อวันที่ {license_obj.expires_on}'}), 403

    active_sessions = get_active_sessions(license_obj)
    
    if len(active_sessions) >= license_obj.max_sessions:
        print(f"[SESSION_FULL] Key: {license_key} ใช้งานครบจำนวนเครื่องแล้ว ({len(active_sessions)}/{license_obj.max_sessions})")
        return jsonify({'isValid': False, 'message': 'License Key นี้ถูกใช้งานครบจำนวนเครื่องแล้ว'}), 429

    new_token = str(uuid.uuid4())
    new_session = {
        "token": new_token,
        "last_seen": datetime.utcnow().isoformat()
    }
    active_sessions.append(new_session)
    
    license_obj.active_sessions = json.dumps(active_sessions)
    db.session.commit()
    
    print(f"[LOGIN_SUCCESS] สร้าง Session ใหม่สำหรับ Key: {license_key}")
    return jsonify({
        'isValid': True,
        'message': 'License Key ถูกต้องและเปิดใช้งานแล้ว',
        'expiresOn': license_obj.expires_on.strftime('%Y-%m-%d'),
        'apiKey': license_obj.api_key,
        'sessionToken': new_token
    })

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json()
    license_key = data.get('licenseKey')
    session_token = data.get('sessionToken')

    if not license_key or not session_token:
        return jsonify({'status': 'invalid_request'}), 400

    license_obj = License.query.filter_by(key=license_key).first()
    if not license_obj:
        return jsonify({'status': 'invalid_session'}), 403

    active_sessions = json.loads(license_obj.active_sessions)
    session_found = False
    
    for session in active_sessions:
        if session['token'] == session_token:
            session['last_seen'] = datetime.utcnow().isoformat()
            session_found = True
            break
            
    if session_found:
        license_obj.active_sessions = json.dumps(active_sessions)
        db.session.commit()
        return jsonify({'status': 'ok'}), 200
    else:
        return jsonify({'status': 'invalid_session'}), 403

# --- 6. Run Application ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
