#!/usr/bin/env python
"""
caren-ontology MCP 도구 검산 — 도구 응답을 careand DB **직접 집계**와 대조한다.

  venv/bin/python mcp_crosscheck.py

tx-ontology 의 crosscheck.php 와 같은 원칙: 새 질의는 원장(DB) 직접 집계와 맞아야만
도구로 열 수 있다(3사에서 이 대조가 naturefood 출하 누락 결함을 잡았다).

대조 항목:
  X1  specialty_supply.caregivers   ↔ caregivers(활성·미삭제) 특기 JSON 직접 집계
  X2  disease_coverage.recipients   ↔ seniors+nursing_patients(미삭제) diseases JSON 직접 집계
  X3  disease_coverage.requests     ↔ match_requests → 대상자 → 질병 직접 집계(ETL 과 같은
                                      우선순위: senior → nursing → postpartum, 소프트삭제 제외)
  X4  caregiver_impact.matches/sessions ↔ matches·care_sessions 행 수(매칭 최다 인력 1명 표본)

disease_coverage.caregivers 는 근접 특기 폐쇄(상·하위)가 걸린 수치라 SQL 로 재현하지 않는다
— 폐쇄 규칙 자체는 ontology.py 의 2026-09-20 실측(형제 유입 차단)으로 이미 검증돼 있다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import defaultdict

_tmp = tempfile.NamedTemporaryFile(prefix="caren-mcp-crosscheck-", suffix=".jsonl", delete=False)
os.environ["CAREN_MCP_LOG"] = _tmp.name

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ontology"))

import mcp_caren  # noqa: E402
from etl_caren import connect, jsoncol, load_vocab  # noqa: E402 — ETL 과 같은 매핑·필터를 쓴다

rows: list[tuple[str, str, str, str, str]] = []
fails = 0


def check(name: str, expected, actual, note: str = "") -> None:
    global fails
    ok = expected == actual
    if not ok:
        fails += 1
    rows.append(("OK " if ok else "FAIL", name, str(expected), str(actual), note))


def local(iri: str) -> str:
    return iri.strip("<>").rsplit("#", 1)[-1]


def main() -> int:
    conn = connect()
    vocab = load_vocab()
    spec_of = {code: local(i) for code, i in vocab.get("Specialty", {}).items()}
    disease_of = {code: local(i) for code, i in vocab.get("Disease", {}).items()}

    s = mcp_caren.new_session()
    s["client"] = "crosscheck"

    def tool(name, args):
        r = mcp_caren.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}, s)
        if r["isError"]:
            sys.exit(f"도구 {name} 오류: {r['content'][0]['text']}")
        return json.loads(r["content"][0]["text"])

    with conn.cursor() as cur:
        # ── X1 특기별 활성 인력 ────────────────────────────────────────────
        cur.execute("SELECT id, specialties FROM caregivers"
                    " WHERE deleted_at IS NULL AND status='active'")
        db_supply: dict[str, set] = defaultdict(set)
        for r in cur.fetchall():
            for code in jsoncol(r["specialties"]):
                if isinstance(code, str) and code.strip() in spec_of:
                    db_supply[spec_of[code.strip()]].add(r["id"])
        supply = {r["id"]: r["caregivers"] for r in tool("specialty_supply", {})["specialties"]}
        for ent in sorted(set(db_supply) | {k for k, v in supply.items() if v}):
            check(f"X1 supply {ent}", len(db_supply.get(ent, set())), supply.get(ent, 0))

        # ── X2 질병별 대상자 ──────────────────────────────────────────────
        db_rec: dict[str, set] = defaultdict(set)
        for table, kind in (("seniors", "senior"), ("nursing_patients", "nursing")):
            cur.execute(f"SELECT id, diseases FROM {table} WHERE deleted_at IS NULL")
            for r in cur.fetchall():
                for code in jsoncol(r["diseases"]):
                    if isinstance(code, str) and code.strip() in disease_of:
                        db_rec[disease_of[code.strip()]].add(f"{kind}-{r['id']}")
        cov = {r["id"]: r for r in tool("disease_coverage", {})["diseases"]}
        for ent in sorted(set(db_rec) | {k for k, v in cov.items() if v["recipients"]}):
            check(f"X2 recipients {ent}", len(db_rec.get(ent, set())),
                  cov.get(ent, {}).get("recipients", 0))

        # ── X3 질병별 매칭요청 (ETL 과 같은 대상자 우선순위·소프트삭제 제외) ──
        alive_dis: dict[str, list] = {}
        for table, kind in (("seniors", "senior"), ("nursing_patients", "nursing")):
            cur.execute(f"SELECT id, diseases FROM {table} WHERE deleted_at IS NULL")
            for r in cur.fetchall():
                alive_dis[f"{kind}-{r['id']}"] = [disease_of[c.strip()] for c in jsoncol(r["diseases"])
                                                  if isinstance(c, str) and c.strip() in disease_of]
        cur.execute("SELECT id FROM postpartum_clients WHERE deleted_at IS NULL")
        for r in cur.fetchall():
            alive_dis[f"postpartum-{r['id']}"] = []          # 산후는 diseases 컬럼이 없다
        cur.execute("SELECT id, senior_id, nursing_patient_id, postpartum_client_id FROM match_requests")
        db_req: dict[str, set] = defaultdict(set)
        for r in cur.fetchall():
            for col, kind in (("senior_id", "senior"), ("nursing_patient_id", "nursing"),
                              ("postpartum_client_id", "postpartum")):
                if r[col]:
                    for ent in alive_dis.get(f"{kind}-{r[col]}", []):
                        db_req[ent].add(r["id"])
                    break
        for ent in sorted(set(db_req) | {k for k, v in cov.items() if v["requests"]}):
            check(f"X3 requests {ent}", len(db_req.get(ent, set())),
                  cov.get(ent, {}).get("requests", 0))

        # ── X4 이탈 영향분석 표본(매칭 최다 인력) ──────────────────────────
        cur.execute("SELECT m.caregiver_id AS cg, COUNT(*) AS n FROM matches m"
                    " JOIN caregivers c ON c.id = m.caregiver_id AND c.deleted_at IS NULL"
                    " GROUP BY m.caregiver_id ORDER BY n DESC LIMIT 1")
        top = cur.fetchone()
        if top:
            cid = top["cg"]
            imp = tool("caregiver_impact", {"caregiver_id": cid})
            check(f"X4 matches cg={cid}", top["n"], len(imp.get("matches", [])))
            cur.execute("SELECT COUNT(*) AS n FROM care_sessions s"
                        " JOIN matches m ON m.id = s.match_id WHERE m.caregiver_id = %s", (cid,))
            check(f"X4 sessions cg={cid}", cur.fetchone()["n"], len(imp.get("sessions", [])))
        else:
            rows.append(("SKIP", "X4", "-", "-", "matches 가 비어 있어 표본이 없다"))

    w = max(len(r[1]) for r in rows) + 2
    print(f"\n{'':<5}{'항목':<{w}}{'DB':>8}{'도구':>8}  비고")
    print("─" * (w + 30))
    for st, name, exp, act, note in rows:
        print(f"{st:<5}{name:<{w}}{exp:>8}{act:>8}  {note}")
    print(f"\n대조 {len(rows)}항목 — FAIL {fails}")
    os.unlink(_tmp.name)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
