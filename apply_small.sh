#!/bin/bash
cd /root/careand-ai-service
LOG=/root/careand-ai-service/model_swap.log
exec > "$LOG" 2>&1
set +e

AUDIO="/var/www/careand-backend/storage/app/voice-logs/1/aHROWsflSjX0fjuWoiZ55Ij0OfbjHvYzrKze1PWi.webm"

echo "=== [1/3] small 모델 로드 검증 + base vs small 비교 (동일 오디오) ==="
./venv/bin/python - <<PY
from faster_whisper import WhisperModel
import time, gc, math, sys
AUDIO = "$AUDIO"
try:
    for name, path in [("base", "/root/careand-ai-service/models/base-ct2"),
                       ("small", "/root/careand-ai-service/models/small-ct2")]:
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
    print("LOAD_OK")
except Exception as e:
    print("LOAD_FAILED:", e); sys.exit(1)
PY
if ! grep -q "LOAD_OK" "$LOG"; then
  echo "!!! small 모델 로드/추론 실패 — .env 미변경, 재시작 안함"
  echo "=== DONE ==="
  exit 1
fi

echo ""
echo "=== [2/3] .env 적용: WHISPER_MODEL -> small-ct2 ==="
cp .env .env.bak-20260624-whisper
if grep -q '^WHISPER_MODEL=' .env; then
  sed -i 's#^WHISPER_MODEL=.*#WHISPER_MODEL=/root/careand-ai-service/models/small-ct2#' .env
else
  echo 'WHISPER_MODEL=/root/careand-ai-service/models/small-ct2' >> .env
fi
grep '^WHISPER_MODEL=' .env

echo ""
echo "=== [3/3] careand-ai 재시작 + 스모크 ==="
systemctl restart careand-ai.service
sleep 5
echo "active=$(systemctl is-active careand-ai.service)"
TOKEN=$(grep -E '^AI_SERVICE_TOKEN=' .env | cut -d= -f2-)
echo "smoke STT(small):"
curl -s --max-time 150 -X POST http://localhost:8001/ai/voice/transcribe \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"audio_url\":\"$AUDIO\",\"language\":\"ko\"}"
echo ""
echo "=== DONE ==="
