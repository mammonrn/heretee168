"""
Phase 1 — ดึงโปรแกรมบอล "วันพรุ่งนี้" จาก API-Football (API-SPORTS โดยตรง) แล้วแสดงผลอ่านง่าย

วิธีใช้:
    pip install -r requirements.txt
    cp .env.example .env      # แล้วใส่ค่า API_FOOTBALL_KEY ลงใน .env
    python3 src/fetch_fixtures.py
"""

from datetime import datetime, timedelta

from api_football import TIMEZONE_NAME, api_get, get_api_key, get_bangkok_tz


def tomorrow_date_str(tz):
    """คำนวณวันพรุ่งนี้ตาม timezone Asia/Bangkok รูปแบบ YYYY-MM-DD"""
    return (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_fixtures(api_key, date_str):
    """เรียก /fixtures ของวันที่ที่ระบุ (เวลาไทย) — error ทุกกรณีถูกจัดการใน api_get"""
    return api_get("fixtures", api_key, {"date": date_str, "timezone": TIMEZONE_NAME})


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
    date_str = tomorrow_date_str(get_bangkok_tz())

    print(f"กำลังดึงข้อมูลโปรแกรมบอลวันที่ {date_str} ...")
    print_fixtures(fetch_fixtures(api_key, date_str), date_str)


if __name__ == "__main__":
    main()
