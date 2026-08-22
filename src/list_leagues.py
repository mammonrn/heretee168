"""
Phase 2 (ตัวช่วยรันครั้งเดียว) — ดึงรายชื่อลีก + league ID จาก API-Football (API-SPORTS)
เอาไว้เลือก ID ไปใส่ config ของบอท

วิธีใช้:
    python3 src/list_leagues.py                # ทั้งหมด (จำกัดจำนวนที่แสดง)
    python3 src/list_leagues.py England        # กรองเฉพาะประเทศ (ชื่อภาษาอังกฤษ)
    python3 src/list_leagues.py South Korea    # ชื่อประเทศหลายคำ พิมพ์ต่อกันได้เลย
    python3 src/list_leagues.py --all          # ทั้งหมดจริง ๆ ไม่จำกัดจำนวนแสดงผล
"""

import sys

from api_football import api_get, get_api_key

# ไม่ระบุประเทศจะได้ลีกมาหลายร้อยรายการ จึงจำกัดจำนวนที่แสดงกันจอท่วม (ข้ามด้วย --all)
DEFAULT_DISPLAY_LIMIT = 60


def parse_args(argv):
    """คืน (country, show_all) จาก argument ที่รับมา — ชื่อประเทศหลายคำจะถูกรวมเป็นชื่อเดียว"""
    args = list(argv)

    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__.strip())
        sys.exit(0)

    show_all = False
    if "--all" in args:
        show_all = True
        args = [arg for arg in args if arg != "--all"]

    country = " ".join(args).strip()
    return (country or None), show_all


def fetch_leagues(api_key, country):
    """เรียก /leagues — ถ้ามีชื่อประเทศให้ส่ง params country ไปกรองที่ฝั่ง API"""
    params = {"country": country} if country else None
    return api_get("leagues", api_key, params)


def build_rows(leagues):
    """แปลงผลลัพธ์ดิบเป็น (ประเทศ, ชื่อลีก, id, ประเภท) แล้วเรียงตามประเทศ → ชื่อลีก"""
    rows = []
    for item in leagues:
        league = item.get("league") or {}
        country_info = item.get("country") or {}

        league_id = league.get("id")
        if league_id is None:
            continue

        rows.append((
            country_info.get("name") or "ไม่ทราบประเทศ",
            league.get("name") or "ไม่ทราบชื่อลีก",
            league_id,
            (league.get("type") or "ไม่ทราบประเภท").lower(),
        ))

    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def print_leagues(rows, country, show_all):
    """แสดงผลรูปแบบ: [league ID] ชื่อลีก (ประเทศ) - ประเภท"""
    header = f"รายชื่อลีกของประเทศ {country}" if country else "รายชื่อลีกทั้งหมด"
    print(header)
    print("-" * 70)

    if not rows:
        if country:
            print(f"ไม่พบลีกของประเทศ '{country}'")
            print("ลองตรวจตัวสะกด (ต้องเป็นชื่อภาษาอังกฤษ เช่น England, Spain, Thailand)")
            print("หรือรันโดยไม่ใส่ชื่อประเทศเพื่อดูรายชื่อทั้งหมด")
        else:
            print("ไม่พบข้อมูลลีกจาก API (ลองใหม่อีกครั้ง)")
        print("-" * 70)
        return

    visible = rows if (show_all or len(rows) <= DEFAULT_DISPLAY_LIMIT) else rows[:DEFAULT_DISPLAY_LIMIT]

    for country_name, league_name, league_id, league_type in visible:
        print(f"[{league_id}] {league_name} ({country_name}) - {league_type}")

    print("-" * 70)

    hidden = len(rows) - len(visible)
    if hidden > 0:
        print(f"แสดง {len(visible)} จากทั้งหมด {len(rows)} ลีก (ซ่อนไว้ {hidden} ลีก)")
        print("ดูทั้งหมดด้วย --all หรือกรองด้วยชื่อประเทศ เช่น: python3 src/list_leagues.py England")
    print(f"เจอลีกทั้งหมด {len(rows)} ลีก")


def main():
    country, show_all = parse_args(sys.argv[1:])
    api_key = get_api_key()

    target = f"ของประเทศ {country} " if country else ""
    print(f"กำลังดึงรายชื่อลีก {target}...")

    rows = build_rows(fetch_leagues(api_key, country))
    print_leagues(rows, country, show_all)


if __name__ == "__main__":
    main()
