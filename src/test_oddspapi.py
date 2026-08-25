"""
สคริปต์สำรวจ OddsPapi API — เจาะดูราคาของ "บิ๊กแมตช์" จริง ๆ

ยิงไม่เกิน 2 requests (free tier 250 ครั้ง/เดือน ต้องประหยัด):
    1) GET /fixtures  คู่บอลวันนี้ถึงมะรืน
    2) (ไม่ยิง API)   ดูว่า OddsPapi เรียกลีก/ประเทศว่าอะไร แล้วคัดบิ๊กแมตช์ด้วยเงื่อนไขเข้ม
    3) GET /odds      ราคาของคู่ที่เลือก + เช็ครายชื่อเจ้ามือ

บทเรียนจากรอบก่อน: การ match ด้วย substring "premier league" ไปโดน
"Northern Territory Premier League" ของออสเตรเลีย รอบนี้จึงบังคับว่า
categoryName (ประเทศ) ต้องตรงกับที่กำหนด และ tournamentName ต้องตรงเป๊ะกับชื่อที่ยอมรับ

วิธีใช้:
    # ใส่ ODDSPAPI_KEY ลงใน .env ก่อน
    python3 src/test_oddspapi.py
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from api_football import fail, get_bangkok_tz

BASE_URL = "https://api.oddspapi.io/v4"
SPORT_ID = 10  # ฟุตบอล
REQUEST_TIMEOUT = 30

# กันเผลอยิงเกินโควตา — รอบนี้ต้องใช้ไม่เกิน 2 ครั้ง
MAX_REQUESTS = 2

DAYS_AHEAD = 3  # วันนี้ + พรุ่งนี้ + มะรืน

# ลีกใหญ่ที่ยอมรับ: ประเทศ (categoryName) ต้องตรง และชื่อรายการต้องตรงเป๊ะกับ alias ที่ระบุ
# เรียงตามลำดับความสำคัญ ใช้ตัดสินตอนต้องเลือกคู่ด้วย
BIG_LEAGUES = (
    ("Premier League (England)", {"england"}, {"premier league", "english premier league"}),
    ("Champions League", {"europe", "uefa", "international clubs", "international", "world"},
     {"champions league", "uefa champions league"}),
    ("La Liga (Spain)", {"spain"}, {"laliga", "la liga", "laliga santander", "primera division",
                                    "primera división"}),
    ("Serie A (Italy)", {"italy"}, {"serie a"}),
    ("Bundesliga (Germany)", {"germany"}, {"bundesliga", "1. bundesliga", "1 bundesliga"}),
)

# เจ้ามือที่อยากรู้ว่ามีให้ใช้ไหม (เอเชีย + เจ้าใหญ่)
KEY_BOOKMAKERS = ("sbobet", "singbet", "pinnacle", "crown", "bet365", "188bet", "ibcbet")
ASIAN_BOOKMAKERS = ("sbobet", "singbet", "crown", "188bet", "ibcbet")

# market id ที่สนใจ
MARKET_1X2 = (("home", 101), ("draw", 102), ("away", 103))
MARKET_AH_MINUS_05 = ("Asian Handicap -0.5", 1068, 1069)  # (ชื่อ, ฝั่งเหย้า, ฝั่งเยือน)
MARKET_AH_0 = ("Asian Handicap 0", 1072, 1073)

# ฟิลด์ที่ "อาจ" บอกจำนวนเจ้ามือของแต่ละคู่ (ยังไม่รู้ชื่อจริง เลยลองหลายแบบ)
BOOKMAKER_COUNT_FIELDS = ("bookmakerCount", "bookmakersCount", "numBookmakers",
                          "oddsCount", "bookmakers", "bookmakerIds")


class RequestCounter:
    """นับ request และปฏิเสธถ้าจะเกินเพดาน — โควตาเดือนละ 250 ครั้ง ต้องกันไว้"""

    def __init__(self, limit=MAX_REQUESTS):
        self.limit = limit
        self.count = 0

    def spend(self, label):
        if self.count >= self.limit:
            fail(f"[ERROR] จะยิงเกินเพดาน {self.limit} requests แล้ว ({label}) — หยุดเพื่อรักษาโควตา")
        self.count += 1
        print(f"  → ยิง API ครั้งที่ {self.count}/{self.limit}: {label}")


def get_api_key():
    """อ่าน ODDSPAPI_KEY จาก .env — ไม่มีให้หยุดทันที (ห้าม hardcode)"""
    load_dotenv()
    api_key = (os.getenv("ODDSPAPI_KEY") or "").strip()

    if not api_key:
        fail(
            "[ERROR] ไม่พบ ODDSPAPI_KEY: ยังไม่ได้ตั้งค่าใน .env",
            "วิธีแก้: เพิ่มบรรทัด ODDSPAPI_KEY=คีย์จริงของคุณ ลงในไฟล์ .env",
        )

    return api_key


def api_get(endpoint, api_key, params, counter, label):
    """เรียก endpoint ของ OddsPapi พร้อมจัดการ error ทุกกรณี ไม่ crash เงียบ"""
    counter.spend(label)

    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    query = dict(params)
    query["apiKey"] = api_key

    try:
        response = requests.get(url, params=query, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        fail(f"[ERROR] เชื่อมต่อ OddsPapi ไม่สำเร็จ: หมดเวลารอ (เกิน {REQUEST_TIMEOUT} วินาที)")
    except requests.exceptions.ConnectionError as exc:
        fail("[ERROR] เชื่อมต่อ OddsPapi ไม่ได้: ตรวจอินเทอร์เน็ต/DNS ของเครื่อง", f"รายละเอียด: {exc}")
    except requests.exceptions.RequestException as exc:
        fail(f"[ERROR] เรียก OddsPapi ล้มเหลว: {exc}")

    status = response.status_code

    if status in (401, 403):
        fail(
            f"[ERROR] ยืนยันตัวตนไม่ผ่าน (HTTP {status}): ODDSPAPI_KEY ผิดหรือหมดอายุ",
            "วิธีแก้: ตรวจค่า ODDSPAPI_KEY ใน .env และสถานะบัญชีบน OddsPapi",
            f"ข้อความจาก API: {response.text[:300]}",
        )

    if status == 429:
        fail(
            "[ERROR] ใช้โควตาหมดหรือยิงถี่เกินไป (HTTP 429)",
            "free tier มีแค่ 250 requests/เดือน — รอโควตารีเซ็ตหรืออัปเกรดแพลน",
            f"ข้อความจาก API: {response.text[:300]}",
        )

    if status != 200:
        fail(f"[ERROR] OddsPapi ตอบกลับสถานะที่ไม่คาดคิด: HTTP {status}",
             f"รายละเอียด: {response.text[:500]}")

    try:
        data = response.json()
    except ValueError:
        fail("[ERROR] ผลลัพธ์ไม่ใช่ JSON ที่ถูกต้อง", f"รายละเอียด: {response.text[:500]}")

    # บาง API ตอบ HTTP 200 แต่แจ้ง error ไว้ใน body
    if isinstance(data, dict):
        for key in ("error", "message", "errors"):
            value = data.get(key)
            if value and not data.get("data") and not data.get("fixtures"):
                fail(f"[ERROR] OddsPapi แจ้งข้อผิดพลาดกลับมา ({key}): {value}")

    return data


def as_list(payload, *keys):
    """ดึง list ออกจากผลลัพธ์ — รองรับทั้ง list ตรง ๆ และแบบห่อใน dict"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys + ("data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def dig(data, *keys, default=None):
    """ไล่อ่านค่าซ้อนชั้นแบบปลอดภัย"""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def normalize(text):
    """ตัดช่องว่างซ้ำ/เครื่องหมาย แล้วทำเป็นตัวพิมพ์เล็ก เพื่อเทียบชื่อแบบตรงตัว"""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def fixture_label(fixture):
    home = fixture.get("participant1Name") or "?"
    away = fixture.get("participant2Name") or "?"
    return f"{home} vs {away}"


def big_league_of(fixture):
    """
    คืนชื่อลีกใหญ่ที่ fixture นี้สังกัด หรือ None ถ้าไม่ใช่ลีกใหญ่
    เงื่อนไขเข้ม: ประเทศต้องตรง และชื่อรายการต้องตรงเป๊ะกับ alias
    (กัน "Northern Territory Premier League" ของออสเตรเลียหลุดเข้ามา)
    """
    category = normalize(fixture.get("categoryName"))
    tournament = normalize(fixture.get("tournamentName"))

    for name, categories, tournaments in BIG_LEAGUES:
        if category in categories and tournament in tournaments:
            return name
    return None


def big_league_rank(fixture):
    """ลำดับความสำคัญของลีกใหญ่ (เลขน้อย = สำคัญกว่า) ใช้ตัดสินตอนเลือกคู่"""
    name = big_league_of(fixture)
    for index, (league_name, _, _) in enumerate(BIG_LEAGUES):
        if league_name == name:
            return index
    return len(BIG_LEAGUES)


def bookmaker_count(fixture):
    """
    เดาจำนวนเจ้ามือของคู่นี้จากฟิลด์ที่ API อาจส่งมา — ไม่มีข้อมูลคืน None
    (ยังไม่รู้ชื่อฟิลด์จริง จึงลองหลายชื่อ ทั้งที่เป็นตัวเลขและที่เป็น list)
    """
    for field in BOOKMAKER_COUNT_FIELDS:
        value = fixture.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, (list, dict)):
            return len(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def market_price(book_odds, market_id):
    """
    ดึงราคาของ market ที่ต้องการจากข้อมูลเจ้ามือหนึ่งเจ้า
    รองรับหลายรูปแบบ (dict คีย์เป็น market id / list ของ object) เพราะยังไม่รู้โครงสร้างแน่ชัด
    """
    if isinstance(book_odds, dict):
        for key in (str(market_id), market_id):
            if key in book_odds:
                value = book_odds[key]
                if isinstance(value, dict):
                    for field in ("price", "odds", "value", "decimal"):
                        if field in value:
                            return value[field]
                    return value
                return value

        for nested in ("markets", "odds", "prices"):
            if isinstance(book_odds.get(nested), (dict, list)):
                found = market_price(book_odds[nested], market_id)
                if found is not None:
                    return found

    if isinstance(book_odds, list):
        for item in book_odds:
            if not isinstance(item, dict):
                continue
            item_id = item.get("marketId", item.get("market_id", item.get("id")))
            if str(item_id) == str(market_id):
                for field in ("price", "odds", "value", "decimal"):
                    if field in item:
                        return item[field]
                return item

    return None


def step1_fixtures(api_key, counter):
    """ขั้น 1: ดึงรายการคู่บอลวันนี้ถึงมะรืน แล้วเก็บเฉพาะที่มีราคา"""
    tz = get_bangkok_tz()
    today = datetime.now(tz).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=DAYS_AHEAD - 1)).strftime("%Y-%m-%d")

    print("=" * 78)
    print(f"ขั้น 1: GET /fixtures  (sportId={SPORT_ID}, from={date_from}, to={date_to})")
    print("=" * 78)

    payload = api_get(
        "fixtures", api_key,
        {"sportId": SPORT_ID, "from": date_from, "to": date_to},
        counter, f"/fixtures {date_from}..{date_to}",
    )

    fixtures = as_list(payload, "fixtures")
    with_odds = [f for f in fixtures if isinstance(f, dict) and f.get("hasOdds")]

    print(f"\nfixtures ทั้งหมด: {len(fixtures)} คู่")
    print(f"มีราคา (hasOdds=true): {len(with_odds)} คู่")

    if isinstance(payload, dict):
        print(f"คีย์ระดับบนสุดของ response: {sorted(payload)}")
    if with_odds:
        print(f"คีย์ของ fixture หนึ่งรายการ: {sorted(with_odds[0])}")

    return with_odds


def print_league_inventory(with_odds):
    """พิมพ์ categoryName + tournamentName ที่ไม่ซ้ำกัน — ให้เห็นว่า OddsPapi เรียกลีกว่าอะไรจริง ๆ"""
    print("\n" + "-" * 78)
    print("ลีก/ประเทศทั้งหมดที่เจอ (categoryName | tournamentName | จำนวนคู่)")
    print("-" * 78)

    tally = Counter(
        (fixture.get("categoryName") or "?", fixture.get("tournamentName") or "?")
        for fixture in with_odds
    )

    for (category, tournament), count in sorted(tally.items()):
        mark = ""
        sample = next((f for f in with_odds
                       if (f.get("categoryName") or "?") == category
                       and (f.get("tournamentName") or "?") == tournament), None)
        if sample is not None and big_league_of(sample):
            mark = "  <<< ลีกใหญ่"
        print(f"  {category:<28} | {tournament:<38} | {count}{mark}")

    print(f"\nรวม {len(tally)} รายการที่ไม่ซ้ำกัน")


def step2_pick(with_odds):
    """ขั้น 2: คัดบิ๊กแมตช์จากผลขั้น 1 (ไม่ยิง API เพิ่ม)"""
    print()
    print("=" * 78)
    print("ขั้น 2: ดูรายชื่อลีกจริง แล้วคัดบิ๊กแมตช์ (ไม่ยิง API)")
    print("=" * 78)

    if not with_odds:
        fail("[ERROR] ไม่มีคู่ที่มีราคาเลย — ทดสอบขั้น 3 ต่อไม่ได้")

    print_league_inventory(with_odds)

    big = [f for f in with_odds if big_league_of(f)]

    print("\n" + "-" * 78)
    print("คู่ในลีกใหญ่ที่ผ่านเงื่อนไขเข้ม (ประเทศตรง + ชื่อรายการตรงเป๊ะ)")
    print("-" * 78)

    if not big:
        print("  ไม่เจอคู่ในลีกใหญ่เลยในช่วง 3 วันนี้")
        print("  (ถ้าในตารางด้านบนมีลีกใหญ่ที่สะกดต่างจากที่โค้ดรู้จัก ให้เพิ่ม alias ใน BIG_LEAGUES)")

        fallback = max(with_odds, key=lambda f: (bookmaker_count(f) or 0))
        print(f"\nเลือกคู่ที่มีเจ้ามือมากที่สุดแทน: [{fallback.get('fixtureId')}] {fixture_label(fallback)}")
        print(f"   {fallback.get('categoryName')} | {fallback.get('tournamentName')}"
              f" | เจ้ามือ: {bookmaker_count(fallback) if bookmaker_count(fallback) is not None else 'ไม่ทราบ'}")
        return fallback

    for fixture in big:
        count = bookmaker_count(fixture)
        print(f"  [{fixture.get('fixtureId')}] {fixture_label(fixture)}")
        print(f"       {fixture.get('categoryName')} | {fixture.get('tournamentName')}"
              f" | {fixture.get('startTime') or fixture.get('startDate') or '?'}"
              f" | เจ้ามือ: {count if count is not None else 'ไม่ทราบ'}")

    # เลือกคู่ที่มีเจ้ามือเยอะสุด ถ้า API ไม่บอกจำนวน ให้ตัดสินด้วยลำดับความสำคัญของลีก
    chosen = max(big, key=lambda f: (bookmaker_count(f) or 0, -big_league_rank(f)))
    counts_known = any(bookmaker_count(f) is not None for f in big)

    print(f"\nเลือก: [{chosen.get('fixtureId')}] {fixture_label(chosen)}"
          f"  ({big_league_of(chosen)})")
    print("เหตุผล: " + ("มีเจ้ามือมากที่สุดในกลุ่มลีกใหญ่"
                        if counts_known else
                        "API ไม่ได้บอกจำนวนเจ้ามือในขั้นนี้ จึงเลือกตามลำดับความสำคัญของลีก"))
    return chosen


def bookmaker_checklist(bookmakers):
    """เช็คทีละเจ้าว่ามีในผลลัพธ์ไหม — คืน dict {ชื่อที่ค้นหา: slug จริงที่เจอ หรือ None}"""
    found = {}
    for name in KEY_BOOKMAKERS:
        found[name] = next((slug for slug in bookmakers if name in slug.lower()), None)
    return found


def step3_odds(api_key, fixture, counter):
    """ขั้น 3: ดึงราคาของคู่ที่เลือก แล้วสำรวจเจ้ามือ/market"""
    fixture_id = fixture.get("fixtureId")

    print()
    print("=" * 78)
    print(f"ขั้น 3: GET /odds  (fixtureId={fixture_id}) — {fixture_label(fixture)}")
    print("=" * 78)

    if fixture_id is None:
        fail("[ERROR] คู่ที่เลือกไม่มี fixtureId — ดึงราคาต่อไม่ได้")

    payload = api_get("odds", api_key, {"fixtureId": fixture_id}, counter, f"/odds fixtureId={fixture_id}")

    if isinstance(payload, dict):
        print(f"\nคีย์ระดับบนสุดของ response: {sorted(payload)}")

    book_odds = dig(payload, "bookmakerOdds", default=None)
    if book_odds is None:
        entries = as_list(payload, "odds")
        book_odds = entries[0].get("bookmakerOdds") if entries and isinstance(entries[0], dict) else None

    if not isinstance(book_odds, dict) or not book_odds:
        print("\n[!] ไม่พบ bookmakerOdds ในผลลัพธ์ — พิมพ์ JSON ดิบบางส่วนให้ดูโครงสร้างแทน")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
        return

    bookmakers = sorted(book_odds)
    print(f"\nเจ้ามือทั้งหมด {len(bookmakers)} เจ้า (เรียงตามตัวอักษร):")
    for index in range(0, len(bookmakers), 4):
        print("  " + "  ".join(f"{slug:<18}" for slug in bookmakers[index:index + 4]))

    print("\n" + "-" * 78)
    print("เช็คเจ้าสำคัญ")
    print("-" * 78)
    checklist = bookmaker_checklist(bookmakers)
    for name, slug in checklist.items():
        status = f"✅ มี  (slug จริง: {slug})" if slug else "❌ ไม่มี"
        print(f"  {name:<10} {status}")

    key_slugs = [slug for slug in checklist.values() if slug]
    if not key_slugs:
        print("\n[!] ไม่เจอเจ้าสำคัญเลย — แสดงราคาของ 5 เจ้าแรกแทน")
        key_slugs = bookmakers[:5]

    print("\n" + "-" * 78)
    print(f"1X2 (market {[mid for _, mid in MARKET_1X2]}) ของเจ้าสำคัญ")
    print("-" * 78)
    for slug in key_slugs:
        prices = {label: market_price(book_odds[slug], mid) for label, mid in MARKET_1X2}
        print(f"  {slug:<18} home {prices['home']}  |  draw {prices['draw']}  |  away {prices['away']}")

    for label, home_market, away_market in (MARKET_AH_MINUS_05, MARKET_AH_0):
        print("\n" + "-" * 78)
        print(f"{label} (market {home_market} = เหย้า, {away_market} = เยือน) ของเจ้าสำคัญ")
        print("-" * 78)
        for slug in key_slugs:
            home = market_price(book_odds[slug], home_market)
            away = market_price(book_odds[slug], away_market)
            print(f"  {slug:<18} home {home}  |  away {away}")

    sample_slug = next((s for s in key_slugs if "pinnacle" in s.lower()), key_slugs[0])
    print("\n" + "-" * 78)
    print(f"JSON ดิบของเจ้า '{sample_slug}' (ไว้เทียบว่า market id ที่ใช้ถูกไหม)")
    print("-" * 78)
    print(json.dumps(book_odds[sample_slug], ensure_ascii=False, indent=2)[:3000])

    asian_found = [slug for slug in bookmakers
                   if any(name in slug.lower() for name in ASIAN_BOOKMAKERS)]
    print("\n" + "=" * 78)
    print(f"สรุป: เจ้ามือทั้งหมด {len(bookmakers)} เจ้า | เจ้าเอเชีย {len(asian_found)} เจ้า"
          f"{' (' + ', '.join(asian_found) + ')' if asian_found else ''}")


def main():
    api_key = get_api_key()
    counter = RequestCounter()

    print(f"สำรวจ OddsPapi — สคริปต์นี้ยิงไม่เกิน {MAX_REQUESTS} requests\n")

    with_odds = step1_fixtures(api_key, counter)
    fixture = step2_pick(with_odds)
    step3_odds(api_key, fixture, counter)

    print("=" * 78)
    print(f"จบการสำรวจ — ยิง API ไปทั้งหมด {counter.count} ครั้ง (เพดาน {counter.limit})")


if __name__ == "__main__":
    main()
