"""
ทดสอบ distill_odds กับ raw response ที่เซฟไว้แล้ว — ไม่ยิง API เลยสักครั้ง

ใช้ไฟล์ที่ได้จาก ODDS_DEBUG_DUMP=1 (ค่าเริ่มต้น: debug_raw_odds.json ที่ root ของโปรเจกต์)
เอาไว้ตรวจว่าโครงสร้าง OddsPapi เปลี่ยนตรงไหน / โค้ดกลั่นอ่านได้ครบไหม โดยไม่เปลืองโควตา

วิธีใช้:
    ODDS_DEBUG_DUMP=1 python3 src/odds_data.py "Club Leon" "Real Salt Lake"   # เซฟไฟล์ครั้งเดียว
    python3 src/test_odds_offline.py                    # แล้วทดสอบซ้ำได้ไม่จำกัด
    python3 src/test_odds_offline.py path/to/other.json
    python3 src/test_odds_offline.py --leagues          # ดูรายชื่อลีกจากรายการคู่ที่แคชไว้ใน cache.db
                                                        # (ไว้ตรวจว่ามีลีกจำลอง/แปลก ๆ หลุดตัวกรองไหม)

รายงานจะบอกด้วยว่าแต่ละเจ้าเปิดเส้นแฮนดิแคปอะไรบ้าง และปักธง mainLine ไว้เส้นไหน
ใช้ตรวจกับ response จริงได้ว่าเราอ่านธง mainLine ถูกที่ถูกชั้นหรือเปล่า
สารบัญ market อ่านจาก cache.db ถ้ามี ไม่มีก็ใช้ตัวสำรองในโค้ด — ไม่ยิง API ทั้งสองทาง
(ตัวสำรองครอบแค่ช่วงที่ยืนยันแล้ว -1.75 ถึง +0.5 เส้นนอกช่วงนี้จะไม่ถูกมองว่าเป็น AH
 จนกว่าจะมีสารบัญจริงจาก /v4/markets ในแคช)
"""

import json
import sys
from collections import Counter
from pathlib import Path

import cache_db
from odds_data import (
    FALLBACK_AH_CATALOG,
    MARKET_1X2_AWAY,
    MARKET_1X2_DRAW,
    MARKET_1X2_HOME,
    MARKET_AH_0_AWAY,
    MARKET_AH_0_HOME,
    MARKET_AH_M05_AWAY,
    MARKET_AH_M05_HOME,
    MARKET_CATALOG_CACHE_KEY,
    MARKET_CATALOG_TTL,
    MARKET_OU25_OVER,
    MARKET_OU25_UNDER,
    PANEL_BOOKS,
    build_market_index,
    distill_odds,
    is_simulated_fixture,
    SCAN_VERDICTS,
    scan_handicap_lines,
    scan_total_lines,
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
        slug = book_slug(book_odds, wanted)
        if slug is None:
            print(f"  {wanted:<12} ไม่มีเจ้านี้ในคู่นี้")
            continue

        index = build_market_index(book_odds[slug])
        available = [label for label, market_id in WATCHED_MARKETS if str(market_id) in index]
        print(f"  {slug:<12} market ทั้งหมดใน index: {len(index)}")
        print(f"  {'':<12} ที่เราใช้: {', '.join(available) if available else 'ไม่มีสักอัน'}")


def offline_catalog():
    """
    สารบัญ market สำหรับโหมดออฟไลน์ — อ่านจาก cache.db ก่อน ไม่มีค่อยใช้สารบัญสำรอง
    ห้ามยิง API เด็ดขาด ทั้งไฟล์นี้ต้องรันได้โดยไม่กินโควตาสักครั้ง
    """
    try:
        cache_db.init_db()
        cached = cache_db.get_odds(MARKET_CATALOG_CACHE_KEY, MARKET_CATALOG_TTL)
        if cached is not None and isinstance(cached["payload"], dict) and cached["payload"]:
            print(f"สารบัญ market: อ่านจากแคช {len(cached['payload'])} รายการ"
                  f" (ดึงเมื่อ {cached['created_at']})")
            return {str(k): v for k, v in cached["payload"].items()}
    except Exception as exc:
        print(f"อ่านสารบัญ market จากแคชไม่ได้ ({exc})")

    print(f"สารบัญ market: ใช้ตัวสำรองในโค้ด {len(FALLBACK_AH_CATALOG)} รายการ"
          " (ยังไม่เคยดึง /markets ลงแคช)")
    return dict(FALLBACK_AH_CATALOG)


def book_slug(book_odds, wanted):
    """ชื่อ key ของเจ้ามือที่ต้องการในไฟล์ dump — ไม่มีคืน None"""
    return next((s for s in sorted(book_odds) if wanted in s.lower()), None)


def line_number(handicap):
    """เลขเส้นแบบมีเครื่องหมาย ไว้พิมพ์ในรายงาน — เส้น 0 พิมพ์ "0" เฉย ๆ ไม่ใช่ "+0" """
    return f"{handicap:+g}" if handicap else "0"


def main_line_conclusions(book_odds, catalog):
    """
    ผลสแกนของทุกเจ้าในไฟล์ dump — {slug: {"handicap": ผลสแกน, "total": ผลสแกน}}
    ใช้ scan_handicap_lines / scan_total_lines ตัวเดียวกับที่ distill_book ใช้คำนวณ
    field "handicap" กับ "total" รายงานกับผล JSON จึงมาจากการตัดสินใจครั้งเดียวกัน
    """
    conclusions = {}

    for wanted in PANEL_BOOKS:
        slug = book_slug(book_odds, wanted)
        if slug is not None:
            conclusions[slug] = {
                "handicap": scan_handicap_lines(book_odds[slug], catalog),
                "total": scan_total_lines(book_odds[slug]),
            }

    return conclusions


def report_main_lines(book_odds, catalog):
    """
    ไล่ดูว่าแต่ละเจ้าเปิดเส้นแฮนดิแคปอะไรบ้าง ปักธง mainLine ไว้ที่ไหน และสรุปว่าระบบใช้เส้นไหน
    ทุกบรรทัดมาจากผลของ scan_handicap_lines() ล้วน ๆ ไม่มีการไล่สแกนซ้ำในนี้เลย
    """
    print("-" * 70)
    print("เส้นที่แต่ละเจ้าเปิด ธง mainLine และเส้นที่ระบบเลือกใช้ (แฮนดิแคป + สูง/ต่ำ)")
    print("-" * 70)

    conclusions = main_line_conclusions(book_odds, catalog)

    for wanted in PANEL_BOOKS:
        slug = book_slug(book_odds, wanted)
        if slug is None:
            print(f"  {wanted:<12} ไม่มีเจ้านี้ในคู่นี้")
            continue

        print(f"  {slug}")
        report_scan(conclusions[slug]["handicap"], "แฮนดิแคป",
                    lambda row: f"handicap {line_number(row['handicap'])}",
                    lambda main: f"market {main['market_ids']['home']}"
                                 f"/{main['market_ids']['away']}"
                                 f" (เส้น {line_number(main['handicap'])})")
        report_scan(conclusions[slug]["total"], "สูง/ต่ำ",
                    lambda row: f"{row['side']} {row['line']:g}",
                    lambda main: f"market {main['market_ids']['over']}"
                                 f"/{main['market_ids']['under']} (เส้น {main['line']:g})")


def report_scan(scan, label, describe_row, describe_main):
    """
    พิมพ์ผลสแกนของตลาดหนึ่งตลาด — ทุกบรรทัดมาจาก scan ที่ส่งเข้ามาล้วน ๆ
    ไม่มีการไล่ข้อมูลเองในนี้เลย รายงานจึงเล่าเรื่องเดียวกับ JSON เสมอ
    """
    rows = scan["outcomes"]
    print(f"  {'':<4}{label}: เจอ {len(rows)} ช่อง")

    for row in rows:
        mark = {True: "  <== mainLine", False: "", None: "  (ไม่มีฟิลด์ mainLine)"}[row["flag"]]
        price = row["price"] if row["price"] is not None else "ไม่มีราคา"
        print(f"  {'':<6} market {row['market_id']} {describe_row(row)} ราคา {price}{mark}")

    if scan["main"] is not None:
        print(f"  {'':<6} สรุป: ใช้เส้นหลัก {describe_main(scan['main'])}")
    else:
        print(f"  {'':<6} สรุป: ไม่ใช้เส้นหลัก — {SCAN_VERDICTS[scan['verdict']]}"
              " (ถอยไปเส้นสำรอง)")


def report_leagues():
    """
    ไล่ดูรายการคู่ที่แคชไว้ใน cache.db แล้วสรุปว่ามีลีกอะไรบ้าง อันไหนถูกตัดเป็นลีกจำลอง
    ใช้ตรวจว่าตัวกรองครอบคลุมพอหรือยัง โดยไม่ต้องยิง API ใหม่
    """
    cache_db.init_db()

    with cache_db._connect() as conn:
        rows = conn.execute(
            "SELECT cache_key, payload FROM odds_cache WHERE cache_key LIKE 'oddspapi_fixtures:%'"
        ).fetchall()

    if not rows:
        print("ยังไม่มีรายการคู่ในแคช — รัน analyze.py หนึ่งครั้งก่อน แล้วค่อยมาดูใหม่")
        return

    kept, dropped = Counter(), Counter()
    for row in rows:
        try:
            fixtures = json.loads(row["payload"])
        except ValueError:
            continue
        for fixture in fixtures:
            name = f"{fixture.get('categoryName') or '?'} | {fixture.get('tournamentName') or '?'}"
            (dropped if is_simulated_fixture(fixture) else kept)[name] += 1

    print(f"อ่านจากแคช {len(rows)} ชุด")
    print("-" * 70)
    print(f"ลีกที่ถูกตัดว่าเป็นลีกจำลอง/eSports ({sum(dropped.values())} คู่)")
    print("-" * 70)
    for name, count in dropped.most_common():
        print(f"  {count:>4}  {name}")
    if not dropped:
        print("  ไม่มีเลย")

    print()
    print("-" * 70)
    print(f"ลีกที่เก็บไว้ใช้จับคู่ ({sum(kept.values())} คู่) — ไล่ดูว่ามีอันไหนดูไม่ใช่บอลจริงหลงเหลือไหม")
    print("-" * 70)
    for name, count in kept.most_common():
        print(f"  {count:>4}  {name}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--leagues":
        report_leagues()
        return

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DUMP
    book_odds, fixture_id = load_dump(path)

    print(f"ไฟล์: {path}")
    print(f"fixtureId: {fixture_id} | เจ้ามือในไฟล์: {len(book_odds)} เจ้า\n")

    report_index(book_odds)

    catalog = offline_catalog()
    print()
    report_main_lines(book_odds, catalog)

    result = distill_odds(book_odds, catalog)
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # นับว่าอ่านราคาได้กี่ช่อง — ถ้าเป็น 0 แปลว่าโครงสร้างเปลี่ยนอีก
    counter = Counter()
    price_fields = ("home", "draw", "away", "over", "under")
    for book in result["books"].values():
        for market, prices in book.items():
            if market in ("handicap", "total") or not isinstance(prices, dict):
                continue  # ช่อง handicap มีข้อมูลประกอบปนอยู่ นับรวมแล้วตัวเลขจะเพี้ยน
            for field, value in prices.items():
                if field in price_fields:
                    counter["มีราคา" if value is not None else "ว่าง"] += 1

    print()
    print("=" * 70)
    print(f"อ่านราคาได้ {counter['มีราคา']} ช่อง / ว่าง {counter['ว่าง']} ช่อง")

    for label, field in (("แฮนดิแคป", "handicap"), ("สูง/ต่ำ", "total")):
        sources = Counter((book.get(field) or {}).get("source", "ไม่มีเส้นเลย")
                          for book in result["books"].values())
        if sources:
            print(f"ที่มาของเส้น{label}ที่จะเอาไปพูด: "
                  + ", ".join(f"{name} {count} เจ้า" for name, count in sources.most_common()))
    if counter["มีราคา"] == 0:
        print("[!] ยังอ่านไม่ได้เลย — เปิดไฟล์ดูโครงสร้างใหม่ได้ทันทีโดยไม่ต้องยิง API ซ้ำ")
    print("ยิง API ไป 0 ครั้ง (อ่านจากไฟล์ล้วน)")


if __name__ == "__main__":
    main()
