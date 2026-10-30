from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
import os
import sqlite3
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "welding_monitor.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

app = Flask(__name__, static_folder=None)

DEFAULT_RECIPE = [
    (25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),
    (25,200),(23,200),(25,200),(25,200),(25,200),(25,200),(25,240),(25,240),(23,220),
    (23,220),(25,200),(25,200),(22,170),(22,170),(22,170),(24,220),(24,220),(25,220)
]


def _postgres_url():
    # Render/Supabase may provide postgres://; psycopg accepts postgresql://.
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://"):]
    return DATABASE_URL


def connect_db():
    """Use Supabase PostgreSQL in deployment; retain SQLite for local development."""
    if USE_POSTGRES:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "检测到 DATABASE_URL，但未安装 psycopg。请检查 requirements.txt。"
            ) from exc
        return psycopg.connect(
            _postgres_url(),
            row_factory=dict_row,
            connect_timeout=10,
            sslmode="require",
            prepare_threshold=None,
        )

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def execute(conn, sql, params=()):
    """Keep one SQL statement style for SQLite (?) and PostgreSQL (%s)."""
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


def init_db():
    with connect_db() as conn:
        if USE_POSTGRES:
            execute(conn, """CREATE TABLE IF NOT EXISTS recipes (
                seam INTEGER PRIMARY KEY,
                voltage DOUBLE PRECISION NOT NULL,
                current DOUBLE PRECISION NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            execute(conn, """CREATE TABLE IF NOT EXISTS alarms (
                id BIGSERIAL PRIMARY KEY,
                time TEXT,
                station TEXT,
                robot TEXT,
                seam INTEGER,
                field TEXT,
                standard TEXT,
                current TEXT,
                delta TEXT,
                state TEXT
            )""")
        else:
            execute(conn, """CREATE TABLE IF NOT EXISTS recipes (
                seam INTEGER PRIMARY KEY, voltage REAL NOT NULL, current REAL NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            execute(conn, """CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, station TEXT, robot TEXT,
                seam INTEGER, field TEXT, standard TEXT, current TEXT, delta TEXT, state TEXT
            )""")

        count = execute(conn, "SELECT COUNT(*) FROM recipes").fetchone()
        count = list(count.values())[0] if isinstance(count, dict) else count[0]
        if count == 0:
            now = datetime.now().isoformat(timespec="seconds")
            rows = [(i + 1, v, c, now) for i, (v, c) in enumerate(DEFAULT_RECIPE)]
            if USE_POSTGRES:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(%s,%s,%s,%s)",
                        rows,
                    )
            else:
                conn.executemany(
                    "INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(?,?,?,?)",
                    rows,
                )


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Yaskawa Welding Monitor",
        "database": "supabase-postgresql" if USE_POSTGRES else "local-sqlite",
        "robot_connected": False,
    })


@app.get("/api/recipes")
def get_recipes():
    with connect_db() as conn:
        rows = execute(
            conn,
            "SELECT seam,voltage,current,updated_at FROM recipes ORDER BY seam"
        ).fetchall()
    return jsonify({"recipes": [dict(row) for row in rows]})


@app.put("/api/recipes")
def put_recipes():
    payload = request.get_json(silent=True) or {}
    recipes = payload.get("recipes")
    if not isinstance(recipes, list) or len(recipes) != 27:
        return jsonify({"error": "必须提交完整的27条焊缝配方"}), 400

    clean = []
    try:
        seen = set()
        for item in recipes:
            seam = int(item["seam"])
            voltage = float(item["voltage"])
            current = float(item["current"])
            if (
                seam < 1 or seam > 27 or seam in seen
                or not (0 < voltage < 100) or not (0 < current < 1000)
            ):
                raise ValueError("焊缝编号或参数范围不正确")
            seen.add(seam)
            clean.append((
                seam, voltage, current,
                datetime.now().isoformat(timespec="seconds")
            ))
        if len(seen) != 27:
            raise ValueError("焊缝编号必须从1到27完整且不重复")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "配方数据格式不正确"}), 400

    with connect_db() as conn:
        if USE_POSTGRES:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(%s,%s,%s,%s)
                    ON CONFLICT(seam) DO UPDATE SET
                    voltage=EXCLUDED.voltage,
                    current=EXCLUDED.current,
                    updated_at=EXCLUDED.updated_at""",
                    clean,
                )
        else:
            conn.executemany(
                """INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(seam) DO UPDATE SET
                voltage=excluded.voltage,
                current=excluded.current,
                updated_at=excluded.updated_at""",
                clean,
            )
    return jsonify({"ok": True, "saved": 27})


@app.get("/api/alarms")
def get_alarms():
    with connect_db() as conn:
        rows = execute(
            conn,
            """SELECT time,station,robot,seam,field,standard,current,delta,state
            FROM alarms ORDER BY id DESC LIMIT 300"""
        ).fetchall()
    return jsonify({"alarms": [dict(row) for row in rows]})


@app.post("/api/alarms")
def post_alarm():
    item = request.get_json(silent=True) or {}
    required = [
        "time", "station", "robot", "seam", "field",
        "standard", "current", "delta", "state"
    ]
    if any(k not in item for k in required):
        return jsonify({"error": "报警记录字段不完整"}), 400

    values = [item[k] for k in required]
    with connect_db() as conn:
        if USE_POSTGRES:
            execute(
                conn,
                """INSERT INTO alarms(
                    time,station,robot,seam,field,standard,current,delta,state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                values,
            )
        else:
            execute(
                conn,
                """INSERT INTO alarms(
                    time,station,robot,seam,field,standard,current,delta,state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                values,
            )
    return jsonify({"ok": True})


# Initialize tables at process startup for both local Flask and Render/Gunicorn.
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
