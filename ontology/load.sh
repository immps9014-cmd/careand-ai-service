#!/usr/bin/env bash
# caren 온톨로지 적재 — 스키마 → 추출 → Fuseki 그래프 교체 → 불변식 점검
#
#   사용: ontology/load.sh [--skip-etl] [--schema-only]
#
# 원칙(3사 MES 온톨로지 load.sh 와 동일):
#   · 그래프 단위 PUT 이라 교체가 통째로 일어난다. 실패하면 아무것도 바뀌지 않는다.
#   · 문법 검증을 따로 돌리지 않는다 — PUT 이 파싱과 저장을 한 트랜잭션에서 하므로
#     깨진 입력은 400(줄·칸 번호 포함)으로 거부되고 기존 그래프가 그대로 남는다.
#     컨테이너 안에서 riot 를 따로 띄우면 moai-fuseki 메모리 한도(550MB)에 걸려
#     스래싱한다 — 그 컨테이너는 moai·kcro·tx-mes 도 함께 서비스한다. 다시 넣지 말 것.
#   · 순서가 중요하다: **스키마를 먼저 올려야** etl_caren.py 가 거기서 어휘(care:code)를
#     읽어 갈 수 있다. 어휘를 ETL 에 베껴 적지 않기 위한 의존이다.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(dirname "$DIR")"
FUSEKI="${FUSEKI_URL:-http://localhost:3030}"
DATASET="${FUSEKI_DATASET:-caren}"
PYTHON="${PYTHON:-$APP/venv/bin/python}"

GRAPH_SCHEMA="http://caren.aiclaude.kr/graph/schema"
GRAPH_DATA="http://caren.aiclaude.kr/graph/caren"
TTL="$DIR/care-domain.ttl"
NT="$DIR/out/caren.nt"

skip_etl=0; schema_only=0
for a in "$@"; do
  case "$a" in
    --skip-etl)    skip_etl=1 ;;
    --schema-only) schema_only=1 ;;
    *) echo "알 수 없는 인자: $a" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m▸\033[0m %s\n' "$*"; }

if ! curl -fsS -m 5 "$FUSEKI/\$/ping" >/dev/null; then
  echo "Fuseki 응답 없음: $FUSEKI — moai-fuseki 컨테이너 확인(/root/scripts/fuseki-watchdog.sh)" >&2
  exit 1
fi

put_graph() {
  local f="$1" graph="$2" ctype="$3" code
  code=$(curl -sS -o /tmp/fuseki-put-caren.log -w '%{http_code}' -m 300 \
         -X PUT -H "Content-Type: $ctype" --data-binary "@$f" \
         "$FUSEKI/$DATASET/data?graph=$graph")
  if [[ "$code" != "200" && "$code" != "201" && "$code" != "204" ]]; then
    echo "적재 실패 (HTTP $code): $graph — 기존 그래프 유지" >&2
    cat /tmp/fuseki-put-caren.log >&2; return 1
  fi
  say "적재 완료 → $graph (HTTP $code)"
}

# ── 1. 스키마 ───────────────────────────────────────────────────────────────
say "스키마 적재: $TTL"
put_graph "$TTL" "$GRAPH_SCHEMA" "text/turtle"
[[ $schema_only -eq 1 ]] && { say "스키마만 적재하고 종료"; exit 0; }

# ── 2. 추출(읽기 전용) ──────────────────────────────────────────────────────
if [[ $skip_etl -eq 0 ]]; then
  say "추출: etl_caren.py"
  "$PYTHON" "$DIR/etl_caren.py"
else
  say "추출 건너뜀 — 기존 $NT 사용"
fi
[[ -s "$NT" ]] || { echo "추출 결과가 비어 있다: $NT" >&2; exit 1; }

# ── 3. 적재 ─────────────────────────────────────────────────────────────────
say "적재: $NT ($(wc -l < "$NT") 트리플)"
put_graph "$NT" "$GRAPH_DATA" "application/n-triples"

# ── 4. 사후 점검 ────────────────────────────────────────────────────────────
say "불변식 점검"
"$PYTHON" "$DIR/check.py"
