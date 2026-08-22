"""
Phase 1 — ดึงโปรแกรมบอล "วันพรุ่งนี้" จาก API-Football (API-SPORTS โดยตรง)
แล้วกรองเฉพาะลีกที่กำหนดไว้ใน leagues.json ก่อนแสดงผล

วิธีใช้:
    pip install -r requirements.txt
    cp .env.example .env      # แล้วใส่ค่า API_FOOTBALL_KEY ลงใน .env
    python3 src/fetch_fixtures.py

หมายเหตุ: รายชื่อลีกที่จะแสดงอ่านจาก leagues.json ที่ root ของโปรเจกต์เสมอ
เพิ่ม/ลดลีกได้โดยแก้ไฟล์ config ไม่ต้องแตะโค้ดไฟล์นี้
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from api_football import TIMEZONE_NAME, api_get, fail, get_api_key, get_bangkok_tz

# อ้างอิงจากตำแหน่งไฟล์ .py ไม่ใช่ working directory — รันจาก path ไหนก็หา config เจอ
LEAGUES_CONFIG_PATH = Path(__file__).resolve().parent.parent / "leagues.json"


def load_leagues(config_path=LEAGUES_CONFIG_PATH):
    """
    โหลด leagues.json แล้วคืน dict: {league_id: {"name_th", "name_en", "priority"}}
    ถ้าไม่พบไฟล์ / JSON เสีย / โครงสร้างผิด → หยุดพร้อมบอกสาเหตุ
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(
            f"[ERROR] ไม่พบไฟล์ config: {config_path}",
            "วิธีแก้: สร้างไฟล์ leagues.json ที่ root ของโปรเจกต์ (ระดับเดียวกับ requirements.txt)",
        )
    except OSError as exc:
        fail(f"[ERROR] อ่านไฟล์ config ไม่ได้: {config_path}", f"รายละเอียด: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(
            f"[ERROR] ไฟล์ config ไม่ใช่ JSON ที่ถูกต้อง: {config_path}",
            f"รายละเอียด: {exc}",
            "วิธีแก้: ตรวจเครื่องหมาย , หรือ \" ที่ตกหล่นในไฟล์",
        )

    # รองรับทั้งแบบ list ตรง ๆ และแบบห่อด้วย {"leagues": [...]}
    if isinstance(data, dict):
        data = data.get("leagues")

    if not isinstance(data, list) or not data:
        fail(
            f"[ERROR] โครงสร้าง config ไม่ถูกต้อง: {config_path}",
            'ต้องเป็น list ของลีก เช่น [{"id": 39, "name_en": "...", "name_th": "...", "priority": 1}]',
        )

    leagues = {}
    for index, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            fail(f"[ERROR] config รายการที่ {index} ไม่ใช่ object ใน {config_path}")

        missing = [field for field in ("id", "name_th", "name_en", "priority") if field not in entry]
        if missing:
            fail(
                f"[ERROR] config รายการที่ {index} ขาดฟิลด์: {', '.join(missing)}",
                f"ไฟล์: {config_path}",
            )

        try:
            league_id = int(entry["id"])
            priority = int(entry["priority"])
        except (TypeError, ValueError):
            fail(
                f"[ERROR] config รายการที่ {index}: id และ priority ต้องเป็นตัวเลข",
                f"ไฟล์: {config_path}",
            )

        leagues[league_id] = {
            "name_th": entry["name_th"],
            "name_en": entry["name_en"],
            "priority": priority,
        }

    return leagues


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


def group_by_league(fixtures, leagues):
    """
    เก็บเฉพาะคู่ที่ league id อยู่ใน config แล้วจัดกลุ่มตามลีก
    คืน list ของ (ข้อมูลลีก, [(เวลาเตะ, ทีมเหย้า, ทีมเยือน), ...])
    เรียงลีกตาม priority และภายในลีกเรียงตามเวลาเตะ
    """
    grouped = {}

    for item in fixtures:
        league_id = (item.get("league") or {}).get("id")
        if league_id not in leagues:
            continue

        teams = item.get("teams") or {}
        grouped.setdefault(league_id, []).append((
            format_kickoff((item.get("fixture") or {}).get("date")),
            (teams.get("home") or {}).get("name") or "ทีมเหย้า ?",
            (teams.get("away") or {}).get("name") or "ทีมเยือน ?",
        ))

    result = []
    for league_id in sorted(grouped, key=lambda key: (leagues[key]["priority"], key)):
        matches = sorted(grouped[league_id], key=lambda match: match[0])
        result.append((leagues[league_id], matches))

    return result


def print_fixtures(groups, total_count, date_str):
    """แสดงผลแยกตามลีก ใช้ name_th เป็นหัวข้อ"""
    print(f"โปรแกรมบอลวันพรุ่งนี้ ({date_str}) เวลาไทย — {TIMEZONE_NAME}")
    print("=" * 70)

    shown_count = sum(len(matches) for _, matches in groups)

    if not groups:
        if total_count:
            print("วันดังกล่าวไม่มีคู่บอลของลีกที่ติดตามอยู่ใน leagues.json")
        else:
            print("ยังไม่มีโปรแกรมแข่งขันสำหรับวันดังกล่าว (หรือ API ยังไม่อัปเดตข้อมูล)")
        print("=" * 70)
        print(f"แสดง {shown_count} คู่ จากทั้งหมด {total_count} คู่")
        return

    for league, matches in groups:
        print()
        print(f"[{league['name_th']}] ({league['name_en']})")
        print("-" * 70)
        for kickoff, home, away in matches:
            print(f"  {kickoff}  {home} vs {away}")

    print()
    print("=" * 70)
    print(f"แสดง {shown_count} คู่ จากทั้งหมด {total_count} คู่")


def main():
    leagues = load_leagues()
    api_key = get_api_key()
    date_str = tomorrow_date_str(get_bangkok_tz())

    print(f"กำลังดึงข้อมูลโปรแกรมบอลวันที่ {date_str} ...")
    fixtures = fetch_fixtures(api_key, date_str)
    print_fixtures(group_by_league(fixtures, leagues), len(fixtures), date_str)


if __name__ == "__main__":
    main()
