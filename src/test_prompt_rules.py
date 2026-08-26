"""
Unit test ของกฎใน prompts/analyst_prompt.txt — อ่านไฟล์ prompt ล้วน ๆ ไม่เรียก Claude API เลย

เทสต์ชุดนี้ล็อกกฎที่ "ผิดแล้วบทวิเคราะห์จะพูดผิด" ไม่ได้ล็อกถ้อยคำเป๊ะ ๆ
แก้สำนวนได้ตามใจ แต่กฎข้างล่างนี้ต้องยังอยู่:
  - "ไทยรอง" ต้องไม่ใช่คำแทน "ฝั่งรอง" ทั่วไป (ผูกกับทีมไทยเท่านั้น)
  - คำที่มีชื่อชาติ/ชื่อทีมอยู่ในตัว ต้องมีกฎกำกับก่อนใช้
  - ห้ามพูดว่า "ตลาดตั้งเส้น" ไม่ว่าเส้นจะมาจาก mainline หรือ fallback
  - ยกราคาต่อรองต้องยกทั้งสองฝั่ง

วิธีรัน:
    python3 src/test_prompt_rules.py
    python3 src/test_prompt_rules.py -v
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import PROMPT_PATH

PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

# หัวข้อในไฟล์ prompt คือบรรทัดที่ไม่ได้ขึ้นต้นด้วย "-" หรือช่องว่าง และไม่ใช่บรรทัดว่าง
SECTION_HEADING = re.compile(r"^(?![-\s])(.+)$", re.MULTILINE)

# ชื่อสโมสร/ทีมจริงที่ไม่ควรไปโผล่ใน "นิยามของคำศัพท์" เพราะเสี่ยงถูกลอกไปทั้งดุ้น
REAL_TEAM_NAMES = ("Liverpool", "Barcelona", "Arsenal", "Chelsea", "Real Madrid",
                   "Manchester City", "Manchester United")

# คำที่มีชื่อชาติอยู่ในตัว — ห้ามอยู่ในคลังคำทั่วไป ต้องอยู่ในหัวข้อที่มีกฎกำกับ
NATION_BOUND_TERMS = ("ไทยรอง", "ไทยต่อ")


def section(title):
    """ตัดเนื้อของหัวข้อที่ระบุออกมา (ตั้งแต่บรรทัดหัวข้อจนถึงหัวข้อถัดไป)"""
    starts = [(match.start(), match.group(1)) for match in SECTION_HEADING.finditer(PROMPT)]

    for index, (position, heading) in enumerate(starts):
        if heading.startswith(title):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(PROMPT)
            return PROMPT[position:end]

    raise AssertionError(f"ไม่พบหัวข้อ '{title}' ในไฟล์ prompt")


class TestVocabularySection(unittest.TestCase):
    """คลังคำศัพท์ทั่วไปต้องมีแต่คำที่ใช้ได้กับทุกคู่"""

    def setUp(self):
        self.vocabulary = section("คลังคำศัพท์ราคา")

    def test_the_general_vocabulary_has_no_nation_bound_terms(self):
        for term in NATION_BOUND_TERMS:
            self.assertNotIn(term, self.vocabulary,
                             f"'{term}' ผูกกับทีมไทย ห้ามอยู่ในคลังคำทั่วไป")

    def test_it_offers_a_neutral_word_for_the_underdog(self):
        self.assertIn("ฝั่งรอง", self.vocabulary)
        self.assertIn("ทีมรอง", self.vocabulary)

    def test_the_neutral_word_is_marked_as_the_default(self):
        self.assertIn("ค่าเริ่มต้น", self.vocabulary + section("คำที่ผูกกับชาติ"))

    def test_term_definitions_do_not_hardcode_a_real_club(self):
        """นิยามของคำห้ามมีชื่อสโมสรจริง เดี๋ยวถูกลอกไปใช้กับคู่ที่ไม่เกี่ยว"""
        for name in REAL_TEAM_NAMES:
            self.assertNotIn(name, self.vocabulary,
                             f"นิยามคำศัพท์ไม่ควรฮาร์ดโค้ดชื่อทีม '{name}'")

    def test_it_warns_that_names_and_numbers_are_only_examples(self):
        self.assertIn("ชื่อทีม", self.vocabulary)
        self.assertRegex(self.vocabulary, r"ห้ามลอกไปใช้")


class TestNationBoundSection(unittest.TestCase):
    """หัวข้อกฎของคำที่ผูกกับชาติ/ชื่อทีม — ต้องมีทั้งกฎรวมและเคส "ไทยรอง" """

    def setUp(self):
        self.rules = section("คำที่ผูกกับชาติ")

    def test_the_section_exists_and_states_a_general_rule(self):
        self.assertIn("กฎรวม", self.rules)
        self.assertIn("ชื่อชาติ", self.rules)
        self.assertIn("ชื่อทีม", self.rules)

    def test_thai_underdog_term_is_defined_as_team_specific(self):
        self.assertIn("ไทยรอง", self.rules)
        self.assertIn("ทีมไทยเป็นฝ่ายรอง", self.rules)

    def test_it_requires_both_conditions_before_use(self):
        self.assertIn("มีทีมไทยลงเล่นจริง", self.rules)
        self.assertIn("handicap_favourite", self.rules)

    def test_it_names_the_safe_fallback_word(self):
        self.assertIn("ให้ใช้ \"ฝั่งรอง\" แทนเสมอ", self.rules)

    def test_it_spells_out_both_failure_modes(self):
        self.assertIn("ไม่มีทีมไทย", self.rules)      # ใช้กับคู่ที่ไม่มีทีมไทย = ไม่มีความหมาย
        self.assertIn("เป็นฝ่ายต่อ", self.rules)      # ทีมไทยเป็นต่อ = ผิดทันที


class TestNoMarketIntentClaims(unittest.TestCase):
    """ห้ามพูดว่า "ตลาดตั้งเส้น" — เรารู้แค่ราคาที่เจ้ามือเปิด ไม่รู้เจตนาตลาด"""

    def test_every_mention_of_setting_a_line_is_a_prohibition(self):
        for line in PROMPT.splitlines():
            if "ตลาดตั้ง" in line or "ตลาดจับ" in line:
                self.assertIn("ห้าม", line,
                              f"บรรทัดนี้พูดถึง 'ตลาดตั้งเส้น' โดยไม่ได้ห้าม: {line.strip()}")

    def test_the_ban_covers_both_mainline_and_fallback(self):
        odds = section("ราคาต่อรอง")
        self.assertIn("ไม่ว่า source จะเป็น mainline หรือ fallback", odds)

    def test_fallback_is_not_described_as_the_market_line(self):
        odds = section("ราคาต่อรอง")
        self.assertIn("ไม่ได้ยืนยันว่าเป็นเส้นที่ตลาดใช้อยู่", odds)


class TestBothSidesRule(unittest.TestCase):
    """ยกราคาต่อรองต้องยกทั้งสองฝั่ง ไม่งั้นคนอ่านไม่เห็นว่าอีกฝั่งจ่ายเท่าไหร่"""

    def setUp(self):
        self.odds = section("ราคาต่อรอง")

    def test_it_demands_both_sides(self):
        self.assertIn("ทั้งสองฝั่ง", self.odds)
        self.assertIn("ห้ามยกฝั่งเดียว", self.odds)

    def test_the_old_pick_one_side_rule_is_gone(self):
        self.assertNotIn("ไม่ต้องยกมาทั้งคู่", self.odds)

    def test_the_worked_example_shows_two_prices(self):
        """ตัวอย่างที่ขึ้นต้นว่า "เขียนว่า" คือแบบที่ให้ลอกโครง ต้องมีราคาครบสองฝั่ง"""
        example = next(line for line in self.odds.splitlines()
                       if line.strip().startswith("เขียนว่า") and "ราคาต่อ" in line)
        numbers = re.findall(r"-?\d+(?:\.\d+)?", example)

        self.assertEqual(len(numbers), 3,
                         f"ตัวอย่างต้องมีเลขเส้นหนึ่งตัวและราคาสองฝั่ง แต่เจอ {numbers}")
        self.assertIn("เจ้าบ้าน", example)
        self.assertIn("ทีมเยือน", example)

    def test_the_two_number_budget_still_holds(self):
        self.assertIn("ไม่เกินสองตัว", self.odds)

    def test_one_sided_prices_disqualify_that_market(self):
        self.assertIn("เป็น null ให้ถือว่าตลาดนั้นใช้ไม่ได้", self.odds)


class TestTotalsRules(unittest.TestCase):
    """เส้นสูง/ต่ำ — เรียกตรง ๆ ยกสองฝั่ง และอยู่ใต้กฎเดียวกับราคาต่อรอง"""

    def setUp(self):
        self.odds = section("ราคาต่อรอง")

    def test_the_total_block_is_documented(self):
        for field in ("total.line", "total.prices", "total.source"):
            self.assertIn(field, self.odds, f"prompt ต้องอธิบาย {field}")

    def test_the_line_is_spoken_plainly_without_a_made_up_name(self):
        self.assertIn("ไม่มีชื่อไทยพิเศษ", self.odds)
        self.assertIn("สูง/ต่ำ 3.5", self.odds)

    def test_square_brackets_stay_reserved_for_the_handicap(self):
        self.assertIn("วงเล็บเหลี่ยมสงวนไว้ให้เลขเส้นแฮนดิแคปเท่านั้น", self.odds)

    def test_totals_must_quote_both_sides(self):
        example = next(line for line in self.odds.splitlines()
                       if line.strip().startswith("เขียนว่า") and "สูง/ต่ำ" in line)

        self.assertIn("ฝั่งสูง", example)
        self.assertIn("ฝั่งต่ำ", example)
        self.assertEqual(len(re.findall(r"-?\d+(?:\.\d+)?", example)), 3,
                         "ตัวอย่างต้องมีเลขเส้นหนึ่งตัวและราคาสองฝั่ง")

    def test_the_market_intent_ban_covers_totals(self):
        self.assertIn("ใช้กับเส้นสูง/ต่ำด้วยทุกข้อ", self.odds)

    def test_the_number_budget_scales_with_how_many_markets_exist(self):
        self.assertIn("มีตลาดเดียว", self.odds)
        self.assertIn("ไม่เกินสองตัว", self.odds)
        self.assertIn("ไม่เกินสี่ตัว", self.odds)

    def test_line_numbers_do_not_count_against_the_budget(self):
        self.assertIn("ไม่นับเป็นตัวเลขราคา", self.odds)

    def test_the_six_line_limit_is_untouched(self):
        self.assertIn("ไม่เกิน 6 บรรทัด", self.odds)

    def test_a_worked_example_shows_a_total_in_a_full_sentence(self):
        examples = [line for line in self.odds.splitlines()
                    if "✅" in line and "สูง/ต่ำ" in line]
        self.assertTrue(examples, "ต้องมีตัวอย่าง ✅ ที่ใช้เส้นสูง/ต่ำ")
        for line in examples:
            self.assertTrue("ฝั่งสูง" in line and "ฝั่งต่ำ" in line,
                            f"ตัวอย่างสูง/ต่ำยกฝั่งเดียว: {line.strip()}")


class TestExamplesStayConsistent(unittest.TestCase):
    """ตัวอย่าง ✅ ในไฟล์ต้องไม่ขัดกับกฎที่เพิ่งเขียนไว้เอง"""

    def test_no_example_quotes_a_nation_bound_term(self):
        for line in PROMPT.splitlines():
            if "✅" not in line:
                continue
            for term in NATION_BOUND_TERMS:
                self.assertNotIn(term, line, f"ตัวอย่างนี้ใช้คำผูกชาติ: {line.strip()}")

    def test_handicap_examples_quote_both_sides(self):
        for line in PROMPT.splitlines():
            if "✅" in line and "ราคาต่อ" in line:
                self.assertTrue("เยือน" in line and "เจ้าบ้าน" in line,
                                f"ตัวอย่างราคาต่อรองยกฝั่งเดียว: {line.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
