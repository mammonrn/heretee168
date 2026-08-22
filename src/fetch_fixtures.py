"""
Phase 1 — ดึงโปรแกรมบอล "วันพรุ่งนี้" จาก API-Football (API-SPORTS โดยตรง) แล้วแสดงผลอ่านง่าย

วิธีใช้:
    pip install -r requirements.txt
    cp .env.example .env      # แล้วใส่ค่า API_FOOTBALL_KEY ลงใน .env
    python src/fetch_fixtures.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - รองรับ Python เก่ากว่า 3.9
    ZoneInfo = None

BASE_URL = "https://v3.football.api-sports.io"
TIMEZONE_NAME = "Asia/Bangkok"
REQUEST_TIMEOUT = 20  # วินาที


def get_bangkok_tz():
    """คืน tzinfo ของ Asia/Bangkok ถ้าเครื่องไม่มีฐานข้อมูล timezone ให้ fallback เป็น UTC+7"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=7))


def get_api_key():
    """อ่าน API key จาก .env — ถ้าไม่พบให้หยุดทันที (fail fast)"""
    load_dotenv()
    api_key = (os.getenv("API_FOOTBALL_KEY") or "").strip()

    if not api_key:
        print("[ERROR] ไม่พบ API key: ยังไม่ได้ตั้งค่า environment variable ชื่อ API_FOOTBALL_KEY")
        print("        วิธีแก้: สร้างไฟล์ .env (คัดลอกจาก .env.example) แล้วใส่บรรทัด")
        print("        API_FOOTBALL_KEY=คีย์จริงของคุณ")
        sys.exit(1)

    if api_key == "your_key_here":
        print("[ERROR] API_FOOTBALL_KEY ยังเป็นค่าตัวอย่าง 'your_key_here' — กรุณาใส่คีย์จริงในไฟล์ .env")
        sys.exit(1)

    return api_key


def tomorrow_date_str(tz):
    """คำนวณวันพรุ่งนี้ตาม timezone Asia/Bangkok รูปแบบ YYYY-MM-DD"""
    return (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_fixtures(api_key, date_str):
    """เรียก /fixtures แล้วคืน list ของ fixture — จัดการ error ทุกกรณี ไม่ crash เงียบ"""
    url = f"{BASE_URL}/fixtures"
    headers = {
        "x-apisports-key": api_key,
    }
    params = {
        "date": date_str,
        "timezone": TIMEZONE_NAME,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        print(f"[ERROR] เชื่อมต่อ API ไม่สำเร็จ: หมดเวลารอ (เกิน {REQUEST_TIMEOUT} วินาที) — ลองใหม่อีกครั้ง")
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        print(f"[ERROR] เชื่อมต่อ API ไม่ได้: ตรวจสอบอินเทอร์เน็ต/DNS ของเครื่อง ({exc})")
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        print(f"[ERROR] เรียก API ล้มเหลว: {exc}")
        sys.exit(1)

    status = response.status_code

    if status == 429:
        print("[ERROR] โดน rate limit (HTTP 429): เรียก API ถี่เกินไปหรือใช้โควตาของแพลนหมดแล้ว")
        print("        วิธีแก้: รอให้โควตารีเซ็ต (ปกติรายวัน/รายนาที) หรืออัปเกรดแพลนบน API-SPORTS")
        sys.exit(1)

    if status in (401, 403):
        print(f"[ERROR] ยืนยันตัวตนไม่ผ่าน (HTTP {status}): API key ผิด หรือบัญชียังไม่ได้ subscribe แพลนบน API-SPORTS")
        print("        วิธีแก้: ตรวจค่า API_FOOTBALL_KEY ใน .env และเช็คสถานะแพลนที่ dashboard.api-football.com")
        sys.exit(1)

    if status != 200:
        print(f"[ERROR] API ตอบกลับด้วยสถานะที่ไม่คาดคิด: HTTP {status}")
        print(f"        รายละเอียด: {response.text[:500]}")
        sys.exit(1)

    try:
        data = response.json()
    except ValueError:
        print("[ERROR] อ่านข้อมูลจาก API ไม่ได้: ผลลัพธ์ไม่ใช่ JSON ที่ถูกต้อง")
        print(f"        รายละเอียด: {response.text[:500]}")
        sys.exit(1)

    # API-SPORTS มักตอบ HTTP 200 แต่แจ้งข้อผิดพลาดไว้ในฟิลด์ errors
    # เช่น key ผิด/หมดอายุ -> {"errors": {"token": "..."}}
    #      โควตาหมด        -> {"errors": {"requests": "..."}}
    errors = data.get("errors")
    if errors:
        if isinstance(errors, dict):
            if "token" in errors:
                print("[ERROR] API key ไม่ถูกต้อง (API-SPORTS ตอบ HTTP 200 พร้อม errors.token):")
                print(f"        {errors['token']}")
                print("        วิธีแก้: ตรวจค่า API_FOOTBALL_KEY ใน .env ว่าเป็นคีย์จาก dashboard.api-football.com")
                print("        (คีย์ของ RapidAPI ใช้กับ endpoint นี้ไม่ได้) และเช็คว่าบัญชี subscribe แพลนแล้ว")
                sys.exit(1)

            if "requests" in errors:
                print("[ERROR] ใช้โควตาเรียก API หมดแล้ว (errors.requests):")
                print(f"        {errors['requests']}")
                print("        วิธีแก้: รอให้โควตารีเซ็ต หรืออัปเกรดแพลนบน API-SPORTS")
                sys.exit(1)

            print("[ERROR] API แจ้งข้อผิดพลาดกลับมา:")
            for key, message in errors.items():
                print(f"        - {key}: {message}")
        else:
            print("[ERROR] API แจ้งข้อผิดพลาดกลับมา:")
            print(f"        - {errors}")
        sys.exit(1)

    return data.get("response", []) or []


def format_kickoff(fixture_date):
    """แปลงเวลาเตะจาก ISO string เป็น HH:MM (เวลาไทยตามที่ขอไปกับ params)"""
    if not fixture_date:
        return "??:??"
    try:
        return datetime.fromisoformat(fixture_date.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return str(fixture_date)


def print_fixtures(fixtures, date_str):
    """แสดงผลรูปแบบ: [ชื่อลีก] เวลาเตะ ทีมเหย้า vs ทีมเยือน"""
    print(f"โปรแกรมบอลวันพรุ่งนี้ ({date_str}) เวลาไทย — {TIMEZONE_NAME}")
    print("-" * 70)

    if not fixtures:
        print("ยังไม่มีโปรแกรมแข่งขันสำหรับวันดังกล่าว (หรือ API ยังไม่อัปเดตข้อมูล)")
        print("-" * 70)
        return

    rows = []
    for item in fixtures:
        league = item.get("league") or {}
        teams = item.get("teams") or {}
        fixture = item.get("fixture") or {}

        league_name = league.get("name") or "ไม่ทราบลีก"
        country = league.get("country")
        if country and country != "World":
            league_name = f"{country} - {league_name}"

        home = (teams.get("home") or {}).get("name") or "ทีมเหย้า ?"
        away = (teams.get("away") or {}).get("name") or "ทีมเยือน ?"
        kickoff = format_kickoff(fixture.get("date"))

        rows.append((kickoff, league_name, home, away))

    rows.sort(key=lambda row: (row[0], row[1]))

    for kickoff, league_name, home, away in rows:
        print(f"[{league_name}] {kickoff} {home} vs {away}")

    print("-" * 70)
    print(f"รวมทั้งหมด {len(rows)} คู่")


def main():
    api_key = get_api_key()
    tz = get_bangkok_tz()
    date_str = tomorrow_date_str(tz)

    print(f"กำลังดึงข้อมูลโปรแกรมบอลวันที่ {date_str} ...")
    fixtures = fetch_fixtures(api_key, date_str)
    print_fixtures(fixtures, date_str)


if __name__ == "__main__":
    main()
