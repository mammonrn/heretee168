"""
โมดูลกลางสำหรับคุยกับ API-Football (API-SPORTS) — ใช้ร่วมกันทุกสคริปต์ในโปรเจกต์

รวมสิ่งที่ทุกสคริปต์ต้องใช้เหมือนกันไว้ที่เดียว:
  - อ่าน API key จาก .env (fail fast ถ้าไม่มี)
  - timezone Asia/Bangkok
  - เรียก endpoint พร้อมจัดการ error ครบทุกกรณี (429 / 401 / 403 / errors ใน body 200)
"""

import os
import sys
from datetime import timedelta, timezone

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - รองรับ Python เก่ากว่า 3.9
    ZoneInfo = None

BASE_URL = "https://v3.football.api-sports.io"
TIMEZONE_NAME = "Asia/Bangkok"
REQUEST_TIMEOUT = 20  # วินาที


def fail(*lines):
    """พิมพ์ข้อความ error แล้วจบโปรแกรมทันที (ไม่ crash เงียบ)"""
    for index, line in enumerate(lines):
        print(line if index == 0 else f"        {line}")
    sys.exit(1)


def get_bangkok_tz():
    """คืน tzinfo ของ Asia/Bangkok ถ้าเครื่องไม่มีฐานข้อมูล timezone ให้ fallback เป็น UTC+7"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=7))


def get_api_key():
    """อ่าน API key จาก .env — ถ้าไม่พบให้หยุดทันที (fail fast)"""
    load_dotenv()
    api_key = (os.getenv("API_FOOTBALL_KEY") or "").strip()

    if not api_key:
        fail(
            "[ERROR] ไม่พบ API key: ยังไม่ได้ตั้งค่า environment variable ชื่อ API_FOOTBALL_KEY",
            "วิธีแก้: สร้างไฟล์ .env (คัดลอกจาก .env.example) แล้วใส่บรรทัด",
            "API_FOOTBALL_KEY=คีย์จริงของคุณ",
        )

    if api_key == "your_key_here":
        fail("[ERROR] API_FOOTBALL_KEY ยังเป็นค่าตัวอย่าง 'your_key_here' — กรุณาใส่คีย์จริงในไฟล์ .env")

    return api_key


def api_get(endpoint, api_key, params=None):
    """
    เรียก endpoint ของ API-SPORTS แล้วคืนค่าในฟิลด์ response (list)
    จัดการ error ทุกกรณีให้ครบ ถ้าพลาดจะพิมพ์สาเหตุแล้วจบโปรแกรม
    """
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    headers = {"x-apisports-key": api_key}

    try:
        response = requests.get(url, headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        fail(f"[ERROR] เชื่อมต่อ API ไม่สำเร็จ: หมดเวลารอ (เกิน {REQUEST_TIMEOUT} วินาที) — ลองใหม่อีกครั้ง")
    except requests.exceptions.ConnectionError as exc:
        fail(f"[ERROR] เชื่อมต่อ API ไม่ได้: ตรวจสอบอินเทอร์เน็ต/DNS ของเครื่อง ({exc})")
    except requests.exceptions.RequestException as exc:
        fail(f"[ERROR] เรียก API ล้มเหลว: {exc}")

    status = response.status_code

    if status == 429:
        fail(
            "[ERROR] โดน rate limit (HTTP 429): เรียก API ถี่เกินไปหรือใช้โควตาของแพลนหมดแล้ว",
            "วิธีแก้: รอให้โควตารีเซ็ต (ปกติรายวัน/รายนาที) หรืออัปเกรดแพลนบน API-SPORTS",
        )

    if status in (401, 403):
        fail(
            f"[ERROR] ยืนยันตัวตนไม่ผ่าน (HTTP {status}): API key ผิด หรือบัญชียังไม่ได้ subscribe แพลนบน API-SPORTS",
            "วิธีแก้: ตรวจค่า API_FOOTBALL_KEY ใน .env และเช็คสถานะแพลนที่ dashboard.api-football.com",
        )

    if status != 200:
        fail(
            f"[ERROR] API ตอบกลับด้วยสถานะที่ไม่คาดคิด: HTTP {status}",
            f"รายละเอียด: {response.text[:500]}",
        )

    try:
        data = response.json()
    except ValueError:
        fail(
            "[ERROR] อ่านข้อมูลจาก API ไม่ได้: ผลลัพธ์ไม่ใช่ JSON ที่ถูกต้อง",
            f"รายละเอียด: {response.text[:500]}",
        )

    _raise_body_errors(data)

    return data.get("response", []) or []


def _raise_body_errors(data):
    """
    API-SPORTS มักตอบ HTTP 200 แต่แจ้งข้อผิดพลาดไว้ในฟิลด์ errors
      key ผิด/หมดอายุ -> {"errors": {"token": "..."}}
      โควตาหมด        -> {"errors": {"requests": "..."}}
    """
    errors = data.get("errors")
    if not errors:
        return

    if isinstance(errors, dict):
        if "token" in errors:
            fail(
                "[ERROR] API key ไม่ถูกต้อง (API-SPORTS ตอบ HTTP 200 พร้อม errors.token):",
                f"{errors['token']}",
                "วิธีแก้: ตรวจค่า API_FOOTBALL_KEY ใน .env ว่าเป็นคีย์จาก dashboard.api-football.com",
                "(คีย์ของ RapidAPI ใช้กับ endpoint นี้ไม่ได้) และเช็คว่าบัญชี subscribe แพลนแล้ว",
            )

        if "requests" in errors:
            fail(
                "[ERROR] ใช้โควตาเรียก API หมดแล้ว (errors.requests):",
                f"{errors['requests']}",
                "วิธีแก้: รอให้โควตารีเซ็ต หรืออัปเกรดแพลนบน API-SPORTS",
            )

        print("[ERROR] API แจ้งข้อผิดพลาดกลับมา:")
        for key, message in errors.items():
            print(f"        - {key}: {message}")
        sys.exit(1)

    fail("[ERROR] API แจ้งข้อผิดพลาดกลับมา:", f"- {errors}")
