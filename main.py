"""
Care& AI 마이크로서비스 (FastAPI)

Laravel 백엔드의 AiService.php가 호출하는 엔드포인트 제공.

구현 상태 (2026-06-10 실구현 전환):
  POST /ai/match/recommend   - 매칭 추천         [rule-v1 실구현]
  POST /ai/voice/transcribe  - STT               [stub — STT 엔진 미도입]
  POST /ai/voice/summarize   - 일지 요약          [LLM(Claude) + 휴리스틱 폴백]
  POST /ai/anomaly/score     - 이상징후 스코어    [rule-v1 실구현]
  POST /ai/chatbot/answer    - 챗봇              [LLM(Claude) + KB 폴백]
  POST /ai/forecast/demand   - 수요 예측          [seasonal-naive-v1 실구현 (DB 이력)]
  POST /chatbot/postpartum   - 산후 챗봇          [LLM(Claude) + 룰 폴백]
  POST /matching/postpartum  - 산후 매칭          [rule-v1 실구현 (DB 직결)]
  POST /care-log/generate    - 케어 일지 생성     [LLM(Claude) + 템플릿 폴백]

LLM: .env의 ANTHROPIC_API_KEY가 있으면 Claude API 실호출, 없거나 호출 실패 시
     각 엔드포인트의 룰/템플릿 폴백으로 응답 (응답 model 필드로 구분 가능).

인증: Authorization: Bearer <AI_SERVICE_TOKEN> (env로 검증)
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import l2r  # 매칭 feature·룰점수·L2R 모델 게이팅의 단일 소스
import ontology  # 온톨로지 SPARQL — 매칭 feature 보강 + STT hotwords 어휘집

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

EXPECTED_TOKEN = os.environ.get("AI_SERVICE_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# LLM 프로바이더 선택: "anthropic"(Claude) | "gemini"(Google). 기본 anthropic.
# 이 박스는 egress 제한이 있으나 Google HTTPS(generativelanguage.googleapis.com)는 도달 가능 —
# Anthropic 키가 없을 때 Gemini로 대체 운용 가능.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
# gemini-2.5-* 는 기본 'thinking'이 켜져 있어 출력 토큰(max_tokens)을 잠식 → 짧은 한도에서
# 본문이 잘려 빈 응답이 되는 문제. 구조화 응답엔 thinking 불필요하므로 0(끔)이 기본.
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
# STT 엔진: "whisper"(로컬 faster-whisper, 기본) | "gemini"(멀티모달, 음성을 Google로 전송).
# 케어 음성은 민감 PII이므로 운영 기본은 로컬 whisper 권장.
STT_PROVIDER = os.environ.get("STT_PROVIDER", "whisper").strip().lower()

logger = logging.getLogger("careand-ai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


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


# ───────────────────────── LLM 헬퍼 (Claude / Gemini 선택형) ─────────────────────────
def active_model() -> str:
    """현재 프로바이더의 모델명(응답 model 라벨용)."""
    return GEMINI_MODEL if LLM_PROVIDER == "gemini" else ANTHROPIC_MODEL


def llm_available() -> bool:
    return bool(GEMINI_API_KEY) if LLM_PROVIDER == "gemini" else bool(ANTHROPIC_API_KEY)


def _llm_anthropic(system: str, user_msg: str, max_tokens: int, temperature: float) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip() or None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        logger.warning("LLM(anthropic) HTTP %s: %s", e.code, detail[:200])
        return None
    except Exception as e:
        logger.warning("LLM(anthropic) 호출 실패: %s", e)
        return None


def _llm_gemini(system: str, user_msg: str, max_tokens: int, temperature: float) -> str | None:
    if not GEMINI_API_KEY:
        return None
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
            # 2.5-flash thinking 끔(출력 토큰 보존). -1이면 동적, 양수면 해당 예산.
            "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
        },
    }).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    # 무료 등급은 일시 과부하/한도(429/500/503)가 잦음 → 짧은 백오프로 최대 3회 시도.
    last_err = ""
    for attempt in range(3):
        req = urllib.request.Request(url, data=payload,
                                     headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cands = data.get("candidates", [])
            if not cands:
                return None
            parts = cands[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip() or None
        except urllib.error.HTTPError as e:
            try:
                last_err = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                last_err = ""
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(1.2 * (attempt + 1))  # 1.2s, 2.4s 백오프
                continue
            logger.warning("LLM(gemini) HTTP %s: %s", e.code, last_err[:200])
            return None
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            logger.warning("LLM(gemini) 호출 실패: %s", last_err)
            return None
    return None


def llm_complete(system: str, user_msg: str, max_tokens: int = 1024, temperature: float = 0.3) -> str | None:
    """활성 프로바이더로 LLM 호출. 키 없음/실패 시 None (호출부가 폴백 처리)."""
    if LLM_PROVIDER == "gemini":
        return _llm_gemini(system, user_msg, max_tokens, temperature)
    return _llm_anthropic(system, user_msg, max_tokens, temperature)


def llm_complete_json(system: str, user_msg: str, max_tokens: int = 1024) -> dict | None:
    """JSON 응답 강제 호출. 파싱 실패 시 None."""
    text = llm_complete(system + "\n\n반드시 유효한 JSON 객체 하나만 출력하라. 마크다운 코드펜스·설명 금지.",
                        user_msg, max_tokens=max_tokens, temperature=0.2)
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        logger.warning("LLM JSON 파싱 실패: %.120s", raw)
        return None


app = FastAPI(
    title="Care& AI Service",
    version="0.2.0",
    description="Care& AI 마이크로서비스 — 실구현 전환(룰/시계열 + LLM 폴백 구조)",
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
    llm_state = f"ready({active_model()} · {LLM_PROVIDER})" if llm_available() else f"fallback ({LLM_PROVIDER} 키 미설정)"
    return {
        "status": "ok",
        "service": "careand-ai-service",
        "phase": "v0.2 실구현",
        "models": {
            "matching": l2r.method_tag(),
            "stt": f"faster-whisper-{WHISPER_MODEL_LABEL}-int8 (lazy)",
            "llm": llm_state,
            "anomaly": "rule-v1",
            "forecast": "seasonal-naive-v1",
            "rag": llm_state,
        },
    }


@app.get("/ai/match/l2r-status", dependencies=[Depends(verify_token)])
def l2r_status() -> dict[str, Any]:
    """L2R 게이트/모델 상태 점검(운영용). active=false면 rule-v3로 동작 중."""
    return l2r.status()


# ───────────────────────── 1. 매칭 추천 (룰 기반 실구현, rule-v3) ─────────────────────────
#
# base = w_특기*특기일치 + w_거리*거리점수 + w_평점*평점점수(베이지안) + w_경험*경험점수
# final = 0.85 * base + 0.15 * 연속성점수   (연속성=동일 대상자 재돌봄 이력, 가산식이라 무이력 시 랭킹 보존)
#  - 특기일치: 어르신 질환↔인력 특기 교집합 비율 (0~1)
#  - 거리점수: max(0, 1 - dist_km/10) (10km 안에서만 양의 점수)
#  - 평점점수: rating_avg / 5.0
#  - 경험점수: min(completed_sessions / 100, 1.0)


class CaregiverFeature(BaseModel):
    id: int
    specialties: list[str] = []
    rating_avg: float = 0.0
    rating_count: int = 0                # 평점 표본 수 (베이지안 보정용)
    completed_sessions: int = 0
    lat: float | None = None
    lng: float | None = None
    prior_matches: int = 0              # 이 대상자를 과거에 맡았던 횟수 (연속성 신호)
    gender: str | None = None          # M|F (성별선호 매칭용)


class MatchRecommendRequest(BaseModel):
    request_id: int
    senior: dict[str, Any]
    caregivers: list[CaregiverFeature]
    top_k: int = 5
    min_score: float = 0.0
    service_domain: str = "senior"      # senior|living_support|nursing|postpartum|childcare|mental_care
    required_skills: list[str] = []     # 미보유 인력은 후보에서 하드 제외
    preferred_gender: str | None = None # M|F — 지정 시 일치 인력에 소프트 가산


# 서브점수/거리/가중치/베이지안/연속성 상수는 모두 l2r.py(단일 소스)로 이전됨.
# rule-v3 = l2r.rule_score, L2R 블렌딩(데이터 임계 미달 시 룰 폴백) = l2r.blended_score.
# 성별선호는 점수가 아니라 정렬 파티션(match_recommend)으로 처리한다 — 점수 미세가산으로는
# 소수 성별(예: 남성 2명)을 상위로 못 올려 선호가 무력화되기 때문.


def _score_caregiver(senior: dict[str, Any], cg: CaregiverFeature, domain: str = "senior", preferred_gender: str | None = None) -> tuple[float, list[str]]:
    """rule-v3 서브점수(l2r 단일 소스) → L2R 블렌딩 점수 + 사람용 reasons.
    모델 비활성(데이터 임계 미달)이면 blended_score가 순수 rule-v3로 자동 폴백한다."""
    sub = l2r.subscores(senior, cg, domain, preferred_gender)
    score = l2r.blended_score(sub, domain)

    reasons: list[str] = []
    if sub["_matched"]:
        reasons.append(f"특기 일치: {', '.join(sorted(sub['_matched']))}")
    if sub["_onto_matched"]:
        reasons.append(f"온톨로지 근접 특기: {', '.join(sorted(sub['_onto_matched']))}")
    if sub["_prior"] > 0:
        reasons.append(f"단골 — 이전 돌봄 {sub['_prior']}회")
    if sub["gender_match"]:
        reasons.append("선호 성별 일치")
    if sub["_dist_km"] is not None:
        reasons.append(f"거리 {sub['_dist_km']:.1f}km")
    if sub["_n"] > 0:
        reasons.append(f"평점 {cg.rating_avg:.2f}/5 ({sub['_n']}건)")
    if sub["_sessions"] >= 50:
        reasons.append(f"경력 {sub['_sessions']}회")

    return score, reasons


# 순위 판정은 rule-v3/L2R(결정적)가 담당하고, 1순위 추천 사유를 보호자용 문장으로
# 풀어주는 설명만 LLM이 생성(하이브리드). 호출 비용/한도 고려해 1순위에만 붙인다.
_MATCH_SYSTEM = (
    "너는 시니어 돌봄 플랫폼의 매칭 안내 도우미다. 시스템이 순위화한 1순위 돌봄전문가의 추천 "
    "근거(태그 목록)를 받아, 보호자가 '왜 이 분인지' 이해할 따뜻한 존댓말 1~2문장으로 풀어 쓴다. "
    "주어진 근거 안에서만 말하고 새로운 사실·이름·수치를 지어내지 마라. 설명 문장만 출력."
)


def _match_reco_note(reasons: list[str], domain: str | None) -> str | None:
    """1순위 후보의 추천 근거(태그)를 보호자용 자연어로 변환(미가용/실패/근거없음 시 None)."""
    if not llm_available() or not reasons:
        return None
    dom = {"senior": "시니어 돌봄", "nursing": "병원 간병", "living_support": "생활지원 서비스",
           "housekeeping": "생활지원 서비스", "postpartum": "산후조리",
           "childcare": "아이돌봄", "mental_care": "마음돌봄"}.get(domain or "", "돌봄")
    user = f"서비스: {dom}\n1순위 추천 근거: {', '.join(reasons)}"
    return llm_complete(_MATCH_SYSTEM, user, max_tokens=300, temperature=0.4)


@app.post("/ai/match/recommend", dependencies=[Depends(verify_token)])
def match_recommend(req: MatchRecommendRequest) -> dict[str, Any]:
    required = set(req.required_skills or [])
    pref = req.preferred_gender
    scored = []
    for cg in req.caregivers:
        # 필수 스킬 하드 필터 — 수리 요청에 청소 인력 차단 (백엔드 풀 필터의 이중 방어)
        if required and not required <= set(cg.specialties or []):
            continue
        score, reasons = _score_caregiver(req.senior, cg, req.service_domain, pref)
        gender_pref = 1 if (pref and cg.gender == pref) else 0
        # 선호 성별 인력은 보호자가 명시적으로 원한 대상이므로 점수 임계(min_score)를 우회해 항상 포함.
        # (그렇지 않으면 base 낮은 소수 성별이 임계에서 탈락해 선호가 무력화됨)
        if score >= req.min_score or gender_pref:
            scored.append({"caregiver_id": cg.id, "score": score, "reasons": reasons, "_gp": gender_pref})

    # 선호 성별 지정 시: 일치 인력을 우선(파티션) → 그 안에서 점수순. 미지정 시: 순수 점수순.
    # 일치 인력이 top_k보다 적으면 미일치 인력이 자연스레 폴백으로 채워진다(빈 결과 방지).
    if pref:
        scored.sort(key=lambda x: (x["_gp"], x["score"]), reverse=True)
    else:
        scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[: req.top_k]
    for i, c in enumerate(top, start=1):
        c["rank"] = i
        c.pop("_gp", None)

    result = {"candidates": top, "scoring_method": l2r.method_tag()}
    # 하이브리드: 1순위 추천 사유를 보호자용 자연어로 best-effort 추가(실패 시 생략)
    if top:
        note = _match_reco_note(top[0].get("reasons", []), req.service_domain)
        if note:
            top[0]["recommendation_note"] = note
            result["recommendation_model"] = active_model()
    return result


# ───────────── 가성비 재랭킹 (역경매 입찰 반영) ─────────────
#
# 후보 생성 시점엔 입찰가가 없으므로, 보호자 조회 시점에 입찰가 대비 가성비를
# AI 매칭 점수에 소프트 가산해 "추천순"을 보정한다. 적합도(ai_score)가 비슷한
# 후보 간 가격 경쟁력으로 타이브레이크하되, 적합도를 뒤집을 만큼 크지는 않게(W_PRICE 작게).

_W_PRICE = float(os.environ.get("AI_W_PRICE", "0.10"))   # 가성비 가중(작게 — 소프트 보정)
_VFM_CHEAP_CAP = 0.15    # 권장가 대비 저렴 보너스 상한
_VFM_PRICEY_CAP = -0.10  # 권장가 대비 비쌈 페널티 하한


class ValueRankItem(BaseModel):
    candidate_id: int
    ai_score: float
    bid_hourly: float | None = None


class ValueRankRequest(BaseModel):
    suggested: float                 # 적정가(권장 시급)
    items: list[ValueRankItem]


def _value_for_money(ai_score: float, bid: float | None, suggested: float) -> tuple[float, str | None]:
    """입찰가 대비 가성비를 ai_score에 소프트 가산. (value_score, reason) 반환."""
    if bid is None or suggested <= 0:
        return ai_score, None
    # 권장가보다 저렴할수록 +, 비쌀수록 - (비대칭 클램프)
    vfm = max(_VFM_PRICEY_CAP, min((suggested - bid) / suggested, _VFM_CHEAP_CAP))
    value_score = ai_score + _W_PRICE * vfm
    reason = None
    if vfm >= 0.05:
        reason = "가성비 좋음"
    elif vfm <= -0.05:
        reason = "권장가 대비 높음"
    return value_score, reason


@app.post("/matching/value-rank", dependencies=[Depends(verify_token)])
def value_rank(req: ValueRankRequest) -> dict[str, Any]:
    ranked = []
    for it in req.items:
        vs, reason = _value_for_money(it.ai_score, it.bid_hourly, req.suggested)
        ranked.append({"candidate_id": it.candidate_id, "value_score": round(vs, 4), "reason": reason})
    ranked.sort(key=lambda x: x["value_score"], reverse=True)
    return {"ranked": ranked, "scoring_method": "value-v1", "w_price": _W_PRICE}


# ───────────────────────── 2. STT (faster-whisper 실구현) ─────────────────────────
#
# CPU(int8) 추론. 박스 RAM이 빠듯하므로(가용 ~1.7G) 기본 모델은 base.
# WHISPER_MODEL env로 small 등 상향 가능. 모델은 첫 호출 시 lazy 로드(이후 상주),
# 추론은 락으로 직렬화(2코어 박스에서 동시 추론 방지).

WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
# 라벨용 짧은 이름 (경로 지정 시 디렉토리명만)
WHISPER_MODEL_LABEL = os.path.basename(WHISPER_MODEL_NAME.rstrip("/")) or WHISPER_MODEL_NAME
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "2"))
_AUDIO_MAX_BYTES = 50 * 1024 * 1024

_whisper_model = None
_whisper_load_lock = threading.Lock()
_whisper_infer_lock = threading.Lock()


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_load_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                logger.info("Whisper 모델 로드 시작: %s (int8, threads=%d)", WHISPER_MODEL_NAME, WHISPER_THREADS)
                try:
                    # local_files_only: 서비스는 절대 네트워크 다운로드를 하지 않는다
                    # (모델 준비는 fetch_model.py 전담 — 부재 시 즉시 503, 행 방지)
                    _whisper_model = WhisperModel(
                        WHISPER_MODEL_NAME,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=WHISPER_THREADS,
                        download_root=os.path.join(os.path.dirname(__file__), "models"),
                        local_files_only=True,
                    )
                except Exception as e:
                    logger.warning("Whisper 모델 미준비: %s", e)
                    raise HTTPException(
                        status_code=503,
                        detail=f"STT model not ready (fetch_model.py로 다운로드 필요): {WHISPER_MODEL_NAME}",
                    )
                logger.info("Whisper 모델 로드 완료")
    return _whisper_model


def _fetch_audio(audio_url: str) -> tuple[str, bool]:
    """audio_url → 로컬 파일 경로. (경로, 임시파일 여부) 반환."""
    if audio_url.startswith(("http://", "https://")):
        suffix = os.path.splitext(audio_url.split("?")[0])[1] or ".audio"
        req = urllib.request.Request(audio_url, headers={"User-Agent": "careand-ai/0.2"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(_AUDIO_MAX_BYTES + 1)
        if len(data) > _AUDIO_MAX_BYTES:
            raise HTTPException(status_code=413, detail="audio file too large (>50MB)")
        if not data:
            raise HTTPException(status_code=422, detail="empty audio file")
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(data)
        tmp.close()
        return tmp.name, True
    if os.path.isfile(audio_url):
        return audio_url, False
    raise HTTPException(status_code=422, detail=f"audio_url not reachable: {audio_url[:120]}")


class TranscribeRequest(BaseModel):
    audio_url: str
    language: str = "ko"
    diseases: list[str] = []  # 대상자 질병/특이사항(옵션) — 온톨로지 associatedTerm으로 STT 어휘 개인화


_STT_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mp3", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
    ".webm": "audio/webm",
}


def _stt_gemini(path: str, language: str) -> str | None:
    """Gemini 멀티모달 전사(인라인 오디오). 케어 음성을 Google로 전송하므로 PII 주의.
    인라인 한도(~19MB) 초과 시 413(대용량은 whisper 권장)."""
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY 미설정")
    if os.path.getsize(path) > 19 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Gemini 인라인 STT 한도(~19MB) 초과 — whisper 사용 권장")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    mime = _STT_MIME.get(os.path.splitext(path)[1].lower(), "audio/wav")
    body = json.dumps({
        "contents": [{"parts": [
            {"text": f"이 {language} 음성을 글자 그대로 정확히 전사(transcribe)해줘. 다른 말 없이 전사 텍스트만 출력."},
            {"inlineData": {"mimeType": mime, "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0.0, "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET}},
    }).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    for attempt in range(3):
        try:
            r = urllib.request.Request(url, data=body, headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(r, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cands = data.get("candidates", [])
            if not cands:
                return None
            return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])).strip() or None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            raise HTTPException(status_code=502, detail=f"Gemini STT HTTP {e.code}: {detail[:160]}")
        except Exception as e:
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise HTTPException(status_code=502, detail=f"Gemini STT 실패: {e}")
    return None


@app.post("/ai/voice/transcribe", dependencies=[Depends(verify_token)])
def transcribe(req: TranscribeRequest) -> dict[str, Any]:
    """음성 → 텍스트 (faster-whisper, CPU int8).

    실패 시 가짜 텍스트를 반환하지 않고 5xx로 응답한다
    (오인식 일지가 보호자에게 나가는 것 방지 — 호출측이 실패 처리).
    """
    path, is_tmp = _fetch_audio(req.audio_url)
    try:
        if STT_PROVIDER == "gemini":
            text = _stt_gemini(path, req.language or "ko")
            if not text:
                raise HTTPException(status_code=502, detail="Gemini STT 빈 결과")
            return {
                "stt_text": text,
                "confidence": 0.9,
                "duration_sec": None,
                "language": req.language or "ko",
                "model": f"{GEMINI_MODEL} (gemini-stt)",
            }
        model = _get_whisper()
        # 케어 도메인 어휘(욕창/섬망/연하곤란 등)를 hotwords로 넘겨 인식률 보강.
        # 전역 어휘 ∪ 대상자 질병 연관 용어(있으면) — 좁히지 않고 합집합만 쓴다(진단명에
        # 없는 증상도 여전히 인식돼야 하므로). 온톨로지 미가용 시 빈 어휘 → hotwords=None 폴백.
        vocab = ontology.care_term_vocabulary() | ontology.associated_term_labels(frozenset(req.diseases))
        hotwords = ", ".join(sorted(vocab)) or None
        with _whisper_infer_lock:
            segments, info = model.transcribe(
                path,
                language=req.language or "ko",
                vad_filter=True,
                hotwords=hotwords,
            )
            seg_list = list(segments)  # generator 소진 (락 안에서)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("STT 실패: %s", e)
        raise HTTPException(status_code=502, detail=f"STT engine error: {e}")
    finally:
        if is_tmp:
            try:
                os.unlink(path)
            except OSError:
                pass

    text = " ".join(s.text.strip() for s in seg_list).strip()
    if seg_list:
        avg_lp = sum(s.avg_logprob for s in seg_list) / len(seg_list)
        confidence = round(max(0.0, min(math.exp(avg_lp), 1.0)), 3)
    else:
        confidence = 0.0

    return {
        "stt_text": text,
        "confidence": confidence,
        "duration_sec": round(info.duration, 1),
        "language": info.language,
        "model": f"faster-whisper-{WHISPER_MODEL_LABEL}-int8",
    }


# ───────────────────────── 3. 일지 요약 (LLM + 휴리스틱 폴백) ─────────────────────────

class SummarizeRequest(BaseModel):
    stt_text: str
    context: dict[str, Any] = {}
    output_versions: list[str] = ["guardian", "medical"]


_SUMMARIZE_SYSTEM = (
    "너는 시니어 돌봄 플랫폼의 케어 일지 요약 도우미다. 요양보호사의 음성 기록(STT)을 받아 "
    "보호자용(따뜻하고 쉬운 존댓말 2~3문장)과 의료진용(간결한 임상 메모)으로 요약한다. "
    "출력 JSON 스키마: {\"guardian_version\": str, \"medical_version\": str, "
    "\"categorized\": {\"meal\"?: {\"percentage\": int}, \"exercise\"?: {\"minutes\": int, \"type\": str}, "
    "\"vital\"?: object, \"mood\"?: str}}. 기록에 없는 사실을 지어내지 마라. "
    "어르신 호칭은 컨텍스트의 이름(성+이름)을 줄이거나 성을 떼지 말고 전체를 그대로 쓴다."
)


def _summarize_heuristic(stt_text: str) -> dict[str, Any]:
    """LLM 불가 시 키워드 기반 최소 분류 폴백."""
    t = stt_text
    categorized: dict[str, Any] = {}
    if any(k in t for k in ("식사", "드시", "그릇", "점심", "아침", "저녁")):
        pct = 50 if ("반 그릇" in t or "절반" in t) else 100 if ("다 드" in t or "완식" in t) else None
        categorized["meal"] = {"percentage": pct} if pct else {"noted": True}
    if any(k in t for k in ("산책", "운동", "걷")):
        categorized["exercise"] = {"type": "walking"}
    if any(k in t for k in ("혈압", "맥박", "체온", "혈당")):
        categorized["vital"] = {"noted": True}
    if any(k in t for k in ("기분", "웃", "좋아 보")):
        categorized["mood"] = "positive"
    elif any(k in t for k in ("우울", "힘들어", "불안")):
        categorized["mood"] = "negative"

    head = t[:120] + ("…" if len(t) > 120 else "")
    return {
        "guardian_version": f"오늘 돌봄 기록 요약입니다: {head}",
        "medical_version": f"[STT 원문 발췌] {head}",
        "categorized": categorized,
        "confidence": 0.5,
        "model": "heuristic-fallback",
    }


@app.post("/ai/voice/summarize", dependencies=[Depends(verify_token)])
def summarize(req: SummarizeRequest) -> dict[str, Any]:
    ctx = json.dumps(req.context, ensure_ascii=False) if req.context else "없음"
    result = llm_complete_json(
        _SUMMARIZE_SYSTEM,
        f"어르신 컨텍스트: {ctx}\n\nSTT 기록:\n{req.stt_text}",
        max_tokens=800,
    )
    if result and result.get("guardian_version"):
        result.setdefault("categorized", {})
        result["confidence"] = 0.9
        result["model"] = active_model()
        return result
    return _summarize_heuristic(req.stt_text)


# ───────────────────────── 4. 이상징후 (룰 기반 실구현) ─────────────────────────
#
# Laravel이 어르신 7일치 데이터를 features로 추출해 보내면, 4개 위험축에 대해
# 룰 임계값으로 점수 산정 후 가장 높은 위험 반환.

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


# 위험 판정은 규칙(결정적)이 담당하고, 그 결과를 보호자가 이해하기 쉬운 자연어로 풀어주는
# 설명만 LLM이 생성한다(하이브리드). LLM이 새 위험/수치를 만들지 않도록 범위를 못박는다.
_ANOMALY_SYSTEM = (
    "너는 시니어 돌봄 플랫폼의 보호자 안내 도우미다. 시스템이 규칙으로 산출한 이상징후 결과"
    "(위험유형·심각도·근거·권고)를 받아 보호자가 이해하기 쉬운 따뜻한 존댓말 2~3문장으로 설명한다. "
    "반드시 주어진 위험유형·근거·권고 범위 안에서만 말하고, 새로운 의학적 진단·수치·위험을 지어내지 마라. "
    "불안을 부추기지 말고 차분하게, 보호자가 지금 할 수 있는 행동을 안내하라. "
    "출력은 설명 문장만(머리말·JSON·마크다운 없이)."
)
_SEVERITY_LABEL = {"critical": "매우 높음", "high": "높음", "mid": "중간", "low": "낮음"}
_RISK_LABEL = {"nutrition": "영양/식사", "depression": "우울/정서", "delirium": "섬망", "fall": "낙상"}


def _anomaly_explanation(result: dict[str, Any]) -> str | None:
    """규칙 결과 위에 보호자용 자연어 설명을 LLM으로 생성(미가용/실패 시 None — 판정엔 영향 없음)."""
    if not llm_available():
        return None
    user = (
        f"위험 유형: {_RISK_LABEL.get(result['risk_type'], result['risk_type'])}\n"
        f"심각도: {_SEVERITY_LABEL.get(result['severity'], result['severity'])} (점수 {result['risk_score']}/100)\n"
        f"근거: {', '.join(result.get('trigger_pattern') or []) or '없음'}\n"
        f"권고: {', '.join(result.get('recommendation') or []) or '없음'}"
    )
    return llm_complete(_ANOMALY_SYSTEM, user, max_tokens=400, temperature=0.4)


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

    result = {
        "senior_id": req.senior_id,
        "risk_score": round(score, 1),
        "risk_type": top["type"],
        "severity": severity,
        "trigger_pattern": top["triggers"],
        "recommendation": recommend_map.get(top["type"], []),
        "all_risks": risks,
        "model": "rule-v1",  # 위험 판정은 규칙(결정적) 유지
    }
    # 하이브리드: 규칙 결과 위에 보호자용 자연어 설명을 best-effort로 덧붙임(실패 시 생략)
    explanation = _anomaly_explanation(result)
    if explanation:
        result["guardian_explanation"] = explanation
        result["explanation_model"] = active_model()
    return result


# ───────────────────────── 5. 챗봇 (LLM + KB 폴백) ─────────────────────────

_CHATBOT_SYSTEM = (
    "너는 Care&(케어앤) 돌봄 플랫폼의 상담 챗봇이다. 장기요양보험, 재가급여, 요양보호사 매칭, "
    "시니어 돌봄 일반에 대해 한국 기준으로 정확하고 간결하게(3~5문장) 존댓말로 답한다. "
    "수치·제도는 확실한 경우에만 제시하고, 불확실하면 공단(1577-1000)·기관 확인을 권하라. "
    "의료적 판단이 필요한 질문은 반드시 의료진 상담을 권하라."
)

_CHATBOT_KB = [
    {
        "keywords": ("등급", "한도", "본인부담", "재가급여", "급여"),
        "title": "장기요양보험 안내",
        "url": "https://www.longtermcare.or.kr",
        "snippet": "장기요양 등급별 재가급여 월 한도액 및 본인부담률 안내",
    },
    {
        "keywords": ("요양보호사", "자격", "교육", "방문요양"),
        "title": "요양보호사 제도 안내",
        "url": "https://www.mohw.go.kr",
        "snippet": "요양보호사 자격·방문요양 서비스 안내",
    },
    {
        "keywords": ("치매", "인지", "섬망"),
        "title": "중앙치매센터",
        "url": "https://www.nid.or.kr",
        "snippet": "치매 단계별 돌봄 가이드",
    },
]


def _kb_sources(question: str) -> list[dict[str, str]]:
    hits = [
        {"title": kb["title"], "url": kb["url"], "snippet": kb["snippet"]}
        for kb in _CHATBOT_KB
        if any(k in question for k in kb["keywords"])
    ]
    return hits or [
        {"title": "장기요양보험 안내", "url": "https://www.longtermcare.or.kr", "snippet": "장기요양보험 제도 전반 안내"}
    ]


class ChatbotRequest(BaseModel):
    question: str
    context: dict[str, Any] = {}


@app.post("/ai/chatbot/answer", dependencies=[Depends(verify_token)])
def chatbot_answer(req: ChatbotRequest) -> dict[str, Any]:
    ctx = json.dumps(req.context, ensure_ascii=False) if req.context else "없음"
    answer = llm_complete(_CHATBOT_SYSTEM, f"컨텍스트: {ctx}\n\n질문: {req.question}", max_tokens=600)
    if answer:
        return {"answer": answer, "sources": _kb_sources(req.question), "model": active_model()}
    return {
        "answer": (
            "지금은 AI 상담 엔진 점검 중이라 정확한 답변을 드리기 어렵습니다. "
            "장기요양보험 관련 문의는 국민건강보험공단(1577-1000) 또는 아래 안내 자료를 참고해 주시고, "
            "급한 문의는 Care& 고객센터로 연락해 주세요."
        ),
        "sources": _kb_sources(req.question),
        "model": "kb-fallback",
    }


# ───────────────────────── 6. 수요 예측 (실구현: 요일 계절성) ─────────────────────────
#
# match_requests 최근 56일 일별 건수 → 요일별 평균(seasonal naive)으로 향후 N일 예측.
# 데이터가 희소한 요일은 전체 일평균으로 보간. available_caregivers는 활성 인력 실측.

class ForecastRequest(BaseModel):
    region: str = "서울"
    days: int = 7


# 예측 수치는 통계(결정적)가 담당하고, 운영자용 자연어 인사이트만 LLM이 생성(하이브리드).
_FORECAST_SYSTEM = (
    "너는 시니어 돌봄 플랫폼의 운영 분석 도우미다. 통계로 산출된 수요예측 요약(기간·일평균/최고 "
    "예상 요청 수·활동 인력 수·수급 부족 예상일)을 받아 운영 담당자가 바로 이해할 간결한 한국어 "
    "2~3문장 인사이트를 쓴다. 주어진 수치 범위 안에서만 말하고 새 수치를 지어내지 마라. "
    "수요가 인력을 초과할 위험이 있으면 인력 확보를, 여유가 있으면 그 점을 알려라. 설명 문장만 출력."
)


def _forecast_insight(result: dict[str, Any]) -> str | None:
    """통계 예측 위에 운영자용 자연어 인사이트를 LLM으로 생성(미가용/실패 시 None)."""
    if not llm_available():
        return None
    fs = result.get("forecasts") or []
    if not fs:
        return None
    peak = max(fs, key=lambda f: f["predicted_requests"])
    avg = round(sum(f["predicted_requests"] for f in fs) / len(fs), 1)
    cg = fs[0].get("available_caregivers", 0)
    short = [f["date"] for f in fs if f["predicted_requests"] > cg]
    user = (
        f"지역: {result.get('region')}\n"
        f"예측 기간: {len(fs)}일\n"
        f"일평균 예상 요청: {avg}건\n"
        f"최고 예상일: {peak['date']} ({peak['predicted_requests']}건)\n"
        f"활동 인력 수: {cg}명\n"
        f"수급 부족 예상일: {', '.join(short) if short else '없음'}"
    )
    return llm_complete(_FORECAST_SYSTEM, user, max_tokens=400, temperature=0.4)


@app.post("/ai/forecast/demand", dependencies=[Depends(verify_token)])
def forecast_demand(req: ForecastRequest) -> dict[str, Any]:
    days = max(1, min(req.days, 30))
    lookback = 56
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DATE(created_at) AS d, COUNT(*) AS cnt
                FROM match_requests
                WHERE created_at >= CURDATE() - INTERVAL %s DAY
                GROUP BY DATE(created_at)
                """,
                (lookback,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) AS c FROM caregivers WHERE status = 'active' AND deleted_at IS NULL"
            )
            active_caregivers = cur.fetchone()["c"]
    finally:
        conn.close()

    counts_by_date = {r["d"]: int(r["cnt"]) for r in rows}
    today = date.today()
    # lookback 전 구간을 0 포함 일별 시계열로 펼침 (요청 없던 날 = 0건)
    start = today - timedelta(days=lookback)
    weekday_counts: dict[int, list[int]] = {i: [] for i in range(7)}
    total: list[int] = []
    for i in range(lookback):
        d = start + timedelta(days=i)
        c = counts_by_date.get(d, 0)
        weekday_counts[d.weekday()].append(c)
        total.append(c)

    overall_avg = sum(total) / len(total) if total else 0.0
    weekday_avg = {
        wd: (sum(v) / len(v) if v else overall_avg) for wd, v in weekday_counts.items()
    }

    forecasts = []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        pred = weekday_avg.get(d.weekday(), overall_avg)
        forecasts.append(
            {
                "date": d.isoformat(),
                "predicted_requests": round(pred, 1),
                "available_caregivers": active_caregivers,
            }
        )

    result = {
        "region": req.region,
        "forecasts": forecasts,
        "history_days": lookback,
        "history_total_requests": sum(total),
        "model": "seasonal-naive-v1",  # 예측 수치는 통계(결정적) 유지
    }
    insight = _forecast_insight(result)
    if insight:
        result["operator_insight"] = insight
        result["insight_model"] = active_model()
    return result


# ───────────── 7. 산후조리 챗봇 (LLM + 룰 폴백) ─────────────

_POSTPARTUM_SYSTEM = (
    "너는 Care&의 산후조리 전문 상담 챗봇이다. 산모의 회복, 모유수유, 신생아 돌봄에 대해 "
    "한국 산후조리 표준에 맞춰 따뜻한 존댓말로 답한다(4~6문장). "
    "산모 컨텍스트(출산 후 경과일, 초산 여부, 수유 방식)를 반영하라. "
    "발열·출혈·통증 악화 등 위험 신호가 언급되면 즉시 의료진 진료를 최우선으로 권하라. "
    "확실하지 않은 의학 정보는 단정하지 마라."
)


class PostpartumChatMessage(BaseModel):
    role: str = "user"
    content: str = ""


class PostpartumChatRequest(BaseModel):
    session_id: int | None = None
    user_message: str = ""
    history: list[PostpartumChatMessage] = []
    context: dict[str, Any] = {}


def _postpartum_rule_answer(req: PostpartumChatRequest) -> str:
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

    return (
        f"문의 주신 \"{req.user_message}\"에 대해 안내드립니다. "
        + " ".join(tips)
        + " 증상이 지속되면 담당 의료진과 상담하시기 바랍니다."
    )


@app.post("/chatbot/postpartum", dependencies=[Depends(verify_token)])
def chatbot_postpartum(req: PostpartumChatRequest) -> dict[str, Any]:
    """산후조리 챗봇. 응답 형식 {answer, sources}는 백엔드 기대와 일치."""
    history_txt = "\n".join(f"{m.role}: {m.content}" for m in req.history[-6:]) or "없음"
    ctx = json.dumps(req.context, ensure_ascii=False) if req.context else "없음"
    answer = llm_complete(
        _POSTPARTUM_SYSTEM,
        f"산모 컨텍스트: {ctx}\n\n대화 이력:\n{history_txt}\n\n산모 질문: {req.user_message}",
        max_tokens=700,
    )
    model = active_model() if answer else "rule-fallback"
    if not answer:
        answer = _postpartum_rule_answer(req)

    return {
        "answer": answer,
        "sources": [
            {
                "title": "산후조리 표준 가이드",
                "url": "https://www.mohw.go.kr",
                "snippet": "산후 회복 및 신생아 돌봄 표준 안내",
            }
        ],
        "model": model,
    }


# ───────────── 8. 산후조리 매칭 (룰 기반 실구현, DB 직결) ─────────────

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

    result = {
        "status": "accepted",
        "match_request_id": req.match_request_id,
        "eligible_caregivers": len(caregivers),
        "candidates_inserted": inserted,
        "model": "rule-v1",  # 순위 판정은 규칙(결정적) 유지
    }
    # 하이브리드: 1순위 추천 사유를 보호자용 자연어로 best-effort 추가(실패 시 생략)
    if top:
        note = _match_reco_note(top[0].get("reasons", []), "postpartum")
        if note:
            result["top_recommendation_note"] = note
            result["recommendation_model"] = active_model()
    return result


# ───────────── 9. C:Writer 케어 일지 생성 (LLM + 템플릿 폴백) ─────────────

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

_CARELOG_SYSTEM = (
    "너는 시니어 돌봄 플랫폼의 케어 일지 작성 도우미다. 요양보호사가 기록한 활동 목록을 받아 "
    "보호자용(따뜻한 존댓말, 3~4문장)과 의료진용(간결한 임상 요약) 일지를 작성한다. "
    "출력 JSON 스키마: {\"guardian_version\": str, \"medical_version\": str}. "
    "기록에 없는 활동·상태를 지어내지 마라. "
    "어르신 호칭은 입력된 이름(성+이름)을 줄이거나 성을 떼지 말고 전체를 그대로 쓴다. "
    "예: 입력 '박순자' → '박순자 어르신'(O) / '순자 어르신'(X)."
)


def _care_log_template(req: CareLogRequest) -> dict[str, Any]:
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
        "confidence": 0.8,
        "model": "template-v1",
    }


@app.post("/care-log/generate", dependencies=[Depends(verify_token)])
def care_log_generate(req: CareLogRequest) -> dict[str, Any]:
    """활동 기록 → 보호자/의료진용 일지 생성. LLM 우선, 실패 시 템플릿."""
    grouped: dict[str, list[str]] = {}
    for a in req.activities:
        label = _CATEGORY_LABEL.get(a.category, a.category)
        grouped.setdefault(label, [])
        if a.memo:
            grouped[label].append(a.memo)

    if req.activities and llm_available():
        acts = "\n".join(
            f"- [{_CATEGORY_LABEL.get(a.category, a.category)}] {a.memo or '수행함(메모 없음)'}"
            for a in req.activities
        )
        result = llm_complete_json(
            _CARELOG_SYSTEM,
            f"어르신: {req.senior_name}\n돌봄 시간: {req.duration_min}분\n"
            f"도메인: {req.service_domain or '-'}\n활동 기록:\n{acts}",
            max_tokens=800,
        )
        if result and result.get("guardian_version") and result.get("medical_version"):
            return {
                "guardian_version": result["guardian_version"],
                "medical_version": result["medical_version"],
                "categorized": grouped,
                "confidence": 0.92,
                "model": active_model(),
            }

    return _care_log_template(req)
