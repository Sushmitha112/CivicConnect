from flask import Flask, request, jsonify, send_from_directory
import tensorflow as tf
import numpy as np
from PIL import Image
import io
import cv2
import os
from datetime import datetime
from flask_cors import CORS
from database import get_db, init_db
from flask import send_file

app = Flask(__name__)
CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})

# Serve frontend files from ../frontend so root URL returns the UI
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")


def find_frontend_file(filename):
    """Resolve clean routes and .html links from the single frontend folder."""
    requested = filename.lstrip("/")
    candidates = [
        os.path.join(FRONTEND_DIR, requested),
        os.path.join(TEMPLATES_DIR, requested),
    ]
    if not requested.endswith(".html"):
        candidates.extend([
            os.path.join(FRONTEND_DIR, requested + ".html"),
            os.path.join(TEMPLATES_DIR, requested + ".html"),
        ])

    for candidate in candidates:
        resolved = os.path.abspath(candidate)
        if os.path.isfile(resolved) and os.path.commonpath([FRONTEND_DIR, resolved]) == FRONTEND_DIR:
            return resolved
    return None


@app.route("/")
def serve_index():
    index_path = find_frontend_file("index.html") or find_frontend_file("home.html")
    if index_path:
        return send_file(index_path)
    return jsonify({"error": "index.html not found"}), 404


@app.route('/<path:filename>', methods=["GET"])
def serve_frontend_files(filename):
    file_path = find_frontend_file(filename)
    if file_path:
        return send_file(file_path)
    return jsonify({"error": "file not found"}), 404

# Allow OPTIONS (important for CORS preflight)
@app.route("/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({"status": "OK"}), 200

# -----------------------------
# LOAD MODEL (keep as before)
# -----------------------------
# (If model file is large and not required for the admin pages, you can lazy-load)
model = tf.keras.models.load_model("civic_issue_model.keras")
class_names = ["garbage", "other", "pothole"]

# -----------------------------
# DB CONNECTION (SQLite via database.py)
# -----------------------------
# Using get_db() from backend/database.py which wraps sqlite3

# -----------------------------
# IMAGE PREPROCESSING / SEVERITY
# -----------------------------
def preprocess_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((224, 224))
    img = np.array(img) / 255.0
    return np.expand_dims(img, axis=0)

def calculate_severity(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = np.array(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    max_area = max([cv2.contourArea(cnt) for cnt in contours], default=0)
    img_area = img.shape[0] * img.shape[1] if img.shape[0] and img.shape[1] else 1
    ratio = (max_area / img_area) * 100
    if ratio < 5:
        level = "LOW"
    elif ratio < 15:
        level = "MEDIUM"
    else:
        level = "HIGH"
    return level, round(ratio, 2)

# -----------------------------
# ML PREDICTION
# -----------------------------
@app.route("/predict", methods=["POST"])
def predict():
    file = request.files.get("image") or request.files.get("photo")
    if not file:
        return jsonify({"error": "No image uploaded"}), 400
    img_bytes = file.read()
    img = preprocess_image(img_bytes)
    preds = model.predict(img)
    class_index = int(np.argmax(preds))
    prediction = class_names[class_index]
    confidence = float(preds[0][class_index])
    severity_level, severity_score = calculate_severity(img_bytes)
    return jsonify({
        "prediction": prediction,
        "confidence": round(confidence, 4),
        "severity_level": severity_level,
        "severity_score": severity_score
    })

# -----------------------------
# SAVE ISSUE
# -----------------------------
@app.route("/report-issue", methods=["POST"])
def report_issue():
    try:
        db = get_db()
        cursor = db.cursor()

        description = request.form.get("description")
        location = request.form.get("location")
        predicted_issue = request.form.get("predicted_issue")
        confidence = request.form.get("confidence")
        severity_score = request.form.get("severity_score")
        lat = request.form.get("lat")
        lng = request.form.get("lng")
        photo_count = int(request.form.get("photo_count", 0))

        citizen_name = request.form.get("citizen_name")
        citizen_email = request.form.get("citizen_email")
        citizen_phone = request.form.get("citizen_phone")

        upload_folder = "uploads"
        os.makedirs(upload_folder, exist_ok=True)

        img_paths = []
        for i in range(1, photo_count + 1):
            file = request.files.get(f"photo_{i}")
            if file:
                filename = file.filename
                filepath = os.path.join(upload_folder, filename)
                file.save(filepath)
                img_paths.append(filename)
            else:
                img_paths.append(None)

        dept_map = {
            "garbage": "Sanitation",
            "pothole": "Roads Department",
            "other": "General Department"
        }

        assigned_department = dept_map.get(predicted_issue, "General Department")

        sql = """
            INSERT INTO issues (
                detected_issue, confidence, severity_score,
                description, location_text, latitude, longitude,
                image1_path, image2_path, assigned_department,
                citizen_name, citizen_email, citizen_phone
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """

        values = (
            predicted_issue, confidence, severity_score,
            description, location, lat, lng,
            img_paths[0] if len(img_paths) > 0 else None,
            img_paths[1] if len(img_paths) > 1 else None,
            assigned_department,
            citizen_name, citizen_email, citizen_phone
        )

        cursor.execute(sql, values)
        db.commit()
        complaint_id = cursor.lastrowid
        db.close()

        return jsonify({"success": True, "complaint_id": complaint_id})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# -----------------------------
# USER SIGNUP / LOGIN
# -----------------------------
@app.route("/signup", methods=["POST"])
def signup():
    try:
        db = get_db()
        cursor = db.cursor()

        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        pincode = request.form.get("pincode")
        phone = request.form.get("phone")   # <-- FIXED (ADD THIS)

        cursor.execute("SELECT * FROM users WHERE email=?", (email,))
        if cursor.fetchone():
            return jsonify({"success": False, "message": "Email already exists"})

        # 🔥 FIX: Insert phone number also
        cursor.execute(
            "INSERT INTO users (name, email, password, pincode, phone) VALUES (?,?,?,?,?)",
            (name, email, password, pincode, phone)
        )

        db.commit()
        db.close()
        return jsonify({"success": True})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/officer-register", methods=["POST"])
def officer_register():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    department = request.form.get("department", "").strip()
    if not all([name, email, password, department]):
        return jsonify({"success": False, "message": "All officer registration fields are required."}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO officers (name, email, password, department, approved) VALUES (?,?,?,?,0)",
            (name, email, password, department)
        )
        db.commit()
        return jsonify({"success": True})
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            return jsonify({"success": False, "message": "An account with this email already exists."}), 409
        return jsonify({"success": False, "message": "Could not submit officer registration."}), 500
    finally:
        db.close()

@app.route("/login", methods=["POST"])
def login():
    db = get_db()
    cursor = db.cursor()
    email = request.form.get("email")
    password = request.form.get("password")
    cursor.execute("SELECT * FROM users WHERE email=? AND password=?", (email, password))
    user = cursor.fetchone()
    db.close()
    if not user:
        return jsonify({"success": False, "message": "Invalid credentials"})
    return jsonify({"success": True, "user": dict(user)})

# -----------------------------
# OFFICER LOGIN
# -----------------------------
@app.route("/officer-login", methods=["POST"])
def officer_login():
    email = request.form.get("email")
    password = request.form.get("password")
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM officers WHERE email=?", (email,))
    officer = cursor.fetchone()
    if not officer:
        return jsonify({"success": False, "message": "Officer not found"})
    if officer["password"] != password:
        return jsonify({"success": False, "message": "Wrong password"})
    if officer["approved"] != 1:
        return jsonify({"success": False, "message": "Admin approval pending"})
    db.close()
    return jsonify({"success": True, "officer": dict(officer)})

# -----------------------------
# ADMIN LOGIN
# -----------------------------
@app.route("/admin-login", methods=["POST"])
def admin_login():
    email = request.form.get("email")
    password = request.form.get("password")
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM admins WHERE email=? AND password=?", (email, password))
    admin = cursor.fetchone()
    db.close()
    if admin:
        return jsonify({"success": True, "admin": dict(admin)})
    return jsonify({"success": False, "message": "Invalid admin credentials"})

# -----------------------------
# ISSUE ROUTES
# -----------------------------
@app.route("/issues/all")
def issues_all():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues ORDER BY issue_id DESC")
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issues/today")
def issues_today():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues WHERE DATE(created_at) = DATE('now') ORDER BY issue_id DESC")
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issues/garbage")
def issues_garbage():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues WHERE detected_issue='garbage' ORDER BY issue_id DESC")
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issues/pothole")
def issues_pothole():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues WHERE detected_issue='pothole' ORDER BY issue_id DESC")
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issue/<int:issue_id>")
def get_issue(issue_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,))
    issue = cursor.fetchone()
    db.close()
    if not issue:
        return jsonify({"error": "Issue not found"}), 404
    return jsonify(dict(issue))

# -----------------------------
# UPDATE ISSUE STATUS
# -----------------------------
@app.route("/update-status", methods=["POST"])
def update_status():
    issue_id = request.form.get("issue_id")
    status = request.form.get("status")
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE issues SET status=? WHERE issue_id=?", (status, issue_id))
    db.commit()
    db.close()
    return jsonify({"message": "Status updated"})

@app.route("/reopen-issue", methods=["POST"])
def reopen_issue():
    issue_id = request.form.get("issue_id")
    db = get_db()
    db.execute("UPDATE issues SET status='Pending' WHERE issue_id=?", (issue_id,))
    db.commit()
    db.close()
    return jsonify({"success": True, "message": "Issue reopened"})

@app.route("/submit-feedback", methods=["POST"])
def submit_feedback():
    issue_id = request.form.get("issue_id")
    feedback = request.form.get("feedback", "").strip()
    db = get_db()
    db.execute("UPDATE issues SET feedback=? WHERE issue_id=?", (feedback, issue_id))
    db.commit()
    db.close()
    return jsonify({"success": True, "message": "Feedback submitted"})

@app.route("/submit-rating", methods=["POST"])
def submit_rating():
    issue_id = request.form.get("issue_id")
    rating = request.form.get("rating")
    db = get_db()
    db.execute("UPDATE issues SET rating=? WHERE issue_id=?", (rating, issue_id))
    db.commit()
    db.close()
    return jsonify({"success": True, "message": "Rating submitted"})

# -----------------------------
# OFFICER APPROVAL
# -----------------------------
@app.route("/officers/pending")
def pending_officers():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM officers WHERE approved=0")
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/approve-officer", methods=["POST"])
def approve_officer():
    officer_id = request.form.get("officer_id")
    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE officers SET approved=1 WHERE id=?", (officer_id,))
    db.commit()
    db.close()
    return jsonify({"message": "Officer approved"})

@app.route("/reject-officer", methods=["POST"])
def reject_officer():
    officer_id = request.form.get("officer_id")
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM officers WHERE id=?", (officer_id,))
    db.commit()
    db.close()
    return jsonify({"message": "Officer rejected"})

# -----------------------------
# COUNTS
# -----------------------------
@app.route("/count/garbage")
def count_garbage():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM issues WHERE detected_issue='garbage'")
    (count,) = cursor.fetchone()
    db.close()
    return jsonify({"count": count})

@app.route("/count/pothole")
def count_pothole():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM issues WHERE detected_issue='pothole'")
    (count,) = cursor.fetchone()
    db.close()
    return jsonify({"count": count})

# -----------------------------
# SERVE UPLOADS
# -----------------------------
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory('uploads', filename)

@app.route("/issues/user/<email>")
def issues_by_user(email):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM issues WHERE citizen_email=? ORDER BY issue_id DESC", (email,))
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

# -----------------------------
# USER COMPLAINT COUNTS
# -----------------------------
@app.route("/user/counts", methods=["POST"])
def user_counts():
    email = request.form.get("email")   # user email from frontend
    db = get_db()
    cursor = db.cursor()

    # Total complaints
    cursor.execute("SELECT COUNT(*) FROM issues WHERE citizen_email=?", (email,))
    total = cursor.fetchone()[0]

    # Pending
    cursor.execute("SELECT COUNT(*) FROM issues WHERE citizen_email=? AND status='Pending'", (email,))
    pending = cursor.fetchone()[0]

    # In Progress
    cursor.execute("SELECT COUNT(*) FROM issues WHERE citizen_email=? AND status='In Progress'", (email,))
    progress = cursor.fetchone()[0]

    # Resolved
    cursor.execute("SELECT COUNT(*) FROM issues WHERE citizen_email=? AND status='Resolved'", (email,))
    resolved = cursor.fetchone()[0]

    db.close()

    return jsonify({
        "total": total,
        "pending": pending,
        "progress": progress,
        "resolved": resolved
    })


@app.route("/user/issues", methods=["POST"])
def user_issues():
    email = request.form.get("email")
    db = get_db()
    cursor = db.cursor()

    cursor.execute("SELECT * FROM issues WHERE citizen_email=? ORDER BY issue_id DESC", (email,))
    issues = cursor.fetchall()
    db.close()

    return jsonify([dict(r) for r in issues])

@app.route("/issues/priority-garbage")
def priority_garbage():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT * FROM issues
        WHERE detected_issue = 'garbage'
        AND status IN ('Pending', 'In Progress')
        ORDER BY severity_score DESC
        LIMIT 5
    """)
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issues/priority-pothole")
def priority_pothole():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT * FROM issues
        WHERE detected_issue = 'pothole'
        AND status IN ('Pending', 'In Progress')
        ORDER BY severity_score DESC
        LIMIT 5
    """)
    rows = cursor.fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/issues/nearby", methods=["GET"])
def get_nearby_issues():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM issues
        ORDER BY created_at DESC
        LIMIT 20
    """)

    data = cursor.fetchall()
    db.close()
    
    return jsonify([dict(r) for r in data])

@app.route("/submit-issue", methods=["POST"])
def submit_issue():
    detected_issue = request.form.get("detected_issue", "").lower()

    # ✅ SECURITY CHECK
    if detected_issue not in ["pothole", "garbage"]:
        return jsonify({
            "success": False,
            "message": "Only pothole and garbage complaints are allowed."
        }), 400

# -----------------------------
# RUN SERVER
# -----------------------------
if __name__ == "__main__":
    # Ensure SQLite DB is initialized before serving requests
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
