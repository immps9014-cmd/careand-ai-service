#!/usr/bin/env bash
# L2R 매칭 모델 재학습 + 반영.
# train_l2r.py로 최신 이력으로 재학습(models/l2r/) 후 careand-ai를 재시작해
# 새 모델/게이트 판정을 로드한다. 데이터가 임계(ML_L2R_MIN_SAMPLES/POSITIVES)를
# 넘으면 이 한 번으로 L2R 블렌딩이 자동 활성화된다.
#
# 주기 재학습 예(매주 일요일 04:10):
#   10 4 * * 0 /root/caren/careand-ai-service/retrain.sh >> /root/caren/careand-ai-service/retrain.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
echo "=== $(date '+%F %T') L2R 재학습 시작 ==="
./venv/bin/python train_l2r.py
systemctl restart careand-ai
sleep 2
systemctl is-active careand-ai
echo "=== 재학습 완료 ==="
