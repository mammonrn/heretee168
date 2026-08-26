"""
Unit test ของการหา "เส้นแฮนดิแคปหลักที่ตลาดใช้จริง" (mainLine) — ไม่ยิง API เลยสักครั้ง

ทุกเคสใช้ข้อมูลจำลอง (mock) ที่เขียนเลียนโครงสร้างจริงของ OddsPapi เท่าที่ยืนยันมาแล้ว
ไม่ได้ต่อเน็ต ไม่ได้แตะ cache.db จริง (แคชถูก monkeypatch ทิ้งในเทสต์ที่เกี่ยวข้อง)

วิธีรัน:
    python3 src/test_mainline.py
    python3 src/test_mainline.py -v
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze
import odds_data
import test_odds_offline


# ---------- ตัวช่วยสร้างข้อมูลจำลอง ----------


def outcome(price, main_line=None, changed_at=None, outcome_id=None):
    """
    outcome หนึ่งช่องตามโครงสร้างจริงที่ยืนยันแล้ว:
        {"players": {"0": {"price": ..., "mainLine": ...}}}
    ทั้งราคาและธง mainLine อยู่ใน dict ใบเดียวกันคือ players["0"]
    main_line=None แปลว่าไม่มีฟิลด์ mainLine เลย
    outcome_id คือ bookmakerOutcomeId เช่น "3.5/over" ซึ่งอยู่ใน dict ใบเดียวกันกับราคา
    """
    player = {"price": price}
    if main_line is not None:
        player["mainLine"] = main_line
    if outcome_id is not None:
        player["bookmakerOutcomeId"] = outcome_id

    data = {"players": {"0": player}}
    if changed_at is not None:
        data["changedAt"] = changed_at
    return data


def group(*pairs):
    """market กลุ่มหนึ่ง: {"outcomes": {market_id: outcome, ...}}"""
    return {"outcomes": {str(market_id): value for market_id, value in pairs}}


def book(markets):
    """ข้อมูลเจ้ามือหนึ่งเจ้า: {"markets": {คีย์กลุ่ม: กลุ่ม}}"""
    return {"markets": markets}


# สารบัญที่ใช้ในเทสต์ — เลียนของที่ /v4/markets ควรคืนมา (เฉพาะ AH)
CATALOG = {
    "1058": -1.75, "1059": 1.75,
    "1068": -0.5, "1069": 0.5,
    "1070": -0.25, "1071": 0.25,
    "1072": 0.0, "1073": 0.0,
    "1074": 0.25, "1075": -0.25,
    "1076": 0.5, "1077": -0.5,
    "1080": -0.75, "1081": 0.75,
    "1090": -1.0, "1091": 1.0,
}


class TestMarketCatalogParsing(unittest.TestCase):
    """แปลง response ของ /v4/markets เป็นสารบัญ {market id: handicap}"""

    def test_keeps_only_asian_handicap_markets(self):
        payload = [
            {"marketId": 101, "marketName": "1X2 Home", "handicap": None},
            {"marketId": 1068, "marketName": "Asian Handicap -0.5", "handicap": -0.5},
            {"marketId": 1074, "marketName": "Asian Handicap +0.25", "handicap": 0.25},
            {"marketId": 1010, "marketName": "Over/Under 2.5 Over", "handicap": 2.5},
        ]
        self.assertEqual(odds_data.parse_market_catalog(payload),
                         {"1068": -0.5, "1074": 0.25})

    def test_accepts_payload_wrapped_in_dict(self):
        payload = {"data": [{"marketId": "1072", "marketName": "Asian Handicap 0",
                             "handicap": "0"}]}
        self.assertEqual(odds_data.parse_market_catalog(payload), {"1072": 0.0})

    def test_parses_string_handicaps_with_plus_sign(self):
        payload = [{"marketId": 1076, "marketName": "Asian Handicap +0.5", "handicap": "+0.5"}]
        self.assertEqual(odds_data.parse_market_catalog(payload), {"1076": 0.5})

    def test_drops_markets_without_a_usable_handicap(self):
        payload = [{"marketId": 1068, "marketName": "Asian Handicap -0.5", "handicap": "n/a"},
                   {"marketId": 1069, "marketName": "Asian Handicap +0.5"}]
        self.assertEqual(odds_data.parse_market_catalog(payload), {})

    def test_accepts_the_short_ah_spelling(self):
        payload = [{"marketId": 1070, "marketName": "AH -0.25", "handicap": -0.25},
                   {"marketId": 1076, "marketName": "AH +0.5", "handicap": 0.5}]
        self.assertEqual(odds_data.parse_market_catalog(payload),
                         {"1070": -0.25, "1076": 0.5})

    def test_short_spelling_does_not_match_unrelated_names(self):
        payload = [{"marketId": 500, "marketName": "Ahead At Half Time", "handicap": 0.5}]
        self.assertEqual(odds_data.parse_market_catalog(payload), {})

    def test_drops_other_periods_and_other_handicap_types(self):
        payload = [
            {"marketId": 2068, "marketName": "1st Half Asian Handicap -0.5", "handicap": -0.5},
            {"marketId": 3068, "marketName": "Corner Asian Handicap -1.5", "handicap": -1.5},
            {"marketId": 4068, "marketName": "European Handicap -1", "handicap": -1.0},
            {"marketId": 1068, "marketName": "Asian Handicap -0.5", "handicap": -0.5},
        ]
        self.assertEqual(odds_data.parse_market_catalog(payload), {"1068": -0.5})

    def test_ignores_junk_entries_without_crashing(self):
        payload = ["ไม่ใช่ dict", None, {"marketName": "Asian Handicap 0", "handicap": 0}]
        self.assertEqual(odds_data.parse_market_catalog(payload), {})


class TestFetchMarketCatalog(unittest.TestCase):
    """fetch_market_catalog ต้อง fail-safe และแคชยาว ไม่กินโควตาต่อการวิเคราะห์"""

    def setUp(self):
        self.calls = []
        odds_data._market_catalog_cache["map"] = None
        self._api_get = odds_data.api_get
        self._get = odds_data.cache_db.get_odds
        self._save = odds_data.cache_db.save_odds
        self._init = odds_data.cache_db.init_db
        # ตัด cache.db จริงออกไปให้หมด เทสต์ต้องไม่แตะไฟล์ฐานข้อมูลของจริง
        self.saved = {}
        odds_data.cache_db.init_db = lambda *a, **k: None
        odds_data.cache_db.get_odds = lambda key, ttl, **k: self.saved.get(key)
        odds_data.cache_db.save_odds = lambda key, payload, **k: self.saved.__setitem__(
            key, {"payload": payload, "created_at": "จำลอง"})

    def tearDown(self):
        odds_data.api_get = self._api_get
        odds_data.cache_db.get_odds = self._get
        odds_data.cache_db.save_odds = self._save
        odds_data.cache_db.init_db = self._init
        odds_data._market_catalog_cache["map"] = None

    def fake_api(self, payload):
        def _api_get(endpoint, params, api_key=None, counter=None):
            self.calls.append((endpoint, dict(params)))
            return payload
        odds_data.api_get = _api_get

    def test_calls_markets_endpoint_with_sport_id(self):
        self.fake_api([{"marketId": 1068, "marketName": "Asian Handicap -0.5", "handicap": -0.5}])
        catalog = odds_data.fetch_market_catalog(api_key="x")

        self.assertEqual(catalog, {"1068": -0.5})
        self.assertEqual(self.calls, [("markets", {"sportId": odds_data.SPORT_ID})])

    def test_second_call_uses_cache_and_spends_no_quota(self):
        self.fake_api([{"marketId": 1068, "marketName": "Asian Handicap -0.5", "handicap": -0.5}])
        odds_data.fetch_market_catalog(api_key="x")
        odds_data._market_catalog_cache["map"] = None  # จำลองการรีสตาร์ทโปรเซส
        catalog = odds_data.fetch_market_catalog(api_key="x")

        self.assertEqual(catalog, {"1068": -0.5})
        self.assertEqual(len(self.calls), 1, "ครั้งที่สองต้องอ่านจากแคช ไม่ยิง API ซ้ำ")

    def test_cache_ttl_is_seven_days(self):
        self.assertEqual(odds_data.MARKET_CATALOG_TTL, 7 * 24 * 60 * 60)

    def test_falls_back_when_api_fails(self):
        def _api_get(endpoint, params, api_key=None, counter=None):
            raise SystemExit(1)  # api_get ใช้ fail() ที่เรียก sys.exit
        odds_data.api_get = _api_get

        self.assertEqual(odds_data.fetch_market_catalog(api_key="x"),
                         odds_data.FALLBACK_AH_CATALOG)

    def test_falls_back_when_response_has_no_handicap_markets(self):
        self.fake_api([{"marketId": 101, "marketName": "1X2 Home"}])
        self.assertEqual(odds_data.fetch_market_catalog(api_key="x"),
                         odds_data.FALLBACK_AH_CATALOG)

    def test_does_not_cache_the_fallback_catalog(self):
        self.fake_api([])
        odds_data.fetch_market_catalog(api_key="x")
        self.assertNotIn(odds_data.MARKET_CATALOG_CACHE_KEY, self.saved)


# raw response จริงของ pinnacle บน VPS — คัดลอกมาทั้งก้อน ไม่ได้ย่อ
# top-level key "1058" คือ "กลุ่มของเส้น -1.75 ทั้งเส้น" ข้างในมีทั้งฝั่งเหย้า (1058)
# และฝั่งเยือน (1059) เป็น sibling กัน แบบเดียวกับ 1068/1069 ที่ทำงานถูกมาตั้งแต่ Phase 5A
REAL_PINNACLE_GROUP_1058 = {
    "outcomes": {
        "1058": {"players": {"0": {"price": 1.917, "mainLine": True,
                                   "bookmakerOutcomeId": "-1.75/home"}}},
        "1059": {"players": {"0": {"price": 1.97, "mainLine": True,
                                   "bookmakerOutcomeId": "-1.75/away"}}},
    }
}

# สารบัญที่ /v4/markets ส่งกลับมาจริงบน VPS — มีแต่ id ฝั่งเหย้า ไม่มี 1059
# เคสนี้แหละที่เคยทำให้ได้ verdict "no_pair" เพราะโค้ดบังคับว่าฝั่งเยือนต้องอยู่ในสารบัญด้วย
REAL_CATALOG_HOME_ONLY = {"1058": -1.75, "1068": -0.5, "1070": -0.25,
                          "1072": 0.0, "1074": 0.25, "1076": 0.5}

# โครงสร้าง outcome จริงจาก raw response ของ pinnacle บน VPS (ยืนยันแล้ว)
# ธง mainLine อยู่ระดับเดียวกับ price คือใน players["0"] และเส้นหลักจริงคือ -1.75 (market 1058)
REAL_PINNACLE_MARKETS = {
    "1058": {"outcomes": {
        "1058": {"players": {"0": {"price": 1.917, "mainLine": True}}},
        "1059": {"players": {"0": {"price": 1.97, "mainLine": True}}},
    }},
    "1068": {"outcomes": {
        "1068": {"players": {"0": {"price": 1.30, "mainLine": False}}},
        "1069": {"players": {"0": {"price": 3.45, "mainLine": False}}},
    }},
    "1072": {"outcomes": {
        "1072": {"players": {"0": {"price": 1.15, "mainLine": False}}},
        "1073": {"players": {"0": {"price": 5.20, "mainLine": False}}},
    }},
}


# raw response จริงของตลาดสูง/ต่ำบน VPS — 2.5 ไม่ใช่เส้นหลักแล้ว เส้นหลักจริงคือ 3.5
# เลขเส้นฝังอยู่ใน bookmakerOutcomeId ตรง ๆ จึงไม่ต้องพึ่งสารบัญ market เลย
REAL_TOTAL_MARKETS = {
    "1010": group((1010, outcome(1.55, False, outcome_id="2.5/over")),
                  (1011, outcome(2.45, False, outcome_id="2.5/under"))),
    "1012": group((1012, outcome(2.02, True, outcome_id="3.5/over")),
                  (1013, outcome(1.83, True, outcome_id="3.5/under"))),
}


class TestRealPinnacleSample(unittest.TestCase):
    """
    เคสหลัก: ข้อมูลจริงจาก VPS ที่เคยพัง — ต้องได้เส้น -1.75 และต้องไม่ fallback

    เดิมพังสองชั้นพร้อมกัน:
      1. market 1058 ไม่อยู่ในสารบัญสำรอง กลุ่มนี้เลยถูกข้ามตั้งแต่ก่อนดูธง mainLine
      2. ธง mainLine อ่านแบบไล่เดาหลายชั้น ซึ่งมีชั้นที่ยืมค่าจาก outcome ตัวแรกของกลุ่ม
    """

    def test_finds_the_real_main_line_at_minus_one_seventy_five(self):
        found = odds_data.find_main_line(book(REAL_PINNACLE_MARKETS), CATALOG)

        self.assertIsNotNone(found, "ต้องเจอเส้นหลัก ไม่ใช่คืน None แล้วไป fallback")
        self.assertEqual(found["source"], "mainline")
        self.assertEqual(found["handicap"], -1.75)
        self.assertEqual(found["line"], 1.75)
        self.assertEqual(found["home"], 1.917)
        self.assertEqual(found["away"], 1.97)
        self.assertEqual(found["market_ids"], {"home": "1058", "away": "1059"})

    def test_works_on_the_fallback_catalog_alone(self):
        """แม้ /v4/markets ใช้ไม่ได้ สารบัญสำรองก็ต้องครอบ 1058 ถึงจะเจอเส้นนี้"""
        found = odds_data.find_main_line(book(REAL_PINNACLE_MARKETS),
                                         odds_data.FALLBACK_AH_CATALOG)

        self.assertIsNotNone(found)
        self.assertEqual(found["handicap"], -1.75)

    def test_reaches_the_prompt_as_the_line_that_gets_spoken(self):
        raw = {"pinnacle": book(REAL_PINNACLE_MARKETS)}
        summary = analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, CATALOG))

        self.assertEqual(summary["handicap"]["line"], "1.75")
        self.assertEqual(summary["handicap"]["line_label"], "ลูกครึ่งควบสอง [1.5-2]")
        self.assertEqual(summary["handicap"]["source"], "mainline")
        self.assertEqual(summary["handicap"]["giver"], "home")
        self.assertEqual(summary["handicap_favourite"], "home")

    def test_the_exact_raw_group_from_the_vps(self):
        """ก้อนข้อมูลจริงตรง ๆ กับสารบัญจริงที่มีแต่ฝั่งเหย้า — ต้องได้ -1.75 ไม่ fallback"""
        scan = odds_data.scan_handicap_lines(
            {"markets": {"1058": REAL_PINNACLE_GROUP_1058}}, REAL_CATALOG_HOME_ONLY)

        self.assertEqual(scan["verdict"], "mainline")
        self.assertEqual(scan["main"]["handicap"], -1.75)
        self.assertEqual(scan["main"]["home"], 1.917)
        self.assertEqual(scan["main"]["away"], 1.97)
        self.assertEqual(scan["main"]["market_ids"], {"home": "1058", "away": "1059"})

    def test_the_away_side_need_not_be_in_the_catalog(self):
        """สารบัญใช้แค่แปลง id ฝั่งเหย้าเป็นเลขเส้น ฝั่งเยือนหาจาก id + 1 ในข้อมูลราคา"""
        self.assertNotIn("1059", REAL_CATALOG_HOME_ONLY)

        raw = {"pinnacle": {"markets": {"1058": REAL_PINNACLE_GROUP_1058}}}
        book_data = odds_data.distill_odds(raw, REAL_CATALOG_HOME_ONLY)["books"]["pinnacle"]

        self.assertEqual(book_data["handicap_verdict"], "mainline")
        self.assertEqual(book_data["handicap"]["handicap"], -1.75)
        self.assertEqual(book_data["handicap"]["source"], "mainline")

    def test_an_away_only_catalog_entry_is_not_read_as_a_home_side(self):
        """สารบัญมีทั้ง 1058 และ 1059 ต้องไม่ทำให้ 1059 ไปจับกับ 1060 กลายเป็นคนละเส้น"""
        data = book({
            "1058": REAL_PINNACLE_GROUP_1058,
            "1060": group((1060, outcome(1.80, False)), (1061, outcome(2.10, False))),
        })
        scan = odds_data.scan_handicap_lines(data, odds_data.FALLBACK_AH_CATALOG)

        self.assertEqual([(pair["home_id"], pair["away_id"]) for pair in scan["pairs"]],
                         [("1058", "1059"), ("1060", "1061")])
        self.assertEqual(scan["main"]["handicap"], -1.75)

    def test_the_flag_sits_beside_the_price_not_above_it(self):
        outcome = REAL_PINNACLE_MARKETS["1058"]["outcomes"]["1058"]

        self.assertTrue(odds_data.main_line_flag(outcome))
        self.assertEqual(odds_data.main_line_player(outcome)["price"], 1.917)
        # ธงไม่ได้อยู่ระดับ outcome — ยืนยันว่าเราไม่ได้อ่านจากชั้นนั้น
        self.assertNotIn("mainLine", outcome)


# ชุดเคสที่เคยทำให้รายงานกับผล JSON เล่าคนละเรื่อง — ต้องตรงกันทุกเคส
CONSISTENCY_CASES = {
    "เส้นหลักปกติ": REAL_PINNACLE_MARKETS,
    "ปักธงหลายเส้น": {
        "1058": group((1058, outcome(1.90, True)), (1059, outcome(1.90, True))),
        "1068": group((1068, outcome(1.30, True)), (1069, outcome(3.45, True))),
    },
    "ปักธงแต่ราคาขาดฝั่งหนึ่ง": {
        "1058": group((1058, outcome(1.917, True)), (1059, outcome(None, True))),
        "1068": group((1068, outcome(1.30, False)), (1069, outcome(3.45, False))),
    },
    "ปักธงฝั่งเดียว": {
        "1058": group((1058, outcome(1.917, True)), (1059, outcome(1.97, False))),
        "1068": group((1068, outcome(1.30, False)), (1069, outcome(3.45, False))),
    },
    "ไม่มีธงเลย": {
        "1068": group((1068, outcome(1.95)), (1069, outcome(1.90))),
    },
    "มีฝั่งเหย้าแต่ไม่มีฝั่งเยือน": {
        "1058": group((1058, outcome(1.917, True))),
    },
    "ฝั่งเยือนอยู่คนละกลุ่มกับฝั่งเหย้า": {
        "1058": group((1058, outcome(1.917, True))),
        "1059": group((1059, outcome(1.97, True))),
    },
    "ไม่มี AH เลย": {
        "101": group((101, outcome(1.80)), (102, outcome(3.50)), (103, outcome(4.20))),
    },
    "มีทั้งแฮนดิแคปและสูง/ต่ำ": {
        "1058": group((1058, outcome(1.917, True, outcome_id="-1.75/home")),
                      (1059, outcome(1.97, True, outcome_id="-1.75/away"))),
        "1010": group((1010, outcome(1.55, False, outcome_id="2.5/over")),
                      (1011, outcome(2.45, False, outcome_id="2.5/under"))),
        "1012": group((1012, outcome(2.02, True, outcome_id="3.5/over")),
                      (1013, outcome(1.83, True, outcome_id="3.5/under"))),
    },
    "สูง/ต่ำปักธงหลายเส้น": {
        "1010": group((1010, outcome(1.95, True, outcome_id="2.5/over")),
                      (1011, outcome(1.87, True, outcome_id="2.5/under"))),
        "1012": group((1012, outcome(2.30, True, outcome_id="3.5/over")),
                      (1013, outcome(1.62, True, outcome_id="3.5/under"))),
    },
    "สูง/ต่ำมีฝั่งเดียว": {
        "1012": group((1012, outcome(2.02, True, outcome_id="3.5/over"))),
    },
    "มีแต่ตลาดประตูทีมเดียว": {
        "1200": group((1200, outcome(1.90, True, outcome_id="home/1.5/over")),
                      (1201, outcome(1.90, True, outcome_id="home/1.5/under"))),
    },
}

# ชื่อตลาดที่รายงานใช้ คู่กับชื่อ field ใน JSON — เทสต์ sync ไล่ทีละคู่
MARKET_LABELS = (("แฮนดิแคป", "handicap"), ("สูง/ต่ำ", "total"))


class TestReportMatchesTheRealResult(unittest.TestCase):
    """
    รายงานวินิจฉัยกับ field ที่ส่งเข้า prompt ต้องมาจากการตัดสินใจครั้งเดียวกัน ทั้งสองตลาด

    เคยแยกกัน: รายงานดูแค่ธง mainLine ส่วน distill_book ดูทั้งการจับคู่และราคาด้วย
    ผลคือรายงานบอก "เจอเส้นหลักที่ 1058" แต่ JSON จริงกลับเป็น fallback เส้น 0.5
    เทสต์กลุ่มนี้ล็อกไม่ให้สองฝั่งแยกกันได้อีก และครอบทั้งแฮนดิแคปและสูง/ต่ำ
    """

    def distilled(self, markets):
        raw = {"pinnacle": book(markets)}
        return odds_data.distill_odds(raw, CATALOG)["books"]["pinnacle"]

    def scans(self, markets):
        return {"handicap": odds_data.scan_handicap_lines(book(markets), CATALOG),
                "total": odds_data.scan_total_lines(book(markets))}

    def printed_summaries(self, markets):
        """อ่านข้อความที่รายงานพิมพ์จริง แล้วคืน {ชื่อตลาด: บรรทัดสรุป}"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            test_odds_offline.report_main_lines({"pinnacle": book(markets)}, CATALOG)

        summaries, label = {}, None
        for line in buffer.getvalue().splitlines():
            for name, _ in MARKET_LABELS:
                if line.strip().startswith(f"{name}:"):
                    label = name
            if "สรุป:" in line and label is not None:
                summaries[label] = line.split("สรุป:")[1].strip()

        return summaries

    def test_every_case_agrees_between_scan_and_json(self):
        for name, markets in CONSISTENCY_CASES.items():
            scans, distilled = self.scans(markets), self.distilled(markets)

            for label, field in MARKET_LABELS:
                with self.subTest(case=name, market=label):
                    scan, chosen = scans[field], distilled[field]

                    if scan["main"] is None:
                        self.assertNotEqual(
                            (chosen or {}).get("source"), "mainline",
                            f"[{name}/{label}] สแกนไม่เจอเส้นหลัก แต่ JSON บอกว่าใช้เส้นหลัก")
                    else:
                        self.assertIsNotNone(chosen, f"[{name}/{label}] JSON ว่างเปล่า")
                        self.assertEqual(chosen["source"], "mainline")
                        self.assertEqual(chosen["line"], scan["main"]["line"])
                        self.assertEqual(chosen["market_ids"], scan["main"]["market_ids"])

    def test_the_verdict_in_the_json_matches_the_scan(self):
        for name, markets in CONSISTENCY_CASES.items():
            scans, distilled = self.scans(markets), self.distilled(markets)
            for label, field in MARKET_LABELS:
                with self.subTest(case=name, market=label):
                    self.assertEqual(distilled[f"{field}_verdict"], scans[field]["verdict"])

    def test_printed_report_says_the_same_thing_as_the_json(self):
        """อ่านข้อความที่รายงานพิมพ์จริง ๆ แล้วเทียบกับ JSON — กันข้อความหลุดจากผล"""
        for name, markets in CONSISTENCY_CASES.items():
            summaries, distilled = self.printed_summaries(markets), self.distilled(markets)

            for label, field in MARKET_LABELS:
                with self.subTest(case=name, market=label):
                    summary = summaries[label]
                    chosen = distilled[field] or {}

                    if chosen.get("source") == "mainline":
                        self.assertIn("ใช้เส้นหลัก", summary)
                        for market_id in chosen["market_ids"].values():
                            self.assertIn(market_id, summary,
                                          f"[{name}/{label}] รายงานกับ JSON ชี้คนละ market")
                    else:
                        self.assertIn("ไม่ใช้เส้นหลัก", summary,
                                      f"[{name}/{label}] JSON ไม่ได้ใช้เส้นหลัก แต่รายงานบอกว่าใช้")

    def test_the_report_reads_from_the_same_function_as_distill(self):
        """ไม่ใช่แค่ผลตรงกัน แต่ต้องเรียกฟังก์ชันตัดสินตัวเดียวกันจริง ๆ"""
        markets = dict(REAL_PINNACLE_MARKETS, **REAL_TOTAL_MARKETS)
        from_report = test_odds_offline.main_line_conclusions({"pinnacle": book(markets)},
                                                              CATALOG)["pinnacle"]

        self.assertEqual(from_report["handicap"]["main"],
                         odds_data.find_main_line(book(markets), CATALOG))
        self.assertEqual(from_report["total"]["main"],
                         odds_data.scan_total_lines(book(markets))["main"])
        self.assertEqual(from_report["handicap"]["main"]["market_ids"]["home"], "1058")
        self.assertEqual(from_report["total"]["main"]["line"], 3.5)

    def test_a_flagged_line_with_a_missing_price_is_not_reported_as_used(self):
        """เคสที่เคยหลอกตา: รายงานเห็นธงเลยขึ้น mainLine แต่ระบบใช้ไม่ได้เพราะราคาขาด"""
        markets = CONSISTENCY_CASES["ปักธงแต่ราคาขาดฝั่งหนึ่ง"]
        scan = odds_data.scan_handicap_lines(book(markets), CATALOG)

        self.assertEqual(scan["verdict"], "missing_price")
        self.assertIsNone(scan["main"])
        self.assertTrue(any(row["flag"] for row in scan["outcomes"]),
                        "รายงานต้องยังโชว์ว่ามีธงอยู่ เพื่อให้เห็นว่าทำไมถึงใช้ไม่ได้")
        self.assertEqual(self.distilled(markets)["handicap"]["source"], "fallback")


class TestTotalOutcomeIdParsing(unittest.TestCase):
    """แกะเลขเส้นจาก bookmakerOutcomeId — และกันตลาดประตูทีมเดียวไม่ให้หลุดเข้ามา"""

    def test_reads_the_line_and_the_side(self):
        self.assertEqual(odds_data.parse_total_outcome_id("2.5/over"), (2.5, "over"))
        self.assertEqual(odds_data.parse_total_outcome_id("3.5/under"), (3.5, "under"))
        self.assertEqual(odds_data.parse_total_outcome_id("3/over"), (3.0, "over"))

    def test_rejects_single_team_goal_markets(self):
        """"home/1.5/over" คือประตูของทีมเดียว ไม่ใช่สกอร์รวม — หลุดเข้ามาแล้วเส้นจะผิด"""
        for raw in ("home/1.5/over", "away/1.5/under", "home/2.5/over"):
            self.assertIsNone(odds_data.parse_total_outcome_id(raw), f"{raw} ต้องไม่เข้าเกณฑ์")

    def test_rejects_anything_that_is_not_a_full_match(self):
        for raw in ("-1.75/home", "2.5/over/1st", "over", "2.5", "", None, "x/over"):
            self.assertIsNone(odds_data.parse_total_outcome_id(raw))

    def test_is_case_insensitive_on_the_side(self):
        self.assertEqual(odds_data.parse_total_outcome_id("3.5/OVER"), (3.5, "over"))

    def test_reads_the_id_from_the_same_dict_as_the_price(self):
        entry = outcome(2.02, True, outcome_id="3.5/over")
        self.assertEqual(odds_data.outcome_bookmaker_id(entry), "3.5/over")
        self.assertEqual(odds_data.main_line_player(entry)["price"], 2.02)


class TestRealTotalSample(unittest.TestCase):
    """เคสหลักของสูง/ต่ำ: ข้อมูลจริงที่เส้น 2.5 ไม่ใช่เส้นหลัก ต้องได้ 3.5 ไม่ fallback"""

    def test_finds_three_point_five_not_the_fixed_two_point_five(self):
        scan = odds_data.scan_total_lines(book(REAL_TOTAL_MARKETS))

        self.assertEqual(scan["verdict"], "mainline")
        self.assertEqual(scan["main"]["line"], 3.5)
        self.assertEqual(scan["main"]["over"], 2.02)
        self.assertEqual(scan["main"]["under"], 1.83)
        self.assertEqual(scan["main"]["market_ids"], {"over": "1012", "under": "1013"})

    def test_needs_no_market_catalog_at_all(self):
        """scan_total_lines ไม่รับ catalog เลย เลขเส้นมาจาก bookmakerOutcomeId ล้วน ๆ"""
        raw = {"pinnacle": book(REAL_TOTAL_MARKETS)}
        book_data = odds_data.distill_odds(raw, {})["books"]["pinnacle"]

        self.assertEqual(book_data["total_verdict"], "mainline")
        self.assertEqual(book_data["total"]["line"], 3.5)
        self.assertEqual(book_data["total"]["source"], "mainline")

    def test_single_team_goal_markets_never_reach_the_result(self):
        markets = dict(REAL_TOTAL_MARKETS, **{
            "1200": group((1200, outcome(1.90, True, outcome_id="home/1.5/over")),
                          (1201, outcome(1.90, True, outcome_id="home/1.5/under"))),
        })
        scan = odds_data.scan_total_lines(book(markets))

        self.assertEqual([row["market_id"] for row in scan["outcomes"]],
                         ["1010", "1011", "1012", "1013"])
        self.assertEqual(scan["main"]["line"], 3.5)

    def test_reaches_the_prompt_with_thai_prices(self):
        raw = {"pinnacle": book(REAL_TOTAL_MARKETS)}
        summary = analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, CATALOG))

        self.assertEqual(summary["total"]["line"], "3.5")
        self.assertEqual(summary["total"]["source"], "mainline")
        # 2.02 -> -0.98 และ 1.83 -> 0.83 ตามตรรกะแปลงราคาเดิมของแฮนดิแคป
        self.assertEqual(summary["total"]["prices"], {"over": -0.98, "under": 0.83})


class TestTotalFallback(unittest.TestCase):
    """หา mainLine ของสูง/ต่ำไม่เจอ ต้องถอยไปเส้น 2.5 เดิม (fail-safe เดียวกับแฮนดิแคป)"""

    def distilled(self, markets):
        raw = {"pinnacle": book(markets)}
        return odds_data.distill_odds(raw, CATALOG)["books"]["pinnacle"]

    def test_no_flag_anywhere_falls_back_to_two_point_five(self):
        book_data = self.distilled({
            "1010": group((1010, outcome(1.95, outcome_id="2.5/over")),
                          (1011, outcome(1.87, outcome_id="2.5/under"))),
            "1012": group((1012, outcome(2.30, outcome_id="3.5/over")),
                          (1013, outcome(1.62, outcome_id="3.5/under"))),
        })

        self.assertEqual(book_data["total_verdict"], "not_flagged")
        self.assertEqual(book_data["total"]["source"], "fallback")
        self.assertEqual(book_data["total"]["line"], 2.5)
        self.assertEqual((book_data["total"]["over"], book_data["total"]["under"]), (1.95, 1.87))

    def test_more_than_one_flagged_line_falls_back(self):
        book_data = self.distilled({
            "1010": group((1010, outcome(1.95, True, outcome_id="2.5/over")),
                          (1011, outcome(1.87, True, outcome_id="2.5/under"))),
            "1012": group((1012, outcome(2.30, True, outcome_id="3.5/over")),
                          (1013, outcome(1.62, True, outcome_id="3.5/under"))),
        })

        self.assertEqual(book_data["total_verdict"], "ambiguous")
        self.assertEqual(book_data["total"]["source"], "fallback")

    def test_a_flagged_line_missing_a_price_falls_back(self):
        book_data = self.distilled({
            "1010": group((1010, outcome(1.95, False, outcome_id="2.5/over")),
                          (1011, outcome(1.87, False, outcome_id="2.5/under"))),
            "1012": group((1012, outcome(2.30, True, outcome_id="3.5/over")),
                          (1013, outcome(None, True, outcome_id="3.5/under"))),
        })

        self.assertEqual(book_data["total_verdict"], "missing_price")
        self.assertEqual(book_data["total"]["line"], 2.5)

    def test_only_one_side_of_a_line_cannot_pair(self):
        scan = odds_data.scan_total_lines(book({
            "1012": group((1012, outcome(2.02, True, outcome_id="3.5/over"))),
        }))

        self.assertEqual(scan["verdict"], "no_pair")
        self.assertIsNone(scan["main"])

    def test_no_totals_market_at_all(self):
        scan = odds_data.scan_total_lines(book({
            "101": group((101, outcome(1.80)), (103, outcome(4.20))),
        }))

        self.assertEqual(scan["verdict"], "no_market")
        self.assertIsNone(scan["main"])

    def test_nothing_usable_leaves_the_field_empty(self):
        book_data = self.distilled({"101": group((101, outcome(1.80)), (103, outcome(4.20)))})

        self.assertIsNone(book_data["total"])
        self.assertEqual(book_data["total_verdict"], "no_market")

    def test_the_prompt_payload_is_empty_when_there_is_no_total(self):
        raw = {"pinnacle": book({"101": group((101, outcome(1.80)), (103, outcome(4.20)))})}
        summary = analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, CATALOG))

        self.assertIsNone(summary["total"])


class TestBothMarketsTogether(unittest.TestCase):
    """แฮนดิแคปกับสูง/ต่ำต้องหากันคนละทาง ไม่รบกวนกัน"""

    def setUp(self):
        self.markets = dict(REAL_PINNACLE_MARKETS, **REAL_TOTAL_MARKETS)
        raw = {"pinnacle": book(self.markets)}
        self.book = odds_data.distill_odds(raw, CATALOG)["books"]["pinnacle"]

    def test_each_market_finds_its_own_main_line(self):
        self.assertEqual(self.book["handicap"]["handicap"], -1.75)
        self.assertEqual(self.book["total"]["line"], 3.5)
        self.assertEqual(self.book["handicap_verdict"], "mainline")
        self.assertEqual(self.book["total_verdict"], "mainline")

    def test_handicap_outcome_ids_do_not_leak_into_totals(self):
        """"-1.75/home" ต้องไม่ถูกอ่านเป็นเส้นสูง/ต่ำ"""
        lines = {row["line"] for row in odds_data.scan_total_lines(book(self.markets))["outcomes"]}
        self.assertEqual(lines, {2.5, 3.5})

    def test_both_reach_the_prompt(self):
        raw = {"pinnacle": book(self.markets)}
        summary = analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, CATALOG))

        self.assertEqual(summary["handicap"]["line"], "1.75")
        self.assertEqual(summary["total"]["line"], "3.5")


class TestFallbackCatalogCoverage(unittest.TestCase):


    """สารบัญสำรองต้องครอบช่วงที่ยืนยันแล้ว และต้องไม่เดาเลยขอบออกไป"""

    def test_covers_every_confirmed_market_id(self):
        confirmed = {"1058": -1.75, "1068": -0.5, "1070": -0.25,
                     "1072": 0.0, "1074": 0.25, "1076": 0.5}
        for market_id, handicap in confirmed.items():
            self.assertEqual(odds_data.FALLBACK_AH_CATALOG.get(market_id), handicap,
                             f"market {market_id} ต้องเป็นเส้น {handicap}")

    def test_holds_home_side_ids_only(self):
        """รูปร่างเดียวกับสารบัญจริง — ฝั่งเยือนหาจาก id + 1 ไม่ต้องเก็บไว้"""
        for market_id in odds_data.FALLBACK_AH_CATALOG:
            self.assertEqual(int(market_id) % 2, 0,
                             f"{market_id} เป็นเลขคี่ = ฝั่งเยือน ไม่ควรอยู่ในสารบัญ")
            self.assertTrue(odds_data.is_ah_home_id(market_id, odds_data.FALLBACK_AH_CATALOG))

    def test_does_not_guess_past_the_confirmed_range(self):
        """เดาเลยขอบแล้วไปชนกับ id ของตลาดสูง-ต่ำ จะรายงานเส้นสูง-ต่ำเป็นแฮนดิแคป"""
        ids = [int(market_id) for market_id in odds_data.FALLBACK_AH_CATALOG]

        self.assertEqual(min(ids), 1058)   # -1.75 คือขอบล่างที่ยืนยันแล้ว
        self.assertEqual(max(ids), 1076)   # +0.5 คือขอบบนที่ยืนยันแล้ว
        self.assertNotIn(str(odds_data.MARKET_OU25_OVER), odds_data.FALLBACK_AH_CATALOG)
        self.assertNotIn(str(odds_data.MARKET_OU25_UNDER), odds_data.FALLBACK_AH_CATALOG)


class TestMainLineFlagIsStrict(unittest.TestCase):
    """
    ธงต้องอ่านจาก players ของ outcome ตัวนั้นเอง และไม่มีธง = ไม่ใช่เส้นหลัก

    เคส singbet/singbet-b ที่ทุกเส้นขึ้นเป็นเส้นหลักพร้อมกันหมด (เป็นไปไม่ได้จริง)
    มาจากการอ่านธงแบบไล่เดาหลายชั้น เทสต์กลุ่มนี้ล็อกไม่ให้กลับมาอีก
    """

    def test_missing_flag_defaults_to_not_main_line(self):
        self.assertIs(odds_data.main_line_flag(outcome(1.90)), False)
        self.assertIsNone(odds_data.read_main_line_flag(outcome(1.90)))

    def test_a_whole_book_without_flags_finds_no_main_line(self):
        data = book({str(1058 + n * 2): group((1058 + n * 2, outcome(1.90)),
                                              (1059 + n * 2, outcome(1.92)))
                     for n in range(5)})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_a_sibling_flag_does_not_leak_onto_the_others(self):
        """เส้นเดียวถูกปักธง เส้นอื่นในกลุ่มอื่นต้องไม่ติดธงตามไปด้วย"""
        data = book({
            "1058": group((1058, outcome(1.91, True)), (1059, outcome(1.97, True))),
            "1068": group((1068, outcome(1.30)), (1069, outcome(3.45))),
            "1072": group((1072, outcome(1.15)), (1073, outcome(5.20))),
        })
        scan = odds_data.scan_handicap_lines(data, CATALOG)
        flagged = [row["market_id"] for row in scan["outcomes"] if row["flag"]]

        self.assertEqual(sorted(flagged), ["1058", "1059"])

    def test_a_flag_on_the_group_above_is_not_borrowed(self):
        """ธงที่ระดับกลุ่มไม่ใช่ path จริง ห้ามเอามาตัดสินแทน players"""
        data = {"markets": {"1058": {
            "mainLine": True,
            "outcomes": {
                "1058": outcome(1.91, False),
                "1059": outcome(1.97, False),
            },
        }}}
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_a_false_flag_stays_false(self):
        self.assertIs(odds_data.main_line_flag(outcome(1.90, False)), False)
        self.assertIs(odds_data.read_main_line_flag(outcome(1.90, False)), False)


class TestFindMainLine(unittest.TestCase):
    """สแกน AH ทุกเส้นที่เจ้ามือเสนอ แล้วหยิบเส้นที่ mainLine=true ทั้งสองฝั่ง"""

    def test_picks_the_line_flagged_on_both_sides(self):
        data = book({
            "101": group((101, outcome(1.80)), (102, outcome(3.50)), (103, outcome(4.20))),
            "1068": group((1068, outcome(1.95, False)), (1069, outcome(1.90, False))),
            "1080": group((1080, outcome(2.02, True)), (1081, outcome(1.84, True))),
            "1090": group((1090, outcome(2.30, False)), (1091, outcome(1.62, False))),
        })
        found = odds_data.find_main_line(data, CATALOG)

        self.assertEqual(found["handicap"], -0.75)
        self.assertEqual(found["line"], 0.75)
        self.assertEqual(found["source"], "mainline")
        self.assertEqual(found["home"], 2.02)
        self.assertEqual(found["away"], 1.84)
        self.assertEqual(found["market_ids"], {"home": "1080", "away": "1081"})

    def test_ignores_a_line_flagged_on_one_side_only(self):
        data = book({"1080": group((1080, outcome(2.02, True)), (1081, outcome(1.84, False)))})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_returns_none_when_no_outcome_carries_the_flag(self):
        data = book({"1068": group((1068, outcome(1.95)), (1069, outcome(1.90)))})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_skips_a_main_line_that_is_missing_a_price(self):
        data = book({"1080": group((1080, outcome(2.02, True)), (1081, outcome(None, True)))})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_ignores_non_handicap_markets_even_when_flagged(self):
        data = book({"101": group((101, outcome(1.80, True)), (102, outcome(3.50, True)))})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_ignores_market_ids_missing_from_the_catalog(self):
        data = book({"9998": group((9998, outcome(1.95, True)), (9999, outcome(1.90, True)))})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_reads_the_line_number_from_the_home_side_market(self):
        """เลขเส้นต้องอ่านจาก market id ฝั่งเหย้า (ตัวน้อยกว่า) ไม่ใช่ฝั่งเยือน"""
        data = book({"1074": group((1075, outcome(1.90, True)), (1074, outcome(1.94, True)))})
        found = odds_data.find_main_line(data, CATALOG)

        self.assertEqual(found["handicap"], 0.25)          # ค่าของ 1074 ไม่ใช่ของ 1075
        self.assertEqual(found["home"], 1.94)
        self.assertEqual(found["market_ids"]["home"], "1074")

    def test_zero_line_is_a_valid_main_line(self):
        data = book({"1072": group((1072, outcome(1.98, True)), (1073, outcome(1.86, True)))})
        found = odds_data.find_main_line(data, CATALOG)

        self.assertEqual(found["handicap"], 0.0)
        self.assertEqual(found["source"], "mainline")

    def test_refuses_to_pick_when_more_than_one_line_is_flagged(self):
        """เจ้ามือมีเส้นหลักได้เส้นเดียว เจอหลายเส้นแปลว่าธงเชื่อไม่ได้ ต้องถอยไปเส้นสำรอง"""
        data = book({
            "1090": group((1090, outcome(2.30, True)), (1091, outcome(1.62, True))),
            "1080": group((1080, outcome(2.02, True)), (1081, outcome(1.84, True))),
        })
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_a_book_that_flags_every_line_falls_back(self):
        """เคส singbet ที่รายงานมา: ปักธงทุกเส้น ต้องไม่หยิบเส้นไหนมาพูดว่าเป็นเส้นหลัก"""
        raw = {"singbet": book({
            "1058": group((1058, outcome(1.90, True)), (1059, outcome(1.90, True))),
            "1068": group((1068, outcome(1.30, True)), (1069, outcome(3.45, True))),
            "1072": group((1072, outcome(1.15, True)), (1073, outcome(5.20, True))),
        })}
        handicap = odds_data.distill_odds(raw, CATALOG)["books"]["singbet"]["handicap"]

        self.assertEqual(handicap["source"], "fallback")
        self.assertEqual(handicap["handicap"], -0.5)   # ถอยไปเส้นตายตัวเดิม

    def test_reads_the_flag_from_string_and_number_forms(self):
        for raw in (True, "true", "True", 1):
            data = book({"1080": group((1080, outcome(2.02, raw)), (1081, outcome(1.84, raw)))})
            self.assertIsNotNone(odds_data.find_main_line(data, CATALOG),
                                 f"ควรอ่าน mainLine={raw!r} เป็นจริงได้")

    def test_reads_the_flag_from_the_player_level(self):
        data = book({"1080": {"outcomes": {
            "1080": {"players": {"0": {"price": 2.02, "mainLine": True}}},
            "1081": {"players": {"0": {"price": 1.84, "mainLine": True}}},
        }}})
        found = odds_data.find_main_line(data, CATALOG)

        self.assertIsNotNone(found)
        self.assertEqual(found["handicap"], -0.75)

    def test_a_flag_at_the_outcome_level_is_ignored(self):
        """ระดับ outcome ไม่ใช่ path จริง อ่านจากตรงนั้นเคยทำให้ได้ผลลัพธ์มั่ว"""
        data = book({"1080": {"outcomes": {
            "1080": {"mainLine": True, "players": {"0": {"price": 2.02}}},
            "1081": {"mainLine": True, "players": {"0": {"price": 1.84}}},
        }}})
        self.assertIsNone(odds_data.find_main_line(data, CATALOG))

    def test_collects_changed_at_stamps_of_the_chosen_line(self):
        data = book({"1080": group((1080, outcome(2.02, True, "2026-08-26T10:00:00Z")),
                                   (1081, outcome(1.84, True, "2026-08-26T11:00:00Z")))})
        stamps = []
        odds_data.find_main_line(data, CATALOG, stamps)

        self.assertEqual(sorted(stamps),
                         ["2026-08-26T10:00:00Z", "2026-08-26T11:00:00Z"])

    def test_empty_catalog_finds_nothing(self):
        data = book({"1080": group((1080, outcome(2.02, True)), (1081, outcome(1.84, True)))})
        self.assertIsNone(odds_data.find_main_line(data, {}))
        self.assertIsNone(odds_data.find_main_line(data, None))


class TestFallbackHandicap(unittest.TestCase):
    """หา mainLine ไม่เจอ ต้องถอยกลับไปเส้นตายตัวเดิมได้ (fail-safe)"""

    def test_prefers_the_half_ball_line(self):
        distilled = {"ah_-0.5": {"home": 1.95, "away": 1.90}, "ah_0": {"home": 1.70, "away": 2.15}}
        found = odds_data.fallback_handicap(distilled)

        self.assertEqual(found["source"], "fallback")
        self.assertEqual(found["handicap"], -0.5)
        self.assertEqual((found["home"], found["away"]), (1.95, 1.90))
        self.assertEqual(found["market_ids"], {"home": "1068", "away": "1069"})

    def test_uses_the_level_line_when_the_half_ball_line_is_incomplete(self):
        distilled = {"ah_-0.5": {"home": 1.95, "away": None}, "ah_0": {"home": 1.70, "away": 2.15}}
        found = odds_data.fallback_handicap(distilled)

        self.assertEqual(found["handicap"], 0.0)
        self.assertEqual((found["home"], found["away"]), (1.70, 2.15))
        self.assertEqual(found["market_ids"], {"home": "1072", "away": "1073"})

    def test_returns_none_when_neither_fixed_line_is_priced(self):
        distilled = {"ah_-0.5": {"home": None, "away": None}, "ah_0": {"home": None, "away": None}}
        self.assertIsNone(odds_data.fallback_handicap(distilled))


class TestDistillWithMainLine(unittest.TestCase):
    """distill_odds ต้องแปะช่อง handicap ให้ทุกเจ้า และบอกที่มาของเส้นไว้ใน notes"""

    def raw(self, markets):
        return {"pinnacle": book(markets)}

    def test_main_line_wins_over_the_fixed_lines(self):
        raw = self.raw({
            "1068": group((1068, outcome(1.95, False)), (1069, outcome(1.90, False))),
            "1080": group((1080, outcome(2.02, True)), (1081, outcome(1.84, True))),
        })
        result = odds_data.distill_odds(raw, CATALOG)
        handicap = result["books"]["pinnacle"]["handicap"]

        self.assertEqual(handicap["source"], "mainline")
        self.assertEqual(handicap["handicap"], -0.75)
        # เส้นตายตัวยังถูกกลั่นไว้เหมือนเดิม ไม่ได้หายไปไหน
        self.assertEqual(result["books"]["pinnacle"]["ah_-0.5"], {"home": 1.95, "away": 1.90})

    def test_falls_back_and_says_so_in_notes(self):
        raw = self.raw({"1068": group((1068, outcome(1.95)), (1069, outcome(1.90)))})
        result = odds_data.distill_odds(raw, CATALOG)

        self.assertEqual(result["books"]["pinnacle"]["handicap"]["source"], "fallback")
        self.assertTrue(any("ไม่ได้ใช้เส้นหลัก" in note for note in result["notes"]))
        # เหตุผลใน note ต้องเป็นตัวเดียวกับที่รายงานวินิจฉัยพิมพ์
        verdict = result["books"]["pinnacle"]["handicap_verdict"]
        self.assertTrue(any(odds_data.SCAN_VERDICTS[verdict] in note for note in result["notes"]))

    def test_works_without_a_catalog_at_all(self):
        raw = self.raw({"1068": group((1068, outcome(1.95, True)), (1069, outcome(1.90, True)))})
        result = odds_data.distill_odds(raw)

        self.assertEqual(result["books"]["pinnacle"]["handicap"]["source"], "fallback")

    def test_notes_when_a_book_has_no_usable_handicap(self):
        raw = self.raw({"101": group((101, outcome(1.80)), (103, outcome(4.20)))})
        result = odds_data.distill_odds(raw, CATALOG)

        self.assertIsNone(result["books"]["pinnacle"]["handicap"])
        self.assertTrue(any("ไม่มีเส้นแฮนดิแคป" in note for note in result["notes"]))
        self.assertEqual(result["books"]["pinnacle"]["handicap_verdict"], "no_market")


class TestHandicapLineLabel(unittest.TestCase):
    """เทียบเลขเส้นกับ HANDICAP_LINE_NAMES — ไม่ตรงให้บอกตัวเลขเฉย ๆ ห้ามเดาชื่อ"""

    def test_known_lines_get_their_thai_name(self):
        self.assertEqual(analyze.handicap_line_label(-0.5), "ครึ่งลูก [0.5]")
        self.assertEqual(analyze.handicap_line_label(0.0), "ต่อเสมอ [0]")
        self.assertEqual(analyze.handicap_line_label(-1.0), "ลูกเดียว [1]")
        self.assertEqual(analyze.handicap_line_label(-1.5), "ลูกครึ่ง [1.5]")

    def test_quarter_lines_map_to_the_combined_names(self):
        self.assertEqual(analyze.handicap_line_label(-0.25), "เสมอควบครึ่ง [0-0.5]")
        self.assertEqual(analyze.handicap_line_label(-0.75), "ครึ่งควบลูก [0.5-1]")
        self.assertEqual(analyze.handicap_line_label(-1.25), "ลูกควบลูกครึ่ง [1-1.5]")

    def test_the_sign_does_not_change_the_name(self):
        self.assertEqual(analyze.handicap_line_label(0.75), analyze.handicap_line_label(-0.75))

    def test_unknown_lines_show_the_number_only(self):
        self.assertEqual(analyze.handicap_line_label(-3.0), "[3]")
        self.assertEqual(analyze.handicap_line_label(-2.75), "[2.75]")
        self.assertEqual(analyze.handicap_line_label(-0.1), "[0.1]")

    def test_non_numeric_input_gives_nothing(self):
        for value in (None, "0.5", True):
            self.assertIsNone(analyze.handicap_line_label(value))

    def test_line_number_formatting(self):
        self.assertEqual(analyze.format_line_number(1.0), "1")
        self.assertEqual(analyze.format_line_number(0.5), "0.5")
        self.assertEqual(analyze.format_line_number(0.25), "0.25")
        self.assertEqual(analyze.format_line_number(0), "0")

    def test_line_giver_reads_the_sign(self):
        self.assertEqual(analyze.handicap_line_giver(-0.75), "home")
        self.assertEqual(analyze.handicap_line_giver(0.75), "away")
        self.assertEqual(analyze.handicap_line_giver(0.0), "level")
        self.assertIsNone(analyze.handicap_line_giver(None))


class TestHandicapFavourite(unittest.TestCase):
    """handicap_favourite ต้องคิดจากเส้น mainLine ที่เจอจริงก่อน แล้วค่อยถอยไปเส้นเดิม"""

    def test_uses_the_main_line_over_the_fixed_lines(self):
        distilled = {
            "handicap": {"home": 1.98, "away": 1.88, "handicap": 0.75, "source": "mainline"},
            "ah_-0.5": {"home": 1.70, "away": 2.20},   # เส้นเดิมชี้คนละฝั่ง ต้องไม่ถูกใช้
            "ah_0": {"home": 1.50, "away": 2.60},
        }
        self.assertEqual(analyze.handicap_favourite(distilled), "away")

    def test_a_real_main_line_is_read_from_the_line_not_the_price(self):
        """เจ้าบ้านต่อลูกเดียว ราคา 1.98/1.88 — เทียบราคาดิบจะได้ away ซึ่งกลับข้างกับความจริง"""
        distilled = {"handicap": {"home": 1.98, "away": 1.88, "handicap": -1.0,
                                  "source": "mainline"}}
        self.assertEqual(analyze.handicap_favourite(distilled), "home")

    def test_a_zero_main_line_still_falls_back_to_comparing_prices(self):
        distilled = {"handicap": {"home": 1.70, "away": 2.20, "handicap": 0.0,
                                  "source": "mainline"}}
        self.assertEqual(analyze.handicap_favourite(distilled), "home")

    def test_a_fallback_line_still_compares_prices_like_before(self):
        """เส้นตายตัวไม่การันตีว่าเป็นเส้นที่ตลาดใช้ เครื่องหมายของมันจึงเชื่อไม่ได้"""
        distilled = {"handicap": {"home": 2.20, "away": 1.70, "handicap": -0.5,
                                  "source": "fallback"}}
        self.assertEqual(analyze.handicap_favourite(distilled), "away")

    def test_falls_back_to_the_fixed_lines_when_there_is_no_main_line(self):
        distilled = {"handicap": None, "ah_-0.5": {"home": 1.70, "away": 2.20},
                     "ah_0": {"home": 1.50, "away": 2.60}}
        self.assertEqual(analyze.handicap_favourite(distilled), "home")

    def test_close_prices_read_as_level(self):
        distilled = {"handicap": {"home": 1.92, "away": 1.90, "handicap": 0.0,
                                  "source": "mainline"}}
        self.assertEqual(analyze.handicap_favourite(distilled), "level")

    def test_no_comparable_price_gives_nothing(self):
        self.assertIsNone(analyze.handicap_favourite({"handicap": None}))
        self.assertIsNone(analyze.handicap_favourite(
            {"handicap": {"home": 1.95, "away": None, "source": "mainline"}}))


class TestSummarizeOddsForPrompt(unittest.TestCase):
    """ข้อมูลที่ส่งเข้า prompt ต้องมีเส้นหลักเส้นเดียว พร้อมเลขเส้นจริงและที่มา"""

    def summary(self, markets, catalog=CATALOG):
        raw = {"pinnacle": book(markets)}
        return analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, catalog))

    def test_main_line_reaches_the_prompt_with_thai_prices(self):
        summary = self.summary({
            "101": group((101, outcome(1.80)), (102, outcome(3.50)), (103, outcome(4.20))),
            "1080": group((1080, outcome(1.68, True)), (1081, outcome(2.30, True))),
        })
        handicap = summary["handicap"]

        self.assertEqual(handicap["line"], "0.75")
        self.assertEqual(handicap["line_label"], "ครึ่งควบลูก [0.5-1]")
        self.assertEqual(handicap["giver"], "home")
        self.assertEqual(handicap["source"], "mainline")
        # 1.68 -> 0.68 (เลขบวก) และ 2.30 -> -0.77 (เลขลบ) ตามตรรกะแปลงราคาเดิม
        self.assertEqual(handicap["prices"], {"home": 0.68, "away": -0.77})
        self.assertEqual(summary["handicap_favourite"], "home")  # เจ้าบ้านเป็นคนต่อ 0.75
        self.assertEqual(summary["market_favourite"], "home")

    def test_fallback_is_labelled_as_fallback(self):
        summary = self.summary({"1068": group((1068, outcome(1.95)), (1069, outcome(1.90)))})
        handicap = summary["handicap"]

        self.assertEqual(handicap["source"], "fallback")
        self.assertEqual(handicap["line_label"], "ครึ่งลูก [0.5]")

    def test_unknown_line_reaches_the_prompt_as_a_bare_number(self):
        catalog = dict(CATALOG, **{"1200": -3.0, "1201": 3.0})
        summary = self.summary(
            {"1200": group((1200, outcome(1.90, True)), (1201, outcome(1.94, True)))}, catalog)

        self.assertEqual(summary["handicap"]["line_label"], "[3]")
        self.assertEqual(summary["handicap"]["line"], "3")

    def test_no_handicap_at_all_leaves_the_field_empty(self):
        summary = self.summary({"101": group((101, outcome(1.80)), (103, outcome(4.20)))})

        self.assertIsNone(summary["handicap"])
        self.assertIsNone(summary["handicap_favourite"])
        self.assertEqual(summary["1x2"]["home"], 1.80)

    def test_book_without_any_price_is_not_chosen(self):
        raw = {"pinnacle": book({"1080": group((1080, outcome(None, True)),
                                               (1081, outcome(None, True)))})}
        self.assertIsNone(analyze.summarize_odds_for_prompt(odds_data.distill_odds(raw, CATALOG)))

    def test_prompt_payload_no_longer_carries_the_fixed_lines(self):
        summary = self.summary({"1068": group((1068, outcome(1.95)), (1069, outcome(1.90)))})

        self.assertNotIn("ah_-0.5", summary)
        self.assertNotIn("ah_0", summary)
        self.assertNotIn("handicap_lines", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
