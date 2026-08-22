"""
Phase 1 — ดึงโปรแกรมบอล 3 วัน (วันนี้ + พรุ่งนี้ + มะรืน) จาก API-Football (API-SPORTS โดยตรง)
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

# จำนวนวันที่ดึง นับจากวันนี้ (3 = วันนี้ + พรุ่งนี้ + มะรืน)
DAYS_AHEAD = 3

# ป้ายหัวข้อวัน เรียงตามลำดับวัน (offset 0, 1, 2 ...) ถ้าเกินจากนี้จะใช้วันที่ล้วน
DAY_LABELS = ("วันนี้", "พรุ่งนี้", "มะรืน")


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


def date_range(tz, days=DAYS_AHEAD):
    """คืนรายการวันที่ตามเวลาไทย เริ่มจากวันนี้ รูปแบบ YYYY-MM-DD (คำนวณสดทุกครั้ง ไม่ hardcode)"""
    today = datetime.now(tz).date()
    return [(today + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days)]


def day_label(date_str, dates):
    """ป้ายหัวข้อวัน เช่น 'วันนี้ (2026-08-22)' — วันที่นอกช่วงจะแสดงเฉพาะวันที่"""
    if date_str in dates:
        offset = dates.index(date_str)
        if offset < len(DAY_LABELS):
            return f"{DAY_LABELS[offset]} ({date_str})"
    return date_str


def fetch_fixtures(api_key, date_from, date_to):
    """
    เรียก /fixtures ครั้งเดียวด้วย date range (from/to) เพื่อประหยัดโควตา
    error ทุกกรณีถูกจัดการใน api_get
    """
    return api_get("fixtures", api_key, {
        "from": date_from,
        "to": date_to,
        "timezone": TIMEZONE_NAME,
    })


def kickoff_parts(fixture_date, tz):
    """
    แปลงเวลาเตะจาก ISO string เป็น (YYYY-MM-DD, HH:MM) ตามเวลาไทย
    แปลงเป็น Asia/Bangkok ก่อนเสมอ คู่ที่เตะดึกจึงถูกนับเป็นวันถัดไปให้ถูกต้อง
    """
    if not fixture_date:
        return "ไม่ทราบวันที่", "??:??"

    try:
        moment = datetime.fromisoformat(fixture_date.replace("Z", "+00:00"))
    except ValueError:
        return str(fixture_date)[:10], "??:??"

    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)

    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


def group_by_day_and_league(fixtures, leagues, tz):
    """
    เก็บเฉพาะคู่ที่ league id อยู่ใน config แล้วจัดกลุ่มเป็น วัน → ลีก
    คืน list ของ (วันที่, [(ข้อมูลลีก, [(เวลาเตะ, ทีมเหย้า, ทีมเยือน), ...]), ...])
    วันเรียงจากเก่าไปใหม่, ลีกเรียงตาม priority, คู่บอลเรียงตามเวลาเตะ
    """
    grouped = {}

    for item in fixtures:
        league_id = (item.get("league") or {}).get("id")
        if league_id not in leagues:
            continue

        teams = item.get("teams") or {}
        date_str, kickoff = kickoff_parts((item.get("fixture") or {}).get("date"), tz)

        grouped.setdefault(date_str, {}).setdefault(league_id, []).append((
            kickoff,
            (teams.get("home") or {}).get("name") or "ทีมเหย้า ?",
            (teams.get("away") or {}).get("name") or "ทีมเยือน ?",
        ))

    days = []
    for date_str in sorted(grouped):
        by_league = grouped[date_str]
        league_groups = [
            (leagues[league_id], sorted(by_league[league_id], key=lambda match: match[0]))
            for league_id in sorted(by_league, key=lambda key: (leagues[key]["priority"], key))
        ]
        days.append((date_str, league_groups))

    return days


def print_fixtures(days, total_count, dates):
    """แสดงผลแยกตามวัน → ลีก (วันที่ไม่มีคู่ในลีกที่ติดตามจะถูกข้ามไปเลย)"""
    print(f"โปรแกรมบอล {len(dates)} วัน ({dates[0]} ถึง {dates[-1]}) เวลาไทย — {TIMEZONE_NAME}")
    print("=" * 70)

    shown_count = sum(
        len(matches)
        for _, league_groups in days
        for _, matches in league_groups
    )

    if not days:
        if total_count:
            print(f"ช่วง {len(dates)} วันนี้ไม่มีคู่บอลของลีกที่ติดตามอยู่ใน leagues.json")
        else:
            print("ยังไม่มีโปรแกรมแข่งขันในช่วงวันดังกล่าว (หรือ API ยังไม่อัปเดตข้อมูล)")
        print("=" * 70)
        print(f"แสดง {shown_count} คู่ ({len(dates)} วัน) จากทั้งหมด {total_count} คู่")
        return

    for date_str, league_groups in days:
        print()
        print(f"===== {day_label(date_str, dates)} =====")
        for league, matches in league_groups:
            print()
            print(f"[{league['name_th']}] ({league['name_en']})")
            print("-" * 70)
            for kickoff, home, away in matches:
                print(f"  {kickoff}  {home} vs {away}")

    print()
    print("=" * 70)
    print(f"แสดง {shown_count} คู่ ({len(dates)} วัน) จากทั้งหมด {total_count} คู่")


def main():
    leagues = load_leagues()
    api_key = get_api_key()
    tz = get_bangkok_tz()
    dates = date_range(tz)

    print(f"กำลังดึงข้อมูลโปรแกรมบอลวันที่ {dates[0]} ถึง {dates[-1]} ...")
    fixtures = fetch_fixtures(api_key, dates[0], dates[-1])
    print_fixtures(group_by_day_and_league(fixtures, leagues, tz), len(fixtures), dates)


if __name__ == "__main__":
    main()
