"""
Learning-to-Rank (L2R) 매칭 — feature·룰점수·모델 게이팅의 단일 소스.

설계: rule-v3가 쓰던 서브점수(특기/거리/평점/경험/연속성/성별일치)를 그대로 feature
벡터로 삼아, 과거 "후보→선택" 이력으로 로지스틱회귀를 학습한다. 서빙은
  blended = (1-α)·rule_score + α·P(선택)
로 재랭킹하되, 학습 표본이 임계(ML_L2R_MIN_SAMPLES) 미달이면 모델을 비활성화하고
순수 rule_score로 폴백한다(데이터 부족 시 정성껏 튜닝한 룰을 절대 망치지 않음).
표본이 쌓여 임계를 넘으면 재학습만으로 자동 활성화된다.

main.py(서빙)와 train_l2r.py(학습)가 동일한 subscores/feature_vector를 import해
학습-서빙 feature 불일치(skew)를 원천 차단한다.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

# ── 룰 상수 (rule-v3와 동일 — 여기가 단일 출처) ─────────────────────────
DOMAIN_WEIGHTS: dict[str, tuple[float, float, float, float]] = {
    "senior": (0.4, 0.3, 0.2, 0.1),
    "nursing": (0.4, 0.3, 0.2, 0.1),
    "housekeeping": (0.3, 0.4, 0.2, 0.1),
}
RATING_PRIOR_MEAN = 3.5
RATING_PRIOR_COUNT = 5
CONTINUITY_SATURATION = 3.0
CONTINUITY_WEIGHT = 0.15

# feature 순서 = 모델 입력 순서 (학습/서빙 공유, 변경 시 재학습 필요)
FEATURE_NAMES = ["specialty", "distance", "rating", "experience", "continuity", "gender_match"]


def _get(o: Any, key: str, default=None):
    """dict(학습 행)와 객체(서빙 CaregiverFeature) 양쪽을 동일하게 읽는다."""
    if isinstance(o, dict):
        return o.get(key, default)
    return getattr(o, key, default)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def subscores(recipient: dict, cg: Any, domain: str = "senior",
              preferred_gender: str | None = None) -> dict[str, Any]:
    """rule-v3 _score_caregiver와 동일한 서브점수를 계산해 dict로 반환.
    `_`로 시작하는 키는 reason 표시용 보조값(모델 입력 아님)."""
    diseases = set(recipient.get("diseases") or [])
    specialties = set(_get(cg, "specialties", []) or [])
    r_lat, r_lng = recipient.get("lat"), recipient.get("lng")
    cg_lat, cg_lng = _get(cg, "lat"), _get(cg, "lng")

    matched = diseases & specialties
    if not diseases:
        specialty_score = 0.5
    elif matched:
        specialty_score = min(len(matched) / len(diseases), 1.0)
    else:
        specialty_score = 0.3

    if r_lat is not None and r_lng is not None and cg_lat is not None and cg_lng is not None:
        dist_km = _haversine_km(float(r_lat), float(r_lng), float(cg_lat), float(cg_lng))
        distance_score = max(0.0, 1.0 - dist_km / 10.0)
    else:
        dist_km = None
        distance_score = 0.5

    n = max(0, int(_get(cg, "rating_count", 0) or 0))
    rating_avg = float(_get(cg, "rating_avg", 0.0) or 0.0)
    bayes = (RATING_PRIOR_COUNT * RATING_PRIOR_MEAN + n * rating_avg) / (RATING_PRIOR_COUNT + n)
    rating_score = max(0.0, min(bayes / 5.0, 1.0))

    sessions = int(_get(cg, "completed_sessions", 0) or 0)
    exp_score = min(sessions / 100.0, 1.0)

    prior = int(_get(cg, "prior_matches", 0) or 0)
    continuity_score = min(prior / CONTINUITY_SATURATION, 1.0) if prior > 0 else 0.0

    gender_match = 1.0 if (preferred_gender and _get(cg, "gender") == preferred_gender) else 0.0

    return {
        "specialty": specialty_score,
        "distance": distance_score,
        "rating": rating_score,
        "experience": exp_score,
        "continuity": continuity_score,
        "gender_match": gender_match,
        "_dist_km": dist_km,
        "_matched": matched,
        "_n": n,
        "_sessions": sessions,
        "_prior": prior,
    }


def feature_vector(sub: dict[str, Any]) -> list[float]:
    return [float(sub[k]) for k in FEATURE_NAMES]


def rule_score(sub: dict[str, Any], domain: str = "senior") -> float:
    """rule-v3 최종 점수 (모델 비활성 시 그대로 사용)."""
    w_spec, w_dist, w_rate, w_exp = DOMAIN_WEIGHTS.get(domain, DOMAIN_WEIGHTS["senior"])
    base = (w_spec * sub["specialty"] + w_dist * sub["distance"]
            + w_rate * sub["rating"] + w_exp * sub["experience"])
    final = (1.0 - CONTINUITY_WEIGHT) * base + CONTINUITY_WEIGHT * sub["continuity"]
    return round(final, 3)


# ── 모델 게이팅/로드/예측 ─────────────────────────────────────────────
MODEL_DIR = os.path.join(os.path.dirname(__file__), "models", "l2r")
ARTIFACT_PATH = os.path.join(MODEL_DIR, "model.joblib")
META_PATH = os.path.join(MODEL_DIR, "meta.json")

# 임계: 학습 표본/양성 수가 이만큼은 돼야 모델을 라이브에 쓴다(과적합 방지). env로 조정.
MIN_SAMPLES = int(os.environ.get("ML_L2R_MIN_SAMPLES", "200"))
MIN_POSITIVES = int(os.environ.get("ML_L2R_MIN_POSITIVES", "60"))
BLEND_ALPHA = float(os.environ.get("ML_L2R_ALPHA", "0.5"))
# "1"=임계 무시 강제 활성, "0"=강제 비활성, ""(기본)=임계 기준 자동
FORCE = os.environ.get("ML_L2R_ENABLED", "").strip()

_state: dict[str, Any] = {"loaded": False, "model": None, "meta": None, "active": False}


def _decide_active(meta: dict | None, has_model: bool) -> bool:
    if FORCE == "0" or not has_model or not meta:
        return False
    if FORCE == "1":
        return True
    return (int(meta.get("n_samples", 0)) >= MIN_SAMPLES
            and int(meta.get("n_positives", 0)) >= MIN_POSITIVES
            and meta.get("cv_auc") is not None)


def _load() -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    meta = None
    model = None
    try:
        if os.path.exists(META_PATH):
            with open(META_PATH, encoding="utf-8") as f:
                meta = json.load(f)
        if os.path.exists(ARTIFACT_PATH) and meta and meta.get("feature_names") == FEATURE_NAMES:
            import joblib  # 지연 import (sklearn 미설치 환경에서도 룰 동작)
            model = joblib.load(ARTIFACT_PATH)
    except Exception:
        meta, model = None, None
    _state["meta"], _state["model"] = meta, model
    _state["active"] = _decide_active(meta, model is not None)


def reload() -> None:
    _state["loaded"] = False
    _load()


def is_active() -> bool:
    _load()
    return bool(_state["active"])


def _proba(sub: dict[str, Any]) -> float:
    fv = [feature_vector(sub)]
    return float(_state["model"].predict_proba(fv)[0][1])


def blended_score(sub: dict[str, Any], domain: str = "senior") -> float:
    """모델 활성 시 (1-α)·룰 + α·P(선택), 아니면 순수 룰 점수."""
    rs = rule_score(sub, domain)
    if not is_active():
        return rs
    try:
        p = _proba(sub)
    except Exception:
        return rs
    return round((1.0 - BLEND_ALPHA) * rs + BLEND_ALPHA * p, 3)


def method_tag() -> str:
    """scoring_method 응답 태그."""
    _load()
    if not _state["active"]:
        return "rule-v3"
    m = _state["meta"] or {}
    return f"l2r-blend(n={m.get('n_samples')},auc={m.get('cv_auc')},α={BLEND_ALPHA})"


def status() -> dict[str, Any]:
    """운영 점검용 상태."""
    _load()
    return {
        "active": _state["active"],
        "has_model": _state["model"] is not None,
        "meta": _state["meta"],
        "thresholds": {"min_samples": MIN_SAMPLES, "min_positives": MIN_POSITIVES},
        "alpha": BLEND_ALPHA,
        "force": FORCE or None,
    }
