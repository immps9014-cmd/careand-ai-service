#!/usr/bin/env python
"""
caren-ontology 원격(HTTP) 접속 토큰 관리 — root 로 실행 (tx token.php 이식)

  venv/bin/python mcp_token.py add <이름>      새 토큰 발급(원문은 이때 한 번만 출력된다)
  venv/bin/python mcp_token.py list            이름·발급일·폐기 여부
  venv/bin/python mcp_token.py revoke <이름>   폐기 — 즉시 효력(서비스 재시작 불필요)

저장은 /etc/caren-mcp/tokens.json 에 sha256 만(0600 root — careand-ai 가 root 로 돈다).
토큰을 잃어버리면 되살릴 수 없다. 폐기하고 새로 발급한다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime

FILE = os.environ.get("CAREN_MCP_TOKENS") or "/etc/caren-mcp/tokens.json"


def load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as fh:
            j = json.load(fh)
        return j if isinstance(j, dict) and "tokens" in j else {"tokens": []}
    except (OSError, ValueError):
        return {"tokens": []}


def save(j: dict) -> None:
    d = os.path.dirname(FILE)
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(j, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def fail(m: str) -> None:
    print(m, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd in ("add", "revoke"):
        if not re.match(r"^[A-Za-z0-9._-]{2,40}$", name):
            fail("이름은 영문·숫자·._- 2~40자")
        if os.geteuid() != 0:
            fail("root 로 실행하세요")

    j = load()
    if cmd == "add":
        for t in j["tokens"]:
            if t["name"] == name and not t.get("revoked"):
                fail(f"이미 쓰는 이름입니다: {name} (먼저 revoke)")
        raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")  # 43자, URL 경로 가능
        j["tokens"].append({"name": name, "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "revoked": False})
        save(j)
        print(f"발급: {name}\n토큰: {raw}")
        print(f"  헤더 방식 : Authorization: Bearer {raw}  →  https://caren.aiclaude.kr/mcp")
        print(f"  경로 방식 : https://caren.aiclaude.kr/mcp/{raw}   (claude.ai 커넥터처럼 헤더를 못 넣을 때)")
        print("이 원문은 다시 볼 수 없습니다.")
    elif cmd == "revoke":
        hit = 0
        for t in j["tokens"]:
            if t["name"] == name and not t.get("revoked"):
                t["revoked"] = datetime.now().astimezone().isoformat(timespec="seconds")
                hit += 1
        if not hit:
            fail(f"살아 있는 토큰이 없습니다: {name}")
        save(j)
        print(f"폐기: {name}")
    elif cmd == "list":
        for t in j["tokens"]:
            state = f"폐기 {t['revoked'][:10]}" if t.get("revoked") else "사용중"
            print(f"{t['name']:<24} {t['created'][:10]}  {state}  {t['sha256'][:8]}…")
        if not j["tokens"]:
            print("(없음)")
    else:
        fail("사용: mcp_token.py add|list|revoke <이름>")


if __name__ == "__main__":
    main()
