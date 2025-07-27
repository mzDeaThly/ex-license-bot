# app.py

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, date

app = Flask(__name__)
CORS(app)

# --- ฐานข้อมูลแบบใหม่: เก็บทั้งวันหมดอายุและ API Key ---
VALID_KEYS = {
    'EX-DEV-888': {
        'expiresOn': '2025-12-31',
        'apiKey': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1' # <-- API Key ของ Capsolver
    },
    'EX-TEST': {
        'expiresOn': '2025-07-31',
        'apiKey': 'CAP-ECED32012CF8CDCBE211FC698950482F8EE7669B23512943594905547D2E60E1' # <-- API Key ของ Capsolver
    }
}
# ----------------------------------------------------

@app.route('/verify-license', methods=['POST'])
def verify_license():
    data = request.get_json()
    if not data:
        return jsonify({'isValid': False, 'message': 'Invalid JSON request'}), 400

    license_key = data.get('licenseKey')

    if not license_key:
        return jsonify({'isValid': False, 'message': 'กรุณาส่ง License Key'}), 400

    if license_key in VALID_KEYS:
        try:
            license_data = VALID_KEYS[license_key]
            expire_str = license_data['expiresOn']
            api_key = license_data['apiKey'] # <-- ดึง API Key
            
            expiration_date = datetime.strptime(expire_str, '%Y-%m-%d').date()
            today = date.today()

            if expiration_date >= today:
                # ถ้า Key ถูกต้อง ให้ส่ง apiKey กลับไปด้วย
                return jsonify({
                    'isValid': True,
                    'message': 'License Key ถูกต้องและเปิดใช้งานแล้ว',
                    'expiresOn': expire_str,
                    'apiKey': api_key # <-- เพิ่ม apiKey ในการตอบกลับ
                })
            else:
                return jsonify({
                    'isValid': False, 
                    'message': f'License Key ของคุณหมดอายุแล้วเมื่อวันที่ {expire_str}'
                }), 403

        except (ValueError, KeyError):
            return jsonify({'isValid': False, 'message': 'ข้อมูลบนเซิร์ฟเวอร์ไม่ถูกต้อง'}), 500
    else:
        return jsonify({'isValid': False, 'message': 'License Key ไม่ถูกต้อง'}), 401

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
