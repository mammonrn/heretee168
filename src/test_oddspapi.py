"""
สคริปต์สำรวจ OddsPapi API (รันครั้งเดียวพอ — free tier มีแค่ 250 requests/เดือน)

ยิงรวมไม่เกิน 3 requests:
    1) GET /fixtures  รายการคู่บอลวันนี้–พรุ่งนี้ (ดูว่าคู่ไหนมีราคา)
    2) (ไม่ยิง API)   คัดคู่เป้าหมายจากผลขั้น 1 ในหน่วยความจำ
    3) GET /odds      ราคาของคู่ที่เลือก ดูว่ามีเจ้าไหนบ้าง โครงสร้างหน้าตาอย่างไร

วิธีใช้:
    # ใส่ ODDSPAPI_KEY ลงใน .env ก่อน
    python3 src/test_oddspapi.py
"""

import json
import os
import sys
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from api_football import fail, get_bangkok_tz

BASE_URL = "https://api.oddspapi.io/v4"
SPORT_ID = 10  # ฟุตบอล
REQUEST_TIMEOUT = 30

# กันเผลอยิงเกินโควตา — สคริปต์นี้ต้องใช้ไม่เกิน 3 ครั้ง
MAX_REQUESTS = 3

# คู่/ลีกที่อยากได้เป็นตัวอย่างในขั้น 2 (คัดในหน่วยความจำ ไม่ยิง API เพิ่ม)
TARGET_TEAMS = ("manchester city", "bournemouth")
TARGET_TOURNAMENTS = ("premier league",)

# market id ที่สนใจ (ตามเอกสาร OddsPapi)
MARKET_AH_HOME = 1068  # Asian Handicap -0.5 ฝั่งเหย้า
MARKET_AH_AWAY = 1069  # Asian Handicap -0.5 ฝั่งเยือน
MARKET_1X2 = (("home", 101), ("draw", 102), ("away", 103))

# เจ้ามือฝั่งเอเชียที่อยากรู้ว่ามีให้ใช้ไหม
ASIAN_BOOKMAKERS = ("sbobet", "singbet", "pinnacle")

SAMPLE_FIXTURES = 10


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
    """
    ดึง list ออกจากผลลัพธ์ — รองรับทั้งแบบ list ตรง ๆ และแบบห่อใน dict
    (ยังไม่รู้โครงสร้างจริงของ OddsPapi จึงเผื่อไว้หลายแบบ)
    """
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


def fixture_label(fixture):
    home = fixture.get("participant1Name") or "?"
    away = fixture.get("participant2Name") or "?"
    return f"{home} vs {away}"


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
    """ขั้น 1: ดึงรายการคู่บอลวันนี้–พรุ่งนี้"""
    tz = get_bangkok_tz()
    today = datetime.now(tz).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    print("=" * 70)
    print(f"ขั้น 1: GET /fixtures  (sportId={SPORT_ID}, from={date_from}, to={date_to})")
    print("=" * 70)

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
    if fixtures and isinstance(fixtures[0], dict):
        print(f"คีย์ของ fixture หนึ่งรายการ: {sorted(fixtures[0])}")

    if not with_odds:
        print("\n[!] ไม่มีคู่ไหนมีราคาเลยในช่วงวันนี้–พรุ่งนี้")
        return with_odds

    print(f"\nตัวอย่าง {min(SAMPLE_FIXTURES, len(with_odds))} คู่แรกที่มีราคา:")
    print("-" * 70)
    for fixture in with_odds[:SAMPLE_FIXTURES]:
        print(f"  [{fixture.get('fixtureId')}] {fixture_label(fixture)}")
        print(f"       ลีก: {fixture.get('tournamentName')}"
              f" | เวลาแข่ง: {fixture.get('startTime') or fixture.get('startDate') or '?'}")

    return with_odds


def step2_pick(with_odds):
    """ขั้น 2: คัดคู่เป้าหมายจากผลขั้น 1 (ไม่ยิง API เพิ่ม)"""
    print()
    print("=" * 70)
    print("ขั้น 2: คัดคู่เป้าหมายจากข้อมูลที่ได้มาแล้ว (ไม่ยิง API)")
    print("=" * 70)

    if not with_odds:
        fail("[ERROR] ไม่มีคู่ที่มีราคาให้ทดสอบขั้น 3")

    for fixture in with_odds:
        names = f"{fixture.get('participant1Name') or ''} {fixture.get('participant2Name') or ''}".lower()
        tournament = (fixture.get("tournamentName") or "").lower()

        if any(team in names for team in TARGET_TEAMS) or any(t in tournament for t in TARGET_TOURNAMENTS):
            print(f"เจอคู่เป้าหมาย: [{fixture.get('fixtureId')}] {fixture_label(fixture)}")
            print(f"   ลีก: {fixture.get('tournamentName')}")
            return fixture

    fixture = with_odds[0]
    print("ไม่เจอคู่เป้าหมาย (Manchester City / Bournemouth / Premier League)")
    print(f"ใช้คู่แรกที่มีราคาแทน: [{fixture.get('fixtureId')}] {fixture_label(fixture)}")
    print(f"   ลีก: {fixture.get('tournamentName')}")
    return fixture


def step3_odds(api_key, fixture, counter):
    """ขั้น 3: ดึงราคาของคู่ที่เลือก แล้วสำรวจโครงสร้าง"""
    fixture_id = fixture.get("fixtureId")

    print()
    print("=" * 70)
    print(f"ขั้น 3: GET /odds  (fixtureId={fixture_id}) — {fixture_label(fixture)}")
    print("=" * 70)

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
    print(f"\nจำนวนเจ้ามือที่ price คู่นี้: {len(bookmakers)}")
    print(f"รายชื่อ: {', '.join(bookmakers)}")

    asian = [b for b in bookmakers if any(name in b.lower() for name in ASIAN_BOOKMAKERS)]
    print(f"\nเจ้าฝั่งเอเชียที่เจอ ({', '.join(ASIAN_BOOKMAKERS)}): {', '.join(asian) if asian else 'ไม่เจอเลย'}")

    print(f"\nAsian Handicap -0.5 (market {MARKET_AH_HOME} = เหย้า, {MARKET_AH_AWAY} = เยือน):")
    print("-" * 70)
    found_ah = False
    for slug in bookmakers:
        home = market_price(book_odds[slug], MARKET_AH_HOME)
        away = market_price(book_odds[slug], MARKET_AH_AWAY)
        if home is None and away is None:
            continue
        found_ah = True
        print(f"  {slug:<20} home {home}  |  away {away}")
    if not found_ah:
        print("  ไม่มีเจ้าไหนเสนอ market นี้ (หรือโครงสร้างต่างจากที่คาด)")

    print(f"\n1X2 (market {[mid for _, mid in MARKET_1X2]}):")
    print("-" * 70)
    found_1x2 = False
    for slug in bookmakers:
        prices = {label: market_price(book_odds[slug], mid) for label, mid in MARKET_1X2}
        if all(value is None for value in prices.values()):
            continue
        found_1x2 = True
        print(f"  {slug:<20} home {prices['home']}  |  draw {prices['draw']}  |  away {prices['away']}")
    if not found_1x2:
        print("  ไม่มีเจ้าไหนเสนอ market นี้ (หรือโครงสร้างต่างจากที่คาด)")

    sample_slug = next((b for b in bookmakers if "pinnacle" in b.lower()), bookmakers[0])
    print(f"\nJSON ดิบของเจ้า '{sample_slug}' (ดูโครงสร้างเต็ม):")
    print("-" * 70)
    print(json.dumps(book_odds[sample_slug], ensure_ascii=False, indent=2)[:4000])


def main():
    api_key = get_api_key()
    counter = RequestCounter()

    print("สำรวจ OddsPapi — สคริปต์นี้ยิงไม่เกิน 3 requests\n")

    with_odds = step1_fixtures(api_key, counter)
    fixture = step2_pick(with_odds)
    step3_odds(api_key, fixture, counter)

    print()
    print("=" * 70)
    print(f"จบการสำรวจ — ยิง API ไปทั้งหมด {counter.count} ครั้ง (เพดาน {counter.limit})")


if __name__ == "__main__":
    main()
