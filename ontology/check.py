#!/usr/bin/env python
"""
적재 후 불변식 점검 — 그래프를 믿어도 되는지 판정한다.

  실행: ./venv/bin/python ontology/check.py [--quiet]
  종료코드: 0 통과 / 1 FAIL 있음  (WARN 은 원천 DB 의 상태 보고일 뿐 실패가 아니다)

보는 것 (3사 MES check.php 와 같은 구성 + caren 고유 두 가지):
  A. 개체 수    — ETL 과 **같은 필터**로 센 DB 행 수와 그래프 객체 수가 같은가
  B. 필수 링크  — 없으면 질의가 조용히 틀리는 연결이 빠지지 않았나
  C. 구조       — 한 IRI 가 두 클래스를 갖지 않는가 / 스키마에 없는 술어를 쓰지 않는가
  D. 어휘 정합  — **caren 고유.** DB 문자열이 전부 어휘 개체에 걸리는가.
                 2026-07-30 PoC 는 여기가 깨져 있었고(특기 22종 중 1종만 일치) 그래서
                 ontology_match 가 구조적으로 0 이었다. 그걸 다시 놓치지 않기 위한 항목이다.
  E. 개인정보   — **caren 고유.** 이름·전화·이메일 원문이 그래프에 실리지 않았는가.
                 Fuseki 는 인증 없이 열려 있고 다른 테넌트와 한 인스턴스를 공유한다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from etl_caren import (ONT, connect, jsoncol, load_term_labels,  # noqa: E402
                       load_vocab, sparql)

GRAPH_DATA = "http://caren.aiclaude.kr/graph/caren"
GRAPH_SCHEMA = "http://caren.aiclaude.kr/graph/schema"
PREFIX = f"PREFIX care: <{ONT}>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"

quiet = "--quiet" in sys.argv[1:]
rows: list[tuple[str, str, str, str, str]] = []
fails = 0
warns = 0


def scalar(q: str, var: str = "n") -> int:
    b = sparql(PREFIX + q)
    return int(b[0][var]["value"]) if b else 0


def in_graph(cls: str) -> int:
    return scalar(f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s a care:{cls} }} }}")


def check(name: str, expected, actual, note: str = "") -> None:
    """적재 무결성 — 깨지면 그래프를 믿을 수 없다."""
    global fails
    ok = expected == actual
    if not ok:
        fails += 1
    rows.append(("OK " if ok else "FAIL", name, str(expected), str(actual), note))


def warn(name: str, expected, actual, note: str = "") -> None:
    """데이터 품질 — 원천 DB 의 상태 보고. 종료코드에 영향 없음."""
    global warns
    ok = expected == actual
    if not ok:
        warns += 1
    rows.append(("OK " if ok else "WARN", name, str(expected), str(actual), note))


def main() -> int:
    conn = connect()
    vocab = load_vocab()
    SPEC, DISEASE = vocab.get("Specialty", {}), vocab.get("Disease", {})
    DOMAIN, ACTIVITY = vocab.get("ServiceDomain", {}), vocab.get("ActivityCategory", {})
    INTENT, TERMS = vocab.get("GuardianIntent", {}), load_term_labels()

    def db(sql: str) -> int:
        with conn.cursor() as cur:
            cur.execute(sql)
            return int(list(cur.fetchone().values())[0])

    def col(sql: str) -> list:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [list(r.values())[0] for r in cur.fetchall()]

    # ── A. 개체 수 (ETL 과 같은 필터) ───────────────────────────────────────
    check("Organization", db("SELECT COUNT(*) FROM organizations"), in_graph("Organization"))
    check("Branch", db("SELECT COUNT(*) FROM branches"), in_graph("Branch"))
    check("ServiceCategory", db("SELECT COUNT(*) FROM service_categories"), in_graph("ServiceCategory"))
    check("PricingRule", db("SELECT COUNT(*) FROM pricing_rules"), in_graph("PricingRule"))
    check("ServiceAddress", db("SELECT COUNT(*) FROM service_addresses WHERE deleted_at IS NULL"),
          in_graph("ServiceAddress"))
    check("Caregiver", db("SELECT COUNT(*) FROM caregivers WHERE deleted_at IS NULL"), in_graph("Caregiver"))
    check("Guardian", db("SELECT COUNT(*) FROM guardians"), in_graph("Guardian"))
    check("Recipient(3테이블 통합)",
          db("SELECT COUNT(*) FROM seniors WHERE deleted_at IS NULL")
          + db("SELECT COUNT(*) FROM nursing_patients WHERE deleted_at IS NULL")
          + db("SELECT COUNT(*) FROM postpartum_clients WHERE deleted_at IS NULL"),
          in_graph("Recipient"), "seniors + nursing_patients + postpartum_clients")
    check("MatchRequest", db("SELECT COUNT(*) FROM match_requests"), in_graph("MatchRequest"))
    check("MatchCandidate", db("SELECT COUNT(*) FROM match_candidates"), in_graph("MatchCandidate"))
    check("Match", db("SELECT COUNT(*) FROM matches"), in_graph("Match"))
    check("CareSession", db("SELECT COUNT(*) FROM care_sessions"), in_graph("CareSession"))
    check("AttendanceEvent", db("SELECT COUNT(*) FROM attendance_logs"), in_graph("AttendanceEvent"))
    check("VoiceLog", db("SELECT COUNT(*) FROM voice_logs"), in_graph("VoiceLog"))
    check("LogSummary", db("SELECT COUNT(*) FROM ai_log_summaries"), in_graph("LogSummary"))
    check("Review", db("SELECT COUNT(*) FROM reviews"), in_graph("Review"))
    check("Payment", db("SELECT COUNT(*) FROM payments"), in_graph("Payment"))
    check("PaymentItem", db("SELECT COUNT(*) FROM payment_items"), in_graph("PaymentItem"))

    # ── B. 필수 링크 누락 ──────────────────────────────────────────────────
    def missing(cls: str, prop: str) -> int:
        return scalar(f"""SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?s a care:{cls} . FILTER NOT EXISTS {{ ?s care:{prop} ?o }} }} }}""")

    for cls, prop, note in [
        ("MatchRequest", "requestedBy", "요청자 없는 요청"),
        ("MatchRequest", "inDomain", ""),
        ("MatchCandidate", "candidateFor", ""),
        ("MatchCandidate", "proposes", "삭제된 인력을 가리키는 후보면 끊긴다"),
        ("Match", "fulfills", ""),
        ("Match", "assignedTo", ""),
        ("CareSession", "ofMatch", ""),
        ("AttendanceEvent", "ofSession", ""),
        ("VoiceLog", "ofSession", ""),
        ("PaymentItem", "ofPayment", ""),
        ("ServiceCategory", "inDomain", ""),
        ("Recipient", "recipientKind", "대상자 종류 미표기"),
    ]:
        check(f"{cls} → {prop} 누락", 0, missing(cls, prop), note)

    # 대상자 링크는 조건부다 — 본인형 가사(living_support/housekeeping) 요청엔 대상자
    # 테이블이 없고 방문지(service_address)만 있다. 무조건 필수로 걸면 정상을 실패로 만든다.
    need_recipient = db("""SELECT COUNT(*) FROM match_requests r
        WHERE (r.senior_id IN (SELECT id FROM seniors WHERE deleted_at IS NULL)
            OR r.nursing_patient_id IN (SELECT id FROM nursing_patients WHERE deleted_at IS NULL)
            OR r.postpartum_client_id IN (SELECT id FROM postpartum_clients WHERE deleted_at IS NULL))""")
    check("MatchRequest → forRecipient", need_recipient, scalar(f"""
        SELECT (COUNT(DISTINCT ?r) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?r a care:MatchRequest ; care:forRecipient ?x }} }}"""),
          "살아있는 대상자를 가진 요청 수 = 링크 수")

    warn("대상자 레코드가 없는 요청", 0, db("""SELECT COUNT(*) FROM match_requests r
        WHERE (r.senior_id IS NOT NULL OR r.nursing_patient_id IS NOT NULL OR r.postpartum_client_id IS NOT NULL)
          AND COALESCE(r.senior_id,0) NOT IN (SELECT id FROM seniors WHERE deleted_at IS NULL)
          AND COALESCE(r.nursing_patient_id,0) NOT IN (SELECT id FROM nursing_patients WHERE deleted_at IS NULL)
          AND COALESCE(r.postpartum_client_id,0) NOT IN (SELECT id FROM postpartum_clients WHERE deleted_at IS NULL)"""),
         "소프트삭제된 대상자를 가리키는 요청 — ETL 이 링크를 생략한다")

    check("본인형(가사) 요청 → atAddress", db("""SELECT COUNT(*) FROM match_requests
        WHERE service_domain IN ('living_support','housekeeping') AND service_address_id IS NOT NULL"""),
          scalar(f"""SELECT (COUNT(DISTINCT ?r) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?r a care:MatchRequest ; care:atAddress ?a ; care:inDomain ?d .
            FILTER(?d IN (care:living_support, care:housekeeping)) }} }}"""),
          "가사 요청은 대상자 대신 방문지로 닫힌다")

    # ── C. 구조 불변식 ─────────────────────────────────────────────────────
    check("한 IRI 가 두 클래스", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?s a ?c1, ?c2 . FILTER(STR(?c1) < STR(?c2)) }} }}"""), "ID 공간 오염")

    check("스키마에 없는 술어", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?p) AS ?n) WHERE {{
            GRAPH <{GRAPH_DATA}> {{ ?s ?p ?o }}
            FILTER(?p != rdf:type)
            FILTER NOT EXISTS {{ GRAPH <{GRAPH_SCHEMA}> {{ ?p a ?decl }} }} }}"""),
          "care-domain.ttl 에 선언 안 된 술어를 ETL 이 쓰면 스키마가 거짓말이 된다")

    check("스키마에 없는 클래스", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?c) AS ?n) WHERE {{
            GRAPH <{GRAPH_DATA}> {{ ?s a ?c }}
            FILTER NOT EXISTS {{ GRAPH <{GRAPH_SCHEMA}> {{ ?c a ?decl }} }} }}"""))

    check("끊긴 참조(대상 객체 없는 링크)", 0, scalar(f"""
        SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s ?p ?o }}
            FILTER(isIRI(?o) && STRSTARTS(STR(?o), "http://caren.aiclaude.kr/id/"))
            FILTER NOT EXISTS {{ GRAPH <{GRAPH_DATA}> {{ ?o a ?c }} }} }}"""),
          "링크만 있고 객체가 없으면 질의가 조용히 0건을 돌려준다")

    check("업무객체가 역할상수 클래스를 갖지 않음", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s a care:Role }} }}"""),
          "care:Role 은 역할 상수(schema)의 클래스다 — 사람 객체가 여기 들어오면 개체 수가 어긋난다")

    # ── D. 어휘 정합 (caren 고유) ──────────────────────────────────────────
    def unmapped(values: list, table: dict[str, str], is_json: bool = True) -> list[str]:
        """DB 값 목록 중 어휘에 없는 것. is_json 이면 JSON 배열/객체를 풀어서 본다.

        ⚠ 여기서 jsoncol 을 빼먹으면 '["당뇨","고혈압"]' 이라는 문자열 통째가 한 값으로
          잡혀 전부 미등록으로 보인다(첫 실행에서 실제로 그랬다). ETL 의 link_codes 와
          같은 방식으로 풀어야 점검이 ETL 을 검증한다.
        """
        out = set()
        for v in values:
            items = jsoncol(v) if is_json else [v]
            if isinstance(items, dict):
                items = list(items)
            for item in items if isinstance(items, list) else []:
                if isinstance(item, str) and item.strip() and item.strip() not in table:
                    out.add(item.strip())
        return sorted(out)

    spec_raw = col("SELECT specialties FROM caregivers WHERE deleted_at IS NULL AND specialties IS NOT NULL")
    bad = unmapped(spec_raw, SPEC)
    check("어휘 미등록 특기", 0, len(bad), ", ".join(bad[:5]) or "caregivers.specialties 전수 일치")

    dis_raw = col("SELECT diseases FROM seniors WHERE deleted_at IS NULL AND diseases IS NOT NULL") + \
        col("SELECT diseases FROM nursing_patients WHERE deleted_at IS NULL AND diseases IS NOT NULL")
    bad = unmapped(dis_raw, DISEASE)
    check("어휘 미등록 질병", 0, len(bad), ", ".join(bad[:5]) or "seniors·nursing_patients 전수 일치")

    dom_raw = [d for v in col("SELECT service_domains FROM caregivers WHERE deleted_at IS NULL")
               for d in str(v or "").split(",") if d]
    bad = unmapped(dom_raw + col("SELECT DISTINCT service_domain FROM match_requests")
                   + col("SELECT DISTINCT domain FROM service_categories"), DOMAIN, is_json=False)
    check("어휘 미등록 서비스 도메인", 0, len(bad), ", ".join(bad[:5]))

    bad = unmapped(col("SELECT DISTINCT intent FROM guardians WHERE intent IS NOT NULL"), INTENT,
                   is_json=False)
    check("어휘 미등록 보호자 의도", 0, len(bad), ", ".join(bad[:5]))

    acuity = set()
    for v in col("SELECT acuity_addons FROM pricing_rules WHERE acuity_addons IS NOT NULL"):
        j = jsoncol(v)
        if isinstance(j, dict):
            acuity |= set(j)
    bad = sorted(t for t in acuity if t not in TERMS)
    check("어휘 미등록 중증도 가산 용어", 0, len(bad), ", ".join(bad[:5]) or "가격 어휘 = 케어일지 어휘")

    bad = unmapped(col("SELECT categorized FROM ai_log_summaries WHERE categorized IS NOT NULL"), ACTIVITY)
    warn("어휘 미등록 활동 분류", 0, len(bad), ", ".join(bad[:5]))

    # 링크 수 대조 — 개수가 아니라 '쌍' 을 센다. 코드 두 개가 한 개념에 접히는 경우
    # (nursing_hospital·병원간병)를 그래프 쪽과 같은 방식으로 세야 한다.
    with conn.cursor() as cur:
        cur.execute("SELECT id, specialties FROM caregivers WHERE deleted_at IS NULL")
        pairs = set()
        for r in cur.fetchall():
            for v in jsoncol(r["specialties"]):
                if isinstance(v, str) and v.strip() in SPEC:
                    pairs.add((r["id"], SPEC[v.strip()]))
    check("hasSpecialty 링크 수", len(pairs), scalar(f"""
        SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s care:hasSpecialty ?o }} }}"""),
          "DB 문자열쌍(코드 접힘 반영) = 그래프 링크")

    # ── E. 개인정보 미적재 (caren 고유) ────────────────────────────────────
    # 법인명은 개인정보가 아니라 의도적으로 싣는다. 그런데 기관 계정의 users.name 이
    # 법인명과 같아서(실측: '병점방문요양센터') 그대로 두면 정상 적재가 유출로 잡힌다.
    org_names = {str(v).strip() for v in col("SELECT name FROM organizations") if v}
    pii = [v for v in (col("SELECT name FROM users") + col("SELECT phone FROM users")
                       + col("SELECT email FROM users") + col("SELECT name FROM seniors")
                       + col("SELECT base_address FROM caregivers WHERE deleted_at IS NULL")
                       + col("SELECT home_address FROM seniors WHERE deleted_at IS NULL"))
           if v and str(v).strip() not in org_names]
    values = ", ".join(json.dumps(str(v)) for v in pii)
    leaked = scalar(f"""SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s ?p ?o }}
        FILTER(isLiteral(?o) && STR(?o) IN ({values})) }}""") if pii else 0
    check("개인정보 원문 리터럴", 0, leaked, "이름·전화·이메일·주소 원문이 그래프에 있으면 FAIL")

    check("STT 전사 본문 술어", 0, scalar(f"""
        SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s ?p ?o }}
        FILTER(CONTAINS(LCASE(STR(?p)), "stttext") || CONTAINS(LCASE(STR(?p)), "transcript")) }}"""))

    # ── F. 매칭 폐쇄성 — 온톨로지가 실제로 '걸리는가' ──────────────────────
    # 질병 → 필요특기(상하위 폐쇄) → 그 특기를 가진 활성 인력. 이 질의가 0 이면
    # 어휘는 등록됐어도 매칭에 아무 영향이 없다는 뜻이다(PoC 가 그랬다).
    reachable = scalar(f"""SELECT (COUNT(DISTINCT ?cg) AS ?n) WHERE {{
        GRAPH <{GRAPH_SCHEMA}> {{ ?d a care:Disease ; care:requiresSpecialty ?req .
                                  ?sp (care:broaderSpecialty|^care:broaderSpecialty)* ?req }}
        GRAPH <{GRAPH_DATA}> {{ ?cg a care:Caregiver ; care:hasSpecialty ?sp ; care:status "active" }} }}""")
    warn("질병→특기→인력 도달 인력 수(>0)", True, reachable > 0, f"{reachable}명")

    covered = scalar(f"""SELECT (COUNT(DISTINCT ?d) AS ?n) WHERE {{
        GRAPH <{GRAPH_SCHEMA}> {{ ?d a care:Disease ; care:code ?dc ; care:requiresSpecialty ?req .
                                  ?sp (care:broaderSpecialty|^care:broaderSpecialty)* ?req }}
        GRAPH <{GRAPH_DATA}> {{ ?cg care:hasSpecialty ?sp }} }}""")
    db_diseases = len({d.strip() for v in dis_raw for d in jsoncol(v) if isinstance(d, str) and d.strip()})
    warn("실DB 질병 중 인력이 닿는 질병", db_diseases, covered,
         "닿지 않는 질병이 있으면 그 질병 요청엔 온톨로지 가점이 0이다")

    # ── G. 데이터 품질 경고 ────────────────────────────────────────────────
    warn("후보 없는 매칭요청", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?r) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?r a care:MatchRequest . FILTER NOT EXISTS {{ ?c care:candidateFor ?r }} }} }}"""))
    warn("세션 없는 성사매칭", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?m) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?m a care:Match . FILTER NOT EXISTS {{ ?s care:ofMatch ?m }} }} }}"""))
    warn("특기 하나도 없는 활성 인력", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?cg) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?cg a care:Caregiver ; care:status "active" .
            FILTER NOT EXISTS {{ ?cg care:hasSpecialty ?sp }} }} }}"""))
    warn("좌표 없는 활성 인력", 0, scalar(f"""
        SELECT (COUNT(DISTINCT ?cg) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{
            ?cg a care:Caregiver ; care:status "active" . FILTER NOT EXISTS {{ ?cg care:lat ?l }} }} }}"""))

    total = scalar(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_DATA}> {{ ?s ?p ?o }} }}")
    schema_n = scalar(f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_SCHEMA}> {{ ?s ?p ?o }} }}")

    if not quiet:
        w = max(len(r[1]) for r in rows) + 2
        print(f"\n{'':<5}{'항목':<{w}}{'기대':>10}{'실제':>10}  비고")
        print("─" * (w + 40))
        for st, name, exp, act, note in rows:
            print(f"{st:<5}{name:<{w}}{exp:>10}{act:>10}  {note}")
    print(f"\n그래프 트리플 수: {total:,}  (스키마 {schema_n:,})")
    print(f"점검 {len(rows)}항목 — FAIL {fails} · WARN {warns}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
