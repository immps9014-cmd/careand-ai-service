"""
Care& AI 마이크로서비스 (FastAPI stub)

Laravel 백엔드의 AiService.php가 호출하는 6개 엔드포인트를 mock 응답으로 노출.
향후 careand-ml 산출물(matching/anomaly/forecast/llm/voice/rag)을 단계적으로 교체.

엔드포인트:
  POST /ai/match/recommend       - 매칭 추천
  POST /ai/voice/transcribe       - STT
  POST /ai/voice/summarize        - 일지 요약 (보호자/의료진)
  POST /ai/anomaly/score          - 이상징후 스코어
  POST /ai/chatbot/answer         - 챗봇 RAG
  POST /ai/forecast/demand        - 수요 예측

인증: Authorization: Bearer <AI_SERVICE_TOKEN> (env로 검증)
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

EXPECTED_TOKEN = os.environ.get("AI_SERVICE_TOKEN", "")


# ───────────────────────── DB 연결 헬퍼 ─────────────────────────
def get_db_conn():
    """careand_platform MySQL 연결 (PyMySQL)."""
    import pymysql

    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "careand"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "careand_platform"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

app = FastAPI(
    title="Care& AI Service",
    version="0.1.0-stub",
    description="Care& AI 마이크로서비스 — Phase D-1 stub 단계",
)


def verify_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer 토큰 검증 (EXPECTED_TOKEN이 비어있으면 검증 생략 — 로컬/스텁용)."""
    if not EXPECTED_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required")
    if authorization.removeprefix("Bearer ").strip() != EXPECTED_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "careand-ai-service",
        "phase": "stub",
        "models": {
            "matching": "stub",
            "stt": "stub",
            "llm": "stub",
            "anomaly": "stub",
            "forecast": "stub",
            "rag": "stub",
        },
    }


# ───────────────────────── 1. 매칭 추천 (룰 기반) ─────────────────────────
#
# 점수 = 0.4 * 특기일치  +  0.3 * 거리점수  +  0.2 * 평점점수  +  0.1 * 경험점수
#  - 특기일치: 어르신 질환↔인력 특기 교집합 비율 (0~1)
#  - 거리점수: max(0, 1 - dist_km/10) (10km 안에서만 양의 점수)
#  - 평점점수: rating_avg / 5.0
#  - 경험점수: min(completed_sessions / 100, 1.0)
#
# 향후 careand-ml의 ALS+KoSimCSE 학습 모델로 교체 가능.

import math


class CaregiverFeature(BaseModel):
    id: int
    specialties: list[str] = []
    rating_avg: float = 0.0
    completed_sessions: int = 0
    lat: float | None = None
    lng: float | None = None


class MatchRecommendRequest(BaseModel):
    request_id: int
    senior: dict[str, Any]
    caregivers: list[CaregiverFeature]
    top_k: int = 5
    min_score: float = 0.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 사이 거리 (km)."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _score_caregiver(senior: dict[str, Any], cg: CaregiverFeature) -> tuple[float, list[str]]:
    diseases = set(senior.get("diseases") or [])
    specialties = set(cg.specialties or [])
    senior_lat = senior.get("lat")
    senior_lng = senior.get("lng")

    # 1. 특기 일치도 (0.3~1.0)
    #    - 데이터 없음: 0.5 중립
    #    - 일치: 비례 점수
    #    - 어르신 질환 있는데 인력 특기 미일치: 0.3 (기본 케어 가능 점수)
    matched = diseases & specialties
    if not diseases:
        specialty_score = 0.5
    elif matched:
        specialty_score = min(len(matched) / len(diseases), 1.0)
    else:
        specialty_score = 0.3

    # 2. 거리 (좌표가 있을 때만)
    if senior_lat is not None and senior_lng is not None and cg.lat is not None and cg.lng is not None:
        dist_km = _haversine_km(senior_lat, senior_lng, cg.lat, cg.lng)
        distance_score = max(0.0, 1.0 - dist_km / 10.0)
    else:
        dist_km = None
        distance_score = 0.5

    # 3. 평점 (0~1)
    rating_score = max(0.0, min(cg.rating_avg / 5.0, 1.0))

    # 4. 경험 (0~1, 100 sessions 이상은 만점)
    exp_score = min(cg.completed_sessions / 100.0, 1.0)

    final = 0.4 * specialty_score + 0.3 * distance_score + 0.2 * rating_score + 0.1 * exp_score

    reasons = []
    if matched:
        reasons.append(f"특기 일치: {', '.join(sorted(matched))}")
    if dist_km is not None:
        reasons.append(f"거리 {dist_km:.1f}km")
    reasons.append(f"평점 {cg.rating_avg:.2f}/5")
    if cg.completed_sessions >= 50:
        reasons.append(f"경력 {cg.completed_sessions}회")

    return round(final, 3), reasons


@app.post("/ai/match/recommend", dependencies=[Depends(verify_token)])
def match_recommend(req: MatchRecommendRequest) -> dict[str, Any]:
    scored = []
    for cg in req.caregivers:
        score, reasons = _score_caregiver(req.senior, cg)
        if score >= req.min_score:
            scored.append({"caregiver_id": cg.id, "score": score, "reasons": reasons})

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[: req.top_k]
    for i, c in enumerate(top, start=1):
        c["rank"] = i

    return {"candidates": top, "scoring_method": "rule-v1"}


# ───────────────────────── 2. STT ─────────────────────────

class TranscribeRequest(BaseModel):
    audio_url: str
    language: str = "ko"


@app.post("/ai/voice/transcribe", dependencies=[Depends(verify_token)])
def transcribe(req: TranscribeRequest) -> dict[str, Any]:
    return {
        "stt_text": "오늘 어머님 점심 반 그릇 드시고, 산책 30분 하셨고, 혈압 정상이었어요. 기분도 좋아 보이셨어요.",
        "confidence": 0.948,
        "duration_sec": 47,
        "language": req.language,
        "model": "stub",
    }


# ───────────────────────── 3. 일지 요약 ─────────────────────────

class SummarizeRequest(BaseModel):
    stt_text: str
    context: dict[str, Any] = {}
    output_versions: list[str] = ["guardian", "medical"]


@app.post("/ai/voice/summarize", dependencies=[Depends(verify_token)])
def summarize(req: SummarizeRequest) -> dict[str, Any]:
    return {
        "guardian_version": (
            "오늘 어머님이 점심을 평소보다 적게 드셨고(반 그릇), "
            "식사 후 거실 산책 20분 다녀오셨습니다. 혈압은 정상 범위였고 기분도 좋아 보이셨어요."
        ),
        "medical_version": "식사량 50% / 운동 20분 / BP 정상 / 정서 안정. 식이 섭취 저하 관찰됨.",
        "categorized": {
            "meal": {"percentage": 50},
            "exercise": {"minutes": 20, "type": "walking"},
            "vital": {"bp": "normal"},
            "mood": "positive",
        },
        "confidence": 0.93,
        "model": "stub",
    }


# ───────────────────────── 4. 이상징후 (룰 기반) ─────────────────────────
#
# Laravel이 어르신 7일치 데이터를 features로 추출해 보내면, 4개 위험축에 대해
# 룰 임계값으로 점수 산정 후 가장 높은 위험 반환.
#
# 위험축:
#   - nutrition  : meal_pct 평균 < 70% (3일 연속 < 50% 시 가산점)
#   - depression : mood_score 평균 < 60 OR 7일 연속 하락
#   - delirium   : 심박/체온 변동성 + 수면 5h 미만
#   - fall       : BP_sys 변동 폭 > 30, 또는 BP_dia 80↑↓
#
# 향후 careand-ml의 IsolationForest+LSTM 앙상블로 교체 가능.

class AnomalyFeatures(BaseModel):
    senior_id: int
    window_days: int = 7
    meal_pct_avg: float | None = None              # 0~100
    meal_pct_3day_min: float | None = None         # 최근 3일 평균
    sleep_hours_avg: float | None = None
    mood_score_avg: float | None = None            # 0~100
    bp_sys_max: int | None = None
    bp_sys_min: int | None = None
    heart_rate_max: int | None = None
    heart_rate_min: int | None = None
    body_temp_max: float | None = None


@app.post("/ai/anomaly/score", dependencies=[Depends(verify_token)])
def anomaly_score(req: AnomalyFeatures) -> dict[str, Any]:
    risks: list[dict[str, Any]] = []

    # 1. 영양
    triggers, score = [], 0.0
    if req.meal_pct_avg is not None and req.meal_pct_avg < 70:
        score = max(score, (70 - req.meal_pct_avg) * 1.5)  # 70→0 ~ 30→60
        triggers.append(f"평균 식사량 {req.meal_pct_avg:.0f}%")
    if req.meal_pct_3day_min is not None and req.meal_pct_3day_min < 50:
        score = max(score, 50 + (50 - req.meal_pct_3day_min))
        triggers.append(f"최근 3일 평균 {req.meal_pct_3day_min:.0f}%")
    if score > 0:
        risks.append({"type": "nutrition", "score": min(score, 100), "triggers": triggers})

    # 2. 우울
    triggers, score = [], 0.0
    if req.mood_score_avg is not None:
        if req.mood_score_avg < 60:
            score = max(score, (60 - req.mood_score_avg) * 1.5)
            triggers.append(f"평균 정서점수 {req.mood_score_avg:.0f}/100")
    if score > 0:
        risks.append({"type": "depression", "score": min(score, 100), "triggers": triggers})

    # 3. 섬망 (수면 + 활력 변동)
    triggers, score = [], 0.0
    if req.sleep_hours_avg is not None and req.sleep_hours_avg < 5:
        score += (5 - req.sleep_hours_avg) * 15
        triggers.append(f"평균 수면 {req.sleep_hours_avg:.1f}h")
    if req.heart_rate_max is not None and req.heart_rate_min is not None:
        hr_var = req.heart_rate_max - req.heart_rate_min
        if hr_var > 40:
            score += (hr_var - 40) * 1.0
            triggers.append(f"심박 변동폭 {hr_var}bpm")
    if req.body_temp_max is not None and req.body_temp_max >= 38.0:
        score += 30
        triggers.append(f"체온 {req.body_temp_max}°C")
    if score > 0:
        risks.append({"type": "delirium", "score": min(score, 100), "triggers": triggers})

    # 4. 낙상 위험 (혈압 변동)
    triggers, score = [], 0.0
    if req.bp_sys_max is not None and req.bp_sys_min is not None:
        bp_var = req.bp_sys_max - req.bp_sys_min
        if bp_var > 30:
            score = (bp_var - 30) * 2.0
            triggers.append(f"수축기 혈압 변동폭 {bp_var}mmHg")
        if req.bp_sys_max > 160:
            score += 20
            triggers.append(f"수축기 최고 {req.bp_sys_max}mmHg")
    if score > 0:
        risks.append({"type": "fall", "score": min(score, 100), "triggers": triggers})

    # 가장 높은 위험 선택
    if not risks:
        return {
            "senior_id": req.senior_id,
            "risk_score": 0.0,
            "risk_type": "none",
            "severity": "low",
            "trigger_pattern": [],
            "recommendation": [],
            "model": "rule-v1",
        }

    top = max(risks, key=lambda r: r["score"])
    score = top["score"]
    severity = (
        "critical" if score >= 80
        else "high" if score >= 60
        else "mid" if score >= 40
        else "low"
    )

    recommend_map = {
        "nutrition": ["수분/영양 보충식 권장", "의료진 영양 상담"],
        "depression": ["가족/지인 방문 권장", "정서 활동 제공", "정신과 상담 검토"],
        "delirium": ["수면 환경 점검", "의료진 즉시 상담", "탈수/약물 부작용 점검"],
        "fall": ["혈압 약 복용 확인", "거주환경 안전 점검", "보행 보조 검토"],
    }

    return {
        "senior_id": req.senior_id,
        "risk_score": round(score, 1),
        "risk_type": top["type"],
        "severity": severity,
        "trigger_pattern": top["triggers"],
        "recommendation": recommend_map.get(top["type"], []),
        "all_risks": risks,
        "model": "rule-v1",
    }


# ───────────────────────── 5. 챗봇 RAG ─────────────────────────

class ChatbotRequest(BaseModel):
    question: str
    context: dict[str, Any] = {}


@app.post("/ai/chatbot/answer", dependencies=[Depends(verify_token)])
def chatbot_answer(req: ChatbotRequest) -> dict[str, Any]:
    return {
        "answer": (
            "장기요양 4등급 재가급여 기준 월 한도액은 1,455,800원이며, "
            "일반 소득 기준 본인부담률은 15%로 약 218,370원입니다."
        ),
        "sources": [
            {
                "title": "장기요양보험 안내",
                "url": "https://www.longtermcare.or.kr",
                "snippet": "재가급여 한도 및 본인부담률 안내",
            }
        ],
        "model": "stub",
    }


# ───────────────────────── 6. 수요 예측 ─────────────────────────

class ForecastRequest(BaseModel):
    region: str = "서울"
    days: int = 7


@app.post("/ai/forecast/demand", dependencies=[Depends(verify_token)])
def forecast_demand(req: ForecastRequest) -> dict[str, Any]:
    forecasts = []
    for i in range(req.days):
        forecasts.append(
            {
                "date": f"2026-05-{4 + i:02d}",
                "predicted_requests": 187 - i * 5,
                "available_caregivers": 42 + i * 2,
            }
        )
    return {"region": req.region, "forecasts": forecasts, "model": "stub"}


# ───────────── 7. 백엔드 호환 엔드포인트 (산후조리 도메인) ─────────────
# careand-backend(Laravel) PostpartumChatbotController / PostpartumMatchingController가
# 호출하는 계약에 맞춘 어댑터. 내부 구현은 stub 단계.

class PostpartumChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class PostpartumChatRequest(BaseModel):
    session_id: int | None = None
    user_message: str = ""
    history: list[PostpartumChatMessage] = []
    context: dict[str, Any] = {}


@app.post("/chatbot/postpartum", dependencies=[Depends(verify_token)])
def chatbot_postpartum(req: PostpartumChatRequest) -> dict[str, Any]:
    """산후조리 챗봇 RAG (stub). 응답 형식 {answer, sources}는 백엔드 기대와 일치."""
    ctx = req.context or {}
    days = ctx.get("days_since_delivery", 0)
    first = ctx.get("is_first_baby", True)
    bf = ctx.get("breastfeeding")

    tips = []
    if isinstance(days, int) and days <= 14:
        tips.append("출산 후 2주 이내에는 충분한 휴식과 수분 섭취가 가장 중요합니다.")
    if first:
        tips.append("초산모이신 만큼 모자동실·수유 자세는 산후조리원 간호 인력에게 적극적으로 문의하세요.")
    if bf:
        tips.append("모유수유 중에는 하루 2~3L의 수분과 균형 잡힌 식사를 권장합니다.")
    if not tips:
        tips.append("산모님의 회복 상태에 맞춘 영양·수면 관리가 필요합니다.")

    answer = (
        f"문의 주신 \"{req.user_message}\"에 대해 안내드립니다. "
        + " ".join(tips)
        + " 증상이 지속되면 담당 의료진과 상담하시기 바랍니다."
    )

    return {
        "answer": answer,
        "sources": [
            {
                "title": "산후조리 표준 가이드",
                "url": "https://www.mohw.go.kr",
                "snippet": "산후 회복 및 신생아 돌봄 표준 안내",
            }
        ],
        "model": "stub",
    }


class PostpartumMatchRequest(BaseModel):
    match_request_id: int | None = None
    postpartum_client_id: int | None = None
    delivery_type: str | None = None
    is_first_baby: bool | None = None
    is_multiple_birth: bool | None = None
    breastfeeding_intent: Any | None = None
    voucher_grade: Any | None = None
    region_code: Any | None = None
    branch_id: Any | None = None
    special_conditions: Any | None = None


def _as_list(value: Any) -> list:
    """JSON 문자열/리스트/None을 list로 정규화."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except (ValueError, TypeError):
            return [value] if value.strip() else []
    return [value]


def _score_postpartum_caregiver(cg: dict, req: "PostpartumMatchRequest") -> tuple[float, list[str]]:
    """규칙기반 점수화: 특기(0.4) + 지점(0.3) + 평점(0.2) + 경력(0.1)."""
    reasons: list[str] = []

    cg_spec = set(s.strip() for s in _as_list(cg.get("specialties")) if str(s).strip())
    client_cond = set(s.strip() for s in _as_list(req.special_conditions) if str(s).strip())
    if client_cond:
        matched = cg_spec & client_cond
        specialty_score = len(matched) / len(client_cond)
        if matched:
            reasons.append(f"특기 적합: {', '.join(sorted(matched))}")
    else:
        specialty_score = 0.5  # 산모 특이조건 없음 → 중립

    cg_branch = cg.get("branch_id")
    if req.branch_id is not None and cg_branch is not None:
        branch_score = 1.0 if cg_branch == req.branch_id else 0.3
        if cg_branch == req.branch_id:
            reasons.append("동일 지점")
    else:
        branch_score = 0.5

    rating = float(cg.get("rating_avg") or 0)
    rating_score = max(0.0, min(rating / 5.0, 1.0))
    reasons.append(f"평점 {rating:.2f}")

    sessions = int(cg.get("completed_sessions") or 0)
    exp_score = min(sessions / 100.0, 1.0)
    if sessions >= 50:
        reasons.append(f"경력 {sessions}회")

    final = 0.4 * specialty_score + 0.3 * branch_score + 0.2 * rating_score + 0.1 * exp_score
    return round(min(final, 9.999), 3), reasons


@app.post("/matching/postpartum", dependencies=[Depends(verify_token)])
def matching_postpartum(req: PostpartumMatchRequest) -> dict[str, Any]:
    """산후조리 매칭: DB에서 적격 케어기버를 조회·점수화해 match_candidates에 INSERT.

    - 적격: service_domains에 'postpartum' 포함 + status='active' + 미삭제
    - 멱등성: 동일 request의 기존 후보 삭제 후 재생성
    - 후보 INSERT 성공 시 match_requests.status='matched' 갱신
    """
    if req.match_request_id is None:
        raise HTTPException(status_code=422, detail="match_request_id required")

    top_k = int(os.environ.get("MATCH_TOP_K", "5"))
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, specialties, rating_avg, completed_sessions, branch_id
                FROM caregivers
                WHERE FIND_IN_SET('postpartum', service_domains)
                  AND status = 'active' AND deleted_at IS NULL
                """
            )
            caregivers = cur.fetchall()

            scored = []
            for cg in caregivers:
                score, reasons = _score_postpartum_caregiver(cg, req)
                scored.append({"caregiver_id": cg["id"], "score": score, "reasons": reasons})
            scored.sort(key=lambda x: x["score"], reverse=True)
            top = scored[:top_k]

            # 멱등성: 기존 후보 제거 후 재생성 (해당 request만)
            cur.execute("DELETE FROM match_candidates WHERE request_id = %s", (req.match_request_id,))

            inserted = 0
            for rank, c in enumerate(top, start=1):
                cur.execute(
                    """
                    INSERT INTO match_candidates
                        (request_id, caregiver_id, ai_score, ai_reasons, `rank`, response, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'pending', NOW(), NOW())
                    """,
                    (
                        req.match_request_id,
                        c["caregiver_id"],
                        c["score"],
                        json.dumps(c["reasons"], ensure_ascii=False),
                        rank,
                    ),
                )
                inserted += 1

            if inserted > 0:
                cur.execute(
                    "UPDATE match_requests SET status = 'matched', matched_at = NOW() WHERE id = %s",
                    (req.match_request_id,),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "status": "accepted",
        "match_request_id": req.match_request_id,
        "eligible_caregivers": len(caregivers),
        "candidates_inserted": inserted,
        "model": "rule-v1",
    }


# ───────────── 8. C:Writer 케어 일지 생성 (#28 LLM 일지) ─────────────

class CareActivity(BaseModel):
    category: str = "other"
    memo: str | None = None


class CareLogRequest(BaseModel):
    session_id: int | None = None
    service_domain: str | None = None
    senior_name: str = "어르신"
    duration_min: int = 0
    activities: list[CareActivity] = []


_CATEGORY_LABEL = {
    "meal": "식사", "medication": "복약", "exercise": "활동", "bath": "목욕",
    "mood": "정서", "cognition": "인지", "other": "기타",
}


@app.post("/care-log/generate", dependencies=[Depends(verify_token)])
def care_log_generate(req: CareLogRequest) -> dict[str, Any]:
    """STT/활동 기록 → 도메인별 보호자 톤 일지 자동 생성 (stub LLM).

    실제 운영에서는 Whisper STT 텍스트 + Claude API로 대체.
    """
    # 활동을 카테고리별로 그룹화
    grouped: dict[str, list[str]] = {}
    for a in req.activities:
        label = _CATEGORY_LABEL.get(a.category, a.category)
        grouped.setdefault(label, [])
        if a.memo:
            grouped[label].append(a.memo)

    if grouped:
        lines = [f"· {cat}: {', '.join(memos) if memos else '정상 수행'}" for cat, memos in grouped.items()]
        body = "\n".join(lines)
    else:
        body = "· 케어 전반: 특이사항 없이 안정적으로 진행되었습니다."

    hours = round(req.duration_min / 60, 1) if req.duration_min else 0

    guardian_version = (
        f"{req.senior_name}께서 오늘 {hours}시간 동안 안정적으로 케어받으셨습니다.\n"
        f"{body}\n"
        "전반적으로 편안한 상태를 유지하셨으며, 특별한 이상 징후는 없었습니다. "
        "궁금하신 점은 언제든 문의해 주세요."
    )
    medical_version = (
        f"[케어 세션 요약] 대상: {req.senior_name} / 소요: {req.duration_min}분 / "
        f"도메인: {req.service_domain or '-'}\n{body}\n"
        "활력징후 안정. 추가 모니터링 권고 사항 없음."
    )

    return {
        "guardian_version": guardian_version,
        "medical_version": medical_version,
        "categorized": grouped,
        "confidence": 0.9,
        "model": "stub-claude",
    }
