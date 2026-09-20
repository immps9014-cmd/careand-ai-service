#!/usr/bin/env bash
# caren 온톨로지 주기 재적재 — cron 진입점
#
#   crontab: 10 * * * * /root/caren/careand-ai-service/ontology/reload.cron.sh
#   (3사 MES 가 :25 hisense · :40 naturefood · :55 infurs 를 쓰므로 caren 은 :10)
#
# load.sh 를 감싸기만 한다. 적재 규칙(그래프 통째 교체·실패 시 이전 그래프 유지)은
# 전부 load.sh 에 있고 여기서 복제하지 않는다. 하는 일은 셋이다:
#   1. 겹쳐 돌지 않게 잠근다.
#   2. 한 줄 로그를 남긴다(3사 reload.cron.sh 와 같은 [시각] OK/FAIL 형식).
#   3. out/status.json 에 상태를 적는다 — 그래프가 언제 것인지 알 수 있어야 한다.
#      data_at 은 **마지막으로 성공한 적재** 시각이다. 실패한 실행이 이 값을 밀어 올리면
#      "방금 기준" 이라고 거짓말하게 된다 — 그래프는 옛것 그대로인데.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${ONTO_LOG:-/var/log/caren-ontology.log}"
STATUS="$DIR/out/status.json"
LOCK=/var/lock/caren-ontology.lock
RUN_LOG=$(mktemp /tmp/caren-onto-reload.XXXXXX)

started=$(date '+%F %T')
t0=$SECONDS

# RAM 3.6GB 서버다. 적재가 매칭·STT 요청과 CPU 를 다투지 않도록 낮춘다.
flock -n "$LOCK" nice -n 15 ionice -c3 "$DIR/load.sh" >"$RUN_LOG" 2>&1
rc=$?

elapsed=$((SECONDS - t0))
strip() { sed 's/\x1b\[[0-9;]*m//g' "$RUN_LOG"; }

if [[ $rc -eq 0 ]]; then
  triples=$(strip | sed -n 's/^그래프 트리플 수: *//p' | tr -d ', ' | sed 's/(.*//' | tail -1)
  warns=$(strip | sed -n 's/.*· WARN \([0-9]*\).*/\1/p' | tail -1)
  echo "[$(date '+%F %T')] OK   트리플 ${triples:-?} · 경고 ${warns:-0}건 · ${elapsed}초" >>"$LOG"
  state=ok
  data_at="$(date '+%F %T')"
elif [[ $rc -eq 1 && ! -s "$RUN_LOG" ]]; then
  # flock -n 이 잠금을 못 얻으면 아무것도 출력하지 않고 1 로 끝난다 — 실패가 아니다.
  echo "[$(date '+%F %T')] SKIP 이전 실행이 아직 돌고 있다" >>"$LOG"
  rm -f "$RUN_LOG"; exit 0
else
  # 그래프 단위 PUT 이라 실패해도 이전 그래프가 그대로 남는다(부분 적재 없음).
  echo "[$(date '+%F %T')] FAIL rc=$rc · ${elapsed}초 — 이전 그래프 유지" >>"$LOG"
  strip | tail -5 | sed 's/^/    /' >>"$LOG"
  state=fail
  data_at=$(sed -n 's/.*"data_at": *"\([^"]*\)".*/\1/p' "$STATUS" 2>/dev/null | head -1)
  triples=$(sed -n 's/.*"triples": *\([0-9]*\).*/\1/p' "$STATUS" 2>/dev/null | head -1)
  warns=$(sed -n 's/.*"warnings": *\([0-9]*\).*/\1/p' "$STATUS" 2>/dev/null | head -1)
fi

if [[ -n "${data_at:-}" ]]; then data_at_json="\"$data_at\""; else data_at_json=null; fi

mkdir -p "$(dirname "$STATUS")"
cat > "$STATUS" <<JSON
{
  "state": "$state",
  "data_at": $data_at_json,
  "run_started_at": "$started",
  "run_finished_at": "$(date '+%F %T')",
  "elapsed_sec": $elapsed,
  "triples": ${triples:-null},
  "warnings": ${warns:-null}
}
JSON

rm -f "$RUN_LOG"
[[ $state == ok ]] || exit 1
