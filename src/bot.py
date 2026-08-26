"""
Phase 4A-1 — บอท Telegram ของเฮียตี๋ (long polling) เมนูปุ่มกดเลือกคู่บอลแล้ววิเคราะห์

flow: /start -> เลือกวัน -> เลือกลีก -> เลือกคู่ -> เฮียตี๋วิเคราะห์
ตอบเฉพาะปุ่มเท่านั้น ข้อความอิสระของผู้ใช้จะไม่ถูกส่งเข้า AI เด็ดขาด
ใช้ได้เฉพาะสมาชิกกลุ่ม (ถ้าตั้ง GROUP_CHAT_ID ไว้) และมี /postgroup ให้แอดมินโพสต์ปุ่มลงกลุ่ม

บอทโต้ตอบเฉพาะ "แชทส่วนตัว" เท่านั้น ในกลุ่มจะเงียบสนิท (กลุ่มเป็นแค่หน้าร้านที่มีปุ่มปักหมุด)
แนะนำให้ตั้ง Privacy Mode ของบอทเป็น ENABLED ด้วย: คุยกับ @BotFather -> /setprivacy -> Enable
บอทจะได้ไม่เห็นข้อความทั่วไปในกลุ่มตั้งแต่แรก ลดภาระและปลอดภัยกว่า
แต่โค้ดกันเองอยู่แล้ว (ดู private_only) ไม่ได้พึ่งการตั้งค่านั้นอย่างเดียว

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
from functools import wraps

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from analyze import analyze_fixture, filter_popular_matches
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

# อายุของแคชผลเช็คสมาชิกกลุ่ม ราย user (วินาที) — ลดจำนวนครั้งที่ถาม Telegram API
MEMBERSHIP_CACHE_TTL = 5 * 60  # 5 นาที

# สถานะที่ถือว่าเป็นสมาชิกกลุ่ม (left / kicked ถือว่าไม่ใช่)
MEMBER_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

# payload ของ deep link ที่ใช้ในปุ่มที่โพสต์ลงกลุ่ม (ไว้ track ที่มาเฉย ๆ)
GROUP_DEEP_LINK_PAYLOAD = "fromgroup"

GREETING = (
    "เฮียตี๋มาแล้วครับ ⚽\n"
    "เลือกวันที่อยากดู แล้วเฮียจะจัดบทวิเคราะห์ให้เป็นคู่ ๆ เลย"
)
PICK_DAY = "เลือกวันที่ต้องการดูโปรแกรมครับ 👇"
PICK_LEAGUE = "เลือกลีกครับ 👇"
PICK_MATCH = "เลือกคู่ที่อยากให้เฮียตี๋วิเคราะห์ครับ 👇"
CHECKING_MATCHES = "⏳ เฮียตี๋กำลังดูว่าคู่ไหนคนสนใจเยอะ รอแป๊บ..."
NO_POPULAR_MATCHES = (
    "ลีกนี้วันนี้มีแต่คู่เล็ก ๆ ที่คนไม่ค่อยตามครับ เฮียเลยไม่เอามาให้เลือก 🙏\n"
    "ลองดูลีกอื่นหรือวันอื่นได้เลย"
)
ANALYZING = "🔍 เฮียตี๋กำลังดูเกมนี้ให้ รอแป๊บ..."
ERROR_MESSAGE = "ขออภัยครับ เฮียตี๋ดูเกมนี้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง 🙏"
ONLY_BUTTONS = "กดเมนูด้านล่างเพื่อดูบทวิเคราะห์จากเฮียตี๋ได้เลยครับ 👇"
NO_FIXTURES = "ช่วงนี้ยังไม่มีคู่บอลของลีกที่เฮียตี๋ตามอยู่ครับ ลองใหม่อีกทีภายหลัง 🙏"
STALE_MENU = "เมนูนี้เก่าไปแล้วครับ กด /start เพื่อเริ่มใหม่"

NOT_MEMBER = (
    "โทษทีครับ เฮียตี๋คุยเฉพาะคนในกลุ่มเท่านั้น 🙏\n"
    "กดเข้ากลุ่มก่อน แล้วค่อยกลับมาทักเฮียใหม่ เดี๋ยวจัดบทวิเคราะห์ให้เต็มที่"
)
JOIN_GROUP_BUTTON = "👉 เข้ากลุ่มก่อนเลย"

GROUP_WELCOME = (
    "เฮียตี๋ประจำกลุ่มนี้แล้วครับ ⚽\n"
    "อยากรู้ว่าคู่ไหนน่าเล่น กดปุ่มข้างล่างไปคุยกับเฮียได้เลย เฮียฟันให้เป็นคู่ ๆ"
)
GROUP_BUTTON_TEXT = "🔍 คุยกับเฮียตี๋ ดูบทวิเคราะห์"

POSTGROUP_DONE = (
    "โพสต์ปุ่มลงกลุ่มเรียบร้อยครับ\n"
    "แนะนำให้ปักหมุด (pin) ข้อความนั้นไว้ สมาชิกใหม่จะได้เห็นตั้งแต่เข้ากลุ่ม"
)
POSTGROUP_PINNED = "โพสต์ปุ่มลงกลุ่มและปักหมุดให้เรียบร้อยแล้วครับ"
POSTGROUP_NOT_ADMIN = "คำสั่งนี้ใช้ได้เฉพาะแอดมินครับ"
POSTGROUP_NO_GROUP = (
    "ยังโพสต์ไม่ได้ครับ ยังไม่ได้ตั้ง GROUP_CHAT_ID ใน .env\n"
    "ใส่ id กลุ่ม (เช่น -1001234567890) แล้วรีสตาร์ทบอทก่อน"
)
POSTGROUP_FAILED = "โพสต์ลงกลุ่มไม่สำเร็จครับ ดูรายละเอียดใน log ของบอท"

# ปุ่มย้อนกลับมีแบบเดียว: พากลับหน้าแรก (เมนูเลือกวัน) ไม่ต้องย้อนทีละชั้น
BACK_TO_HOME = "⬅️ กลับหน้าแรก"
BACK_HOME_CALLBACK = "back:home"

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("heretee.bot")


# ค่าคอนฟิกกลุ่ม/แอดมิน โหลดครั้งเดียวตอนบอทเริ่มทำงาน
CONFIG = {
    "group_chat_id": None,
    "group_invite_link": None,
    "admin_user_id": None,
}


def _env_int(name):
    """อ่าน env var ที่ต้องเป็นตัวเลข — ไม่มีหรือไม่ใช่ตัวเลขคืน None พร้อม log เตือน"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s ใน .env ไม่ใช่ตัวเลข (%r) — ข้ามค่านี้ไป", name, raw)
        return None


def load_config():
    """
    อ่านค่ากลุ่ม/แอดมินจาก .env
    ถ้าไม่ได้ตั้ง GROUP_CHAT_ID บอทยังรันได้ แต่จะข้ามการเช็คสมาชิก (ให้ทดสอบส่วนอื่นได้)
    """
    load_dotenv()
    CONFIG["group_chat_id"] = _env_int("GROUP_CHAT_ID")
    CONFIG["group_invite_link"] = (os.getenv("GROUP_INVITE_LINK") or "").strip() or None
    CONFIG["admin_user_id"] = _env_int("ADMIN_USER_ID")

    if CONFIG["group_chat_id"] is None:
        logger.warning("ยังไม่ได้ตั้ง GROUP_CHAT_ID ใน .env — ข้ามการเช็คสมาชิกกลุ่ม (ใครก็ใช้บอทได้)")
    elif CONFIG["group_invite_link"] is None:
        logger.warning("ยังไม่ได้ตั้ง GROUP_INVITE_LINK — คนนอกกลุ่มจะไม่เห็นปุ่มเข้ากลุ่ม")

    if CONFIG["admin_user_id"] is None:
        logger.warning("ยังไม่ได้ตั้ง ADMIN_USER_ID — คำสั่ง /postgroup จะใช้ไม่ได้")

    return CONFIG


class MembershipCache:
    """จำผลเช็คสมาชิกราย user ไว้ 5 นาที ลดการยิง Telegram API ซ้ำ ๆ"""

    def __init__(self, ttl=MEMBERSHIP_CACHE_TTL, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._entries = {}  # user_id -> (is_member, checked_at)

    def get(self, user_id):
        """คืน True/False ถ้ายังไม่หมดอายุ — ไม่มีหรือหมดอายุคืน None"""
        entry = self._entries.get(user_id)
        if entry is None:
            return None
        is_member, checked_at = entry
        if (self.clock() - checked_at) >= self.ttl:
            del self._entries[user_id]
            return None
        return is_member

    def set(self, user_id, is_member):
        self._entries[user_id] = (is_member, self.clock())


async def is_group_member(bot, user_id, cache=None):
    """
    เช็คว่า user อยู่ในกลุ่มหรือไม่
    ยังไม่ได้ตั้ง GROUP_CHAT_ID -> ถือว่าผ่าน (ปิดฟีเจอร์)
    เรียก API ไม่สำเร็จ / status เป็น left, kicked -> ไม่ใช่สมาชิก
    """
    group_chat_id = CONFIG["group_chat_id"]
    if group_chat_id is None:
        return True

    if cache is not None:
        cached = cache.get(user_id)
        if cached is not None:
            return cached

    try:
        member = await bot.get_chat_member(group_chat_id, user_id)
        is_member = member.status in MEMBER_STATUSES
        logger.info("เช็คสมาชิก user_id=%s status=%s -> %s", user_id, member.status, is_member)
    except Exception as exc:
        # ยังไม่ได้เพิ่มบอทเข้ากลุ่ม / id ผิด / user ไม่เคยอยู่ในกลุ่ม ก็เข้าทางนี้
        logger.warning("เช็คสมาชิก user_id=%s ไม่สำเร็จ (%s) — ถือว่าไม่ใช่สมาชิก", user_id, exc)
        is_member = False

    if cache is not None:
        cache.set(user_id, is_member)
    return is_member


def join_group_keyboard():
    """ปุ่มลิงก์เข้ากลุ่ม — ถ้ายังไม่ได้ตั้ง GROUP_INVITE_LINK ก็ไม่ต้องมีปุ่ม"""
    link = CONFIG["group_invite_link"]
    if not link:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(JOIN_GROUP_BUTTON, url=link)]])


async def check_membership(context, user_id):
    """เช็คสมาชิกโดยใช้แคชที่เก็บไว้ใน bot_data"""
    cache = context.application.bot_data.get("membership_cache")
    return await is_group_member(context.bot, user_id, cache)


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
    buttons.append([InlineKeyboardButton(BACK_TO_HOME, callback_data=BACK_HOME_CALLBACK)])
    return InlineKeyboardMarkup(buttons)


def match_keyboard(snapshot, date_str, league_id, matches=None):
    """
    ปุ่มเลือกคู่บอลในลีกนั้น
    ส่ง matches เข้ามาได้ถ้ากรองมาแล้ว (ไม่ส่งจะใช้ทุกคู่ใน snapshot)
    """
    if matches is None:
        _, matches = find_league(snapshot, date_str, league_id)
    buttons = []

    for kickoff, home, away, fixture_id in (matches or [])[:MATCHES_PER_PAGE]:
        if fixture_id is None:
            continue  # ไม่มี id ก็วิเคราะห์ต่อไม่ได้ ไม่ต้องโชว์ปุ่ม
        buttons.append([InlineKeyboardButton(
            f"{kickoff}  {home} vs {away}",
            callback_data=f"match:{fixture_id}:{date_str}:{league_id}",
        )])

    buttons.append([InlineKeyboardButton(BACK_TO_HOME, callback_data=BACK_HOME_CALLBACK)])
    return InlineKeyboardMarkup(buttons)


def home_keyboard():
    """ปุ่มเดียวสำหรับย้อนกลับทุกจุด — พากลับหน้าแรก (เมนูเลือกวัน)"""
    return InlineKeyboardMarkup([[InlineKeyboardButton(BACK_TO_HOME,
                                                       callback_data=BACK_HOME_CALLBACK)]])


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


def private_only(handler):
    """
    ครอบ handler ให้รับเฉพาะแชทส่วนตัว
    update จากกลุ่ม / ซูเปอร์กลุ่ม / แชนแนล (รวมถึงการกดปุ่มที่ค้างอยู่ในกลุ่ม) จะถูกเพิกเฉยเงียบ ๆ
    ไม่ตอบ ไม่โชว์เมนู ไม่เรียก AI — กลุ่มเป็นแค่หน้าร้านที่มีปุ่ม deep link ปักหมุดไว้
    """

    @wraps(handler)
    async def wrapper(update, context):
        chat = getattr(update, "effective_chat", None)
        chat_type = getattr(chat, "type", None)

        if chat_type != ChatType.PRIVATE:
            logger.info("เพิกเฉย update จาก chat type=%s (บอทตอบเฉพาะแชทส่วนตัว)", chat_type)
            return

        return await handler(update, context)

    return wrapper


@private_only
async def start_handler(update, context):
    """/start — รองรับ deep link payload (/start <payload>) โดยยังไม่ทำ logic พิเศษ"""
    payload = context.args[0] if getattr(context, "args", None) else None
    user = update.effective_user
    user_id = user.id if user else None
    logger.info("start จาก user_id=%s payload=%s", user_id, payload)

    if user_id is not None and not await check_membership(context, user_id):
        logger.info("ปฏิเสธ user_id=%s (ไม่ได้อยู่ในกลุ่ม)", user_id)
        await update.message.reply_text(NOT_MEMBER, reply_markup=join_group_keyboard())
        return

    await update.message.reply_text(GREETING)
    await show_days(update.message, context)


@private_only
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


@private_only
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

    header = f"{league['name_th']} — {day_label(date_str, snapshot['dates'])}"

    # เช็คความนิยมของแต่ละคู่ (ยิง OddsPapi) เป็นงานบล็อกและใช้เวลา จึงบอกผู้ใช้ก่อนแล้วโยนไปเธรดแยก
    await safe_edit(query, f"{header}\n{CHECKING_MATCHES}", None)

    popular, stats = await asyncio.to_thread(
        filter_popular_matches, matches, date_str, league.get("name_en"), logger.info)

    logger.info("ลีก %s วัน %s: แสดง %d จาก %d คู่ (ซ่อน %d, ยังไม่เช็ค %d, fallback=%s)",
                league.get("name_en"), date_str, len(popular), stats["total"],
                stats["hidden"], stats["unchecked"], stats["fallback"])

    if not popular:
        await safe_edit(query, f"{header}\n{NO_POPULAR_MATCHES}", home_keyboard())
        return

    await safe_edit(
        query,
        f"{header}\n{PICK_MATCH}",
        match_keyboard(snapshot, date_str, league_id, matches=popular),
    )


@private_only
async def match_handler(update, context):
    """กดปุ่มคู่บอล -> วิเคราะห์ (จุดเดียวที่เรียก AI)"""
    query = update.callback_query
    await query.answer()

    _, fixture_id_raw, date_str, league_id_raw = query.data.split(":", 3)
    fixture_id = int(fixture_id_raw)
    league_id = int(league_id_raw)
    user_id = query.from_user.id
    logger.info("ขอวิเคราะห์ fixture_id=%s (user_id=%s)", fixture_id, user_id)

    # เช็คสมาชิกซ้ำตรงนี้ด้วย เผื่อออกจากกลุ่มระหว่างใช้งาน — บล็อกก่อนเปลืองโควตา AI
    if not await check_membership(context, user_id):
        logger.info("บล็อก user_id=%s ก่อนเรียก AI (ไม่ได้อยู่ในกลุ่มแล้ว)", user_id)
        await safe_edit(query, NOT_MEMBER, join_group_keyboard())
        return

    await safe_edit(query, ANALYZING, None)

    try:
        # analyze_fixture เป็นงานบล็อก (network + sqlite) จึงโยนไปรันในเธรดแยก
        result = await asyncio.to_thread(analyze_fixture, fixture_id, False, logger.info)
    except SystemExit as exc:
        # โค้ดเดิมใช้ fail() ที่เรียก sys.exit — ในบอทถือเป็นข้อผิดพลาดธรรมดา ห้ามให้บอทตาย
        logger.error("วิเคราะห์ fixture_id=%s ไม่สำเร็จ (fail/exit code=%s)", fixture_id, exc.code)
        await safe_edit(query, ERROR_MESSAGE, home_keyboard())
        return
    except Exception:
        logger.exception("วิเคราะห์ fixture_id=%s ไม่สำเร็จ", fixture_id)
        await safe_edit(query, ERROR_MESSAGE, home_keyboard())
        return

    logger.info(
        "ส่งบทวิเคราะห์ %s (จากแคช=%s, ยิง API-SPORTS %d ครั้ง)",
        result["match_name"], result["from_cache"], result["sports_requests"],
    )
    await safe_edit(query, result["analysis"], home_keyboard())


@private_only
async def back_handler(update, context):
    """
    ปุ่มย้อนกลับทุกจุดพากลับหน้าแรก (เมนูเลือกวัน) เลย ไม่ต้องย้อนทีละชั้น
    รับ back: ทุกแบบ รวมถึง callback รูปแบบเก่าที่อาจค้างอยู่ในแชท
    """
    query = update.callback_query
    await query.answer()

    logger.info("กลับหน้าแรก (callback=%s, user_id=%s)", query.data, query.from_user.id)
    await edit_to_days(query, context)


# ไม่ครอบ private_only: จำกัดด้วย ADMIN_USER_ID อยู่แล้ว และแอดมินอาจสั่งจากที่ไหนก็ได้
async def postgroup_handler(update, context):
    """/postgroup — แอดมินสั่งให้บอทโพสต์ปุ่ม deep link ลงกลุ่ม (ใช้ครั้งเดียวตอนตั้งระบบ)"""
    user = update.effective_user
    user_id = user.id if user else None
    admin_id = CONFIG["admin_user_id"]

    if admin_id is None or user_id != admin_id:
        logger.warning("user_id=%s พยายามใช้ /postgroup แต่ไม่ใช่แอดมิน", user_id)
        await update.message.reply_text(POSTGROUP_NOT_ADMIN)
        return

    group_chat_id = CONFIG["group_chat_id"]
    if group_chat_id is None:
        await update.message.reply_text(POSTGROUP_NO_GROUP)
        return

    try:
        me = await context.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start={GROUP_DEEP_LINK_PAYLOAD}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(GROUP_BUTTON_TEXT, url=deep_link)]])

        posted = await context.bot.send_message(group_chat_id, GROUP_WELCOME, reply_markup=keyboard)
        logger.info("โพสต์ปุ่มลงกลุ่ม %s แล้ว (message_id=%s, link=%s)",
                    group_chat_id, posted.message_id, deep_link)
    except Exception:
        logger.exception("โพสต์ปุ่มลงกลุ่ม %s ไม่สำเร็จ", group_chat_id)
        await update.message.reply_text(POSTGROUP_FAILED)
        return

    # ปักหมุดให้เลยถ้าบอทมีสิทธิ์ ถ้าไม่มีก็แค่บอกให้แอดมินปักเอง ไม่ถือเป็นความผิดพลาด
    try:
        await context.bot.pin_chat_message(group_chat_id, posted.message_id,
                                           disable_notification=True)
    except Exception as exc:
        logger.warning("ปักหมุดข้อความในกลุ่มไม่สำเร็จ (%s) — ให้แอดมินปักเอง", exc)
        await update.message.reply_text(POSTGROUP_DONE)
        return

    logger.info("ปักหมุดข้อความในกลุ่มเรียบร้อย")
    await update.message.reply_text(POSTGROUP_PINNED)


@private_only
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
    application.bot_data["membership_cache"] = MembershipCache()

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("postgroup", postgroup_handler))
    application.add_handler(CallbackQueryHandler(day_handler, pattern=r"^day:"))
    application.add_handler(CallbackQueryHandler(league_handler, pattern=r"^league:"))
    application.add_handler(CallbackQueryHandler(match_handler, pattern=r"^match:"))
    application.add_handler(CallbackQueryHandler(back_handler, pattern=r"^back:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback_handler))
    application.add_error_handler(error_handler)

    return application


def main():
    token = get_bot_token()
    load_config()
    logger.info("เริ่มบอทเฮียตี๋ (long polling)")
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
