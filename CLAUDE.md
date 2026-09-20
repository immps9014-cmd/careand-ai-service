# CLAUDE.md — Care& (careand) 통합돌봄 플랫폼

요양보호사·간병인·가사서비스전문가와 보호자를 연결하는 통합돌봄 매칭 플랫폼.
멀티도메인(시니어돌봄/병원간병/산모산후관리/아이돌봄/마음돌봄/생활지원서비스) 서비스.

**이 파일은 5개 레포 각각의 루트에 동일하게 배치되어 있다** (단일 모노레포가 아님). 어느 레포에서
작업하든 전체 플랫폼 토폴로지를 파악할 수 있도록 하기 위함. 레포별 세부 규칙은 하단 "레포별 규칙" 참조.

**2026-07-30 경로 통합**: backend를 제외한 4개 레포(admin-web/member-web/www/ai-service)는 원래
`/root` 바로 밑에 다른 테넌트(hisense, healing 등)와 뒤섞여 있었으나, `/root/caren/` 밑으로 모았다.
backend는 Apache DocumentRoot·PHP-FPM 설정 의존성 때문에 `/var/www/careand-backend` 그대로 유지.

---

## 배포 토폴로지

서버 `103.55.191.157` (SSH만 가능, 외부 아웃바운드 화이트리스트 제한 — pip/npm 등은 국내 미러 경유),
도메인 `caren.aiclaude.kr` (구 `careand.aiclaude.kr`는 301 리다이렉트), Apache vhost `/etc/httpd/conf.d/20-migrated-ssl.conf`.

```
caren.aiclaude.kr
├── /              → careand-www 302 리다이렉트 (비로그인 마케팅/탐색)
├── /www           → careand-www           :3107  Next.js  systemd careand-www
├── /app           → careand-member-web    :3106  Next.js  systemd careand-member-web (보호자/인력)
├── /admin         → careand-admin-web     :3105  Next.js  systemd careand-admin-web
└── /api/v1        → careand-backend       PHP-FPM 127.0.0.1:9000  systemd 없음(php-fpm)
                       └─ AI 호출 →  careand-ai-service :8001  systemd careand-ai
```

| 레포 | 경로 | 역할 | 포트 | systemd |
|------|------|------|------|---------|
| **careand-backend** | `/var/www/careand-backend` | Laravel 11 API, DB `careand_platform`(MySQL) | 9000(fpm) | `php-fpm` |
| **careand-admin-web** | `/root/caren/careand-admin-web` | 관리자 콘솔 (Next.js, basePath `/admin`) | 3105 | `careand-admin-web` |
| **careand-member-web** | `/root/caren/careand-member-web` | 보호자/돌봄전문가 회원 웹 (Next.js, basePath `/app`) | 3106 | `careand-member-web` |
| **careand-www** | `/root/caren/careand-www` | 공개 마케팅/탐색 웹 (Next.js, basePath `/www`) | 3107 | `careand-www` |
| **careand-ai-service** | `/root/caren/careand-ai-service` | 매칭 추천·챗봇·일지요약·이상징후·수요예측 (FastAPI) | 8001 | `careand-ai` |
| (부가) | — | 큐 워커 (`php artisan queue:work redis`, Job 코드 변경 시 재시작 필수) | — | `careand-queue` |

DB=MySQL `careand_platform`, 캐시/큐=Redis. 인증은 JWT(`access_token`, Sanctum 아님). AI 서비스는
`AI_SERVICE_TOKEN`(Bearer, backend `.env`와 동일값) 인증.

---

## 배포 명령어

```bash
careand-deploy backend    # git 스냅샷 커밋 → migrate --force → queue 재시작 → /api/v1/health 확인 → 실패 시 git reset --hard로 코드 롤백
careand-deploy admin      # git 스냅샷 → npm run build → .next.prev 백업 → systemd restart → HTTP 확인 → 실패 시 .next.prev로 롤백
careand-deploy member     # 위와 동일 패턴 (member-web)
careand-deploy www        # 위와 동일 패턴 (www)
careand-deploy ai         # git 스냅샷 → systemd restart → /health 확인
```
스크립트 원본: `/usr/local/bin/careand-deploy`.

**주의**
- backend 마이그레이션 실패 시 `careand-deploy`가 **코드까지 git reset --hard로 롤백**한다. DDL은 자동 롤백되지 않으므로 부분 적용된 DB는 수동 점검 필요. 실수로 되돌려진 커밋은 `git reflog`로 복구 가능.
- backend 라우트 추가/변경 후 반드시 `php artisan route:cache && config:cache && systemctl reload php-fpm` (안 하면 신규 라우트 404).
- Next.js 빌드는 `.next` 제자리 재빌드만 하고 systemd 재시작을 빼먹으면 옛 CSS 해시로 400이 나서 무스타일 화면이 뜬다 — 수동 빌드 후 반드시 해당 서비스 `restart`.
- MariaDB/MySQL 버전이 낮아(10.3) `ALTER TABLE ... AFTER` + `INSTANT` 알고리즘 조합을 지원하지 않는 경우가 있음 — 마이그레이션 작성 시 유의.
- 큐 Job(`app/Jobs/*`) 코드만 바꾸고 `careand-queue` 재시작을 안 하면 워커가 옛 코드를 계속 실행한다.

---

## Claude Code 에이전트 / 스킬

이 플랫폼 전용 에이전트 6개 + 스킬 5개가 정의돼 있다. `/root/caren/.claude/{agents,skills}/`에 있고
admin-web/member-web/www/ai-service 4개 레포가 이 경로를 상속해서 공용으로 쓴다(`/root` git 저장소에
추적됨). backend는 경로가 달라(`/var/www/careand-backend`) `deploy-checklist` 스킬만 이 레포
`.claude/skills/`에 동일 내용으로 복제해뒀다. 케어앤 관련 작업을 할 땐 아래 중 맞는 게 있는지 먼저
확인하고 재사용할 것 — 코드 실태와 어긋난 내용을 발견하면 새로 만들지 말고 해당 파일을 갱신할 것.

**에이전트**
- `backend-engineer` — Laravel API/JWT·권한/외부연동 스텁/큐 Job/마이그레이션
- `frontend-engineer` — admin/member/www 공통 Next.js, 인증 아키텍처, basePath, 도메인 분기
- `ai-service-engineer` — FastAPI(careand-ai-service), LLM우선+결정적폴백, L2R, Fuseki 연동, 모델교체
- `devops-deploy` — systemd/Apache vhost/서버 제약(아웃바운드 화이트리스트·메모리), 장애대응
- `matching-ontology` — 매칭 스코어링(rule-v3+L2R+가성비재랭킹), 가격 레이어, 온톨로지 스키마
- `design-doc-editor` — AI 설계서 docx/pdf 편집·재생성 툴체인

**스킬**
- `deploy-checklist` — `careand-deploy` 배포 전/후 점검
- `e2e-test-setup` — lt-server 헤드리스 브라우저 E2E 테스트 셋업
- `domain-target-check` — 신규 서비스 도메인 추가 시 SSOT 3곳(backend config/온톨로지TTL/frontend) 동기화 체크
- `ai-model-swap` — Whisper STT 모델 교체, L2R 재학습 절차
- `venv-path-fix` — venv 이동 후 shebang 깨짐(`203/EXEC`) 진단·수정

---

## 코딩 규칙

### 공통
- 시크릿/토큰은 `.env`로만 관리, 코드/커밋에 하드코딩 금지.
- production DB 직접 수정 금지 — 반드시 Laravel 마이그레이션 사용.

### Laravel (careand-backend)
- 외부연동(OTP/PG/FCM/NHIS/홈택스/복지부)은 `app()->environment()`가 아니라 `config('services.external.stub')`(`.env` `EXTERNAL_STUB`)로 스텁 분기 — `APP_ENV`와 무관하게 프로덕션에서도 스텁 유지 가능해야 하는 서비스가 있음.
- 매칭후보/AI일지 등 비동기 처리는 큐 Job으로 (`GenerateMatchCandidatesJob`, `GenerateCareLogJob` 등). 동기 처리로 되돌리지 말 것.

### Next.js (admin/member/www 공통)
- 회원가입 관련 role: 대리형(요양보호사가 어르신을 대신 요청 — 요양/간병/아이/마음)과 본인형(산모산후/가사, 본인이 직접 요청)이 구분됨. 새 도메인 추가 시 이 분기를 따를 것.
- `guardians.intent`로 요청 성격을 구분 — 별도 role이 아님. **실DB 값은 `care`·`housekeeping`·`postpartum` 세 가지다**
  (2026-09-20 온톨로지 적재 중 실측 3건 — 산후 본인형 신청자가 guardian으로 별칭 처리되는 흐름의 흔적).
  값을 늘릴 땐 `careand-ai-service/ontology/care-domain.ttl`의 `care:GuardianIntent` 개체도 같이 추가할 것 —
  빠지면 `ontology/check.py`의 '어휘 미등록 보호자 의도'가 FAIL 난다.
- www의 서버사이드 self-fetch(`NEXT_PUBLIC_API_URL`)는 `/etc/hosts`의 `127.0.0.1 caren.aiclaude.kr` 항목에 의존한다 — 이 항목이 없으면 서버가 자기 자신을 공인 IP로 해석해 egress 차단에 걸려 통계 API가 타임아웃-폴백(더미 문구)된다.
- 돌봄전문가(인력) 개인정보는 비로그인 상태로 노출 금지 — 목록/상세는 로그인 후 member-web `/app/caregivers`에서만.

### AI 서비스 (careand-ai-service)
- 각 엔드포인트는 LLM(Claude API, `ANTHROPIC_API_KEY` 있을 때) 우선, 실패/미설정 시 룰/템플릿 폴백 — 응답의 `model` 필드로 실제 사용된 경로 구분 가능. 폴백 경로를 제거하지 말 것(키 미설정 환경에서도 서비스가 죽지 않아야 함).

---

## 절대 금지

- `git push --force` 금지.
- production DB 직접 UPDATE/DELETE 금지 — 마이그레이션 또는 승인된 스크립트로만.
- 마이그레이션 없이 스키마 변경 금지.
- `.env` 시크릿을 커밋하거나 코드에 하드코딩 금지.
- 돌봄전문가(인력) 개인정보 비로그인 공개 노출 금지.
- 큐 Job/backend 코드 변경 후 관련 systemd(`careand-queue`, `php-fpm`) 재시작 생략 금지.

---

## 레포별 규칙

- **careand-backend**: Laravel 표준 관례. 라우트/캐시 변경 후 `route:cache`+`config:cache`+`php-fpm reload` 필수(위 참조).
- **careand-admin-web / careand-member-web / careand-www**: Next.js. `.next.prev`는 배포 스크립트의 자동 롤백용이므로 수동 삭제 금지. member-web은 E2E 시 jsdelivr 폰트 요청을 차단해야 hydration이 정상 동작함(헤드리스 테스트 환경 특이사항).
- **careand-ai-service**: FastAPI + venv(`venv/bin/uvicorn`). 모델 교체 스크립트(`swap_whisper*.sh`, `retrain.sh`)는 실행 전 `.env.bak-*` 백업 관례를 따를 것. **venv를 다른 경로로 옮기면 `venv/bin/*`의 shebang(`#!/<구경로>/venv/bin/python3.9`)이 전부 깨져 `systemctl start`가 `203/EXEC`로 죽는다** — 디렉토리 이동 시 `venv/bin/` 전체에서 구 경로 shebang을 새 경로로 일괄 치환(`sed -i '1s|^#!<구경로>|#!<신경로>|'`) 필요.
