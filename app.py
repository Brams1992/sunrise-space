"""
Sunrise Space — backend (Flask + SQLite ou PostgreSQL)
Mise en relation clients <-> chauffeurs de moto-taxi, Côte d'Ivoire.

Lancer :  python app.py      (http://localhost:5000)
"""
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import base64
import zlib
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlencode
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Request, Response, g, jsonify, redirect, request, send_from_directory
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
PUSH_DRIVER_MAX_AGE_H = 2      # on prévient un chauffeur « disponible » même app fermée, si sa dernière position a moins de 2 h
GHOST_AFTER_H = 12            # « Disponible » sans position depuis 12 h : repassé automatiquement en indisponible
MAX_TRIP_KM = 300               # destination à plus de 300 km du départ : refusée
DRIVER_AWAY_MIN = 120          # un chauffeur « Disponible » qui quitte l'application reste « en ligne » 2 h dans l'espace admin
ONLINE_WINDOW_MIN = 3          # « en ligne » = application ouverte à l'écran dans les 3 dernières minutes
NUDGE_STALE_MIN = 2           # chauffeur en course sans position depuis 2 min : on lui envoie un rappel
NUDGE_EVERY_MIN = 4           # au plus un rappel toutes les 4 min
CHAIN_RADIUS_KM = 3         # un chauffeur en course peut accepter une demande à moins de 3 km de la destination de son client
CHAIN_MAX_QUEUE = 3         # nombre maximum de courses en attente derrière la course en cours
FILE_MAX_BYTES = 12 * 1024 * 1024   # taille maximale d'un fichier envoyé par l'admin
FILE_KEEP_DAYS = 7                  # un fichier non téléchargé est effacé du serveur après 7 jours
LOCATION_TTL_MIN = 10        # un chauffeur sans position récente n'apparaît plus
REQUEST_TTL_MIN = 30         # une demande non prise expire après 30 min
TOKEN_TTL_DAYS = 7
VEHICLE_TYPES = {"moto": "Moto-taxi", "voiture": "Taxi (voiture)", "tricycle": "Tricycle"}
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

RESTORE_PATH = "/api/admin/restore"
RESTORE_MAX_BYTES = 25 * 1024 * 1024   # taille maximale d'un fichier de sauvegarde envoyé pour restauration


class AppRequest(Request):
    """Corps limité à 64 Ko partout, sauf pour l'envoi d'une sauvegarde (réservé à l'admin)."""

    @property
    def max_content_length(self):
        return RESTORE_MAX_BYTES if self.path == RESTORE_PATH else 64 * 1024

    @max_content_length.setter
    def max_content_length(self, value):
        pass


app = Flask(__name__, static_folder=None)  # fichiers servis par la liste blanche ci-dessous
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # derrière Render : vraie adresse IP du visiteur, https
app.request_class = AppRequest
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # corps de requête limité

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL CHECK(role IN ('client','driver','admin')),
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  is_blocked INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  staff_scope TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drivers(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  plate TEXT NOT NULL,
  vehicle_type TEXT NOT NULL DEFAULT 'moto',
  moto_model TEXT,
  permis TEXT,
  commune TEXT,
  whatsapp TEXT,
  approval TEXT NOT NULL DEFAULT 'pending' CHECK(approval IN ('pending','approved','rejected')),
  approval_note TEXT,
  availability TEXT NOT NULL DEFAULT 'unavailable' CHECK(availability IN ('available','busy','unavailable')),
  lat REAL, lng REAL, location_at TEXT
);
CREATE TABLE IF NOT EXISTS user_positions(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  lat REAL NOT NULL, lng REAL NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','dismissed')),
  created_at TEXT NOT NULL,
  handled_at TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_copies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
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
  vehicle_type TEXT NOT NULL DEFAULT 'any',
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','accepted','completed','cancelled','expired')),
  created_at TEXT NOT NULL, accepted_at TEXT, closed_at TEXT,
  rating INTEGER, rating_comment TEXT,
  dest_lat REAL, dest_lng REAL
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
CREATE TABLE IF NOT EXISTS announcements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  audience TEXT NOT NULL DEFAULT 'all' CHECK(audience IN ('all','client','driver')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_reads(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, announcement_id)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  sender TEXT NOT NULL CHECK(sender IN ('user','admin')),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER REFERENCES ride_requests(id) ON DELETE CASCADE,
  caller_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  callee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'ringing',
  created_at TEXT NOT NULL,
  answered_at TEXT,
  ended_at TEXT
);
CREATE TABLE IF NOT EXISTS call_signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  sender_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_id, status);
CREATE INDEX IF NOT EXISTS idx_signals_call ON call_signals(call_id, id);
CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS message_files(
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, mime TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL,
  data BLOB, state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
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

    def executemany(self, sql, rows):
        rows = [tuple(r) for r in rows]
        if USE_PG:
            cur = self.conn.cursor()
            cur.executemany(_pg(sql), rows)
            return cur
        return self.conn.executemany(sql, rows)

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
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION").replace(" BLOB", " BYTEA")
    return [part.strip() for part in sql.split(";") if part.strip()]


def ensure_column(db, table, column, ddl):
    """Ajoute une colonne aux bases créées avant cette version (sans toucher aux données existantes)."""
    def exists():
        try:
            db.execute(f"SELECT {column} FROM {table} LIMIT 0")
            return True
        except Exception:
            return False
        finally:
            db.rollback()
    if exists():
        return
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        db.commit()
    except Exception:
        db.rollback()
        if not exists():   # un autre processus l'a peut-être ajoutée entre-temps
            raise


def init_db(create_admin=True):
    db = DB()
    try:
        if USE_PG:
            db.execute("SELECT pg_advisory_xact_lock(7461)")  # évite les conflits si plusieurs processus démarrent
        else:
            db.conn.execute("PRAGMA journal_mode=WAL")
        for statement in schema_sql():
            db.execute(statement)
        db.commit()
        ensure_column(db, "drivers", "vehicle_type", "TEXT NOT NULL DEFAULT 'moto'")
        ensure_column(db, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "users", "staff_scope", "TEXT")
        ensure_column(db, "users", "last_seen_at", "TEXT")
        ensure_column(db, "login_history", "ip", "TEXT")
        ensure_column(db, "login_history", "device", "TEXT")
        ensure_column(db, "login_history", "kind", "TEXT NOT NULL DEFAULT 'login'")
        ensure_column(db, "ride_requests", "vehicle_type", "TEXT NOT NULL DEFAULT 'any'")
        ensure_column(db, "ride_requests", "rating", "INTEGER")
        ensure_column(db, "ride_requests", "rating_comment", "TEXT")
        ensure_column(db, "ride_requests", "dest_lat", "REAL")
        ensure_column(db, "ride_requests", "dest_lng", "REAL")
        if get_setting(db, "calls_v2") != "1":
            # les appels sont éphémères : on recrée leurs tables pour que request_id soit facultatif (appels de l'admin)
            db.execute("DROP TABLE IF EXISTS call_signals")
            db.execute("DROP TABLE IF EXISTS calls")
            for statement in schema_sql():
                if "calls" in statement or "call_signals" in statement:
                    db.execute(statement)
            set_setting(db, "calls_v2", "1")
            db.commit()
        if create_admin:
            ensure_admin(db)
    finally:
        db.close()


def ensure_admin(db):
    """Crée le compte administrateur au premier lancement."""
    existing = db.execute("SELECT id FROM users WHERE role='admin' AND staff_scope IS NULL LIMIT 1").fetchone()
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


def clean_text(value, max_len):
    """Texte multi-lignes (messages) : garde les retours à la ligne, limite les lignes vides."""
    t = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:max_len]


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


# Un « validateur » est un compte admin à accès réduit (staff_scope='validator') : il ne peut appeler QUE ces pages.
VALIDATOR_ENDPOINTS = {"admin_drivers", "admin_driver_approval", "admin_users", "me", "logout", "change_password", "push_key", "push_subscribe", "push_unsubscribe"}


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
            if row["must_change_password"] and request.endpoint not in ("change_password", "logout", "me"):
                return err("Choisissez d'abord un nouveau mot de passe.", 403)
            if row["role"] == "admin" and row["staff_scope"] == "validator" and request.endpoint not in VALIDATOR_ENDPOINTS:
                return err("Accès refusé.", 403)
            if not row["last_seen_at"] or row["last_seen_at"] < ts(now() - timedelta(seconds=60)):
                db.execute("UPDATE users SET last_seen_at=? WHERE id=?", (ts(), row["id"]))   # au plus une écriture par minute
                db.commit()
            g.user = row
            g.token_hash = hash_token(token)
            return fn(*a, **kw)
        return wrapper
    return deco


_login_fails = {}


def client_ip():
    """Adresse de la personne (derrière un hébergeur, elle est dans X-Forwarded-For)."""
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else "") or request.remote_addr or ""


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
    return {"id": u["id"], "role": u["role"], "name": u["name"], "phone": u["phone"],
            "must_change_password": bool(u["must_change_password"]), "scope": u["staff_scope"]}


def driver_stats(db, user_id):
    row = db.execute("SELECT COUNT(rating), AVG(rating) FROM ride_requests WHERE driver_id=? AND rating IS NOT NULL", (user_id,)).fetchone()
    done = db.execute("SELECT COUNT(*) FROM ride_requests WHERE driver_id=? AND status='completed'", (user_id,)).fetchone()[0]
    return {"rating_count": row[0], "rating_avg": round(float(row[1]), 1) if row[1] is not None else None, "rides_done": done}


_last_sweep = [0.0]


def sweep_ghost_drivers(db):
    """Un chauffeur « Disponible » dont l'application est fermée depuis longtemps ne doit pas le rester."""
    if time.time() - _last_sweep[0] < 60:
        return
    _last_sweep[0] = time.time()
    db.execute("UPDATE message_files SET data=NULL, state='expired' WHERE state='pending' AND created_at<?",
               (ts(now() - timedelta(days=FILE_KEEP_DAYS)),))
    db.commit()
    limit = ts(now() - timedelta(hours=GHOST_AFTER_H))
    rows = db.execute("SELECT user_id FROM drivers WHERE availability='available' AND (location_at IS NULL OR location_at<?)", (limit,)).fetchall()
    if not rows:
        return
    ids = [r["user_id"] for r in rows]
    for uid in ids:
        db.execute("UPDATE drivers SET availability='unavailable' WHERE user_id=? AND availability='available'", (uid,))
    db.commit()
    notify_users(db, ids, "Vous êtes passé indisponible", "Sans position depuis 12 h. Rouvrez Sunrise Space et repassez « Disponible » pour recevoir des demandes.", tag="ghost")


def driver_profile(db, user_id):
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (user_id,)).fetchone()
    if not d:
        return None
    return {
        **driver_stats(db, user_id),
        "plate": d["plate"], "vehicle_type": d["vehicle_type"], "moto_model": d["moto_model"], "permis": d["permis"],
        "commune": d["commune"], "whatsapp": d["whatsapp"],
        "approval": d["approval"], "approval_note": d["approval_note"],
        "availability": d["availability"],
        "lat": d["lat"], "lng": d["lng"], "location_age_min": minutes_ago(d["location_at"]),
    }


@app.before_request
def force_https():
    """En production (derrière Render), tout passe en https : un visiteur arrivé en http est redirigé."""
    if request.headers.get("X-Forwarded-Proto") == "http" and not app.debug:
        host = (request.host or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1"):
            return redirect(request.url.replace("http://", "https://", 1), code=301)


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
        "img-src 'self' data: blob: https://unpkg.com https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://server.arcgisonline.com; "
        "media-src 'self' blob:; connect-src 'self' https://router.project-osrm.org; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path in ("/sw.js", "/"):
        resp.headers["Cache-Control"] = "no-cache"
    return compress(resp)


GZIP_TYPES = ("text/html", "text/css", "text/plain", "text/xml", "application/javascript", "text/javascript",
              "application/json", "application/manifest+json", "application/xml", "image/svg+xml")


def compress(resp):
    """Compresse le texte (HTML, JS, CSS, JSON) : les pages se chargent plus vite sur une connexion faible."""
    try:
        if resp.status_code != 200 or "gzip" not in request.headers.get("Accept-Encoding", "") \
                or resp.headers.get("Content-Encoding") or resp.mimetype not in GZIP_TYPES:
            return resp
        resp.direct_passthrough = False
        data = resp.get_data()
        if len(data) < 800:
            return resp
        import gzip
        packed = gzip.compress(data, 6)
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers.add("Vary", "Accept-Encoding")
        resp.headers.pop("ETag", None)
        resp.headers["Content-Length"] = str(len(packed))
    except Exception:
        app.logger.exception("compression")
    return resp


# --- Pages / méta -------------------------------------------------------------
PUBLIC_FILES = {
    "index.html", "bienvenue.html", "bienvenue.js", "app.js", "style.css", "sw.js", "manifest.webmanifest",
    "icon.svg", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "favicon-32.png", "og-image.png",
    "confidentialite.html", "conditions.html", "404.html", "fondateur.jpg",
}
PAGES = {"bienvenue": "bienvenue.html", "confidentialite": "confidentialite.html", "conditions": "conditions.html"}


@app.get("/")
def index():
    """Page d'accueil : l'adresse du site (aperçu sur les réseaux sociaux) est celle réellement utilisée."""
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("{{BASE}}", request.url_root.rstrip("/"))
    return Response(html, mimetype="text/html")


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(BASE_DIR, "favicon-32.png", mimetype="image/png")


@app.get("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
    return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {base}/sitemap.xml\n", mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    urls = ["/", "/bienvenue", "/confidentialite", "/conditions"]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "".join(f"  <url><loc>{base}{u}</loc></url>\n" for u in urls) + "</urlset>\n")
    return Response(xml, mimetype="application/xml")


@app.get("/<name>")
def public_file(name):
    """Sert uniquement les fichiers de l'interface (jamais app.py ni la base de données)."""
    if name in PAGES:
        return send_from_directory(BASE_DIR, PAGES[name])
    if name not in PUBLIC_FILES or name in ("404.html", "index.html"):
        return not_found(None)
    return send_from_directory(BASE_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "sunrise-space"})


@app.get("/api/meta")
def meta():
    return jsonify({"communes": COMMUNES, "report_reasons": REPORT_REASONS, "vehicle_types": VEHICLE_TYPES,
                    "default_center": {"lat": 5.3600, "lng": -4.0083}})


# --- Authentification ---------------------------------------------------------
def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    if db.execute("SELECT 1 FROM app_settings WHERE key=?", (key,)).fetchone():
        db.execute("UPDATE app_settings SET value=? WHERE key=?", (value, key))
    else:
        db.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", (key, value))


def send_welcome(db, uid, role, name):
    """Message de bienvenue automatique (réglé par l'admin) déposé dans la messagerie du nouvel inscrit."""
    text = get_setting(db, f"welcome_{role}").strip()
    if not text:
        return
    first = (name.split() or [""])[0]
    db.execute("INSERT INTO messages(user_id,sender,body,created_at) VALUES(?,?,?,?)",
               (uid, "admin", text.replace("{prenom}", first), ts()))
    db.commit()


@app.post("/api/auth/register")
def register():
    data = body()
    if data.get("website"):   # champ piège invisible : seuls les robots le remplissent
        return err("Inscription refusée.", 400)
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
    if weak_password(password, phone):
        return err(WEAK_MSG)

    driver = {}
    if role == "driver":
        d = data.get("driver") if isinstance(data.get("driver"), dict) else {}
        plate = clean(d.get("plate"), 20).upper()
        permis = clean(d.get("permis"), 30)
        commune = clean(d.get("commune"), 40)
        whatsapp = normalize_phone(d.get("whatsapp")) if d.get("whatsapp") else phone
        vehicle_type = d.get("vehicle_type") or "moto"
        if vehicle_type not in VEHICLE_TYPES:
            return err("Choisissez votre type de véhicule : moto, voiture ou tricycle.")
        if len(plate) < 3:
            return err("Entrez la plaque d'immatriculation du véhicule.")
        if len(permis) < 3:
            return err("Entrez votre numéro de permis de conduire.")
        if commune not in COMMUNES:
            return err("Choisissez votre commune / ville.")
        if not whatsapp:
            return err("Numéro WhatsApp invalide.")
        driver = {"plate": plate, "permis": permis, "commune": commune, "whatsapp": whatsapp,
                  "vehicle_type": vehicle_type, "moto_model": clean(d.get("moto_model"), 40)}

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
        return err("Ce numéro a déjà un compte. Connectez-vous.", 409)
    uid = db.insert(
        "INSERT INTO users(role,name,phone,password_hash,created_at) VALUES(?,?,?,?,?)",
        (role, name, phone, generate_password_hash(password), ts()),
    )
    if role == "driver":
        db.execute(
            "INSERT INTO drivers(user_id,plate,vehicle_type,moto_model,permis,commune,whatsapp) VALUES(?,?,?,?,?,?,?)",
            (uid, driver["plate"], driver["vehicle_type"], driver["moto_model"], driver["permis"], driver["commune"], driver["whatsapp"]),
        )
    db.commit()
    if role == "driver":
        notify_staff(db, "🏍️ Chauffeur à valider", f"{name} ({commune_label(driver)}) vient de s'inscrire.", "staff-driver")
    try:
        send_welcome(db, uid, role, name)
    except Exception:
        db.rollback()
        app.logger.exception("message de bienvenue")
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
    db.execute("INSERT INTO login_history(user_id, at, ip, device) VALUES(?,?,?,?)",
               (user["id"], ts(), clean(client_ip(), 60), clean(request.headers.get("User-Agent"), 200)))
    db.execute("DELETE FROM login_history WHERE user_id=? AND id NOT IN "
               "(SELECT id FROM login_history WHERE user_id=? ORDER BY id DESC LIMIT 200)", (user["id"], user["id"]))
    db.commit()
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
    db = get_db()
    if g.user["role"] != "admin":
        # Les sessions durent des jours : l'ouverture de l'application avec une session déjà active
        # est aussi notée (au plus une fois toutes les 30 min par personne).
        last = db.execute("SELECT MAX(at) AS at FROM login_history WHERE user_id=?", (g.user["id"],)).fetchone()
        if not last or not last["at"] or last["at"] < ts(now() - timedelta(minutes=30)):
            db.execute("INSERT INTO login_history(user_id, at, ip, device, kind) VALUES(?,?,?,?,'reprise')",
                       (g.user["id"], ts(), clean(client_ip(), 60), clean(request.headers.get("User-Agent"), 200)))
            db.execute("DELETE FROM login_history WHERE user_id=? AND id NOT IN "
                       "(SELECT id FROM login_history WHERE user_id=? ORDER BY id DESC LIMIT 200)", (g.user["id"], g.user["id"]))
            db.commit()
    return jsonify({"user": user_public(g.user), "driver": driver_profile(db, g.user["id"])})


# --- Mot de passe : changement, oubli ------------------------------------------
TEMP_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sans 0/O, 1/I/L : facile à recopier
_forgot_hits = {}                                        # adresse IP -> instants des demandes récentes
FORGOT_MAX_PER_HOUR = 5


COMMON_PASSWORDS = {"123456", "1234567", "12345678", "123456789", "000000", "111111", "123123", "654321", "azerty", "azerty123",
                    "password", "motdepasse", "qwerty", "abc123", "sunrise", "sunrise123", "007007", "121212"}


def weak_password(password, phone=""):
    """Refuse les mots de passe évidents (numéro de téléphone, chiffre répété, suites et mots courants)."""
    p = password.lower()
    digits = re.sub(r"\D", "", phone or "")
    return p in COMMON_PASSWORDS or len(set(p)) == 1 or (digits and (p == digits or p == digits[-6:] or p == digits[-8:]))


WEAK_MSG = "Ce mot de passe est trop facile à deviner. Évitez votre numéro et les suites comme 123456."


@app.post("/api/auth/change-password")
@auth_required()
def change_password():
    data = body()
    current = str(data.get("current") or "")
    new = str(data.get("new") or "")
    key = ("pw", g.user["id"])
    if login_locked(key):
        return err("Trop de tentatives. Réessayez dans quelques minutes.", 429)
    if not check_password_hash(g.user["password_hash"], current):
        login_failed(key)
        return err("Le mot de passe actuel est incorrect.", 403)
    if len(new) < 6 or len(new) > 100:
        return err("Le nouveau mot de passe doit contenir au moins 6 caractères.")
    if weak_password(new, g.user["phone"]):
        return err(WEAK_MSG)
    if new == current:
        return err("Choisissez un mot de passe différent de l'ancien.")
    _login_fails.pop(key, None)
    db = get_db()
    db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
               (generate_password_hash(new), g.user["id"]))
    db.execute("DELETE FROM tokens WHERE user_id=? AND token_hash!=?", (g.user["id"], g.token_hash))  # autres appareils déconnectés
    db.commit()
    return jsonify({"ok": True, "user": user_public(db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone())})


@app.post("/api/auth/forgot")
def forgot_password():
    """Prévient l'équipe : un administrateur vérifie la personne puis lui remet un mot de passe provisoire.
    La réponse est toujours la même, que le numéro existe ou non (on ne révèle pas qui a un compte)."""
    phone = normalize_phone(body().get("phone"))
    if not phone:
        return err("Numéro invalide. Entrez les 10 chiffres.")
    ip = request.remote_addr or "?"
    recent = [t for t in _forgot_hits.get(ip, []) if time.time() - t < 3600]
    if len(recent) >= FORGOT_MAX_PER_HOUR:
        return err("Trop de demandes. Réessayez plus tard.", 429)
    if len(_forgot_hits) > 5000:
        _forgot_hits.clear()
    _forgot_hits[ip] = recent + [time.time()]
    db = get_db()
    user = db.execute("SELECT id, role FROM users WHERE phone=?", (phone,)).fetchone()
    if user and user["role"] != "admin" \
            and not db.execute("SELECT 1 FROM password_resets WHERE user_id=? AND status='open'", (user["id"],)).fetchone():
        db.execute("INSERT INTO password_resets(user_id, created_at) VALUES(?,?)", (user["id"], ts()))
        db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/password-resets")
@auth_required("admin")
def admin_password_resets():
    rows = get_db().execute(
        """SELECT p.id, p.created_at, u.id AS user_id, u.name, u.phone, u.role, u.is_blocked
           FROM password_resets p JOIN users u ON u.id=p.user_id
           WHERE p.status='open' ORDER BY p.id""").fetchall()
    return jsonify({"requests": [dict(r) for r in rows]})


@app.post("/api/admin/password-resets/<int:rid>")
@auth_required("admin")
def admin_password_reset_action(rid):
    action = body().get("action")
    if action not in ("reset", "dismiss"):
        return err("Action invalide.")
    db = get_db()
    r = db.execute(
        """SELECT p.id, p.status, u.id AS user_id, u.name, u.phone, u.role
           FROM password_resets p JOIN users u ON u.id=p.user_id WHERE p.id=?""", (rid,)).fetchone()
    if not r:
        return err("Demande introuvable.", 404)
    if r["status"] != "open":
        return err("Cette demande a déjà été traitée.", 409)
    if action == "dismiss":
        db.execute("UPDATE password_resets SET status='dismissed', handled_at=? WHERE id=?", (ts(), rid))
        db.commit()
        return jsonify({"ok": True})
    if r["role"] == "admin":
        return err("Le mot de passe d'un administrateur ne se réinitialise pas ici.", 403)
    temp = "".join(secrets.choice(TEMP_PW_ALPHABET) for _ in range(8))
    db.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
               (generate_password_hash(temp), r["user_id"]))
    db.execute("DELETE FROM tokens WHERE user_id=?", (r["user_id"],))   # on déconnecte tout appareil encore ouvert
    db.execute("UPDATE password_resets SET status='done', handled_at=? WHERE user_id=? AND status='open'", (ts(), r["user_id"]))
    db.commit()
    return jsonify({"ok": True, "temp_password": temp, "name": r["name"], "phone": r["phone"]})


# --- Client : chauffeurs proches ---------------------------------------------
@app.get("/api/drivers/nearby")
@auth_required("admin")   # les clients ne voient plus la liste des chauffeurs : ils envoient une demande et attendent
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
        """SELECT u.id, u.name, u.phone, d.plate, d.vehicle_type, d.moto_model, d.commune, d.whatsapp,
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
                "plate": r["plate"], "vehicle_type": r["vehicle_type"], "moto_model": r["moto_model"], "commune": r["commune"],
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
        "vehicle_type": r["vehicle_type"], "client_name": r["client_name"] if reveal else (r["client_name"].split() or ["Client"])[0],
        "age_min": minutes_ago(r["created_at"]),
        "dest_lat": r["dest_lat"], "dest_lng": r["dest_lng"],
        "trip_km": round(haversine_km(r["lat"], r["lng"], r["dest_lat"], r["dest_lng"]), 1) if r["dest_lat"] is not None else None,
    }
    if dlat is not None:
        out["distance_km"] = round(haversine_km(dlat, dlng, r["lat"], r["lng"]), 2)
    if reveal:
        out["client_id"] = r["client_id"]   # sert au signalement ; le numéro, lui, n'est JAMAIS transmis (appel Sunrise)
    return out


def driver_rides(db, uid):
    """Courses acceptées par un chauffeur, dans l'ordre : la première est la course en cours, les autres attendent."""
    return db.execute(
        """SELECT r.*, u.name AS client_name, u.phone AS client_phone
           FROM ride_requests r JOIN users u ON u.id=r.client_id
           WHERE r.driver_id=? AND r.status='accepted' ORDER BY r.accepted_at ASC, r.id ASC""", (uid,)).fetchall()


def chain_anchor(rides):
    """La course en cours si le chauffeur peut encore recevoir des demandes sur son trajet (destination connue, file pas pleine)."""
    if rides and rides[0]["dest_lat"] is not None and len(rides) - 1 < CHAIN_MAX_QUEUE:
        return rides[0]
    return None


def chain_km(anchor, r):
    return haversine_km(anchor["dest_lat"], anchor["dest_lng"], r["lat"], r["lng"])


def free_driver_if_idle(db, uid):
    if not db.execute("SELECT 1 FROM ride_requests WHERE driver_id=? AND status='accepted'", (uid,)).fetchone():
        db.execute("UPDATE drivers SET availability='available' WHERE user_id=? AND availability='busy'", (uid,))


@app.get("/api/driver/history")
@auth_required("driver")
def driver_history():
    """Historique des courses du chauffeur (le numéro du client n'est jamais transmis)."""
    db = get_db()
    rows = db.execute(
        """SELECT r.*, u.name AS client_name FROM ride_requests r JOIN users u ON u.id=r.client_id
           WHERE r.driver_id=? ORDER BY r.id DESC LIMIT 200""", (g.user["id"],)).fetchall()
    day, week = ts(now() - timedelta(days=1)), ts(now() - timedelta(days=7))
    items, done = [], [r for r in rows if r["status"] == "completed"]
    for r in rows:
        items.append({
            "id": r["id"], "status": r["status"], "client_name": r["client_name"],
            "pickup_text": r["pickup_text"], "destination_text": r["destination_text"],
            "vehicle_type": r["vehicle_type"], "rating": r["rating"],
            "accepted_at": r["accepted_at"], "closed_at": r["closed_at"], "created_at": r["created_at"],
            "trip_km": round(haversine_km(r["lat"], r["lng"], r["dest_lat"], r["dest_lng"]), 1) if r["dest_lat"] is not None else None,
        })
    stats = {
        "completed": len(done),
        "today": sum(1 for r in done if (r["closed_at"] or "") >= day),
        "week": sum(1 for r in done if (r["closed_at"] or "") >= week),
        "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
    }
    return jsonify({"rides": items, "stats": stats})


@app.get("/api/driver/dashboard")
@auth_required("driver")
def driver_dashboard():
    db = get_db()
    expire_requests(db)
    sweep_ghost_drivers(db)
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (g.user["id"],)).fetchone()
    profile = driver_profile(db, g.user["id"])
    job, requests, queue = None, [], []
    rides = driver_rides(db, g.user["id"])
    anchor = chain_anchor(rides)
    if rides:
        job = serialize_request_for_driver(rides[0], d["lat"], d["lng"], True)
        queue = [serialize_request_for_driver(r, d["lat"], d["lng"], True) for r in rides[1:]]
    if (anchor or (not rides and d["approval"] == "approved" and d["lat"] is not None and d["availability"] == "available")) and d["lat"] is not None:
        rows = db.execute(
            """SELECT r.*, u.name AS client_name, u.phone AS client_phone
               FROM ride_requests r JOIN users u ON u.id=r.client_id
               WHERE r.status='open' AND r.vehicle_type IN ('any', ?) ORDER BY r.id DESC LIMIT 200""",
            (d["vehicle_type"],),
        ).fetchall()
        for r in rows:
            item = serialize_request_for_driver(r, d["lat"], d["lng"], False)
            if anchor:   # en course : seulement les demandes proches de la destination du client actuel
                item["chain_km"] = round(chain_km(anchor, r), 2)
                if item["chain_km"] <= CHAIN_RADIUS_KM:
                    requests.append(item)
            elif item["distance_km"] <= NEARBY_RADIUS_KM:
                requests.append(item)
        requests.sort(key=lambda x: x.get("chain_km", x["distance_km"]))
        requests = requests[:30]
    chain = {"radius_km": CHAIN_RADIUS_KM, "max_queue": CHAIN_MAX_QUEUE, "can": bool(anchor), "has_dest": bool(rides and rides[0]["dest_lat"] is not None)}
    return jsonify({"driver": profile, "job": job, "queue": queue, "requests": requests, "chain": chain})



# --- Notifications push (Web Push, RFC 8291 + VAPID RFC 8292) -----------------
# Implémenté avec « cryptography » seulement. Fonctionne téléphone verrouillé ou application fermée
# (iPhone : application ajoutée à l'écran d'accueil, iOS 16.4 ou plus).
PUSH_HOSTS = (".googleapis.com", ".mozilla.com", ".push.apple.com", ".windows.com", ".mozaws.net")


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def push_endpoint_ok(endpoint):
    """Seuls les vrais services push des navigateurs sont acceptés (évite qu'on fasse appeler une adresse interne)."""
    try:
        u = urlsplit(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and len(endpoint) <= 600 and any(host.endswith(h) or host == h[1:] for h in PUSH_HOSTS)


def vapid_keys(db):
    """Clé VAPID : variable VAPID_PRIVATE_KEY, sinon créée une fois et gardée dans la base."""
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not raw:
        row = db.execute("SELECT value FROM app_settings WHERE key='vapid_private'").fetchone()
        if row:
            raw = row["value"]
        else:
            raw = b64u(ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big"))
            try:
                db.execute("INSERT INTO app_settings(key,value) VALUES('vapid_private',?)", (raw,))
                db.commit()
            except Exception:
                db.rollback()
                row = db.execute("SELECT value FROM app_settings WHERE key='vapid_private'").fetchone()
                raw = row["value"]
    priv = ec.derive_private_key(int.from_bytes(b64u_dec(raw), "big"), ec.SECP256R1())
    return priv, vapid_public_b64(priv)


def vapid_public_b64(priv):
    from cryptography.hazmat.primitives import serialization
    return b64u(priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def webpush_encrypt(payload, p256dh, auth, as_priv=None, salt=None):
    """Chiffre le message pour un abonné (RFC 8291, aes128gcm). Renvoie le corps à envoyer."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_pub = b64u_dec(p256dh)
    auth_secret = b64u_dec(auth)
    as_priv = as_priv or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + cipher


def vapid_header(priv, pub_b64, endpoint):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urlsplit(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
              "sub": os.environ.get("VAPID_SUBJECT", "mailto:contact@sunrise-space.app")}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body_ = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{head}.{body_}".encode()
    r, s_ = decode_dss_signature(priv.sign(signing, ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{body_}.{b64u(r.to_bytes(32, 'big') + s_.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={pub_b64}"


def _send_one(priv, pub_b64, sub, payload):
    """Envoie une notification. Renvoie « ok », « gone » (abonnement mort : à supprimer) ou « error »."""
    try:
        try:   # demande de course : si le téléphone est éteint ou sans réseau, elle n'est plus livrée après 2 min (course périmée)
            ttl = 120 if json.loads(payload).get("urgent") else 1800
        except Exception:
            ttl = 1800
        data = webpush_encrypt(payload, sub["p256dh"], sub["auth"])
        req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers={
            "Authorization": vapid_header(priv, pub_b64, sub["endpoint"]),
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
            "TTL": str(ttl), "Urgency": "high"})
        urllib.request.urlopen(req, timeout=8).close()
        return "ok"
    except urllib.error.HTTPError as e:
        return "gone" if e.code in (404, 410) else "error"
    except Exception:
        return "error"


def _deliver(subs, payload):
    """Envoi en arrière-plan : n'attend jamais la réponse des services push."""
    def work():
        db = DB()
        try:
            priv, pub = vapid_keys(db)
            for sub in subs:
                if _send_one(priv, pub, sub, payload) == "gone":
                    db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],))
                    db.commit()
        except Exception:
            pass
        finally:
            db.close()
    threading.Thread(target=work, daemon=True).start()


def notify_users(db, user_ids, title, body_text, tag=None, urgent=False):
    """Prévient ces utilisateurs sur tous leurs appareils abonnés."""
    ids = list({int(i) for i in user_ids})
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    subs = [dict(r) for r in db.execute(
        f"SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id IN ({marks})", ids).fetchall()]
    if subs:
        _deliver(subs, json.dumps({"title": title, "body": body_text, "tag": tag or "sunrise", "url": "/", "urgent": bool(urgent)}).encode())
    return len(subs)


def commune_label(driver):
    return f"{driver.get('commune', '')} · {driver.get('vehicle_type', '')}".strip(" ·")


def staff_ids(db, validators=True):
    """Comptes administrateurs actifs (avec ou sans les validateurs) à prévenir par notification."""
    rows = db.execute("SELECT id, staff_scope FROM users WHERE role='admin' AND is_blocked=0").fetchall()
    return [r["id"] for r in rows if validators or r["staff_scope"] != "validator"]


def notify_staff(db, title, text, tag, validators=True):
    try:
        notify_users(db, staff_ids(db, validators), title, text, tag=tag, urgent=True)
    except Exception:
        app.logger.exception("notification administrateurs")


@app.get("/api/push/key")
@auth_required()
def push_key():
    return jsonify({"key": vapid_keys(get_db())[1]})


@app.post("/api/push/subscribe")
@auth_required()
def push_subscribe():
    data = body()
    endpoint = data.get("endpoint") if isinstance(data.get("endpoint"), str) else ""
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (push_endpoint_ok(endpoint) and isinstance(p256dh, str) and isinstance(auth, str)):
        return err("Abonnement invalide.")
    try:
        if len(b64u_dec(p256dh)) != 65 or len(b64u_dec(auth)) != 16:
            return err("Abonnement invalide.")
    except Exception:
        return err("Abonnement invalide.")
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))   # l'appareil change de compte : il suit le dernier connecté
    db.execute("INSERT INTO push_subscriptions(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)",
               (g.user["id"], endpoint, p256dh, auth, ts()))
    old = db.execute("SELECT id FROM push_subscriptions WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    for r in old[5:]:                                                           # 5 appareils au plus par personne
        db.execute("DELETE FROM push_subscriptions WHERE id=?", (r["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
@auth_required()
def push_unsubscribe():
    endpoint = body().get("endpoint")
    db = get_db()
    if isinstance(endpoint, str):
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (endpoint, g.user["id"]))
    db.commit()
    return jsonify({"ok": True})


def notify_drivers_of_request(db, rid, lat, lng, wanted, pickup, trip_km=None):
    """Nouvelle demande : on prévient les chauffeurs disponibles à moins de 15 km de leur dernière position connue."""
    fresh = ts(now() - timedelta(hours=PUSH_DRIVER_MAX_AGE_H))
    rows = db.execute(
        """SELECT d.user_id, d.lat, d.lng, d.availability FROM drivers d JOIN users u ON u.id=d.user_id
           WHERE d.approval='approved' AND d.availability IN ('available','busy') AND u.is_blocked=0
                 AND d.lat IS NOT NULL AND d.location_at>=?
                 AND (? = 'any' OR d.vehicle_type=?)""", (fresh, wanted, wanted)).fetchall()
    cand = []
    for r in rows:
        if r["availability"] == "available":
            km = haversine_km(lat, lng, r["lat"], r["lng"])
            if km <= NEARBY_RADIUS_KM:
                cand.append((km, r["user_id"], False))
        else:   # en course : prévenu seulement si la demande est près de la destination de son client actuel
            anchor = chain_anchor(driver_rides(db, r["user_id"]))
            if anchor:
                km = haversine_km(anchor["dest_lat"], anchor["dest_lng"], lat, lng)
                if km <= CHAIN_RADIUS_KM:
                    cand.append((km, r["user_id"], True))
    cand.sort()
    near = [(km, uid) for km, uid, _chain in cand[:30]]
    for km, uid, chain in cand[:30]:
        where = f" · {pickup}" if pickup else ""
        trip = f" · trajet ≈ {trip_km:.1f} km" if trip_km is not None else ""
        if chain:
            notify_users(db, [uid], "🙋 Demande sur votre trajet", f"À {km:.1f} km de la destination de votre client actuel{where}{trip}", tag=f"req-{rid}", urgent=True)
        else:
            notify_users(db, [uid], "🙋 Nouvelle demande de course", f"À environ {km:.1f} km de votre dernière position{where}{trip}", tag=f"req-{rid}", urgent=True)
    if near and PUSH_REMINDER_DELAYS:
        threading.Thread(target=_remind_drivers, args=(rid, [u for _, u in near]), daemon=True).start()
    return len(near)


PUSH_REMINDER_DELAYS = (10,) * 8     # tant que la demande est ouverte : le téléphone re-sonne 8 fois, toutes les 10 s (environ 1 min 30)


def _remind_drivers(rid, driver_ids):
    """Une notification ne sonne qu'un instant : on la renouvelle tant que personne n'a pris la demande."""
    for delay in PUSH_REMINDER_DELAYS:
        time.sleep(delay)
        db = DB()
        try:
            r = db.execute("SELECT status FROM ride_requests WHERE id=?", (rid,)).fetchone()
            if not r or r["status"] != "open":
                return
            marks = ",".join("?" * len(driver_ids))
            still = [x["user_id"] for x in db.execute(
                f"SELECT user_id FROM drivers WHERE availability IN ('available','busy') AND user_id IN ({marks})", driver_ids).fetchall()]
            notify_users(db, still, "🙋 Demande toujours en attente", "Touchez pour ouvrir et accepter la course.", tag=f"req-{rid}", urgent=True)
        except Exception:
            app.logger.exception("rappel push")
            return
        finally:
            db.close()


_nudges = {}


def nudge_stale_driver(db, driver_id, location_at):
    """Course en cours mais le chauffeur a quitté l'application : un rappel qui la rouvre."""
    age = minutes_ago(location_at)
    if age is not None and age < NUDGE_STALE_MIN:
        return
    if time.time() - _nudges.get(driver_id, 0) < NUDGE_EVERY_MIN * 60:
        return
    _nudges[driver_id] = time.time()
    notify_users(db, [driver_id], "📍 Votre client vous attend",
                 "Ouvrez Sunrise Space pour partager votre position.", tag="nudge")


# --- Demandes de moto ---------------------------------------------------------
def serialize_request_for_client(db, r):
    out = {
        "id": r["id"], "status": r["status"], "pickup_text": r["pickup_text"],
        "destination_text": r["destination_text"], "note": r["note"],
        "lat": r["lat"], "lng": r["lng"], "age_min": minutes_ago(r["created_at"]),
        "vehicle_type": r["vehicle_type"], "driver": None, "rating": r["rating"],
        "dest_lat": r["dest_lat"], "dest_lng": r["dest_lng"],
    }
    if r["driver_id"] and r["status"] in ("accepted", "completed"):
        d = db.execute(
            """SELECT u.id, u.name, d.plate, d.vehicle_type, d.moto_model, d.lat, d.lng, d.location_at
               FROM users u JOIN drivers d ON d.user_id=u.id WHERE u.id=?""",
            (r["driver_id"],),
        ).fetchone()
        if d and r["status"] == "accepted":
            out["queued_behind"] = db.execute(
                """SELECT COUNT(*) AS n FROM ride_requests WHERE driver_id=? AND status='accepted' AND id!=?
                   AND (accepted_at<? OR (accepted_at=? AND id<?))""",
                (r["driver_id"], r["id"], r["accepted_at"], r["accepted_at"], r["id"])).fetchone()["n"]
        if d:
            out["driver"] = {
                "id": d["id"], "name": d["name"],
                "plate": d["plate"], "vehicle_type": d["vehicle_type"], "moto_model": d["moto_model"],
                "lat": d["lat"] if r["status"] == "accepted" else None,
                "lng": d["lng"] if r["status"] == "accepted" else None,
                **{k: v for k, v in driver_stats(db, d["id"]).items() if k != "rides_done"},
                "location_age_min": minutes_ago(d["location_at"]),
            }
    return out


# --- Appels Sunrise dans l'application (voix par internet, WebRTC) --------------
# Le serveur ne fait que passer les messages de mise en relation (signalisation) ; la voix va d'un téléphone à l'autre.
# Personne ne voit jamais le numéro de l'autre : l'écran affiche seulement « Sunrise ».
CALL_RING_SECONDS = 45
CALL_MAX_SIGNALS = 300


def ice_servers():
    servers = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]
    raw = os.environ.get("CALL_ICE_SERVERS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, list):
                return servers + [x for x in extra if isinstance(x, dict)]
        except ValueError:
            pass
    turn = os.environ.get("TURN_URLS", "").strip()
    if turn:
        servers.append({"urls": [u.strip() for u in turn.split(",") if u.strip()],
                        "username": os.environ.get("TURN_USERNAME", ""), "credential": os.environ.get("TURN_CREDENTIAL", "")})
    return servers


def expire_calls(db):
    limit = ts(now() - timedelta(seconds=CALL_RING_SECONDS))
    db.execute("UPDATE calls SET status='missed', ended_at=? WHERE status='ringing' AND created_at<?", (ts(), limit))
    db.commit()


def call_for(db, cid, user_id):
    c = db.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    if not c or user_id not in (c["caller_id"], c["callee_id"]):
        return None
    return c


def peer_info(db, uid):
    """Rôle et nom de l'interlocuteur (client/chauffeur) pour l'écran d'appel. L'administrateur reste « Sunrise »."""
    u = db.execute("SELECT name, role FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["role"] not in ("client", "driver"):
        return None
    return {"role": u["role"], "name": u["name"]}


def valid_signal(text, limit=20000):
    return isinstance(text, str) and 0 < len(text) <= limit


@app.get("/api/calls/ice")
@auth_required("client", "driver", "admin")
def call_ice():
    return jsonify({"iceServers": ice_servers()})


def admin_call_start(db, data):
    """L'administrateur appelle un client ou un chauffeur actuellement connecté ; il voit « Sunrise » de l'autre côté."""
    if g.user["staff_scope"] == "validator":
        return err("Accès refusé.", 403)
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return err("Utilisateur invalide.")
    u = db.execute("SELECT id, role, last_seen_at FROM users WHERE id=? AND role IN ('client','driver') AND is_blocked=0", (uid,)).fetchone()
    if not u:
        return err("Utilisateur introuvable.", 404)
    if not u["last_seen_at"] or minutes_ago(u["last_seen_at"]) > ONLINE_WINDOW_MIN:
        return err("Cette personne n'est pas connectée en ce moment.", 409)
    expire_calls(db)
    if db.execute("SELECT 1 FROM calls WHERE status IN ('ringing','active') AND (callee_id=? OR caller_id=?)", (uid, uid)).fetchone():
        return err("Cette personne est déjà en communication.", 409)
    db.execute("UPDATE calls SET status='ended', ended_at=? WHERE caller_id=? AND status IN ('ringing','active')", (ts(), g.user["id"]))
    cid = db.insert("INSERT INTO calls(request_id,caller_id,callee_id,status,created_at) VALUES(?,?,?,?,?)",
                    (None, g.user["id"], uid, "ringing", ts()))
    db.execute("INSERT INTO call_signals(call_id,sender_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
               (cid, g.user["id"], "offer", data["offer"], ts()))
    db.commit()
    notify_users(db, [uid], "📞 Appel Sunrise", "Appel entrant. Ouvrez Sunrise Space pour répondre.", tag="call", urgent=True)
    return jsonify({"id": cid})


@app.post("/api/calls")
@auth_required("client", "driver", "admin")
def call_start():
    data = body()
    if not valid_signal(data.get("offer")):
        return err("Appel invalide.")
    db = get_db()
    if g.user["role"] == "admin":
        return admin_call_start(db, data)
    try:
        rid = int(data.get("request_id"))
    except (TypeError, ValueError):
        return err("Course invalide.")
    r = db.execute("SELECT * FROM ride_requests WHERE id=? AND status='accepted'", (rid,)).fetchone()
    me_id = g.user["id"]
    if not r or me_id not in (r["client_id"], r["driver_id"]):
        return err("L'appel n'est possible que pendant une course acceptée.", 403)
    peer = r["driver_id"] if me_id == r["client_id"] else r["client_id"]
    expire_calls(db)
    db.execute("UPDATE calls SET status='ended', ended_at=? WHERE request_id=? AND status IN ('ringing','active')", (ts(), rid))
    cid = db.insert("INSERT INTO calls(request_id,caller_id,callee_id,status,created_at) VALUES(?,?,?,?,?)",
                    (rid, me_id, peer, "ringing", ts()))
    db.execute("INSERT INTO call_signals(call_id,sender_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
               (cid, me_id, "offer", data["offer"], ts()))
    db.commit()
    notify_users(db, [peer], "📞 Appel Sunrise", "Appel entrant. Ouvrez Sunrise Space pour répondre.", tag="call", urgent=True)
    return jsonify({"id": cid, "peer": peer_info(db, peer)})


@app.get("/api/calls/incoming")
@auth_required("client", "driver", "admin")
def call_incoming():
    db = get_db()
    expire_calls(db)
    c = db.execute("SELECT * FROM calls WHERE callee_id=? AND status='ringing' ORDER BY id DESC LIMIT 1", (g.user["id"],)).fetchone()
    if not c:
        return jsonify({"call": None})
    sig = db.execute("SELECT payload FROM call_signals WHERE call_id=? AND kind='offer' ORDER BY id LIMIT 1", (c["id"],)).fetchone()
    return jsonify({"call": {"id": c["id"], "offer": sig["payload"] if sig else None, "peer": peer_info(db, c["caller_id"])}})


@app.get("/api/calls/<int:cid>")
@auth_required("client", "driver", "admin")
def call_poll(cid):
    db = get_db()
    expire_calls(db)
    c = call_for(db, cid, g.user["id"])
    if not c:
        return err("Appel introuvable.", 404)
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    rows = db.execute(
        "SELECT id, kind, payload FROM call_signals WHERE call_id=? AND id>? AND sender_id!=? ORDER BY id LIMIT 100",
        (cid, after, g.user["id"])).fetchall()
    return jsonify({"status": c["status"], "signals": [{"id": x["id"], "kind": x["kind"], "payload": x["payload"]} for x in rows]})


@app.post("/api/calls/<int:cid>/accept")
@auth_required("client", "driver", "admin")
def call_accept(cid):
    db = get_db()
    expire_calls(db)
    c = call_for(db, cid, g.user["id"])
    answer = body().get("answer")
    if not c or c["callee_id"] != g.user["id"]:
        return err("Appel introuvable.", 404)
    if c["status"] != "ringing":
        return err("Cet appel est terminé.", 409)
    if not valid_signal(answer):
        return err("Réponse invalide.")
    db.execute("UPDATE calls SET status='active', answered_at=? WHERE id=?", (ts(), cid))
    db.execute("INSERT INTO call_signals(call_id,sender_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
               (cid, g.user["id"], "answer", answer, ts()))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/calls/<int:cid>/signal")
@auth_required("client", "driver", "admin")
def call_signal(cid):
    db = get_db()
    c = call_for(db, cid, g.user["id"])
    data = body()
    if not c:
        return err("Appel introuvable.", 404)
    if c["status"] not in ("ringing", "active"):
        return jsonify({"ok": False})
    if data.get("kind") != "ice" or not valid_signal(data.get("payload"), 4000):
        return err("Signal invalide.")
    n = db.execute("SELECT COUNT(*) AS n FROM call_signals WHERE call_id=?", (cid,)).fetchone()["n"]
    if n >= CALL_MAX_SIGNALS:
        return err("Trop de signaux.", 429)
    db.execute("INSERT INTO call_signals(call_id,sender_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
               (cid, g.user["id"], "ice", data["payload"], ts()))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/calls/<int:cid>/end")
@auth_required("client", "driver", "admin")
def call_end(cid):
    db = get_db()
    c = call_for(db, cid, g.user["id"])
    if not c:
        return err("Appel introuvable.", 404)
    if c["status"] in ("ringing", "active"):
        status = "declined" if (c["status"] == "ringing" and c["callee_id"] == g.user["id"]) else "ended"
        db.execute("UPDATE calls SET status=?, ended_at=? WHERE id=?", (status, ts(), cid))
        db.commit()
    return jsonify({"ok": True})


@app.post("/api/requests")
@auth_required("client")
def create_request():
    data = body()
    coords = get_coords(data)
    if not coords:
        return err("Position de prise en charge invalide.")
    wanted = data.get("vehicle_type") or "any"
    if wanted != "any" and wanted not in VEHICLE_TYPES:
        return err("Type de véhicule invalide.")
    dest = None                      # point d'arrivée facultatif (choisi sur la carte ou dans la recherche)
    if data.get("dest_lat") is not None or data.get("dest_lng") is not None:
        dest = get_coords({"lat": data.get("dest_lat"), "lng": data.get("dest_lng")})
        if not dest:
            return err("Destination invalide.")
        if haversine_km(coords[0], coords[1], dest[0], dest[1]) > MAX_TRIP_KM:
            return err(f"Destination trop éloignée (plus de {MAX_TRIP_KM} km).")
    db = get_db()
    expire_requests(db)
    sweep_ghost_drivers(db)
    active = db.execute(
        "SELECT 1 FROM ride_requests WHERE client_id=? AND status IN ('open','accepted')", (g.user["id"],)
    ).fetchone()
    if active:
        return err("Vous avez déjà une demande en cours.", 409)
    new_id = db.insert(
        """INSERT INTO ride_requests(client_id,lat,lng,pickup_text,destination_text,note,vehicle_type,created_at,dest_lat,dest_lng)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (g.user["id"], coords[0], coords[1], clean(data.get("pickup_text"), 120),
         clean(data.get("destination_text"), 120), clean(data.get("note"), 200), wanted, ts(),
         dest[0] if dest else None, dest[1] if dest else None),
    )
    db.commit()
    r = db.execute("SELECT * FROM ride_requests WHERE id=?", (new_id,)).fetchone()
    try:
        notify_drivers_of_request(db, new_id, coords[0], coords[1], wanted, clean(data.get("pickup_text"), 60),
                                  haversine_km(coords[0], coords[1], dest[0], dest[1]) if dest else None)
    except Exception:
        app.logger.exception("push demande")
    return jsonify({"request": serialize_request_for_client(db, r)}), 201


_geo_cache = {}
_geo_last = [0.0]


def _nominatim(q, lat, lng):
    """Recherche de lieux (OpenStreetMap / Nominatim), limitée à la Côte d'Ivoire, autour de la position du client."""
    d = 0.5
    params = {"format": "jsonv2", "q": q, "countrycodes": "ci", "limit": "6", "accept-language": "fr"}
    if lat is not None:
        params.update(viewbox=f"{lng - d},{lat + d},{lng + d},{lat - d}")
    url = "https://nominatim.openstreetmap.org/search?" + urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SunriseSpace/1.0 (recherche de destination)"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode())
    return [{"label": ", ".join(x.get("display_name", "").split(", ")[:3]), "lat": float(x["lat"]), "lng": float(x["lon"])} for x in data]


@app.get("/api/geocode")
@auth_required("client")
def geocode():
    q = clean(request.args.get("q"), 80)
    if len(q) < 3:
        return jsonify({"results": []})
    try:
        lat, lng = float(request.args.get("lat")), float(request.args.get("lng"))
    except (TypeError, ValueError):
        lat = lng = None
    key = (q.lower(), round(lat, 1) if lat is not None else None, round(lng, 1) if lng is not None else None)
    hit = _geo_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return jsonify({"results": hit[1]})
    if time.time() - _geo_last[0] < 1.0:          # règle d'usage de Nominatim : 1 requête par seconde au plus
        return jsonify({"results": [], "busy": True})
    _geo_last[0] = time.time()
    try:
        results = _nominatim(q, lat, lng)
    except Exception:
        return jsonify({"results": [], "unavailable": True})
    if len(_geo_cache) > 500:
        _geo_cache.clear()
    _geo_cache[key] = (time.time(), results)
    return jsonify({"results": results})


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
    if r and r["status"] == "accepted" and r["driver_id"]:
        try:
            dr = db.execute("SELECT location_at FROM drivers WHERE user_id=?", (r["driver_id"],)).fetchone()
            if dr:
                nudge_stale_driver(db, r["driver_id"], dr["location_at"])
        except Exception:
            app.logger.exception("push rappel")
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
        free_driver_if_idle(db, r["driver_id"])
    db.commit()
    if r["driver_id"]:
        notify_users(db, [r["driver_id"]], "Course annulée", "Le client a annulé sa demande.", tag=f"can-{rid}")
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/accept")
@auth_required("driver")
def accept_request(rid):
    db = get_db()
    d = db.execute("SELECT * FROM drivers WHERE user_id=?", (g.user["id"],)).fetchone()
    if d["approval"] != "approved":
        return err("Votre profil n'est pas encore validé.", 403)
    rides = driver_rides(db, g.user["id"])
    expire_requests(db)
    if rides:   # déjà en course : on peut empiler des demandes proches de la destination du client actuel
        anchor = chain_anchor(rides)
        if not anchor:
            msg = ("Vous avez déjà le maximum de courses en attente." if rides[0]["dest_lat"] is not None
                   else "Terminez votre course en cours avant d'en accepter une autre (elle n'a pas de destination précise).")
            return err(msg, 409)
        cand = db.execute("SELECT lat, lng FROM ride_requests WHERE id=? AND status='open'", (rid,)).fetchone()
        if cand and chain_km(anchor, cand) > CHAIN_RADIUS_KM:
            return err(f"Cette demande est à plus de {CHAIN_RADIUS_KM} km de la destination de votre client actuel.", 409)
    elif d["availability"] != "available":
        return err("Passez en « Disponible » pour accepter une demande.", 409)
    cur = db.execute(
        "UPDATE ride_requests SET status='accepted', driver_id=?, accepted_at=? "
        "WHERE id=? AND status='open' AND vehicle_type IN ('any', ?)",
        (g.user["id"], ts(), rid, d["vehicle_type"]),
    )
    if cur.rowcount == 0:
        db.rollback()
        other = db.execute("SELECT vehicle_type FROM ride_requests WHERE id=? AND status='open'", (rid,)).fetchone()
        if other:
            return err(f"Cette demande est réservée aux chauffeurs de type « {VEHICLE_TYPES.get(other['vehicle_type'], '')} ».", 403)
        return err("Cette demande a déjà été prise ou a expiré.", 409)
    db.execute("UPDATE drivers SET availability='busy' WHERE user_id=?", (g.user["id"],))
    db.commit()
    row = db.execute("SELECT client_id FROM ride_requests WHERE id=?", (rid,)).fetchone()
    if row:
        if rides:
            notify_users(db, [row["client_id"]], "✅ Course acceptée", f"{g.user['name']} termine une première course puis viendra vous chercher. Vous pouvez suivre sa position.", tag=f"acc-{rid}")
        else:
            notify_users(db, [row["client_id"]], "✅ Course acceptée", f"{g.user['name']} arrive vers vous. Ouvrez l'application pour le suivre.", tag=f"acc-{rid}")
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
    free_driver_if_idle(db, g.user["id"])
    db.commit()
    row = db.execute("SELECT client_id FROM ride_requests WHERE id=?", (rid,)).fetchone()
    if row:
        notify_users(db, [row["client_id"]], "Le chauffeur n'est plus disponible", "Votre demande est de nouveau ouverte : un autre chauffeur peut l'accepter.", tag=f"rel-{rid}")
    return jsonify({"ok": True})


@app.post("/api/requests/<int:rid>/rate")
@auth_required("client")
def rate_request(rid):
    """Le client note son chauffeur (1 à 5 étoiles) une fois la course terminée."""
    data = body()
    stars = data.get("stars")
    if not isinstance(stars, int) or isinstance(stars, bool) or not 1 <= stars <= 5:
        return err("Choisissez une note de 1 à 5.")
    db = get_db()
    r = db.execute("SELECT status, driver_id, rating FROM ride_requests WHERE id=? AND client_id=?", (rid, g.user["id"])).fetchone()
    if not r:
        return err("Course introuvable.", 404)
    if r["status"] != "completed" or not r["driver_id"]:
        return err("Vous pourrez noter le chauffeur une fois la course terminée.", 409)
    if r["rating"] is not None:
        return err("Vous avez déjà noté cette course.", 409)
    db.execute("UPDATE ride_requests SET rating=?, rating_comment=? WHERE id=?", (stars, clean(data.get("comment"), 200), rid))
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
    free_driver_if_idle(db, r["driver_id"])
    db.commit()
    nxt = driver_rides(db, r["driver_id"])
    if nxt and r["id"] < 10**12:   # la course suivante devient la course en cours : son client est prévenu
        notify_users(db, [nxt[0]["client_id"]], "🏍️ Votre chauffeur arrive", "Il a terminé sa course précédente et vient maintenant vous chercher.", tag=f"next-{nxt[0]['id']}")
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
    sweep_ghost_drivers(db)
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
        "messages_unread": one("SELECT COUNT(*) FROM messages WHERE sender='user' AND read_at IS NULL"),
        "rides_week": one("SELECT COUNT(*) FROM ride_requests WHERE status='completed' AND closed_at>=?", (ts(now() - timedelta(days=7)),)),
        "avg_rating": (lambda v: round(float(v), 1) if v is not None else None)(one("SELECT AVG(rating) FROM ride_requests WHERE rating IS NOT NULL")),
        "password_resets_open": one("SELECT COUNT(*) FROM password_resets WHERE status='open'"),
    })


@app.post("/api/me/location")
@auth_required("client")
def client_location():
    """Position du client tant que son application est ouverte : visible UNIQUEMENT par l'administrateur (carte)."""
    coords = get_coords(body())
    if not coords:
        return err("Position invalide.")
    db = get_db()
    if db.execute("SELECT 1 FROM user_positions WHERE user_id=?", (g.user["id"],)).fetchone():
        db.execute("UPDATE user_positions SET lat=?, lng=?, at=? WHERE user_id=?", (coords[0], coords[1], ts(), g.user["id"]))
    else:
        db.execute("INSERT INTO user_positions(user_id,lat,lng,at) VALUES(?,?,?,?)", (g.user["id"], coords[0], coords[1], ts()))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/map")
@auth_required("admin")
def admin_map():
    """Carte : chauffeurs et clients connectés (application ouverte) avec leur dernière position connue."""
    db = get_db()
    since = ts(now() - timedelta(minutes=ONLINE_WINDOW_MIN))
    away_since = ts(now() - timedelta(minutes=DRIVER_AWAY_MIN))
    pos_fresh = ts(now() - timedelta(minutes=LOCATION_TTL_MIN * 3))
    drivers, clients, hidden = [], [], 0
    rows = db.execute(
        """SELECT u.id, u.name, u.phone, u.last_seen_at, d.vehicle_type, d.availability, d.approval, d.lat, d.lng, d.location_at
           FROM users u JOIN drivers d ON d.user_id=u.id
           WHERE u.role='driver' AND u.is_blocked=0
             AND (u.last_seen_at>=? OR (d.availability IN ('available','busy') AND u.last_seen_at>=?))
           ORDER BY u.last_seen_at DESC LIMIT 400""", (since, away_since)).fetchall()
    for r in rows:
        if r["lat"] is None or not r["location_at"] or r["location_at"] < min(pos_fresh, away_since):
            hidden += 1
            continue
        drivers.append({"id": r["id"], "name": r["name"], "phone": r["phone"], "lat": r["lat"], "lng": r["lng"],
                        "vehicle_type": r["vehicle_type"], "availability": r["availability"], "approval": r["approval"],
                        "seen_min": minutes_ago(r["last_seen_at"]), "pos_min": minutes_ago(r["location_at"]),
                        "away": minutes_ago(r["last_seen_at"]) > ONLINE_WINDOW_MIN})
    rows = db.execute(
        """SELECT u.id, u.name, u.phone, u.last_seen_at, p.lat, p.lng, p.at
           FROM users u LEFT JOIN user_positions p ON p.user_id=u.id
           WHERE u.role='client' AND u.is_blocked=0 AND u.last_seen_at>=? ORDER BY u.last_seen_at DESC LIMIT 400""", (since,)).fetchall()
    for r in rows:
        if r["lat"] is None or r["at"] < pos_fresh:
            hidden += 1
            continue
        clients.append({"id": r["id"], "name": r["name"], "phone": r["phone"], "lat": r["lat"], "lng": r["lng"],
                        "seen_min": minutes_ago(r["last_seen_at"]), "pos_min": minutes_ago(r["at"])})
    return jsonify({"drivers": drivers, "clients": clients, "without_position": hidden})


@app.get("/api/admin/online")
@auth_required("admin")
def admin_online():
    """Qui est en ligne : application ouverte (vu il y a moins de 3 min) ; les chauffeurs disponibles restent en ligne 2 h après avoir quitté l'application."""
    db = get_db()
    since = ts(now() - timedelta(minutes=ONLINE_WINDOW_MIN))
    away_since = ts(now() - timedelta(minutes=DRIVER_AWAY_MIN))
    rows = db.execute(
        """SELECT u.id, u.role, u.name, u.phone, u.last_seen_at, d.vehicle_type, d.availability, d.approval
           FROM users u LEFT JOIN drivers d ON d.user_id=u.id
           WHERE u.role IN ('client','driver') AND u.is_blocked=0
             AND (u.last_seen_at>=? OR (u.role='driver' AND d.availability IN ('available','busy') AND u.last_seen_at>=?))
           ORDER BY u.last_seen_at DESC LIMIT 300""", (since, away_since)).fetchall()
    out = {"clients": [], "drivers": []}
    for r in rows:
        item = {"id": r["id"], "name": r["name"], "phone": r["phone"], "seen_min": minutes_ago(r["last_seen_at"])}
        if r["role"] == "driver":
            item.update(vehicle_type=r["vehicle_type"], availability=r["availability"], approval=r["approval"],
                        away=item["seen_min"] > ONLINE_WINDOW_MIN)
            out["drivers"].append(item)
        else:
            out["clients"].append(item)
    return jsonify(out)


@app.get("/api/admin/requests")
@auth_required("admin")
def admin_requests():
    """Demandes de course par état (annulées par défaut)."""
    status = request.args.get("status", "cancelled")
    if status not in ("cancelled", "expired", "completed", "open", "accepted", "all"):
        status = "cancelled"
    sql = """SELECT r.id, r.status, r.pickup_text, r.destination_text, r.note, r.vehicle_type, r.created_at, r.closed_at, r.accepted_at, r.rating,
                    c.id AS client_id, c.name AS client_name, c.phone AS client_phone,
                    d.name AS driver_name, d.phone AS driver_phone
             FROM ride_requests r JOIN users c ON c.id=r.client_id LEFT JOIN users d ON d.id=r.driver_id"""
    params = []
    if status != "all":
        sql += " WHERE r.status=?"
        params.append(status)
    sql += " ORDER BY r.id DESC LIMIT 100"
    db = get_db()
    expire_requests(db)
    rows = db.execute(sql, params).fetchall()
    counts = {k: db.execute("SELECT COUNT(*) FROM ride_requests WHERE status=?", (k,)).fetchone()[0]
              for k in ("cancelled", "expired", "completed", "open")}
    return jsonify({"requests": [dict(r) for r in rows], "counts": counts})


@app.get("/api/admin/welcome")
@auth_required("admin")
def admin_get_welcome():
    db = get_db()
    return jsonify({"client": get_setting(db, "welcome_client"), "driver": get_setting(db, "welcome_driver")})


@app.put("/api/admin/welcome")
@auth_required("admin")
def admin_set_welcome():
    data = body()
    db = get_db()
    for role in ("client", "driver"):
        if role in data:
            set_setting(db, f"welcome_{role}", clean_text(data.get(role), 800))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/drivers")
@auth_required("admin")
def admin_drivers():
    status = request.args.get("approval", "pending")
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    q = """SELECT u.id, u.name, u.phone, u.is_blocked, u.created_at, d.plate, d.vehicle_type, d.moto_model, d.permis,
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


@app.get("/api/admin/validators")
@auth_required("admin")
def admin_validators():
    rows = get_db().execute("SELECT id, name, phone, is_blocked, created_at FROM users WHERE role='admin' AND staff_scope='validator' ORDER BY id DESC").fetchall()
    return jsonify({"validators": [dict(r) for r in rows]})


@app.post("/api/admin/validators")
@auth_required("admin")
def admin_create_validator():
    """Ouvre un accès réduit : valider les chauffeurs + voir la liste des inscrits, rien d'autre."""
    data = body()
    name = clean(data.get("name"), 60)
    phone = normalize_phone(data.get("phone"))
    password = str(data.get("password") or "")
    if len(name) < 2:
        return err("Entrez le nom du validateur.")
    if not phone:
        return err("Numéro invalide. Entrez les 10 chiffres.")
    if len(password) < 6 or len(password) > 100:
        return err("Le mot de passe provisoire doit contenir au moins 6 caractères.")
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
        return err("Ce numéro a déjà un compte.", 409)
    uid = db.insert("INSERT INTO users(role,name,phone,password_hash,created_at,must_change_password,staff_scope) VALUES('admin',?,?,?,?,1,'validator')",
                    (name, phone, generate_password_hash(password), ts()))
    db.commit()
    return jsonify({"ok": True, "id": uid}), 201


@app.post("/api/admin/validators/<int:uid>/password")
@auth_required("admin")
def admin_validator_password(uid):
    password = str(body().get("password") or "")
    if len(password) < 6 or len(password) > 100:
        return err("Le mot de passe provisoire doit contenir au moins 6 caractères.")
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND staff_scope='validator'", (uid,)).fetchone():
        return err("Validateur introuvable.", 404)
    db.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (generate_password_hash(password), uid))
    db.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/admin/validators/<int:uid>")
@auth_required("admin")
def admin_delete_validator(uid):
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND staff_scope='validator'", (uid,)).fetchone():
        return err("Validateur introuvable.", 404)
    db.execute("DELETE FROM message_files WHERE message_id IN (SELECT id FROM messages WHERE user_id=?)", (uid,))
    for t in ("tokens", "push_subscriptions", "password_resets", "announcement_reads", "messages"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
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


@app.get("/api/admin/users/connected")
@auth_required("admin")
def admin_users_connected():
    """Chauffeurs et clients ayant ouvert une session (connexion par mot de passe) entre deux dates."""
    d_from = clean(request.args.get("from"), 10)
    d_to = clean(request.args.get("to"), 10)
    try:
        datetime.strptime(d_from or "", "%Y-%m-%d")
        datetime.strptime(d_to or "", "%Y-%m-%d")
    except ValueError:
        return err("Indiquez une date de début et de fin valides (AAAA-MM-JJ).")
    start, end = d_from + " 00:00:00", d_to + " 23:59:59"
    if start > end:
        start, end = end[:10] + " 00:00:00", start[:10] + " 23:59:59"
    role = request.args.get("role")
    db = get_db()
    sql = """SELECT u.id, u.role, u.name, u.phone, u.is_blocked, COUNT(h.id) AS n, MAX(h.at) AS last_at
             FROM login_history h JOIN users u ON u.id = h.user_id
             WHERE h.at BETWEEN ? AND ? AND u.role != 'admin'"""
    params = [start, end]
    if role in ("client", "driver"):
        sql += " AND u.role=?"
        params.append(role)
    sql += " GROUP BY u.id ORDER BY last_at DESC LIMIT 500"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.get("/api/admin/frequentation")
@auth_required("admin")
def admin_frequentation():
    """Fréquentation d'après le journal des connexions : personnes actives par jour, taux sur les inscrits."""
    try:
        days = max(1, min(int(request.args.get("days") or 7), 90))
    except ValueError:
        days = 7
    db = get_db()
    start_day = (now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    start = start_day + " 00:00:00"
    rows = db.execute("""SELECT substr(h.at,1,10) AS day, u.role AS role,
                                COUNT(DISTINCT h.user_id) AS people, COUNT(*) AS opens
                         FROM login_history h JOIN users u ON u.id = h.user_id
                         WHERE h.at >= ? AND u.role != 'admin'
                         GROUP BY substr(h.at,1,10), u.role""", (start,)).fetchall()
    per = {}
    for r in rows:
        d = per.setdefault(r["day"], {"client": 0, "driver": 0, "opens": 0})
        d[r["role"]] = r["people"]
        d["opens"] += r["opens"]
    series = []
    for i in range(days):
        day = (now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        d = per.get(day, {"client": 0, "driver": 0, "opens": 0})
        series.append({"day": day, "clients": d["client"], "drivers": d["driver"], "opens": d["opens"]})
    active = db.execute("""SELECT u.role AS role, COUNT(DISTINCT h.user_id) AS n FROM login_history h
                           JOIN users u ON u.id = h.user_id WHERE h.at >= ? AND u.role != 'admin' GROUP BY u.role""",
                        (start,)).fetchall()
    active = {r["role"]: r["n"] for r in active}
    reg = db.execute("SELECT role, COUNT(*) AS n FROM users WHERE role != 'admin' AND is_blocked=0 GROUP BY role").fetchall()
    reg = {r["role"]: r["n"] for r in reg}
    today = now().strftime("%Y-%m-%d")
    t = db.execute("SELECT COUNT(DISTINCT user_id) AS n FROM login_history h JOIN users u ON u.id=h.user_id "
                   "WHERE h.at >= ? AND u.role != 'admin'", (today + " 00:00:00",)).fetchone()
    return jsonify({
        "days": days, "series": series,
        "active": {"clients": active.get("client", 0), "drivers": active.get("driver", 0)},
        "registered": {"clients": reg.get("client", 0), "drivers": reg.get("driver", 0)},
        "today": t["n"] if t else 0,
        "opens": sum(x["opens"] for x in series),
    })


@app.get("/api/admin/users/<int:uid>/history")
@auth_required("admin")
def admin_user_history(uid):
    db = get_db()
    u = db.execute("SELECT id FROM users WHERE id=? AND role!='admin'", (uid,)).fetchone()
    if not u:
        return err("Utilisateur introuvable.", 404)
    rows = db.execute("SELECT at, ip, device, kind FROM login_history WHERE user_id=? ORDER BY id DESC LIMIT 200", (uid,)).fetchall()
    return jsonify({"connexions": [r["at"] for r in rows], "details": [dict(r) for r in rows]})


@app.get("/api/admin/logins")
@auth_required("admin")
def admin_logins():
    """Journal de TOUTES les connexions, de la plus récente à la plus ancienne."""
    role = request.args.get("role")
    sql = """SELECT h.id, h.at, h.ip, h.device, h.kind, u.id AS user_id, u.name, u.phone, u.role
             FROM login_history h JOIN users u ON u.id = h.user_id WHERE u.role != 'admin'"""
    params = []
    if role in ("client", "driver"):
        sql += " AND u.role=?"
        params.append(role)
    try:
        before = int(request.args.get("before") or 0)
    except ValueError:
        before = 0
    if before:
        sql += " AND h.id < ?"
        params.append(before)
    sql += " ORDER BY h.id DESC LIMIT 100"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify({"logins": [dict(r) for r in rows]})


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



# --- Copies de sécurité automatiques (corbeille) -------------------------------
def save_safety_copy(db, kind, label, payload):
    """Garde une copie compressée avant une action irréversible. On ne garde que les plus récentes."""
    raw = base64.b64encode(zlib.compress(json.dumps(payload, ensure_ascii=False, default=str).encode(), 6)).decode()
    db.execute("INSERT INTO safety_copies(created_at,kind,label,data) VALUES(?,?,?,?)", (ts(), kind, label, raw))
    db.execute("DELETE FROM safety_copies WHERE kind='user_delete' AND created_at<?", (ts(now() - timedelta(days=30)),))
    old = db.execute("SELECT id FROM safety_copies WHERE kind='restore' ORDER BY id DESC").fetchall()
    for r in old[3:]:
        db.execute("DELETE FROM safety_copies WHERE id=?", (r["id"],))
    db.commit()


def load_safety_copy(row):
    return json.loads(zlib.decompress(base64.b64decode(row["data"])).decode())


@app.get("/api/admin/safety-copies")
@auth_required("admin")
def admin_safety_copies():
    rows = get_db().execute("SELECT id, created_at, kind, label FROM safety_copies ORDER BY id DESC LIMIT 60").fetchall()
    return jsonify({"copies": [dict(r) for r in rows]})


@app.get("/api/admin/safety-copies/<int:cid>/download")
@auth_required("admin")
def admin_safety_download(cid):
    row = get_db().execute("SELECT * FROM safety_copies WHERE id=? AND kind='restore'", (cid,)).fetchone()
    if not row:
        return err("Copie introuvable.", 404)
    payload = json.dumps(load_safety_copy(row), ensure_ascii=False)
    return Response(payload, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="sunrise-avant-restauration-{row["created_at"][:10]}.json"', "Cache-Control": "no-store"})


@app.post("/api/admin/safety-copies/<int:cid>/restore-user")
@auth_required("admin")
def admin_restore_user(cid):
    """Remet un compte supprimé par erreur (même mot de passe, mêmes messages et courses)."""
    db = get_db()
    row = db.execute("SELECT * FROM safety_copies WHERE id=? AND kind='user_delete'", (cid,)).fetchone()
    if not row:
        return err("Copie introuvable.", 404)
    data = load_safety_copy(row)
    user = data["user"]
    if db.execute("SELECT 1 FROM users WHERE phone=? OR id=?", (user["phone"], user["id"])).fetchone():
        return err("Ce numéro (ou cette identité) existe déjà : impossible de restaurer ce compte.", 409)

    def put(table, rec):
        cols = [c for c in table_columns(db, table) if c in rec]
        db.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", [rec[c] for c in cols])
    try:
        put("users", user)
        if data.get("driver"):
            d = dict(data["driver"]); d["availability"] = "unavailable"
            put("drivers", d)
        for m in data.get("messages", []):
            put("messages", m)
        for r in data.get("rides", []):
            if r.get("status") in ("open", "accepted"):
                r = {**r, "status": "cancelled", "closed_at": ts()}
            r = {**r, "driver_id": r.get("driver_id") if db.execute("SELECT 1 FROM users WHERE id=?", (r.get("driver_id"),)).fetchone() else None}
            put("ride_requests", r)
        db.execute("DELETE FROM safety_copies WHERE id=?", (cid,))
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Échec de la restauration d'un compte")
        return err("La restauration du compte a échoué. Rien n'a été modifié.", 500)
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<int:uid>")
@auth_required("admin")
def admin_delete_user(uid):
    """Suppression DÉFINITIVE d'un compte client ou chauffeur et de ses données."""
    if clean(body().get("confirm"), 20) != "SUPPRIMER":
        return err("Confirmation manquante : tapez SUPPRIMER.", 400)
    db = get_db()
    u = db.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["role"] == "admin":
        return err("Utilisateur introuvable.", 404)
    # Copie de sécurité (corbeille, 30 jours) avant d'effacer quoi que ce soit
    urow = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    save_safety_copy(db, "user_delete", f"{urow['name']} ({'chauffeur' if u['role'] == 'driver' else 'client'}) · {urow['phone']}", {
        "user": dict(urow),
        "driver": (lambda r: dict(r) if r else None)(db.execute("SELECT * FROM drivers WHERE user_id=?", (uid,)).fetchone()),
        "messages": [dict(r) for r in db.execute("SELECT * FROM messages WHERE user_id=?", (uid,)).fetchall()],
        "rides": [dict(r) for r in db.execute("SELECT * FROM ride_requests WHERE client_id=?", (uid,)).fetchall()],
    })
    # Signalements faits par ou contre cette personne, puis ceux liés à ses demandes de course
    db.execute("DELETE FROM reports WHERE reporter_id=? OR target_user_id=?", (uid, uid))
    db.execute("UPDATE reports SET request_id=NULL WHERE request_id IN (SELECT id FROM ride_requests WHERE client_id=?)", (uid,))
    db.execute("DELETE FROM ride_requests WHERE client_id=?", (uid,))
    # Courses où elle était chauffeur : l'historique des clients reste, sans le chauffeur
    db.execute("UPDATE ride_requests SET status='cancelled', closed_at=? WHERE driver_id=? AND status='accepted'", (ts(), uid))
    db.execute("UPDATE ride_requests SET driver_id=NULL WHERE driver_id=?", (uid,))
    db.execute("DELETE FROM message_files WHERE message_id IN (SELECT id FROM messages WHERE user_id=?)", (uid,))
    for t in ("tokens", "push_subscriptions", "password_resets", "announcement_reads", "messages", "drivers"):
        db.execute(f"DELETE FROM {t} WHERE " + ("user_id=?"), (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
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


# --- Sauvegarde ----------------------------------------------------------------
# Les sessions (table tokens) ne sont pas sauvegardées : chacun se reconnecte après une restauration.
BACKUP_TABLES = [("users", "id"), ("drivers", "user_id"), ("ride_requests", "id"), ("reports", "id"),
                 ("announcements", "id"), ("announcement_reads", "user_id, announcement_id"), ("messages", "id")]


def export_all(db):
    data = {"format": "sunrise-backup-1", "created_at": ts(), "engine": "postgresql" if USE_PG else "sqlite", "tables": {}}
    for table, order in BACKUP_TABLES:
        data["tables"][table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()]
    return data


class BackupError(ValueError):
    """Fichier de sauvegarde refusé (message lisible par l'administrateur)."""


MAX_BACKUP_ROWS = 200_000
SEQUENCE_TABLES = ("users", "ride_requests", "reports", "announcements", "messages")


def table_columns(db, table):
    cur = db.execute(f"SELECT * FROM {table} LIMIT 0")
    return [getattr(d, "name", None) or d[0] for d in cur.description]


def count_rows(db):
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t, _ in BACKUP_TABLES}


def validate_backup(db, data):
    """Vérifie le fichier AVANT de toucher à la base. Renvoie un résumé ; lève BackupError sinon.
    Les noms de colonnes sont comparés à ceux de la vraie table : rien d'inconnu n'atteint le SQL."""
    if not isinstance(data, dict) or data.get("format") != "sunrise-backup-1" or not isinstance(data.get("tables"), dict):
        raise BackupError("Ce fichier n'est pas une sauvegarde Sunrise Space.")
    tables = data["tables"]
    total = 0
    for table, _order in BACKUP_TABLES:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            raise BackupError(f"Sauvegarde abîmée (table « {table} »).")
        known = set(table_columns(db, table))
        total += len(rows)
        for row in rows:
            if not isinstance(row, dict) or not row:
                raise BackupError(f"Sauvegarde abîmée (table « {table} »).")
            unknown = set(row) - known
            if unknown:
                raise BackupError(f"Sauvegarde incompatible avec cette version de l'application (table « {table} »). "
                                  "Elle vient sans doute d'une version plus récente.")
            for v in row.values():
                if v is not None and not isinstance(v, (str, int, float, bool)):
                    raise BackupError(f"Sauvegarde abîmée (table « {table} »).")
    if total > MAX_BACKUP_ROWS:
        raise BackupError("Sauvegarde trop volumineuse pour être restaurée depuis l'application.")
    admins = [u for u in tables.get("users", []) if u.get("role") == "admin" and u.get("staff_scope") != "validator" and not u.get("is_blocked") and u.get("password_hash")]
    if not admins:
        raise BackupError("Cette sauvegarde ne contient aucun compte administrateur actif : vous ne pourriez plus vous connecter.")
    return {
        "created_at": data.get("created_at"),
        "backup": {t: len(tables.get(t, [])) for t, _ in BACKUP_TABLES},
        "current": count_rows(db),
        "admin_phones": [u.get("phone") for u in admins],
    }


def restore_all(db, data):
    """Remplace TOUT le contenu par celui de la sauvegarde, dans une seule transaction (l'appelant valide ou annule)."""
    for table in ["call_signals", "calls", "tokens", "push_subscriptions", "password_resets"] + [t for t, _ in BACKUP_TABLES][::-1]:
        db.execute(f"DELETE FROM {table}")
    for table, _order in BACKUP_TABLES:
        groups = {}
        for row in data["tables"].get(table, []):
            groups.setdefault(tuple(row), []).append([row[c] for c in row])
        for cols, values in groups.items():
            db.executemany(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", values)
    if USE_PG:   # remet les compteurs d'identifiants après les insertions avec id explicite
        for table in SEQUENCE_TABLES:
            db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}")


@app.post(RESTORE_PATH)
@auth_required("admin")
def admin_restore():
    """Étape 1 (dry_run, par défaut) : vérifie le fichier et renvoie un aperçu, sans rien modifier.
    Étape 2 (dry_run=false + confirmation='RESTAURER') : remplace les données ; tout le monde doit se reconnecter."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("backup"), dict):
        return err("Fichier de sauvegarde illisible.", 400)
    db = get_db()
    try:
        summary = validate_backup(db, body["backup"])
    except BackupError as e:
        return err(str(e), 400)
    if body.get("dry_run") is not False:
        return jsonify({"ok": True, "dry_run": True, **summary})
    if body.get("confirmation") != "RESTAURER":
        return err("Tapez RESTAURER en majuscules pour confirmer.", 400)
    try:
        save_safety_copy(db, "restore", "Avant la restauration du " + ts(), export_all(db))
        restore_all(db, body["backup"])
        db.commit()
    except Exception:
        db.rollback()
        app.logger.exception("Échec de la restauration")
        return err("La restauration a échoué. Rien n'a été modifié.", 500)
    print(f"Restauration effectuée par l'admin {g.user['id']} : {summary['backup']}")
    return jsonify({"ok": True, "dry_run": False, **summary})


@app.get("/api/admin/backup")
@auth_required("admin")
def admin_backup():
    payload = json.dumps(export_all(get_db()), ensure_ascii=False)
    name = "sunrise-sauvegarde-%s.json" % now().strftime("%Y-%m-%d")
    return Response(payload, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# --- Messagerie : annonces + boîte de réception --------------------------------
MSG_MAX = 1000
ANN_TITLE_MAX = 80
ANN_BODY_MAX = 1500
AUDIENCES = {"all": "Tout le monde", "client": "Clients", "driver": "Chauffeurs"}


def ann_cutoff():
    """Les annonces restent visibles 30 jours dans la boîte de réception."""
    return ts(now() - timedelta(days=30))


def unread_for_user(db, user):
    ann = db.execute(
        """SELECT COUNT(*) FROM announcements a
           WHERE a.audience IN ('all', ?) AND a.created_at>=?
             AND NOT EXISTS (SELECT 1 FROM announcement_reads r WHERE r.announcement_id=a.id AND r.user_id=?)""",
        (user["role"], ann_cutoff(), user["id"]),
    ).fetchone()[0]
    msg = db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id=? AND sender='admin' AND read_at IS NULL", (user["id"],)
    ).fetchone()[0]
    return ann + msg


@app.get("/api/inbox/unread")
@auth_required()
def inbox_unread():
    db = get_db()
    if g.user["role"] == "admin":
        n = db.execute("SELECT COUNT(*) FROM messages WHERE sender='user' AND read_at IS NULL").fetchone()[0]
    else:
        n = unread_for_user(db, g.user)
    return jsonify({"unread": n})


FILE_KINDS = {  # type MIME accepté -> (genre, libellé par défaut)
    "image/jpeg": "image", "image/png": "image", "image/webp": "image", "image/gif": "image",
    "video/mp4": "video", "video/quicktime": "video", "video/webm": "video",
    "audio/mp4": "audio", "audio/x-m4a": "audio", "audio/aac": "audio", "audio/mpeg": "audio", "audio/webm": "audio", "audio/ogg": "audio",
    "application/pdf": "pdf",
}
FILE_LABEL = {"image": "📷 Photo", "video": "🎬 Vidéo", "audio": "🎤 Message vocal", "pdf": "📄 Document PDF"}


def attach_files(db, msgs):
    """Ajoute à chaque message son fichier joint (sans le contenu)."""
    ids = [m["id"] for m in msgs]
    if not ids:
        return msgs
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT message_id, kind, mime, name, size, state FROM message_files WHERE message_id IN ({marks})", ids).fetchall()
    by = {r["message_id"]: {"kind": r["kind"], "mime": r["mime"], "name": r["name"], "size": r["size"], "state": r["state"]} for r in rows}
    for m in msgs:
        if m["id"] in by:
            m["file"] = by[m["id"]]
    return msgs


@app.post("/api/admin/conversations/<int:uid>/files")
@auth_required("admin")
def admin_send_file(uid):
    request.max_content_length = FILE_MAX_BYTES + 4096
    mime = (request.mimetype or "").lower()
    kind = FILE_KINDS.get(mime)
    if not kind:
        return err("Type de fichier non accepté (photo, vidéo, vocal ou PDF).", 415)
    data = request.get_data()
    if not data:
        return err("Fichier vide.")
    if len(data) > FILE_MAX_BYTES:
        return err(f"Fichier trop lourd (maximum {FILE_MAX_BYTES // (1024 * 1024)} Mo).", 413)
    if kind == "pdf" and not data.startswith(b"%PDF"):
        return err("Ce fichier n'est pas un PDF valide.")
    db = get_db()
    u = db.execute("SELECT id FROM users WHERE id=? AND role!='admin'", (uid,)).fetchone()
    if not u:
        return err("Utilisateur introuvable.", 404)
    name = clean(request.args.get("name"), 80) or FILE_LABEL[kind].split(" ", 1)[1]
    caption = clean_text(request.args.get("caption"), MSG_MAX)
    label = caption or (f"📄 {name}" if kind == "pdf" else FILE_LABEL[kind])
    mid = insert(db, "INSERT INTO messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", (uid, "admin", label, ts()))
    db.execute("INSERT INTO message_files(message_id,kind,mime,name,size,data,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (mid, kind, mime, name, len(data), data, "pending", ts()))
    db.commit()
    notify_users(db, [uid], "📎 Message de Sunrise Space", label[:120], tag=f"admmsg-{uid}")
    return jsonify({"ok": True, "id": mid}), 201


@app.get("/api/inbox/files/<int:mid>")
@auth_required("client", "driver")
def inbox_file(mid):
    db = get_db()
    r = db.execute("""SELECT f.mime, f.data, f.state FROM message_files f JOIN messages m ON m.id=f.message_id
                      WHERE f.message_id=? AND m.user_id=? AND m.sender='admin'""", (mid, g.user["id"])).fetchone()
    if not r:
        return err("Fichier introuvable.", 404)
    if r["state"] != "pending" or r["data"] is None:
        return err("Ce fichier n'est plus disponible sur le serveur.", 410)
    resp = Response(bytes(r["data"]), mimetype=r["mime"])
    resp.headers["Content-Disposition"] = "attachment"
    return resp


@app.post("/api/inbox/files/<int:mid>/ack")
@auth_required("client", "driver")
def inbox_file_ack(mid):
    """Le téléphone a bien reçu le fichier : le serveur l'efface (rien n'est conservé)."""
    db = get_db()
    db.execute("""UPDATE message_files SET data=NULL, state='downloaded' WHERE message_id=? AND state='pending'
                  AND message_id IN (SELECT id FROM messages WHERE user_id=? AND sender='admin')""", (mid, g.user["id"]))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/inbox")
@auth_required("client", "driver")
def inbox():
    db = get_db()
    u = g.user
    anns = db.execute(
        """SELECT a.id, a.title, a.body, a.created_at,
                  CASE WHEN EXISTS (SELECT 1 FROM announcement_reads r WHERE r.announcement_id=a.id AND r.user_id=?)
                       THEN 0 ELSE 1 END AS unread
           FROM announcements a WHERE a.audience IN ('all', ?) AND a.created_at>=?
           ORDER BY a.id DESC LIMIT 50""",
        (u["id"], u["role"], ann_cutoff()),
    ).fetchall()
    msgs = db.execute(
        "SELECT id, sender, body, created_at, read_at FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 100",
        (u["id"],),
    ).fetchall()
    msgs = attach_files(db, [dict(m) for m in reversed(msgs)])
    for m in msgs:
        m["unread"] = 1 if (m["sender"] == "admin" and not m["read_at"]) else 0
    return jsonify({"announcements": [dict(a) for a in anns], "messages": msgs})


@app.post("/api/inbox/read")
@auth_required("client", "driver")
def inbox_read():
    db = get_db()
    u = g.user
    db.execute(
        """INSERT INTO announcement_reads(user_id, announcement_id)
           SELECT ?, a.id FROM announcements a
           WHERE a.audience IN ('all', ?) AND a.created_at>=?
             AND NOT EXISTS (SELECT 1 FROM announcement_reads r WHERE r.announcement_id=a.id AND r.user_id=?)""",
        (u["id"], u["role"], ann_cutoff(), u["id"]),
    )
    db.execute(
        "UPDATE messages SET read_at=? WHERE user_id=? AND sender='admin' AND read_at IS NULL", (ts(), u["id"])
    )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/inbox/messages")
@auth_required("client", "driver")
def inbox_send():
    text = clean_text(body().get("body"), MSG_MAX)
    if not text:
        return err("Écrivez votre message.")
    db = get_db()
    recent = db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id=? AND sender='user' AND created_at>?",
        (g.user["id"], ts(now() - timedelta(hours=1))),
    ).fetchone()[0]
    if recent >= 20:
        return err("Trop de messages envoyés. Réessayez dans un moment.", 429)
    db.execute("INSERT INTO messages(user_id,sender,body,created_at) VALUES(?,?,?,?)",
               (g.user["id"], "user", text, ts()))
    db.commit()
    who = "client" if g.user["role"] == "client" else "chauffeur"
    notify_staff(db, f"✉️ Message de {g.user['name']}", f"({who}) {text[:120]}", f"staff-msg-{g.user['id']}", validators=False)
    return jsonify({"ok": True}), 201


@app.post("/api/admin/announcements")
@auth_required("admin")
def admin_announce():
    data = body()
    audience = data.get("audience", "all")
    title = clean(data.get("title"), ANN_TITLE_MAX)
    text = clean_text(data.get("body"), ANN_BODY_MAX)
    if audience not in AUDIENCES:
        return err("Destinataires invalides.")
    if not title or not text:
        return err("Ajoutez un titre et un message.")
    db = get_db()
    aid = db.insert("INSERT INTO announcements(audience,title,body,created_at) VALUES(?,?,?,?)",
                    (audience, title, text, ts()))
    db.commit()
    return jsonify({"ok": True, "id": aid}), 201


@app.get("/api/admin/announcements")
@auth_required("admin")
def admin_announcements():
    db = get_db()
    rows = db.execute(
        """SELECT a.id, a.audience, a.title, a.body, a.created_at,
                  (SELECT COUNT(*) FROM announcement_reads r WHERE r.announcement_id=a.id) AS reads,
                  (SELECT COUNT(*) FROM users u WHERE u.role!='admin' AND u.created_at<=a.created_at
                     AND (a.audience='all' OR u.role=a.audience)) AS audience_size
           FROM announcements a ORDER BY a.id DESC LIMIT 100"""
    ).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["audience_label"] = AUDIENCES.get(r["audience"], r["audience"])
    return jsonify({"announcements": out})


@app.delete("/api/admin/announcements/<int:aid>")
@auth_required("admin")
def admin_announcement_delete(aid):
    db = get_db()
    db.execute("DELETE FROM announcement_reads WHERE announcement_id=?", (aid,))
    db.execute("DELETE FROM announcements WHERE id=?", (aid,))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/conversations")
@auth_required("admin")
def admin_conversations():
    rows = get_db().execute(
        """SELECT u.id, u.name, u.phone, u.role, m.body AS last_body, m.created_at AS last_at, m.sender AS last_sender,
                  (SELECT COUNT(*) FROM messages x WHERE x.user_id=u.id AND x.sender='user' AND x.read_at IS NULL) AS unread
           FROM users u JOIN messages m ON m.id=(SELECT MAX(id) FROM messages WHERE user_id=u.id)
           ORDER BY m.id DESC LIMIT 100"""
    ).fetchall()
    return jsonify({"conversations": [dict(r) for r in rows]})


@app.get("/api/admin/conversations/<int:uid>")
@auth_required("admin")
def admin_conversation(uid):
    db = get_db()
    u = db.execute("SELECT id, name, phone, role FROM users WHERE id=? AND role!='admin'", (uid,)).fetchone()
    if not u:
        return err("Utilisateur introuvable.", 404)
    db.execute("UPDATE messages SET read_at=? WHERE user_id=? AND sender='user' AND read_at IS NULL", (ts(), uid))
    db.commit()
    msgs = db.execute(
        "SELECT id, sender, body, created_at FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 200", (uid,)
    ).fetchall()
    return jsonify({"user": dict(u), "messages": attach_files(db, [dict(m) for m in reversed(msgs)])})


@app.post("/api/admin/conversations/<int:uid>")
@auth_required("admin")
def admin_reply(uid):
    text = clean_text(body().get("body"), MSG_MAX)
    if not text:
        return err("Écrivez votre message.")
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=? AND role!='admin'", (uid,)).fetchone():
        return err("Utilisateur introuvable.", 404)
    db.execute("INSERT INTO messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", (uid, "admin", text, ts()))
    db.commit()
    notify_users(db, [uid], "✉️ Message de Sunrise Space", text[:120], tag=f"admmsg-{uid}")
    return jsonify({"ok": True}), 201


# --- Erreurs JSON -------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return err("Ressource introuvable.", 404)
    resp = send_from_directory(BASE_DIR, "404.html")
    resp.status_code = 404
    return resp


@app.errorhandler(413)
def too_large(_e):
    return err("Requête trop volumineuse.", 413)


@app.errorhandler(405)
def bad_method(_e):
    return err("Méthode non autorisée.", 405)


@app.errorhandler(500)
def server_error(_e):
    return err("Erreur du serveur. Réessayez.", 500)


if not os.environ.get("SUNRISE_SKIP_INIT"):   # SUNRISE_SKIP_INIT : utilisé par restore_backup.py
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
