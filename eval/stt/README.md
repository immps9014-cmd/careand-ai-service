# STT 케어용어 인식률 평가 (사업계획서 KPI 1)

목표: 정상처리율(STT 케어용어 정상 인식률) **90%**.
정의: 정답 전사에 등장한 케어용어 중 STT가 맞게 전사한 비율(재현율). 띄어쓰기·문장부호 차이는 오인식으로 치지 않는다.

## 준비물 (도입기업 협조 — S0 회의 요청 사항, 기한 10/17)
- 실제 케어 상황을 재연한 음성 30건(3–5분, 휴대폰 녹음 webm/m4a/wav) → `eval/stt/audio/`
- 음성마다 정답 전사(사람이 들은 그대로) → `eval/stt/manifest.csv`
- 녹음 참여 동의서 (음성은 개인정보 — `audio/` 는 git 에 올리지 않는다)

manifest.csv
```
file,reference,diseases
audio/001.m4a,"어르신 오늘 점심 반 그릇 드셨고 욕창 부위 소독했습니다 ...",치매|고혈압
```
diseases 는 선택(대상자 질병, `|` 구분) — 운영과 같게 질병 연관 어휘가 hotwords 에 더해진다.

## 기준 용어 목록
- `terms-ontology.txt` — 온톨로지 CareTerm 전체(현재 38개, `--dump-terms` 로 재추출)
- 협약 KPI 는 CareTerm **36개** 기준 — S0 회의 안건 5 결정 후 `terms-kpi36.txt` 를 만든다.

## 실행
```
cd /root/caren/careand-ai-service
venv/bin/python eval/stt_careterm_eval.py --manifest eval/stt/manifest.csv \
    --terms eval/stt/terms-kpi36.txt --term-set kpi36 --label 2026-10-평가1
cd /var/www/careand-backend && php artisan kpi:import-stt /root/caren/careand-ai-service/eval/stt/results/2026-10-평가1.json
```
적재하면 관리자 대시보드 「사업 KPI」 카드에 최신 평가가 표시된다.
운영 서비스와 같은 `main.transcribe()`(같은 Whisper 모델·hotwords)를 쓰며, 모델을 한 번 더 메모리에 올린다(약 0.5GB).
