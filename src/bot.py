"""
Phase 4A-1 — บอท Telegram ของเฮียตี๋ (long polling) เมนูปุ่มกดเลือกคู่บอลแล้ววิเคราะห์

flow: /start -> เลือกวัน -> เลือกลีก -> เลือกคู่ -> เฮียตี๋วิเคราะห์
ตอบเฉพาะปุ่มเท่านั้น ข้อความอิสระของผู้ใช้จะไม่ถูกส่งเข้า AI เด็ดขาด

วิธีใช้:
    pip install -r requirements.txt
    # ใส่ TELEGRAM_BOT_TOKEN ลงใน .env (ขอจาก @BotFather)
    python3 src/bot.py
"""

import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from analyze import analyze_fixture
from api_football import fail, get_api_key
from fetch_fixtures import (
    date_range,
    day_label,
    fetch_fixtures,
    get_bangkok_tz,
    group_by_day_and_league,
    load_leagues,
)

# ---------- ค่าคงที่ที่แก้บ่อย ----------

# อายุของแคช "รายการคู่บอล" ในหน่วยความจำ (วินาที)
# ผู้ใช้กดเข้า-ออกเมนูบ่อย ถ้ายิง API ทุกครั้งจะเปลืองโควตามาก
FIXTURES_CACHE_TTL = 30 * 60  # 30 นาที

MATCHES_PER_PAGE = 40  # กันกรณีลีกเดียวมีคู่เยอะจนปุ่มล้น (Telegram จำกัดขนาด keyboard)

GREETING = (
    "เฮียตี๋มาแล้วครับ ⚽\n"
    "เลือกวันที่อยากดู แล้วเฮียจะจัดบทวิเคราะห์ให้เป็นคู่ ๆ เลย"
)
PICK_DAY = "เลือกวันที่ต้องการดูโปรแกรมครับ 👇"
PICK_LEAGUE = "เลือกลีกครับ 👇"
PICK_MATCH = "เลือกคู่ที่อยากให้เฮียตี๋วิเคราะห์ครับ 👇"
ANALYZING = "🔍 เฮียตี๋กำลังดูเกมนี้ให้ รอแป๊บ..."
ERROR_MESSAGE = "ขออภัยครับ เฮียตี๋ดูเกมนี้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง 🙏"
ONLY_BUTTONS = "กดเมนูด้านล่างเพื่อดูบทวิเคราะห์จากเฮียตี๋ได้เลยครับ 👇"
NO_FIXTURES = "ช่วงนี้ยังไม่มีคู่บอลของลีกที่เฮียตี๋ตามอยู่ครับ ลองใหม่อีกทีภายหลัง 🙏"
STALE_MENU = "เมนูนี้เก่าไปแล้วครับ กด /start เพื่อเริ่มใหม่"

BACK_TO_DAYS = "⬅️ ย้อนกลับ"
BACK_TO_LEAGUES = "⬅️ ย้อนกลับ"
BACK_TO_MATCHES = "⬅️ เลือกคู่อื่น"

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("heretee.bot")


def get_bot_token():
    """อ่าน TELEGRAM_BOT_TOKEN จาก .env — ไม่มีให้หยุดทันที (ห้าม hardcode)"""
    load_dotenv()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    if not token:
        fail(
            "[ERROR] ไม่พบ TELEGRAM_BOT_TOKEN: ยังไม่ได้ตั้งค่าใน .env",
            "วิธีแก้: เพิ่มบรรทัด TELEGRAM_BOT_TOKEN=โทเคนจริงของบอท ลงในไฟล์ .env",
            "ขอโทเคนได้จาก @BotFather ในแอป Telegram",
        )

    return token


# ---------- แคชรายการคู่บอลในหน่วยความจำ ----------


class FixturesCache:
    """
    เก็บรายการคู่บอลไว้ในหน่วยความจำ (คนละชั้นกับแคชบทวิเคราะห์ใน cache.db)
    รีเฟรชเมื่อเกิน ttl เท่านั้น ทุกการกดปุ่มเมนูจึงไม่ยิง API ใหม่
    """

    def __init__(self, ttl=FIXTURES_CACHE_TTL, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._snapshot = None
        self._fetched_at = None

    def is_stale(self):
        return self._snapshot is None or (self.clock() - self._fetched_at) >= self.ttl

    def get(self, force=False):
        """คืน snapshot ล่าสุด ดึงใหม่เฉพาะตอนหมดอายุ (บล็อก — เรียกผ่าน to_thread ในบอท)"""
        if force or self.is_stale():
            logger.info("รายการคู่บอลหมดอายุ กำลังดึงใหม่จาก API-SPORTS")
            self._snapshot = build_snapshot()
            self._fetched_at = self.clock()
            logger.info(
                "ดึงรายการคู่บอลแล้ว: %d วันที่มีคู่",
                len(self._snapshot["days"]),
            )
        return self._snapshot


def build_snapshot():
    """ดึงโปรแกรมบอลตาม logic เดิมของ fetch_fixtures แล้วจัดเป็น วัน -> ลีก -> คู่"""
    leagues = load_leagues()
    tz = get_bangkok_tz()
    dates = date_range(tz)
    fixtures = fetch_fixtures(get_api_key(), dates)
    return {
        "dates": dates,
        "days": group_by_day_and_league(fixtures, leagues, tz),
    }


def find_day(snapshot, date_str):
    """คืน list ของ (ข้อมูลลีก, คู่บอล) ของวันนั้น — ไม่มีคืน None"""
    for day_date, league_groups in snapshot["days"]:
        if day_date == date_str:
            return league_groups
    return None


def find_league(snapshot, date_str, league_id):
    """คืน (ข้อมูลลีก, คู่บอล) ของลีกนั้นในวันนั้น — ไม่มีคืน (None, None)"""
    for league, matches in find_day(snapshot, date_str) or []:
        if league.get("id") == league_id:
            return league, matches
    return None, None


# ---------- ปุ่ม / callback data ----------


def day_keyboard(snapshot):
    """ปุ่มเลือกวัน เฉพาะวันที่มีคู่จริง"""
    buttons = []
    for date_str, league_groups in snapshot["days"]:
        count = sum(len(matches) for _, matches in league_groups)
        buttons.append([InlineKeyboardButton(
            f"{day_label(date_str, snapshot['dates'])} · {count} คู่",
            callback_data=f"day:{date_str}",
        )])
    return InlineKeyboardMarkup(buttons)


def league_keyboard(snapshot, date_str):
    """ปุ่มเลือกลีกของวันนั้น เรียงตาม priority (group_by_day_and_league เรียงมาให้แล้ว)"""
    buttons = []
    for league, matches in find_day(snapshot, date_str) or []:
        buttons.append([InlineKeyboardButton(
            f"{league['name_th']} · {len(matches)} คู่",
            callback_data=f"league:{league['id']}:{date_str}",
        )])
    buttons.append([InlineKeyboardButton(BACK_TO_DAYS, callback_data="back:days")])
    return InlineKeyboardMarkup(buttons)


def match_keyboard(snapshot, date_str, league_id):
    """ปุ่มเลือกคู่บอลในลีกนั้น"""
    _, matches = find_league(snapshot, date_str, league_id)
    buttons = []

    for kickoff, home, away, fixture_id in (matches or [])[:MATCHES_PER_PAGE]:
        if fixture_id is None:
            continue  # ไม่มี id ก็วิเคราะห์ต่อไม่ได้ ไม่ต้องโชว์ปุ่ม
        buttons.append([InlineKeyboardButton(
            f"{kickoff}  {home} vs {away}",
            callback_data=f"match:{fixture_id}:{date_str}:{league_id}",
        )])

    buttons.append([InlineKeyboardButton(BACK_TO_LEAGUES, callback_data=f"back:leagues:{date_str}")])
    return InlineKeyboardMarkup(buttons)


def after_analysis_keyboard(date_str, league_id):
    """ปุ่มหลังอ่านบทวิเคราะห์จบ — กลับไปเลือกคู่อื่นในลีกเดิม"""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        BACK_TO_MATCHES,
        callback_data=f"back:matches:{date_str}:{league_id}",
    )]])


async def get_snapshot(context):
    """ดึง snapshot แบบไม่บล็อก event loop และกันการดึงซ้อนกันหลายคำขอ"""
    cache = context.application.bot_data["fixtures_cache"]
    lock = context.application.bot_data["fixtures_lock"]
    async with lock:
        return await asyncio.to_thread(cache.get)


async def show_days(target_message, context, header=PICK_DAY):
    """แสดงเมนูเลือกวัน (ใช้ทั้งตอน /start, ปุ่มย้อนกลับ และตอบข้อความอิสระ)"""
    snapshot = await get_snapshot(context)

    if not snapshot["days"]:
        await target_message.reply_text(NO_FIXTURES)
        return

    await target_message.reply_text(header, reply_markup=day_keyboard(snapshot))


async def edit_to_days(query, context):
    snapshot = await get_snapshot(context)
    if not snapshot["days"]:
        await safe_edit(query, NO_FIXTURES, None)
        return
    await safe_edit(query, PICK_DAY, day_keyboard(snapshot))


async def safe_edit(query, text, reply_markup):
    """แก้ข้อความเดิม — ถ้าเนื้อหาเหมือนเดิม Telegram จะโวยว่า not modified ให้ข้ามไป"""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# ---------- handlers ----------


async def start_handler(update, context):
    """/start — รองรับ deep link payload (/start <payload>) โดยยังไม่ทำ logic พิเศษ"""
    payload = context.args[0] if getattr(context, "args", None) else None
    user = update.effective_user
    logger.info("start จาก user_id=%s payload=%s", user.id if user else "?", payload)

    await update.message.reply_text(GREETING)
    await show_days(update.message, context)


async def day_handler(update, context):
    """กดปุ่มวัน -> โชว์ลีกของวันนั้น"""
    query = update.callback_query
    await query.answer()

    date_str = query.data.split(":", 1)[1]
    logger.info("เลือกวัน %s (user_id=%s)", date_str, query.from_user.id)

    snapshot = await get_snapshot(context)
    if find_day(snapshot, date_str) is None:
        await safe_edit(query, STALE_MENU, None)
        return

    await safe_edit(query, f"{day_label(date_str, snapshot['dates'])}\n{PICK_LEAGUE}",
                    league_keyboard(snapshot, date_str))


async def league_handler(update, context):
    """กดปุ่มลีก -> โชว์คู่บอลในลีกนั้น"""
    query = update.callback_query
    await query.answer()

    _, league_id_raw, date_str = query.data.split(":", 2)
    league_id = int(league_id_raw)
    logger.info("เลือกลีก %s วัน %s (user_id=%s)", league_id, date_str, query.from_user.id)

    snapshot = await get_snapshot(context)
    league, matches = find_league(snapshot, date_str, league_id)
    if league is None:
        await safe_edit(query, STALE_MENU, None)
        return

    await safe_edit(
        query,
        f"{league['name_th']} — {day_label(date_str, snapshot['dates'])}\n{PICK_MATCH}",
        match_keyboard(snapshot, date_str, league_id),
    )


async def match_handler(update, context):
    """กดปุ่มคู่บอล -> วิเคราะห์ (จุดเดียวที่เรียก AI)"""
    query = update.callback_query
    await query.answer()

    _, fixture_id_raw, date_str, league_id_raw = query.data.split(":", 3)
    fixture_id = int(fixture_id_raw)
    league_id = int(league_id_raw)
    logger.info("ขอวิเคราะห์ fixture_id=%s (user_id=%s)", fixture_id, query.from_user.id)

    await safe_edit(query, ANALYZING, None)

    try:
        # analyze_fixture เป็นงานบล็อก (network + sqlite) จึงโยนไปรันในเธรดแยก
        result = await asyncio.to_thread(analyze_fixture, fixture_id, False, logger.info)
    except SystemExit as exc:
        # โค้ดเดิมใช้ fail() ที่เรียก sys.exit — ในบอทถือเป็นข้อผิดพลาดธรรมดา ห้ามให้บอทตาย
        logger.error("วิเคราะห์ fixture_id=%s ไม่สำเร็จ (fail/exit code=%s)", fixture_id, exc.code)
        await safe_edit(query, ERROR_MESSAGE, after_analysis_keyboard(date_str, league_id))
        return
    except Exception:
        logger.exception("วิเคราะห์ fixture_id=%s ไม่สำเร็จ", fixture_id)
        await safe_edit(query, ERROR_MESSAGE, after_analysis_keyboard(date_str, league_id))
        return

    logger.info(
        "ส่งบทวิเคราะห์ %s (จากแคช=%s, ยิง API-SPORTS %d ครั้ง)",
        result["match_name"], result["from_cache"], result["sports_requests"],
    )
    await safe_edit(query, result["analysis"], after_analysis_keyboard(date_str, league_id))


async def back_handler(update, context):
    """ปุ่มย้อนกลับทุกระดับ: back:days / back:leagues:<date> / back:matches:<date>:<league_id>"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    target = parts[1]

    if target == "days":
        await edit_to_days(query, context)
        return

    snapshot = await get_snapshot(context)
    date_str = parts[2]

    if target == "leagues":
        if find_day(snapshot, date_str) is None:
            await safe_edit(query, STALE_MENU, None)
            return
        await safe_edit(query, f"{day_label(date_str, snapshot['dates'])}\n{PICK_LEAGUE}",
                        league_keyboard(snapshot, date_str))
        return

    if target == "matches":
        league_id = int(parts[3])
        league, _ = find_league(snapshot, date_str, league_id)
        if league is None:
            await safe_edit(query, STALE_MENU, None)
            return
        await safe_edit(
            query,
            f"{league['name_th']} — {day_label(date_str, snapshot['dates'])}\n{PICK_MATCH}",
            match_keyboard(snapshot, date_str, league_id),
        )


async def text_fallback_handler(update, context):
    """
    ข้อความอิสระ: ตอบด้วยเมนูเท่านั้น
    ห้ามส่งข้อความของผู้ใช้เข้า AI เด็ดขาด — AI ถูกเรียกจากปุ่ม match: เท่านั้น
    """
    user = update.effective_user
    logger.info("ข้อความอิสระจาก user_id=%s (ไม่ส่งเข้า AI)", user.id if user else "?")
    await show_days(update.message, context, header=ONLY_BUTTONS)


async def error_handler(update, context):
    """กันบอทล้มทั้งตัวเพราะคำขอเดียวพัง — log ไว้ฝั่ง server ผู้ใช้เห็นแค่ข้อความสุภาพ"""
    logger.exception("เกิดข้อผิดพลาดที่ไม่ได้ดักไว้", exc_info=context.error)

    message = getattr(update, "effective_message", None)
    if message is not None:
        try:
            await message.reply_text(ERROR_MESSAGE)
        except Exception:
            logger.exception("ส่งข้อความแจ้ง error กลับไปไม่สำเร็จ")


def build_application(token):
    """ประกอบ Application + handler ทั้งหมด (แยกออกมาเพื่อให้เทสต์เรียกดูได้)"""
    application = Application.builder().token(token).build()

    application.bot_data["fixtures_cache"] = FixturesCache()
    application.bot_data["fixtures_lock"] = asyncio.Lock()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CallbackQueryHandler(day_handler, pattern=r"^day:"))
    application.add_handler(CallbackQueryHandler(league_handler, pattern=r"^league:"))
    application.add_handler(CallbackQueryHandler(match_handler, pattern=r"^match:"))
    application.add_handler(CallbackQueryHandler(back_handler, pattern=r"^back:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback_handler))
    application.add_error_handler(error_handler)

    return application


def main():
    token = get_bot_token()
    logger.info("เริ่มบอทเฮียตี๋ (long polling)")
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
