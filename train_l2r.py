#!/usr/bin/env python
"""
L2R 매칭 모델 학습기.

학습 데이터: match_candidates 1행 = 1샘플. 라벨 = 그 후보의 케어기버가 해당
request의 실제 매칭(matches)으로 선택됐는가(1/0). feature는 서빙과 동일하게
l2r.subscores로 계산(학습-서빙 skew 차단).

산출물: models/l2r/model.joblib + meta.json. 표본/양성 수가 임계 미달이어도
아티팩트는 남기되, 게이팅(l2r._decide_active)이 라이브 활성 여부를 판단한다.

실행: ./venv/bin/python train_l2r.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import l2r

DB = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "careand"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "careand_platform"),
)


def _jsoncol(v):
    if v is None:
        return []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def _f(v):
    return float(v) if v is not None else None


def load_dotenv_env():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_rows():
    import pymysql
    load_dotenv_env()
    DB.update(password=os.environ.get("DB_PASSWORD", DB["password"]))
    conn = pymysql.connect(charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, **DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, service_domain, senior_id, nursing_patient_id, "
                        "service_address_id, category_id, requirements, created_at "
                        "FROM match_requests")
            requests = {r["id"]: r for r in cur.fetchall()}

            cur.execute("SELECT id, diseases, home_lat, home_lng, gender FROM seniors")
            seniors = {r["id"]: r for r in cur.fetchall()}
            cur.execute("SELECT id, diseases, hospital_lat, hospital_lng, gender FROM nursing_patients")
            patients = {r["id"]: r for r in cur.fetchall()}
            cur.execute("SELECT id, lat, lng FROM service_addresses")
            addrs = {r["id"]: r for r in cur.fetchall()}

            cur.execute("SELECT id, specialties, rating_avg, rating_count, completed_sessions, "
                        "base_lat, base_lng, gender FROM caregivers")
            caregivers = {r["id"]: r for r in cur.fetchall()}

            cur.execute("SELECT request_id, caregiver_id, created_at FROM match_candidates")
            candidates = cur.fetchall()
            cur.execute("SELECT request_id, caregiver_id, created_at FROM matches")
            matches = cur.fetchall()
    finally:
        conn.close()
    return requests, seniors, patients, addrs, caregivers, candidates, matches


def recipient_key(req):
    d = req["service_domain"]
    if d == "nursing":
        return ("nursing", req["nursing_patient_id"])
    if d == "housekeeping":
        return ("housekeeping", req["service_address_id"])
    return ("senior", req["senior_id"])


def recipient_features(req, seniors, patients, addrs):
    d = req["service_domain"]
    if d == "nursing":
        p = patients.get(req["nursing_patient_id"]) or {}
        return {"diseases": _jsoncol(p.get("diseases")), "lat": _f(p.get("hospital_lat")),
                "lng": _f(p.get("hospital_lng"))}
    if d == "housekeeping":
        a = addrs.get(req["service_address_id"]) or {}
        return {"diseases": [], "lat": _f(a.get("lat")), "lng": _f(a.get("lng"))}
    s = seniors.get(req["senior_id"]) or {}
    return {"diseases": _jsoncol(s.get("diseases")), "lat": _f(s.get("home_lat")),
            "lng": _f(s.get("home_lng"))}


def build_dataset():
    requests, seniors, patients, addrs, caregivers, candidates, matches = fetch_rows()

    # 라벨: (request_id, caregiver_id) ∈ matches
    selected = {(m["request_id"], m["caregiver_id"]) for m in matches}

    # 연속성: 케어기버×대상자 과거 매칭 시각 목록 (point-in-time prior 계산용)
    hist: dict = {}
    for m in matches:
        req = requests.get(m["request_id"])
        if not req:
            continue
        key = (m["caregiver_id"], recipient_key(req))
        hist.setdefault(key, []).append(m["created_at"])

    X, y, domains = [], [], []
    skipped = 0
    for c in candidates:
        req = requests.get(c["request_id"])
        cg = caregivers.get(c["caregiver_id"])
        if not req or not cg:
            skipped += 1
            continue
        domain = req["service_domain"] or "senior"
        if domain not in l2r.DOMAIN_WEIGHTS:
            # l2r.rule_score()/subscores()가 실제로 다루는 도메인(DOMAIN_WEIGHTS의 senior/nursing/
            # housekeeping)만 학습 표본으로 쓴다. postpartum은 /matching/postpartum이 별도
            # 룰스코어러(_score_postpartum_caregiver)로 서빙하고 이 l2r 파이프라인을 아예 안 씀 —
            # 게다가 postpartum 요청은 senior_id가 NULL이라(비시니어 도메인, DB 마이그레이션
            # 2026_06_12_100001) recipient_features()에 넣으면 diseases=[]/lat=lng=None인
            # 가짜 행만 만들어져 여기 포함시키면 안 됨.
            skipped += 1
            continue
        rec = recipient_features(req, seniors, patients, addrs)

        prior = sum(1 for t in hist.get((c["caregiver_id"], recipient_key(req)), [])
                    if req["created_at"] and t and t < req["created_at"])

        cg_feat = {
            "specialties": _jsoncol(cg.get("specialties")),
            "rating_avg": cg.get("rating_avg"),
            "rating_count": cg.get("rating_count"),
            "completed_sessions": cg.get("completed_sessions"),
            "lat": _f(cg.get("base_lat")),
            "lng": _f(cg.get("base_lng")),
            "gender": cg.get("gender"),
            "prior_matches": prior,
        }
        pref = (_jsoncol(req.get("requirements")) or {})
        pref_gender = pref.get("preferred_gender") if isinstance(pref, dict) else None

        sub = l2r.subscores(rec, cg_feat, domain, pref_gender)
        X.append(l2r.feature_vector(sub))
        y.append(1 if (c["request_id"], c["caregiver_id"]) in selected else 0)
        domains.append(domain)
    return X, y, domains, skipped


def main():
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    X, y, domains, skipped = build_dataset()
    n = len(y)
    n_pos = int(sum(y))
    n_neg = n - n_pos
    print(f"samples={n} positives={n_pos} negatives={n_neg} skipped={skipped}")

    if n < 4 or n_pos == 0 or n_neg == 0:
        print("학습 불가(표본 부족 또는 단일 클래스) — 아티팩트 미생성, 룰 폴백 유지.")
        return

    Xa, ya = np.array(X, dtype=float), np.array(y, dtype=int)
    model = LogisticRegression(max_iter=2000, class_weight="balanced")

    # 교차검증 AUC (양 클래스 최소 표본이 fold 수 이상일 때만)
    cv_auc = None
    try:
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        folds = min(5, n_pos, n_neg)
        if folds >= 2:
            skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
            scores = cross_val_score(model, Xa, ya, cv=skf, scoring="roc_auc")
            cv_auc = round(float(scores.mean()), 4)
            print(f"CV AUC ({folds}-fold) = {cv_auc}  (per-fold {[round(s,3) for s in scores]})")
        else:
            print("CV 생략(클래스별 표본<2)")
    except Exception as e:
        print(f"CV 실패: {e}")

    model.fit(Xa, ya)
    coef = dict(zip(l2r.FEATURE_NAMES, [round(float(c), 4) for c in model.coef_[0]]))
    print(f"coef = {coef}  intercept={round(float(model.intercept_[0]),4)}")

    os.makedirs(l2r.MODEL_DIR, exist_ok=True)
    import joblib
    joblib.dump(model, l2r.ARTIFACT_PATH)
    meta = {
        "n_samples": n,
        "n_positives": n_pos,
        "n_negatives": n_neg,
        "cv_auc": cv_auc,
        "feature_names": l2r.FEATURE_NAMES,
        "coef": coef,
        "intercept": round(float(model.intercept_[0]), 4),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "sklearn_version": __import__("sklearn").__version__,
        "min_samples": l2r.MIN_SAMPLES,
        "min_positives": l2r.MIN_POSITIVES,
    }
    with open(l2r.META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    active = l2r._decide_active(meta, True)
    print(f"saved → {l2r.ARTIFACT_PATH}")
    print(f"라이브 활성 여부(게이트): {active}  "
          f"(임계 n>={l2r.MIN_SAMPLES}, pos>={l2r.MIN_POSITIVES})")


if __name__ == "__main__":
    main()
