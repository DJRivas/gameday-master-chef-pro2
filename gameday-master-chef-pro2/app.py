import os, uuid, sqlite3
from sqlite3 import IntegrityError
from flask import Flask, render_template, request, jsonify, g, Response, make_response, redirect, url_for, session

DATABASE = os.environ.get("DATABASE_URL", "ratings.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "replace-me")  # needed for admin session

ENTRANTS = ["Javier","Lindsay","Yesenia","Bryan","Viviana","Bernie","Rogelio","Daniella","Colleen","Justin","Paige","Nic","Martha"]

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SECRET_KEY

# -------- DB helpers --------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DATABASE, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS ratings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entrant_index INTEGER NOT NULL,
            taste INTEGER NOT NULL CHECK(taste BETWEEN 1 AND 5),
            presentation INTEGER NOT NULL CHECK(presentation BETWEEN 1 AND 5),
            easy INTEGER NOT NULL CHECK(easy BETWEEN 1 AND 5),
            judge TEXT,
            device_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (entrant_index, device_id)
        )
    """)
    db.commit()

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

with app.app_context():
    init_db()

# -------- Helpers --------
def device_id_from_request():
    return request.cookies.get("device_id") or "anon"

# -------- Routes --------
@app.route("/")
def home():
    resp = make_response(render_template("index.html", entrants=ENTRANTS, title="2025 GAME DAY MASTER CHEF COMPETITION CHALLENGE"))
    if not request.cookies.get("device_id"):
        resp.set_cookie("device_id", str(uuid.uuid4()), max_age=60*60*24*365, samesite="Lax")
    return resp

# Submit (create or update) a rating for this device and entrant
@app.route("/api/rate", methods=["POST"])
def api_rate():
    data = request.get_json(silent=True) or {}
    try:
        entrant_index = int(data.get("entrant_index"))
        taste = int(data.get("taste"))
        presentation = int(data.get("presentation"))
        easy = int(data.get("easy"))
        judge = (data.get("judge") or "").strip()[:50] or None
    except Exception:
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    if not (0 <= entrant_index < len(ENTRANTS)):
        return jsonify({"ok": False, "error": "Invalid entrant"}), 400
    for v in (taste, presentation, easy):
        if v < 1 or v > 5:
            return jsonify({"ok": False, "error": "Scores must be 1–5"}), 400

    device_id = device_id_from_request()
    db = get_db()
    # Upsert to allow updating previous ratings from same device
    db.execute(
        """
        INSERT INTO ratings (entrant_index, taste, presentation, easy, judge, device_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(entrant_index, device_id) DO UPDATE SET
            taste=excluded.taste,
            presentation=excluded.presentation,
            easy=excluded.easy,
            judge=excluded.judge
        """,
        (entrant_index, taste, presentation, easy, judge, device_id),
    )
    db.commit()
    return jsonify({"ok": True})

# Return this device's rating for an entrant, if any
@app.route("/api/my-rating")
def api_my_rating():
    try:
        entrant_index = int(request.args.get("entrant_index", "-1"))
    except Exception:
        return jsonify({"ok": False, "error": "Bad entrant index"}), 400
    if not (0 <= entrant_index < len(ENTRANTS)):
        return jsonify({"ok": True, "rating": None})

    device_id = device_id_from_request()
    db = get_db()
    row = db.execute(
        "SELECT taste, presentation, easy, judge FROM ratings WHERE entrant_index=? AND device_id=?",
        (entrant_index, device_id),
    ).fetchone()
    if not row:
        return jsonify({"ok": True, "rating": None})
    return jsonify({"ok": True, "rating": dict(row)})

# Public leaderboard JSON
@app.route("/api/leaderboard")
def api_leaderboard():
    db = get_db()
    rows = db.execute("""
        SELECT entrant_index,
               COUNT(*) AS votes,
               AVG(taste) AS avg_taste,
               AVG(presentation) AS avg_presentation,
               AVG(easy) AS avg_easy,
               AVG(taste + presentation + easy) AS avg_total
        FROM ratings
        GROUP BY entrant_index
        ORDER BY avg_total DESC
    """).fetchall()

    out = []
    for r in rows:
        out.append({
            "name": ENTRANTS[r["entrant_index"]],
            "votes": r["votes"],
            "avg_taste": round(r["avg_taste"], 2) if r["avg_taste"] is not None else 0,
            "avg_presentation": round(r["avg_presentation"], 2) if r["avg_presentation"] is not None else 0,
            "avg_easy": round(r["avg_easy"], 2) if r["avg_easy"] is not None else 0,
            "avg_total": round(r["avg_total"], 2) if r["avg_total"] is not None else 0
        })
    return jsonify(out)

# CSV export
@app.route("/export.csv")
def export_csv():
    db = get_db()
    rows = db.execute("""
        SELECT id, entrant_index, taste, presentation, easy, judge, device_id, created_at
        FROM ratings
        ORDER BY created_at ASC
    """).fetchall()

    def generate():
        header = ["id","entrant_name","taste","presentation","easy","judge","device_id","created_at"]
        yield ",".join(header) + "\n"
        for r in rows:
            name = ENTRANTS[r["entrant_index"]]
            line = [
                str(r["id"]),
                '"' + name.replace('"','""') + '"',
                str(r["taste"]),
                str(r["presentation"]),
                str(r["easy"]),
                '"' + (r["judge"] or "").replace('"','""') + '"',
                '"' + (r["device_id"] or "") + '"',
                str(r["created_at"])
            ]
            yield ",".join(line) + "\n"

    return Response(generate(), mimetype="text/csv")

# -------- Admin: detailed results with simple password gate --------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "MASTERCHEF2025")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Incorrect password")
    if not session.get("is_admin"):
        return render_template("admin_login.html")
    # Show detailed table
    db = get_db()
    rows = db.execute("""
        SELECT r.id, r.entrant_index, r.taste, r.presentation, r.easy, r.judge, r.device_id, r.created_at
        FROM ratings r
        ORDER BY r.entrant_index ASC, r.created_at ASC
    """).fetchall()
    detailed = [{
        "id": r["id"],
        "entrant": ENTRANTS[r["entrant_index"]],
        "taste": r["taste"],
        "presentation": r["presentation"],
        "easy": r["easy"],
        "total": r["taste"] + r["presentation"] + r["easy"],
        "judge": r["judge"] or "",
        "device_id": r["device_id"] or "",
        "created_at": r["created_at"],
    } for r in rows]
    # Also compute leaderboard snapshot
    lb = get_db().execute("""
        SELECT entrant_index,
               COUNT(*) AS votes,
               AVG(taste + presentation + easy) AS avg_total
        FROM ratings GROUP BY entrant_index
        ORDER BY avg_total DESC
    """).fetchall()
    lb_data = [{"name": ENTRANTS[r["entrant_index"]], "votes": r["votes"], "avg_total": round(r["avg_total"] or 0, 2)} for r in lb]
    return render_template("admin_results.html", detailed=detailed, leaderboard=lb_data, title="Admin Detailed Results")

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin"))
