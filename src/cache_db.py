"""
SQLite cache สำหรับเก็บบทวิเคราะห์บอล — ใช้ sqlite3 ที่ติดมากับ Python ไม่ต้องลง lib เพิ่ม

ไฟล์ฐานข้อมูล: cache.db ที่ root ของโปรเจกต์ (path อ้างอิงจากตำแหน่งไฟล์ .py
แบบเดียวกับที่ fetch_fixtures.py หา leagues.json) รันจาก path ไหนก็เจอไฟล์เดิม

ทดสอบว่าสร้าง db ได้:
    python3 src/cache_db.py
"""

import json
import sqlite3
import time
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

# แคชราคาต่อรองแยกจากแคชบทวิเคราะห์ เพราะราคาขยับทั้งวัน อายุจึงสั้นกว่ากันมาก
# payload เก็บเป็น JSON string, fetched_at เป็น epoch วินาที ไว้คำนวณ TTL
CREATE_ODDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS odds_cache (
    cache_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    created_at TEXT NOT NULL
)
"""

# นับจำนวน request ที่ยิงไป OddsPapi ต่อวัน (free tier 250 ครั้ง/เดือน จึงอยากเห็นตัวเลข)
CREATE_USAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS odds_api_usage (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL
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
    """สร้างไฟล์ db + ตารางทั้งหมดถ้ายังไม่มี — เรียกซ้ำได้ไม่พัง"""
    with _connect(db_path) as conn:
        with conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(CREATE_ODDS_TABLE_SQL)
            conn.execute(CREATE_USAGE_TABLE_SQL)
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


# ---------- แคชราคาต่อรอง (อายุสั้น แยกตารางจากบทวิเคราะห์) ----------


def get_odds(cache_key, ttl_seconds, db_path=DB_PATH, now=None):
    """
    อ่านราคาจากแคชถ้ายังไม่หมดอายุ — หมดอายุหรือไม่มีคืน None
    ttl_seconds สั้น ๆ (15-30 นาที) เพราะราคาขยับตลอดวัน
    """
    now = time.time() if now is None else now

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload, fetched_at, created_at FROM odds_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()

    if row is None:
        return None

    if (now - row["fetched_at"]) >= ttl_seconds:
        return None  # ปล่อยแถวเก่าไว้ เดี๋ยวถูกเขียนทับตอนดึงใหม่

    try:
        payload = json.loads(row["payload"])
    except ValueError:
        return None

    return {"payload": payload, "created_at": row["created_at"]}


def save_odds(cache_key, payload, db_path=DB_PATH, now=None):
    """บันทึกราคาลงแคช (เขียนทับ key เดิม) — คืนเวลาที่บันทึก"""
    now = time.time() if now is None else now
    created_at = _bangkok_now_iso()

    with _connect(db_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO odds_cache (cache_key, payload, fetched_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, json.dumps(payload, ensure_ascii=False), now, created_at),
            )

    return created_at


def count_odds(db_path=DB_PATH):
    """จำนวนรายการในแคชราคา (ไว้ debug)"""
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM odds_cache").fetchone()[0]


# ---------- ตัวนับ request ของ OddsPapi ----------


def record_odds_request(db_path=DB_PATH, day=None):
    """
    บวกตัวนับ request ของวันนี้ แล้วคืน (จำนวนวันนี้, จำนวนเดือนนี้)
    ไม่ได้บังคับ rate limit — แค่ให้เห็นตัวเลขว่าใช้โควตาไปเท่าไร
    """
    day = day or _bangkok_now_iso()[:10]

    with _connect(db_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO odds_api_usage (day, count) VALUES (?, 1)
                ON CONFLICT(day) DO UPDATE SET count = count + 1
                """,
                (day,),
            )

        today = conn.execute("SELECT count FROM odds_api_usage WHERE day = ?", (day,)).fetchone()[0]
        month = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM odds_api_usage WHERE day LIKE ?",
            (f"{day[:7]}%",),
        ).fetchone()[0]

    return today, month


def odds_usage(db_path=DB_PATH, day=None):
    """อ่านตัวเลขการใช้งานโดยไม่บวกเพิ่ม — คืน (วันนี้, เดือนนี้)"""
    day = day or _bangkok_now_iso()[:10]

    with _connect(db_path) as conn:
        row = conn.execute("SELECT count FROM odds_api_usage WHERE day = ?", (day,)).fetchone()
        month = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM odds_api_usage WHERE day LIKE ?",
            (f"{day[:7]}%",),
        ).fetchone()[0]

    return (row[0] if row else 0), month


def count_analyses(db_path=DB_PATH):
    """จำนวนแถวทั้งหมดในตาราง analyses (ไว้ debug)"""
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]


if __name__ == "__main__":
    path = init_db()
    today, month = odds_usage()
    print(f"ฐานข้อมูลพร้อมใช้งาน: {path}")
    print(f"ตอนนี้มีบทวิเคราะห์ในแคช {count_analyses()} รายการ")
    print(f"แคชราคาต่อรอง {count_odds()} รายการ")
    print(f"ยิง OddsPapi ไปแล้ว: วันนี้ {today} ครั้ง | เดือนนี้ {month} ครั้ง")
