"""
SQLite cache สำหรับเก็บบทวิเคราะห์บอล — ใช้ sqlite3 ที่ติดมากับ Python ไม่ต้องลง lib เพิ่ม

ไฟล์ฐานข้อมูล: cache.db ที่ root ของโปรเจกต์ (path อ้างอิงจากตำแหน่งไฟล์ .py
แบบเดียวกับที่ fetch_fixtures.py หา leagues.json) รันจาก path ไหนก็เจอไฟล์เดิม

ทดสอบว่าสร้าง db ได้:
    python3 src/cache_db.py
"""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - รองรับ Python เก่ากว่า 3.9
    ZoneInfo = None

# อ้างอิงจากตำแหน่งไฟล์ .py ไม่ใช่ working directory
DB_PATH = Path(__file__).resolve().parent.parent / "cache.db"

TIMEZONE_NAME = "Asia/Bangkok"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS analyses (
    fixture_id    INTEGER PRIMARY KEY,
    match_name    TEXT NOT NULL,
    analysis_text TEXT NOT NULL,
    model_used    TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""


def _bangkok_now_iso():
    """เวลาปัจจุบันโซน Asia/Bangkok รูปแบบ ISO 8601 เช่น 2026-08-22T21:30:15+07:00"""
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(TIMEZONE_NAME)
        except Exception:
            tz = None
    if tz is None:
        tz = timezone(timedelta(hours=7))  # fallback ถ้าเครื่องไม่มีฐานข้อมูล timezone
    return datetime.now(tz).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path=DB_PATH):
    """
    เปิด connection แล้วปิดให้เสมอ ไม่ให้ไฟล์ค้าง lock

    หมายเหตุ: `with sqlite3.connect(...)` เพียว ๆ จะ commit/rollback ให้ก็จริง
    แต่ "ไม่ปิด" connection ตอนออกจากบล็อก จึงต้องห่อด้วย closing() อีกชั้น
    ส่วนการ commit ยังใช้ `with conn:` ตามปกติในฟังก์ชันที่เขียนข้อมูล
    """
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def init_db(db_path=DB_PATH):
    """สร้างไฟล์ db + ตาราง analyses ถ้ายังไม่มี — เรียกซ้ำได้ไม่พัง"""
    with _connect(db_path) as conn:
        with conn:
            conn.execute(CREATE_TABLE_SQL)
    return db_path


def get_analysis(fixture_id, db_path=DB_PATH):
    """คืนบทวิเคราะห์ของ fixture_id เป็น dict — ถ้าไม่มีคืน None"""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT fixture_id, match_name, analysis_text, model_used, created_at
            FROM analyses
            WHERE fixture_id = ?
            """,
            (fixture_id,),
        ).fetchone()

    return dict(row) if row is not None else None


def save_analysis(fixture_id, match_name, analysis_text, model_used, db_path=DB_PATH):
    """
    บันทึกบทวิเคราะห์ ถ้ามี fixture_id นี้อยู่แล้วจะเขียนทับ (INSERT OR REPLACE)
    created_at ใส่เวลาปัจจุบันโซนไทยให้อัตโนมัติ — คืนค่า created_at ที่บันทึกไป
    """
    created_at = _bangkok_now_iso()

    with _connect(db_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO analyses
                    (fixture_id, match_name, analysis_text, model_used, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fixture_id, match_name, analysis_text, model_used, created_at),
            )

    return created_at


def count_analyses(db_path=DB_PATH):
    """จำนวนแถวทั้งหมดในตาราง analyses (ไว้ debug)"""
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]


if __name__ == "__main__":
    path = init_db()
    print(f"ฐานข้อมูลพร้อมใช้งาน: {path}")
    print(f"ตอนนี้มีบทวิเคราะห์ในแคช {count_analyses()} รายการ")
