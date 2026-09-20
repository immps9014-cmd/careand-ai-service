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


# ═════════════════════════════════════════════════════════════════════════════
# 분석 질의 — 관리자 화면(/admin/ontology)용
#
#   hisense MES 의 /impact 화면과 같은 방침:
#   · **화이트리스트 질의만 실행한다.** 클라이언트가 SPARQL 문자열을 보내지 못한다.
#     파라미터는 정수 ID 뿐이고 IRI 를 코드가 조립한다.
#   · Fuseki 가 죽어도 화면이 통째로 죽지 않게 각 함수가 빈 결과로 폴백한다
#     (호출측이 '온톨로지 미가용' 배너를 띄운다).
#   · 스키마(어휘)와 데이터(업무객체)를 함께 봐야 하므로 FROM 을 둘 다 건다.
# ═════════════════════════════════════════════════════════════════════════════

GRAPH_DATA = os.environ.get("FUSEKI_GRAPH_DATA", "http://caren.aiclaude.kr/graph/caren")
ID_BASE = "http://caren.aiclaude.kr/id/"

# 분석 질의는 매칭 경로(1.5초)보다 여유를 준다 — 화면 한 번 그릴 때만 돈다.
ANALYSIS_TIMEOUT_SEC = float(os.environ.get("FUSEKI_ANALYSIS_TIMEOUT_SEC", "8"))

_BOTH = f"FROM <{GRAPH_SCHEMA}>\nFROM <{GRAPH_DATA}>"
_PRE = ("PREFIX care: <http://caren.aiclaude.kr/ontology#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n")

# 근접 특기 폐쇄 — 상·하위만(형제 특기 유입 차단, related_specialty_labels 와 같은 규칙).
_CLOSURE = "{{ ?sp care:broaderSpecialty* ?req }} UNION {{ ?req care:broaderSpecialty* ?sp }}"


def _rows(query: str, timeout: float = ANALYSIS_TIMEOUT_SEC) -> list[dict]:
    """SELECT 실행 → [{var: value}]. 실패 시 빈 리스트(화면이 폴백 배너를 띄운다)."""
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        _SPARQL_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/sparql-results+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    return [{k: v["value"] for k, v in b.items()} for b in data.get("results", {}).get("bindings", [])]


def _local(iri: str) -> str:
    return iri.rsplit("/", 1)[-1] if iri.startswith(ID_BASE) else iri.rsplit("#", 1)[-1]


def graph_available() -> bool:
    return bool(_rows(f"{_PRE}SELECT ?s {_BOTH} WHERE {{ ?s a care:Caregiver }} LIMIT 1", timeout=3))


def disease_coverage() -> list[dict]:
    """질병별 공급 커버리지 — 이 화면의 핵심.

    '어떤 질병은 필요한 특기를 가진 인력이 아예 없다' 를 드러내는 게 목적이다.
    hisense /impact 가 자재→수주 영향을 보여주듯, 여기선 질병→특기→인력을 본다.
    """
    caregivers = {r["d"]: int(r["n"]) for r in _rows(f"""{_PRE}
        SELECT ?d (COUNT(DISTINCT ?cg) AS ?n) {_BOTH} WHERE {{
          ?d a care:Disease ; care:requiresSpecialty ?req .
          {_CLOSURE.format()}
          ?cg a care:Caregiver ; care:hasSpecialty ?sp ; care:status "active" .
        }} GROUP BY ?d""")}
    recipients = {r["d"]: int(r["n"]) for r in _rows(f"""{_PRE}
        SELECT ?d (COUNT(DISTINCT ?r) AS ?n) {_BOTH} WHERE {{
          ?r a care:Recipient ; care:hasDisease ?d }} GROUP BY ?d""")}
    requests = {r["d"]: int(r["n"]) for r in _rows(f"""{_PRE}
        SELECT ?d (COUNT(DISTINCT ?mr) AS ?n) {_BOTH} WHERE {{
          ?mr a care:MatchRequest ; care:forRecipient ?r . ?r care:hasDisease ?d }} GROUP BY ?d""")}
    needs: dict[str, list[str]] = {}
    for r in _rows(f"""{_PRE}
        SELECT ?d ?label {_BOTH} WHERE {{
          ?d a care:Disease ; care:requiresSpecialty ?req . ?req rdfs:label ?label }}"""):
        needs.setdefault(r["d"], []).append(r["label"])

    out = []
    for r in _rows(f"""{_PRE}
        SELECT ?d ?label ?code {_BOTH} WHERE {{
          ?d a care:Disease ; rdfs:label ?label . OPTIONAL {{ ?d care:code ?code }} }}"""):
        out.append({
            "id": _local(r["d"]),
            "label": r["label"],
            "code": r.get("code"),          # code 가 없으면 DB 에 없는 질병(어휘 선행 등록분)
            "in_db": bool(r.get("code")),
            "required_specialties": sorted(needs.get(r["d"], [])),
            "caregivers": caregivers.get(r["d"], 0),
            "recipients": recipients.get(r["d"], 0),
            "requests": requests.get(r["d"], 0),
        })
    # 공백(인력 0)이면서 실제 대상자가 있는 질병을 맨 위로 — 화면에서 제일 먼저 봐야 할 줄이다.
    out.sort(key=lambda x: (x["caregivers"] > 0, -x["recipients"], x["label"]))
    return out


def specialty_supply() -> list[dict]:
    """특기별 활성 인력 수 — 커버리지 공백의 원인을 짚는 표."""
    counts = {r["sp"]: int(r["n"]) for r in _rows(f"""{_PRE}
        SELECT ?sp (COUNT(DISTINCT ?cg) AS ?n) {_BOTH} WHERE {{
          ?cg a care:Caregiver ; care:hasSpecialty ?sp ; care:status "active" }} GROUP BY ?sp""")}
    needed = {r["sp"] for r in _rows(f"""{_PRE}
        SELECT DISTINCT ?sp {_BOTH} WHERE {{ ?d a care:Disease ; care:requiresSpecialty ?sp }}""")}
    out = []
    for r in _rows(f"""{_PRE}
        SELECT ?sp ?label {_BOTH} WHERE {{ ?sp a care:Specialty ; rdfs:label ?label }}"""):
        out.append({"id": _local(r["sp"]), "label": r["label"],
                    "caregivers": counts.get(r["sp"], 0),
                    "required_by_disease": r["sp"] in needed})
    out.sort(key=lambda x: (-x["caregivers"], x["label"]))
    return out


def caregiver_directory() -> list[dict]:
    """영향분석 대상 고르기용 목록(마스킹 이름)."""
    specs: dict[str, list[str]] = {}
    for r in _rows(f"""{_PRE}
        SELECT ?cg ?label {_BOTH} WHERE {{
          ?cg a care:Caregiver ; care:hasSpecialty ?sp . ?sp rdfs:label ?label }}"""):
        specs.setdefault(r["cg"], []).append(r["label"])
    out = []
    for r in _rows(f"""{_PRE}
        SELECT ?cg ?name ?status ?grade ?rating {_BOTH} WHERE {{
          ?cg a care:Caregiver ; care:status ?status .
          OPTIONAL {{ ?cg care:displayName ?name }} OPTIONAL {{ ?cg care:gradeLevel ?grade }}
          OPTIONAL {{ ?cg care:ratingAvg ?rating }} }}"""):
        out.append({"id": int(_local(r["cg"])), "name": r.get("name") or "—",
                    "status": r["status"], "grade": int(r["grade"]) if r.get("grade") else None,
                    "rating": float(r["rating"]) if r.get("rating") else None,
                    "specialties": sorted(specs.get(r["cg"], []))})
    out.sort(key=lambda x: (x["status"] != "active", x["id"]))
    return out


def caregiver_impact(caregiver_id: int) -> dict:
    """인력 1명이 빠지면 무엇이 흔들리는가 — hisense /impact 의 caren 대응.

    자재 대신 사람이고, 수주 대신 매칭·세션이다. 대체 후보는 온톨로지 근접 특기
    폐쇄로 찾는다(같은 특기 완전일치만 보면 대체 가능한 사람을 놓친다).
    """
    cg = f"<{ID_BASE}Caregiver/{int(caregiver_id)}>"
    info = _rows(f"""{_PRE}
        SELECT ?name ?status ?grade ?rating ?lat ?lng ?sessions {_BOTH} WHERE {{
          {cg} a care:Caregiver ; care:status ?status .
          OPTIONAL {{ {cg} care:displayName ?name }} OPTIONAL {{ {cg} care:gradeLevel ?grade }}
          OPTIONAL {{ {cg} care:ratingAvg ?rating }} OPTIONAL {{ {cg} care:lat ?lat }}
          OPTIONAL {{ {cg} care:lng ?lng }} OPTIONAL {{ {cg} care:completedSessions ?sessions }} }}""")
    if not info:
        return {"found": False}
    i = info[0]

    specialties = [r["label"] for r in _rows(f"""{_PRE}
        SELECT ?label {_BOTH} WHERE {{ {cg} care:hasSpecialty ?sp . ?sp rdfs:label ?label }}""")]

    # 담당 매칭 — 대상자·일정·상태. 대상자 이름은 그래프에 마스킹본만 있다.
    matches = [{
        "match_id": int(_local(r["m"])), "status": r.get("status"),
        "scheduled_start": r.get("start"), "recipient": r.get("rname") or "—",
        "recipient_kind": r.get("kind"), "domain": _local(r["domain"]) if r.get("domain") else None,
    } for r in _rows(f"""{_PRE}
        SELECT ?m ?status ?start ?rname ?kind ?domain {_BOTH} WHERE {{
          ?m a care:Match ; care:assignedTo {cg} ; care:fulfills ?mr .
          OPTIONAL {{ ?m care:status ?status }} OPTIONAL {{ ?m care:scheduledStart ?start }}
          OPTIONAL {{ ?mr care:inDomain ?domain }}
          OPTIONAL {{ ?mr care:forRecipient ?r .
                     OPTIONAL {{ ?r care:displayName ?rname }} OPTIONAL {{ ?r care:recipientKind ?kind }} }}
        }} ORDER BY ?start""")]

    sessions = [{
        "session_id": int(_local(r["s"])), "status": r.get("status"),
        "scheduled_start": r.get("start"), "review_status": r.get("review"),
    } for r in _rows(f"""{_PRE}
        SELECT ?s ?status ?start ?review {_BOTH} WHERE {{
          ?s a care:CareSession ; care:ofMatch ?m . ?m care:assignedTo {cg} .
          OPTIONAL {{ ?s care:status ?status }} OPTIONAL {{ ?s care:scheduledStart ?start }}
          OPTIONAL {{ ?s care:reviewStatus ?review }} }} ORDER BY ?start""")]

    # 담당 대상자들의 질병 — 대체 인력이 갖춰야 할 요건의 근거.
    diseases = sorted({r["label"] for r in _rows(f"""{_PRE}
        SELECT DISTINCT ?label {_BOTH} WHERE {{
          ?m a care:Match ; care:assignedTo {cg} ; care:fulfills ?mr .
          ?mr care:forRecipient ?r . ?r care:hasDisease ?d . ?d rdfs:label ?label }}""")})

    lat = float(i["lat"]) if i.get("lat") else None
    lng = float(i["lng"]) if i.get("lng") else None
    alternatives = []
    for r in _rows(f"""{_PRE}
        SELECT ?alt ?name ?rating ?lat ?lng (COUNT(DISTINCT ?sp) AS ?shared) {_BOTH} WHERE {{
          {cg} care:hasSpecialty ?req .
          {_CLOSURE.format()}
          ?alt a care:Caregiver ; care:hasSpecialty ?sp ; care:status "active" .
          FILTER(?alt != {cg})
          OPTIONAL {{ ?alt care:displayName ?name }} OPTIONAL {{ ?alt care:ratingAvg ?rating }}
          OPTIONAL {{ ?alt care:lat ?lat }} OPTIONAL {{ ?alt care:lng ?lng }}
        }} GROUP BY ?alt ?name ?rating ?lat ?lng ORDER BY DESC(?shared) LIMIT 10"""):
        dist = None
        if lat is not None and lng is not None and r.get("lat") and r.get("lng"):
            # 좌표는 소수 2자리로 반올림돼 있다(개인정보 정책) — 거리도 그 정밀도까지만 의미 있다.
            dist = round(((float(r["lat"]) - lat) ** 2 + ((float(r["lng"]) - lng) * 0.79) ** 2) ** 0.5 * 111, 1)
        alternatives.append({"id": int(_local(r["alt"])), "name": r.get("name") or "—",
                             "rating": float(r["rating"]) if r.get("rating") else None,
                             "shared_specialties": int(r["shared"]), "distance_km": dist})

    return {
        "found": True,
        "caregiver": {"id": int(caregiver_id), "name": i.get("name") or "—",
                      "status": i["status"], "grade": int(i["grade"]) if i.get("grade") else None,
                      "rating": float(i["rating"]) if i.get("rating") else None,
                      "completed_sessions": int(i["sessions"]) if i.get("sessions") else 0,
                      "specialties": sorted(specialties)},
        "matches": matches,
        "sessions": sessions,
        "recipient_diseases": diseases,
        "alternatives": alternatives,
    }
