"""AI 케어일지 사실성 검증 · 안전 알림 감지 (사업계획서 기능 29·41, 2026-09-28 구현계획 S3).

LLM 이 만든 일지를 원문(음성 전사·활동 메모)과 대조해 근거 없는 주장을 찾고, 원문에서 안전 알림 10종을 감지한다.
판정은 결정적 규칙이다(LLM 을 다시 부르지 않음 — 비용·국외 이전 없이 매번 같은 결과).

  · 사실성: 일지 속 수치(분·시간·%·그릇·회·도·mmHg·mg/dL…)와 케어용어(CareTerm)가 원문에 있는지 본다.
            risk = 근거 없는 주장 수 / 전체 주장 수. 케어용어(질환·증상) 조작은 가중 2.
  · 민감정보: 일지에 전화·주민번호·이메일이 들어 있으면 가리고 표시한다.
  · 안전 알림: 원문에 낙상·발열·호흡곤란 등 표현이 있고 바로 뒤에 부정("않/없/안")이 없으면 알림.
백엔드는 alerts 가 있거나 risk ≥ RISK_REVIEW 이거나 민감정보가 있으면 관리자 검수로 돌리고, 아니면 바로 보호자에게 보낸다.
"""
from __future__ import annotations

import re
from typing import Any

import ontology
import pii

RISK_REVIEW = 0.25   # 이 이상이면 검수 큐

# 온톨로지를 못 읽을 때 쓰는 내장본 — ontology/care-domain.ttl 의 care:SafetyAlert 와 같게 유지(기능 42 장애 복원)
_FALLBACK_ALERTS = [
    {"id": "Alert_Fall", "label": "낙상", "severity": "high", "keywords": ["낙상", "넘어지", "넘어졌", "미끄러지", "미끄러졌", "침대에서 떨어"]},
    {"id": "Alert_Fever", "label": "발열", "severity": "high", "keywords": ["발열", "고열", "열이 나", "열이 났", "열이 높"]},
    {"id": "Alert_Dyspnea", "label": "호흡곤란", "severity": "critical", "keywords": ["호흡곤란", "숨이 차", "숨을 못", "숨쉬기 힘들", "숨쉬기가 힘들"]},
    {"id": "Alert_Aspiration", "label": "흡인·사레", "severity": "high", "keywords": ["흡인", "사레", "기도가 막"]},
    {"id": "Alert_Consciousness", "label": "의식저하", "severity": "critical", "keywords": ["의식저하", "의식이 없", "의식을 잃", "반응이 없", "깨워도 안"]},
    {"id": "Alert_Seizure", "label": "경련", "severity": "critical", "keywords": ["경련", "발작"]},
    {"id": "Alert_ChestPain", "label": "흉통", "severity": "critical", "keywords": ["흉통", "가슴 통증", "가슴이 아프", "가슴을 움켜"]},
    {"id": "Alert_Dehydration", "label": "탈수", "severity": "high", "keywords": ["탈수", "소변을 거의", "소변이 거의"]},
    {"id": "Alert_Jaundice", "label": "황달(신생아)", "severity": "high", "keywords": ["황달", "눈이 노랗", "피부가 노랗"]},
    {"id": "Alert_Crisis", "label": "위기징후(자해·자살)", "severity": "critical", "keywords": ["죽고 싶", "자살", "자해", "극단적 선택", "살고 싶지 않"]},
]

# "넘어지지 않게", "열은 없", "사레 안 들리" — 감지 표현 바로 뒤(8자 안)의 부정
_NEG_AFTER = re.compile(r"^.{0,8}?(않|없|안\s|아니|말도록|않도록|주의)")

_UNIT = r"(분|시간|%|퍼센트|그릇|공기|숟가락|회|번|잔|컵|ml|mL|cc|mg|g|kg|도|℃|mmHg|mg/dL|개|알|정)"
_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*" + _UNIT)
_SENSITIVE = [(k, p) for k, p in pii._PATTERNS if k in ("주민번호", "전화", "이메일")]

# 원문의 말 표현 ↔ 수치 (반 그릇 = 50% 등) — 일지가 수치로 바꿔 쓴 경우 근거로 인정
_SPOKEN = {"반": ["50", "0.5"], "절반": ["50", "0.5"], "다": ["100"], "전부": ["100"], "한": ["1"], "두": ["2"], "세": ["3"], "네": ["4"],
           "다섯": ["5"], "열": ["10"], "스무": ["20"], "서른": ["30"], "마흔": ["40"], "쉰": ["50"], "한 시간": ["60", "1"], "삼십": ["30"], "이십": ["20"]}


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def alert_rules() -> list[dict[str, Any]]:
    return ontology.safety_alert_rules() or _FALLBACK_ALERTS


def detect_alerts(source: str) -> list[dict[str, Any]]:
    """원문에서 안전 알림 감지 — [{id, label, severity, evidence}] (심각한 것 먼저)."""
    found = []
    text = source or ""
    for rule in alert_rules():
        for kw in sorted(rule["keywords"], key=len, reverse=True):
            for m in re.finditer(re.escape(kw), text):
                if _NEG_AFTER.match(text[m.end():m.end() + 12]):
                    continue
                s = max(0, m.start() - 15)
                found.append({"id": rule["id"], "label": rule["label"], "severity": rule["severity"],
                              "evidence": text[s:m.end() + 15].strip()})
                break
            if found and found[-1]["id"] == rule["id"]:
                break
    order = {"critical": 0, "high": 1}
    return sorted(found, key=lambda a: order.get(a["severity"], 2))


def _numbers(text: str) -> list[str]:
    return [m.group(0) for m in _NUM.finditer(text or "")]


# 같은 뜻의 단위 묶음 — 원문과 일지가 다른 표기를 써도 같은 단위로 본다
_UNIT_GROUP = {"%": "pct", "퍼센트": "pct", "ml": "ml", "mL": "ml", "cc": "ml", "도": "temp", "℃": "temp",
               "회": "times", "번": "times", "개": "piece", "알": "piece", "정": "piece"}
# 말할 때 단위를 빼는 수치(혈압 "130에 80", 혈당 "150") — 숫자만 맞아도 근거로 인정
_BARE_OK = {"mmHg", "mg/dL"}


def _supported_number(claim: str, source_norm: str, source_raw: str) -> bool:
    m = _NUM.match(claim)
    val, unit = m.group(1), m.group(2)
    v = val.rstrip("0").rstrip(".") if "." in val else val
    group = _UNIT_GROUP.get(unit, unit)
    for sm in _NUM.finditer(source_norm):                     # 같은 수치·같은 단위(묶음)
        sv = sm.group(1).rstrip("0").rstrip(".") if "." in sm.group(1) else sm.group(1)
        if sv == v and _UNIT_GROUP.get(sm.group(2), sm.group(2)) == group:
            return True
    if unit in _BARE_OK and re.search(r"(?<!\d)" + re.escape(v) + r"(?!\d)", source_norm):
        return True
    for word, vals in _SPOKEN.items():                         # 반 그릇 → 50% 등
        if v in vals and word in source_raw:
            return True
    return False


def verify(output: str, source: str) -> dict[str, Any]:
    """일지(output)를 원문(source)과 대조. 결과: risk·근거 없는 주장·민감정보·안전 알림·검수 필요 여부."""
    src_norm, out = _norm(source), output or ""
    claims, unsupported = 0, []

    for c in dict.fromkeys(_numbers(out)):             # 같은 수치 반복은 한 번만
        claims += 1
        if not _supported_number(c, src_norm, source or ""):
            unsupported.append({"type": "수치", "claim": c})

    # 안전 알림의 감지 표현은 해당 용어의 근거로 인정 — 원문 "미끄러지셨다" → 일지 "낙상"
    synonyms: dict[str, list[str]] = {}
    for rule in alert_rules():
        for part in re.split(r"[·()]", rule["label"]):
            if part.strip():
                synonyms.setdefault(part.strip(), []).extend(rule["keywords"])
    vocab = ontology.care_term_vocabulary()
    for term in vocab:
        if term in out:
            claims += 2
            evid = [term] + synonyms.get(term, [])
            if not any(_norm(e) in src_norm for e in evid):
                unsupported.append({"type": "케어용어", "claim": term})

    weight = sum(2 if u["type"] == "케어용어" else 1 for u in unsupported)
    risk = round(weight / claims, 3) if claims else 0.0

    sensitive = []
    masked = out
    for kind, pat in _SENSITIVE:
        if pat.search(masked):
            sensitive.append(kind)
            masked = pat.sub(f"[{kind}]", masked)

    alerts = detect_alerts(source)
    reasons = []
    if alerts:
        reasons.append("안전 알림: " + ", ".join(a["label"] for a in alerts))
    if risk >= RISK_REVIEW:
        reasons.append(f"원문에 없는 내용 의심 {len(unsupported)}건(위험도 {risk:.2f})")
    if sensitive:
        reasons.append("일지에 민감정보 포함(" + ", ".join(sensitive) + ") — 가림 처리")

    return {
        "risk": risk,
        "claims": claims,
        "unsupported": unsupported,
        "sensitive": sensitive,
        "alerts": alerts,
        "needs_review": bool(reasons),
        "reasons": reasons,
        "masked_output": masked if sensitive else None,
        "method": "rule-v1",
    }
