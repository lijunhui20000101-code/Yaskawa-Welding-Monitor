from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
import sqlite3
import json
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "welding_monitor.db"
app = Flask(__name__, static_folder=None)

DEFAULT_RECIPE = [
    (25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),(25,200),
    (25,200),(23,200),(25,200),(25,200),(25,200),(25,200),(25,240),(25,240),(23,220),
    (23,220),(25,200),(25,200),(22,170),(22,170),(22,170),(24,220),(24,220),(25,220)
]

def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with connect_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS recipes (
            seam INTEGER PRIMARY KEY, voltage REAL NOT NULL, current REAL NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, station TEXT, robot TEXT,
            seam INTEGER, field TEXT, standard TEXT, current TEXT, delta TEXT, state TEXT
        )""")
        count = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        if count == 0:
            now = datetime.now().isoformat(timespec="seconds")
            conn.executemany("INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(?,?,?,?)",
                [(i+1, v, c, now) for i,(v,c) in enumerate(DEFAULT_RECIPE)])

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "Yaskawa Welding Monitor", "robot_connected": False})

@app.get("/api/recipes")
def get_recipes():
    with connect_db() as conn:
        rows = conn.execute("SELECT seam,voltage,current,updated_at FROM recipes ORDER BY seam").fetchall()
    return jsonify({"recipes":[dict(row) for row in rows]})

@app.put("/api/recipes")
def put_recipes():
    payload = request.get_json(silent=True) or {}
    recipes = payload.get("recipes")
    if not isinstance(recipes, list) or len(recipes) != 27:
        return jsonify({"error":"必须提交完整的27条焊缝配方"}), 400
    clean = []
    try:
        seen = set()
        for item in recipes:
            seam = int(item["seam"])
            voltage = float(item["voltage"])
            current = float(item["current"])
            if seam < 1 or seam > 27 or seam in seen or not (0 < voltage < 100) or not (0 < current < 1000):
                raise ValueError("焊缝编号或参数范围不正确")
            seen.add(seam)
            clean.append((seam,voltage,current,datetime.now().isoformat(timespec="seconds")))
        if len(seen) != 27:
            raise ValueError("焊缝编号必须从1到27完整且不重复")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error":str(exc) or "配方数据格式不正确"}), 400
    with connect_db() as conn:
        conn.executemany("""INSERT INTO recipes(seam,voltage,current,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(seam) DO UPDATE SET voltage=excluded.voltage,current=excluded.current,updated_at=excluded.updated_at""", clean)
    return jsonify({"ok":True,"saved":27})

@app.get("/api/alarms")
def get_alarms():
    with connect_db() as conn:
        rows = conn.execute("SELECT time,station,robot,seam,field,standard,current,delta,state FROM alarms ORDER BY id DESC LIMIT 300").fetchall()
    return jsonify({"alarms":[dict(row) for row in rows]})

@app.post("/api/alarms")
def post_alarm():
    item = request.get_json(silent=True) or {}
    required = ["time","station","robot","seam","field","standard","current","delta","state"]
    if any(k not in item for k in required):
        return jsonify({"error":"报警记录字段不完整"}), 400
    with connect_db() as conn:
        conn.execute("""INSERT INTO alarms(time,station,robot,seam,field,standard,current,delta,state)
            VALUES(?,?,?,?,?,?,?,?,?)""", [item[k] for k in required])
    return jsonify({"ok":True})

# 导入应用时也初始化数据库，确保本地运行和 Render/Gunicorn 部署都可用。
init_db()

if __name__ == "__main__":
    # 开发阶段监听所有网卡，方便同一局域网内手机测试；不要直接暴露到公网。
    app.run(host="0.0.0.0", port=5000, debug=False)
