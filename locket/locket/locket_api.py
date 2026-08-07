from flask import Blueprint, jsonify, request
import requests

api_bp = Blueprint('api', __name__)

@api_bp.route('/unlock', methods=['POST'])
def unlock_gold():
    data = request.get_json() or {}
    username = data.get('username')
    
    if not username:
        return jsonify({"success": False, "message": "Vui lòng nhập tên tài khoản!"}), 400
    
    try:
        # Viết logic code xử lý mở khóa hoặc gọi API thực tế của anh ở đây
        # Ví dụ gửi request hoặc xử lý dữ liệu:
        # response = requests.post('URL_API_CUA_ANH', json={"username": username})
        
        return jsonify({
            "success": True, 
            "message": f"Thành công! Đã xử lý mở khóa cho tài khoản: {username}"
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Lỗi hệ thống: {str(e)}"}), 500
