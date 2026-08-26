"""
Phase 3C — วิเคราะห์คู่บอลด้วย Claude API ในบุคลิก "เฮียตี๋"

ขั้นตอน: เช็คแคชก่อน -> ถ้าไม่มีค่อยดึงข้อมูลด้วย match_data.collect_match_data()
-> ส่งเข้า Claude -> แสดงบทวิเคราะห์ -> เก็บลงแคช (cache_db.py)

คู่ที่วิเคราะห์แล้วจะไม่ถูกวิเคราะห์ซ้ำ ประหยัดทั้งโควตา API-SPORTS และค่า Claude

วิธีใช้:
    pip install -r requirements.txt
    python3 src/analyze.py 1557375
    python3 src/analyze.py 1557375 --fresh   # บังคับวิเคราะห์ใหม่ ข้ามแคช (ใช้ตอนจูน prompt)

ต้องมีใน .env:
    API_FOOTBALL_KEY=...     สำหรับดึงข้อมูลบอล (API-SPORTS)
    ANTHROPIC_API_KEY=...    สำหรับเรียก Claude API
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import cache_db
from api_football import fail, get_api_key
from match_data import CountingClient, collect_match_data
from odds_data import fetch_oddspapi_fixtures, get_match_odds

# โมเดลที่ใช้วิเคราะห์ — แก้ตรงนี้จุดเดียวถ้าจะเปลี่ยนรุ่น
MODEL = "claude-sonnet-4-6"
# บทวิเคราะห์สั้น 5-6 บรรทัด แต่ภาษาไทยกิน token มาก จึงเผื่อเพดานไว้กันข้อความถูกตัดกลางคัน
MAX_TOKENS = 1024

TRUNCATED_NOTE = "(หมายเหตุ: บทวิเคราะห์อาจถูกตัด เพราะยาวเกินเพดาน)"

# path อ้างอิงจากตำแหน่งไฟล์ .py แบบเดียวกับ leagues.json — รันจากที่ไหนก็เจอ
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyst_prompt.txt"

# แคชราคาต่อรองอายุสั้นกว่าบทวิเคราะห์มาก เพราะราคาขยับทั้งวัน (บทวิเคราะห์เก็บยาวได้)
ODDS_CACHE_TTL = 20 * 60  # 20 นาที

# ขอรายการคู่จาก OddsPapi เป็นช่วง ไม่ใช่วันเดียว
# เหตุผล: ยิงด้วย from == to (วันเดียวเป๊ะ) ได้ผลลัพธ์แทบว่าง (เจอจริงบน VPS: 1 คู่)
# ส่วนช่วงหลายวันได้ครบ (~700 คู่ / 3 วัน) จึงขอวันก่อนหน้าถึงวันถัดไปของวันเตะเสมอ
ODDS_FIXTURE_WINDOW_DAYS = 1  # กว้างออกไปข้างละกี่วันจากวันเตะ

# ฟิลด์ของ fixture ฝั่ง OddsPapi ที่ต้องใช้จับคู่ — ตัดที่เหลือทิ้งก่อนเก็บลงแคช
ODDSPAPI_FIXTURE_FIELDS = ("fixtureId", "participant1Name", "participant2Name",
                           "startTime", "tournamentName", "categoryName", "hasOdds")

# เจ้าที่ยกมาให้ AI ดูเป็นหลัก (เจ้าคมราคา) — ไม่ต้องยัดราคาทุกเจ้าเข้า prompt
SHARP_BOOK_FOR_PROMPT = "pinnacle"

# ---- การกรองคู่บอลตามความนิยม ----
# ใช้จำนวนเจ้ามือที่ให้ราคา (total_books) เป็นตัวชี้วัด: คู่ดังเจ้ามือรับเยอะ คู่ไม่มีคนสนใจรับน้อย
# หมายเหตุ: ค่า 20 นี้ตั้งจากการเดา ยังไม่ได้ปรับจากข้อมูลจริง
# ดูค่าจริงของแต่ละคู่ได้จาก log "ความนิยม" แล้วค่อยปรับตัวเลขนี้ทีหลัง
MIN_POPULARITY_BOOKS = 20

# เพดานจำนวนคู่ที่ยอมยิง /odds ต่อการกดเลือกลีกหนึ่งครั้ง (กันโควตาพุ่งถ้าลีกมีคู่เยอะ)
# คู่ที่เกินเพดานจะไม่ถูกเช็ค และ "ไม่ถูกซ่อน" — ยอมให้คู่ที่ยังไม่รู้ความนิยมโผล่ดีกว่าซ่อนคู่ดังทิ้ง
MAX_ODDS_CHECKS_PER_LEAGUE = 20

USER_INSTRUCTION = (
    "นี่คือข้อมูลของคู่บอลที่จะเตะ วิเคราะห์คู่นี้ตามสไตล์ของเฮียตี๋ "
    "แล้วฟันธงว่าทีมไหนได้เปรียบ พร้อมเหตุผลจากข้อมูลจริง\n\n"
    "ข้อมูลคู่บอล (JSON):\n"
)


def parse_args(argv):
    """รับ fixture_id และ flag --fresh (บังคับวิเคราะห์ใหม่ ข้ามแคช)"""
    args = list(argv)

    if any(arg in ("-h", "--help") for arg in args):
        print(__doc__.strip())
        sys.exit(0)

    fresh = "--fresh" in args
    args = [arg for arg in args if arg != "--fresh"]

    if len(args) != 1:
        fail(
            "[ERROR] ต้องระบุ fixture_id หนึ่งค่า",
            "ตัวอย่าง: python3 src/analyze.py 1557375",
            "เพิ่ม --fresh ถ้าต้องการวิเคราะห์ใหม่โดยไม่ใช้แคช",
        )

    try:
        return int(args[0]), fresh
    except ValueError:
        fail(f"[ERROR] fixture_id ต้องเป็นตัวเลข แต่ได้รับ: {args[0]!r}")


def print_analysis(analysis, footer):
    """แสดงบทวิเคราะห์พร้อมบรรทัดบอกที่มา"""
    print()
    print("=" * 70)
    print(analysis)
    print("=" * 70)
    print(footer)


def load_system_prompt(prompt_path=PROMPT_PATH):
    """อ่าน system prompt จากไฟล์ — ไม่พบไฟล์หรือไฟล์ว่างให้หยุดพร้อมบอกสาเหตุ"""
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        fail(
            f"[ERROR] ไม่พบไฟล์ prompt: {prompt_path}",
            "วิธีแก้: ตรวจว่าไฟล์ prompts/analyst_prompt.txt อยู่ครบหลัง git pull",
        )
    except OSError as exc:
        fail(f"[ERROR] อ่านไฟล์ prompt ไม่ได้: {prompt_path}", f"รายละเอียด: {exc}")

    if not prompt:
        fail(f"[ERROR] ไฟล์ prompt ว่างเปล่า: {prompt_path}")

    return prompt


def get_anthropic_key():
    """อ่าน ANTHROPIC_API_KEY จาก .env — ไม่มีให้หยุดทันที (ห้าม hardcode key)"""
    load_dotenv()
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

    if not api_key:
        fail(
            "[ERROR] ไม่พบ ANTHROPIC_API_KEY: ยังไม่ได้ตั้งค่าใน .env",
            "วิธีแก้: เพิ่มบรรทัด ANTHROPIC_API_KEY=คีย์จริงของคุณ ลงในไฟล์ .env",
            "ขอคีย์ได้ที่ https://console.anthropic.com/settings/keys",
        )

    return api_key


def analyze_match(match_summary, api_key, system_prompt, model=MODEL, odds_summary=None):
    """
    ส่งข้อมูลคู่บอลเข้า Claude แล้วคืน (บทวิเคราะห์, ถูกตัดเพราะชนเพดานหรือไม่)
    odds_summary ใส่เพิ่มได้เมื่อมีราคาจริง — ไม่มีก็ไม่ต้องส่งอะไรเข้า prompt เลย
    """
    client = anthropic.Anthropic(api_key=api_key)
    match_json = json.dumps(match_summary, ensure_ascii=False, indent=2)

    user_content = USER_INSTRUCTION + match_json
    if odds_summary:
        user_content += ("\n\nราคาต่อรองจากตลาด (ข้อมูลประกอบเท่านั้น ห้ามชวนแทงหรือพูดถึงราคาในเชิงแนะนำ):\n"
                         + json.dumps(odds_summary, ensure_ascii=False, indent=2))

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        fail(
            "[ERROR] Claude API ปฏิเสธคีย์ (401): ANTHROPIC_API_KEY ผิดหรือถูกยกเลิกแล้ว",
            "วิธีแก้: สร้างคีย์ใหม่ที่ https://console.anthropic.com/settings/keys แล้วอัปเดต .env",
        )
    except anthropic.PermissionDeniedError:
        fail(
            "[ERROR] คีย์นี้ไม่มีสิทธิ์เรียกใช้งาน (403)",
            "วิธีแก้: ตรวจสิทธิ์ของคีย์/workspace ใน Anthropic Console",
        )
    except anthropic.RateLimitError as exc:
        retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
        fail(
            "[ERROR] เรียก Claude API ถี่เกินไปหรือเครดิตหมด (429)",
            f"ลองใหม่อีกครั้งใน {retry_after} วินาที" if retry_after else "รอสักครู่แล้วลองใหม่",
            "ถ้าเครดิตหมด เติมได้ที่ https://console.anthropic.com/settings/billing",
        )
    except anthropic.NotFoundError:
        fail(
            f"[ERROR] ไม่พบโมเดล {model} (404)",
            "วิธีแก้: ตรวจค่าคงที่ MODEL ใน src/analyze.py ว่าสะกดถูกและบัญชีเข้าถึงโมเดลนี้ได้",
        )
    except anthropic.BadRequestError as exc:
        fail("[ERROR] คำขอไม่ถูกต้อง (400)", f"รายละเอียด: {exc}")
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            fail(f"[ERROR] ฝั่ง Claude API มีปัญหา ({exc.status_code}) — ลองใหม่อีกครั้งภายหลัง")
        fail(f"[ERROR] Claude API ตอบกลับผิดพลาด ({exc.status_code})", f"รายละเอียด: {exc}")
    except anthropic.APITimeoutError:
        fail("[ERROR] เรียก Claude API แล้วหมดเวลารอ — ลองใหม่อีกครั้ง")
    except anthropic.APIConnectionError as exc:
        fail("[ERROR] เชื่อมต่อ Claude API ไม่ได้: ตรวจอินเทอร์เน็ตของเครื่อง", f"รายละเอียด: {exc}")

    if response.stop_reason == "refusal":
        fail("[ERROR] Claude ปฏิเสธที่จะตอบคำขอนี้ (stop_reason=refusal)")

    text = "\n".join(block.text for block in response.content if block.type == "text").strip()

    if not text:
        fail("[ERROR] Claude ตอบกลับมาแต่ไม่มีข้อความ — ลองใหม่อีกครั้ง")

    return text, response.stop_reason == "max_tokens"


def trim_oddspapi_fixture(fixture):
    """เก็บเฉพาะฟิลด์ที่ใช้จับคู่ ก่อนเอาลงแคช (ผลดิบมีหลายร้อยคู่ ไม่ต้องเก็บทั้งก้อน)"""
    return {field: fixture.get(field) for field in ODDSPAPI_FIXTURE_FIELDS}


def fixture_window(date_str, days=ODDS_FIXTURE_WINDOW_DAYS):
    """
    ขยายวันเตะเป็นช่วงวัน (ก่อนหน้า .. ถัดไป) สำหรับส่งเป็น from/to ให้ OddsPapi

    ต้องกว้างกว่าหนึ่งวันเสมอ เพราะ from == to ได้ผลลัพธ์แทบว่างเปล่า
    ถ้าแปลงวันที่ไม่ได้ ก็คืนค่าเดิมไปให้ปลายทางจัดการต่อ
    """
    try:
        day = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return [date_str]

    return [(day - timedelta(days=days)).isoformat(), (day + timedelta(days=days)).isoformat()]


def load_oddspapi_fixtures(date_str, log):
    """
    ดึงรายการคู่ของ OddsPapi รอบ ๆ วันที่แข่ง โดยใช้แคชร่วมกันข้ามการวิเคราะห์หลายคู่
    cache key ยังผูกกับวันเตะเหมือนเดิม แค่ query ให้กว้างขึ้น (ดู fixture_window)
    """
    cache_key = f"oddspapi_fixtures:{date_str}"

    cached = cache_db.get_odds(cache_key, ODDS_CACHE_TTL)
    if cached is not None:
        log(f"ใช้รายการคู่ของ OddsPapi จากแคช ({len(cached['payload'])} คู่)")
        return cached["payload"]

    window = fixture_window(date_str)
    fixtures = [trim_oddspapi_fixture(f) for f in fetch_oddspapi_fixtures(window)]
    cache_db.save_odds(cache_key, fixtures)
    log(f"ดึงรายการคู่ของ OddsPapi ใหม่ ({len(fixtures)} คู่ ช่วง {window[0]} ถึง {window[-1]})"
        " แล้วเก็บลงแคช")
    return fixtures


def fetch_odds_context(match_summary, log=lambda message: None):
    """
    หาราคาต่อรองของคู่นี้มาเป็นข้อมูลประกอบ — คืน dict หรือ None

    fail-open เสมอ: จับคู่ไม่ได้ / OddsPapi ล่ม / โควตาหมด / timeout ให้คืน None เงียบ ๆ
    บทวิเคราะห์ต้องออกได้ตามปกติเหมือนไม่มีราคาเลย เพราะ odds เป็นของเสริมไม่ใช่ของหลัก
    (odds_data ใช้ fail() ที่เรียก sys.exit จึงต้องดัก SystemExit ด้วย ไม่ใช่แค่ Exception)
    """
    match = match_summary.get("match") or {}
    home = (match_summary.get("home") or {}).get("name")
    away = (match_summary.get("away") or {}).get("name")
    kickoff = match.get("kickoff")
    fixture_id = match_summary.get("fixture_id")

    if not home or not away:
        return None

    date_str = (kickoff or "")[:10]
    if not date_str:
        log("ไม่มีเวลาเตะในข้อมูล จึงข้ามการดึงราคา")
        return None

    cache_key = f"odds:{fixture_id}:{date_str}"

    try:
        cache_db.init_db()

        cached = cache_db.get_odds(cache_key, ODDS_CACHE_TTL)
        if cached is not None:
            payload = cached["payload"]
            if not payload.get("found"):
                log("แคชบอกว่าคู่นี้จับกับ OddsPapi ไม่ได้ — ข้ามราคาไป (ไม่ยิง API ซ้ำ)")
                return None
            log(f"ใช้ราคาจากแคช (ดึงเมื่อ {cached['created_at']})")
            return payload.get("odds")

        fixtures = load_oddspapi_fixtures(date_str, log)
        odds = get_match_odds(home, away, kickoff, match.get("league"), fixtures)

        # เก็บผลลัพธ์ลงแคชทั้งกรณีเจอและไม่เจอ กรณีไม่เจอจะได้ไม่ยิงซ้ำจนหมดโควตา
        cache_db.save_odds(cache_key, {"found": odds is not None, "odds": odds})

        if odds is None:
            log("จับคู่กับ OddsPapi ไม่ได้ — วิเคราะห์ต่อโดยไม่มีราคา")
        else:
            log(f"ได้ราคาจาก {len(odds.get('books') or {})} เจ้า")

        return odds

    except SystemExit as exc:
        log(f"[เตือน] ดึงราคาไม่สำเร็จ (exit code={exc.code}) — วิเคราะห์ต่อโดยไม่มีราคา")
        return None
    except Exception as exc:
        log(f"[เตือน] ดึงราคาไม่สำเร็จ ({exc}) — วิเคราะห์ต่อโดยไม่มีราคา")
        return None


def summarize_odds_for_prompt(odds):
    """
    ย่อราคาที่กลั่นแล้วให้เหลือเท่าที่ AI ต้องใช้ — ไม่ยัดราคาทุกเจ้าเข้า prompt
    เลือกเจ้าคมราคา (pinnacle) ถ้ามี ไม่มีก็ใช้เจ้าแรกที่มีข้อมูล แล้วบอกด้วยว่าใครเป็นต่อในสายตาตลาด
    """
    books = (odds or {}).get("books") or {}
    if not books:
        return None

    def has_price(book):
        return any(value is not None
                   for market, prices in book.items() if isinstance(prices, dict)
                   for value in prices.values())

    slug = next((s for s in books if SHARP_BOOK_FOR_PROMPT in s.lower() and has_price(books[s])), None)
    if slug is None:
        slug = next((s for s in books if has_price(books[s])), None)
    if slug is None:
        return None

    book = books[slug]
    one_x_two = book.get("1x2") or {}

    # ราคาต่ำสุดของ 1X2 = ฝั่งที่ตลาดมองว่าได้เปรียบที่สุด
    priced = {side: value for side, value in one_x_two.items() if isinstance(value, (int, float))}
    favourite = min(priced, key=priced.get) if priced else None

    return {
        "source": "OddsPapi",
        "bookmaker": slug,
        "1x2": one_x_two,
        "ah_-0.5": book.get("ah_-0.5"),
        "ah_0": book.get("ah_0"),
        "market_favourite": favourite,
        "total_books": odds.get("total_books"),
        "updated_at": book.get("changed_at"),
    }


def match_popularity(fixture_id, home, away, kickoff, league_hint, fixtures, log):
    """
    ความนิยมของคู่นี้ = จำนวนเจ้ามือที่ให้ราคา (total_books)
    จับคู่กับ OddsPapi ไม่ได้ / มีปัญหาใด ๆ ให้ถือว่า 0 (ไม่นิยม) ไม่ throw
    ผลถูกแคชต่อคู่ + วันที่ TTL เดียวกับราคา
    """
    date_str = (kickoff or "")[:10]
    cache_key = f"popularity:{fixture_id}:{date_str}"

    cached = cache_db.get_odds(cache_key, ODDS_CACHE_TTL)
    if cached is not None:
        return cached["payload"].get("total_books", 0), True

    try:
        odds = get_match_odds(home, away, kickoff, league_hint, fixtures)
    except SystemExit as exc:
        log(f"[เตือน] เช็คความนิยมของ {home} vs {away} ไม่สำเร็จ (exit code={exc.code}) — นับเป็น 0")
        return 0, False
    except Exception as exc:
        log(f"[เตือน] เช็คความนิยมของ {home} vs {away} ไม่สำเร็จ ({exc}) — นับเป็น 0")
        return 0, False

    total = (odds or {}).get("total_books") or 0
    cache_db.save_odds(cache_key, {"total_books": total})
    return total, False


def filter_popular_matches(matches, date_str, league_hint=None, log=lambda message: None,
                           min_books=MIN_POPULARITY_BOOKS, max_checks=MAX_ODDS_CHECKS_PER_LEAGUE):
    """
    คัดเฉพาะคู่ที่ "มีคนสนใจ" ออกมาแสดง โดยดูจากจำนวนเจ้ามือที่ให้ราคา

    matches เป็น list ของ (เวลาเตะ, ทีมเหย้า, ทีมเยือน, fixture_id) ตามที่ fetch_fixtures จัดมา
    คืน (คู่ที่เหลือ, สถิติ) — สถิติมี total/kept/hidden/unchecked/checked ไว้ log และเทสต์

    fail-open: ถ้าระบบราคาล่มทั้งยวง (ดึงรายการคู่ไม่ได้) จะคืนคู่ทั้งหมดตามเดิม
    ยอมโชว์เกินดีกว่าโชว์หน้าว่างเพราะของเสริมพัง
    """
    matches = list(matches or [])
    stats = {"total": len(matches), "kept": 0, "hidden": 0, "unchecked": 0,
             "checked": 0, "from_cache": 0, "fallback": False}

    if not matches:
        return [], stats

    try:
        cache_db.init_db()
        fixtures = load_oddspapi_fixtures(date_str, log)
    except SystemExit as exc:
        log(f"[เตือน] ดึงรายการคู่ของ OddsPapi ไม่ได้ (exit code={exc.code}) — แสดงทุกคู่ตามเดิม")
        stats.update(fallback=True, kept=len(matches))
        return matches, stats
    except Exception as exc:
        log(f"[เตือน] ดึงรายการคู่ของ OddsPapi ไม่ได้ ({exc}) — แสดงทุกคู่ตามเดิม")
        stats.update(fallback=True, kept=len(matches))
        return matches, stats

    kept = []
    for index, match in enumerate(matches):
        kickoff, home, away, fixture_id = match

        if index >= max_checks:
            stats["unchecked"] += 1
            kept.append(match)
            continue

        popularity, from_cache = match_popularity(
            fixture_id, home, away, f"{date_str}T{kickoff}:00+07:00", league_hint, fixtures, log)
        stats["checked"] += 1
        stats["from_cache"] += 1 if from_cache else 0

        if popularity >= min_books:
            kept.append(match)
            stats["kept"] += 1
        else:
            stats["hidden"] += 1

        log(f"ความนิยม: {home} vs {away} = {popularity} เจ้า"
            f" -> {'แสดง' if popularity >= min_books else 'ซ่อน'}"
            f"{' (จากแคช)' if from_cache else ''}")

    if stats["unchecked"]:
        log(f"[เตือน] ลีกนี้มี {stats['total']} คู่ เกินเพดาน {max_checks} คู่ต่อครั้ง"
            f" — {stats['unchecked']} คู่ท้ายยังไม่ได้เช็คความนิยม (แสดงไว้ก่อน)")

    log(f"สรุปการกรอง: แสดง {len(kept)} คู่ จากทั้งหมด {stats['total']} คู่"
        f" (ซ่อน {stats['hidden']}, ยังไม่เช็ค {stats['unchecked']},"
        f" ยิง OddsPapi ใหม่ {stats['checked'] - stats['from_cache']} ครั้ง)")

    return kept, stats


def analyze_fixture(fixture_id, fresh=False, log=lambda message: None):
    """
    หัวใจของการวิเคราะห์หนึ่งคู่ ใช้ร่วมกันทั้ง CLI และบอท Telegram (ไม่ print เอง)

    เช็คแคชก่อน ถ้าไม่เจอค่อยดึงข้อมูล + เรียก Claude + เก็บลงแคช
    คืน dict: analysis, match_name, from_cache, created_at, model, sports_requests, truncated
    ส่ง callable เข้าทาง log ได้ถ้าอยากเห็นความคืบหน้า (CLI ส่ง print, บอทส่ง logger.info)
    """
    cache_db.init_db()

    if fresh:
        log("โหมด --fresh: ข้ามแคช วิเคราะห์ใหม่แล้วเขียนทับของเดิม")
    else:
        cached = cache_db.get_analysis(fixture_id)
        if cached:
            log(f"เจอในแคช: {cached['match_name']}")
            return {
                "analysis": cached["analysis_text"],
                "match_name": cached["match_name"],
                "from_cache": True,
                "created_at": cached["created_at"],
                "model": cached["model_used"],
                "sports_requests": 0,
                "truncated": False,
            }

    system_prompt = load_system_prompt()
    anthropic_key = get_anthropic_key()

    client = CountingClient(get_api_key())
    log(f"กำลังดึงข้อมูลเชิงลึกของ fixture_id={fixture_id} ...")
    match_summary = collect_match_data(client, fixture_id)
    log(f"ดึงข้อมูลครบ (ยิง API-SPORTS ไป {client.request_count} ครั้ง)")

    match_name = (match_summary.get("match") or {}).get("name") or f"fixture {fixture_id}"

    # ราคาต่อรองเป็นของเสริม — ล้มเหลวเมื่อไรก็วิเคราะห์ต่อโดยไม่มีมัน
    odds_summary = summarize_odds_for_prompt(fetch_odds_context(match_summary, log))

    log(f"กำลังให้เฮียตี๋วิเคราะห์ {match_name} ด้วย {MODEL} ...")
    analysis, truncated = analyze_match(match_summary, anthropic_key, system_prompt,
                                        odds_summary=odds_summary)

    created_at = cache_db.save_analysis(fixture_id, match_name, analysis, MODEL)

    return {
        "analysis": analysis,
        "match_name": match_name,
        "from_cache": False,
        "created_at": created_at,
        "model": MODEL,
        "sports_requests": client.request_count,
        "truncated": truncated,
    }


def main():
    fixture_id, fresh = parse_args(sys.argv[1:])
    result = analyze_fixture(fixture_id, fresh=fresh, log=print)

    if result["from_cache"]:
        footer = (
            f"(จากแคช — วิเคราะห์เมื่อ {result['created_at']} ด้วย {result['model']}) "
            f"| ยิง API 0 ครั้ง"
        )
    else:
        footer = (
            f"(วิเคราะห์ใหม่ + เก็บลงแคชแล้ว เมื่อ {result['created_at']}) "
            f"| ยิง API-SPORTS {result['sports_requests']} ครั้ง + Claude 1 ครั้ง"
        )
        if result["truncated"]:
            # ชน max_tokens แปลว่าข้อความถูกตัดกลางคัน ต้องรู้ทันที ไม่ปล่อยผ่านเงียบ ๆ
            footer += f"\n{TRUNCATED_NOTE}"

    print_analysis(result["analysis"], footer)


if __name__ == "__main__":
    main()
