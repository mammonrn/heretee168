"""
Phase 5A — โมดูลดึง / จับคู่ / กลั่นข้อมูลราคาต่อรองจาก OddsPapi

ยังไม่ผูกกับ bot.py หรือ analyze.py — เฟสนี้ทำเป็นโมดูลเดี่ยวให้เรียกใช้ทีหลัง

หน้าที่หลัก:
    fetch_oddspapi_fixtures()  ดึงรายการคู่ที่มีราคา (1 request ต่อช่วงวัน)
    match_fixture()            จับคู่ fixture ของ API-SPORTS เข้ากับของ OddsPapi
    fetch_odds()               ดึงราคาของคู่นั้น
    fetch_market_catalog()     สารบัญ market id -> เลขเส้นแฮนดิแคป (แคช 7 วัน)
    find_main_line()           หาเส้นแฮนดิแคปหลักที่ตลาดใช้จริงจากธง mainLine
    distill_odds()             กลั่น bookmakerOdds ดิบให้เหลือเฉพาะที่ใช้วิเคราะห์ได้
    get_match_odds()           รวมทุกขั้นข้างบนให้เรียกทีเดียว

ทดสอบเอง (ยิง API ไม่เกิน 3 ครั้ง — ครั้งที่ 3 คือสารบัญ market ซึ่งแคชไว้ 7 วัน):
    python3 src/odds_data.py                       # ใช้คู่ตัวอย่างที่ hardcode ไว้
    python3 src/odds_data.py "Real Madrid" "Barcelona"
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import cache_db
from api_football import fail, get_bangkok_tz

# เปิด ODDS_DEBUG_DUMP=1 เพื่อเซฟ raw response ของ /odds ลงไฟล์
# มีไว้เพราะโครงสร้าง OddsPapi เคยเปลี่ยนมาแล้ว ครั้งหน้าจะได้ตรวจจากไฟล์ ไม่ต้องยิง API ซ้ำ
DEBUG_DUMP_ENV = "ODDS_DEBUG_DUMP"
DEBUG_DUMP_PATH = Path(__file__).resolve().parent.parent / "debug_raw_odds.json"

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

# ---- สารบัญ market ของแฮนดิแคป (Asian Handicap) ----
# เส้นแฮนดิแคปมี market id แยกกันทุกเส้น จะไปฮาร์ดโค้ดให้ครบทุกเส้นไม่ไหว
# จึงดึงสารบัญทั้งหมดจาก GET /v4/markets?sportId=10 แล้วแคชไว้ยาว ๆ (ดู fetch_market_catalog)
MARKETS_ENDPOINT = "markets"
MARKET_CATALOG_CACHE_KEY = f"oddspapi_markets:{SPORT_ID}"
MARKET_CATALOG_TTL = 7 * 24 * 60 * 60  # 7 วัน — สารบัญ market แทบไม่เปลี่ยน ยิงบ่อยก็เปลืองโควตาเปล่า

# ชื่อ market ที่ถือว่าเป็นแฮนดิแคปเอเชียแบบ "เต็มเวลา"
# ยังไม่เคยเห็น response จริงของ /v4/markets จึงรับทั้งชื่อเต็มและตัวย่อ "AH -0.5"
AH_NAME_HINTS = ("asian handicap", "handicap asian")
AH_NAME_ABBREVIATION = re.compile(r"^ah[\s:_-]")
# คำที่เจอในชื่อ market แล้วต้องตัดทิ้ง — เป็นแฮนดิแคปคนละแบบหรือคนละช่วงเวลา
# (แฮนดิแคปยุโรปเป็นแบบ 3 ทาง คนละเรื่องกับเอเชีย ส่วนครึ่งแรก/คอร์เนอร์/ใบเหลืองก็คนละตลาด)
AH_NAME_BLOCKERS = ("european", "3-way", "3 way", "three way", "corner", "card", "booking",
                    "half", "1st", "2nd", "first ", "second ", "period", "extra time",
                    "overtime", "penalt", "shot", "foul", "offside")

# สารบัญสำรอง ใช้เมื่อ /v4/markets เรียกไม่ได้ (โควตาหมด/เน็ตล่ม/โครงสร้างเปลี่ยน)
# ค่าที่ยืนยันแล้วจากของจริง: 1068 = AH -0.5, 1070 = AH -0.25, 1072 = AH 0,
#                            1074 = AH +0.25, 1076 = AH +0.5  (ทั้งหมดเป็นตัวเลขฝั่งเหย้า)
# ส่วน id เลขคี่คือฝั่งเยือนของคู่เดียวกัน (1068 คู่กับ 1069 ตามที่เห็นใน raw response จริง)
# ค่า handicap ของฝั่งเยือนในตารางนี้ใส่แบบกลับเครื่องหมายไว้เฉย ๆ ยังไม่เคยยืนยันกับของจริง
# แต่ไม่กระทบผลลัพธ์ เพราะเลขเส้นที่เอาไปแสดงอ่านจากฝั่งเหย้าเสมอ (ดู find_main_line)
FALLBACK_AH_CATALOG = {
    "1068": -0.5, "1069": 0.5,
    "1070": -0.25, "1071": 0.25,
    "1072": 0.0, "1073": 0.0,
    "1074": 0.25, "1075": -0.25,
    "1076": 0.5, "1077": -0.5,
}

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

# ถ้าฝั่งหนึ่งมีคำพวกนี้แต่อีกฝั่งไม่มี แปลว่าคนละทีม (ทีมหญิง/ทีมสำรอง/ทีมเยาวชน/ทีมจำลอง)
DISQUALIFYING_TOKENS = {
    "women", "ladies", "w", "femenino", "feminine",
    "u17", "u18", "u19", "u20", "u21", "u23", "youth", "academy",
    "ii", "b", "reserves", "reserve",
    # ทีมในลีกจำลอง (Simulated Reality League) ตั้งชื่อเลียนทีมจริง เช่น "Real Madrid SRL"
    "srl", "esports", "efootball", "virtual", "cyber",
}

# ลีกจำลอง / eSports ที่ไม่ใช่การแข่งจริง — ตัดทิ้งตั้งแต่ต้นทาง ไม่เอาเข้ารายการที่ใช้จับคู่เลย
# เหตุผล: ชื่อทีมเลียนของจริง ("Real Madrid SRL") ทำให้ชื่อทีมจริงดูกำกวมจนระบบไม่ยอมจับคู่
SIMULATED_LEAGUE_TOKENS = {"srl", "esports", "efootball", "virtual", "cyber", "simulated"}
SIMULATED_LEAGUE_PHRASES = ("simulated reality", "e-sports", "e sports", "virtual football",
                            "cyber league", "gt leagues", "fifa esports")

MIN_CONTAINMENT_LEN = 5  # ชื่อสั้นกว่านี้ห้ามใช้กติกา "เป็นส่วนหนึ่งของอีกชื่อ" (สั้นไปเสี่ยงชนกัน)

# คำโดด ๆ ที่พบทั่วไปในชื่อสโมสรทั่วโลก ถ้า normalize แล้วเหลือแค่คำพวกนี้คำเดียว
# ห้ามใช้กติกา containment เด็ดขาด — "athletic" คำเดียวไปเข้ากับ Dunfermline Athletic,
# Forfar Athletic, Saint Patrick's Athletic ได้หมด (เจอจริงจนจับ Athletic Club ไม่ได้)
GENERIC_SINGLE_TOKENS = {
    "athletic", "atletico", "united", "city", "real", "sporting", "sport", "olympic",
    "olympique", "dynamo", "dinamo", "rovers", "wanderers", "albion", "county", "town",
    "rangers", "academy", "national", "central", "juniors", "stars", "eagles", "lions",
}

# ไฟล์ชื่อเรียกอื่นของทีมเดียวกัน (API-Football กับ OddsPapi เรียกไม่เหมือนกัน)
TEAM_ALIASES_PATH = Path(__file__).resolve().parent.parent / "data" / "team_aliases.json"

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

    log_quota_usage(endpoint)
    return data


def log_quota_usage(endpoint):
    """
    บันทึกและ log จำนวน request ที่ยิงไป OddsPapi (free tier 250 ครั้ง/เดือน)
    ไม่ได้บังคับ rate limit — แค่ให้เห็นตัวเลขใน log ว่าใช้ไปเท่าไรแล้ว
    ถ้าเขียน db ไม่ได้ก็แค่ข้าม ไม่ให้กระทบการทำงานหลัก
    """
    try:
        cache_db.init_db()
        today, month = cache_db.record_odds_request()
        logger.info("โควตา OddsPapi: /%s | วันนี้ %d ครั้ง | เดือนนี้ %d ครั้ง (free tier 250/เดือน)",
                    endpoint, today, month)
    except Exception as exc:  # ตัวนับพังไม่ควรทำให้การดึงราคาพังไปด้วย
        logger.warning("บันทึกตัวนับ request ของ OddsPapi ไม่สำเร็จ (%s)", exc)


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


# ---------- สารบัญ market (ไว้แปลง market id เป็นเลขเส้นแฮนดิแคป) ----------


def parse_handicap_value(raw):
    """
    แปลงค่า handicap ที่ API ส่งมาเป็น float — รองรับทั้งตัวเลขและสตริง ("-0.5", "+0.25", "0")
    ค่าที่แปลงไม่ได้คืน None (จะได้ข้าม market นั้นไปเงียบ ๆ ไม่ทำให้พัง)
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip().replace("+", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def is_asian_handicap_market(name):
    """
    ชื่อ market นี้เป็นแฮนดิแคปเอเชียแบบเต็มเวลาไหม

    หมายเหตุ: ยังไม่เคยเห็น response จริงของ /v4/markets จึงกรองแบบ "เข้มไว้ก่อน" —
    ต้องมีคำว่า Asian Handicap และต้องไม่มีคำที่บอกว่าเป็นคนละตลาด/คนละช่วงเวลา
    (ครึ่งแรก คอร์เนอร์ ใบเหลือง แฮนดิแคปยุโรป ฯลฯ) เพราะถ้าหลุดเข้ามาแล้วมันดัน mainLine
    บอทจะพูดเลขเส้นผิดทันที ยอมกรองทิ้งเกินดีกว่าปล่อยเส้นผิดผ่าน
    """
    text = (name or "").strip().lower()
    if not text:
        return False
    if not any(hint in text for hint in AH_NAME_HINTS) and not AH_NAME_ABBREVIATION.match(text):
        return False
    return not any(blocker in text for blocker in AH_NAME_BLOCKERS)


def parse_market_catalog(payload):
    """
    แปลง response ของ /v4/markets เป็น dict {market id (str): handicap (float)}
    เก็บเฉพาะ Asian Handicap เต็มเวลาที่มีเลข handicap จริง ๆ
    """
    catalog = {}

    for entry in as_list(payload, "markets"):
        if not isinstance(entry, dict):
            continue

        market_id = entry.get("marketId", entry.get("id"))
        if market_id is None:
            continue
        if not is_asian_handicap_market(entry.get("marketName") or entry.get("name")):
            continue

        handicap = parse_handicap_value(entry.get("handicap"))
        if handicap is None:
            continue

        catalog[str(market_id)] = handicap

    return catalog


_market_catalog_cache = {"map": None}


def fetch_market_catalog(api_key=None, counter=None, force=False):
    """
    ดึงสารบัญ market ทั้งหมดของฟุตบอล (GET /v4/markets?sportId=10)
    คืน dict {market id (str): handicap (float)} เฉพาะ Asian Handicap เต็มเวลา

    ทำไมถึงไม่กินโควตา: สารบัญ market แทบไม่เปลี่ยน จึงแคชไว้ 7 วัน (MARKET_CATALOG_TTL)
    ทั้งในหน่วยความจำและใน cache.db — ต่อการวิเคราะห์หนึ่งคู่จึงไม่ยิงเพิ่มเลยแทบทุกครั้ง

    fail-safe เสมอ: ยิงไม่ได้/โครงสร้างเปลี่ยน/โควตาหมด -> คืน FALLBACK_AH_CATALOG
    (เท่าที่ยืนยันแล้ว) ไม่โยน error ออกไปให้การวิเคราะห์ล้ม
    """
    if not force and _market_catalog_cache["map"] is not None:
        return _market_catalog_cache["map"]

    if not force:
        try:
            cache_db.init_db()
            cached = cache_db.get_odds(MARKET_CATALOG_CACHE_KEY, MARKET_CATALOG_TTL)
            if cached is not None and isinstance(cached["payload"], dict):
                catalog = {str(k): v for k, v in cached["payload"].items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool)}
                if catalog:
                    logger.info("ใช้สารบัญ market จากแคช (%d AH markets, ดึงเมื่อ %s)",
                                len(catalog), cached["created_at"])
                    _market_catalog_cache["map"] = catalog
                    return catalog
        except Exception as exc:  # แคชพังไม่ควรทำให้ดึงสารบัญไม่ได้
            logger.warning("อ่านแคชสารบัญ market ไม่ได้ (%s) — จะยิงใหม่", exc)

    try:
        payload = api_get(MARKETS_ENDPOINT, {"sportId": SPORT_ID},
                          api_key=api_key, counter=counter)
        catalog = parse_market_catalog(payload)
    except SystemExit as exc:  # api_get ใช้ fail() ที่เรียก sys.exit
        logger.warning("ดึงสารบัญ market ไม่สำเร็จ (exit code=%s) — ใช้สารบัญสำรอง", exc.code)
        catalog = {}
    except Exception as exc:
        logger.warning("ดึงสารบัญ market ไม่สำเร็จ (%s) — ใช้สารบัญสำรอง", exc)
        catalog = {}

    if not catalog:
        logger.warning("สารบัญ market จาก /%s ว่างเปล่าหรือเรียกไม่ได้ — ใช้สารบัญสำรอง %d รายการ",
                       MARKETS_ENDPOINT, len(FALLBACK_AH_CATALOG))
        catalog = dict(FALLBACK_AH_CATALOG)
    else:
        logger.info("ดึงสารบัญ market ใหม่: เจอ AH markets %d รายการ", len(catalog))
        try:
            cache_db.init_db()
            cache_db.save_odds(MARKET_CATALOG_CACHE_KEY, catalog)
        except Exception as exc:  # เขียนแคชไม่ได้ก็แค่ยิงใหม่รอบหน้า
            logger.warning("เก็บสารบัญ market ลงแคชไม่สำเร็จ (%s)", exc)

    _market_catalog_cache["map"] = catalog
    return catalog


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
    real = drop_simulated(with_odds)
    logger.info("OddsPapi: fixtures %d คู่ มีราคา %d คู่ ใช้จับคู่ได้ %d คู่ (%s ถึง %s)",
                len(fixtures), len(with_odds), len(real), dates[0], dates[-1])
    return real


def dump_raw_odds(payload, fixture_id):
    """เซฟ raw response ลงไฟล์เมื่อเปิด ODDS_DEBUG_DUMP=1 — เขียนไม่สำเร็จก็แค่เตือน ไม่ให้พัง"""
    if (os.getenv(DEBUG_DUMP_ENV) or "").strip() not in ("1", "true", "TRUE", "yes"):
        return

    try:
        DEBUG_DUMP_PATH.write_text(
            json.dumps({"fixtureId": fixture_id, "response": payload}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("เซฟ raw response ของ /odds ไว้ที่ %s (ตรวจโครงสร้างได้โดยไม่ต้องยิง API ซ้ำ)",
                    DEBUG_DUMP_PATH)
    except OSError as exc:
        logger.warning("เซฟไฟล์ debug ไม่สำเร็จ (%s) — ข้ามไป", exc)


def fetch_odds(oddspapi_fixture_id, api_key=None, counter=None):
    """
    ยิง /odds ของคู่ที่ระบุ แล้วคืน bookmakerOdds ดิบ (dict ว่างถ้าไม่มี)
    ตั้ง ODDS_DEBUG_DUMP=1 ถ้าอยากได้ raw response เก็บไว้ตรวจโครงสร้างทีหลัง
    """
    payload = api_get("odds", {"fixtureId": oddspapi_fixture_id}, api_key=api_key, counter=counter)
    dump_raw_odds(payload, oddspapi_fixture_id)

    if isinstance(payload, dict) and isinstance(payload.get("bookmakerOdds"), dict):
        return payload["bookmakerOdds"]

    entries = as_list(payload, "odds")
    if entries and isinstance(entries[0], dict) and isinstance(entries[0].get("bookmakerOdds"), dict):
        return entries[0]["bookmakerOdds"]

    logger.warning("OddsPapi: ไม่พบ bookmakerOdds ของ fixtureId=%s", oddspapi_fixture_id)
    return {}


# ---------- การจับคู่ fixture ----------


def is_simulated_fixture(fixture):
    """
    คู่นี้มาจากลีกจำลอง/eSports หรือเปล่า — เช็คทั้งชื่อรายการ ชื่อประเทศ และชื่อทีม
    ใช้การเทียบแบบคำ (token) สำหรับคำสั้นอย่าง srl กันไปโดนคำอื่นที่บังเอิญมีตัวอักษรเรียงกัน
    """
    if not isinstance(fixture, dict):
        return False

    fields = (fixture.get("tournamentName"), fixture.get("categoryName"),
              fixture.get("participant1Name"), fixture.get("participant2Name"))

    for value in fields:
        text = normalize_name(value)
        if not text:
            continue
        if SIMULATED_LEAGUE_TOKENS & set(text.split()):
            return True
        if any(phrase in text for phrase in SIMULATED_LEAGUE_PHRASES):
            return True

    return False


def drop_simulated(fixtures):
    """คัดคู่จากลีกจำลองออก แล้ว log ว่าตัดอะไรไปบ้าง (ไว้ตรวจว่ากรองโดนของจริงหรือเปล่า)"""
    keep, dropped = [], []

    for fixture in fixtures or []:
        (dropped if is_simulated_fixture(fixture) else keep).append(fixture)

    if dropped:
        names = sorted({(f.get("tournamentName") or "?") for f in dropped})
        logger.info("OddsPapi: ตัดคู่จากลีกจำลอง/eSports ทิ้ง %d คู่ (ลีก: %s)",
                    len(dropped), ", ".join(names[:5]) + (" ..." if len(names) > 5 else ""))

    return keep


_alias_cache = {"path": None, "map": None}


def load_team_aliases(path=TEAM_ALIASES_PATH):
    """
    อ่าน data/team_aliases.json แล้วคืน dict: ชื่อที่ normalize แล้ว -> รหัสกลุ่ม
    ทุกชื่อในกลุ่มเดียวกันได้รหัสเดียวกัน = ถือว่าเป็นทีมเดียวกัน
    ไฟล์หาย / JSON เสีย -> คืน dict ว่าง (ระบบยังทำงานได้ แค่ไม่มี alias ช่วย)
    """
    if _alias_cache["path"] == str(path) and _alias_cache["map"] is not None:
        return _alias_cache["map"]

    mapping = {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        groups = data.get("aliases", []) if isinstance(data, dict) else data

        for index, group in enumerate(groups or []):
            if not isinstance(group, list):
                continue
            for name in group:
                normalized = normalize_name(name)
                if normalized:
                    mapping[normalized] = f"alias{index}"
    except FileNotFoundError:
        logger.info("ไม่พบไฟล์ alias ทีม: %s — ข้ามการใช้ alias", path)
    except (OSError, ValueError) as exc:
        logger.warning("อ่านไฟล์ alias ทีมไม่ได้ (%s) — ข้ามการใช้ alias", exc)

    _alias_cache.update(path=str(path), map=mapping)
    return mapping


def alias_key(normalized_name):
    """รหัสกลุ่ม alias ของชื่อนี้ (ถ้ามี) — ไม่มีคืน None"""
    return load_team_aliases().get(normalized_name)


def normalize_name(name):
    """
    ทำให้ชื่อทีมเทียบกันได้: ตัดเครื่องหมาย, เป็นตัวพิมพ์เล็ก, ตัดคำอย่าง FC/AC/CF ที่ไม่ได้แยกทีม
    เก็บคำอย่าง united/city ไว้เสมอ เพราะเป็นตัวแยกทีมจริง ๆ (ดูหมายเหตุที่ STRIPPABLE_TOKENS)
    """
    return normalize_details(name)[0]


def normalize_details(name):
    """
    เหมือน normalize_name แต่บอกด้วยว่ามีการตัดคำทิ้งไปหรือเปล่า
    คืน (ชื่อที่ normalize แล้ว, ตัดคำทิ้งไปไหม)

    ที่ต้องรู้ว่า "ตัดไปไหม" เพราะชื่อที่เหลือคำเดียวเพราะโดนตัด (เช่น "Athletic Club" -> "athletic")
    เชื่อถือไม่ได้พอจะเอาไป match แบบ containment (ดู name_score)
    """
    text = (name or "").lower()
    # ตัดจุด/อะพอสทรอฟีทิ้งก่อนโดยไม่แทนที่ด้วยช่องว่าง เพื่อให้ "A.C. Milan" -> "ac milan"
    # (ถ้าแทนด้วยช่องว่างจะได้ "a c milan" แล้ว "ac" จะไม่ถูกตัดออกตาม STRIPPABLE_TOKENS)
    text = re.sub(r"[.'’`]", "", text)
    cleaned = re.sub(r"[^\w\s]", " ", text)
    raw_tokens = cleaned.split()
    tokens = [t for t in raw_tokens if t not in STRIPPABLE_TOKENS]
    return " ".join(tokens), len(tokens) < len(raw_tokens)


def name_tokens(name):
    return set(normalize_name(name).split())


def name_score(a, b):
    """
    ให้คะแนนความเหมือนของชื่อทีมสองชื่อ
        2 = ตรงกันเป๊ะหลัง normalize
        1 = ชื่อหนึ่งเป็นส่วนหนึ่งของอีกชื่อ (Newcastle ⊂ Newcastle United)
        0 = ไม่เข้าเกณฑ์ / เป็นคนละทีมแน่ ๆ
    """
    left, left_reduced = normalize_details(a)
    right, right_reduced = normalize_details(b)
    if not left or not right:
        return 0

    tokens_left, tokens_right = set(left.split()), set(right.split())

    # ทีมหญิง/สำรอง/เยาวชน ปนกับทีมชุดใหญ่ไม่ได้
    for token in DISQUALIFYING_TOKENS:
        if (token in tokens_left) != (token in tokens_right):
            return 0

    if left == right:
        return 2

    # ชื่อเรียกอื่นของทีมเดียวกัน (เช่น Athletic Club = Athletic Bilbao) ถือว่าตรงกันเป๊ะ
    left_alias, right_alias = alias_key(left), alias_key(right)
    if left_alias and left_alias == right_alias:
        return 2

    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < MIN_CONTAINMENT_LEN or shorter not in longer:
        return 0

    # ชื่อที่เหลือ "คำเดียว" ห้าม match แบบ containment ถ้า
    #   (ก) มันเหลือคำเดียวเพราะโดนตัดคำอย่าง Club/FC ทิ้ง  หรือ
    #   (ข) คำนั้นเป็นคำสามัญที่สโมสรทั่วโลกใช้กัน (athletic, united, city, ...)
    # เคสแบบนี้ต้องตรงเป๊ะหรือมี alias เท่านั้น ("Athletic Club" ไม่ควรไปเข้ากับ Forfar Athletic)
    for side, reduced in ((left, left_reduced), (right, right_reduced)):
        tokens = side.split()
        if len(tokens) == 1 and (reduced or tokens[0] in GENERIC_SINGLE_TOKENS):
            return 0

    return 1


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
    # กันอีกชั้นเผื่อรายการที่ส่งเข้ามาไม่ได้ผ่าน fetch_oddspapi_fixtures (เช่น มาจากแคชเก่า)
    fixtures = [f for f in (oddspapi_fixtures or [])
                if isinstance(f, dict) and not is_simulated_fixture(f)]
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


def market_entries(book_data):
    """คืน list ของ (key, entry) จากส่วน markets ของเจ้ามือหนึ่งเจ้า — รองรับ dict, list และโครงสร้างแบน"""
    if not isinstance(book_data, (dict, list)):
        return []

    if isinstance(book_data, list):
        return [(entry.get("marketId", entry.get("id")), entry)
                for entry in book_data if isinstance(entry, dict)]

    for nested in ("markets", "odds", "prices"):
        container = book_data.get(nested)
        if isinstance(container, dict):
            return list(container.items())
        if isinstance(container, list):
            return [(entry.get("marketId", entry.get("id")), entry)
                    for entry in container if isinstance(entry, dict)]

    # ไม่มีชั้น markets เลย ถือว่า book_data เองคือตาราง market (โครงสร้างแบบเก่า)
    return list(book_data.items())


def build_market_index(book_data):
    """
    แบนราบราคาของเจ้ามือหนึ่งเจ้าเป็น {market_id (str): outcome dict}

    ทำไมต้องทำ index: OddsPapi **ไม่ได้** ใช้ทุก market id เป็น key ระดับบนของ markets
    แต่จับ market ที่เกี่ยวข้องกันเป็นกลุ่ม แล้วใช้ id ตัวแรกของกลุ่มเป็น key เท่านั้น
    ส่วน id ที่เหลือของกลุ่มไปอยู่เป็น key ใน outcomes เช่น (ของจริงจาก raw response)

        markets["101"]["outcomes"] = {"101": ..., "102": ..., "103": ...}   # 1X2 ครบสามผล
        markets["1068"]["outcomes"] = {"1068": ..., "1069": ...}            # AH -0.5 สองฝั่ง

    ฉะนั้นการไปหา markets["102"] หรือ markets["1069"] ตรง ๆ จะไม่เจอ (ได้ null ทุกช่อง)
    วิธีที่ถูกคือไล่ทุกกลุ่ม แล้วเก็บ key ที่อยู่ใน outcomes ทั้งหมดลงตารางแบนใบเดียว
    """
    index = {}

    def remember(market_id, outcome, entry):
        """เก็บ outcome ลง index — ถ้า changedAt อยู่ระดับกลุ่ม ให้พ่วงติดไปกับ outcome ด้วย"""
        if isinstance(outcome, dict):
            stamp = outcome_changed_at(entry) if isinstance(entry, dict) else None
            if stamp and not outcome_changed_at(outcome):
                outcome = dict(outcome, changedAt=stamp)  # copy ตื้น ๆ ไม่แก้ข้อมูลต้นฉบับ
        index.setdefault(str(market_id), outcome)

    for key, entry in market_entries(book_data):
        outcomes = entry.get("outcomes") if isinstance(entry, dict) else None

        if isinstance(outcomes, dict):
            # บางรูปแบบ outcomes เป็นตัว outcome เดี่ยว ๆ เลย (มี players อยู่ตรงนั้น)
            # ไม่ได้ key ด้วย market id — ใช้ key ของกลุ่มแทน
            if "players" in outcomes:
                if key is not None:
                    remember(key, outcomes, entry)
                continue
            for outcome_id, outcome in outcomes.items():
                remember(outcome_id, outcome, entry)
            continue

        if isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                outcome_id = outcome.get("outcomeId", outcome.get("marketId", outcome.get("id")))
                remember(outcome_id if outcome_id is not None else key, outcome, entry)
            continue

        # ไม่มีชั้น outcomes — ตัว entry เองคือ outcome (โครงสร้างแบบเก่า)
        if key is not None:
            index.setdefault(str(key), entry)

    return index


def unwrap_outcome(entry):
    """
    แกะชั้น "outcomes" ออกจน (เกือบ) ถึงตัวที่มี players

    โครงสร้างจริงของ OddsPapi ที่ยืนยันจากการรันจริงคือ
        bookmakerOdds[slug]["markets"][market_id]["outcomes"]["players"]["0"]["price"]
    เดิมโค้ดหยุดที่ระดับ market_id แล้วส่งต่อให้ outcome_price เลย ทำให้หา players ไม่เจอ
    ราคาจึงออกมาเป็น null ทั้งหมด — ฟังก์ชันนี้แกะชั้นที่หายไปให้
    (รองรับทั้งกรณี outcomes เป็น dict และเป็น list ของ outcome)
    """
    current = entry
    for _ in range(4):  # กันวนไม่จบถ้าโครงสร้างแปลก
        if not isinstance(current, dict) or "players" in current:
            return current

        inner = current.get("outcomes")
        if isinstance(inner, dict):
            current = inner
            continue
        if isinstance(inner, list):
            first = next((item for item in inner if isinstance(item, dict)), None)
            if first is None:
                return current
            current = first
            continue
        return current

    return current


def find_outcome(book_data, market_id):
    """
    หา outcome ของ market ที่ต้องการจากข้อมูลเจ้ามือหนึ่งเจ้า
    ทำ index แบนราบก่อนแล้วค่อย lookup ด้วย market id ตรง ๆ (ดูเหตุผลใน build_market_index)
    """
    return unwrap_outcome(build_market_index(book_data).get(str(market_id)))


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


def price_of(market_index, market_id, changed_stamps):
    """ดึงราคาของ market เดียวจาก index พร้อมเก็บ changedAt ไว้หาค่าล่าสุดทีหลัง"""
    outcome = unwrap_outcome(market_index.get(str(market_id)))

    stamp = outcome_changed_at(outcome)
    if stamp:
        changed_stamps.append(stamp)

    return outcome_price(outcome)


# ---------- หาเส้นแฮนดิแคปหลักที่ตลาดใช้จริง (mainLine) ----------

# ชื่อฟิลด์ที่อาจใช้บอกว่า outcome นี้คือเส้นหลัก — ยืนยันมาแล้วว่าใช้ "mainLine"
# ที่เผื่อชื่ออื่นไว้เพราะ OddsPapi เคยเปลี่ยนโครงสร้างมาแล้ว และของถูกก็ยังถูกอยู่ดี
MAIN_LINE_FIELDS = ("mainLine", "mainline", "main_line", "isMainLine")


def flag_value(raw):
    """แปลงค่าที่อ่านได้เป็น True/False — รองรับทั้ง bool, "true"/"false" และ 0/1"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes", "y")
    return None


def flag_from_dict(data):
    """อ่าน mainLine จาก dict ใบเดียว — ไม่มีฟิลด์เลยคืน None (ต่างจาก False)"""
    if not isinstance(data, dict):
        return None
    for field in MAIN_LINE_FIELDS:
        if field in data:
            return flag_value(data[field])
    return None


def main_line_flag(outcome):
    """
    outcome นี้เป็นเส้นหลักไหม — คืน True/False ตามที่ API บอก, None เมื่อไม่มีฟิลด์ mainLine เลย

    ไล่หาสามชั้น (ระดับ outcome -> ระดับที่แกะ outcomes แล้ว -> ในแต่ละ player)
    เพราะยืนยันมาแค่ว่า "ทุก outcome มีฟิลด์ mainLine" แต่ยังไม่ได้ยืนยันว่าอยู่ชั้นไหนแน่
    """
    direct = flag_from_dict(outcome)
    if direct is not None:
        return direct

    inner = unwrap_outcome(outcome)
    if inner is not outcome:
        nested = flag_from_dict(inner)
        if nested is not None:
            return nested
    else:
        inner = outcome

    players = inner.get("players") if isinstance(inner, dict) else None
    if isinstance(players, dict):
        for player in players.values():
            value = flag_from_dict(player)
            if value is not None:
                return value

    return None


def market_groups(book_data):
    """
    คืน list ของ (คีย์กลุ่ม, {market id: outcome}) ตามที่ OddsPapi จัดกลุ่มมา

    ต่างจาก build_market_index ตรงที่ "ไม่แบนราบ" — เก็บไว้ว่า market id ไหนอยู่กลุ่มเดียวกัน
    ซึ่งจำเป็นสำหรับแฮนดิแคป เพราะฝั่งเหย้ากับฝั่งเยือนของเส้นเดียวกันอยู่กลุ่มเดียวกันเสมอ
    (ของจริง: markets["1068"]["outcomes"] = {"1068": ..., "1069": ...})
    """
    groups = []

    for key, entry in market_entries(book_data):
        outcomes = entry.get("outcomes") if isinstance(entry, dict) else None
        mapping = {}

        if isinstance(outcomes, dict):
            if "players" in outcomes:
                if key is not None:
                    mapping[str(key)] = outcomes
            else:
                for outcome_id, outcome in outcomes.items():
                    mapping[str(outcome_id)] = outcome
        elif isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                outcome_id = outcome.get("outcomeId", outcome.get("marketId", outcome.get("id")))
                mapping[str(outcome_id if outcome_id is not None else key)] = outcome
        elif key is not None:
            mapping[str(key)] = entry

        if mapping:
            groups.append((str(key) if key is not None else None, mapping))

    return groups


def market_id_sort_key(market_id):
    """เรียง market id แบบตัวเลขก่อน (ตัวเลขมาก่อนตัวอักษร) — id ฝั่งเหย้าน้อยกว่าฝั่งเยือนเสมอ"""
    text = str(market_id)
    return (0, int(text), "") if text.lstrip("-").isdigit() else (1, 0, text)


def find_main_line(book_data, catalog, stamps=None):
    """
    สแกน AH market ทั้งหมดที่เจ้ามือเจ้านี้เสนอ แล้วหาเส้นที่ mainLine=true ทั้งสองฝั่ง

    เกณฑ์ครบทุกข้อถึงจะรับ:
      ก. กลุ่มนั้นมี market id ที่อยู่ในสารบัญ AH พอดีสองตัว (= ฝั่งเหย้ากับฝั่งเยือนของเส้นเดียวกัน)
      ข. ทั้งสองฝั่ง mainLine=true (ฝั่งเดียวไม่พอ เดี๋ยวได้เส้นครึ่ง ๆ กลาง ๆ)
      ค. ทั้งสองฝั่งมีราคาจริงเป็นตัวเลข (ขาดฝั่งใดฝั่งหนึ่งเอาไปเทียบว่าใครต่อไม่ได้)

    เลขเส้นอ่านจากฝั่งเหย้า (market id น้อยกว่า) เสมอ ตามที่ยืนยันจากของจริง
    (1068 = AH -0.5 คู่กับ 1069, 1072 = AH 0 คู่กับ 1073)

    คืน dict ของเส้นหลัก หรือ None ถ้าไม่เจอ (ให้ปลายทาง fallback ไปเส้นตายตัว)
    """
    catalog = catalog or {}
    candidates = []

    for _, mapping in market_groups(book_data):
        ah_ids = [market_id for market_id in mapping if market_id in catalog]
        if len(ah_ids) != 2:
            continue

        home_id, away_id = sorted(ah_ids, key=market_id_sort_key)
        home_outcome, away_outcome = mapping[home_id], mapping[away_id]

        if not (main_line_flag(home_outcome) and main_line_flag(away_outcome)):
            continue

        home_price = outcome_price(unwrap_outcome(home_outcome))
        away_price = outcome_price(unwrap_outcome(away_outcome))
        numeric = [isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in (home_price, away_price)]
        if not all(numeric):
            logger.info("เจอเส้นหลัก (market %s/%s) แต่ราคาไม่ครบสองฝั่ง — ข้ามไปใช้เส้นสำรอง",
                        home_id, away_id)
            continue

        handicap = catalog[home_id]
        candidates.append({
            "home": home_price,
            "away": away_price,
            "handicap": handicap,
            "line": abs(handicap),
            "source": "mainline",
            "market_ids": {"home": home_id, "away": away_id},
            "outcomes": (home_outcome, away_outcome),
        })

    if not candidates:
        return None

    if len(candidates) > 1:
        found = ", ".join(item["market_ids"]["home"] for item in candidates)
        logger.warning("เจอเส้นหลักมากกว่าหนึ่งเส้น (market %s) — เลือกอันแรกตาม market id", found)

    chosen = min(candidates, key=lambda item: market_id_sort_key(item["market_ids"]["home"]))
    outcomes = chosen.pop("outcomes")

    if stamps is not None:
        for outcome in outcomes:
            stamp = outcome_changed_at(outcome) or outcome_changed_at(unwrap_outcome(outcome))
            if stamp:
                stamps.append(stamp)

    logger.info("เส้นหลักของเจ้านี้: handicap=%s (market %s/%s) ราคา %s / %s",
                chosen["handicap"], chosen["market_ids"]["home"], chosen["market_ids"]["away"],
                chosen["home"], chosen["away"])
    return chosen


# เส้นตายตัวที่ใช้เป็นตัวสำรองเมื่อหา mainLine ไม่เจอ — เรียงตามลำดับที่อยากได้ก่อน
# (คีย์ใน book ที่กลั่นแล้ว, เลข handicap ฝั่งเหย้า, market id ฝั่งเหย้า, market id ฝั่งเยือน)
FALLBACK_HANDICAP_MARKETS = (
    ("ah_-0.5", -0.5, MARKET_AH_M05_HOME, MARKET_AH_M05_AWAY),
    ("ah_0", 0.0, MARKET_AH_0_HOME, MARKET_AH_0_AWAY),
)


def fallback_handicap(book):
    """
    หา mainLine ไม่เจอ ก็ถอยกลับไปใช้เส้นตายตัวเดิม (AH -0.5 ก่อน ไม่มีค่อย AH 0)
    ต้องมีราคาครบสองฝั่งถึงจะใช้ได้ — คืน None ถ้าไม่มีเส้นไหนใช้ได้เลย

    ผลที่คืนหน้าตาเหมือนของ find_main_line ทุกอย่าง ต่างแค่ source = "fallback"
    ปลายทางจะได้รู้ว่านี่ "ไม่ใช่" เส้นหลักที่ตลาดใช้จริง ห้ามพูดว่าคู่นี้ตลาดตั้งต่อเท่านี้
    """
    for market, handicap, home_id, away_id in FALLBACK_HANDICAP_MARKETS:
        prices = book.get(market) or {}
        home, away = prices.get("home"), prices.get("away")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in (home, away)):
            continue

        return {
            "home": home,
            "away": away,
            "handicap": handicap,
            "line": abs(handicap),
            "source": "fallback",
            "market_ids": {"home": str(home_id), "away": str(away_id)},
        }

    return None


def distill_book(book_data, catalog=None):
    """
    กลั่นราคาของเจ้ามือหนึ่งเจ้าให้เหลือเฉพาะ market ที่ใช้วิเคราะห์

    ช่อง "handicap" คือเส้นแฮนดิแคปที่จะเอาไปพูดจริง — หาเส้นหลักที่ตลาดใช้ ณ ขณะนั้น
    จาก mainLine ก่อน หาไม่เจอค่อยถอยไปเส้นตายตัวเดิม (ดู find_main_line / fallback_handicap)
    ส่วน ah_-0.5 / ah_0 ยังเก็บไว้เหมือนเดิม เผื่อไว้ตรวจสอบและเป็นตัวสำรอง
    """
    index = build_market_index(book_data)  # ทำครั้งเดียวต่อเจ้า แล้วใช้ซ้ำทุก market
    stamps = []

    book = {
        "1x2": {
            "home": price_of(index, MARKET_1X2_HOME, stamps),
            "draw": price_of(index, MARKET_1X2_DRAW, stamps),
            "away": price_of(index, MARKET_1X2_AWAY, stamps),
        },
        "ah_-0.5": {
            "home": price_of(index, MARKET_AH_M05_HOME, stamps),
            "away": price_of(index, MARKET_AH_M05_AWAY, stamps),
        },
        "ah_0": {
            "home": price_of(index, MARKET_AH_0_HOME, stamps),
            "away": price_of(index, MARKET_AH_0_AWAY, stamps),
        },
        "ou_2.5": {
            "over": price_of(index, MARKET_OU25_OVER, stamps),
            "under": price_of(index, MARKET_OU25_UNDER, stamps),
        },
    }

    book["handicap"] = find_main_line(book_data, catalog, stamps) or fallback_handicap(book)

    book["changed_at"] = max(stamps) if stamps else None
    return book


def distill_odds(raw_odds, catalog=None):
    """
    กลั่น bookmakerOdds ดิบให้เหลือเฉพาะเจ้าใน PANEL_BOOKS และ market ที่ใช้จริง
    market ไหนเจ้านั้นไม่มี จะเป็น None ไม่ทำให้พัง

    catalog คือสารบัญ {market id: handicap} จาก fetch_market_catalog() ใช้หาเส้นหลัก
    ไม่ส่งมาก็ยังทำงานได้ แค่จะหา mainLine ไม่เจอแล้วถอยไปใช้เส้นตายตัวแทน
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

        book = distill_book(raw_odds[slug], catalog)
        books[slug] = book

        missing = [market for market in ("1x2", "ah_-0.5", "ah_0")
                   if all(value is None for value in book[market].values())]
        if missing:
            notes.append(f"{slug} ไม่มี market: {', '.join(missing)}")

        handicap = book.get("handicap")
        if handicap is None:
            notes.append(f"{slug} ไม่มีเส้นแฮนดิแคปที่ใช้ได้เลย")
        elif handicap.get("source") == "fallback":
            notes.append(f"{slug} หาเส้นหลัก (mainLine) ไม่เจอ — ใช้เส้นตายตัวแทน")

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
    # สารบัญ market แคชไว้ 7 วัน จึงแทบไม่ยิง API เพิ่มต่อการวิเคราะห์หนึ่งคู่
    catalog = fetch_market_catalog(api_key=api_key, counter=counter)
    distilled = distill_odds(raw, catalog)
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
    # 3 ครั้ง = /fixtures + /odds + /markets (อันหลังยิงแค่รอบแรก จากนั้นอ่านจากแคช 7 วัน)
    counter = RequestCounter(limit=3)

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
