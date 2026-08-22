"""
Phase 3B — ดึงข้อมูลเชิงลึกของคู่บอลหนึ่งคู่จาก API-Football (API-SPORTS)
แล้วกลั่นเหลือเฉพาะข้อมูลที่มีความหมาย ก่อนแสดงเป็น JSON อ่านง่าย

ยังไม่ต่อ AI และยังไม่เก็บ cache — เฟสนี้แค่ดึง + กลั่น + แสดงให้ตรวจ

วิธีใช้:
    python3 src/match_data.py 1234567

ยิง API 5 ครั้งต่อหนึ่งคู่:
    1) /fixtures?id=...                      รายละเอียดคู่ (ต้องดึงก่อนเพื่อเอา team id / league / season)
    2) /teams/statistics  ทีมเหย้า
    3) /teams/statistics  ทีมเยือน
    4) /fixtures/headtohead?h2h=A-B&last=5   ห้านัดหลังสุดที่เจอกัน
    5) /standings                            อันดับในตาราง (เก็บเฉพาะสองทีมนี้)

ฟอร์ม 5 นัดล่าสุด (เช่น "WWDLW") ติดมากับ /teams/statistics อยู่แล้ว จึงไม่ต้องยิงแยก
"""

import json
import sys

from api_football import TIMEZONE_NAME, api_get, fail, get_api_key

H2H_LAST = 5


def parse_args(argv):
    """รับ fixture_id ตัวเดียวจาก command line"""
    if any(arg in ("-h", "--help") for arg in argv):
        print(__doc__.strip())
        sys.exit(0)

    if len(argv) != 1:
        fail(
            "[ERROR] ต้องระบุ fixture_id หนึ่งค่า",
            "ตัวอย่าง: python3 src/match_data.py 1234567",
            "หา fixture_id ได้จากผลลัพธ์ของ fetch_fixtures.py",
        )

    try:
        return int(argv[0])
    except ValueError:
        fail(f"[ERROR] fixture_id ต้องเป็นตัวเลข แต่ได้รับ: {argv[0]!r}")


def dig(data, *keys, default=None):
    """ไล่อ่านค่าซ้อนชั้นแบบปลอดภัย — ชั้นไหนหายหรือไม่ใช่ dict ก็คืน default ไม่พัง"""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def to_number(value):
    """แปลงเป็นตัวเลข (API ส่งค่าเฉลี่ยมาเป็น string เช่น "1.5") — แปลงไม่ได้คืน None"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return float(text) if "." in text else int(text)
    except (TypeError, ValueError):
        return None


class CountingClient:
    """ห่อ api_get ไว้เพื่อนับจำนวน request ที่ยิงจริง และบอกความคืบหน้าระหว่างทาง"""

    def __init__(self, api_key):
        self.api_key = api_key
        self.request_count = 0

    def get(self, endpoint, params=None, label=""):
        self.request_count += 1
        print(f"  - ยิง API ครั้งที่ {self.request_count}: /{endpoint} {label}".rstrip())
        return api_get(endpoint, self.api_key, params)


def extract_fixture_info(response):
    """ดึงข้อมูลหลักของคู่จากผล /fixtures?id= — ถ้าไม่พบคู่คืน None"""
    if not response:
        return None

    item = response[0]
    if not isinstance(item, dict):
        return None

    return {
        "fixture_id": dig(item, "fixture", "id"),
        "kickoff": dig(item, "fixture", "date"),
        "status": dig(item, "fixture", "status", "long"),
        "venue": dig(item, "fixture", "venue", "name"),
        "league_id": dig(item, "league", "id"),
        "league_name": dig(item, "league", "name"),
        "league_country": dig(item, "league", "country"),
        "season": dig(item, "league", "season"),
        "round": dig(item, "league", "round"),
        "home_id": dig(item, "teams", "home", "id"),
        "home_name": dig(item, "teams", "home", "name"),
        "away_id": dig(item, "teams", "away", "id"),
        "away_name": dig(item, "teams", "away", "name"),
    }


def summarize_team_stats(response):
    """
    กลั่นผล /teams/statistics เหลือเฉพาะตัวเลขที่ใช้วิเคราะห์
    ข้อมูลส่วนไหนไม่มีจะเป็น None ไม่ทำให้พัง
    """
    stats = response if isinstance(response, dict) else {}

    return {
        "form": dig(stats, "form"),
        "played": to_number(dig(stats, "fixtures", "played", "total")),
        "wins": to_number(dig(stats, "fixtures", "wins", "total")),
        "draws": to_number(dig(stats, "fixtures", "draws", "total")),
        "loses": to_number(dig(stats, "fixtures", "loses", "total")),
        "goals_for": to_number(dig(stats, "goals", "for", "total", "total")),
        "goals_against": to_number(dig(stats, "goals", "against", "total", "total")),
        "goals_for_avg": to_number(dig(stats, "goals", "for", "average", "total")),
        "goals_against_avg": to_number(dig(stats, "goals", "against", "average", "total")),
        "clean_sheets": to_number(dig(stats, "clean_sheet", "total")),
        "failed_to_score": to_number(dig(stats, "failed_to_score", "total")),
    }


def find_standing(response, team_id):
    """หาแถวตารางคะแนนของทีมที่ต้องการจากผล /standings — ไม่เจอคืน None"""
    if not response or team_id is None:
        return None

    for league_entry in response:
        groups = dig(league_entry, "league", "standings", default=[])
        if not isinstance(groups, list):
            continue

        for group in groups:
            # ปกติเป็น list ของแถว แต่บางลีกส่งมาเป็นแถวเดี่ยว ๆ
            rows = group if isinstance(group, list) else [group]
            for row in rows:
                if dig(row, "team", "id") != team_id:
                    continue
                return {
                    "rank": to_number(dig(row, "rank")),
                    "points": to_number(dig(row, "points")),
                    "goals_diff": to_number(dig(row, "goalsDiff")),
                    "form": dig(row, "form"),
                    "group": dig(row, "group"),
                    "played": to_number(dig(row, "all", "played")),
                }

    return None


def summarize_h2h(response, limit=H2H_LAST):
    """กลั่นผล /fixtures/headtohead เหลือ วันที่ / คู่ / สกอร์"""
    if not response:
        return []

    matches = []
    for item in response[:limit]:
        home_goals = dig(item, "goals", "home")
        away_goals = dig(item, "goals", "away")
        score = (
            f"{home_goals}-{away_goals}"
            if home_goals is not None and away_goals is not None
            else "ไม่มีข้อมูลสกอร์"
        )

        matches.append({
            "date": (dig(item, "fixture", "date") or "")[:10] or None,
            "league": dig(item, "league", "name"),
            "home": dig(item, "teams", "home", "name"),
            "away": dig(item, "teams", "away", "name"),
            "score": score,
        })

    return matches


def build_summary(info, home_stats, away_stats, standings_response, h2h_response):
    """ประกอบ dict สรุปทั้งคู่ พร้อมบันทึกไว้ใน notes ว่าส่วนไหนไม่มีข้อมูล"""
    notes = []

    home = {"team_id": info["home_id"], "name": info["home_name"]}
    away = {"team_id": info["away_id"], "name": info["away_name"]}

    for side, label, raw in ((home, "ทีมเหย้า", home_stats), (away, "ทีมเยือน", away_stats)):
        summary = summarize_team_stats(raw)
        if all(value is None for value in summary.values()):
            notes.append(f"ไม่มีสถิติของ{label} ({side['name']}) ในลีก/ฤดูกาลนี้")
        side.update(summary)

    for side, label in ((home, "ทีมเหย้า"), (away, "ทีมเยือน")):
        standing = find_standing(standings_response, side["team_id"])
        if standing is None:
            notes.append(f"ไม่มีข้อมูลอันดับตารางของ{label} ({side['name']})")
        side["standing"] = standing

    h2h = summarize_h2h(h2h_response)
    if not h2h:
        notes.append("ไม่มีสถิติการเจอกันของสองทีมนี้")

    return {
        "fixture_id": info["fixture_id"],
        "match": {
            "name": f"{info['home_name']} vs {info['away_name']}",
            "league": info["league_name"],
            "league_id": info["league_id"],
            "country": info["league_country"],
            "season": info["season"],
            "round": info["round"],
            "kickoff": info["kickoff"],
            "status": info["status"],
            "venue": info["venue"],
        },
        "home": home,
        "away": away,
        "h2h": h2h,
        "notes": notes,
    }


def collect_match_data(client, fixture_id):
    """ยิงทั้ง 5 endpoint ตามลำดับแล้วคืน dict สรุป"""
    fixture_response = client.get(
        "fixtures",
        {"id": fixture_id, "timezone": TIMEZONE_NAME},
        label=f"(fixture_id={fixture_id})",
    )

    info = extract_fixture_info(fixture_response)
    if info is None or info["fixture_id"] is None:
        fail(
            f"[ERROR] ไม่พบคู่บอล fixture_id={fixture_id}",
            "วิธีแก้: ตรวจว่า id ถูกต้อง (ดูได้จากผลลัพธ์ของ fetch_fixtures.py)",
            "หมายเหตุ: free plan ดูข้อมูลได้เฉพาะช่วง เมื่อวาน–พรุ่งนี้ เท่านั้น",
        )

    league_id = info["league_id"]
    season = info["season"]

    def team_stats(team_id, team_name):
        if team_id is None or league_id is None or season is None:
            return None
        return client.get(
            "teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
            label=f"({team_name})",
        )

    home_stats = team_stats(info["home_id"], info["home_name"])
    away_stats = team_stats(info["away_id"], info["away_name"])

    h2h_response = []
    if info["home_id"] is not None and info["away_id"] is not None:
        h2h_response = client.get(
            "fixtures/headtohead",
            {"h2h": f"{info['home_id']}-{info['away_id']}", "last": H2H_LAST},
            label=f"(ย้อนหลัง {H2H_LAST} นัด)",
        )

    standings_response = []
    if league_id is not None and season is not None:
        standings_response = client.get(
            "standings",
            {"league": league_id, "season": season},
            label=f"(league={league_id}, season={season})",
        )

    return build_summary(info, home_stats, away_stats, standings_response, h2h_response)


def main():
    fixture_id = parse_args(sys.argv[1:])
    client = CountingClient(get_api_key())

    print(f"กำลังดึงข้อมูลเชิงลึกของ fixture_id={fixture_id} ...")
    summary = collect_match_data(client, fixture_id)

    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print(f"ยิง API ไปทั้งหมด {client.request_count} ครั้ง")


if __name__ == "__main__":
    main()
