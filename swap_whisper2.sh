#!/bin/bash
cd /root/careand-ai-service
LOG=/root/careand-ai-service/model_swap.log
exec > "$LOG" 2>&1
set +e

AUDIO="/var/www/careand-backend/storage/app/voice-logs/1/aHROWsflSjX0fjuWoiZ55Ij0OfbjHvYzrKze1PWi.webm"

echo "=== [1/4] small 모델 다운로드 (재시도 포함) ==="
./venv/bin/python - <<'PY'
from faster_whisper import download_model
import time, os
OUT = "/root/careand-ai-service/models/small-ct2"
for attempt in range(1, 9):
    try:
        p = download_model("small", output_dir=OUT)
        sz = os.path.getsize(os.path.join(OUT, "model.bin"))
        print(f"OK downloaded attempt={attempt} model.bin={sz/1e6:.0f}MB -> {p}")
        break
    except Exception as e:
        print(f"attempt {attempt} FAILED: {e}")
        time.sleep(6)
else:
    print("DOWNLOAD GAVE UP")
PY

if [ ! -f models/small-ct2/model.bin ] || [ "$(stat -c%s models/small-ct2/model.bin 2>/dev/null || echo 0)" -lt 400000000 ]; then
  echo "!!! 다운로드 실패 — 중단(.env 미변경, 재시작 안함)"
  echo "=== DONE ==="
  exit 1
fi
echo "small 준비됨: $(du -sh models/small-ct2 | cut -f1)"

echo ""
echo "=== [2/4] base vs small 비교 (동일 오디오) ==="
./venv/bin/python - <<PY
from faster_whisper import WhisperModel
import time, gc, math
AUDIO = "$AUDIO"
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
PY

echo ""
echo "=== [3/4] .env 적용: WHISPER_MODEL -> small-ct2 ==="
cp .env .env.bak-20260624-whisper
if grep -q '^WHISPER_MODEL=' .env; then
  sed -i 's#^WHISPER_MODEL=.*#WHISPER_MODEL=/root/careand-ai-service/models/small-ct2#' .env
else
  echo 'WHISPER_MODEL=/root/careand-ai-service/models/small-ct2' >> .env
fi
grep '^WHISPER_MODEL=' .env

echo ""
echo "=== [4/4] careand-ai 재시작 + 스모크 ==="
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
