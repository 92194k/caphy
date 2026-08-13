"""
Flask routes for camera detection and selection
Add these to your app.py Flask app
"""

from flask import jsonify, request
from camera_detector import (
    get_all_cameras,
    test_camera_connection,
    save_camera_config,
    load_camera_config
)

def register_camera_routes(app):
    """Register camera routes to Flask app"""

    @app.route('/api/cameras', methods=['GET'])
    def get_cameras():
        """List all detected cameras"""
        cameras = get_all_cameras()
        return jsonify(cameras)

    @app.route('/api/cameras/saved', methods=['GET'])
    def get_saved_camera():
        """Get currently saved camera"""
        saved = load_camera_config()
        return jsonify({"saved_camera": saved})

    @app.route('/api/cameras/select', methods=['POST'])
    def select_camera():
        """User selects which camera to use"""
        data = request.json
        camera_source = data.get('camera')

        if not camera_source:
            return jsonify({"error": "No camera specified"}), 400

        # Save to config file (works offline)
        save_camera_config(camera_source)

        return jsonify({
            "status": "success",
            "message": "Camera selected",
            "camera": camera_source
        })

    @app.route('/api/cameras/test', methods=['POST'])
    def test_camera():
        """Test if camera connection works"""
        data = request.json
        camera_source = data.get('camera')

        if not camera_source:
            return jsonify({"error": "No camera specified"}), 400

        result = test_camera_connection(camera_source)

        return jsonify({
            "status": "success" if result['connected'] else "failed",
            "connected": result['connected'],
            "camera": str(camera_source),
            "error": result['error']
        })

    return app
