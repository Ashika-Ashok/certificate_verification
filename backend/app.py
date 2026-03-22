from flask import Flask, render_template, request, redirect, session, send_file, url_for
from google import genai
import pytesseract
from PIL import Image
import os
import json
import qrcode
from web3 import Web3
import hashlib
from dotenv import load_dotenv
from twilio.rest import Client
import random
from PyPDF2 import PdfReader
import re
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import smtplib
from email.message import EmailMessage
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash


load_dotenv()

# -----------------------------
# CONNECT TO GANACHE
# -----------------------------
w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
abi_path = os.path.join(BASE_DIR, "..", "Blockchain", "abi.json")

with open(abi_path) as f:
    abi = json.load(f)

contract_address = os.getenv("CONTRACT_ADDRESS")
contract = w3.eth.contract(address=contract_address, abi=abi)

account = w3.eth.accounts[0]
private_key = os.getenv("PRIVATE_KEY")

# -----------------------------
# FLASK APP
# -----------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# ---------------- DATABASE ---------------- #

def create_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        email TEXT UNIQUE,
        password TEXT
    )
    """)

    conn.commit()
    conn.close()

create_db()

# -----------------------------
# TWILIO SETUP
# -----------------------------
twilio_client = Client(
    os.getenv("TWILIO_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)
TWILIO_PHONE = os.getenv("TWILIO_PHONE")

# -----------------------------
# GEMINI
# -----------------------------
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# -----------------------------
# HELPERS
# -----------------------------
def standardize_phone(phone):
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = "+91" + phone
    return phone

def extract_text(image_path):
    img = Image.open(image_path)
    return pytesseract.image_to_string(img)

def extract_certificate_hash_from_text(text):
    match = re.search(r"\b[a-fA-F0-9]{64}\b", text)
    return match.group(0) if match else ""

# -----------------------------
# AI VERIFICATION
# -----------------------------
def ai_full_verification(text):
    prompt = f"""
    You are a certificate verification assistant.
Rules:
1. If company name appears legitimate and formatting looks consistent, mark VALID.
2. Do NOT assume fake unless strong evidence of forgery exists.
3. If OCR text is incomplete but not suspicious, mark VALID.
4. Only mark LIKELY FAKE if clear inconsistencies exist.
Return ONLY valid JSON.

Format:
{{
  "student_name": "",
  "company_name": "",
  "designation": "",
  "duration": "",
  "confidence_score": "0-100",
  "final_verdict": "VALID / SUSPICIOUS / LIKELY FAKE",
  "reason_summary": ""
}}

Certificate Text:
{text}
"""
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw_output = response.text.strip()
    try:
        cleaned = raw_output.replace("```json", "").replace("```", "")
        return json.loads(cleaned)
    except:
        return {
            "student_name": "Unknown",
            "company_name": "Unknown",
            "designation": "Unknown",
            "duration": "Unknown",
            "confidence_score": "50",
            "final_verdict": "SUSPICIOUS",
            "reason_summary": "AI parsing failed."
        }

# -----------------------------
# ROUTES
# -----------------------------
@app.route('/')
def index():
    return render_template("login.html")   # LOGIN FIRST

# ---------------- SIGNUP ---------------- #

@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = generate_password_hash(request.form["password"])

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute(
        "INSERT INTO users(username,email,password) VALUES(?,?,?)",
        (username,email,password))

        conn.commit()
        conn.close()

        return redirect("/login")

    return render_template("signup.html")

# ---------------- LOGIN ---------------- #

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE email=?",(email,))
        user = cursor.fetchone()

        conn.close()

        if user and check_password_hash(user[3], password):
            session["user"] = user[1]
            return redirect("/dashboard")

        return "Invalid login"

    return render_template("login.html")

# ---------------- DASHBOARD ---------------- #

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")

    return render_template("index.html", user=session["user"])

# ---------------- LOGOUT ---------------- #

@app.route("/logout")
def logout():
    session.pop("user",None)
    return redirect("/login")

# -----------------------------
# AI Verification Route
# -----------------------------
@app.route('/verify', methods=["POST"])
def verify():
    file = request.files.get("certificate")
    email = request.form.get("email", "").strip()
    phone = standardize_phone(request.form.get("phone", ""))

    if not (file and email and phone):
        return "Missing file/email/phone."

    upload_folder = os.path.join(app.root_path, "uploads")
    os.makedirs(upload_folder, exist_ok=True)

    filepath = os.path.join(upload_folder, file.filename)
    file.save(filepath)

    text = extract_text(filepath)
    result = ai_full_verification(text)
    verdict = result.get("final_verdict", "").strip().upper()

    if verdict != "VALID":
        return render_template("result.html", data=result, status="NOT VALID")
    with open(filepath, "rb") as f:
        certificate_hash = hashlib.sha256(f.read()).hexdigest()

    email_hash = hashlib.sha256(email.encode()).hexdigest()
    phone_hash = hashlib.sha256(phone.encode()).hexdigest()

    user_name = result.get("student_name", "").strip()
    tx = contract.functions.storeCertificate(
        file.filename,
        user_name,
        certificate_hash,
        email_hash,
        phone_hash
    ).build_transaction({
        'from': account,
        'nonce': w3.eth.get_transaction_count(account),
        'gas': 2000000,
        'gasPrice': w3.to_wei('20', 'gwei')
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    result.update({
        "blockchain_hash": certificate_hash,
        "transaction_hash": tx_hash.hex(),
        "block_number": receipt.blockNumber,
        "certificate_id": file.filename
    })

    qr_data = f"Certificate ID: {file.filename}\nTransaction: {tx_hash.hex()}"
    qr = qrcode.make(qr_data)
    qr_folder = os.path.join(app.root_path, "static", "qr")
    os.makedirs(qr_folder, exist_ok=True)
    qr_filename = f"{file.filename}_qr.png"
    qr.save(os.path.join(qr_folder, qr_filename))
    result["qr_image"] = f"qr/{qr_filename}"

    return render_template("result.html", data=result, status="VALID & STORED ON BLOCKCHAIN")

# -----------------------------
# HASH VERIFICATION WITH EMAIL OTP
# -----------------------------
@app.route('/hash_verify_original', methods=["POST"])
def hash_verify_original():
    file = request.files.get("certificate")
    email = request.form.get("email", "").strip()
    phone = standardize_phone(request.form.get("phone", ""))

    if not (file and email and phone):
        return "Missing file/email/phone."

    upload_folder = os.path.join(app.root_path, "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, file.filename)
    file.save(filepath)

    # AI verification
    text = extract_text(filepath)
    result = ai_full_verification(text)

    # Hash verification
    with open(filepath, "rb") as f:
        uploaded_hash = hashlib.sha256(f.read()).hexdigest()

    try:
        cert = contract.functions.getCertificate(file.filename).call()
        stored_hash = cert[2]
        stored_user_name = cert[1]
    except:
        return "Certificate not found on blockchain."

    if uploaded_hash == stored_hash:
        verification_status = "AI VERIFIED & HASH VERIFIED"
    else:
        verification_status = "AI VERIFIED BUT HASH NOT MATCHED"

    # Store verification data in session for OTP step
    session["hash_verification_result"] = {
        "certificate_id": file.filename,
        "user_name": stored_user_name,
        "status": verification_status,
        "email": email
    }

    # Render email input page
    return render_template("hash_verify_email.html", email=email)

# -----------------------------
# EMAIL OTP ROUTES
# -----------------------------
@app.route("/verify_hash_otp", methods=["POST"])
def verify_hash_otp():
    email = request.form.get("otp_email", "").strip()
    if not email:
        return "Email is required."

    session["hash_email"] = email
    otp = str(random.randint(100000, 999999))
    session["hash_otp"] = otp

    # Send OTP via email
    msg = EmailMessage()
    msg["Subject"] = "Your Certificate Verification OTP"
    msg["From"] = os.getenv("EMAIL_FROM")
    msg["To"] = email
    msg.set_content(f"Your OTP for certificate verification is: {otp}")

    try:
        # STARTTLS for port 587
        with smtplib.SMTP(os.getenv("SMTP_SERVER"), int(os.getenv("SMTP_PORT"))) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(os.getenv("EMAIL_FROM"), os.getenv("EMAIL_PASSWORD"))
            smtp.send_message(msg)
    except Exception as e:
        return f"Failed to send OTP: {e}"

    return render_template("enter_hash_otp.html")  # Page to enter OTP

@app.route("/confirm_hash_otp", methods=["POST"])
def confirm_hash_otp():
    otp_input = request.form.get("otp_input", "").strip()
    saved_otp = session.get("hash_otp")
    if not saved_otp:
        return "Session expired. Please retry verification."
    if otp_input != saved_otp:
        return "Invalid OTP. Please try again."

    # Get stored hash verification data from session
    result = session.get("hash_verification_result")
    if not result:
        return "Session expired. Please re-upload certificate."

    # Clear OTP from session
    session.pop("hash_otp", None)

    return render_template(
        "hash_result.html",
        certificate_id=result["certificate_id"],
        user_name=result["user_name"],
        status=result["status"]
    )

# -----------------------------
# HASH VERIFY Ownership REPORT (UNCHANGED)
# -----------------------------
@app.route('/hash_verify_report', methods=["POST"])
def hash_verify_report():
    file = request.files.get("hash_certificate")
    if not file:
        return "No file uploaded."

    upload_folder = os.path.join(app.root_path, "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    filepath = os.path.join(upload_folder, file.filename)
    file.save(filepath)

    reader = PdfReader(filepath)
    raw_text = ""
    for page in reader.pages:
        if page.extract_text():
            raw_text += page.extract_text() + "\n"

    # Normalize spaces + line breaks
    text = re.sub(r"[ \t]+", " ", raw_text)
    text = re.sub(r"\n+", "\n", text)

    def extract(label):
        pattern = rf"{label}\s*\n?\s*(.+)"
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    certificate_id_in_report = extract("Certificate ID")
    user_name_in_report = extract("User Name")
    certificate_hash_in_report = extract("Certificate Hash")

    try:
        cert = contract.functions.getCertificate(certificate_id_in_report).call()
        stored_id = cert[0]
        stored_user_name = cert[1]
        stored_hash = cert[2]
    except:
        return "Certificate not found on blockchain."

    def normalize(x):
        return re.sub(r"\s+", "", x or "").lower()

    if (
        normalize(certificate_id_in_report) == normalize(stored_id) and
        normalize(user_name_in_report) == normalize(stored_user_name) and
        normalize(certificate_hash_in_report) == normalize(stored_hash)
    ):
        status = "HASH VERIFIED & OWNERSHIP CONFIRMED"
    else:
        status = "HASH NOT VERIFIED / DATA MISMATCH"

    return render_template(
        "hash_result.html",
        certificate_id=certificate_id_in_report,
        user_name=user_name_in_report,
        status=status
    )

# -----------------------------
# CLAIM & OTP ROUTES (UNCHANGED)
# -----------------------------
@app.route("/claim/<certificate_id>")
def claim_certificate(certificate_id):
    session["certificate_id"] = certificate_id
    return render_template("claim.html")

@app.route("/send_otp", methods=["POST"])
def send_otp():
    phone = standardize_phone(request.form.get("phone"))
    session["claim_phone"] = phone

    if len(phone) != 13:
        return "Invalid phone number"

    certificate_id = session.get("certificate_id")
    if not certificate_id:
        return "Session expired"

    otp = str(random.randint(100000, 999999))
    session["otp"] = otp

    twilio_client.messages.create(
        body=f"Your CertiSure OTP is: {otp}",
        from_=TWILIO_PHONE,
        to=phone
    )

    return render_template("verify_otp.html")

@app.route("/verify_otp", methods=["POST"])
def verify_otp():
    if request.form.get("otp") != session.get("otp"):
        return "Invalid OTP"

    certificate_id = session.get("certificate_id")
    phone_entered = session.get("claim_phone")
    entered_phone_hash = hashlib.sha256(phone_entered.encode()).hexdigest()
    cert = contract.functions.getCertificate(certificate_id).call()

    if entered_phone_hash != cert[4]:
        return "Ownership verification failed."

    session.pop("otp", None)
    return redirect("/generate_report")

@app.route("/generate_report")
def generate_report():
    certificate_id = session.get("certificate_id")
    cert = contract.functions.getCertificate(certificate_id).call()

    pdf_path = os.path.join(app.root_path, "static", "ownership_report.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()

    elements.append(Paragraph("<b>Certificate Ownership Verification Report</b>", styles["Title"]))
    elements.append(Spacer(1, 20))

    data = [
        ["Certificate ID", cert[0]],
        ["User Name", cert[1]],
        ["Certificate Hash", cert[2]],
        ["Timestamp", str(cert[5])],
        ["Ownership Status", "CONFIRMED"]
    ]

    table = Table(data, colWidths=[180, 360], repeatRows=1)
    table.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 1, colors.grey)]))
    elements.append(table)
    doc.build(elements)

    return send_file(pdf_path, as_attachment=True)

# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)