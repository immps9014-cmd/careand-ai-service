#!/usr/bin/env python
"""
자연어 질문 처리율 KPI — caren-ontology MCP 호출 로그에서 계산한다 (tx kpi.php 이식, CAREN-ONT-MCP C1)

  사용: venv/bin/python mcp_kpi.py [--days 30] [--json] [--log <경로> ...]
  --log 를 안 주면 ontology/out/mcp-calls.jsonl 하나만 센다 — caren 은 stdio(root)와
  HTTP(careand-ai, root)가 같은 프로세스 권한이라 로그가 한 파일이다(tx 는 두 파일).

정의 (Onto-Ask 기술개요서 §7 "전체 질문 중 의도를 해석해 답한 비율" — tx 와 동일)
  질문    = 한 세션 안에서 같은 question 문장으로 들어온 호출 묶음.
  처리    = 그 묶음에 답을 낸 호출(answered)이나 "해당 없음"이라는 답(empty)이 하나라도 있음.
            빈 결과도 답이다 — "공급 공백 없음"은 질문에 대한 올바른 답이다.
  미처리  = 끝까지 답이 없었던 질문. 사유 우선순위: unanswered > unsupported > ambiguous > error.
  question 없는 호출은 질문 수에 넣지 않고 따로 센다 — 처리율을 부풀리지도 깎지도 않게.

⚠ 분모의 한계: 도구를 한 번도 부르지 않고 끝난 질문은 log_unanswered 로만 잡힌다.
  "질문 수"는 실제 질문 수의 하한이다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOGS = [os.environ.get("CAREN_MCP_LOG") or os.path.join(HERE, "ontology", "out", "mcp-calls.jsonl")]

_REASON_RANK = {"unanswered": 4, "unsupported": 3, "ambiguous": 2, "error": 1}


def compute(days: int = 30, logs: list[str] | None = None) -> dict[str, Any]:
    days = max(1, min(365, int(days)))
    logs = list(dict.fromkeys(logs or DEFAULT_LOGS))
    since = time.time() - days * 86400

    recs, sources = [], {}
    for log in logs:
        if not os.path.exists(log):
            sources[log] = "missing"
            continue
        try:
            with open(log, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            sources[log] = "unreadable"
            continue
        sources[log] = "read"
        for ln in lines:
            try:
                r = json.loads(ln)
                if isinstance(r, dict) and r.get("ts") and \
                        datetime.fromisoformat(r["ts"]).timestamp() >= since:
                    recs.append(r)
            except (ValueError, TypeError):
                continue

    by_tool: dict[str, dict[str, int]] = {}
    by_client: dict[str, int] = {}
    questions: dict[str, dict[str, Any]] = {}
    no_q = 0
    for r in recs:
        t, o = r.get("tool", "?"), r.get("outcome", "answered")
        c = r.get("client") or "-"
        by_client[c] = by_client.get(c, 0) + 1
        bt = by_tool.setdefault(t, {"calls": 0})
        bt["calls"] += 1
        bt[o] = bt.get(o, 0) + 1

        q = re.sub(r"\s+", " ", str(r.get("question") or "")).strip()
        if not q:
            no_q += 1
            continue
        k = f"{r.get('sid', '-')}|{q}"
        e = questions.setdefault(k, {"question": q, "first_ts": r["ts"], "tools": set(),
                                     "outcomes": set(), "reason": None})
        e["tools"].add(t)
        e["outcomes"].add(o)
        if t == "log_unanswered":
            e["reason"] = (r.get("args") or {}).get("reason")

    processed, unproc = 0, []
    why = {"unanswered": 0, "unsupported": 0, "ambiguous": 0, "error": 0}
    for e in questions.values():
        if "answered" in e["outcomes"] or "empty" in e["outcomes"]:
            processed += 1
            continue
        w = max(e["outcomes"], key=lambda o: _REASON_RANK.get(o, 0), default="error")
        w = w if w in why else "error"
        why[w] += 1
        unproc.append({"question": e["question"], "why": w, "reason": e["reason"],
                       "tools": sorted(e["tools"]), "ts": e["first_ts"]})
    unproc.sort(key=lambda u: u["ts"], reverse=True)

    n = len(questions)
    return {
        "period": {"days": days,
                   "from": datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M"),
                   "to": datetime.now().strftime("%Y-%m-%d %H:%M")},
        "questions": n,
        "processed": processed,
        "rate": round(processed / n, 3) if n else None,
        "unprocessed_by_reason": why,
        "calls": len(recs),
        "calls_without_question": no_q,
        "by_tool": dict(sorted(by_tool.items())),
        "by_client": by_client,
        "sources": sources,
        "unprocessed": unproc[:30],
        "definition": "처리율 = 답(빈 결과 포함)을 낸 질문 / 질문. 질문 = 세션 안의 같은 question 문장. "
                      "도구를 안 부르고 끝난 질문은 log_unanswered 로만 잡히므로 질문 수는 하한이다.",
    }


def main() -> int:
    argv = sys.argv[1:]

    def opt(name: str, default=None):
        try:
            return argv[argv.index("--" + name) + 1]
        except (ValueError, IndexError):
            return default

    logs = [argv[i + 1] for i, a in enumerate(argv) if a == "--log" and i + 1 < len(argv)]
    res = compute(days=int(opt("days", 30)), logs=logs or None)

    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    n, p, why = res["questions"], res["processed"], res["unprocessed_by_reason"]
    print(f"\n  자연어 질문 처리율 — 최근 {res['period']['days']}일 "
          f"({res['period']['from']} ~ {res['period']['to']})\n  " + "─" * 60)
    print(f"  질문 {n} · 처리 {p} · 처리율 " + (f"{p / n * 100:.1f}%" if n else "-"))
    print(f"  미처리 사유: 도구 없음 {why['unanswered']} · 미지원 {why['unsupported']}"
          f" · 모호 {why['ambiguous']} · 오류 {why['error']}")
    print(f"  호출 {res['calls']} (질문 문장 없는 호출 {res['calls_without_question']})")
    for t, c in res["by_tool"].items():
        parts = " · ".join(f"{k} {v}" for k, v in c.items() if k != "calls")
        print(f"    {t:<20} {c['calls']:>3}  {parts}")
    if res["unprocessed"]:
        print("\n  못 푼 질문(최근)")
        for u in res["unprocessed"][:10]:
            print(f"    [{u['why']}] {u['question']}" + (f"  — {u['reason']}" if u["reason"] else ""))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
