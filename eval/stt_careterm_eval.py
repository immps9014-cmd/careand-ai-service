"""STT 케어용어 정상 인식률 평가 — 사업계획서 KPI 1 (2026-09-28, 구현계획 S1).

정의(계획서 2.1): 정답 전사에 등장한 핵심 케어용어 중 STT가 올바르게 전사한 용어의 비율(재현율).
  정상처리율 = (정확히 인식된 케어용어 수) / (정답 전사에 등장한 케어용어 총수) × 100

운영 경로와 같은 조건으로 잰다 — main.transcribe()를 그대로 호출하므로 같은 Whisper 모델,
같은 hotwords(온톨로지 CareTerm 어휘 ∪ 대상자 질병 연관어)가 적용된다.

사용법 (venv 파이썬으로):
  venv/bin/python eval/stt_careterm_eval.py --dump-terms eval/stt/terms-ontology.txt   # 기준 용어 목록 뽑기
  venv/bin/python eval/stt_careterm_eval.py --manifest eval/stt/manifest.csv \
      --terms eval/stt/terms-kpi36.txt --term-set kpi36 --label 2026-10-평가1
  → eval/stt/results/<label>.json 생성 → backend 에서
     php artisan kpi:import-stt /root/caren/careand-ai-service/eval/stt/results/<label>.json

manifest.csv 열: file(음성 파일 경로, manifest 기준 상대경로 가능), reference(정답 전사),
                 diseases(선택, '|'로 구분 — 대상자 질병, 운영과 같은 어휘 개인화용)
용어 목록 파일: 한 줄에 용어 하나(# 주석 가능). 안건 5 결정 전까지는 온톨로지 목록을 쓴다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # careand-ai-service 루트 — main·ontology import

KST = timezone(timedelta(hours=9))


def norm(text: str) -> str:
    """띄어쓰기·문장부호 차이는 오인식으로 치지 않는다('연하 곤란' = '연하곤란')."""
    return re.sub(r"[\s\.,!?·'\"()\[\]-]+", "", text or "")


def load_terms(path: str | None) -> tuple[list[str], str]:
    if path:
        with open(path, encoding="utf-8") as f:
            terms = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        return sorted(set(terms)), os.path.splitext(os.path.basename(path))[0]
    import ontology
    vocab = ontology.care_term_vocabulary()
    if not vocab:
        sys.exit("온톨로지(Fuseki)에서 CareTerm 목록을 못 읽었습니다 — --terms 로 목록 파일을 주세요.")
    return sorted(vocab), f"ontology-{len(vocab)}"


def count_terms(text: str, terms: list[str]) -> dict[str, int]:
    t = norm(text)
    out = {}
    for term in terms:
        n = t.count(norm(term))
        if n:
            out[term] = n
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="STT 케어용어 정상 인식률 평가 (KPI 1)")
    ap.add_argument("--manifest", help="평가 목록 CSV (file,reference[,diseases])")
    ap.add_argument("--terms", help="기준 용어 목록 파일(없으면 온톨로지 CareTerm 전체)")
    ap.add_argument("--term-set", help="기준 목록 이름(없으면 파일명/ontology-N)")
    ap.add_argument("--label", help="평가 실행 이름(결과 파일명)")
    ap.add_argument("--out", help="결과 JSON 경로(기본 eval/stt/results/<label>.json)")
    ap.add_argument("--dump-terms", metavar="PATH", help="온톨로지 CareTerm 목록을 파일로 쓰고 종료")
    args = ap.parse_args()

    if args.dump_terms:
        terms, name = load_terms(None)
        with open(args.dump_terms, "w", encoding="utf-8") as f:
            f.write(f"# 온톨로지 CareTerm 전체 {len(terms)}개 — {datetime.now(KST):%Y-%m-%d} 추출\n")
            f.write("\n".join(terms) + "\n")
        print(f"{len(terms)}개 → {args.dump_terms}")
        return

    if not args.manifest:
        ap.error("--manifest 가 필요합니다 (또는 --dump-terms)")

    terms, default_set = load_terms(args.terms)
    term_set = args.term_set or default_set
    label = args.label or f"stt-{datetime.now(KST):%Y%m%d-%H%M}"
    base = os.path.dirname(os.path.abspath(args.manifest))

    with open(args.manifest, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("file") or "").strip()]
    if not rows:
        sys.exit("manifest 에 평가 음성이 없습니다.")

    import main as svc  # 운영과 같은 transcribe() — 모델·hotwords 동일

    per_term: dict[str, dict[str, int]] = {t: {"ref": 0, "hit": 0} for t in terms}
    samples = []
    total = correct = 0
    for i, r in enumerate(rows, 1):
        path = r["file"].strip()
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        diseases = [d.strip() for d in (r.get("diseases") or "").split("|") if d.strip()]
        t0 = time.time()
        res = svc.transcribe(svc.TranscribeRequest(audio_url=path, diseases=diseases))
        hyp = res["stt_text"]
        ref_c = count_terms(r["reference"], terms)
        hyp_c = count_terms(hyp, terms)
        s_total = sum(ref_c.values())
        s_hit = sum(min(n, hyp_c.get(t, 0)) for t, n in ref_c.items())
        for t, n in ref_c.items():
            per_term[t]["ref"] += n
            per_term[t]["hit"] += min(n, hyp_c.get(t, 0))
        total += s_total
        correct += s_hit
        missed = [t for t, n in ref_c.items() if hyp_c.get(t, 0) < n]
        samples.append({
            "file": r["file"].strip(), "terms_total": s_total, "terms_correct": s_hit,
            "missed": missed, "stt_text": hyp, "sec": round(time.time() - t0, 1),
        })
        print(f"[{i}/{len(rows)}] {r['file'].strip()}: {s_hit}/{s_total}" + (f"  놓침: {', '.join(missed)}" if missed else ""))

    engine = res.get("model") or getattr(svc, "WHISPER_MODEL_NAME", "whisper")
    result = {
        "run_label": label,
        "term_set": term_set,
        "stt_engine": f"{engine} + hotwords",
        "samples": len(rows),
        "terms_total": total,
        "terms_correct": correct,
        "rate": round(correct / total * 100, 2) if total else None,
        "evaluated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "details": {
            "terms": {t: v for t, v in per_term.items() if v["ref"]},
            "samples": samples,
        },
    }
    out = args.out or os.path.join(HERE, "stt", "results", f"{label}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    rate = f"{result['rate']:.2f}%" if total else "계산 불가(정답에 케어용어 없음)"
    print(f"\n정상 인식률 {correct}/{total} = {rate}  ({term_set}, 음성 {len(rows)}건)\n→ {out}")
    print(f"적재: php artisan kpi:import-stt {out}")


if __name__ == "__main__":
    main()
