#!/usr/bin/env python
"""
caren-ontology MCP 서버 — stdio 전송 (CAREN-ONT-MCP C1. HTTP 전송은 mcp_caren.py 의 router)

  등록: claude mcp add --scope user caren-ontology -- \
          /root/caren/careand-ai-service/venv/bin/python /root/caren/careand-ai-service/mcp_stdio.py

한 줄에 JSON-RPC 메시지 하나. stdout 은 프로토콜 전용이다 — 진단은 전부 stderr 로.
도구·리소스는 mcp_caren.handle() 그대로라 전송과 무관하게 동작이 같다(tx server.php 와 같은 구조).
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import mcp_caren  # noqa: E402


def send(m: dict) -> None:
    sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    session = mcp_caren.new_session()
    session["client"] = "stdio"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 파싱 실패"}})
            continue
        if not isinstance(msg, dict):
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "잘못된 메시지"}})
            continue
        is_request = "id" in msg
        try:
            result = mcp_caren.handle(msg, session)
            if is_request:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except mcp_caren.RpcError as e:
            if is_request:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.rpc_code, "message": str(e)}})
        except Exception as e:  # noqa: BLE001 — 프로토콜 응답은 항상 돌려준다
            print(f"[caren-ontology] {type(e).__name__}: {e}", file=sys.stderr)
            if is_request:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "서버 내부 오류"}})


if __name__ == "__main__":
    main()
