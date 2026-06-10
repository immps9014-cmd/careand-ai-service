"""model.bin 전용 재시도 다운로더 — 엔드포인트 교차, 5분 간격, 최대 12시간."""
from __future__ import annotations
import os
import sys
import time

CACHE = "/root/careand-ai-service/models"
REPO = "Systran/faster-whisper-base"
ENDPOINTS = ["https://hf-mirror.com", "https://huggingface.co"]
MIN_SIZE = 70 * 1024 * 1024


def done() -> str | None:
    base = os.path.join(CACHE, "models--Systran--faster-whisper-base", "snapshots")
    if not os.path.isdir(base):
        return None
    for snap in os.listdir(base):
        p = os.path.join(base, snap, "model.bin")
        if os.path.exists(p) and os.path.getsize(os.path.realpath(p)) >= MIN_SIZE:
            return p
    return None


for attempt in range(1, 145):
    if done():
        print(f"COMPLETE attempt={attempt} path={done()}", flush=True)
        sys.exit(0)
    ep = ENDPOINTS[attempt % 2]
    os.environ["HF_ENDPOINT"] = ep
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    try:
        from huggingface_hub import hf_hub_download

        hf_hub_download(REPO, "model.bin", cache_dir=CACHE)
        if done():
            print(f"COMPLETE attempt={attempt} endpoint={ep} path={done()}", flush=True)
            sys.exit(0)
        print(f"attempt={attempt} endpoint={ep} downloaded but size check failed", flush=True)
    except Exception as e:
        print(f"attempt={attempt} endpoint={ep} FAIL: {type(e).__name__}: {str(e)[:120]}", flush=True)
    time.sleep(300)

print("GIVEUP after 144 attempts", flush=True)
sys.exit(1)
