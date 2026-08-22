"""
Phase 3C — วิเคราะห์คู่บอลด้วย Claude API ในบุคลิก "เฮียตี๋"

ขั้นตอน: ดึงข้อมูลเชิงลึกด้วย match_data.collect_match_data() -> ส่งเข้า Claude -> แสดงบทวิเคราะห์
(ยังไม่เก็บ cache — นั่นเป็นงานของ Phase 3D)

วิธีใช้:
    pip install -r requirements.txt
    python3 src/analyze.py 1557375

ต้องมีใน .env:
    API_FOOTBALL_KEY=...     สำหรับดึงข้อมูลบอล (API-SPORTS)
    ANTHROPIC_API_KEY=...    สำหรับเรียก Claude API
"""

import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from api_football import fail, get_api_key
from match_data import CountingClient, collect_match_data, parse_args

# โมเดลที่ใช้วิเคราะห์ — แก้ตรงนี้จุดเดียวถ้าจะเปลี่ยนรุ่น
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 500  # บทวิเคราะห์สั้น 5-6 บรรทัด

# path อ้างอิงจากตำแหน่งไฟล์ .py แบบเดียวกับ leagues.json — รันจากที่ไหนก็เจอ
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyst_prompt.txt"

USER_INSTRUCTION = (
    "นี่คือข้อมูลของคู่บอลที่จะเตะ วิเคราะห์คู่นี้ตามสไตล์ของเฮียตี๋ "
    "แล้วฟันธงว่าทีมไหนได้เปรียบ พร้อมเหตุผลจากข้อมูลจริง\n\n"
    "ข้อมูลคู่บอล (JSON):\n"
)


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


def analyze_match(match_summary, api_key, system_prompt, model=MODEL):
    """ส่งข้อมูลคู่บอลเข้า Claude แล้วคืนบทวิเคราะห์เป็นข้อความ"""
    client = anthropic.Anthropic(api_key=api_key)
    match_json = json.dumps(match_summary, ensure_ascii=False, indent=2)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": USER_INSTRUCTION + match_json}],
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

    return text


def main():
    fixture_id = parse_args(sys.argv[1:])
    system_prompt = load_system_prompt()
    anthropic_key = get_anthropic_key()

    client = CountingClient(get_api_key())
    print(f"กำลังดึงข้อมูลเชิงลึกของ fixture_id={fixture_id} ...")
    match_summary = collect_match_data(client, fixture_id)
    print(f"ดึงข้อมูลครบ (ยิง API-SPORTS ไป {client.request_count} ครั้ง)")

    match_name = (match_summary.get("match") or {}).get("name") or f"fixture {fixture_id}"
    print(f"กำลังให้เฮียตี๋วิเคราะห์ {match_name} ด้วย {MODEL} ...")
    analysis = analyze_match(match_summary, anthropic_key, system_prompt)

    print()
    print("=" * 70)
    print(analysis)
    print("=" * 70)


if __name__ == "__main__":
    main()
