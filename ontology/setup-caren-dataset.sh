#!/bin/bash
# 기존 moai-fuseki 컨테이너(host network, localhost:3030, 인증 없이 관리 API 개방)에
# caren 전용 데이터셋을 새로 만들고 care-domain.ttl을 적재한다.
# 새 컨테이너/JVM을 띄우지 않음 — 이 서버는 RAM 여유가 거의 없어(free -h 기준 available < 600Mi)
# 기존 Fuseki 인스턴스를 멀티테넌트로 공유하는 쪽을 택함.
set -euo pipefail

FUSEKI_URL="${FUSEKI_URL:-http://localhost:3030}"
DATASET="${DATASET:-caren}"
TTL_FILE="$(dirname "$0")/care-domain.ttl"

echo "[1/3] 데이터셋 존재 확인: ${DATASET}"
if curl -sf "${FUSEKI_URL}/\$/datasets/${DATASET}" > /dev/null 2>&1; then
    echo "  이미 존재 — 생성 단계 건너뜀"
else
    echo "[2/3] TDB2 데이터셋 생성 (mem 아님 — 컨테이너 재시작에도 유실 안 되게)"
    curl -sf -X POST "${FUSEKI_URL}/\$/datasets" \
        --data-urlencode "dbName=${DATASET}" \
        --data-urlencode "dbType=tdb2" \
        -o /dev/null
fi

echo "[3/3] care-domain.ttl 적재"
curl -sf -X POST "${FUSEKI_URL}/${DATASET}/data?default" \
    -H "Content-Type: text/turtle" \
    --data-binary "@${TTL_FILE}" \
    -o /dev/null

echo "완료. 확인:"
echo "  curl -s '${FUSEKI_URL}/${DATASET}/sparql' --data-urlencode 'query=SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }' -H 'Accept: application/sparql-results+json'"
