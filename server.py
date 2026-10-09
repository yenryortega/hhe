from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2, psycopg2.extras, os, traceback, re, hmac, time

app = Flask(__name__)

# ── CORS ───────────────────────────────────────────────────────────────
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Config ─────────────────────────────────────────────────────────────
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "HILTON2026")
# PIN for the Analytics section of admin.html. Set it in Railway → Variables.
# If it is not set, analytics stays locked (no default PIN on purpose).
ANALYTICS_PIN  = os.environ.get("ANALYTICS_PIN", "").strip()


def get_db():
    return psycopg2.connect(DATABASE_URL)


def run_sql(cur, label, sql):
    """Execute a SQL statement, printing errors without raising."""
    try:
        cur.execute(sql)
    except Exception as e:
        print(f"[init_db] WARNING — step '{label}' failed: {e}")


def init_db():
    if not DATABASE_URL:
        print("[init_db] ERROR — DATABASE_URL is not set. Skipping DB init.")
        return

    with get_db() as con:
        with con.cursor() as cur:

            # 1. Create table if it doesn't exist
            run_sql(cur, "create table", """
                CREATE TABLE IF NOT EXISTS registrations (
                    id          SERIAL PRIMARY KEY,
                    first_name  TEXT    NOT NULL,
                    last_name   TEXT    NOT NULL,
                    email       TEXT    NOT NULL,
                    phone       TEXT,
                    country     TEXT,
                    lang        TEXT      DEFAULT 'en',
                    source      TEXT      DEFAULT 'web',
                    ticket_used BOOLEAN   DEFAULT FALSE,
                    deleted     BOOLEAN   DEFAULT FALSE,
                    deleted_at  TIMESTAMP,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.commit()

            # 2. Add deleted / deleted_at columns to existing installs
            run_sql(cur, "add deleted cols", """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='registrations' AND column_name='deleted'
                    ) THEN
                        ALTER TABLE registrations ADD COLUMN deleted BOOLEAN DEFAULT FALSE;
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='registrations' AND column_name='deleted_at'
                    ) THEN
                        ALTER TABLE registrations ADD COLUMN deleted_at TIMESTAMP;
                    END IF;
                END $$;
            """)
            con.commit()

            # 2b. Add source column to existing installs
            #     'web'   → index.html (con drink ticket)
            #     'media' → media.html (sin ticket)
            run_sql(cur, "add source col", """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='registrations' AND column_name='source'
                    ) THEN
                        ALTER TABLE registrations ADD COLUMN source TEXT DEFAULT 'web';
                    END IF;
                END $$;
            """)
            con.commit()

            # 3. Make room column nullable (existing DB has it NOT NULL)
            run_sql(cur, "room nullable", """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='registrations' AND column_name='room'
                        AND is_nullable = 'NO'
                    ) THEN
                        ALTER TABLE registrations ALTER COLUMN room DROP NOT NULL;
                    END IF;
                END $$;
            """)
            con.commit()

            # 4. Drop old combined unique constraint if it exists
            run_sql(cur, "drop old constraint", """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'registrations_first_name_last_name_email_key'
                    ) THEN
                        ALTER TABLE registrations
                            DROP CONSTRAINT registrations_first_name_last_name_email_key;
                    END IF;
                END $$;
            """)
            con.commit()

            # 5. Deduplicate by email — keep most recent
            run_sql(cur, "dedup email", """
                DELETE FROM registrations
                WHERE id NOT IN (
                    SELECT MAX(id) FROM registrations GROUP BY email
                );
            """)
            con.commit()

            # 6. Deduplicate by (first_name, last_name) — keep most recent
            run_sql(cur, "dedup name", """
                DELETE FROM registrations
                WHERE id NOT IN (
                    SELECT MAX(id) FROM registrations GROUP BY first_name, last_name
                );
            """)
            con.commit()

            # 7. Add unique constraints if missing
            run_sql(cur, "add unique constraints", """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'registrations_email_key'
                    ) THEN
                        ALTER TABLE registrations
                            ADD CONSTRAINT registrations_email_key UNIQUE (email);
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'registrations_first_name_last_name_key'
                    ) THEN
                        ALTER TABLE registrations
                            ADD CONSTRAINT registrations_first_name_last_name_key
                            UNIQUE (first_name, last_name);
                    END IF;
                END $$;
            """)
            con.commit()

            # 8. Create settings table (key/value store, e.g. wifi_password)
            run_sql(cur, "create settings table", """
                CREATE TABLE IF NOT EXISTS settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.commit()

            # 9. Seed default wifi_password if not present
            run_sql(cur, "seed wifi_password", """
                INSERT INTO settings (key, value)
                VALUES ('wifi_password', '2026aug')
                ON CONFLICT (key) DO NOTHING
            """)
            con.commit()

    print("[init_db] Done.")


# ── Auth helper ────────────────────────────────────────────────────────
def check_admin_auth():
    username = request.headers.get("X-Admin-Username", "")
    password = request.headers.get("X-Admin-Password", "")
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


# ── POST /register ─────────────────────────────────────────────────────
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    first_name = data.get("firstName", "").strip()
    last_name  = data.get("lastName", "").strip()
    email      = data.get("email", "").strip().lower()
    phone      = data.get("phone", "").strip()
    country    = data.get("country", "").strip()
    lang       = data.get("lang", "en").strip()

    # De qué formulario viene el registro. Solo se aceptan valores conocidos:
    # cualquier otra cosa se guarda como 'web' para no ensuciar la columna.
    source     = (data.get("source") or "web").strip().lower()
    if source not in ("web", "media"):
        source = "web"

    if not first_name or not last_name or not email:
        return jsonify({"error": "missing_fields"}), 400

    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    """SELECT id FROM registrations
                       WHERE first_name = %s AND last_name = %s AND deleted = FALSE""",
                    (first_name, last_name)
                )
                if cur.fetchone():
                    return jsonify({"error": "guest_already_registered", "field": "name"}), 409

                cur.execute(
                    "SELECT id FROM registrations WHERE email = %s AND deleted = FALSE",
                    (email,)
                )
                if cur.fetchone():
                    return jsonify({"error": "guest_already_registered", "field": "email"}), 409

                cur.execute(
                    """INSERT INTO registrations
                       (first_name, last_name, email, phone, country, lang, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (first_name, last_name, email, phone, country, lang, source)
                )
            con.commit()
    except psycopg2.errors.UniqueViolation as e:
        field = "email" if "email" in str(e) else "name"
        return jsonify({"error": "guest_already_registered", "field": field}), 409

    return jsonify({"success": True}), 201


# ── GET /check-ticket ──────────────────────────────────────────────────
@app.route("/check-ticket", methods=["GET"])
def check_ticket():
    email  = request.args.get("email", "").strip().lower()
    nombre = request.args.get("nombre", "").strip()

    if not email and not nombre:
        return jsonify({"registered": False, "ticket_used": False})

    with get_db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = None

            if email:
                cur.execute(
                    """SELECT first_name, last_name, ticket_used, created_at
                       FROM registrations WHERE email = %s AND deleted = FALSE""",
                    (email,)
                )
                row = cur.fetchone()

            if not row and nombre:
                parts = nombre.strip().split(" ", 1)
                first = parts[0] if len(parts) > 0 else ""
                last  = parts[1] if len(parts) > 1 else ""
                if first and last:
                    cur.execute(
                        """SELECT first_name, last_name, ticket_used, created_at
                           FROM registrations
                           WHERE first_name = %s AND last_name = %s AND deleted = FALSE""",
                        (first, last)
                    )
                    row = cur.fetchone()

    if not row:
        return jsonify({"registered": False, "ticket_used": False})

    return jsonify({
        "registered":  True,
        "ticket_used": bool(row["ticket_used"]),
        "name":        row["first_name"] + " " + row["last_name"],
        "at":          str(row["created_at"])
    })


# ── POST /use-ticket ───────────────────────────────────────────────────
@app.route("/use-ticket", methods=["POST"])
def use_ticket():
    data   = request.get_json(silent=True) or {}
    email  = data.get("email", "").strip().lower()
    nombre = data.get("nombre", "").strip()

    if not email and not nombre:
        return jsonify({"error": "missing_fields"}), 400

    with get_db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = None

            if email:
                cur.execute(
                    "SELECT id, ticket_used FROM registrations WHERE email = %s AND deleted = FALSE",
                    (email,)
                )
                row = cur.fetchone()

            if not row and nombre:
                parts = nombre.strip().split(" ", 1)
                first = parts[0] if len(parts) > 0 else ""
                last  = parts[1] if len(parts) > 1 else ""
                if first and last:
                    cur.execute(
                        """SELECT id, ticket_used FROM registrations
                           WHERE first_name = %s AND last_name = %s AND deleted = FALSE""",
                        (first, last)
                    )
                    row = cur.fetchone()

            if not row:
                return jsonify({"error": "not_found"}), 404

            if row["ticket_used"]:
                return jsonify({"error": "already_used"}), 409

            cur.execute(
                "UPDATE registrations SET ticket_used = TRUE WHERE id = %s",
                (row["id"],)
            )
        con.commit()

    return jsonify({"success": True}), 200


# ── GET /admin/registrations — active records only ─────────────────────
@app.route("/admin/registrations", methods=["GET"])
def admin_registrations():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    with get_db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT first_name, last_name, email, phone,
                          country, lang, source, ticket_used, created_at
                   FROM registrations
                   WHERE deleted = FALSE
                   ORDER BY created_at DESC"""
            )
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


# ── GET /admin/trash — soft-deleted records ────────────────────────────
@app.route("/admin/trash", methods=["GET"])
def admin_trash():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    with get_db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT first_name, last_name, email, phone,
                          country, lang, source, ticket_used, created_at, deleted_at
                   FROM registrations
                   WHERE deleted = TRUE
                   ORDER BY deleted_at DESC"""
            )
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


# ── POST /admin/soft-delete ────────────────────────────────────────────
@app.route("/admin/soft-delete", methods=["POST"])
def admin_soft_delete():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "missing_email"}), 400

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                """UPDATE registrations
                   SET deleted = TRUE, deleted_at = CURRENT_TIMESTAMP
                   WHERE email = %s AND deleted = FALSE""",
                (email,)
            )
            updated = cur.rowcount
        con.commit()

    if updated == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"success": True}), 200


# ── POST /admin/restore ────────────────────────────────────────────────
@app.route("/admin/restore", methods=["POST"])
def admin_restore():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "missing_email"}), 400

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                """UPDATE registrations
                   SET deleted = FALSE, deleted_at = NULL
                   WHERE email = %s AND deleted = TRUE""",
                (email,)
            )
            updated = cur.rowcount
        con.commit()

    if updated == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"success": True}), 200


# ── DELETE /admin/delete — permanent ──────────────────────────────────
@app.route("/admin/delete", methods=["DELETE"])
def admin_delete():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    data  = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "missing_email"}), 400

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM registrations WHERE email = %s", (email,))
            deleted = cur.rowcount
        con.commit()

    if deleted == 0:
        return jsonify({"error": "not_found"}), 404

    return jsonify({"success": True, "deleted": deleted}), 200


# ── GET /admin/analytics — protected by admin auth + ANALYTICS_PIN ────
#    Query params: from=YYYY-MM-DD, to=YYYY-MM-DD (both optional, JST dates)
#    Header:       X-Analytics-Pin
#    Only active records (deleted = FALSE). Days are grouped in JST.
#    created_at is stored in UTC (Postgres CURRENT_TIMESTAMP on Railway),
#    so it is converted UTC → Asia/Tokyo before taking the date.

# Simple brute-force protection: after 5 wrong PINs from the same IP,
# lock that IP out for 5 minutes. In-memory, resets on redeploy.
_PIN_MAX_FAILS   = 5
_PIN_LOCK_SECS   = 300
_pin_failures    = {}   # ip -> (fail_count, locked_until)
_DATE_RE         = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


@app.route("/admin/analytics", methods=["GET"])
def admin_analytics():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    if not ANALYTICS_PIN:
        return jsonify({"error": "analytics_pin_not_set"}), 503

    ip = _client_ip()
    fails, locked_until = _pin_failures.get(ip, (0, 0))
    now = time.time()
    if locked_until > now:
        return jsonify({"error": "too_many_attempts",
                        "retry_after": int(locked_until - now)}), 429

    pin = request.headers.get("X-Analytics-Pin", "").strip()
    if not hmac.compare_digest(pin.encode(), ANALYTICS_PIN.encode()):
        fails += 1
        if fails >= _PIN_MAX_FAILS:
            _pin_failures[ip] = (0, now + _PIN_LOCK_SECS)
            return jsonify({"error": "too_many_attempts",
                            "retry_after": _PIN_LOCK_SECS}), 429
        _pin_failures[ip] = (fails, 0)
        return jsonify({"error": "invalid_pin",
                        "attempts_left": _PIN_MAX_FAILS - fails}), 403
    _pin_failures.pop(ip, None)

    date_from = request.args.get("from", "").strip() or None
    date_to   = request.args.get("to", "").strip() or None
    for d in (date_from, date_to):
        if d and not _DATE_RE.match(d):
            return jsonify({"error": "invalid_date"}), 400
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    with get_db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """WITH r AS (
                       SELECT ((created_at AT TIME ZONE 'UTC')
                                AT TIME ZONE 'Asia/Tokyo')::date AS day,
                              (source = 'media')        AS is_media,
                              (ticket_used IS TRUE)     AS used
                       FROM registrations
                       WHERE deleted = FALSE
                   )
                   SELECT day,
                          COUNT(*)                                        AS total,
                          COUNT(*) FILTER (WHERE is_media IS NOT TRUE)    AS web,
                          COUNT(*) FILTER (WHERE is_media IS TRUE)        AS media,
                          COUNT(*) FILTER (WHERE is_media IS NOT TRUE AND used)     AS used,
                          COUNT(*) FILTER (WHERE is_media IS NOT TRUE AND NOT used) AS pending
                   FROM r
                   WHERE (%(f)s::date IS NULL OR day >= %(f)s::date)
                     AND (%(t)s::date IS NULL OR day <= %(t)s::date)
                   GROUP BY day
                   ORDER BY day NULLS FIRST""",
                {"f": date_from, "t": date_to}
            )
            rows = cur.fetchall()

    keys   = ("total", "web", "media", "used", "pending")
    totals = {k: 0 for k in keys}
    days   = []
    for r in rows:
        day = r["day"].isoformat() if r["day"] else "Unknown"
        entry = {"day": day, **{k: int(r[k]) for k in keys}}
        days.append(entry)
        for k in keys:
            totals[k] += entry[k]

    return jsonify({"from": date_from, "to": date_to,
                    "totals": totals, "days": days})


# ── GET /wifi-password — public, used by ticket.html ──────────────────
@app.route("/wifi-password", methods=["GET"])
def get_wifi_password():
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'wifi_password'")
            row = cur.fetchone()

    return jsonify({"wifi_password": row[0] if row else ""})


# ── POST /admin/wifi-password — protected, used by admin.html ─────────
@app.route("/admin/wifi-password", methods=["POST"])
def set_wifi_password():
    if not check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401

    data     = request.get_json(silent=True) or {}
    password = data.get("wifi_password", "").strip()

    if not password:
        return jsonify({"error": "missing_password"}), 400

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES ('wifi_password', %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP""",
                (password,)
            )
        con.commit()

    return jsonify({"success": True, "wifi_password": password}), 200


# ── GET /health ────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ── Startup ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# Gunicorn entry point
try:
    init_db()
except Exception:
    traceback.print_exc()
