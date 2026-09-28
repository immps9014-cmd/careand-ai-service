"""
칩 기반 케어일지 — 사업계획서 기능 40 「칩→SPARQL 조회→정규화 JSON→LLM 조립」 (2026-09-28, 구현계획 S5)

  1. 칩 코드 → SPARQL(스키마 그래프 care-journal 계층)로 분류·표현조각·안전 알림을 읽는다
  2. 정규화 JSON: {sections:[{category, items:[{code,label,phrase,tone}]}], alerts:[...], note}
  3. LLM 은 정규화 JSON 의 표현조각만으로 문장을 조립한다(새 사실 금지). 결과는 verify.py 로 원문(표현조각+메모) 대조
  4. Fuseki 가 죽으면 같은 원본 ontology/care_journal.json 으로, LLM 이 없으면 표현조각을 이어 붙인 문장으로 대신한다(기능 42)

위험 칩(cj:raisesAlert)은 LLM 결과와 상관없이 안전 알림으로 확정한다 — 결정적 알림(기능 41).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import ontology

_SRC = os.path.join(os.path.dirname(__file__), "ontology", "care_journal.json")
_catalog_cache: dict[str, Any] | None = None

_CATALOG_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX cj: <http://caren.aiclaude.kr/ontology/journal#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?code ?label ?cat ?catLabel ?catOrder ?text ?tone ?order ?alert ?alertLabel ?severity FROM <%s> WHERE {
  ?c a cj:Chip ; cj:code ?code ; rdfs:label ?label ; cj:inCategory ?catIri ; cj:hasPhrase ?p ; cj:tone ?tone ; cj:order ?order .
  ?p cj:text ?text . ?catIri rdfs:label ?catLabel ; cj:order ?catOrder .
  BIND(STRAFTER(STR(?catIri), "#Cat_") AS ?cat)
  OPTIONAL { ?c cj:raisesAlert ?alertIri . ?alertIri rdfs:label ?alertLabel ; care:alertSeverity ?severity .
             BIND(STRAFTER(STR(?alertIri), "#") AS ?alert) }
} ORDER BY ?order
""" % ontology.GRAPH_SCHEMA


def _from_json() -> dict[str, Any]:
    d = json.load(open(_SRC, encoding="utf-8"))
    alerts = {r["id"]: r for r in (ontology.safety_alert_rules() or [])}
    cats = {c["id"]: c for c in d["categories"]}
    chips = []
    for i, ch in enumerate(d["chips"], 1):
        a = ch.get("alert")
        chips.append({
            "code": ch["id"], "label": ch["label"], "category": ch["cat"], "category_label": cats[ch["cat"]]["label"],
            "category_order": cats[ch["cat"]]["order"], "phrase": ch["phrase"], "tone": ch["tone"], "order": i,
            "alert": ({"id": a, "label": alerts.get(a, {}).get("label", ch["label"]),
                       "severity": alerts.get(a, {}).get("severity", "high")} if a else None),
        })
    return {"chips": chips, "source": "json"}


def catalog() -> dict[str, Any]:
    """칩 목록(앱 화면용). SPARQL 우선, 실패 시 JSON 원본. SPARQL 결과만 캐시한다(JSON 폴백은 다음에 다시 시도)."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    rows = ontology._sparql_select(_CATALOG_QUERY)
    if not rows:
        return _from_json()
    chips = []
    for b in rows:
        v = lambda k: b[k]["value"] if k in b else None   # noqa: E731
        chips.append({
            "code": v("code"), "label": v("label"), "category": v("cat"), "category_label": v("catLabel"),
            "category_order": int(v("catOrder")), "phrase": v("text"), "tone": v("tone"), "order": int(v("order")),
            "alert": ({"id": v("alert"), "label": v("alertLabel"), "severity": v("severity")} if v("alert") else None),
        })
    _catalog_cache = {"chips": chips, "source": "sparql"}
    return _catalog_cache


def reset_cache() -> None:
    global _catalog_cache
    _catalog_cache = None


def normalize(codes: list[str], note: str | None = None) -> dict[str, Any]:
    """선택한 칩 → 정규화 JSON. 모르는 코드는 unknown 으로 돌려준다(조용히 버리지 않음)."""
    cat = catalog()
    by_code = {c["code"]: c for c in cat["chips"]}
    picked = [by_code[c] for c in dict.fromkeys(codes) if c in by_code]
    sections: dict[str, dict[str, Any]] = {}
    for c in sorted(picked, key=lambda x: (x["category_order"], x["order"])):
        s = sections.setdefault(c["category"], {"category": c["category_label"], "order": c["category_order"], "items": []})
        s["items"].append({"code": c["code"], "label": c["label"], "phrase": c["phrase"], "tone": c["tone"]})
    alerts, seen = [], set()
    for c in picked:
        if c["alert"] and c["alert"]["id"] not in seen:
            seen.add(c["alert"]["id"])
            alerts.append({**c["alert"], "evidence": f"칩: {c['label']}"})
    order = {"critical": 0, "high": 1}
    return {
        "sections": sorted(sections.values(), key=lambda s: s["order"]),
        "alerts": sorted(alerts, key=lambda a: order.get(a["severity"], 2)),
        "note": (note or "").strip() or None,
        "unknown": [c for c in codes if c not in by_code],
        "catalog_source": cat["source"],
    }


def source_text(norm: dict[str, Any], duration_min: int) -> str:
    """사실성 검증의 원문 — 표현조각과 돌봄전문가 메모만. LLM 이 여기 없는 말을 하면 verify 가 잡는다."""
    lines = [f"돌봄 시간 {duration_min}분"]
    for s in norm["sections"]:
        lines.append(f"{s['category']}: " + " / ".join(i["phrase"] for i in s["items"]))
    if norm["note"]:
        lines.append(f"메모: {norm['note']}")
    return "\n".join(lines)


def template(norm: dict[str, Any], name: str, duration_min: int) -> dict[str, str]:
    """LLM 없이 표현조각을 이어 붙인 일지(폴백). 안전 알림이 있으면 맨 앞에 쓴다."""
    g = []
    if norm["alerts"]:
        g.append("오늘 돌봄 중 확인이 필요한 상황이 있었어요(" + ", ".join(a["label"] for a in norm["alerts"]) + ").")
    hours = round(duration_min / 60, 1) if duration_min else None
    g.append(f"{name}의 오늘 돌봄 기록이에요" + (f"({hours}시간)." if hours else "."))
    for s in norm["sections"]:
        g.append(" ".join(i["phrase"] + "." for i in s["items"]))
    if norm["note"]:
        g.append(f"돌봄전문가 메모: {norm['note']}")
    med = [f"[칩 기록] 대상 {name} / {duration_min}분"]
    for s in norm["sections"]:
        med.append(f"- {s['category']}: " + ", ".join(i["label"] for i in s["items"]))
    if norm["alerts"]:
        med.append("- 안전 알림: " + ", ".join(f"{a['label']}({a['severity']})" for a in norm["alerts"]))
    return {"guardian_version": "\n".join(g), "medical_version": "\n".join(med)}


SYSTEM = (
    "너는 돌봄 플랫폼의 케어일지 작성 도우미다. 입력은 돌봄전문가가 누른 '칩'을 정규화한 JSON 이다. "
    "sections 의 phrase(표현조각)와 note 만 근거로 보호자용 일지(따뜻한 존댓말, 3~6문장)와 의료진용 요약(간결)을 쓴다. "
    "표현조각에 없는 활동·상태·수치를 새로 만들지 마라. 표현조각은 자연스럽게 이어 쓰되 뜻을 바꾸지 마라. "
    "alerts 가 있으면 보호자용 첫 문장에서 그 상황을 차분하게 알리고, 과장하거나 진단하지 마라. "
    "'다행히', '이상 없었다', '괜찮으셨다', '편안한 하루였다'처럼 입력에 없는 확인 결과·안심·총평 문장을 덧붙이지 마라. "
    "대상자 호칭은 입력 이름을 그대로 쓴다. "
    "출력 JSON 스키마: {\"guardian_version\": str, \"medical_version\": str}."
)


# 알림 상황에서 LLM 이 덧붙이기 쉬운 근거 없는 안심 문구 — 표현조각·메모에 없으면 LLM 결과를 쓰지 않는다
_REASSURE = re.compile(r"다행히|이상(은|이)?\s*(없|발견되지)|괜찮으셨|문제(는|가)?\s*없|편안(한|하게)\s*(하루|보내)")


def unsupported_reassurance(output: str, source: str) -> list[str]:
    """출력에만 있는 안심 표현 목록(원문에 같은 표현이 있으면 인정)."""
    return [m.group(0) for m in _REASSURE.finditer(output or "") if m.group(0) not in (source or "")]
