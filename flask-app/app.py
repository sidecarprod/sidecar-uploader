import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone

from flask import (
    Flask, abort, g, redirect, render_template, request,
    session, url_for, jsonify
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

UPLOAD_ROOT = os.environ["UPLOAD_ROOT"]
DB_PATH = os.environ["DB_PATH"]
TUSD_STAGING_DIR = os.environ.get("TUSD_STAGING_DIR", "/data/tusd-staging")
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
BRAND_NAME = os.environ.get("BRAND_NAME", "Sidecar Productions")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            allowed_ext TEXT NOT NULL,        -- JSON array, e.g. [".mp4", ".pdf"]
            max_file_size_mb INTEGER NOT NULL,
            folder_path TEXT NOT NULL,
            confirmation_message TEXT NOT NULL DEFAULT 'Thanks! Your file was received.',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def slugify(text):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def sanitize_folder_name(name):
    # Keep it human-readable (spaces are fine on the NAS) but strip anything
    # that would break a path or look like traversal.
    name = name.strip()
    name = re.sub(r'[\/\\:\*\?"<>\|]', "", name)
    name = re.sub(r"\.\.+", ".", name)
    return name or "event"


def parse_ext_list(raw):
    """Accepts 'mp4, mov, .pdf' style input, returns ['.mp4', '.mov', '.pdf']"""
    parts = [p.strip().lower() for p in raw.replace("\n", ",").split(",") if p.strip()]
    cleaned = []
    for p in parts:
        if not p.startswith("."):
            p = "." + p
        cleaned.append(p)
    return cleaned


def get_event(slug):
    row = get_db().execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    return {
        "slug": row["slug"],
        "name": row["name"],
        "title": row["title"],
        "description": row["description"],
        "allowed_ext": json.loads(row["allowed_ext"]),
        "max_file_size_mb": row["max_file_size_mb"],
        "folder_path": row["folder_path"],
        "confirmation_message": row["confirmation_message"],
        "created_at": row["created_at"],
    }


def require_admin():
    if session.get("admin_ok") is True:
        return
    token = request.args.get("token") or request.form.get("token") or request.headers.get("X-Admin-Token")
    if token and token == ADMIN_TOKEN:
        session["admin_ok"] = True
        return
    abort(401)


# --------------------------------------------------------------------------
# Public event pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return f"{BRAND_NAME} file upload portal.", 200


@app.route("/<slug>")
def event_page(slug):
    event = get_event(slug)
    if event is None:
        return render_template("not_found.html", brand=BRAND_NAME), 404

    client_config = {
        "eventSlug": event["slug"],
        "allowedExt": event["allowed_ext"],
        "maxFileSizeMB": event["max_file_size_mb"],
        "confirmationMessage": event["confirmation_message"],
    }
    return render_template(
        "upload.html",
        brand=BRAND_NAME,
        event=event,
        client_config_json=json.dumps(client_config),
    )


# --------------------------------------------------------------------------
# tusd webhook receiver
# https://tus.github.io/tusd/advanced-topics/hooks/
# --------------------------------------------------------------------------

@app.route("/hooks", methods=["POST"])
def tusd_hooks():
    payload = request.get_json(silent=True) or {}
    hook_name = payload.get("Type") or request.headers.get("Hook-Name", "")

    # tusd v2 nests the upload under "Event"; older versions send it flat.
    # Handle both so this doesn't silently break on a tusd version bump.
    event_payload = payload.get("Event")
    if not isinstance(event_payload, dict):
        event_payload = payload
    upload = event_payload.get("Upload", {})
    metadata = upload.get("MetaData", {}) or {}
    app.logger.info(f"tusd hook: name={hook_name!r} metadata={metadata!r}")

    if hook_name == "pre-create":
        return handle_pre_create(upload, metadata)

    if hook_name == "post-finish":
        handle_post_finish(upload, metadata)
        return jsonify({}), 200

    # pre-finish, post-create, post-terminate, post-receive: no-op
    return jsonify({}), 200


def reject_upload(status_code, message):
    # tusd expects 200 from the hook itself; RejectUpload + HTTPResponse
    # tells tusd what status/body to actually send back to the browser.
    return jsonify({
        "RejectUpload": True,
        "HTTPResponse": {
            "StatusCode": status_code,
            "Body": message,
        },
    }), 200


def handle_pre_create(upload, metadata):
    slug = metadata.get("eventSlug", "")
    event = get_event(slug) if slug else None

    if event is None:
        return reject_upload(400, "Unknown or missing event link.")

    filename = metadata.get("filename", "")
    ext = os.path.splitext(filename)[1].lower()
    if not ext or ext not in event["allowed_ext"]:
        allowed = ", ".join(event["allowed_ext"])
        return reject_upload(400, f"File type '{ext or 'unknown'}' isn't allowed here. Allowed types: {allowed}")

    size = upload.get("Size")
    max_bytes = event["max_file_size_mb"] * 1024 * 1024
    if isinstance(size, int) and size > max_bytes:
        return reject_upload(400, f"File exceeds the {event['max_file_size_mb']} MB limit for this event.")

    return jsonify({}), 200


def handle_post_finish(upload, metadata):
    slug = metadata.get("eventSlug", "")
    event = get_event(slug) if slug else None
    if event is None:
        return  # shouldn't happen since pre-create already validated

    upload_id = upload.get("ID", "")
    storage = upload.get("Storage") or {}
    src_path = storage.get("Path")

    if not src_path or not os.path.exists(src_path):
        # fall back to the standard tusd staging layout
        src_path = os.path.join(TUSD_STAGING_DIR, upload_id)
        if not os.path.exists(src_path):
            return

    os.makedirs(event["folder_path"], exist_ok=True)

    filename = metadata.get("filename") or upload_id
    dest_path = os.path.join(event["folder_path"], filename)
    dest_path = avoid_collision(dest_path, upload_id)

    shutil.copyfile(src_path, dest_path)

    # clean up tusd's staging copy (.bin + .info)
    for suffix in ("", ".info"):
        p = src_path + suffix if suffix else src_path
        try:
            os.remove(p)
        except OSError:
            pass


def avoid_collision(dest_path, upload_id):
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(dest_path)
    short_id = upload_id[:8] if upload_id else str(int(time.time()))
    return f"{base}-{short_id}{ext}"


# --------------------------------------------------------------------------
# Admin: create/list event links
# --------------------------------------------------------------------------

@app.route("/admin", methods=["GET"])
def admin_home():
    require_admin()
    events = get_db().execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    return render_template("admin.html", brand=BRAND_NAME, events=events, token=ADMIN_TOKEN)


@app.route("/admin/events", methods=["POST"])
def admin_create_event():
    require_admin()

    name = request.form.get("name", "").strip()
    slug = slugify(request.form.get("slug", "") or name)
    title = request.form.get("title", "").strip() or name
    description = request.form.get("description", "").strip()
    ext_raw = request.form.get("allowed_ext", "")
    max_size = int(request.form.get("max_file_size_mb", "15360") or 15360)
    confirmation_message = request.form.get(
        "confirmation_message", ""
    ).strip() or "Thanks! Your file was received."

    if not name or not slug:
        abort(400, "Event name is required.")

    allowed_ext = parse_ext_list(ext_raw)
    if not allowed_ext:
        abort(400, "At least one allowed file extension is required.")

    folder_name = sanitize_folder_name(name)
    folder_path = os.path.join(UPLOAD_ROOT, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    get_db().execute(
        """INSERT INTO events
           (slug, name, title, description, allowed_ext, max_file_size_mb,
            folder_path, confirmation_message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(slug) DO UPDATE SET
             name=excluded.name, title=excluded.title, description=excluded.description,
             allowed_ext=excluded.allowed_ext, max_file_size_mb=excluded.max_file_size_mb,
             folder_path=excluded.folder_path, confirmation_message=excluded.confirmation_message
        """,
        (
            slug, name, title, description, json.dumps(allowed_ext), max_size,
            folder_path, confirmation_message, datetime.now(timezone.utc).isoformat(),
        ),
    )
    get_db().commit()

    return redirect(url_for("admin_home", token=ADMIN_TOKEN))


@app.route("/admin/events/<slug>/delete", methods=["POST"])
def admin_delete_event(slug):
    require_admin()
    get_db().execute("DELETE FROM events WHERE slug = ?", (slug,))
    get_db().commit()
    return redirect(url_for("admin_home", token=ADMIN_TOKEN))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    return redirect(url_for("admin_home"))


@app.errorhandler(401)
def unauthorized(e):
    return render_template("admin_login.html", brand=BRAND_NAME), 401
