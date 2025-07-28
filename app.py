import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response, redirect
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

# --- 4. สร้างแผงควบคุมสำหรับ Admin ---
admin = Admin(app, name='License Manager', template_mode='bootstrap4', index_view=ProtectedAdminIndexView())
admin.add_view(ProtectedModelView(License, db.session))

# --- สร้าง Table ในฐานข้อมูล ---
with app.app_context():
    db.create_all()

# --- 5. API Endpoints ---
TIER_CONFIG = {
    'basic': {'price_satang': 120000, 'duration_days': 30, 'max_sessions': 1},
    'basic3': {'price_satang': 180000, 'duration_days': 90, 'max_sessions': 3},
    'pro': {'price_satang': 250000, 'duration_days': 30, 'max_sessions': 1},
    'pro3': {'price_satang': 450000, 'duration_days': 90, 'max_sessions': 3}
}

@app.route('/create-charge-with-tier', methods=['POST'])
def create_charge_with_tier():
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

    try:
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
    license_entry = License.query.filter_by(api_key=charge_id).first()
    
    if not license_entry:
        return jsonify({'status': 'not_found'})
        
    if license_entry.tier == 'Pending':
        return jsonify({'status': 'pending'})
    else:
        return jsonify({'status': 'successful', 'license_key': license_entry.key})

@app.route('/omise-webhook', methods=['POST'])
def omise_webhook():
    event = None
    payload = request.data
    sig_header = request.headers['STRIPE_SIGNATURE'] # Corrected from Stripe to Omise later if needed, but Omise uses a similar concept
    endpoint_secret = os.environ.get('OMISE_WEBHOOK_SECRET')

    try:
        # Note: Omise webhook verification is simpler than Stripe's.
        # This is a simplified check. For production, refer to Omise's library for verification.
        event = json.loads(payload)
    except Exception as e:
        return jsonify({'error': 'Invalid payload'}), 400

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
                license_to_update.tier = tier
                license_to_update.max_sessions = tier_info['max_sessions']
                
                db.session.commit()
                print(f"✅ Webhook: Payment successful! Activated '{tier}' license: {requested_key}")

    return jsonify({'status': 'ok'})

# ... (verify-license and heartbeat endpoints) ...

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
