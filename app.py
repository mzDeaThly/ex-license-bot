import os
import json
import uuid
from datetime import datetime, date, timedelta

from flask import Flask, request, jsonify, Response, redirect
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin, AdminIndexView
from flask_admin.contrib.sqla import ModelView
import stripe

# --- 1. Basic Setup ---
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# --- Environment Variables Setup ---
stripe.api_version = '2024-06-20'
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
stripe_webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN', 'http://127.0.0.1:5000')

# --- Database Setup for Render Persistent Disk ---
DISK_STORAGE_PATH = '/var/data'
DATABASE_FILE = 'licenses.db'
if not os.path.exists(DISK_STORAGE_PATH):
    DISK_STORAGE_PATH = '.'
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

# --- 3. Admin Panel ---
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

# --- Create Database Tables ---
with app.app_context():
    db.create_all()

# --- 5. API Endpoints ---
TIER_CONFIG = {
    'basic': {'price_satang': 120000, 'duration_days': 30, 'max_sessions': 1, 'name': 'Basic Plan (1 Session)'},
    'basic3': {'price_satang': 180000, 'duration_days': 90, 'max_sessions': 3, 'name': 'Basic Plan (3 Sessions)'},
    'pro': {'price_satang': 250000, 'duration_days': 30, 'max_sessions': 1, 'name': 'Pro Plan (1 Session)'},
    'pro3': {'price_satang': 450000, 'duration_days': 90, 'max_sessions': 3, 'name': 'Pro Plan (3 Sessions)'}
}

@app.route('/')
def index():
    return redirect(YOUR_DOMAIN + '/register.html')

@app.route('/create-stripe-checkout-session', methods=['POST'])
def create_stripe_checkout_session():
    try:
        data = request.get_json()
        email = data.get('email')
        user_key = data.get('licenseKey')
        tier = data.get('tier')

        if not all([email, user_key, tier]):
            return jsonify({'message': 'Missing or invalid information'}), 400

        if License.query.filter_by(key=user_key).first():
            return jsonify({'message': 'This License Key is already in use.'}), 409

        tier_info = TIER_CONFIG[tier]
        session = stripe.checkout.Session.create(
            payment_method_types=['card', 'promptpay'],
            line_items=[{
                'price_data': {
                    'currency': 'thb',
                    'product_data': {
                        'name': f"{tier_info['name']} - Key: {user_key}",
                    },
                    'unit_amount': tier_info['price_satang'],
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=email,
            success_url=f"{YOUR_DOMAIN}/register.html?status=success&license_key={user_key}",
            cancel_url=f"{YOUR_DOMAIN}/register.html?status=cancelled",
            metadata={ 'license_key': user_key, 'tier': tier }
        )
        return jsonify({'sessionId': session.id})
    except Exception as e:
        return jsonify({'message': str(e)}), 500

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    event = None
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, stripe_webhook_secret)
    except Exception as e:
        return str(e), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        if session.get('payment_status') == 'paid':
            metadata = session.get('metadata')
            requested_key = metadata.get('license_key')
            tier = metadata.get('tier')

            if License.query.filter_by(key=requested_key).first():
                return jsonify({'status': 'skipped', 'message': 'License already exists.'})

            tier_info = TIER_CONFIG[tier]
            new_license = License(
                key=requested_key,
                expires_on=date.today() + timedelta(days=tier_info['duration_days']),
                
                # ✨ บรรทัดนี้คือการใส่ค่า Capsolver API Key แบบตายตัวเหมือนไฟล์แรก ✨
                api_key="CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1", # 
                
                tier=tier,
                max_sessions=tier_info['max_sessions'],
                active_sessions='[]'
            )
            db.session.add(new_license)
            db.session.commit()
            print(f"✅ Webhook: Payment successful! Activated '{tier}' license: {requested_key}")

    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
