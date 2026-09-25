#!/usr/bin/env python
"""
caren-ontology MCP 셀프테스트 — 디스패치·도구·리소스·인증을 실호출로 확인한다.

  venv/bin/python mcp_selftest.py            빠른 검사(run_invariants 제외)
  venv/bin/python mcp_selftest.py --full     run_invariants(DB 접속, 수 초)까지

운영 KPI 로그를 오염시키지 않게 호출 로그는 임시 파일로 돌린다(tx selftest 와 같은 방침).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="caren-mcp-selftest-", suffix=".jsonl", delete=False)
os.environ["CAREN_MCP_LOG"] = _tmp.name

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_caren  # noqa: E402

n_ok = n_fail = 0


def check(name: str, ok: bool, note: str = "") -> None:
    global n_ok, n_fail
    if ok:
        n_ok += 1
    else:
        n_fail += 1
    print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f"  — {note}" if note else ""))


def call(session, method, params=None):
    return mcp_caren.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, session)


def tool(session, name, args):
    r = call(session, "tools/call", {"name": name, "arguments": args})
    data = json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"]
    return r["isError"], data


def main() -> int:
    full = "--full" in sys.argv[1:]
    s = mcp_caren.new_session()
    s["client"] = "selftest"

    r = call(s, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "selftest"}})
    check("initialize", r["serverInfo"]["name"] == "caren-ontology" and bool(r["instructions"]))

    r = call(s, "tools/list")
    names = [t["name"] for t in r["tools"]]
    check("tools/list = 6종", len(names) == 6, ", ".join(names))

    r = call(s, "resources/list")
    check("resources/list = 3종", len(r["resources"]) == 3)
    for uri in ("schema://care-domain", "pitfalls://caren", "status://caren"):
        r = call(s, "resources/read", {"uri": uri})
        check(f"resources/read {uri}", len(r["contents"][0]["text"]) > 100)

    err, d = tool(s, "disease_coverage", {"question": "selftest — 질병 커버리지"})
    check("disease_coverage", not err and "freshness" in d and d["count"] > 0,
          f"{d.get('count')}개 질병" if not err else str(d)[:120])
    total = d.get("count", 0) if not err else 0

    err, d = tool(s, "disease_coverage", {"only_gaps": True})
    check("disease_coverage only_gaps", not err and d["count"] <= total, f"공백 {d.get('count')}개")

    err, d = tool(s, "specialty_supply", {"question": "selftest — 특기 공급"})
    check("specialty_supply", not err and d["count"] > 0, f"{d.get('count')}개 특기")

    err, d = tool(s, "caregiver_impact", {})
    ok = not err and d.get("count", 0) > 0 and all("○" in c["name"] or c["name"] == "—"
                                                   for c in d.get("caregivers", []))
    check("caregiver_impact 목록(이름 전부 마스킹)", ok, f"{d.get('count')}명")
    first = d["caregivers"][0]["id"] if ok else None

    if first is not None:
        err, d = tool(s, "caregiver_impact", {"caregiver_id": first})
        check(f"caregiver_impact id={first}", not err and d.get("found") is True,
              f"매칭 {len(d.get('matches', []))} · 세션 {len(d.get('sessions', []))}" if not err else str(d)[:120])
    err, d = tool(s, "caregiver_impact", {"caregiver_id": 99999999})
    check("caregiver_impact 없는 id → empty", not err and d.get("found") is False)

    err, d = tool(s, "resolve_care_term", {"terms": ["치매"], "question": "selftest"})
    check("resolve_care_term 치매", not err and len(d.get("related_specialties", [])) > 0,
          f"특기 {len(d.get('related_specialties', []))} · 연관어 {len(d.get('associated_terms', []))}")
    err, d = tool(s, "resolve_care_term", {"terms": []})
    check("resolve_care_term 빈 배열 → 도구 오류", err is True)

    err, d = tool(s, "log_unanswered", {"question": "selftest 미응답", "reason": "selftest"})
    check("log_unanswered", not err and d.get("recorded") is True)

    try:
        call(s, "tools/call", {"name": "no_such_tool", "arguments": {}})
        check("알 수 없는 도구 → RpcError", False)
    except mcp_caren.RpcError as e:
        check("알 수 없는 도구 → RpcError", e.rpc_code == -32602)
    try:
        call(s, "nope/method")
        check("알 수 없는 메서드 → RpcError", False)
    except mcp_caren.RpcError as e:
        check("알 수 없는 메서드 → RpcError", e.rpc_code == -32601)

    if full:
        err, d = tool(s, "run_invariants", {"question": "selftest — 점검"})
        check("run_invariants", not err and d.get("fails") is not None,
              f"FAIL {d.get('fails')} · WARN {d.get('warns')}" if not err else str(d)[:200])
        check("run_invariants passed", not err and d.get("passed") is True)

    # HTTP 인증 — TestClient 가 있으면 401/200 만 확인(운영 토큰 파일은 건드리지 않는다)
    try:
        import hashlib
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"tokens": [{"name": "selftest", "revoked": False,
                               "sha256": hashlib.sha256(b"selftest-token-0123456789").hexdigest()}]}, tf)
        tf.close()
        mcp_caren.TOKENS_FILE = tf.name
        app = FastAPI()
        app.include_router(mcp_caren.router)
        c = TestClient(app)
        check("HTTP 무인증 → 401", c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Authorization": "Bearer selftest-token-0123456789"})
        check("HTTP 토큰 → 200", r.status_code == 200 and r.json().get("result") == {})
        r = c.post("/mcp/selftest-token-0123456789x", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        check("HTTP 틀린 경로 토큰 → 401", r.status_code == 401)
        check("HTTP GET → 405", c.get("/mcp").status_code == 405)
        os.unlink(tf.name)
    except ImportError:
        print("SKIP HTTP 검사 — fastapi TestClient(httpx) 없음. 배포 후 curl 로 확인할 것.")

    with open(_tmp.name, encoding="utf-8") as fh:
        lines = fh.read().strip().splitlines()
    check("호출 로그 적재", len(lines) >= 8, f"{len(lines)}건 (임시 파일)")
    os.unlink(_tmp.name)

    print(f"\n검사 {n_ok + n_fail}건 — OK {n_ok} · FAIL {n_fail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
