"""AI 서비스 단위시험 (S6, 2026-09-29) — 외부 호출 없이 도는 핵심 로직.

  실행: venv/bin/python -m unittest discover -s tests -v
  온톨로지 SPARQL 은 막아 둔다(ontology._sparql_select → None) — Fuseki 장애 때의 대체 경로(기능 42)까지 같이 시험된다.
  REQ: F-29 사실성 검증 · F-41 안전 알림 · F-40 칩 일지 · N-05/국외이전 가명 처리
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ontology  # noqa: E402

ontology._sparql_select = lambda *a, **k: None   # Fuseki 없이
ontology.reset_cache()
import journal  # noqa: E402
import pii  # noqa: E402
import verify  # noqa: E402


class VerifyTest(unittest.TestCase):
    """UT-29 사실성 검증"""

    def test_supported_numbers_and_units(self):
        v = verify.verify("체온 37.9도, 식사 50% 드셨어요", "저녁에 체온 37.9℃, 밥 반 그릇")
        self.assertEqual(v["unsupported"], [])
        self.assertEqual(v["risk"], 0.0)

    def test_unsupported_number_raises_risk(self):
        v = verify.verify("혈압 150/90mmHg 이었어요", "산책을 함께 했어요")
        self.assertTrue(any(u["type"] == "수치" for u in v["unsupported"]))
        self.assertGreaterEqual(v["risk"], verify.RISK_REVIEW)
        self.assertTrue(v["needs_review"])

    def test_bare_vitals_are_claims(self):
        """단위 없이 쓴 활력징후도 원문 대조(S6 발견·수정)"""
        self.assertTrue(verify.verify("혈압 150/90 이었어요", "산책을 함께 했어요")["unsupported"])
        self.assertEqual(verify.verify("혈압 150/90 이었어요", "혈압 150에 90")["unsupported"], [])
        self.assertEqual(verify.verify("혈당은 180", "혈당 180 나옴")["unsupported"], [])

    def test_temperature_spoken_without_unit(self):
        """원문 '체온 36.5'(단위 생략)는 일지 '36.5도'의 근거(S6 오탐 수정)"""
        self.assertEqual(verify.verify("체온 36.5도", "체온 36.5")["unsupported"], [])

    def test_sensitive_info_masked(self):
        v = verify.verify("보호자 연락처 010-1234-5678 로 알려 드렸어요", "특이사항 없음")
        self.assertIn("[", v["masked_output"])
        self.assertNotIn("010-1234-5678", v["masked_output"])


class SafetyAlertTest(unittest.TestCase):
    """UT-41 안전 알림 10종 — 내장본(Fuseki 없음)으로 감지, 부정 표현 제외"""

    def test_fall_detected(self):
        a = verify.detect_alerts("화장실에서 넘어지셨어요")
        self.assertEqual(a[0]["id"], "Alert_Fall")

    def test_negation_not_alerted(self):
        self.assertEqual(verify.detect_alerts("넘어지지 않게 부축해 드렸어요"), [])

    def test_fever_new_keyword(self):
        self.assertEqual(verify.detect_alerts("열이 있어 물수건을 드렸어요")[0]["id"], "Alert_Fever")

    def test_critical_first(self):
        a = verify.detect_alerts("열이 나고 숨이 차 보이셨어요")
        self.assertEqual(a[0]["severity"], "critical")

    def test_ten_rules(self):
        self.assertEqual(len(verify.alert_rules()), 10)


class ChipJournalTest(unittest.TestCase):
    """UT-40 칩 일지 — 원본 JSON 폴백, 정규화, 조립, 안심 문구 감지"""

    def test_catalog_falls_back_to_json(self):
        c = journal.catalog()
        self.assertEqual(c["source"], "json")
        self.assertEqual(len(c["chips"]), 55)
        self.assertEqual(sum(1 for x in c["chips"] if x["alert"]), 10)

    def test_catalog_matches_ttl_source(self):
        src = json.load(open(os.path.join(os.path.dirname(journal.__file__), "ontology", "care_journal.json"), encoding="utf-8"))
        self.assertEqual(len({c["id"] for c in src["chips"]}), 55, "칩 코드 중복 없음")
        self.assertEqual(len({c["phrase"] for c in src["chips"]}), 55, "표현조각 중복 없음")

    def test_normalize_orders_and_alerts(self):
        n = journal.normalize(["Walk", "ChipFall", "MealHalf", "MealHalf", "BAD"], "메모")
        self.assertEqual([s["category"] for s in n["sections"]], ["식사·수분", "활동·운동", "안전 알림"])
        self.assertEqual(n["alerts"][0]["id"], "Alert_Fall")
        self.assertEqual(n["unknown"], ["BAD"])

    def test_template_mentions_alert_first(self):
        n = journal.normalize(["MealAll", "ChipFever"])
        t = journal.template(n, "김영희 어르신", 120)
        self.assertTrue(t["guardian_version"].startswith("오늘 돌봄 중 확인이 필요한 상황"))
        self.assertIn("발열", t["medical_version"])

    def test_source_text_contains_labels(self):
        n = journal.normalize(["ChipFever"])
        self.assertIn("발열", journal.source_text(n, 60))

    def test_unsupported_reassurance(self):
        self.assertTrue(journal.unsupported_reassurance("다행히 별다른 이상은 발견되지 않았습니다", "넘어지는 일이 있어"))
        self.assertEqual(journal.unsupported_reassurance("식사를 절반 드셨어요", "식사는 절반 정도 드셨어요"), [])


class PiiTest(unittest.TestCase):
    """UT-N 국외 이전 최소화 — 이름·전화·이메일 가명 처리 후 복원"""

    def test_mask_and_unmask_roundtrip(self):
        m = pii.Masker(("박순자",))
        text = "박순자 어르신(010-2222-3333, park@example.com) 산책"
        masked = m.mask(text)
        for raw in ("박순자", "010-2222-3333", "park@example.com"):
            self.assertNotIn(raw, masked)
        self.assertEqual(m.unmask(masked), text)


if __name__ == "__main__":
    unittest.main()
