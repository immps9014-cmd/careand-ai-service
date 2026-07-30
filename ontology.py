"""
온톨로지(Fuseki) SPARQL 클라이언트 — 'caren' 데이터셋의 질병→특기(requiresSpecialty) 및
특기 상하위(broaderSpecialty) 관계를 조회해 l2r.py의 매칭 feature를 보강한다.

Fuseki 미기동/네트워크 실패/타임아웃 시 전부 빈 결과로 폴백한다 — 매칭 서비스는
온톨로지 유무와 무관하게 항상 동작해야 한다(다른 엔드포인트의 LLM 폴백과 동일한 원칙,
CLAUDE.md "폴백 경로를 제거하지 말 것" 참조).
"""
from __future__ import annotations

import functools
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


def _sparql_literals(values: frozenset[str]) -> str:
    return ", ".join(json.dumps(v) for v in values)


@functools.lru_cache(maxsize=256)
def related_specialty_labels(diseases: frozenset[str]) -> frozenset[str]:
    """질병 라벨 집합 → 온톨로지상 관련 특기 라벨 집합. 미조회/실패 시 빈 집합.
    프로세스 내 동일 질병조합 재조회를 피하기 위해 캐시(실패도 캐시돼 다운타임 중 재시도 폭주 방지)."""
    if not diseases:
        return frozenset()
    query = _RELATED_SPECIALTIES_QUERY.format(disease_values=_sparql_literals(diseases))
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
        return frozenset()
    return frozenset(b["label"]["value"] for b in data.get("results", {}).get("bindings", []))


def reset_cache() -> None:
    """Fuseki 데이터 갱신 후(재적재 등) 테스트/운영에서 캐시 무효화용."""
    related_specialty_labels.cache_clear()
