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

# r2.0 부터 caren 데이터셋은 named graph 두 개로 나뉜다(그래프 단위 원자적 교체를 위해서 —
# ontology/load.sh 참조). 어휘는 schema 그래프에만 있으므로 FROM 으로 기본그래프를 지정한다.
# ⚠ FROM 을 빼면 TDB2 기본그래프가 비어 있어 **조용히 빈 결과**가 돌아온다(폴백과 구분 안 됨).
GRAPH_SCHEMA = os.environ.get("FUSEKI_GRAPH_SCHEMA", "http://caren.aiclaude.kr/graph/schema")
_FROM_SCHEMA = f"FROM <{GRAPH_SCHEMA}>"

# 요구 특기(requiresSpecialty)의 **상위·하위** 특기까지 라벨/코드로 반환.
# 예: 질병 "치매" → 요구특기 "치매케어" → 하위 "인지자극"도 포함.
# ⚠ 상·하위를 따로 잇는다(위로 한 경로, 아래로 한 경로). r1 처럼 (broader|^broader)* 로
#   섞으면 '위로 올라갔다 다시 내려오는' 경로가 생겨 **형제 특기까지 근접으로 인정**된다
#   — 2026-09-20 실측에서 치매→가족상담, 당뇨→고혈압관리가 그렇게 딸려 들어왔다.
_RELATED_SPECIALTIES_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?label {from_schema} WHERE {{
  ?disease a care:Disease ; care:requiresSpecialty ?req .
  {{ ?disease rdfs:label ?dLabel }} UNION {{ ?disease care:code ?dLabel }}
  FILTER(STR(?dLabel) IN ({disease_values}))
  {{ ?related care:broaderSpecialty* ?req }} UNION {{ ?req care:broaderSpecialty* ?related }}
  {{ ?related rdfs:label ?label }} UNION {{ ?related care:code ?label }}
}}
"""

# CareTerm(및 하위 HealthStatus/MentalStatus/LifeStatus) 전체 라벨 — STT hotwords 어휘집.
_CARE_TERM_VOCAB_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?label FROM <%s> WHERE {
  ?term a ?cls . ?cls rdfs:subClassOf* care:CareTerm .
  ?term rdfs:label ?label .
}
""" % GRAPH_SCHEMA

# 질병별 연관 관찰 용어(associatedTerm) — 전역 어휘(care_term_vocabulary)를 보완하는
# 환자 맞춤 확장분. 전역 어휘를 대체하지 않고 합집합으로만 쓴다(main.py 참조) — 좁히면
# "진단명엔 없지만 실제 관찰된 증상"의 인식률이 떨어질 위험이 있어서.
_ASSOCIATED_TERMS_QUERY = """
PREFIX care: <http://caren.aiclaude.kr/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?label {from_schema} WHERE {{
  ?disease a care:Disease ; care:associatedTerm ?term .
  {{ ?disease rdfs:label ?dLabel }} UNION {{ ?disease care:code ?dLabel }}
  FILTER(STR(?dLabel) IN ({disease_values}))
  ?term rdfs:label ?label .
}}
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
_assoc_cache: dict[frozenset[str], frozenset[str]] = {}
_vocab_cache: frozenset[str] | None = None


def related_specialty_labels(diseases: frozenset[str]) -> frozenset[str]:
    """질병 라벨/코드 집합 → 관련 특기의 **라벨과 DB 코드를 모두** 담은 집합.

    l2r.subscores() 가 이 집합을 caregivers.specialties(DB 원문 문자열)와 교집합하므로
    DB 코드가 반드시 들어가야 한다. 개념 라벨만 돌려주던 r1 에서는 'hk_cleaning' 같은
    코드값 특기가 영원히 안 걸렸다(2026-09-20 실측). 조회 실패 시 빈 집합(캐시 안 함).
    """
    if not diseases:
        return frozenset()
    if diseases in _cache:
        return _cache[diseases]
    query = _RELATED_SPECIALTIES_QUERY.format(disease_values=_sparql_literals(diseases),
                                              from_schema=_FROM_SCHEMA)
    bindings = _sparql_select(query)
    if bindings is None:
        return frozenset()
    result = frozenset(b["label"]["value"] for b in bindings)
    _cache[diseases] = result
    return result


def associated_term_labels(diseases: frozenset[str]) -> frozenset[str]:
    """질병 라벨 집합 → 온톨로지상 연관 CareTerm 라벨 집합(환자 맞춤 STT 어휘 확장분).
    조회 실패 시 빈 집합(캐시 안 함) — related_specialty_labels와 동일한 정책."""
    if not diseases:
        return frozenset()
    if diseases in _assoc_cache:
        return _assoc_cache[diseases]
    query = _ASSOCIATED_TERMS_QUERY.format(disease_values=_sparql_literals(diseases),
                                           from_schema=_FROM_SCHEMA)
    bindings = _sparql_select(query)
    if bindings is None:
        return frozenset()
    result = frozenset(b["label"]["value"] for b in bindings)
    _assoc_cache[diseases] = result
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
    _assoc_cache.clear()
    _vocab_cache = None
