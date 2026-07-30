#!/bin/bash
# whisper base CT2 model.bin curl 재시도 다운로더 (resume, 엔드포인트 교차)
# 목표 크기: 74790398 bytes (Systran/faster-whisper-base model.bin)
OUT=/root/caren/careand-ai-service/models/base-ct2/model.bin
TARGET=74790398
URLS=(
  "https://hf-mirror.com/Systran/faster-whisper-base/resolve/main/model.bin"
  "https://huggingface.co/Systran/faster-whisper-base/resolve/main/model.bin"
)
for i in $(seq 1 288); do  # 3분 간격 최대 ~14시간
  size=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
  if [ "$size" -ge "$TARGET" ]; then
    echo "[$(date +%F\ %T)] FETCH-COMPLETE size=$size"
    exit 0
  fi
  url="${URLS[$((i % 2))]}"
  echo "[$(date +%F\ %T)] attempt=$i size=$size url=$url"
  curl -sL -C - --connect-timeout 15 --speed-limit 1024 --speed-time 30 -o "$OUT" "$url"
  rc=$?
  size=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
  echo "[$(date +%F\ %T)] attempt=$i done rc=$rc size=$size"
  if [ "$size" -ge "$TARGET" ]; then
    echo "[$(date +%F\ %T)] FETCH-COMPLETE size=$size"
    exit 0
  fi
  sleep 180
done
echo "[$(date +%F\ %T)] FETCH-GIVEUP"
exit 1
