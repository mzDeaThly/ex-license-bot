# app.py

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, date

# สร้าง Flask application
app = Flask(__name__)
# เปิดใช้งาน CORS สำหรับทุก request ที่เข้ามา
CORS(app)

# --- ฐานข้อมูล License Key พร้อมวันหมดอายุ (ตัวอย่าง) ---
# ในระบบจริง ควรใช้ฐานข้อมูลเช่น MySQL, PostgreSQL, หรือ MongoDB
# รูปแบบคือ 'LICENSE_KEY': 'YYYY-MM-DD'
VALID_KEYS = {
    'EX-ADMIN': '2025-12-31',
    'EXPIRED-KEY-TEST': '2024-01-01' # Key ที่หมดอายุแล้วสำหรับทดสอบ
}
# ----------------------------------------------------

@app.route('/verify-license', methods=['POST'])
def verify_license():
    """
    Endpoint สำหรับตรวจสอบ License Key และวันหมดอายุ
    """
    data = request.get_json()
    if not data:
        return jsonify({'isValid': False, 'message': 'Invalid JSON request'}), 400

    license_key = data.get('licenseKey')

    print(f"ได้รับคำขอตรวจสอบ Key: {license_key}")

    if not license_key:
        return jsonify({'isValid': False, 'message': 'กรุณาส่ง License Key'}), 400

    if license_key in VALID_KEYS:
        try:
            expire_str = VALID_KEYS[license_key]
            expiration_date = datetime.strptime(expire_str, '%Y-%m-%d').date()
            today = date.today()

            if expiration_date >= today:
                print(f"ผลลัพธ์: Key ถูกต้อง, หมดอายุวันที่ {expire_str}")
                return jsonify({
                    'isValid': True, 
                    'message': 'License Key ถูกต้องและเปิดใช้งานแล้ว',
                    'expiresOn': expire_str
                })
            else:
                print(f"ผลลัพธ์: Key หมดอายุแล้วตั้งแต่วันที่ {expire_str}")
                return jsonify({
                    'isValid': False, 
                    'message': f'License Key ของคุณหมดอายุแล้วเมื่อวันที่ {expire_str}'
                }), 403
                
        except ValueError:
            print("ผลลัพธ์: Format วันที่ในฐานข้อมูลไม่ถูกต้อง")
            return jsonify({'isValid': False, 'message': 'เกิดข้อผิดพลาดฝั่งเซิร์ฟเวอร์ (Invalid date format)'}), 500
    else:
        print("ผลลัพธ์: Key ไม่ถูกต้อง")
        return jsonify({'isValid': False, 'message': 'License Key ไม่ถูกต้อง'}), 401

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)