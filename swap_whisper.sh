#!/bin/bash
cd /root/caren/careand-ai-service
LOG=/root/caren/careand-ai-service/model_swap.log
exec > "$LOG" 2>&1
set +e

echo "=== 대기: small 모델 다운로드 완료 ==="
prev=0
while true; do
  if [ -f models/small-ct2/model.bin ]; then
    sz=$(stat -c%s models/small-ct2/model.bin 2>/dev/null || echo 0)
    if [ "$sz" -gt 400000000 ] && [ "$sz" = "$prev" ]; then break; fi
    prev=$sz
  fi
  sleep 5
done
echo "small 준비됨: $(du -sh models/small-ct2 | cut -f1)"

AUDIO="/var/www/careand-backend/storage/app/voice-logs/1/aHROWsflSjX0fjuWoiZ55Ij0OfbjHvYzrKze1PWi.webm"

echo ""
echo "=== base vs small 비교 (동일 오디오) ==="
./venv/bin/python - <<PY
from faster_whisper import WhisperModel
import time, gc, math
AUDIO = "$AUDIO"
for name, path in [("base", "/root/caren/careand-ai-service/models/base-ct2"),
                   ("small", "/root/caren/careand-ai-service/models/small-ct2")]:
    m = WhisperModel(path, device="cpu", compute_type="int8")
    t = time.time()
    segs, info = m.transcribe(AUDIO, language="ko", vad_filter=True)
    seg_list = list(segs)
    el = time.time() - t
    text = " ".join(s.text.strip() for s in seg_list).strip()
    if seg_list:
        avg_lp = sum(s.avg_logprob for s in seg_list) / len(seg_list)
        conf = round(max(0.0, min(math.exp(avg_lp), 1.0)), 3)
    else:
        conf = 0.0
    print(f"[{name}] elapsed={el:.1f}s conf={conf} audio_dur={info.duration:.1f}s")
    print(f"    TEXT: {text}")
    del m; gc.collect()
PY

echo ""
echo "=== .env 적용: WHISPER_MODEL -> small-ct2 ==="
cp .env .env.bak-20260624-whisper
if grep -q '^WHISPER_MODEL=' .env; then
  sed -i 's#^WHISPER_MODEL=.*#WHISPER_MODEL=/root/caren/careand-ai-service/models/small-ct2#' .env
else
  echo 'WHISPER_MODEL=/root/caren/careand-ai-service/models/small-ct2' >> .env
fi
grep '^WHISPER_MODEL=' .env

echo ""
echo "=== careand-ai 재시작 ==="
systemctl restart careand-ai.service
sleep 5
echo "active=$(systemctl is-active careand-ai.service)"

echo ""
echo "=== 재시작 후 STT 스모크 (HTTP, small 로드) ==="
TOKEN=$(grep -E '^AI_SERVICE_TOKEN=' .env | cut -d= -f2-)
curl -s --max-time 150 -X POST http://localhost:8001/ai/voice/transcribe \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"audio_url\":\"$AUDIO\",\"language\":\"ko\"}"
echo ""
echo "=== DONE ==="
