"""
온톨로지(Fuseki) SPARQL 클라이언트 — 'caren' 데이터셋의 질병→특기(requiresSpecialty),
특기 상하위(broaderSpecialty), 케어 관찰 용어(CareTerm) 관계를 조회해 l2r.py의 매칭
feature와 STT(main.py /ai/voice/transcribe) hotwords 힌트를 보강한다.

Fuseki 미기동/네트워크 실패/타임아웃 시 전부 빈 결과로 폴백한다 — 매칭/STT 서비스는
온톨로지 유무와 무관하게 항상 동작해야 한다(다른 엔드포인트의 LLM 폴백과 동일한 원칙,
CLAUDE.md "폴백 경로를 제거하지 말 것" 참조).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

FUSEKI_URL = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
FUSEKI_DATASET = os.environ.get("FUSEKI_DATASET", "caren")
TIMEOUT_SEC = float(os.environ.get("FUSEKI_TIMEOUT_SEC", "1.5"))

_SPARQL_ENDPOINT = f"{FUSEKI_URL}/{FUSEKI_DATASET}/sparql"

# 요구 특기(requiresSpecialty)의 상하위 특기(broaderSpecialty, 양방향 폐쇄)까지 라벨로 반환.
# 예: 질병 "치매" → 요구특기 "치매케어" → broaderSpecialty 역방향으로 "인지자극"도 포함.
_RELATED_SPECIALTIES_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?label WHERE {{
  ?disease a care:Disease ; rdfs:label ?dLabel ; care:requiresSpecialty ?req .
  FILTER(STR(?dLabel) IN ({disease_values}))
  ?related (care:broaderSpecialty|^care:broaderSpecialty)* ?req .
  ?related rdfs:label ?label .
}}
"""

# CareTerm(및 하위 HealthStatus/MentalStatus/LifeStatus) 전체 라벨 — STT hotwords 어휘집.
_CARE_TERM_VOCAB_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?label WHERE {
  ?term a ?cls . ?cls rdfs:subClassOf* care:CareTerm .
  ?term rdfs:label ?label .
}
"""


def _sparql_literals(values: frozenset[str]) -> str:
    return ", ".join(json.dumps(v) for v in values)


def _sparql_select(query: str) -> list[dict] | None:
    """SPARQL SELECT 실행 → bindings 리스트. 실패 시 None(호출측이 캐시 여부 결정)."""
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        _SPARQL_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/sparql-results+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return data.get("results", {}).get("bindings", [])


# 성공한 조회만 캐시(빈 결과도 성공이면 캐시 — 매핑이 원래 없는 질병 재조회를 막음).
# 실패(네트워크/타임아웃)는 캐시하지 않는다 — Fuseki 다운 중엔 매 호출이 그대로 재시도되지만,
# 복구 즉시 다음 호출에서 바로 정상 결과로 돌아와야 하기 때문(장애 중 캐싱하면 프로세스
# 재시작 전까지 복구 후에도 계속 폴백 상태로 굳어버림 — 2026-07-30 케이스4 테스트에서 확인된 문제).
_cache: dict[frozenset[str], frozenset[str]] = {}
_vocab_cache: frozenset[str] | None = None


def related_specialty_labels(diseases: frozenset[str]) -> frozenset[str]:
    """질병 라벨 집합 → 온톨로지상 관련 특기 라벨 집합. 조회 실패 시 빈 집합(캐시 안 함)."""
    if not diseases:
        return frozenset()
    if diseases in _cache:
        return _cache[diseases]
    query = _RELATED_SPECIALTIES_QUERY.format(disease_values=_sparql_literals(diseases))
    bindings = _sparql_select(query)
    if bindings is None:
        return frozenset()
    result = frozenset(b["label"]["value"] for b in bindings)
    _cache[diseases] = result
    return result


def care_term_vocabulary() -> frozenset[str]:
    """건강/정신/생활상태 CareTerm 라벨 전체(STT hotwords용). 조회 실패 시 빈 집합(캐시 안 함).
    프로세스 생애주기 동안 한 번 성공하면 계속 재사용 — 어휘집은 매칭 feature와 달리 입력값이
    없어(전역 어휘) 캐시 키가 필요 없다."""
    global _vocab_cache
    if _vocab_cache is not None:
        return _vocab_cache
    bindings = _sparql_select(_CARE_TERM_VOCAB_QUERY)
    if bindings is None:
        return frozenset()
    _vocab_cache = frozenset(b["label"]["value"] for b in bindings)
    return _vocab_cache


def reset_cache() -> None:
    """Fuseki 데이터 갱신 후(재적재 등) 테스트/운영에서 캐시 무효화용."""
    global _vocab_cache
    _cache.clear()
    _vocab_cache = None
