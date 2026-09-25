"""
caren-ontology MCP 서버 코어 (CAREN-ONT-MCP C1) — 도구·리소스·JSON-RPC 처리(handle)와
FastAPI HTTP 전송(router). stdio 전송은 mcp_stdio.py.

  등록(stdio): claude mcp add --scope user caren-ontology -- \
                 /root/caren/careand-ai-service/venv/bin/python /root/caren/careand-ai-service/mcp_stdio.py
  원격(HTTP) : https://caren.aiclaude.kr/mcp  (Authorization: Bearer <토큰> 또는 /mcp/<토큰>)
               careand-ai.service(uvicorn :8001) 안에서 돌므로 별도 프로세스가 없다.
  토큰       : venv/bin/python mcp_token.py add|list|revoke  (해시만 /etc/caren-mcp/tokens.json)
  시험       : venv/bin/python mcp_selftest.py

── 지키는 선 (CAREN-ONT-MCP §04 = TX-ONT-MCP G1~G5 상속 + CG-1·CG-2) ──────────
 G1   SPARQL 원문을 받는 도구는 없다. 질의는 ontology.py 의 함수(화이트리스트)만 부른다
      — 이 파일에는 질의문이 한 줄도 없다(복사 금지).
 G2   Fuseki(moai-fuseki 1GB 공유)를 지킨다 — ontology.py 타임아웃(1.5/8초)과 행 상한.
 G3   모든 도구 응답에 데이터 기준 시각(ontology/out/status.json)과 stale 여부를 붙인다.
      재적재 cron 이 멈춰 있으면 stale 로 드러난다.
 G4   모호하면 고르지 않는다 — caregiver_impact 는 id 없이 부르면 목록만 돌려준다.
 G5   읽기 전용. careand DB 에도 Fuseki 에도 쓰지 않는다(호출 로그 파일만 예외).
 CG-1 개인정보는 도구 계층에서도 마스킹 유지 — 그래프에 원문이 없고(ETL 이 김○○·좌표 2자리로
      떨어뜨림), 이름 원문·연락처를 돌려주는 도구를 만들지 않는다. 호출 로그(질문 원문 포함)는
      0600 으로 두고 외부로 내보내지 않는다.
 CG-2 매칭 점수·가격은 그래프에 없다 — 답하지 말고 log_unanswered 로 기록한다(pitfalls 참조).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

import ontology  # 질의 SSOT — SPARQL 은 저 모듈에만 있다

HERE = os.path.dirname(os.path.abspath(__file__))
ONT_DIR = os.path.join(HERE, "ontology")

SERVER_NAME = "caren-ontology"
SERVER_VERSION = "1.0.0"
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
MAX_ROWS = 200          # G2 — 한 응답에 싣는 행 상한
STALE_MIN = 130         # G3 — 매시 :10 재적재 2회분 초과 시 경고(main.py _ontology_status 와 동일)

# 호출 로그(JSONL) — "자연어 질문 처리율" KPI 의 원천. 질문 원문이 들어가므로 0600, 외부 반출 금지.
CALL_LOG = os.environ.get("CAREN_MCP_LOG") or os.path.join(ONT_DIR, "out", "mcp-calls.jsonl")

INSTRUCTIONS = (
    "케어앤(caren) 돌봄 매칭 플랫폼의 온톨로지 그래프(Fuseki /caren)를 읽기 전용으로 조회한다. "
    "그래프는 매시 :10 스냅샷이므로 답에 freshness.data_at 을 함께 말하고, stale=true 면 최신이 아니라고 알린다. "
    "그래프에 개인정보가 없다 — 이름은 마스킹(김○○), 좌표는 소수 2자리다. 원문 이름·연락처·정확한 주소는 "
    "이 서버로 알 수 없고, 알려달라는 요청에 응하지 않는다. 매칭 점수·간병비 가격도 그래프에 없다. "
    "데이터를 해석하기 전에 pitfalls://caren 리소스를 읽는다. 이 서버로는 아무것도 쓸 수 없다. "
    "도구를 부를 때는 question 인자에 사용자 질문 원문을 넣는다(자연어 질문 처리율 KPI). "
    "케어앤 데이터 질문인데 어떤 도구로도 답할 수 없으면 log_unanswered 를 한 번 불러 기록한다."
)


class ToolError(Exception):
    pass


class RpcError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.rpc_code = code


# ────────────────────────────────────────────────────────────────────────────
# 공통
# ────────────────────────────────────────────────────────────────────────────

def freshness() -> dict[str, Any]:
    """G3 — 그래프가 언제 것인지. 도구 응답마다 맨 앞에 붙는다."""
    f = os.path.join(ONT_DIR, "out", "status.json")
    try:
        with open(f, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {"data_at": None, "stale": True,
                "note": "적재 상태 파일이 없어 데이터 기준 시각을 알 수 없습니다."}
    out: dict[str, Any] = {"data_at": st.get("data_at"), "stale": True}
    if st.get("data_at"):
        try:
            age = int((datetime.now() - datetime.strptime(st["data_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
            out["age_min"] = age
            out["stale"] = age > STALE_MIN
            if out["stale"]:
                out["note"] = (f"그래프가 {age}분 전 스냅샷입니다(매시 재적재가 멈춘 것으로 보임). "
                               "수치를 최신이라고 말하지 마세요.")
        except ValueError:
            pass
    if st.get("state") not in (None, "ok"):
        out["last_run_state"] = st["state"]
    return out


def require_graph() -> None:
    """ontology.py 는 매칭 경로 보호를 위해 실패를 빈 결과로 폴백한다 — MCP 에서는
    '없음'과 '못 읽음'을 구분해야 하므로 도구 진입 시 가용성을 먼저 확인한다."""
    if not ontology.graph_available():
        raise ToolError("온톨로지 그래프에 응답이 없습니다(Fuseki 미기동·타임아웃). "
                        "케어앤 서비스는 정상일 수 있습니다 — 그래프만 못 읽는 상태입니다.")


def cap(rows: list, out: dict[str, Any]) -> list:
    if len(rows) > MAX_ROWS:
        out["truncated"] = True
        out["note_truncated"] = f"행이 많아 {MAX_ROWS}개까지만 실었습니다."
        return rows[:MAX_ROWS]
    return rows


def str_arg(args: dict, key: str, max_len: int = 100) -> str:
    v = str(args.get(key) or "").strip()
    if not v:
        raise ToolError(f"{key} 가 비어 있습니다.")
    if len(v) > max_len:
        raise ToolError(f"{key} 가 너무 깁니다(최대 {max_len}자).")
    return v


# ────────────────────────────────────────────────────────────────────────────
# 도구
# ────────────────────────────────────────────────────────────────────────────

_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_Q = {"type": "string", "description": "사용자 질문 원문(KPI 집계용)", "maxLength": 500}


def tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "disease_coverage",
            "title": "질병 커버리지",
            "description": "질병별로 요구 특기·활성 인력 수·대상자 수·매칭요청 수를 본다. "
                           "인력 0인데 대상자가 있는 질병(공급 공백)이 맨 위에 온다. "
                           "code 가 없는 질병은 DB 에 아직 없는 어휘 선행 등록분이다.",
            "inputSchema": {"type": "object", "properties": {
                "question": _Q,
                "only_gaps": {"type": "boolean", "description": "true 면 활성 인력 0인 질병만"},
            }},
            "annotations": _RO,
        },
        {
            "name": "specialty_supply",
            "title": "특기별 공급",
            "description": "특기별 활성 인력 수와 '어떤 질병이 요구하는 특기인지'를 본다 — 커버리지 공백의 원인 표.",
            "inputSchema": {"type": "object", "properties": {"question": _Q}},
            "annotations": _RO,
        },
        {
            "name": "caregiver_impact",
            "title": "인력 이탈 영향분석",
            "description": "요양보호사 한 명이 빠지면 흔들리는 매칭·세션·질병 커버리지와 대체 후보(근접 특기)를 본다. "
                           "caregiver_id 없이 부르면 대상 목록(마스킹 이름)만 돌려준다 — 목록에서 고르는 건 사용자다.",
            "inputSchema": {"type": "object", "properties": {
                "question": _Q,
                "caregiver_id": {"type": "integer", "minimum": 1, "description": "내부 인력 id (목록의 id)"},
            }},
            "annotations": _RO,
        },
        {
            "name": "resolve_care_term",
            "title": "케어 용어 확장",
            "description": "질병 라벨/코드를 매칭이 실제 쓰는 확장 그대로 풀어본다 — 근접 특기(상·하위만, 형제 제외)와 "
                           "연관 관찰 용어(STT 어휘 확장분). '당뇨는 어떤 특기로 이어지나' 류 질문.",
            "inputSchema": {"type": "object", "properties": {
                "question": _Q,
                "terms": {"type": "array", "items": {"type": "string", "maxLength": 60},
                          "minItems": 1, "maxItems": 10, "description": "질병 라벨 또는 DB 코드"},
            }, "required": ["terms"]},
            "annotations": _RO,
        },
        {
            "name": "run_invariants",
            "title": "불변식 점검",
            "description": "ontology/check.py 53항목을 지금 실행한다 — 그래프와 careand DB 가 맞는지, "
                           "어휘 미등록·개인정보 누출이 없는지. FAIL 이 있으면 그래프를 믿지 말 것.",
            "inputSchema": {"type": "object", "properties": {"question": _Q}},
            "annotations": {**_RO, "idempotentHint": False},
        },
        {
            "name": "log_unanswered",
            "title": "미응답 질문 기록",
            "description": "케어앤 데이터 질문인데 어떤 도구로도 답할 수 없을 때 한 번 기록한다(도구 백로그·KPI).",
            "inputSchema": {"type": "object", "properties": {
                "question": {**_Q, "description": "사용자 질문 원문"},
                "reason": {"type": "string", "maxLength": 300, "description": "왜 답할 수 없는지"},
            }, "required": ["question", "reason"]},
            "annotations": {**_RO, "idempotentHint": False},
        },
    ]


def tool_disease_coverage(args: dict) -> dict[str, Any]:
    require_graph()
    rows = ontology.disease_coverage()
    if args.get("only_gaps"):
        rows = [r for r in rows if r["caregivers"] == 0]
    out: dict[str, Any] = {"diseases": None, "count": len(rows)}
    out["diseases"] = cap(rows, out)
    if not rows:
        out["_outcome"] = "empty"
    out["note"] = ("caregivers=0 이면서 recipients>0 인 줄이 공급 공백입니다. "
                   "in_db=false 는 어휘 선행 등록분(실데이터 아님)입니다.")
    return out


def tool_specialty_supply(args: dict) -> dict[str, Any]:
    require_graph()
    rows = ontology.specialty_supply()
    out: dict[str, Any] = {"specialties": None, "count": len(rows)}
    out["specialties"] = cap(rows, out)
    if not rows:
        out["_outcome"] = "empty"
    out["note"] = "required_by_disease=false 인 특기는 질병 요구와 무관한 공급(가사 등)일 수 있습니다."
    return out


def tool_caregiver_impact(args: dict) -> dict[str, Any]:
    require_graph()
    cid = args.get("caregiver_id")
    if cid is None:
        # G4 — id 가 없으면 고르지 않는다. 목록을 주고 사용자가 고르게 한다.
        rows = ontology.caregiver_directory()
        out: dict[str, Any] = {"caregivers": None, "count": len(rows),
                               "note": "caregiver_id 를 골라 다시 부르세요. 이름은 마스킹본이 전부입니다 — "
                                       "원문 이름으로 특정할 수 없고, 후보가 여럿이면 사용자에게 물으세요."}
        out["caregivers"] = cap(rows, out)
        return out
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        raise ToolError("caregiver_id 는 정수여야 합니다.")
    if cid < 1:
        raise ToolError("caregiver_id 는 1 이상이어야 합니다.")
    r = ontology.caregiver_impact(cid)
    if not r.get("found"):
        return {"found": False, "_outcome": "empty",
                "note": f"caregiver {cid} 가 그래프에 없습니다(비활성·삭제·미적재)."}
    r["note"] = ("alternatives 는 근접 특기 기준 후보일 뿐 매칭 점수가 아닙니다(그래프에 점수 없음). "
                 "distance_km 는 좌표가 2자리 반올림이라 ±1km 오차가 있습니다.")
    return r


def tool_resolve_care_term(args: dict) -> dict[str, Any]:
    terms = args.get("terms")
    if not isinstance(terms, list) or not terms or len(terms) > 10:
        raise ToolError("terms 는 1~10개의 문자열 배열이어야 합니다.")
    clean = frozenset(str(t).strip() for t in terms if str(t).strip())
    if not clean:
        raise ToolError("terms 가 비어 있습니다.")
    require_graph()
    specialties = sorted(ontology.related_specialty_labels(clean))
    associated = sorted(ontology.associated_term_labels(clean))
    out: dict[str, Any] = {"terms": sorted(clean),
                           "related_specialties": specialties,
                           "associated_terms": associated,
                           "note": "related_specialties 에는 라벨과 DB 코드가 섞여 있습니다(매칭이 실제 쓰는 집합 그대로). "
                                   "둘 다 비면 그 라벨/코드가 어휘에 없는 것입니다 — care-domain.ttl 등록 여부를 확인하세요."}
    if not specialties and not associated:
        out["_outcome"] = "empty"
    return out


def tool_run_invariants(args: dict) -> dict[str, Any]:
    try:
        p = subprocess.run([sys.executable, os.path.join(ONT_DIR, "check.py")],
                           cwd=HERE, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ToolError("점검이 120초 안에 끝나지 않았습니다.")
    text = (p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip()
    m = re.search(r"FAIL (\d+) · WARN (\d+)", text)
    return {"passed": p.returncode == 0,
            "fails": int(m.group(1)) if m else None,
            "warns": int(m.group(2)) if m else None,
            "report": text[-8000:],
            "note": "FAIL=적재 무결성 깨짐(그래프를 믿지 말 것) · WARN=원천 DB 품질 보고(실패 아님)."}


def tool_log_unanswered(args: dict) -> dict[str, Any]:
    # 기록 자체는 호출 로그가 한다(outcome=unanswered). 여기서는 입력만 확인한다.
    str_arg(args, "question", 500)
    str_arg(args, "reason", 300)
    return {"recorded": True, "_outcome": "unanswered",
            "note": "기록했습니다. 사용자에게는 이 질문에 지금 도구로 답할 수 없다고 솔직히 알리세요."}


_TOOL_FNS = {
    "disease_coverage": tool_disease_coverage,
    "specialty_supply": tool_specialty_supply,
    "caregiver_impact": tool_caregiver_impact,
    "resolve_care_term": tool_resolve_care_term,
    "run_invariants": tool_run_invariants,
    "log_unanswered": tool_log_unanswered,
}


def log_call(session: dict, tool: str, args: dict, outcome: str, ms: int, err: str | None = None) -> None:
    """호출 한 건을 JSONL 로. 로그 실패는 도구 응답을 막지 않는다(stderr 로만 알림)."""
    q = args.get("question")
    a = {k: (str(v)[:200] if isinstance(v, (str, int, float, bool)) else "(생략)")
         for k, v in args.items() if k != "question"}
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
           "sid": session.get("sid"), "client": session.get("client"), "tool": tool,
           "question": (str(q).strip()[:500] if q is not None else None),
           "outcome": outcome, "ms": ms, "args": a}
    if err is not None:
        rec["error"] = err[:300]
    try:
        new = not os.path.exists(CALL_LOG)
        with open(CALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if new:
            os.chmod(CALL_LOG, 0o600)   # 질문 원문이 들어간다(CG-1)
    except OSError as e:
        print(f"[caren-ontology] 호출 로그를 쓰지 못했습니다: {e}", file=sys.stderr)


# ────────────────────────────────────────────────────────────────────────────
# 리소스
# ────────────────────────────────────────────────────────────────────────────

def resource_list() -> list[dict[str, Any]]:
    return [
        {"uri": "schema://care-domain", "name": "care-domain", "title": "돌봄 도메인 온톨로지(care-domain.ttl)",
         "description": "클래스·관계·어휘 개체의 SSOT. ontology/ 원본을 그대로 읽는다.", "mimeType": "text/turtle"},
        {"uri": "pitfalls://caren", "name": "pitfalls-caren", "title": "caren 함정",
         "description": "caren 데이터를 해석할 때 잘못된 결론으로 가기 쉬운 지점. 답하기 전에 읽을 것.",
         "mimeType": "text/markdown"},
        {"uri": "status://caren", "name": "status-caren", "title": "caren 적재 상태",
         "description": "마지막 적재 시각·트리플 수·경고 수.", "mimeType": "application/json"},
    ]


def resource_read(uri: str) -> tuple[str, str]:
    if uri == "schema://care-domain":
        with open(os.path.join(ONT_DIR, "care-domain.ttl"), encoding="utf-8") as fh:
            return "text/turtle", fh.read()
    if uri == "pitfalls://caren":
        with open(os.path.join(ONT_DIR, "PITFALLS.md"), encoding="utf-8") as fh:
            return "text/markdown", fh.read()
    if uri == "status://caren":
        f = os.path.join(ONT_DIR, "out", "status.json")
        st = None
        try:
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            pass
        body = {"dataset": "caren", "graphs": [ontology.GRAPH_SCHEMA, ontology.GRAPH_DATA],
                "freshness": freshness(), "last_load": st}
        return "application/json", json.dumps(body, ensure_ascii=False, indent=2)
    raise ToolError(f"알 수 없는 리소스: {uri}")


# ────────────────────────────────────────────────────────────────────────────
# JSON-RPC
# ────────────────────────────────────────────────────────────────────────────

def new_session() -> dict[str, Any]:
    return {"sid": secrets.token_hex(4), "client": None}


def handle(msg: dict, session: dict) -> Any:
    method = msg.get("method") or ""
    p = msg.get("params") or {}
    if method == "initialize":
        if isinstance(p.get("clientInfo"), dict) and p["clientInfo"].get("name"):
            session["client"] = str(p["clientInfo"]["name"])[:60]
        want = p.get("protocolVersion") or PROTOCOLS[0]
        return {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "title": "케어앤 돌봄 온톨로지", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_defs()}
    if method == "tools/call":
        name = str(p.get("name") or "")
        args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
        fn = _TOOL_FNS.get(name)
        if fn is None:
            raise RpcError(-32602, f"알 수 없는 도구: {name}")
        t0 = time.monotonic()
        try:
            data = fn(args)
            outcome = data.pop("_outcome", "answered")
            log_call(session, name, args, outcome, int((time.monotonic() - t0) * 1000))
            data = {"freshness": freshness(), **data}          # G3 — 기준 시각을 맨 앞에
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except ToolError as e:
            log_call(session, name, args, "error", int((time.monotonic() - t0) * 1000), str(e))
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    if method == "resources/list":
        return {"resources": resource_list()}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        uri = str(p.get("uri") or "")
        try:
            mime, text = resource_read(uri)
        except (ToolError, OSError) as e:
            raise RpcError(-32002, str(e))
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}
    raise RpcError(-32601, f"지원하지 않는 메서드: {method}")


# ────────────────────────────────────────────────────────────────────────────
# HTTP 전송 (Streamable HTTP — careand-ai 의 FastAPI 에 마운트, tx http.php 이식)
# ────────────────────────────────────────────────────────────────────────────

TOKENS_FILE = os.environ.get("CAREN_MCP_TOKENS") or "/etc/caren-mcp/tokens.json"
RATE_PER_MIN = int(os.environ.get("CAREN_MCP_RATE") or 120)
MAX_BODY = 262144                              # 256KB — 도구 인자는 짧다
ALLOWED_ORIGIN = {"https://claude.ai", "https://claude.com", "https://caren.aiclaude.kr"}

_rate_lock = threading.Lock()
_rate: dict[str, tuple[int, int]] = {}         # sha256 → (분 창, 호출 수). 프로세스 1개라 메모리로 충분


def _find_token(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        with open(TOKENS_FILE, encoding="utf-8") as fh:
            j = json.load(fh)
    except (OSError, ValueError):
        return None
    h = hashlib.sha256(raw.encode()).hexdigest()
    for t in j.get("tokens", []):
        if not t.get("revoked") and secrets.compare_digest(t.get("sha256", ""), h):
            return t
    return None


def _rate_ok(sha: str) -> bool:
    win = int(time.time() // 60)
    with _rate_lock:
        w, n = _rate.get(sha, (win, 0))
        n = n + 1 if w == win else 1
        _rate[sha] = (win, n)
    return n <= RATE_PER_MIN


def _rpc_error(code: int, msg: str, id_=None) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, Response
except ImportError:                             # stdio 단독 실행(FastAPI 불필요) 대비
    APIRouter = None

if APIRouter is not None:
    router = APIRouter()

    async def _mcp(request: Request, path_token: str | None) -> Response:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in ALLOWED_ORIGIN:
            return JSONResponse(_rpc_error(-32000, "허용되지 않은 Origin"), status_code=403)

        bearer = None
        auth = request.headers.get("authorization") or ""
        m = re.match(r"^Bearer\s+(\S+)$", auth, re.I)
        if m:
            bearer = m.group(1)
        tok = _find_token(bearer or path_token)
        if tok is None:
            time.sleep(0.3)                     # 무차별 대입을 조금 늦춘다. 틀림/없음을 구분해 주지 않는다
            return JSONResponse(_rpc_error(-32001, "인증이 필요합니다"), status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="caren-ontology"'})
        if not _rate_ok(tok["sha256"]):
            return JSONResponse(_rpc_error(-32000, "호출이 너무 잦습니다. 잠시 후 다시 시도하세요."),
                                status_code=429, headers={"Retry-After": str(60 - int(time.time()) % 60)})

        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse(_rpc_error(-32600, "요청이 너무 큽니다"), status_code=413)
        try:
            msg = json.loads(body)
        except ValueError:
            return JSONResponse(_rpc_error(-32700, "JSON 파싱 실패"), status_code=400)
        if not isinstance(msg, (dict, list)):
            return JSONResponse(_rpc_error(-32700, "JSON 파싱 실패"), status_code=400)

        # 세션은 무상태 — initialize 가 준 id 를 클라이언트가 돌려보내면 KPI 의 sid 로 쓴다
        session = new_session()
        sid = request.headers.get("mcp-session-id") or ""
        if re.match(r"^[a-f0-9]{16}$", sid):
            session["sid"] = sid
        session["client"] = "http:" + str(tok.get("name") or "?")[:40]

        batch = isinstance(msg, list)
        msgs = msg if batch else [msg]
        out, new_sid = [], None
        for one in msgs:
            if not isinstance(one, dict):
                out.append(_rpc_error(-32600, "잘못된 메시지"))
                continue
            if "method" not in one:
                continue                        # 클라이언트가 보낸 응답 — 받을 요청이 없으니 무시
            is_req = "id" in one
            try:
                client = session["client"]
                res = handle(one, session)
                session["client"] = client      # initialize 가 clientInfo 로 덮어써도 토큰 이름 유지
                if one.get("method") == "initialize":
                    new_sid = secrets.token_hex(8)
                    session["sid"] = new_sid
                if is_req:
                    out.append({"jsonrpc": "2.0", "id": one["id"], "result": res})
            except RpcError as e:
                if is_req:
                    out.append(_rpc_error(e.rpc_code, str(e), one.get("id")))
            except Exception as e:              # noqa: BLE001 — 프로토콜 응답은 항상 돌려준다
                print(f"[caren-ontology] {type(e).__name__}: {e}", file=sys.stderr)
                if is_req:
                    out.append(_rpc_error(-32603, "서버 내부 오류", one.get("id")))
        headers = {"Cache-Control": "no-store"}
        if new_sid:
            headers["Mcp-Session-Id"] = new_sid
        if not out:
            return Response(status_code=202, headers=headers)
        return JSONResponse(out if batch else out[0], headers=headers)

    @router.post("/mcp")
    async def mcp_root(request: Request) -> Response:
        return await _mcp(request, None)

    @router.post("/mcp/{token}")
    async def mcp_path_token(request: Request, token: str) -> Response:
        if not re.match(r"^[A-Za-z0-9_-]{20,128}$", token):
            return JSONResponse(_rpc_error(-32001, "인증이 필요합니다"), status_code=401)
        return await _mcp(request, token)

    @router.get("/mcp")
    @router.delete("/mcp")
    @router.get("/mcp/{token}")
    @router.delete("/mcp/{token}")
    async def mcp_no_sse() -> Response:         # SSE 스트림·세션 종료 없음 — POST 한 번에 JSON 한 번
        return Response(status_code=405, headers={"Allow": "POST"})
