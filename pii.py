"""외부 LLM 전송 전 개인정보 가명 처리 (2026-09-28, 구현계획 S2-5 후속 — 국외 이전 최소화 ①).

외부 LLM(Gemini·Claude API)으로 나가는 문장에서 개인을 알아볼 수 있는 값을 대체어로 바꾸고,
답을 받은 뒤 원래 값으로 되돌린다. 저장되는 일지·답변은 기존과 같다.

  · 이름: 요청에 담긴 이름(대상자·인력·보호자·산모 — name/senior_name/… 키와 pii_terms)을 [이름1]… 로.
    성을 뗀 이름(영희)도 함께 가린다. 조사는 그대로 붙는다("[이름1]님이").
  · 형태로 찾는 값: 주민등록번호, 휴대폰·전화번호, 이메일, 도로명·지번 주소 → [주민번호]·[전화1]·[이메일1]·[주소1]
동작 범위는 요청 하나 — 엔드포인트가 use(req) 로 이름을 등록하고 llm_complete() 가 mask/unmask 한다.
"""
from __future__ import annotations

import contextvars
import re
from typing import Any

_terms: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("pii_terms", default=())

_NAME_KEYS = {"name", "senior_name", "recipient_name", "caregiver_name", "guardian_name", "client_name", "patient_name"}
_GENERIC = {"대상자", "어르신", "보호자", "산모", "환자", "아이", "회원", "고객", "돌봄전문가", "매니저", "선생님"}

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("주민번호", re.compile(r"\b\d{6}\s?-\s?[1-4]\d{6}\b")),
    ("전화", re.compile(r"(?<!\d)(?:01[016789]|0[2-6]\d?)[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")),
    ("이메일", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("주소", re.compile(r"(?:[가-힣]+(?:특별시|광역시|특별자치시|도|시)\s+)?[가-힣]+(?:시|군|구)\s+[가-힣0-9]+(?:로|길|동|읍|면|리)"
                      r"(?:\s*\d+(?:-\d+)?(?:번길)?)?(?:\s*\d+(?:-\d+)?)?(?:\s*[0-9]+동)?(?:\s*[0-9]+호)?")),
]


def _collect(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "pii_terms" and isinstance(v, (list, tuple)):
                out.update(str(x) for x in v if x)
            elif k in _NAME_KEYS and isinstance(v, str):
                out.add(v)
            else:
                _collect(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect(v, out)


def use(req: Any) -> None:
    """요청(pydantic 모델 또는 dict)에서 가릴 이름을 모아 이 요청 동안 쓴다."""
    data = req.model_dump() if hasattr(req, "model_dump") else (req.dict() if hasattr(req, "dict") else req)
    found: set[str] = set()
    _collect(data, found)
    terms: set[str] = set()
    for t in found:
        t = t.strip()
        if len(t) < 2 or t in _GENERIC or "○" in t:
            continue
        terms.add(t)
        if re.fullmatch(r"[가-힣]{3,4}", t):      # 한글 성명 → 성 뗀 이름(2자 이상)도
            terms.add(t[1:])
    _terms.set(tuple(sorted(terms, key=len, reverse=True)))   # 긴 것부터 바꿔 부분 겹침 방지


class Masker:
    def __init__(self, terms: tuple[str, ...] | None = None):
        self.terms = _terms.get() if terms is None else terms
        self.back: dict[str, str] = {}
        self.count = 0

    def _token(self, kind: str, original: str) -> str:
        for tok, orig in self.back.items():
            if orig == original and tok.startswith(f"[{kind}"):
                return tok
        n = sum(1 for t in self.back if t.startswith(f"[{kind}")) + 1
        tok = f"[{kind}{n}]" if kind != "주민번호" else f"[주민번호{n}]"
        self.back[tok] = original
        return tok

    def mask(self, text: str) -> str:
        if not text:
            return text
        out = text
        for kind, pat in _PATTERNS:
            out = pat.sub(lambda m: self._tok_count(kind, m.group(0)), out)
        for term in self.terms:
            if term in out:
                tok = self._token("이름", term)
                self.count += out.count(term)
                out = out.replace(term, tok)
        return out

    def _tok_count(self, kind: str, s: str) -> str:
        self.count += 1
        return self._token(kind, s)

    def unmask(self, text: str | None) -> str | None:
        if not text or not self.back:
            return text
        for tok in sorted(self.back, key=len, reverse=True):
            text = text.replace(tok, self.back[tok])
        return text


SYSTEM_NOTE = "\n\n[안내] 입력의 [이름1]·[전화1]·[주소1] 같은 대괄호 표기는 가려진 개인정보다. 그대로 옮겨 쓰고 풀어 쓰거나 추측하지 마라."
