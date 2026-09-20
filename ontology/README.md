# caren 온톨로지 (Fuseki `caren` 데이터셋)

careand_platform(MySQL)의 업무객체와 돌봄 도메인 어휘를 RDF 로 투영한다.
텔롬엑스 3사 MES 온톨로지(TX-ONT-DESIGN, hisense·naturefood·infurs)와 **같은 운영 구조**
— ETL → 그래프 통째 교체 → 불변식 점검 → 매시 재적재 — 를 쓴다.

```
ontology/
├── care-domain.ttl     스키마 SSOT (클래스·관계·어휘 개체)  → graph/schema
├── etl_caren.py        DB → N-Triples (SELECT 전용)         → graph/caren
├── check.py            적재 후 불변식 점검 53항목
├── load.sh             스키마 적재 → 추출 → 적재 → 점검
├── reload.cron.sh      cron 진입점(잠금·로그·status.json)
└── out/                caren.nt, status.json  (gitignore)
```

## 운영

| | |
|---|---|
| 데이터셋 | `http://localhost:3030/caren` (moai-fuseki 컨테이너 공유 — 전용 컨테이너 안 띄운다) |
| 그래프 | `…/graph/schema` 715 · `…/graph/caren` 6,937 트리플 (2026-09-20 기준) |
| 주기 | 매시 :10 (`reload.cron.sh`, 약 2초) — 3사는 :25 hisense · :40 naturefood · :55 infurs |
| 로그 | `/var/log/caren-ontology.log` (한 줄), 상태 `out/status.json` |
| 수동 | `./venv/bin/python` 이 필요하다 → `ontology/load.sh` (스키마만: `--schema-only`) |

```bash
cd /root/caren/careand-ai-service
ontology/load.sh                      # 전체 재적재 + 점검
venv/bin/python ontology/check.py     # 점검만
```

## 소비처 — 이 그래프를 실제로 읽는 코드

- `ontology.py` → `l2r.py` 의 `ontology_match` feature (질병 → 필요 특기 → 보유 인력)
- `ontology.py` → `main.py /ai/voice/transcribe` 의 STT hotwords (전역 38 + 질병별 연관 용어)

`ontology.py` 는 **schema 그래프만** 본다(`FROM <…/graph/schema>`).
⚠ FROM 을 빼면 TDB2 기본그래프가 비어 있어 **조용히 빈 결과**가 돌아온다 — Fuseki 다운
폴백과 구분이 안 된다. r2.0 에서 named graph 로 옮기며 생긴 제약이다.

## 설계 결정 (바꾸려면 근거가 필요하다)

**DB 가 SSOT, 그래프는 읽기 투영.** ETL 은 SELECT 만 한다. 업무 상태를 그래프로 바꾸지 않는다.

**IRI = `http://caren.aiclaude.kr/id/{Class}/{key}`.** 3사 MES 는 업무키(수주번호·품목코드)를
자연키로 썼지만 careand_platform 엔 업무키 컬럼이 거의 없다 — 코드가 있는 건
`service_categories.code`·`branches.code` 뿐이고 나머지는 PK 를 쓴다. 업무키가 생기면 그때 바꾼다.

**대상자 세 테이블을 한 클래스로.** `seniors`·`nursing_patients`·`postpartum_clients` →
`care:Recipient` + `care:recipientKind`. 도메인별로 클래스를 늘리지 않는다
(`MatchRequest::recipientFeatures()` 의 도메인 추상화와 같은 방침).

**개인정보 미적재 (caren 고유 — 3사엔 없던 제약).** Fuseki 는 인증 없이 localhost 에 열려 있고
moai·kcro·tx-mes 와 한 인스턴스를 공유한다. 그래서
- 이름 → 마스킹(`김○○`), 좌표 → 소수 2자리 반올림(≈1.1km)
- 연락처·이메일·주소원문·면허번호·암호화컬럼·**STT 전사 본문**·AI 요약 본문 → 적재하지 않음
- 법인명(`organizations.name`)은 개인정보가 아니라 그대로 싣는다

`check.py` 가 DB 의 실제 이름·전화·이메일·주소 문자열이 그래프에 있는지 매 적재마다 확인한다.

**시계열은 적재하지 않는다.** 3사에서 센서를 뺀 것과 같은 이유로 `notifications`(45행,
알림 이력)도 빼고, `attendance_logs`·`voice_logs` 는 세션당 몇 건이라 싣는다.

**Kitchen Sink 미승계.** `guardians.preferences`, `match_requests.price_estimate`,
`reviews.tags` 처럼 의미가 화면/서비스 코드에만 있는 JSON 은 싣지 않는다.
`requirements` 는 `preferred_gender` 한 키만 꺼낸다(매칭 파티션 정렬의 근거값이라서).

## 어휘 정합 — 이 디렉터리의 존재 이유 절반

`care:code` 는 **DB 에 저장된 문자열 그대로**다. 개념 라벨(`rdfs:label`)과 다를 수 있고
(`hk_cleaning` ↔ "가사 청소"), 한 개념이 DB 문자열 두 개로 들어와 있으면 code 를 둘 단다
(`nursing_hospital`·`병원간병` → `care:SpNursingHospital` 하나).

2026-07-30 PoC 는 여기가 깨져 있었다 — `caregivers.specialties` 22종 중 온톨로지와 같은 값이
"정서지원" 하나뿐이었고 `고혈압`·`관절염`·`허리`는 질병 어휘에 아예 없어서 `ontology_match`
feature 가 **구조적으로 늘 0** 이었다. r2.0 에서 DB 실값 전수로 맞췄고, `check.py` 의
'어휘 미등록 특기/질병/도메인/의도/중증도용어 = 0' 이 재발을 막는다.

**근접 특기는 상·하위만 인정한다.** `?related broaderSpecialty* ?req` 와 그 반대 방향을
따로 UNION 한다. r1 처럼 `(broader|^broader)*` 로 섞으면 위로 갔다 내려오는 경로가 생겨
**형제 특기까지** 근접으로 인정된다(실측: 치매→가족상담, 이제는 안 걸린다).

적용 결과(2026-09-20 실측, 활성 인력 기준):
치매 11명 · 뇌졸중 5명 · 당뇨 4명 · 고혈압 4명 · **관절염 0명 · 허리 0명**.
뒤 둘이 0 인 건 어휘 문제가 아니라 **이동보조·재활보조 특기를 가진 인력이 DB 에 없어서**다.

## 점검 53항목이 보는 것

A 개체 수(ETL 과 같은 필터) · B 필수 링크 · C 구조(이중 클래스·미선언 술어·끊긴 참조) ·
D 어휘 정합 · E 개인정보 · F 매칭 폐쇄성 · G 데이터 품질 경고.

FAIL 은 그래프를 믿을 수 없다는 뜻(종료코드 1), WARN 은 원천 DB 의 상태 보고다.
현재 WARN 5건 — 전부 데이터 사실이고 적재 오류가 아니다:

| WARN | 내용 |
|---|---|
| 대상자 레코드가 없는 요청 1 | `match_requests#24` → `postpartum_clients#3` 이 소프트삭제(2026-07-03)됐다. ETL 이 링크를 만들지 않는다 |
| 질병 6종 중 인력이 닿는 건 4종 | 관절염·허리 (위 참조) |
| 후보 없는 매칭요청 2 | |
| 특기 없는 활성 인력 2 / 좌표 없는 활성 인력 1 | 매칭에서 자연히 밀린다 |

## 함정

- **스키마를 먼저 올려야 ETL 이 돈다.** `etl_caren.py` 는 `care:code` 매핑을 TTL 에서가 아니라
  **Fuseki 의 schema 그래프에서** 읽는다(어휘를 두 군데 적으면 반드시 어긋나므로).
  `load.sh` 가 그 순서를 보장한다. 스키마만 고쳤으면 `load.sh --schema-only`.
- **문법 검증(riot)을 따로 돌리지 말 것.** PUT 이 파싱·저장을 한 트랜잭션에서 하므로 깨진
  입력은 400(줄·칸 번호 포함)으로 거부되고 기존 그래프가 남는다. 컨테이너 안에서 JVM 을
  추가로 띄우면 moai-fuseki 메모리 한도(550MB)에 걸려 스래싱한다 — 3사에서 겪은 사고다.
- **`ontology.py` 캐시.** 성공한 조회만 캐시하고 실패는 캐시하지 않는다(Fuseki 복구 후 즉시
  정상화돼야 하므로). 재적재로 어휘가 바뀌면 `careand-ai` 재시작 전까지 옛 값이 남는다 —
  어휘를 고쳤으면 `careand-deploy ai`.
- **`out/` 은 커밋하지 않는다**(`.gitignore`).
