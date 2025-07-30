import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView
import omise # กลับมาใช้ Omise

# --- 1. Basic Setup ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# --- Omise API Key Setup from Environment Variables ---
omise.api_version = '2019-05-29'
omise.secret_key = os.environ.get('OMISE_SECRET_KEY')

# --- Database Setup (เหมือนเดิม) ---
DISK_STORAGE_PATH = '/var/data'
DATABASE_FILE = 'licenses.db'
DATABASE_PATH = os.path.join(DISK_STORAGE_PATH, DATABASE_FILE)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + DATABASE_PATH
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key-for-local-dev')

db = SQLAlchemy(app)

# --- 2. Database Model (เหมือนเดิม) ---
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

# --- 3 & 4. Admin Panel (เหมือนเดิม) ---
# ... (ส่วนของ Admin Panel ไม่มีการเปลี่ยนแปลง) ...
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

# --- 5. API Endpoints (กลับมาเป็นเวอร์ชัน Omise) ---
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
        
        # เก็บ Charge ID ไว้เพื่อใช้อ้างอิง
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
        return jsonify({'status': 'not_found', 'message': 'charge_id is required'}), 400

    license_entry = License.query.filter_by(api_key=charge_id).first()
    
    if not license_entry:
        return jsonify({'status': 'not_found'})
        
    # ตรวจสอบสถานะโดยตรงจาก Omise API เพื่อความแม่นยำ
    try:
        charge = omise.Charge.retrieve(charge_id)
        if charge.paid:
             # ถ้าจ่ายแล้วแต่ใน DB ยังเป็น Pending ให้ทำการอัปเดต
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
            return jsonify({'status': 'error', 'message': 'Missing metadata'})

        license_to_update = License.query.filter_by(api_key=charge_id, tier='Pending').first()
        
        if license_to_update:
            tier_info = TIER_CONFIG[tier]
            license_to_update.key = requested_key
            license_to_update.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
            license_to_update.api_key = "CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1"
            license_to_update.tier = tier
            license_to_update.max_sessions = tier_info['max_sessions']
            
            db.session.commit()
            print(f"✅ Webhook: Payment successful! Activated '{tier}' license: {requested_key}")

    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
