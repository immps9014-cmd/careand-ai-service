#!/usr/bin/env python
"""
careand_platform(MySQL) → 온톨로지 N-Triples 추출기.

  실행: ./venv/bin/python ontology/etl_caren.py     (보통은 ontology/load.sh 가 부른다)
  산출: ontology/out/caren.nt  → Fuseki 그래프 http://caren.aiclaude.kr/graph/caren

설계(3사 MES 온톨로지 TX-ONT-DESIGN 과 같은 방침):
  · **DB 가 SSOT, 그래프는 읽기 투영이다.** 이 스크립트는 SELECT 만 한다.
  · 그래프는 스냅샷이다 — 언제 것인지는 reload.cron.sh 가 out/status.json 에 남긴다.
  · 스키마(클래스·관계·어휘)는 care-domain.ttl 이 SSOT 다. 코드값→개체 IRI 매핑은
    **Fuseki 의 schema 그래프에서 읽어온다** — TTL 을 여기서 다시 적는 순간 둘이 어긋난다.

caren 고유 제약(3사엔 없던 것):
  · **개인정보 미적재.** Fuseki 는 인증 없이 localhost 에 열려 있고 moai·kcro·tx-mes 와
    한 인스턴스를 공유한다. 이름·연락처·주소원문·면허번호·암호화컬럼·STT 전사본문은
    적재하지 않는다. 이름은 마스킹(김○○), 좌표는 소수 2자리(≈1.1km)로 반올림한다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "caren.nt")

ONT = "http://caren.aiclaude.kr/ontology#"
IDB = "http://caren.aiclaude.kr/id/"
GRAPH_SCHEMA = "http://caren.aiclaude.kr/graph/schema"

FUSEKI = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
DATASET = os.environ.get("FUSEKI_DATASET", "caren")

XSD = "http://www.w3.org/2001/XMLSchema#"

warnings: list[str] = []


# ─────────────────────────── 환경 ───────────────────────────

def load_dotenv() -> None:
    """ai-service 의 .env 를 읽는다(train_l2r.py 와 같은 방식)."""
    path = os.path.join(os.path.dirname(HERE), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def connect():
    import pymysql
    load_dotenv()
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "careand"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "careand_platform"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


# ─────────────────────────── 트리플 출력 ───────────────────────────

_lines: list[str] = []


def _esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def iri(cls: str, key) -> str:
    return f"<{IDB}{cls}/{urllib.parse.quote(str(key), safe='')}>"


def o(term: str) -> str:
    return f"<{ONT}{term}>"


def emit(s: str, p: str, obj: str) -> None:
    _lines.append(f"{s} {o(p)} {obj} .")


def _typed(v, dt: str) -> str:
    return f'"{_esc(str(v))}"^^<{XSD}{dt}>'


def lit(v) -> str | None:
    """파이썬 값 → N-Triples 리터럴. None/빈문자열은 None(=술어를 안 쓴다)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return _typed("true" if v else "false", "boolean")
    if isinstance(v, int):
        return _typed(v, "integer")
    if isinstance(v, Decimal):
        return _typed(v, "decimal")
    if isinstance(v, float):
        return _typed(repr(v), "decimal")
    if isinstance(v, datetime):
        return _typed(v.strftime("%Y-%m-%dT%H:%M:%S"), "dateTime")
    if isinstance(v, date):
        return _typed(v.isoformat(), "date")
    s = str(v).strip()
    return f'"{_esc(s)}"' if s else None


def put(subj: str, prop: str, v) -> None:
    """값이 있을 때만 술어를 단다 — 빈 값을 리터럴로 싣지 않는다."""
    t = lit(v)
    if t is not None:
        emit(subj, prop, t)


def put_bool(subj: str, prop: str, v) -> None:
    if v is not None:
        emit(subj, prop, _typed("true" if int(v) else "false", "boolean"))


# ─────────────────────────── 값 변환 ───────────────────────────

def jsoncol(v):
    """longtext JSON 컬럼 → 파이썬 값(깨진 값은 빈 리스트)."""
    if v is None or v == "":
        return []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def mask_name(name: str | None) -> str | None:
    """'김수진' → '김○○'. 원문 이름은 그래프에 싣지 않는다."""
    if not name:
        return None
    name = name.strip()
    if len(name) <= 1:
        return name
    return name[0] + "○" * (len(name) - 1)


def round_coord(v):
    """좌표 반올림(소수 2자리 ≈ 1.1km). 집 주소를 그래프로 특정할 수 없게 한다."""
    if v is None:
        return None
    return Decimal(v).quantize(Decimal("0.01"))


def birth_year(v):
    return v.year if isinstance(v, (date, datetime)) else None


# ─────────────────────────── 스키마 어휘 로딩 ───────────────────────────

def sparql(query: str) -> list[dict]:
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"{FUSEKI}/{DATASET}/sparql", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["results"]["bindings"]


def load_vocab() -> dict[str, dict[str, str]]:
    """schema 그래프의 care:code → IRI 매핑을 클래스별로 읽는다.

    ⚠ TTL 을 여기에 베껴 적지 않는 이유: 어휘가 두 군데 있으면 반드시 어긋난다.
      load.sh 가 스키마를 먼저 PUT 한 뒤 이 스크립트를 부르므로 항상 최신이다.
    """
    rows = sparql(f"""
        PREFIX care: <{ONT}>
        SELECT ?s ?c ?t WHERE {{ GRAPH <{GRAPH_SCHEMA}> {{ ?s care:code ?c ; a ?t }} }}
    """)
    vocab: dict[str, dict[str, str]] = {}
    for r in rows:
        cls = r["t"]["value"].rsplit("#", 1)[-1]
        vocab.setdefault(cls, {})[r["c"]["value"]] = f"<{r['s']['value']}>"
    if not vocab.get("Specialty"):
        sys.exit("schema 그래프에 어휘가 없다 — load.sh 가 care-domain.ttl 을 먼저 적재해야 한다")
    return vocab


def load_term_labels() -> dict[str, str]:
    """CareTerm·Disease 라벨 → IRI (가격 규칙의 중증도 용어를 잇는 데 쓴다)."""
    rows = sparql(f"""
        PREFIX care: <{ONT}>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?s ?l WHERE {{ GRAPH <{GRAPH_SCHEMA}> {{
            {{ ?s a ?cls . ?cls rdfs:subClassOf* care:CareTerm }} UNION {{ ?s a care:Disease }}
            ?s rdfs:label ?l }} }}
    """)
    return {r["l"]["value"]: f"<{r['s']['value']}>" for r in rows}


def link_codes(subj: str, prop: str, values, table: dict[str, str], what: str, who) -> None:
    """DB 문자열 목록 → 어휘 개체 링크. 매핑 안 되는 값은 경고로 남긴다.

    조용히 흘리면 '온톨로지가 붙어는 있는데 아무것도 안 걸리는' 상태가 된다
    (2026-07-30 PoC 가 정확히 그랬다). check.py 가 이걸 FAIL 로 잡는다.
    """
    seen = set()
    for v in values or []:
        if not isinstance(v, str):
            continue
        key = v.strip()
        target = table.get(key)
        if target is None:
            warnings.append(f"{what} 미등록 값 '{key}' ({who}) — care-domain.ttl 에 care:code 추가 필요")
            continue
        if target not in seen:
            emit(subj, prop, target)
            seen.add(target)


# ─────────────────────────── 추출 ───────────────────────────

def main() -> None:
    conn = connect()
    vocab = load_vocab()
    SPEC = vocab.get("Specialty", {})
    DISEASE = vocab.get("Disease", {})
    DOMAIN = vocab.get("ServiceDomain", {})
    ACTIVITY = vocab.get("ActivityCategory", {})
    INTENT = vocab.get("GuardianIntent", {})
    TERMS = load_term_labels()

    counts: dict[str, int] = {}

    RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"

    def new(cls: str, key, table: str, db_id=None) -> str:
        s = iri(cls, key)
        _lines.append(f"{s} {RDF_TYPE} {o(cls)} .")
        emit(s, "sourceTable", lit(table))
        if db_id is not None:
            put(s, "dbId", int(db_id))
        counts[cls] = counts.get(cls, 0) + 1
        return s

    with conn.cursor() as cur:
        # 이름은 users 에서 가져와 마스킹해서만 쓴다(users 자체는 객체로 만들지 않는다).
        cur.execute("SELECT id, name FROM users")
        user_name = {r["id"]: mask_name(r["name"]) for r in cur.fetchall()}

        # ── 기관 · 지점 ────────────────────────────────────────────────────
        cur.execute("SELECT id, name, biz_type, status FROM organizations")
        for r in cur.fetchall():
            s = new("Organization", r["id"], "organizations", r["id"])
            put(s, "displayName", r["name"])   # 법인명은 개인정보가 아니다
            put(s, "status", r["status"])

        cur.execute("SELECT id, code, name, type, parent_branch_id, region_code, status FROM branches")
        branch_key = {}
        for r in cur.fetchall():
            key = r["code"] or f"id-{r['id']}"
            branch_key[r["id"]] = key
            s = new("Branch", key, "branches", r["id"])
            put(s, "displayName", r["name"])
            put(s, "code", r["code"])
            put(s, "status", r["status"])
            put(s, "regionCode", r["region_code"])
        cur.execute("SELECT id, parent_branch_id FROM branches WHERE parent_branch_id IS NOT NULL")
        for r in cur.fetchall():
            if r["parent_branch_id"] in branch_key:
                emit(iri("Branch", branch_key[r["id"]]), "parentBranch",
                     iri("Branch", branch_key[r["parent_branch_id"]]))

        # ── 서비스 카테고리(자연키 = code) · 가격 규칙 ──────────────────────
        cur.execute("SELECT id, code, domain, name, base_rate, is_active FROM service_categories")
        cat_key = {}
        for r in cur.fetchall():
            cat_key[r["id"]] = r["code"]
            s = new("ServiceCategory", r["code"], "service_categories", r["id"])
            put(s, "code", r["code"])
            put(s, "displayName", r["name"])
            put(s, "baseRate", r["base_rate"])
            put_bool(s, "isValid", r["is_active"])
            if r["domain"] in DOMAIN:
                emit(s, "inDomain", DOMAIN[r["domain"]])
            else:
                warnings.append(f"카테고리 {r['code']} 의 도메인 '{r['domain']}' 이 어휘에 없다")

        cur.execute("SELECT id, category_id, region_code, region_index, night_mult, holiday_mult,"
                    " emergency_mult, acuity_addons, min_hourly, is_active FROM pricing_rules")
        for r in cur.fetchall():
            s = new("PricingRule", r["id"], "pricing_rules", r["id"])
            if r["category_id"] in cat_key:
                emit(s, "appliesToCategory", iri("ServiceCategory", cat_key[r["category_id"]]))
            put(s, "regionCode", r["region_code"])
            put(s, "regionIndex", r["region_index"])
            put(s, "nightMult", r["night_mult"])
            put(s, "holidayMult", r["holiday_mult"])
            put(s, "emergencyMult", r["emergency_mult"])
            put(s, "minHourly", r["min_hourly"])
            put_bool(s, "isValid", r["is_active"])
            # 가산 대상 용어 — 가격이 쓰는 어휘와 케어일지가 쓰는 어휘가 같은지 여기서 드러난다.
            link_codes(s, "acuityTerm", list(jsoncol(r["acuity_addons"]) or {}), TERMS,
                       "중증도 가산 용어", f"pricing_rules#{r['id']}")

        # ── 방문지 ─────────────────────────────────────────────────────────
        cur.execute("SELECT id, lat, lng, dwelling_type FROM service_addresses WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            s = new("ServiceAddress", r["id"], "service_addresses", r["id"])
            put(s, "lat", round_coord(r["lat"]))
            put(s, "lng", round_coord(r["lng"]))
            put(s, "dwellingType", r["dwelling_type"])

        # ── 돌봄전문가 ─────────────────────────────────────────────────────
        cur.execute("SELECT id, user_id, org_id, birth_date, gender, specialties, base_lat, base_lng,"
                    " branch_id, service_domains, career_track, mentor_caregiver_id, rating_avg,"
                    " rating_count, completed_sessions, grade_level, default_rate, auto_bid, status,"
                    " created_at FROM caregivers WHERE deleted_at IS NULL")
        caregivers = cur.fetchall()
        for r in caregivers:
            s = new("Caregiver", r["id"], "caregivers", r["id"])
            put(s, "displayName", user_name.get(r["user_id"]))
            put(s, "gender", r["gender"])
            put(s, "birthYear", birth_year(r["birth_date"]))
            put(s, "lat", round_coord(r["base_lat"]))
            put(s, "lng", round_coord(r["base_lng"]))
            put(s, "status", r["status"])
            put(s, "careerTrack", r["career_track"])
            put(s, "gradeLevel", r["grade_level"])
            put(s, "ratingAvg", r["rating_avg"])
            put(s, "ratingCount", r["rating_count"])
            put(s, "completedSessions", r["completed_sessions"])
            put(s, "defaultRate", r["default_rate"])
            put_bool(s, "autoBid", r["auto_bid"])
            put(s, "createdAt", r["created_at"])
            if r["org_id"]:
                emit(s, "employedBy", iri("Organization", r["org_id"]))
            if r["branch_id"] in branch_key:
                emit(s, "atBranch", iri("Branch", branch_key[r["branch_id"]]))
            # service_domains 는 SET 컬럼이라 콤마 문자열로 온다.
            link_codes(s, "servesDomain", str(r["service_domains"] or "").split(","), DOMAIN,
                       "서비스 도메인", f"caregivers#{r['id']}")
            # ★ 매칭의 핵심 연결 — 문자열 특기를 어휘 개체로 잇는다.
            link_codes(s, "hasSpecialty", jsoncol(r["specialties"]), SPEC,
                       "특기", f"caregivers#{r['id']}")
        alive = {r["id"] for r in caregivers}
        for r in caregivers:
            if r["mentor_caregiver_id"] in alive:
                emit(iri("Caregiver", r["id"]), "mentoredBy", iri("Caregiver", r["mentor_caregiver_id"]))

        # ── 보호자 ─────────────────────────────────────────────────────────
        cur.execute("SELECT id, user_id, relation, intent, created_at FROM guardians")
        guardians = cur.fetchall()
        guardian_by_user = {r["user_id"]: r["id"] for r in guardians}
        for r in guardians:
            s = new("Guardian", r["id"], "guardians", r["id"])
            put(s, "displayName", user_name.get(r["user_id"]))
            put(s, "relation", r["relation"])
            put(s, "createdAt", r["created_at"])
            link_codes(s, "hasIntent", [r["intent"]], INTENT, "보호자 의도", f"guardians#{r['id']}")

        # ── 돌봄대상자 — 세 테이블을 한 클래스로(care:recipientKind 로 구분) ──
        recipient_keys: set[str] = set()

        cur.execute("SELECT id, guardian_id, name, birth_date, gender, care_grade, diseases,"
                    " home_lat, home_lng FROM seniors WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            s = new("Recipient", f"senior-{r['id']}", "seniors", r["id"])
            recipient_keys.add(f"senior-{r['id']}")
            put(s, "recipientKind", "senior")
            put(s, "displayName", mask_name(r["name"]))
            put(s, "gender", r["gender"])
            put(s, "birthYear", birth_year(r["birth_date"]))
            put(s, "careGrade", r["care_grade"])
            put(s, "lat", round_coord(r["home_lat"]))
            put(s, "lng", round_coord(r["home_lng"]))
            link_codes(s, "hasDisease", jsoncol(r["diseases"]), DISEASE, "질병", f"seniors#{r['id']}")
            if r["guardian_id"]:
                emit(iri("Guardian", r["guardian_id"]), "guardianOf", s)

        cur.execute("SELECT id, guardian_id, name, birth_date, gender, diseases, mobility,"
                    " hospital_lat, hospital_lng FROM nursing_patients WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            s = new("Recipient", f"nursing-{r['id']}", "nursing_patients", r["id"])
            recipient_keys.add(f"nursing-{r['id']}")
            put(s, "recipientKind", "nursing_patient")
            put(s, "displayName", mask_name(r["name"]))
            put(s, "gender", r["gender"])
            put(s, "birthYear", birth_year(r["birth_date"]))
            put(s, "lat", round_coord(r["hospital_lat"]))
            put(s, "lng", round_coord(r["hospital_lng"]))
            link_codes(s, "hasDisease", jsoncol(r["diseases"]), DISEASE, "질병",
                       f"nursing_patients#{r['id']}")
            if r["guardian_id"]:
                emit(iri("Guardian", r["guardian_id"]), "guardianOf", s)

        # 산후는 본인형이라 보호자가 따로 없다 — 같은 user_id 의 guardian 행이 본인이다.
        cur.execute("SELECT id, user_id, name, birth_date, region_code, branch_id, voucher_grade,"
                    " status FROM postpartum_clients WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            s = new("Recipient", f"postpartum-{r['id']}", "postpartum_clients", r["id"])
            recipient_keys.add(f"postpartum-{r['id']}")
            put(s, "recipientKind", "postpartum_client")
            put(s, "displayName", mask_name(r["name"]))
            put(s, "birthYear", birth_year(r["birth_date"]))
            put(s, "regionCode", r["region_code"])
            put(s, "status", r["status"])
            if r["branch_id"] in branch_key:
                emit(s, "atBranch", iri("Branch", branch_key[r["branch_id"]]))
            gid = guardian_by_user.get(r["user_id"])
            if gid:
                emit(iri("Guardian", gid), "guardianOf", s)

        # ── 매칭 요청 ──────────────────────────────────────────────────────
        cur.execute("SELECT id, guardian_id, senior_id, postpartum_client_id, nursing_patient_id,"
                    " service_address_id, category_id, mode, service_domain, scheduled_start,"
                    " duration_min, requirements, budget_hourly, status, created_at FROM match_requests")
        for r in cur.fetchall():
            s = new("MatchRequest", r["id"], "match_requests", r["id"])
            emit(s, "requestedBy", iri("Guardian", r["guardian_id"]))
            # 대상자 컬럼이 도메인마다 다르다 — MatchRequest::recipientFeatures() 와 같은 추상화.
            # ⚠ 소프트삭제된 대상자를 가리키는 요청이 있다(실측: postpartum_client_id=3,
            #   deleted_at 2026-07-03). 그런 참조는 링크하지 않는다 — 링크만 만들고 객체가
            #   없으면 질의가 조용히 0 건을 돌려준다. check.py 의 '끊긴 참조=0' 이 이걸 지킨다.
            for col, kind in (("senior_id", "senior"), ("nursing_patient_id", "nursing"),
                              ("postpartum_client_id", "postpartum")):
                if r[col]:
                    key = f"{kind}-{r[col]}"
                    if key in recipient_keys:
                        emit(s, "forRecipient", iri("Recipient", key))
                    else:
                        warnings.append(
                            f"요청 #{r['id']} 의 대상자 {key} 가 없다(소프트삭제 추정) — 링크 생략")
                    break
            if r["service_address_id"]:
                emit(s, "atAddress", iri("ServiceAddress", r["service_address_id"]))
            if r["category_id"] in cat_key:
                emit(s, "ofCategory", iri("ServiceCategory", cat_key[r["category_id"]]))
            link_codes(s, "inDomain", [r["service_domain"]], DOMAIN, "서비스 도메인",
                       f"match_requests#{r['id']}")
            put(s, "urgency", r["mode"])
            put(s, "scheduledStart", r["scheduled_start"])
            put(s, "durationMin", r["duration_min"])
            put(s, "budgetHourly", r["budget_hourly"])
            put(s, "status", r["status"])
            put(s, "createdAt", r["created_at"])
            req = jsoncol(r["requirements"])
            if isinstance(req, dict):
                put(s, "requiresGender", req.get("preferred_gender"))

        # ── 매칭 후보 · 성사 ───────────────────────────────────────────────
        cur.execute("SELECT id, request_id, caregiver_id, source, ai_score, bid_hourly, bid_status,"
                    " `rank`, response, created_at FROM match_candidates")
        for r in cur.fetchall():
            s = new("MatchCandidate", r["id"], "match_candidates", r["id"])
            emit(s, "candidateFor", iri("MatchRequest", r["request_id"]))
            if r["caregiver_id"] in alive:
                emit(s, "proposes", iri("Caregiver", r["caregiver_id"]))
            put(s, "candidateSource", r["source"])
            put(s, "aiScore", r["ai_score"])
            put(s, "rank", r["rank"])
            put(s, "bidHourly", r["bid_hourly"])
            put(s, "bidStatus", r["bid_status"])
            put(s, "response", r["response"])
            put(s, "createdAt", r["created_at"])

        cur.execute("SELECT id, request_id, caregiver_id, scheduled_start, scheduled_end,"
                    " hourly_rate, estimated_amount, status, is_manual, created_at FROM matches")
        for r in cur.fetchall():
            s = new("Match", r["id"], "matches", r["id"])
            emit(s, "fulfills", iri("MatchRequest", r["request_id"]))
            if r["caregiver_id"] in alive:
                emit(s, "assignedTo", iri("Caregiver", r["caregiver_id"]))
            put(s, "scheduledStart", r["scheduled_start"])
            put(s, "scheduledEnd", r["scheduled_end"])
            put(s, "hourlyRate", r["hourly_rate"])
            put(s, "amount", r["estimated_amount"])
            put(s, "status", r["status"])
            put_bool(s, "isManual", r["is_manual"])
            put(s, "createdAt", r["created_at"])

        # ── 세션 · 출퇴근 · 음성일지 · 요약 ────────────────────────────────
        cur.execute("SELECT id, match_id, scheduled_start, scheduled_end, actual_start, actual_end,"
                    " duration_min, status, review_status, created_at FROM care_sessions")
        for r in cur.fetchall():
            s = new("CareSession", r["id"], "care_sessions", r["id"])
            emit(s, "ofMatch", iri("Match", r["match_id"]))
            put(s, "scheduledStart", r["scheduled_start"])
            put(s, "scheduledEnd", r["scheduled_end"])
            put(s, "actualStart", r["actual_start"])
            put(s, "actualEnd", r["actual_end"])
            put(s, "durationMin", r["duration_min"])
            put(s, "status", r["status"])
            put(s, "reviewStatus", r["review_status"])
            put(s, "createdAt", r["created_at"])

        cur.execute("SELECT id, session_id, event_type, lat, lng, distance_m, is_valid, logged_at"
                    " FROM attendance_logs")
        for r in cur.fetchall():
            s = new("AttendanceEvent", r["id"], "attendance_logs", r["id"])
            emit(s, "ofSession", iri("CareSession", r["session_id"]))
            put(s, "eventType", r["event_type"])
            put(s, "lat", round_coord(r["lat"]))
            put(s, "lng", round_coord(r["lng"]))
            put(s, "distanceM", r["distance_m"])
            put_bool(s, "isValid", r["is_valid"])
            put(s, "loggedAt", r["logged_at"])

        # ⚠ stt_text(전사 본문)는 적재하지 않는다 — 대상자 건강정보다.
        cur.execute("SELECT id, session_id, duration_sec, stt_confidence, status, created_at"
                    " FROM voice_logs")
        for r in cur.fetchall():
            s = new("VoiceLog", r["id"], "voice_logs", r["id"])
            emit(s, "ofSession", iri("CareSession", r["session_id"]))
            put(s, "durationSec", r["duration_sec"])
            put(s, "sttConfidence", r["stt_confidence"])
            put(s, "status", r["status"])
            put(s, "createdAt", r["created_at"])

        # ⚠ guardian_version/medical_version(요약 본문)도 적재하지 않는다 — 분류 키만.
        cur.execute("SELECT id, session_id, voice_log_id, categorized, confidence, llm_model,"
                    " generated_at FROM ai_log_summaries")
        for r in cur.fetchall():
            s = new("LogSummary", r["id"], "ai_log_summaries", r["id"])
            if r["session_id"]:
                emit(s, "ofSession", iri("CareSession", r["session_id"]))
            if r["voice_log_id"]:
                emit(s, "fromVoiceLog", iri("VoiceLog", r["voice_log_id"]))
            put(s, "confidence", r["confidence"])
            put(s, "llmModel", r["llm_model"])
            put(s, "createdAt", r["generated_at"])
            cats = jsoncol(r["categorized"])
            link_codes(s, "categorized", list(cats) if isinstance(cats, dict) else cats,
                       ACTIVITY, "활동 분류", f"ai_log_summaries#{r['id']}")

        # ── 후기 · 결제 ────────────────────────────────────────────────────
        cur.execute("SELECT id, match_id, reviewer_role, rating, created_at FROM reviews")
        for r in cur.fetchall():
            s = new("Review", r["id"], "reviews", r["id"])
            emit(s, "ofMatch", iri("Match", r["match_id"]))
            put(s, "reviewerRole", r["reviewer_role"])
            put(s, "rating", r["rating"])
            put(s, "createdAt", r["created_at"])

        cur.execute("SELECT id, guardian_id, match_id, total_amount, amount_self_pay, amount_ltc_pay,"
                    " status, created_at FROM payments")
        for r in cur.fetchall():
            s = new("Payment", r["id"], "payments", r["id"])
            if r["match_id"]:
                emit(s, "ofMatch", iri("Match", r["match_id"]))
            if r["guardian_id"]:
                emit(s, "paidBy", iri("Guardian", r["guardian_id"]))
            put(s, "amount", r["total_amount"])
            put(s, "amountSelfPay", r["amount_self_pay"])
            put(s, "amountLtcPay", r["amount_ltc_pay"])
            put(s, "status", r["status"])
            put(s, "createdAt", r["created_at"])

        cur.execute("SELECT id, payment_id, item_type, amount FROM payment_items")
        for r in cur.fetchall():
            s = new("PaymentItem", r["id"], "payment_items", r["id"])
            emit(s, "ofPayment", iri("Payment", r["payment_id"]))
            put(s, "itemType", r["item_type"])
            put(s, "amount", r["amount"])

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")

    total = len(_lines)
    print(f"추출 완료: {OUT}")
    print(f"  트리플 {total:,} · 객체 {sum(counts.values()):,}")
    for k in sorted(counts, key=lambda x: -counts[x]):
        print(f"    {k:<18} {counts[k]:>6,}")
    if warnings:
        uniq = sorted(set(warnings))
        print(f"  경고 {len(uniq)}종:")
        for w in uniq[:20]:
            print(f"    WARN {w}")
        if len(uniq) > 20:
            print(f"    … 외 {len(uniq) - 20}종")


if __name__ == "__main__":
    main()
