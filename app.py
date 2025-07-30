import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response, redirect, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView

import stripe

# --- 1. ตั้งค่าพื้นฐาน ---
app = Flask(__name__, static_folder='public', static_url_path='')
CORS(app)

# --- Stripe API Settings ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN')

# Ensure YOUR_DOMAIN is set for success/cancel URLs
if not YOUR_DOMAIN:
    print("WARNING: YOUR_DOMAIN environment variable is not set. Set it to your Render app's URL for Stripe redirects to work correctly.")
    YOUR_DOMAIN = "http://localhost:5000"

# --- NEW: Add Content Security Policy Header ---
@app.after_request
def add_security_headers(response):
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://js.stripe.com; connect-src 'self' https://api.stripe.com; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com;"
    return response

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
    
    # New fields for Stripe payment tracking
    stripe_session_id = db.Column(db.String(200), unique=True, nullable=True)
    stripe_payment_status = db.Column(db.String(50), default='PENDING')

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
    'basic': {'price_satang': 120000, 'duration_days': 30, 'max_sessions': 1, 'name': 'Basic (1 เดือน 1 Session)'},
    'basic3': {'price_satang': 180000, 'duration_days': 30, 'max_sessions': 3, 'name': 'Basic (1 เดือน 3 Session)'},
    'pro': {'price_satang': 250000, 'duration_days': 30, 'max_sessions': 1, 'name': 'Pro (1 เดือน 1 Session)'},
    'pro3': {'price_satang': 450000, 'duration_days': 30, 'max_sessions': 3, 'name': 'Pro (1 เดือน 3 Session)'}
}
SESSION_TIMEOUT_MINUTES = 10

def get_active_sessions(license_obj):
    """ดึงและทำความสะอาด active sessions ที่หมดอายุแล้ว"""
    if not license_obj.active_sessions: # Handle empty or None string
        return []
    try:
        sessions = json.loads(license_obj.active_sessions)
        if not isinstance(sessions, list): # Ensure it's a list
            print(f"WARNING: active_sessions for {license_obj.key} is not a list. Resetting.")
            return []
    except json.JSONDecodeError:
        print(f"WARNING: Invalid JSON in active_sessions for {license_obj.key}. Resetting.")
        return []
        
    fresh_sessions = []
    now = datetime.utcnow()
    
    for session in sessions:
        if not isinstance(session, dict) or 'last_seen' not in session or 'token' not in session:
            print(f"WARNING: Malformed session entry for {license_obj.key}. Skipping.")
            continue # Skip malformed session
        try:
            last_seen = datetime.fromisoformat(session['last_seen'])
            if now - last_seen < timedelta(minutes=SESSION_TIMEOUT_MINUTES):
                fresh_sessions.append(session)
        except ValueError:
            print(f"WARNING: Invalid 'last_seen' datetime format for {license_obj.key}. Skipping session.")
            continue # Skip session with invalid datetime format
            
    return fresh_sessions

@app.route('/')
def index():
    return "License Server is running."

# --- Stripe Checkout Session creation endpoint ---
@app.route('/create-stripe-checkout-session', methods=['POST'])
def create_stripe_checkout_session():
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
        amount_satang = tier_info['price_satang']
        tier_name = tier_info['name']

        new_license = License(
            key=f"PENDING-{user_key}",
            expires_on=date.today(), # Temp date
            api_key="PENDING", # Temp API key
            tier=tier.capitalize(),
            max_sessions=0,
            stripe_payment_status='PENDING'
        )
        db.session.add(new_license)
        db.session.commit()

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['promptpay', 'card'],
            line_items=[
                {
                    'price_data': {
                        'currency': 'thb',
                        'unit_amount': amount_satang,
                        'product_data': {
                            'name': f'License Key: {tier_name}',
                            'description': f'License for {tier_name}',
                        },
                    },
                    'quantity': 1,
                }
            ],
            mode='payment',
            success_url=f'{YOUR_DOMAIN}/check-stripe-payment-status?session_id={{CHECKOUT_SESSION_ID}}&license_id={new_license.id}',
            cancel_url=f'{YOUR_DOMAIN}/register.html?status=cancelled',
            metadata={
                'license_id': new_license.id,
                'requested_key': user_key,
                'tier': tier,
                'email': email
            }
        )

        new_license.stripe_session_id = checkout_session.id
        db.session.commit()
        
        return jsonify({'checkoutUrl': checkout_session.url})
    except stripe.error.StripeError as e:
        print(f"Stripe API error: {e}")
        return jsonify({'message': str(e)}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'message': str(e)}), 500

# --- Endpoint for checking Stripe payment status after redirect ---
@app.route('/check-stripe-payment-status')
def check_stripe_payment_status():
    session_id = request.args.get('session_id')
    license_id = request.args.get('license_id')

    if not session_id or not license_id:
        return redirect(f'{YOUR_DOMAIN}/register.html?status=error&message=Missing_Stripe_Session_ID_or_License_ID')

    license_obj = License.query.filter_by(id=license_id, stripe_session_id=session_id).first()

    if not license_obj:
        return redirect(f'{YOUR_DOMAIN}/register.html?status=error&message=License_or_Session_not_found')

    if license_obj.stripe_payment_status == 'PAID':
        return redirect(f'{YOUR_DOMAIN}/register.html?status=success&license_key={license_obj.key}')

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        if session.payment_status == 'paid':
            print(f"Stripe Status Check: Session {session_id} is paid. Activating license {license_obj.key}")
            tier_info = TIER_CONFIG[license_obj.tier.lower()]
            license_obj.key = session.metadata.get('requested_key', license_obj.key)
            license_obj.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
            license_obj.api_key = "YOUR_DEFAULT_CAPSOLVER_API_KEY"
            license_obj.tier = license_obj.tier.capitalize()
            license_obj.max_sessions = tier_info['max_sessions']
            license_obj.stripe_payment_status = 'PAID'
            db.session.commit()
            return redirect(f'{YOUR_DOMAIN}/register.html?status=success&license_key={license_obj.key}')
        elif session.payment_status == 'unpaid' or session.status == 'open':
            print(f"Stripe Status Check: Session {session_id} status is {session.payment_status}/{session.status}. Still pending.")
            return redirect(f'{YOUR_DOMAIN}/register.html?status=pending&message=Payment_still_pending')
        else:
            print(f"Stripe Status Check: Session {session_id} status is {session.payment_status}/{session.status}. Failed/Cancelled.")
            license_obj.stripe_payment_status = 'FAILED'
            db.session.commit()
            return redirect(f'{YOUR_DOMAIN}/register.html?status=failed&message=Payment_failed_or_cancelled')

    except stripe.error.StripeError as e:
        print(f"Stripe API error retrieving session: {e}")
        return redirect(f'{YOUR_DOMAIN}/register.html?status=error&message=Stripe_API_error:{str(e)}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return redirect(f'{YOUR_DOMAIN}/register.html?status=error&message=Internal_server_error:{str(e)}')

# --- Stripe Webhook endpoint ---
@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('stripe-signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        license_id = session.metadata.get('license_id')
        requested_key = session.metadata.get('requested_key')
        tier = session.metadata.get('tier')
        email = session.metadata.get('email')

        if license_id:
            license_obj = License.query.get(license_id)
            if license_obj and license_obj.stripe_payment_status == 'PENDING':
                tier_info = TIER_CONFIG[tier]
                license_obj.key = requested_key
                license_obj.expires_on = date.today() + timedelta(days=tier_info['duration_days'])
                license_obj.api_key = "CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1"
                license_obj.tier = tier.capitalize()
                license_obj.max_sessions = tier_info['max_sessions']
                license_obj.stripe_payment_status = 'PAID'
                db.session.commit()
                print(f"✅ Stripe Webhook: Payment successful! Activated '{tier}' license: {requested_key}")
            else:
                print(f"Stripe Webhook: License {license_id} not found or already processed.")
        else:
            print("Stripe Webhook: license_id not found in metadata.")

    elif event['type'] == 'checkout.session.async_payment_succeeded':
        session = event['data']['object']
        print(f"Stripe Webhook: Async payment succeeded for session {session.id}. Review and activate if not already.")

    elif event['type'] == 'checkout.session.async_payment_failed':
        session = event['data']['object']
        print(f"Stripe Webhook: Async payment failed for session {session.id}. Mark license as failed.")
        license_id = session.metadata.get('license_id')
        if license_id:
            license_obj = License.query.get(license_id)
            if license_obj and license_obj.stripe_payment_status == 'PENDING':
                license_obj.stripe_payment_status = 'FAILED'
                db.session.commit()

    return jsonify(success=True), 200

# --- Existing /verify-license and /heartbeat endpoints ---
@app.route('/verify-license', methods=['POST'])
def verify_license_main():
    data = request.get_json()
    license_key = data.get('licenseKey')

    if not license_key:
        return jsonify({'isValid': False, 'message': 'กรุณาส่ง License Key'}), 400

    license_obj = License.query.filter_by(key=license_key).first()

    if not license_obj:
        return jsonify({'isValid': False, 'message': 'License Key ไม่ถูกต้อง'}), 401
    
    if license_obj.key.startswith('PENDING-') and license_obj.stripe_payment_status == 'FAILED':
         return jsonify({'isValid': False, 'message': 'License Key นี้ไม่สามารถใช้งานได้ เนื่องจากการชำระเงินไม่สำเร็จ'}), 403

    if license_obj.expires_on < date.today():
        return jsonify({'isValid': False, 'message': f'License Key หมดอายุแล้วเมื่อวันที่ {license_obj.expires_on}'}), 403
    
    if license_obj.stripe_payment_status == 'PENDING':
         return jsonify({'isValid': False, 'message': 'License Key นี้ยังอยู่ระหว่างรอการชำระเงิน'}), 403

    try: # Added try-except block here for get_active_sessions
        active_sessions = get_active_sessions(license_obj)
    except Exception as e:
        print(f"Error getting active sessions for {license_key}: {e}")
        # Consider logging the full traceback here for debugging
        return jsonify({'isValid': False, 'message': 'เกิดข้อผิดพลาดในการประมวลผลเซสชัน กรุณาลองใหม่'}), 500
    
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

    # Added error handling around get_active_sessions and json.dumps
    try:
        active_sessions = get_active_sessions(license_obj)
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
    except Exception as e:
        print(f"Error processing heartbeat for {license_key}: {e}")
        # Consider logging the full traceback here for debugging
        return jsonify({'status': 'internal_error', 'message': 'เกิดข้อผิดพลาดภายในในการประมวลผล Heartbeat'}), 500


# --- 6. Run Application ---
if __name__ == '__main__':
    if not os.path.exists(DISK_STORAGE_PATH):
        os.makedirs(DISK_STORAGE_PATH)
    app.run(host='0.0.0.0', port=5000)
