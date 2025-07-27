# app.py

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta, date
import uuid

app = Flask(__name__)
CORS(app)

# ฐานข้อมูล License Key (ในระบบจริงควรใช้ Database)
VALID_LICENSES = {
    'EX-DEAR': {
        'expires_on': '2025-12-31',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1' 
    },
    'EX-DEV-888': {
        'expires_on': '2025-12-31',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1' 
    },
    'EX-TEST': {
        'expires_on': '2025-07-31',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1'
    }
}

# ฐานข้อมูลสำหรับเก็บ Session ที่กำลังใช้งาน (ในระบบจริงควรใช้ Redis)
ACTIVE_SESSIONS = {}
SESSION_TIMEOUT_MINUTES = 10 

@app.route('/verify-license', methods=['POST'])
def verify_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({'isValid': False, 'message': 'กรุณาส่ง License Key'}), 400

    license_key = data.get('licenseKey')
    print(f"ได้รับคำขอตรวจสอบ Key: {license_key}")

    if license_key not in VALID_LICENSES:
        print("ผลลัพธ์: Key ไม่ถูกต้อง")
        return jsonify({'isValid': False, 'message': 'License Key ไม่ถูกต้อง'}), 401

    license_info = VALID_LICENSES[license_key]
    
    try:
        expiration_date = datetime.strptime(license_info['expires_on'], '%Y-%m-%d').date()
        if expiration_date < date.today():
            print(f"ผลลัพธ์: Key หมดอายุแล้ว")
            return jsonify({'isValid': False, 'message': f'License Key หมดอายุแล้วเมื่อวันที่ {license_info["expires_on"]}'}), 403
    except ValueError:
        print("ผลลัพธ์: Format วันที่ในฐานข้อมูลไม่ถูกต้อง")
        return jsonify({'isValid': False, 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500
        
    # อนุญาตให้การ Login ใหม่เขียนทับข้อมูล Session เก่าได้เลย (Last-one-wins)
    session_token = str(uuid.uuid4())
    ACTIVE_SESSIONS[license_key] = {
        'session_token': session_token,
        'last_seen': datetime.utcnow()
    }
    
    print(f"ผลลัพธ์: Key ถูกต้อง, สร้าง/อัปเดต Session Token สำเร็จ")
    return jsonify({
        'isValid': True,
        'message': 'License Key ถูกต้องและเปิดใช้งานแล้ว',
        'expiresOn': license_info['expires_on'],
        'apiKey': license_info['api_key'],
        'sessionToken': session_token
    })

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json()
    if not data or 'licenseKey' not in data or 'sessionToken' not in data:
        return jsonify({'status': 'invalid_request'}), 400
        
    license_key = data.get('licenseKey')
    session_token = data.get('sessionToken')

    if license_key in ACTIVE_SESSIONS and ACTIVE_SESSIONS[license_key]['session_token'] == session_token:
        ACTIVE_SESSIONS[license_key]['last_seen'] = datetime.utcnow()
        print(f"[Heartbeat] ได้รับสัญญาณจาก Key: {license_key}")
        return jsonify({'status': 'ok'}), 200
    else:
        # ถ้า Token ไม่ตรงกัน (เพราะมีเครื่องอื่นมา Login ทับ) จะคืนค่า 403
        print(f"[Heartbeat] Token ไม่ถูกต้องสำหรับ Key: {license_key}. อาจถูกเครื่องอื่น Login ทับ")
        return jsonify({'status': 'invalid_session'}), 403

if __name__ == '__main__':
    # ในการใช้งานจริง ควรใช้ Production Server เช่น Gunicorn หรือ uWSGI
    app.run(host='0.0.0.0', port=5000)
