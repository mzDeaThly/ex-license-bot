# app.py

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta, date
import uuid

app = Flask(__name__)
CORS(app)

# --- [การเปลี่ยนแปลงที่ 1] โครงสร้างข้อมูลใหม่ ---
# เพิ่ม key 'session' เข้าไปในแต่ละ license เพื่อเก็บข้อมูล session ของตัวเองโดยตรง
# ค่าเริ่มต้นเป็น None คือยังไม่มี session
VALID_LICENSES = {
    'EX-DEAR': {
        'expires_on': '2025-12-31',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1',
        'session': None 
    },
    'EX-DEV-888': {
        'expires_on': '2024-07-31',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1',
        'session': None
    },
    'EX-TEST': {
        'expires_on': '2024-01-01',
        'api_key': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1',
        'session': None
    }
}

# ไม่จำเป็นต้องใช้ ACTIVE_SESSIONS แบบ global อีกต่อไป
# SESSION_TIMEOUT_MINUTES = 10 # หากต้องการใช้การ timeout สามารถนำกลับมาได้

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

    # ดึงข้อมูล license ทั้ง object
    license_info = VALID_LICENSES[license_key]
    
    try:
        expiration_date = datetime.strptime(license_info['expires_on'], '%Y-%m-%d').date()
        if expiration_date < date.today():
            print(f"ผลลัพธ์: Key หมดอายุแล้ว")
            return jsonify({'isValid': False, 'message': f'License Key หมดอายุแล้วเมื่อวันที่ {license_info["expires_on"]}'}), 403
    except ValueError:
        print("ผลลัพธ์: Format วันที่ในฐานข้อมูลไม่ถูกต้อง")
        return jsonify({'isValid': False, 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์'}), 500
        
    # --- [การเปลี่ยนแปลงที่ 2] สร้าง/เขียนทับ Session ลงใน license_info โดยตรง ---
    session_token = str(uuid.uuid4())
    license_info['session'] = {
        'token': session_token,
        'last_seen': datetime.utcnow()
    }
    
    print(f"ผลลัพธ์: Key ถูกต้อง, สร้าง/อัปเดต Session Token สำหรับ {license_key} สำเร็จ")
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

    # --- [การเปลี่ยนแปลงที่ 3] ตรวจสอบ Session จากใน license_info โดยตรง ---
    if license_key in VALID_LICENSES:
        license_info = VALID_LICENSES[license_key]
        # ตรวจสอบว่ามี session อยู่ และ token ตรงกันหรือไม่
        if license_info['session'] and license_info['session']['token'] == session_token:
            license_info['session']['last_seen'] = datetime.utcnow()
            print(f"[Heartbeat] ได้รับสัญญาณจาก Key: {license_key}")
            return jsonify({'status': 'ok'}), 200

    # ถ้าเงื่อนไขข้างบนไม่ผ่าน (key ไม่มี, ไม่มี session, หรือ token ไม่ตรง)
    print(f"[Heartbeat] Token ไม่ถูกต้องสำหรับ Key: {license_key}. อาจถูกเครื่องอื่น Login ทับ")
    return jsonify({'status': 'invalid_session'}), 403

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
