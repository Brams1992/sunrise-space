"""
Sunrise Space — backend (Flask + SQLite ou PostgreSQL)
Mise en relation clients <-> chauffeurs de moto-taxi, Côte d'Ivoire.

Lancer :  python app.py      (http://localhost:5000)
"""
import hashlib
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SUNRISE_DB", os.path.join(BASE_DIR, "sunrise.db"))
# Si DATABASE_URL est défini (PostgreSQL, ex. Neon), les données y sont gardées durablement.
# Sinon, on utilise le fichier SQLite ci-dessus (pratique en local).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg
else:
    import sqlite3

# --- Réglages -----------------------------------------------------------------
LOCATION_TTL_MIN = 10        # un chauffeur sans position récente n'apparaît plus
REQUEST_TTL_MIN = 30         # une demande non prise expire après 30 min
TOKEN_TTL_DAYS = 30
NEARBY_RADIUS_KM = 15.0      # rayon par défaut de recherche
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 300

COMMUNES = [
    "Abobo", "Adjamé", "Anyama", "Attécoubé", "Bingerville", "Cocody", "Koumassi",
    "Marcory", "Plateau", "Port-Bouët", "Songon", "Treichville", "Yopougon",
    "Abengourou", "Abidjan (autre)", "Agboville", "Bondoukou", "Bouaflé", "Bouaké",
    "Daloa", "Dimbokro", "Divo", "Ferkessédougou", "Gagnoa", "Grand-Bassam", "Guiglo",
    "Korhogo", "Man", "Odienné", "San-Pédro", "Séguéla", "Soubré", "Yamoussoukro",
    "Autre ville",
]
REPORT_REASONS = {
    "comportement": "Comportement irrespectueux",
    "securite": "Conduite dangereuse / sécurité",
    "fraude": "Fraude / arnaque / faux profil",
    "prix": "Prix abusif",
    "absent": "Ne s'est pas présenté",
    "autre": "Autre",
}

app = Flask(__name__, static_folder=None)  # fichiers servis par la liste blanche ci-dessous
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # corps de requête limité

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL CHECK(role IN ('client','driver','admin')),
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_blocked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drivers(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  plate TEXT NOT NULL,
  moto_model TEXT,
  permis TEXT,
  commune TEXT,
  whatsapp TEXT,
  approval TEXT NOT NULL DEFAULT 'pending' CHECK(approval IN ('pending','approved','rejected')),
  approval_note TEXT,
  availability TEXT NOT NULL DEFAULT 'unavailable' CHECK(availability IN ('available','busy','unavailable')),
  lat REAL, lng REAL, location_at TEXT
);
CREATE TABLE IF NOT EXISTS tokens(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ride_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER NOT NULL REFERENCES users(id),
  driver_id INTEGER REFERENCES users(id),
  lat REAL NOT NULL, lng REAL NOT NULL,
  pickup_text TEXT, destination_text TEXT, note TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','completed','cancelled','expired')),
  created_at TEXT NOT NULL, accepted_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id INTEGER NOT NULL REFERENCES users(id),
  target_user_id INTEGER NOT NULL REFERENCES users(id),
  request_id INTEGER REFERENCES ride_requests(id),
  reason TEXT NOT NULL,
  details TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','dismissed')),
  admin_note TEXT,
  created_at TEXT NOT NULL,
  handled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_drivers_search ON drivers(approval, availability);
CREATE INDEX IF NOT EXISTS idx_req_status ON ride_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_req_client ON ride_requests(client_id, status);
CREATE INDEX IF NOT EXISTS idx_req_driver ON ride_requests(driver_id, status);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
"""


# --- Base de données ----------------------------------------------------------
class Row(dict):
    """Ligne de résultat : accessible par nom (row["id"]) ou par position (row[0])."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in (cursor.description or [])]
    return lambda values: Row(zip(names, values))


def _pg(sql):
    """Adapte une requête écrite pour SQLite à PostgreSQL."""
    return sql.replace("?", "%s").replace(" LIKE ", " ILIKE ")


class DB:
    """Petite couche commune SQLite / PostgreSQL."""

    def __init__(self):
        if USE_PG:
            self.conn = psycopg.connect(DATABASE_URL, row_factory=_row_factory, connect_timeout=20, prepare_threshold=None)
        else:
            self.conn = sqlite3.connect(DB_PATH, timeout=10)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, params=()):
        if USE_PG:
            cur = self.conn.cursor()
            cur.execute(_pg(sql), tuple(params))
            return cur
        return self.conn.execute(sql, tuple(params))

    def insert(self, sql, params=()):
        """Exécute un INSERT dans une table à colonne id et renvoie l'id créé."""
        if USE_PG:
            return self.execute(sql + " RETURNING id", params).fetchone()["id"]
        return self.conn.execute(sql, tuple(params)).lastrowid

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def schema_sql():
    sql = SCHEMA
    if USE_PG:
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
    return [part.strip() for part in sql.split(";") if part.strip()]


def init_db():
    db = DB()
    try:
        if USE_PG:
            db.execute("SELECT pg_advisory_xact_lock(7461)")  # évite les conflits si plusieurs processus démarrent
        else:
            db.conn.execute("PRAGMA journal_mode=WAL")
        for statement in schema_sql():
            db.execute(statement)
        db.commit()
        ensure_admin(db)
    finally:
        db.close()


def ensure_admin(db):
    """Crée le compte administrateur au premier lancement."""
    existing = db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if existing:
        # Réinitialisation volontaire : RESET_ADMIN_PASSWORD=oui + ADMIN_PASSWORD (+ ADMIN_PHONE)
        wanted = os.environ.get("ADMIN_PASSWORD")
        if os.environ.get("RESET_ADMIN_PASSWORD", "").lower() in ("oui", "yes", "1", "true") and wanted:
            new_phone = normalize_phone(os.environ.get("ADMIN_PHONE", "")) 
            db.execute("UPDATE users SET password_hash=?, is_blocked=0 WHERE id=?",
                       (generate_password_hash(wanted), existing[0]))
            if new_phone:
                taken = db.execute("SELECT 1 FROM users WHERE phone=? AND id!=?", (new_phone, existing[0])).fetchone()
                if not taken:
                    db.execute("UPDATE users SET phone=? WHERE id=?", (new_phone, existing[0]))
            db.execute("DELETE FROM tokens WHERE user_id=?", (existing[0],))
            db.commit()
            print("Mot de passe admin réinitialisé. Retirez maintenant RESET_ADMIN_PASSWORD.")
        return
    phone = normalize_phone(os.environ.get("ADMIN_PHONE", "0700000000")) or "0700000000"
    password = os.environ.get("ADMIN_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True
    db.execute(
        "INSERT INTO users(role,name,phone,password_hash,created_at) VALUES('admin','Administrateur',?,?,?)",
        (phone, generate_password_hash(password), ts()),
    )
    db.commit()
    print("=" * 60)
    print("COMPTE ADMIN CRÉÉ")
    print(f"  Téléphone    : {phone}")
    if generated:
        print(f"  Mot de passe : {password}   (notez-le, il ne sera plus affiché)")
    else:
        print("  Mot de passe : celui défini dans ADMIN_PASSWORD")
    print("=" * 60)


# --- Utilitaires --------------------------------------------------------------
def now():
    return datetime.now(timezone.utc)


def ts(dt=None):
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def minutes_ago(s):
    d = parse_ts(s)
    if not d:
        return None
    return max(0, int((now() - d).total_seconds() // 60))


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize_phone(raw):
    """Numéro ivoirien à 10 chiffres (ex. 0707070707). Accepte +225 / 00225."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00225"):
        digits = digits[5:]
    elif digits.startswith("225") and len(digits) == 13:
        digits = digits[3:]
    return digits if len(digits) == 10 else None


def clean(value, max_len):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:max_len]


def get_coords(data):
    try:
        lat, lng = float(data.get("lat")), float(data.get("lng"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or math.isnan(lat) or math.isnan(lng):
        return None
    return lat, lng


def err(message, status=400):
    return jsonify({"error": message}), status


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db, user_id):
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO tokens(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (hash_token(token), user_id, ts(), ts(now() + timedelta(days=TOKEN_TTL_DAYS))),
    )
    db.execute("DELETE FROM tokens WHERE expires_at < ?", (ts(),))
    db.commit()
    return token


def auth_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return err("Connexion requise.", 401)
            token = header[7:].strip()
            db = get_db()
            row = db.execute(
                """SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id
                   WHERE t.token_hash=? AND t.expires_at>?""",
                (hash_token(token), ts()),
            ).fetchone()
            if not row:
                return err("Session expirée. Reconnectez-vous.", 401)
            if row["is_blocked"]:
                return err("Ce compte est suspendu. Contactez le support.", 403)
            if roles and row["role"] not in roles:
                return err("Accès refusé.", 403)
            g.user = row
            g.token_hash = hash_token(token)
            return fn(*a, **kw)
        return wrapper
    return deco


_login_fails = {}


def login_locked(key):
    rec = _login_fails.get(key)
    if rec and rec[0] >= MAX_LOGIN_FAILS and time.time() - rec[1] < LOGIN_LOCK_SECONDS:
        return True
    if rec and time.time() - rec[1] >= LOGIN_LOCK_SECONDS:
        _login_fails.pop(key, None)
    return False


def login_failed(key):
    count = _login_fails.get(key, (0, 0))[0] + 1
    _login_fails[key] = (count, time.time())


def expire_requests(db):
    cutoff = ts(now() - timedelta(minutes=REQUEST_TTL_MIN))
    db.execute(
        "UPDATE ride_requests SET status='expired', closed_at=? WHERE status='open' AND created_at<?",
        (ts(), cutoff),
    )
    db.commit()


def user_public(u):
    return {"id": u["id"], "role": u["role"], "name": u["name"], "phone": u["phone"]}


def driver_profile(db, user_id):
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (user_id,)).fetchone()
    if not d:
        return None
    return {
        "plate": d["plate"], "moto_model": d["moto_model"], "permis": d["permis"],
        "commune": d["commune"], "whatsapp": d["whatsapp"],
        "approval": d["approval"], "approval_note": d["approval_note"],
        "availability": d["availability"],
        "lat": d["lat"], "lng": d["lng"], "location_age_min": minutes_ago(d["location_at"]),
    }


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self)"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: blob: https://unpkg.com https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://router.project-osrm.org; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path in ("/sw.js", "/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- Pages / méta -------------------------------------------------------------
PUBLIC_FILES = {
    "index.html", "app.js", "style.css", "sw.js", "manifest.webmanifest",
    "icon.svg", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
}


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<name>")
def public_file(name):
    """Sert uniquement les fichiers de l'interface (jamais app.py ni la base de données)."""
    if name not in PUBLIC_FILES:
        return not_found(None)
    return send_from_directory(BASE_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "sunrise-space"})


@app.get("/api/meta")
def meta():
    return jsonify({"communes": COMMUNES, "report_reasons": REPORT_REASONS,
                    "default_center": {"lat": 5.3600, "lng": -4.0083}})


# --- Authentification ---------------------------------------------------------
@app.post("/api/auth/register")
def register():
    data = body()
    role = data.get("role")
    if role not in ("client", "driver"):
        return err("Choisissez un profil : client ou chauffeur.")
    name = clean(data.get("name"), 60)
    phone = normalize_phone(data.get("phone"))
    password = str(data.get("password") or "")
    if len(name) < 2:
        return err("Entrez votre nom complet.")
    if not phone:
        return err("Numéro invalide. Entrez les 10 chiffres (ex. 07 07 07 07 07).")
    if len(password) < 6 or len(password) > 100:
        return err("Le mot de passe doit contenir au moins 6 caractères.")

    driver = {}
    if role == "driver":
        d = data.get("driver") if isinstance(data.get("driver"), dict) else {}
        plate = clean(d.get("plate"), 20).upper()
        permis = clean(d.get("permis"), 30)
        commune = clean(d.get("commune"), 40)
        whatsapp = normalize_phone(d.get("whatsapp")) if d.get("whatsapp") else phone
        if len(plate) < 3:
            return err("Entrez la plaque d'immatriculation de la moto.")
        if len(permis) < 3:
            return err("Entrez votre numéro de permis de conduire.")
        if commune not in COMMUNES:
            return err("Choisissez votre commune / ville.")
        if not whatsapp:
            return err("Numéro WhatsApp invalide.")
        driver = {"plate": plate, "permis": permis, "commune": commune, "whatsapp": whatsapp,
                  "moto_model": clean(d.get("moto_model"), 40)}

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
        return err("Ce numéro a déjà un compte. Connectez-vous.", 409)
    uid = db.insert(
        "INSERT INTO users(role,name,phone,password_hash,created_at) VALUES(?,?,?,?,?)",
        (role, name, phone, generate_password_hash(password), ts()),
    )
    if role == "driver":
        db.execute(
            "INSERT INTO drivers(user_id,plate,moto_model,permis,commune,whatsapp) VALUES(?,?,?,?,?,?)",
            (uid, driver["plate"], driver["moto_model"], driver["permis"], driver["commune"], driver["whatsapp"]),
        )
    db.commit()
    token = issue_token(db, uid)
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return jsonify({"token": token, "user": user_public(user), "driver": driver_profile(db, uid)}), 201


@app.post("/api/auth/login")
def login():
    data = body()
    phone = normalize_phone(data.get("phone"))
    password = str(data.get("password") or "")
    if not phone:
        return err("Numéro invalide. Entrez les 10 chiffres.")
    key = (phone, request.remote_addr)
    if login_locked(key):
        return err("Trop de tentatives. Réessayez dans quelques minutes.", 429)
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        login_failed(key)
        return err("Numéro ou mot de passe incorrect.", 401)
    if user["is_blocked"]:
        return err("Ce compte est suspendu. Contactez le support.", 403)
    _login_fails.pop(key, None)
    token = issue_token(db, user["id"])
    return jsonify({"token": token, "user": user_public(user), "driver": driver_profile(db, user["id"])})


@app.post("/api/auth/logout")
@auth_required()
def logout():
    db = get_db()
    db.execute("DELETE FROM tokens WHERE token_hash=?", (g.token_hash,))
    if g.user["role"] == "driver":  # un chauffeur déconnecté ne reste pas « disponible »
        db.execute("UPDATE drivers SET availability='unavailable' WHERE user_id=? AND availability='available'",
                   (g.user["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/me")
@auth_required()
def me():
    return jsonify({"user": user_public(g.user), "driver": driver_profile(get_db(), g.user["id"])})


# --- Client : chauffeurs proches ---------------------------------------------
@app.get("/api/drivers/nearby")
@auth_required("client", "admin")
def drivers_nearby():
    coords = get_coords(request.args)
    if not coords:
        return err("Position invalide.")
    try:
        radius = min(max(float(request.args.get("radius_km", NEARBY_RADIUS_KM)), 0.5), 100.0)
    except ValueError:
        radius = NEARBY_RADIUS_KM
    lat, lng = coords
    cutoff = ts(now() - timedelta(minutes=LOCATION_TTL_MIN))
    rows = get_db().execute(
        """SELECT u.id, u.name, u.phone, d.plate, d.moto_model, d.commune, d.whatsapp,
                  d.lat, d.lng, d.location_at
           FROM drivers d JOIN users u ON u.id=d.user_id
           WHERE d.approval='approved' AND d.availability='available' AND u.is_blocked=0
                 AND d.lat IS NOT NULL AND d.location_at>=?""",
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        dist = haversine_km(lat, lng, r["lat"], r["lng"])
        if dist <= radius:
            out.append({
                "id": r["id"], "name": r["name"], "phone": r["phone"], "whatsapp": r["whatsapp"],
                "plate": r["plate"], "moto_model": r["moto_model"], "commune": r["commune"],
                "lat": r["lat"], "lng": r["lng"], "distance_km": round(dist, 2),
                "updated_min_ago": minutes_ago(r["location_at"]),
            })
    out.sort(key=lambda x: x["distance_km"])
    return jsonify({"drivers": out[:50], "radius_km": radius})


# --- Chauffeur : statut, position, tableau de bord ---------------------------
@app.put("/api/driver/status")
@auth_required("driver")
def driver_status():
    wanted = body().get("availability")
    if wanted not in ("available", "unavailable"):
        return err("Statut invalide.")
    db = get_db()
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (g.user["id"],)).fetchone()
    if d["approval"] != "approved":
        return err("Votre profil doit d'abord être validé par l'équipe Sunrise Space.", 403)
    if d["availability"] == "busy":
        return err("Vous avez une course en cours. Terminez-la d'abord.", 409)
    if wanted == "available" and d["lat"] is None:
        return err("Activez votre position pour devenir disponible.", 409)
    db.execute("UPDATE drivers SET availability=? WHERE user_id=?", (wanted, g.user["id"]))
    db.commit()
    return jsonify({"driver": driver_profile(db, g.user["id"])})


@app.post("/api/driver/location")
@auth_required("driver")
def driver_location():
    coords = get_coords(body())
    if not coords:
        return err("Position invalide.")
    db = get_db()
    db.execute("UPDATE drivers SET lat=?, lng=?, location_at=? WHERE user_id=?",
               (coords[0], coords[1], ts(), g.user["id"]))
    db.commit()
    return jsonify({"ok": True})


def serialize_request_for_driver(r, dlat, dlng, reveal):
    out = {
        "id": r["id"], "pickup_text": r["pickup_text"], "destination_text": r["destination_text"],
        "note": r["note"], "lat": r["lat"], "lng": r["lng"], "status": r["status"],
        "client_name": r["client_name"], "age_min": minutes_ago(r["created_at"]),
    }
    if dlat is not None:
        out["distance_km"] = round(haversine_km(dlat, dlng, r["lat"], r["lng"]), 2)
    if reveal:
        out["client_phone"] = r["client_phone"]
        out["client_id"] = r["client_id"]
    return out


@app.get("/api/driver/dashboard")
@auth_required("driver")
def driver_dashboard():
    db = get_db()
    expire_requests(db)
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (g.user["id"],)).fetchone()
    profile = driver_profile(db, g.user["id"])
    job, requests = None, []
    row = db.execute(
        """SELECT r.*, u.name AS client_name, u.phone AS client_phone
           FROM ride_requests r JOIN users u ON u.id=r.client_id
           WHERE r.driver_id=? AND r.status='accepted' ORDER BY r.id DESC LIMIT 1""",
        (g.user["id"],),
    ).fetchone()
    if row:
        job = serialize_request_for_driver(row, d["lat"], d["lng"], True)
    elif d["approval"] == "approved" and d["lat"] is not None and d["availability"] == "available":
        rows = db.execute(
            """SELECT r.*, u.name AS client_name, u.phone AS client_phone
               FROM ride_requests r JOIN users u ON u.id=r.client_id
               WHERE r.status='open' ORDER BY r.id DESC LIMIT 200"""
        ).fetchall()
        for r in rows:
            item = serialize_request_for_driver(r, d["lat"], d["lng"], False)
            if item["distance_km"] <= NEARBY_RADIUS_KM:
                requests.append(item)
        requests.sort(key=lambda x: x["distance_km"])
        requests = requests[:30]
    return jsonify({"driver": profile, "job": job, "requests": requests})


# --- Demandes de moto ---------------------------------------------------------
def serialize_request_for_client(db, r):
    out = {
        "id": r["id"], "status": r["status"], "pickup_text": r["pickup_text"],
        "destination_text": r["destination_text"], "note": r["note"],
        "lat": r["lat"], "lng": r["lng"], "age_min": minutes_ago(r["created_at"]),
        "driver": None,
    }
    if r["driver_id"] and r["status"] in ("accepted", "completed"):
        d = db.execute(
            """SELECT u.id, u.name, u.phone, d.whatsapp, d.plate, d.moto_model, d.lat, d.lng, d.location_at
               FROM users u JOIN drivers d ON d.user_id=u.id WHERE u.id=?""",
            (r["driver_id"],),
        ).fetchone()
        if d:
            out["driver"] = {
                "id": d["id"], "name": d["name"], "phone": d["phone"], "whatsapp": d["whatsapp"],
                "plate": d["plate"], "moto_model": d["moto_model"],
                "lat": d["lat"] if r["status"] == "accepted" else None,
                "lng": d["lng"] if r["status"] == "accepted" else None,
            }
    return out


@app.post("/api/requests")
@auth_required("client")
def create_request():
    data = body()
    coords = get_coords(data)
    if not coords:
        return err("Position de prise en charge invalide.")
    db = get_db()
    expire_requests(db)
    active = db.execute(
        "SELECT 1 FROM ride_requests WHERE client_id=? AND status IN ('open','accepted')", (g.user["id"],)
    ).fetchone()
    if active:
        return err("Vous avez déjà une demande en cours.", 409)
    new_id = db.insert(
        """INSERT INTO ride_requests(client_id,lat,lng,pickup_text,destination_text,note,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (g.user["id"], coords[0], coords[1], clean(data.get("pickup_text"), 120),
         clean(data.get("destination_text"), 120), clean(data.get("note"), 200), ts()),
    )
    db.commit()
    r = db.execute("SELECT * FROM ride_requests WHERE id=?", (new_id,)).fetchone()
    return jsonify({"request": serialize_request_for_client(db, r)}), 201


@app.get("/api/requests/current")
@auth_required("client")
def current_request():
    db = get_db()
    expire_requests(db)
    r = db.execute(
        """SELECT * FROM ride_requests WHERE client_id=?
           AND (status IN ('open','accepted') OR (status IN ('completed','expired','cancelled') AND closed_at>?))
           ORDER BY id DESC LIMIT 1""",
        (g.user["id"], ts(now() - timedelta(minutes=2))),
    ).fetchone()
    return jsonify({"request": serialize_request_for_client(db, r) if r else None})


@app.get("/api/requests/history")
@auth_required("client")
def request_history():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM ride_requests WHERE client_id=? ORDER BY id DESC LIMIT 20", (g.user["id"],)
    ).fetchall()
    return jsonify({"requests": [serialize_request_for_client(db, r) for r in rows]})


@app.post("/api/requests/<int:rid>/location")
@auth_required("client")
def update_request_location(rid):
    """Le client envoie sa position à jour tant que sa demande est ouverte ou acceptée."""
    coords = get_coords(body())
    if not coords:
        return err("Position invalide.")
    db = get_db()
    r = db.execute("SELECT status FROM ride_requests WHERE id=? AND client_id=?", (rid, g.user["id"])).fetchone()
    if not r:
        return err("Demande introuvable.", 404)
    if r["status"] not in ("open", "accepted"):
        return err("Cette demande est terminée.", 409)
    db.execute("UPDATE ride_requests SET lat=?, lng=? WHERE id=?", (coords[0], coords[1], rid))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/cancel")
@auth_required("client")
def cancel_request(rid):
    db = get_db()
    r = db.execute("SELECT * FROM ride_requests WHERE id=? AND client_id=?", (rid, g.user["id"])).fetchone()
    if not r:
        return err("Demande introuvable.", 404)
    if r["status"] not in ("open", "accepted"):
        return err("Cette demande est déjà terminée.", 409)
    db.execute("UPDATE ride_requests SET status='cancelled', closed_at=? WHERE id=?", (ts(), rid))
    if r["driver_id"]:
        db.execute("UPDATE drivers SET availability='available' WHERE user_id=? AND availability='busy'",
                   (r["driver_id"],))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/accept")
@auth_required("driver")
def accept_request(rid):
    db = get_db()
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (g.user["id"],)).fetchone()
    if d["approval"] != "approved":
        return err("Votre profil n'est pas encore validé.", 403)
    if d["availability"] != "available":
        return err("Passez en « Disponible » pour accepter une demande.", 409)
    expire_requests(db)
    cur = db.execute(
        "UPDATE ride_requests SET status='accepted', driver_id=?, accepted_at=? WHERE id=? AND status='open'",
        (g.user["id"], ts(), rid),
    )
    if cur.rowcount == 0:
        db.rollback()
        return err("Cette demande a déjà été prise ou a expiré.", 409)
    db.execute("UPDATE drivers SET availability='busy' WHERE user_id=?", (g.user["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/release")
@auth_required("driver")
def release_request(rid):
    """Le chauffeur abandonne la course : la demande redevient ouverte."""
    db = get_db()
    cur = db.execute(
        "UPDATE ride_requests SET status='open', driver_id=NULL, accepted_at=NULL "
        "WHERE id=? AND driver_id=? AND status='accepted'",
        (rid, g.user["id"]),
    )
    if cur.rowcount == 0:
        db.rollback()
        return err("Course introuvable.", 404)
    db.execute("UPDATE drivers SET availability='available' WHERE user_id=?", (g.user["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/complete")
@auth_required("driver", "client")
def complete_request(rid):
    db = get_db()
    col = "driver_id" if g.user["role"] == "driver" else "client_id"
    r = db.execute(f"SELECT * FROM ride_requests WHERE id=? AND {col}=?", (rid, g.user["id"])).fetchone()
    if not r:
        return err("Course introuvable.", 404)
    if r["status"] != "accepted":
        return err("Cette course n'est pas en cours.", 409)
    db.execute("UPDATE ride_requests SET status='completed', closed_at=? WHERE id=?", (ts(), rid))
    db.execute("UPDATE drivers SET availability='available' WHERE user_id=? AND availability='busy'",
               (r["driver_id"],))
    db.commit()
    return jsonify({"ok": True})


# --- Signalements -------------------------------------------------------------
@app.post("/api/reports")
@auth_required("client", "driver")
def create_report():
    data = body()
    try:
        target = int(data.get("target_user_id"))
    except (TypeError, ValueError):
        return err("Personne à signaler introuvable.")
    reason = data.get("reason")
    if reason not in REPORT_REASONS:
        return err("Choisissez un motif.")
    if target == g.user["id"]:
        return err("Vous ne pouvez pas vous signaler vous-même.")
    db = get_db()
    t = db.execute("SELECT id, role FROM users WHERE id=?", (target,)).fetchone()
    if not t or t["role"] == "admin":
        return err("Personne à signaler introuvable.", 404)
    recent = db.execute(
        "SELECT COUNT(*) c FROM reports WHERE reporter_id=? AND created_at>?",
        (g.user["id"], ts(now() - timedelta(days=1))),
    ).fetchone()["c"]
    if recent >= 10:
        return err("Limite de signalements atteinte pour aujourd'hui.", 429)
    request_id = data.get("request_id")
    try:
        request_id = int(request_id) if request_id else None
    except (TypeError, ValueError):
        request_id = None
    db.execute(
        "INSERT INTO reports(reporter_id,target_user_id,request_id,reason,details,created_at) VALUES(?,?,?,?,?,?)",
        (g.user["id"], target, request_id, reason, clean(data.get("details"), 500), ts()),
    )
    db.commit()
    return jsonify({"ok": True}), 201


# --- Administration -----------------------------------------------------------
@app.get("/api/admin/stats")
@auth_required("admin")
def admin_stats():
    db = get_db()
    expire_requests(db)
    one = lambda q, p=(): db.execute(q, p).fetchone()[0]
    fresh = ts(now() - timedelta(minutes=LOCATION_TTL_MIN))
    today = ts(now().replace(hour=0, minute=0, second=0))
    return jsonify({
        "clients": one("SELECT COUNT(*) FROM users WHERE role='client'"),
        "drivers": one("SELECT COUNT(*) FROM users WHERE role='driver'"),
        "drivers_pending": one("SELECT COUNT(*) FROM drivers WHERE approval='pending'"),
        "drivers_approved": one("SELECT COUNT(*) FROM drivers WHERE approval='approved'"),
        "drivers_available_now": one(
            "SELECT COUNT(*) FROM drivers WHERE approval='approved' AND availability='available' AND location_at>=?",
            (fresh,)),
        "requests_open": one("SELECT COUNT(*) FROM ride_requests WHERE status='open'"),
        "requests_today": one("SELECT COUNT(*) FROM ride_requests WHERE created_at>=?", (today,)),
        "rides_completed": one("SELECT COUNT(*) FROM ride_requests WHERE status='completed'"),
        "reports_open": one("SELECT COUNT(*) FROM reports WHERE status='open'"),
    })


@app.get("/api/admin/drivers")
@auth_required("admin")
def admin_drivers():
    status = request.args.get("approval", "pending")
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    q = """SELECT u.id, u.name, u.phone, u.is_blocked, u.created_at, d.plate, d.moto_model, d.permis,
                  d.commune, d.whatsapp, d.approval, d.approval_note, d.availability
           FROM drivers d JOIN users u ON u.id=d.user_id"""
    params = ()
    if status != "all":
        q += " WHERE d.approval=?"
        params = (status,)
    q += " ORDER BY u.id DESC LIMIT 200"
    return jsonify({"drivers": [dict(r) for r in get_db().execute(q, params).fetchall()]})


@app.post("/api/admin/drivers/<int:uid>/approval")
@auth_required("admin")
def admin_driver_approval(uid):
    data = body()
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        return err("Décision invalide.")
    db = get_db()
    cur = db.execute("UPDATE drivers SET approval=?, approval_note=?, availability='unavailable' WHERE user_id=?",
                     (decision, clean(data.get("note"), 200), uid))
    if cur.rowcount == 0:
        return err("Chauffeur introuvable.", 404)
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/users")
@auth_required("admin")
def admin_users():
    q = clean(request.args.get("q"), 40)
    role = request.args.get("role")
    sql, params = "SELECT id, role, name, phone, is_blocked, created_at FROM users WHERE role!='admin'", []
    if role in ("client", "driver"):
        sql += " AND role=?"
        params.append(role)
    if q:
        digits = re.sub(r"\D", "", q)
        sql += " AND (name LIKE ? OR phone LIKE ?)"
        params += [f"%{q}%", f"%{digits or q}%"]
    sql += " ORDER BY id DESC LIMIT 100"
    return jsonify({"users": [dict(r) for r in get_db().execute(sql, params).fetchall()]})


@app.post("/api/admin/users/<int:uid>/block")
@auth_required("admin")
def admin_block(uid):
    blocked = 1 if body().get("blocked") else 0
    db = get_db()
    u = db.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["role"] == "admin":
        return err("Utilisateur introuvable.", 404)
    db.execute("UPDATE users SET is_blocked=? WHERE id=?", (blocked, uid))
    if blocked:
        db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
        db.execute("UPDATE drivers SET availability='unavailable' WHERE user_id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/reports")
@auth_required("admin")
def admin_reports():
    status = request.args.get("status", "open")
    if status not in ("open", "resolved", "dismissed", "all"):
        status = "open"
    q = """SELECT r.id, r.reason, r.details, r.status, r.admin_note, r.created_at, r.request_id,
                  a.id AS reporter_id, a.name AS reporter_name, a.phone AS reporter_phone, a.role AS reporter_role,
                  t.id AS target_id, t.name AS target_name, t.phone AS target_phone, t.role AS target_role,
                  t.is_blocked AS target_blocked
           FROM reports r JOIN users a ON a.id=r.reporter_id JOIN users t ON t.id=r.target_user_id"""
    params = ()
    if status != "all":
        q += " WHERE r.status=?"
        params = (status,)
    q += " ORDER BY r.id DESC LIMIT 200"
    rows = [dict(r) for r in get_db().execute(q, params).fetchall()]
    for r in rows:
        r["reason_label"] = REPORT_REASONS.get(r["reason"], r["reason"])
    return jsonify({"reports": rows})


@app.post("/api/admin/reports/<int:rid>")
@auth_required("admin")
def admin_report_handle(rid):
    data = body()
    status = data.get("status")
    if status not in ("resolved", "dismissed"):
        return err("Statut invalide.")
    db = get_db()
    r = db.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        return err("Signalement introuvable.", 404)
    db.execute("UPDATE reports SET status=?, admin_note=?, handled_at=? WHERE id=?",
               (status, clean(data.get("note"), 300), ts(), rid))
    if data.get("block_target"):
        db.execute("UPDATE users SET is_blocked=1 WHERE id=? AND role!='admin'", (r["target_user_id"],))
        db.execute("DELETE FROM tokens WHERE user_id=?", (r["target_user_id"],))
        db.execute("UPDATE drivers SET availability='unavailable' WHERE user_id=?", (r["target_user_id"],))
    db.commit()
    return jsonify({"ok": True})


# --- Erreurs JSON -------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return err("Ressource introuvable.", 404)
    return send_from_directory(BASE_DIR, "index.html")


@app.errorhandler(413)
def too_large(_e):
    return err("Requête trop volumineuse.", 413)


@app.errorhandler(405)
def bad_method(_e):
    return err("Méthode non autorisée.", 405)


@app.errorhandler(500)
def server_error(_e):
    return err("Erreur du serveur. Réessayez.", 500)


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
