"""
将项目 data/welding_monitor.db 中的 SQLite 数据迁移到 Supabase PostgreSQL。
运行前请设置 DATABASE_URL；如果没有设置，脚本会安全地隐藏输入数据库连接字符串。
脚本不会删除 Supabase 中已有的数据：
- 27条配方按焊缝编号更新/补齐；
- 报警按原 id 导入，遇到相同 id 则跳过，避免重复。
"""
from pathlib import Path
import getpass
import os
import sqlite3
import sys

import psycopg
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "data" / "welding_monitor.db"


def main():
    if not SQLITE_PATH.exists():
        print(f"找不到本地 SQLite 数据库：{SQLITE_PATH}")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        db_url = getpass.getpass("请粘贴 Supabase Transaction Pooler 连接字符串（输入不会显示）：").strip()
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    if not db_url.startswith("postgresql://"):
        print("连接字符串格式不正确，应以 postgresql:// 开头。")
        sys.exit(1)

    with sqlite3.connect(SQLITE_PATH) as src:
        src.row_factory = sqlite3.Row
        recipes = [dict(r) for r in src.execute(
            "SELECT seam,voltage,current,updated_at FROM recipes ORDER BY seam"
        )]
        alarms = [dict(r) for r in src.execute(
            """SELECT id,time,station,robot,seam,field,standard,current,delta,state
            FROM alarms ORDER BY id"""
        )]

    if len(recipes) != 27:
        print(f"停止迁移：本地配方数量为 {len(recipes)}，不是完整的27条。")
        sys.exit(1)

    print(f"准备迁移：{len(recipes)} 条配方、{len(alarms)} 条报警记录。")
    print("目标数据库已有的报警记录不会被删除；相同报警编号会自动跳过。")

    with psycopg.connect(
        db_url, row_factory=dict_row, connect_timeout=10, sslmode="require",
        prepare_threshold=None
    ) as dst:
        dst.execute("""CREATE TABLE IF NOT EXISTS recipes (
            seam INTEGER PRIMARY KEY,
            voltage DOUBLE PRECISION NOT NULL,
            current DOUBLE PRECISION NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        dst.execute("""CREATE TABLE IF NOT EXISTS alarms (
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

        with dst.cursor() as cur:
            cur.executemany(
                """INSERT INTO recipes(seam,voltage,current,updated_at)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(seam) DO UPDATE SET
                voltage=EXCLUDED.voltage,
                current=EXCLUDED.current,
                updated_at=EXCLUDED.updated_at""",
                [
                    (r["seam"], r["voltage"], r["current"], r["updated_at"])
                    for r in recipes
                ],
            )

        for a in alarms:
            dst.execute(
                """INSERT INTO alarms(
                    id,time,station,robot,seam,field,standard,current,delta,state
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING""",
                (
                    a["id"], a["time"], a["station"], a["robot"], a["seam"],
                    a["field"], a["standard"], a["current"], a["delta"], a["state"]
                ),
            )

        if alarms:
            dst.execute("""
                SELECT setval(
                    pg_get_serial_sequence('alarms', 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM alarms), 1), 1),
                    EXISTS(SELECT 1 FROM alarms)
                )
            """)

        recipe_count = dst.execute("SELECT COUNT(*) AS n FROM recipes").fetchone()["n"]
        alarm_count = dst.execute("SELECT COUNT(*) AS n FROM alarms").fetchone()["n"]

    print("迁移完成。")
    print(f"Supabase 当前配方总数：{recipe_count}")
    print(f"Supabase 当前报警总数：{alarm_count}")
    print("请再打开 /api/recipes 和 /api/alarms 核对结果。")


if __name__ == "__main__":
    main()
