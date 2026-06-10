"""whisper 모델 백그라운드 다운로더 — 네트워크 플랩 대응 재시도(resume 지원).

hf 본 엔드포인트와 미러를 번갈아 시도. 완료 시 COMPLETE 출력 후 종료.
"""
import os
import sys
import time

CACHE = "/root/careand-ai-service/models"
REPO = "Systran/faster-whisper-base"
ENDPOINTS = ["https://huggingface.co", "https://hf-mirror.com"]

for attempt in range(1, 145):  # 최대 ~12시간 (5분 간격)
    ep = ENDPOINTS[attempt % len(ENDPOINTS)]
    os.environ["HF_ENDPOINT"] = ep
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "30"
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(REPO, cache_dir=CACHE)
        print(f"COMPLETE attempt={attempt} endpoint={ep} path={path}", flush=True)
        sys.exit(0)
    except BaseException as e:
        print(f"attempt={attempt} endpoint={ep} FAIL: {type(e).__name__}: {str(e)[:140]}", flush=True)
        time.sleep(300)

print("GIVEUP after 144 attempts", flush=True)
sys.exit(1)
