import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, date, timedelta
import uuid
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
import json

# --- 1. ตั้งค่าพื้นฐานและฐานข้อมูล ---
app = Flask(__name__)
CORS(app)

DISK_STORAGE_PATH = '/var/data'
DATABASE_FILE = 'licenses.db'
DATABASE_PATH = os.path.join(DISK_STORAGE_PATH, DATABASE_FILE)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + DATABASE_PATH
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'exadmin' 

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

# --- 3. สร้างแผงควบคุมสำหรับ Admin ---
admin = Admin(app, name='License Manager', template_mode='bootstrap4')
admin.add_view(ModelView(License, db.session))

# --- [แก้ไข] ย้าย db.create_all() มาไว้ตรงนี้ ---
# สร้างไฟล์ฐานข้อมูลและ Table ทั้งหมดในครั้งแรกที่รัน
# และจะข้ามไปหาก Table มีอยู่แล้ว
with app.app_context():
    db.create_all()

# --- 4. ปรับแก้ API Endpoints ให้ทำงานกับฐานข้อมูล (โค้ดเดิม) ---
SESSION_TIMEOUT_MINUTES = 10

def get_active_sessions(license_obj):
    # ... โค้ดเดิม ...
    sessions = json.loads(license_obj.active_sessions)
    fresh_sessions = []
    now = datetime.utcnow()
    
    for session in sessions:
        last_seen = datetime.fromisoformat(session['last_seen'])
        if now - last_seen < timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            fresh_sessions.append(session)
            
    return fresh_sessions

@app.route('/verify-license', methods=['POST'])
def verify_license():
    # ... โค้ดเดิม ...
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
    # ... โค้ดเดิม ...
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


# [ลบ] นำ db.create_all() ออกจากส่วนนี้
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
