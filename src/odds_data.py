"""
Phase 5A — โมดูลดึง / จับคู่ / กลั่นข้อมูลราคาต่อรองจาก OddsPapi

ยังไม่ผูกกับ bot.py หรือ analyze.py — เฟสนี้ทำเป็นโมดูลเดี่ยวให้เรียกใช้ทีหลัง

หน้าที่หลัก:
    fetch_oddspapi_fixtures()  ดึงรายการคู่ที่มีราคา (1 request ต่อช่วงวัน)
    match_fixture()            จับคู่ fixture ของ API-SPORTS เข้ากับของ OddsPapi
    fetch_odds()               ดึงราคาของคู่นั้น
    distill_odds()             กลั่น bookmakerOdds ดิบให้เหลือเฉพาะที่ใช้วิเคราะห์ได้
    get_match_odds()           รวมสามขั้นข้างบนให้เรียกทีเดียว

ทดสอบเอง (ยิง API 2 ครั้ง):
    python3 src/odds_data.py                       # ใช้คู่ตัวอย่างที่ hardcode ไว้
    python3 src/odds_data.py "Real Madrid" "Barcelona"
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from api_football import fail, get_bangkok_tz

BASE = "https://api.oddspapi.io/v4"
SPORT_ID = 10  # ฟุตบอล
REQUEST_TIMEOUT = 30

# market id ของ OddsPapi
MARKET_1X2_HOME = 101
MARKET_1X2_DRAW = 102
MARKET_1X2_AWAY = 103
MARKET_AH_M05_HOME = 1068  # Asian Handicap -0.5 ฝั่งเหย้า
MARKET_AH_M05_AWAY = 1069
MARKET_AH_0_HOME = 1072  # Asian Handicap 0 (เสมอคืนทุน)
MARKET_AH_0_AWAY = 1073
MARKET_OU25_OVER = 1010  # Over/Under 2.5
MARKET_OU25_UNDER = 1011

# เจ้ามือที่สนใจ — แก้ตรงนี้จุดเดียวถ้าอยากเพิ่ม/ลด
ASIAN_BOOKS = ("sbobet", "singbet", "singbet-b")
SHARP_BOOKS = ("pinnacle",)
PANEL_BOOKS = ASIAN_BOOKS + SHARP_BOOKS  # เจ้าที่จะกลั่นราคาออกมาแสดง

# ยอมให้เวลาเตะต่างกันได้เท่าไรถึงถือว่าเป็นคู่เดียวกัน (เผื่อ timezone / การปัดเวลา)
KICKOFF_TOLERANCE = timedelta(hours=2)

# คำต่อท้าย/นำหน้าที่ตัดทิ้งได้อย่างปลอดภัยตอน normalize ชื่อทีม
# หมายเหตุสำคัญ: ไม่ตัด "united" / "city" / "wanderers" เพราะเป็นคำที่ใช้แยกทีมกันจริง ๆ
# (ถ้าตัด Manchester United กับ Manchester City จะเหลือ "manchester" เหมือนกัน = จับผิดคู่แน่นอน)
STRIPPABLE_TOKENS = {
    "fc", "afc", "cf", "sc", "ac", "ss", "ssc", "as", "cd", "ud", "sd", "fk", "sk",
    "bk", "if", "club", "calcio", "futbol", "football", "de", "the",
}

# ถ้าฝั่งหนึ่งมีคำพวกนี้แต่อีกฝั่งไม่มี แปลว่าคนละทีม (ทีมหญิง/ทีมสำรอง/ทีมเยาวชน)
DISQUALIFYING_TOKENS = {
    "women", "ladies", "w", "femenino", "feminine",
    "u17", "u18", "u19", "u20", "u21", "u23", "youth", "academy",
    "ii", "b", "reserves", "reserve",
}

MIN_CONTAINMENT_LEN = 5  # ชื่อสั้นกว่านี้ห้ามใช้กติกา "เป็นส่วนหนึ่งของอีกชื่อ" (สั้นไปเสี่ยงชนกัน)

logger = logging.getLogger("heretee.odds")


class RequestCounter:
    """นับ request และปฏิเสธถ้าจะเกินเพดาน — free tier มีแค่ 250 ครั้ง/เดือน"""

    def __init__(self, limit=2):
        self.limit = limit
        self.count = 0

    def spend(self, label):
        if self.count >= self.limit:
            fail(f"[ERROR] จะยิงเกินเพดาน {self.limit} requests แล้ว ({label}) — หยุดเพื่อรักษาโควตา")
        self.count += 1
        print(f"  → ยิง OddsPapi ครั้งที่ {self.count}/{self.limit}: {label}")


# ---------- การเรียก API ----------


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


def api_get(endpoint, params, api_key=None, counter=None):
    """
    เรียก endpoint ของ OddsPapi — จัดการ error ครบแบบเดียวกับ api_football.py
    (401/403 คีย์ผิด, 429 โควตาหมด, timeout, JSON เสีย, error ที่ซ่อนมาใน body ตอน HTTP 200)
    """
    api_key = api_key or get_api_key()
    if counter is not None:
        counter.spend(f"/{endpoint} {params}")

    url = f"{BASE}/{endpoint.lstrip('/')}"
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
        fail("[ERROR] ผลลัพธ์จาก OddsPapi ไม่ใช่ JSON ที่ถูกต้อง", f"รายละเอียด: {response.text[:500]}")

    if isinstance(data, dict):
        for key in ("error", "errors"):
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


def fetch_oddspapi_fixtures(dates, api_key=None, counter=None):
    """
    ยิง /fixtures ครั้งเดียวครอบทั้งช่วงวัน (dates = list ของ 'YYYY-MM-DD')
    คืนเฉพาะคู่ที่ hasOdds=true — คู่ที่ไม่มีราคาไม่มีประโยชน์กับเรา
    """
    if not dates:
        return []

    payload = api_get(
        "fixtures",
        {"sportId": SPORT_ID, "from": dates[0], "to": dates[-1]},
        api_key=api_key, counter=counter,
    )

    fixtures = as_list(payload, "fixtures")
    with_odds = [f for f in fixtures if isinstance(f, dict) and f.get("hasOdds")]
    logger.info("OddsPapi: fixtures %d คู่ มีราคา %d คู่ (%s ถึง %s)",
                len(fixtures), len(with_odds), dates[0], dates[-1])
    return with_odds


def fetch_odds(oddspapi_fixture_id, api_key=None, counter=None):
    """ยิง /odds ของคู่ที่ระบุ แล้วคืน bookmakerOdds ดิบ (dict ว่างถ้าไม่มี)"""
    payload = api_get("odds", {"fixtureId": oddspapi_fixture_id}, api_key=api_key, counter=counter)

    if isinstance(payload, dict) and isinstance(payload.get("bookmakerOdds"), dict):
        return payload["bookmakerOdds"]

    entries = as_list(payload, "odds")
    if entries and isinstance(entries[0], dict) and isinstance(entries[0].get("bookmakerOdds"), dict):
        return entries[0]["bookmakerOdds"]

    logger.warning("OddsPapi: ไม่พบ bookmakerOdds ของ fixtureId=%s", oddspapi_fixture_id)
    return {}


# ---------- การจับคู่ fixture ----------


def normalize_name(name):
    """
    ทำให้ชื่อทีมเทียบกันได้: ตัดเครื่องหมาย, เป็นตัวพิมพ์เล็ก, ตัดคำอย่าง FC/AC/CF ที่ไม่ได้แยกทีม
    เก็บคำอย่าง united/city ไว้เสมอ เพราะเป็นตัวแยกทีมจริง ๆ (ดูหมายเหตุที่ STRIPPABLE_TOKENS)
    """
    text = (name or "").lower()
    # ตัดจุด/อะพอสทรอฟีทิ้งก่อนโดยไม่แทนที่ด้วยช่องว่าง เพื่อให้ "A.C. Milan" -> "ac milan"
    # (ถ้าแทนด้วยช่องว่างจะได้ "a c milan" แล้ว "ac" จะไม่ถูกตัดออกตาม STRIPPABLE_TOKENS)
    text = re.sub(r"[.'’`]", "", text)
    cleaned = re.sub(r"[^\w\s]", " ", text)
    tokens = [t for t in cleaned.split() if t not in STRIPPABLE_TOKENS]
    return " ".join(tokens)


def name_tokens(name):
    return set(normalize_name(name).split())


def name_score(a, b):
    """
    ให้คะแนนความเหมือนของชื่อทีมสองชื่อ
        2 = ตรงกันเป๊ะหลัง normalize
        1 = ชื่อหนึ่งเป็นส่วนหนึ่งของอีกชื่อ (Newcastle ⊂ Newcastle United)
        0 = ไม่เข้าเกณฑ์ / เป็นคนละทีมแน่ ๆ
    """
    left, right = normalize_name(a), normalize_name(b)
    if not left or not right:
        return 0

    tokens_left, tokens_right = set(left.split()), set(right.split())

    # ทีมหญิง/สำรอง/เยาวชน ปนกับทีมชุดใหญ่ไม่ได้
    for token in DISQUALIFYING_TOKENS:
        if (token in tokens_left) != (token in tokens_right):
            return 0

    if left == right:
        return 2

    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= MIN_CONTAINMENT_LEN and shorter in longer:
        return 1

    return 0


def is_ambiguous(name, candidate_names):
    """
    ชื่อที่ให้มา "กำกวม" ไหม — คือไปเข้าเกณฑ์กับทีมมากกว่าหนึ่งทีมในรายการหรือเปล่า
    เช่น "Manchester" เข้าได้ทั้ง Manchester City และ Manchester United -> กำกวม ห้ามเดา
    ส่วน "Newcastle" เข้าได้ทีมเดียว (Newcastle United) -> ใช้ได้
    ตรวจจากข้อมูลจริงในวันนั้น ไม่ต้องฮาร์ดโค้ดรายชื่อคู่แข่งร่วมเมือง
    """
    matched = {normalize_name(other) for other in candidate_names if name_score(name, other) >= 1}
    return len(matched) > 1


def parse_iso(value):
    """แปลง ISO string เป็น datetime แบบมี timezone (ถือว่าเป็น UTC ถ้าไม่ระบุ)"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def kickoff_gap(fixture, kickoff_iso):
    """คืนผลต่างเวลาเตะระหว่าง fixture ของ OddsPapi กับเวลาที่เรามี — เทียบไม่ได้คืน None"""
    ours = parse_iso(kickoff_iso)
    theirs = parse_iso(fixture.get("startTime") or fixture.get("startDate"))
    if ours is None or theirs is None:
        return None
    return abs(theirs - ours)


def match_fixture(oddspapi_fixtures, home_name, away_name, kickoff_iso=None, league_hint=None):
    """
    จับคู่ fixture ของ API-SPORTS เข้ากับ fixture ของ OddsPapi แล้วคืน fixtureId (หรือ None)

    เกณฑ์:
      ก. เวลาเตะต่างกันไม่เกิน KICKOFF_TOLERANCE (2 ชม.) เผื่อ timezone/การปัดเวลา
         ถ้าไม่ได้ส่ง kickoff_iso มา จะข้ามข้อนี้ แล้วบังคับให้ชื่ออย่างน้อยฝั่งหนึ่งตรงเป๊ะแทน
      ข. ชื่อทั้งสองฝั่งต้องเข้าเกณฑ์ (home เทียบ home, away เทียบ away) โดยอย่างน้อย
         ฝั่งหนึ่งต้องตรงเป๊ะ — กันเคสที่ชื่อสั้นไปพ้องกับทีมอื่น
         และชื่อที่แมตช์แบบเป็นส่วนหนึ่งของอีกชื่อต้องไม่กำกวมในวันนั้น (ดู is_ambiguous)
      ค. ถ้าผ่านหลายคู่ เลือกคู่ที่เวลาเตะใกล้ที่สุด (ถ้าเทียบเวลาไม่ได้ ใช้คะแนนชื่อสูงสุด)

    ปรัชญา: ยอมไม่มีราคาดีกว่าจับผิดคู่ — ไม่มั่นใจเมื่อไร คืน None
    league_hint ใช้เป็นข้อมูลประกอบใน log เท่านั้น (ชื่อลีกสองเจ้าเรียกไม่เหมือนกัน
    จึงไม่เอามาเป็นเงื่อนไขตัดสิน)
    """
    fixtures = [f for f in (oddspapi_fixtures or []) if isinstance(f, dict)]
    home_pool = [f.get("participant1Name") for f in fixtures]
    away_pool = [f.get("participant2Name") for f in fixtures]

    candidates = []

    for fixture in fixtures:
        home_score = name_score(home_name, fixture.get("participant1Name"))
        away_score = name_score(away_name, fixture.get("participant2Name"))
        if home_score == 0 or away_score == 0:
            continue

        # ชื่อที่แมตช์แบบ "เป็นส่วนหนึ่งของอีกชื่อ" ต้องไม่กำกวม
        # (Manchester -> Manchester City / Manchester United ได้ทั้งคู่ = เดาไม่ได้ ข้ามไป)
        if home_score == 1 and is_ambiguous(home_name, home_pool):
            logger.info("OddsPapi: ชื่อ '%s' กำกวม (เข้าได้หลายทีม) — ไม่จับคู่", home_name)
            continue
        if away_score == 1 and is_ambiguous(away_name, away_pool):
            logger.info("OddsPapi: ชื่อ '%s' กำกวม (เข้าได้หลายทีม) — ไม่จับคู่", away_name)
            continue

        gap = kickoff_gap(fixture, kickoff_iso)

        if kickoff_iso is None:
            # ไม่มีเวลาเตะให้ยืนยัน จึงต้องเข้มกับชื่อ: อย่างน้อยฝั่งหนึ่งต้องตรงเป๊ะ
            if max(home_score, away_score) < 2:
                continue
        elif gap is None or gap > KICKOFF_TOLERANCE:
            continue

        candidates.append((fixture, home_score + away_score, gap))

    if not candidates:
        logger.info("OddsPapi: จับคู่ไม่ได้สำหรับ %s vs %s (%s) — ไม่มีคู่ไหนผ่านเกณฑ์ชื่อ+เวลา",
                    home_name, away_name, league_hint or "ไม่ระบุลีก")
        return None

    # เวลาใกล้สุดมาก่อน ถ้าเทียบเวลาไม่ได้ให้ใช้คะแนนชื่อ
    fixture, score, gap = min(
        candidates,
        key=lambda item: (item[2] if item[2] is not None else timedelta.max, -item[1]),
    )

    fixture_id = fixture.get("fixtureId")
    logger.info("OddsPapi: จับคู่ได้ %s vs %s -> fixtureId=%s (%s | คะแนนชื่อ %d, ห่างจากเวลาเตะ %s)",
                home_name, away_name, fixture_id, fixture.get("tournamentName"), score,
                gap if gap is not None else "ไม่ทราบ")
    return fixture_id


# ---------- การกลั่นราคา ----------


def find_outcome(book_data, market_id):
    """
    หา outcome ของ market ที่ต้องการจากข้อมูลเจ้ามือหนึ่งเจ้า
    รองรับหลายรูปแบบ (dict คีย์เป็น market id, ซ้อนใน markets/odds/outcomes, list ของ object)
    เพราะโครงสร้างจริงของ OddsPapi ยังไม่นิ่ง
    """
    if isinstance(book_data, dict):
        for key in (str(market_id), market_id):
            if key in book_data:
                return book_data[key]
        for nested in ("markets", "odds", "outcomes", "prices"):
            if isinstance(book_data.get(nested), (dict, list)):
                found = find_outcome(book_data[nested], market_id)
                if found is not None:
                    return found

    if isinstance(book_data, list):
        for item in book_data:
            if isinstance(item, dict):
                item_id = item.get("marketId", item.get("market_id", item.get("id")))
                if str(item_id) == str(market_id):
                    return item

    return None


def outcome_price(outcome):
    """
    ดึงราคา decimal ออกจาก outcome — รูปแบบหลักคือ outcome.players["0"].price
    เผื่อรูปแบบอื่นไว้ด้วย (price/odds/value ตรง ๆ หรือเป็นตัวเลขล้วน)
    """
    if outcome is None:
        return None
    if isinstance(outcome, (int, float)) and not isinstance(outcome, bool):
        return outcome

    if isinstance(outcome, dict):
        players = outcome.get("players")
        if isinstance(players, dict) and players:
            player = players.get("0") or players.get(0) or players[sorted(players)[0]]
            if isinstance(player, dict):
                for field in ("price", "odds", "value", "decimal"):
                    if field in player:
                        return player[field]
            elif isinstance(player, (int, float)):
                return player

        for field in ("price", "odds", "value", "decimal"):
            value = outcome.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value

    return None


def outcome_changed_at(outcome):
    """เวลาที่ราคานี้ขยับล่าสุด (ใช้ดู line movement) — ไม่มีคืน None"""
    if not isinstance(outcome, dict):
        return None
    for field in ("changedAt", "changed_at", "updatedAt", "lastChanged"):
        if outcome.get(field):
            return outcome[field]

    players = outcome.get("players")
    if isinstance(players, dict):
        for player in players.values():
            if isinstance(player, dict):
                for field in ("changedAt", "changed_at", "updatedAt"):
                    if player.get(field):
                        return player[field]
    return None


def price_of(book_data, market_id, changed_stamps):
    """ดึงราคาของ market เดียว พร้อมเก็บ changedAt ไว้หาค่าล่าสุดทีหลัง"""
    outcome = find_outcome(book_data, market_id)
    stamp = outcome_changed_at(outcome)
    if stamp:
        changed_stamps.append(stamp)
    return outcome_price(outcome)


def distill_book(book_data):
    """กลั่นราคาของเจ้ามือหนึ่งเจ้าให้เหลือเฉพาะ market ที่ใช้วิเคราะห์"""
    stamps = []

    book = {
        "1x2": {
            "home": price_of(book_data, MARKET_1X2_HOME, stamps),
            "draw": price_of(book_data, MARKET_1X2_DRAW, stamps),
            "away": price_of(book_data, MARKET_1X2_AWAY, stamps),
        },
        "ah_-0.5": {
            "home": price_of(book_data, MARKET_AH_M05_HOME, stamps),
            "away": price_of(book_data, MARKET_AH_M05_AWAY, stamps),
        },
        "ah_0": {
            "home": price_of(book_data, MARKET_AH_0_HOME, stamps),
            "away": price_of(book_data, MARKET_AH_0_AWAY, stamps),
        },
        "ou_2.5": {
            "over": price_of(book_data, MARKET_OU25_OVER, stamps),
            "under": price_of(book_data, MARKET_OU25_UNDER, stamps),
        },
    }

    book["changed_at"] = max(stamps) if stamps else None
    return book


def distill_odds(raw_odds):
    """
    กลั่น bookmakerOdds ดิบให้เหลือเฉพาะเจ้าใน PANEL_BOOKS และ market ที่ใช้จริง
    market ไหนเจ้านั้นไม่มี จะเป็น None ไม่ทำให้พัง
    """
    raw_odds = raw_odds if isinstance(raw_odds, dict) else {}
    slugs = sorted(raw_odds)
    notes = []
    books = {}

    for wanted in PANEL_BOOKS:
        slug = next((s for s in slugs if wanted in s.lower()), None)
        if slug is None:
            notes.append(f"ไม่มีราคาจาก {wanted}")
            continue

        book = distill_book(raw_odds[slug])
        books[slug] = book

        missing = [market for market in ("1x2", "ah_-0.5", "ah_0")
                   if all(value is None for value in book[market].values())]
        if missing:
            notes.append(f"{slug} ไม่มี market: {', '.join(missing)}")

    asian_found = [s for s in slugs if any(name in s.lower() for name in ASIAN_BOOKS)]

    if not books:
        notes.append("ไม่มีเจ้ามือที่สนใจเลยในคู่นี้")

    return {
        "books": books,
        "asian_book_count": len(asian_found),
        "total_books": len(slugs),
        "notes": notes,
    }


def get_match_odds(home_name, away_name, kickoff_iso, league_hint, oddspapi_fixtures,
                   api_key=None, counter=None):
    """
    รวมทุกขั้น: จับคู่ -> ดึงราคา -> กลั่น
    จับคู่ไม่ได้คืน None (ไม่ถือเป็น error — แค่คู่นี้ไม่มีราคาให้ใช้)
    """
    fixture_id = match_fixture(oddspapi_fixtures, home_name, away_name, kickoff_iso, league_hint)
    if fixture_id is None:
        return None

    raw = fetch_odds(fixture_id, api_key=api_key, counter=counter)
    distilled = distill_odds(raw)
    distilled["oddspapi_fixture_id"] = fixture_id
    distilled["match"] = f"{home_name} vs {away_name}"
    return distilled


# ---------- ทดสอบเอง (คุมโควตาไว้ที่ 2 requests) ----------


def main():
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)

    args = sys.argv[1:]
    home_name = args[0] if len(args) > 0 else "Real Madrid"
    away_name = args[1] if len(args) > 1 else "Barcelona"

    api_key = get_api_key()
    counter = RequestCounter(limit=2)

    tz = get_bangkok_tz()
    today = datetime.now(tz).date()
    dates = [(today + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(3)]

    print(f"ทดสอบ odds_data: {home_name} vs {away_name} (ช่วง {dates[0]} ถึง {dates[-1]})\n")

    fixtures = fetch_oddspapi_fixtures(dates, api_key=api_key, counter=counter)
    print(f"คู่ที่มีราคาในช่วงนี้: {len(fixtures)} คู่\n")

    # ทดสอบจาก CLI ไม่มีเวลาเตะจริงของ API-SPORTS จึงส่ง kickoff_iso=None (เข้มกับชื่อแทน)
    result = get_match_odds(home_name, away_name, None, None, fixtures,
                            api_key=api_key, counter=counter)

    if result is None:
        print("\nจับคู่ไม่ได้ — ลองเช็คชื่อทีมที่ OddsPapi ใช้จากรายการด้านล่าง (10 คู่แรก):")
        for fixture in fixtures[:10]:
            print(f"  [{fixture.get('fixtureId')}] {fixture.get('participant1Name')}"
                  f" vs {fixture.get('participant2Name')}"
                  f"  ({fixture.get('categoryName')} | {fixture.get('tournamentName')})")
    else:
        print("\nผลที่กลั่นแล้ว:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    print(f"\nยิง OddsPapi ไปทั้งหมด {counter.count} ครั้ง (เพดาน {counter.limit})")


if __name__ == "__main__":
    main()
