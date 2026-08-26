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
    market_groups,
    read_main_line_flag,
    unwrap_outcome,
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


def report_main_lines(book_odds, catalog):
    """
    ไล่ดูว่าแต่ละเจ้าเปิดเส้นแฮนดิแคปอะไรบ้าง และปักธง mainLine ไว้ที่เส้นไหน
    เป็นจุดที่ใช้ตรวจกับ response จริงได้ตรงที่สุดว่าเราอ่านธง mainLine ถูกไหม
    """
    print("-" * 70)
    print("เส้นแฮนดิแคปที่แต่ละเจ้าเปิด และธง mainLine")
    print("-" * 70)

    for wanted in PANEL_BOOKS:
        slug = next((s for s in sorted(book_odds) if wanted in s.lower()), None)
        if slug is None:
            print(f"  {wanted:<12} ไม่มีเจ้านี้ในคู่นี้")
            continue

        rows, flagged = [], []
        for _, mapping in market_groups(book_odds[slug]):
            for market_id, outcome in sorted(mapping.items()):
                if market_id not in catalog:
                    continue
                flag = read_main_line_flag(unwrap_outcome(outcome))
                mark = {True: "  <== mainLine", False: "", None: "  (ไม่มีฟิลด์ mainLine)"}[flag]
                if flag is True:
                    flagged.append(market_id)
                value = catalog[market_id]
                shown = f"{value:+g}" if value else "0"  # กัน "+0" ที่อ่านแล้วสะดุด
                rows.append(f"  {'':<12} market {market_id} handicap {shown}{mark}")

        print(f"  {slug:<12} AH markets ที่เจอ: {len(rows)}")
        for row in rows or [f"  {'':<12} ไม่มีเส้นแฮนดิแคปที่อยู่ในสารบัญเลย"]:
            print(row)

        # เจ้ามือควรปักธงเส้นหลักแค่เส้นเดียว = 2 id (ฝั่งเหย้า + ฝั่งเยือน)
        # มากกว่านั้นแปลว่าข้อมูลดิบเองปักธงหลายเส้น ไม่ใช่โค้ดอ่านผิด (อ่านจาก path เดียวแล้ว)
        if len(flagged) > 2:
            print(f"  {'':<12} [!] ปักธง mainLine ไว้ {len(flagged)} ช่อง ({', '.join(flagged)})"
                  " — ในไฟล์ดิบเป็นแบบนี้จริง ไม่ใช่โค้ดเดาชั้นเอง")


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
            if market == "handicap" or not isinstance(prices, dict):
                continue  # ช่อง handicap มีข้อมูลประกอบปนอยู่ นับรวมแล้วตัวเลขจะเพี้ยน
            for field, value in prices.items():
                if field in price_fields:
                    counter["มีราคา" if value is not None else "ว่าง"] += 1

    print()
    print("=" * 70)
    print(f"อ่านราคาได้ {counter['มีราคา']} ช่อง / ว่าง {counter['ว่าง']} ช่อง")

    sources = Counter((book.get("handicap") or {}).get("source", "ไม่มีเส้นเลย")
                      for book in result["books"].values())
    if sources:
        print("ที่มาของเส้นแฮนดิแคปที่จะเอาไปพูด: "
              + ", ".join(f"{name} {count} เจ้า" for name, count in sources.most_common()))
    if counter["มีราคา"] == 0:
        print("[!] ยังอ่านไม่ได้เลย — เปิดไฟล์ดูโครงสร้างใหม่ได้ทันทีโดยไม่ต้องยิง API ซ้ำ")
    print("ยิง API ไป 0 ครั้ง (อ่านจากไฟล์ล้วน)")


if __name__ == "__main__":
    main()
