"""
ทดสอบ distill_odds กับ raw response ที่เซฟไว้แล้ว — ไม่ยิง API เลยสักครั้ง

ใช้ไฟล์ที่ได้จาก ODDS_DEBUG_DUMP=1 (ค่าเริ่มต้น: debug_raw_odds.json ที่ root ของโปรเจกต์)
เอาไว้ตรวจว่าโครงสร้าง OddsPapi เปลี่ยนตรงไหน / โค้ดกลั่นอ่านได้ครบไหม โดยไม่เปลืองโควตา

วิธีใช้:
    ODDS_DEBUG_DUMP=1 python3 src/odds_data.py "Club Leon" "Real Salt Lake"   # เซฟไฟล์ครั้งเดียว
    python3 src/test_odds_offline.py                    # แล้วทดสอบซ้ำได้ไม่จำกัด
    python3 src/test_odds_offline.py path/to/other.json
"""

import json
import sys
from collections import Counter
from pathlib import Path

from odds_data import (
    MARKET_1X2_AWAY,
    MARKET_1X2_DRAW,
    MARKET_1X2_HOME,
    MARKET_AH_0_AWAY,
    MARKET_AH_0_HOME,
    MARKET_AH_M05_AWAY,
    MARKET_AH_M05_HOME,
    MARKET_OU25_OVER,
    MARKET_OU25_UNDER,
    PANEL_BOOKS,
    build_market_index,
    distill_odds,
)
from api_football import fail

DEFAULT_DUMP = Path(__file__).resolve().parent.parent / "debug_raw_odds.json"

WATCHED_MARKETS = (
    ("1X2 home", MARKET_1X2_HOME), ("1X2 draw", MARKET_1X2_DRAW), ("1X2 away", MARKET_1X2_AWAY),
    ("AH -0.5 home", MARKET_AH_M05_HOME), ("AH -0.5 away", MARKET_AH_M05_AWAY),
    ("AH 0 home", MARKET_AH_0_HOME), ("AH 0 away", MARKET_AH_0_AWAY),
    ("OU 2.5 over", MARKET_OU25_OVER), ("OU 2.5 under", MARKET_OU25_UNDER),
)


def load_dump(path):
    """อ่านไฟล์ dump แล้วคืน (bookmakerOdds, ข้อมูลประกอบ)"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(
            f"[ERROR] ไม่พบไฟล์ dump: {path}",
            "สร้างก่อนด้วย: ODDS_DEBUG_DUMP=1 python3 src/odds_data.py \"ทีมเหย้า\" \"ทีมเยือน\"",
        )
    except (OSError, ValueError) as exc:
        fail(f"[ERROR] อ่านไฟล์ dump ไม่ได้: {path}", f"รายละเอียด: {exc}")

    # ไฟล์ที่ dump_raw_odds เขียนไว้จะห่อด้วย {"fixtureId": ..., "response": {...}}
    response = data.get("response", data) if isinstance(data, dict) else {}
    fixture_id = data.get("fixtureId") if isinstance(data, dict) else None

    book_odds = response.get("bookmakerOdds") if isinstance(response, dict) else None
    if not isinstance(book_odds, dict) or not book_odds:
        fail(f"[ERROR] ไม่พบ bookmakerOdds ในไฟล์ {path} — โครงสร้างอาจเปลี่ยนอีกแล้ว")

    return book_odds, fixture_id


def report_index(book_odds):
    """ดูว่าเจ้าที่เราสนใจมี market ไหนบ้างหลังทำ index แบนราบ"""
    print("-" * 70)
    print("market ที่แต่ละเจ้ามีจริง (หลังแบน markets -> outcomes)")
    print("-" * 70)

    for wanted in PANEL_BOOKS:
        slug = next((s for s in sorted(book_odds) if wanted in s.lower()), None)
        if slug is None:
            print(f"  {wanted:<12} ไม่มีเจ้านี้ในคู่นี้")
            continue

        index = build_market_index(book_odds[slug])
        available = [label for label, market_id in WATCHED_MARKETS if str(market_id) in index]
        print(f"  {slug:<12} market ทั้งหมดใน index: {len(index)}")
        print(f"  {'':<12} ที่เราใช้: {', '.join(available) if available else 'ไม่มีสักอัน'}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DUMP
    book_odds, fixture_id = load_dump(path)

    print(f"ไฟล์: {path}")
    print(f"fixtureId: {fixture_id} | เจ้ามือในไฟล์: {len(book_odds)} เจ้า\n")

    report_index(book_odds)

    result = distill_odds(book_odds)
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # นับว่าอ่านราคาได้กี่ช่อง — ถ้าเป็น 0 แปลว่าโครงสร้างเปลี่ยนอีก
    counter = Counter()
    for book in result["books"].values():
        for market, prices in book.items():
            if isinstance(prices, dict):
                for value in prices.values():
                    counter["มีราคา" if value is not None else "ว่าง"] += 1

    print()
    print("=" * 70)
    print(f"อ่านราคาได้ {counter['มีราคา']} ช่อง / ว่าง {counter['ว่าง']} ช่อง")
    if counter["มีราคา"] == 0:
        print("[!] ยังอ่านไม่ได้เลย — เปิดไฟล์ดูโครงสร้างใหม่ได้ทันทีโดยไม่ต้องยิง API ซ้ำ")
    print("ยิง API ไป 0 ครั้ง (อ่านจากไฟล์ล้วน)")


if __name__ == "__main__":
    main()
