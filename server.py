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
            # 兼容先前在 Supabase 控制台手动创建、字段不完整的 alarms 表。
            for column, sql_type in (
                ("time", "TEXT"),
                ("station", "TEXT"),
                ("robot", "TEXT"),
                ("seam", "INTEGER"),
                ("field", "TEXT"),
                ("standard", "TEXT"),
                ("current", "TEXT"),
                ("delta", "TEXT"),
                ("state", "TEXT"),
            ):
                execute(
                    conn,
                    f"ALTER TABLE alarms ADD COLUMN IF NOT EXISTS {column} {sql_type}"
                )
        else:
            execute(conn, """CREATE TABLE IF NOT EXISTS recipes (
                seam INTEGER PRIMARY KEY, voltage REAL NOT NULL, current REAL NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            execute(conn, """CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, station TEXT, robot TEXT,
                seam INTEGER, field TEXT, standard TEXT, current TEXT, delta TEXT, state TEXT
            )""")
            # SQLite 本地开发时也补齐旧表缺少的字段。
            existing_columns = {
                row["name"] for row in execute(conn, "PRAGMA table_info(alarms)").fetchall()
            }
            for column, sql_type in (
                ("time", "TEXT"),
                ("station", "TEXT"),
                ("robot", "TEXT"),
                ("seam", "INTEGER"),
                ("field", "TEXT"),
                ("standard", "TEXT"),
                ("current", "TEXT"),
                ("delta", "TEXT"),
                ("state", "TEXT"),
            ):
                if column not in existing_columns:
                    execute(conn, f"ALTER TABLE alarms ADD COLUMN {column} {sql_type}")

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
    # 标准焊接配方为只读数据。禁止通过网页/API手动覆盖；
    # 后续接入现场采集程序时，应使用独立的授权写入通道。
    return jsonify({
        "error": "标准配方为只读数据，禁止通过网页修改；请由授权的现场采集程序更新。"
    }), 405

@app.get("/api/alarms")
def get_alarms():
    with connect_db() as conn:
        rows = execute(
            conn,
            """SELECT id,time,station,robot,seam,field,standard,current,delta,state
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
    if not isinstance(item, dict) or any(k not in item for k in required):
        return jsonify({"error": "报警记录字段不完整"}), 400

    try:
        seam = int(item["seam"])
        if seam < 1 or seam > 27:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "焊缝编号必须是1到27之间的整数"}), 400

    values = [
        item["time"], item["station"], item["robot"], seam,
        item["field"], item["standard"], item["current"],
        item["delta"], item["state"],
    ]
    with connect_db() as conn:
        if USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO alarms(
                        time,station,robot,seam,field,standard,current,delta,state
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    values,
                )
                alarm_id = cur.fetchone()["id"]
        else:
            cur = execute(
                conn,
                """INSERT INTO alarms(
                    time,station,robot,seam,field,standard,current,delta,state
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                values,
            )
            alarm_id = cur.lastrowid
    return jsonify({"ok": True, "id": alarm_id})


# Initialize tables at process startup for both local Flask and Render/Gunicorn.
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
