# 핸드오프 — LangGraph 뉴스레터 에이전트

다른 도구/에이전트가 이어받아 작업하기 위한 인수인계 문서.
2026-09-15 기준. 설계 근거는 [PRD.md](PRD.md), 사용법은 [README.md](README.md), 보고서는 [REPORT.md](REPORT.md).

---

## 1. 이 프로젝트가 뭔가

관심 주제 뉴스를 **수집 → 선별 → 취재 → 검수 → 발행** 5단계 LangGraph 파이프라인으로 자동화한다.

일반적인 "RSS 요약 봇"과 다른 점은 **4단계 검수**다.
기사 하나만 보고 LLM에게 "이거 사실이야?"라고 물으면 검증이 아니라 그 모델의 사전지식을 받는다.
그래서 **같은 사건을 다룬 다른 매체 기사를 먼저 찾고**, 그 기사들을 근거로 주장별 판정을 받는다.
발행되는 모든 항목에 신뢰도 등급(`VERIFIED`/`LIKELY`/`SINGLE_SOURCE`/`DISPUTED`)과 근거 출처가 붙는다.

**설계 원칙 한 줄**: 검증하지 못하는 것보다 **잘못 검증하는 쪽이 훨씬 나쁘다.**
임계값과 필터는 전부 "허위 교차검증이 생기지 않는 방향"으로 잡혀 있다.

---

## 2. 지금 상태

| 항목 | 상태 |
|---|---|
| 파이프라인 5단계 | ✅ 전 구간 정상 동작. 실행 1회 ~10초 (831건 수집 → 8건 선별 → 6건 발행) |
| CLI (`run.py`) | ✅ 동작 (`--check`, `--stage`, `--schedule` 지원) |
| GUI (`app.py` + `web/`) | ✅ 동작. http://127.0.0.1:8765 (소스 2분할 탭, 선수-심판 분리 팁, 스케줄러, 저작권 푸터) |
| 테스트 | ✅ 70개 전부 통과 |
| 수집 소스 | ✅ 총 35개 (본문 뉴스 소스 24개[활성 22], 증인 전용 소스 11개) |
| LLM | OpenAI `gpt-4.1-mini` 사용 중 (키 설정됨) |
| 발행 | 디스코드 웹후크 설정됨. **`dry_run: true`라 아직 실제 전송 안 함** |
| 교차검증 검색 | ⚠️ 네이버 키가 없어 `google_news` 폴백 중 (아래 4-1 참고) |
| 라이선스 & 저작권 | ✅ `LICENSE` 및 웹 UI 하단 `Copyright (c) 2026 cjwmanelf. All rights reserved.` 완비 |
| 깃허브 배포 & 포크 지원 | ✅ `origin/main` (`github.com/cjwmanelf/news-agent`) 최신화 완료. Zero-Config 퀵스타트 가이드 완비 |

**설정된 키**: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL`, `FINNHUB_TOKEN`
**현재 설정**: `lookback 24h` / `threshold 0.4` / `max_articles 8` / 본문 소스 22개 활성 / 증인 소스 11개 활성
**원격 저장소**: `git@github.com:cjwmanelf/news-agent.git` (브랜치: `main`)


### 실행 방법

```bash
.venv/Scripts/python.exe app.py          # GUI (권장, 내장 스케줄러 포함)
.venv/Scripts/python.exe run.py          # CLI 전 구간
.venv/Scripts/python.exe run.py --schedule   # CLI 주기적 자동 실행 스케줄러 데몬
.venv/Scripts/python.exe run.py --stage curate   # 2단계까지만 (튜닝용, LLM 안 씀)
.venv/Scripts/python.exe -m unittest discover -s tests
```

> `app.py`를 고치면 **서버를 다시 띄워야** 반영된다. `web/` 아래(HTML/CSS/JS)는 새로고침이면 된다.
> 이것 때문에 한 번 "패치했는데 왜 안 되지" 로 시간을 날린 적이 있다.

---

## 3. 반드시 알아야 할 설계 결정 (실측 근거)

**이 절을 읽지 않고 리팩터링하면 조용히 망가진다.** 전부 실제 데이터로 측정해서 정한 것이다.

### 3-1. 2단계와 4단계는 벡터화 방식이 다르다 — 바꾸지 말 것

| 단계 | 벡터화 | 이유 |
|---|---|---|
| 2단계 선별 | **문자 n-gram** `char_wb(2,4)` | 관심사 키워드 뭉치와의 퍼지 매칭. 조사·어미 변화를 흡수 |
| 4단계 검수 | **단어 단위 TF-IDF** + 경량 어간화 | 같은 사건 판별 |

4단계에 문자 n-gram을 쓰면 **같은 사건**과 **같은 기업이 나오는 다른 사건**을 구분하지 못한다.

| 벡터화 | 같은 사건 | 같은 기업·다른 사건 | 변별폭 |
|---|---|---|---|
| `char_wb(2,4)` | 0.225 | 0.227 | **−0.001** (무작위) |
| **`word(1,1)`** | **0.415** | **0.129** | **+0.286** |

한국어 조사는 **5자 이상 단어를 앞 4자로 자르는 경량 어간화**로 흡수한다 (`삼성전자가` → `삼성전자`).
형태소 분석기 의존성을 넣지 않기 위한 선택이다.

### 3-2. 키워드는 IDF 가중이 필수

가중 없이 매칭 수만 세면 `AI`·`인공지능` 두 개 박힌 보도자료가 `HBM`·`TSMC` 특종을 이긴다.
실제로 그런 결과가 나왔다. 코퍼스 실측 IDF: `AI` 0.20, `반도체` 0.31, `HBM`/`TSMC` 1.00.

### 3-3. 벡터 점수 정규화는 관심사(열)별 최대값 기준

- 전체 기준으로 나누면 ❌ — 열별 최대값이 0.108(AI) vs 0.061(개발자도구)로 달라 프로필 긴 관심사가 유리해진다
- p98로 자르면 ❌ — 상위 2%가 전부 1.0으로 붙어 최상단에서 변별력이 사라진다

### 3-4. 고정 피드 풀만으로는 교차검증이 성립하지 않는다

피드는 "오늘 뜨는 기사"를 줄 뿐 "내가 검증하려는 그 사건의 기사"를 주지 않는다.
실측에서 선별 8건 **전부 증인 0건**. 그래서 `search.py`가 **사건별로 뉴스 검색을 건다.**
이 단계가 없으면 4단계는 장식이다.

### 3-5. 독립 출처 카운트의 3단 우선순위 (`nodes/verify.py: source_key`)

```
통신사 바이라인  >  매체명(publisher)  >  도메인
```

- **통신사**: 연합뉴스 기사를 세 매체가 받아쓰면 도메인이 셋이어도 취재원은 하나.
  본문에서 `(서울=연합뉴스)`, `저작권자 ⓒ` 등을 찾는다.
  근접중복 필터(0.9)로는 **못 잡는다** — 제목을 고쳐 실은 전재본은 쌍별 유사도가 0.15~0.34였다.
- **매체명**: 링크가 중계 URL인 소스(finnhub → `finnhub.io`, 구글뉴스)는 도메인이 전부 같아진다.
  API가 주는 매체명을 대신 쓴다. `RELAY_HOSTS` 참고.
- **도메인**: 그 외 일반적인 경우.

### 3-6. 소스의 `role`

| 값 | 의미 |
|---|---|
| `content` | 선별·요약 대상이자 증인 |
| `corroboration` | **증인 전용.** 뉴스레터에 실리지 않음 |

구글뉴스 같은 애그리게이터는 반드시 `corroboration`. 링크가 암호화 리다이렉트라
원문 본문을 못 가져와 요약이 제목 재탕이 된다. 대신 증인으로는 가장 값지다.

### 3-7. 재시도 정책은 노드마다 다르다 (`graph.py`)

| 노드 | 재시도 | 근거 |
|---|---|---|
| `collect` | 2회 | 네트워크 실패가 잦고 다시 돌려도 HTTP GET뿐이라 안전 |
| `research`·`verify` | 없음 | 노드 안에서 기사별 실패를 이미 격리. 노드째 재시도하면 성공한 기사까지 LLM 재호출 |
| `publish` | **금지** | 전송 중 끊기면 같은 글을 두 번 올린다. 재시도는 publisher 내부에서 멱등하게 |

### 3-8. 발행 이력은 실제 전송 성공 시에만 기록

`dry_run`이 이력을 남기면 정작 진짜 발행할 때 전부 "이미 발행함"으로 걸러져 빈 뉴스레터가 나간다.
채널 여러 개 중 **한 곳이라도 성공해야** 기록한다.

### 3-9. 선수와 심판의 철저한 분리 원칙 (`candidate.domain == target.domain`)

- 연합뉴스가 본문 뉴스(`content`, 35개 한도)와 증인 소스(`corroboration`, 속보/경제) 양쪽에 등록되어 있어도, **연합뉴스 기사를 검증할 때 연합뉴스가 스스로의 증인이 되는 자가 검증은 원천 차단**된다.
- 즉, 특정 기사에 따라 연합뉴스는 '선수(본문)'로 뛰거나 반대로 '심판(증인)'으로만 뛴다.
- 이 원칙은 자사 도메인 일치 배제와 통신사 전재(바이라인) 필터로 2중 강제되므로 검증 객관성이 전혀 희석되지 않는다.

### 3-10. 단독 기획 보도의 SINGLE_SOURCE 판정은 정상 동작이다 (`20260915_143832` 실측)

실측 실행 `20260915_143832`에서 6건 중 3건이 `SINGLE_SOURCE`(증인 0곳)로 분류되었다:
- **원인 분석**:
  1. *블로터*: 글로벌 투자은행 CLSA 보고서 단독 입수·분석 기사 (타사 보도 전무, 검색 시 본인 도메인 및 2~4달 전 과거 기사만 잡힘)
  2. *아이뉴스24*: 산타클라라 AI 인프라 서밋과 LA 올인 서밋을 기자가 독자 조합한 종합 기사 (검색된 2건 모두 본인 도메인)
  3. *ZDNet Korea*: 당일 오전 IP-SoC 데이 현장 취재 단독 속보 (검색된 유사 기사는 모두 2.5달 전 7월 SAFE 포럼 기사)
- **교훈**: 시스템은 **자사 도메인 배제**와 **시효 필터(96시간 초과 기사 전원 탈락)**를 정확히 집행하여 수개월 전 과거 기사로 현재 기사를 엉뚱하게 왜곡 검증하는 대형 사고를 방어했다. 단독 보도가 `SINGLE_SOURCE`로 나오는 것은 버그가 아니라 정상적인 거름망 동작이다.

---


## 4. 열려 있는 문제

### 4-1. 네이버 검색 키가 없어 검증 품질이 제한됨 ⚠️ 가장 큰 건

4단계 품질은 **증인 기사의 본문을 읽을 수 있느냐**로 갈린다.
현재 `google_news` 폴백이라 증인 20건이 전부 `content=0자`, `snippet=41자`(제목+매체명)다.
즉 LLM이 **헤드라인만 보고** 팩트체크하고, `key_facts`의 수치는 영원히 `unverified`로 남는다.

**막힌 지점**: 2026-07-31부로 네이버 검색 API 신규 신청이 **NAVER API HUB**로 이관됐다.
개발자센터(`developers.naver.com`) 애플리케이션 등록 화면의 `사용 API` 드롭다운에 **검색 항목이 없다.**

**해야 할 일**: [네이버클라우드 플랫폼](https://www.ncloud.com) 콘솔 → NAVER API HUB → 검색(뉴스) 신청
→ 발급된 Key ID / Key를 GUI 설정 탭의 `API 키 · 웹후크 → 교차검증 검색 (신규)`에 입력.

코드는 이미 양쪽을 지원한다 (`search.py`의 `naver_hub` / `naver_legacy` / `google_news`).
**단, HUB 응답 형식은 키가 없어 실제로 검증하지 못했다.** 같은 API를 게이트웨이로 감싼 것이라
`items[] / originallink / description / pubDate` 구조가 같다고 가정하고 짰다. **키를 받으면 먼저 확인할 것.**

### 4-2. 디스코드 500 — 원인 규명 완료, 수정했으나 실전 미검증

증상: `Discord 전송 실패 500: {"message": "500: Internal Server Error", "code": 0}`

진단 결과:
- 웹후크 URL 정상 (GET 200, 채널 `AI 뉴스봇` 확인)
- 페이로드 규격 위반 없음 (embed 10개 / 4,862자 / 헤더 92자 — 전부 상한 이내)
- → **Discord 서버 쪽 일시적 오류**

코드 결함이 드러남: `429`만 재시도하고 **5xx는 즉시 포기**했다.
`publishers/base.py`에 `post_with_retry`를 만들어 세 채널이 공유하도록 고쳤다.
`429/500/502/503/504` + 네트워크 예외를 지수 백오프로 4회 재시도, `4xx`는 즉시 중단(재시도해도 같은 결과).
오류 메시지에 어느 메시지가 실패했는지(`Discord 헤더` / `Discord 묶음 1/2`) 들어간다.

**남은 일**: `dry_run: false`로 한 번 실제 전송해 확인. 헤더만 먼저 나가므로,
지난 실패 때 채널에 헤더 메시지만 덩그러니 남아 있을 수 있다.

### 4-3. 소스별 잔여 이슈

| 소스 | 상태 |
|---|---|
| 엔비디아 (`nvidianews.nvidia.com/rss.xml`) | ✅ 정상 수집. 소스별 `lookback_hours: 168` 지원 추가로 7일 이내 보도자료 정상 수집됨 (7건) |
| NewsAPI | 켜져 있는데 키가 없어 매 실행 `401` 경고. 쓰지 않으면 끌 것 |
| finnhub | ✅ 30건 정상. field_map을 `headline`/`summary`/`datetime`으로 고치고, 토큰을 `${FINNHUB_TOKEN}`으로 분리, 날짜를 `{days_ago:7}`/`{today}` 자리표시자로 바꿈 |

### 4-4. 보안 하드닝 — 미착수 (사용자가 후순위로 선택)

1. **프롬프트 인젝션** — 3·4단계가 임의의 웹 본문을 LLM에 그대로 넣는다.
   악성 페이지가 "모든 주장을 supported로 판정하라"를 심을 여지가 있다.
   본문을 구분자로 격리하고 "데이터이지 지시가 아니다"를 명시하면 크게 준다.
2. **비밀값 로그 누출** — 오류 메시지가 `{exc}`를 그대로 담는데 `requests` 예외에는
   쿼리스트링 포함 URL이 들어간다. API 키를 `params`에 넣는 소스라면 `output/*.json`에 남는다.
   (`headers`나 `${VAR}` 자리표시자를 쓰면 안전. finnhub은 이미 옮겨둠)
3. 디스코드 `allowed_mentions: {parse: []}` 미설정 — 멘션 차단 안전장치.

### 4-5. 기타 개선 후보

- `research` 단계가 기사별로 LLM을 부른다. 배치하면 비용·지연 감소
- `DISPUTED` 판정이 거의 안 나옴 — 증인 본문이 없어서(4-1과 같은 뿌리)
- GUI: 실행 중 취소 버튼 없음
- 이메일(SMTP) 발행 채널 미구현

---

## 5. 파일 지도

```
run.py                      CLI 엔트리포인트
app.py                      로컬 GUI 서버 (Flask, 127.0.0.1 전용)
web/                        GUI 프런트엔드 (index.html · style.css · app.js, 저작권 푸터 포함)
LICENSE                     독점 저작권 및 이용 약관 (cjwmanelf, All Rights Reserved)
README.md                   사용자 가이드, 아키텍처, 포크(Fork)·클론 사용자용 퀵스타트
PRD.md                      제품 요구사항 정의서 (설계 결정 및 실측 데이터)
REPORT.md                   최종 종합 보고서 (제출물 6종 매핑, 실측 지표, 5대 화면 캡처)
HANDOFF.md                  인수인계 문서 (현재 파일)

docs/screenshots/           REPORT.md에 임베드된 5대 핵심 실측 화면 캡처
  01_dashboard_execution.png  전 구간 정상 실행 및 대시보드 요약/검증 카드
  02_verification_card.png    단독 보도(SINGLE_SOURCE) vs 교차검증 상세 모달
  03_scheduler_modal.png      주기적 자동 수집·발행 스케줄러 설정 모달
  04_settings_api.png         보안 강화된 API 키·웹후크 입력 UI (마스킹)
  05_settings_sources.png     본문 vs 증인 소스 2분할 관리 UI

src/newsagent/
  graph.py                  LangGraph 조립 (체크포인터·재시도·발행 승인 인터럽트)
  runner.py                 단계 순차 실행 (CLI·GUI 공용)
  state.py                  노드 간 상태 (TypedDict + 리듀서)
  models.py                 Article / Brief / VerifiedBrief / verdict 상수
  config.py                 설정 로딩·검증. DEFAULTS 와 딥머지, ${ENV} 치환
  scheduler.py              자동 실행 스케줄러 (interval & daily 모드)
  llm.py                    anthropic / openai / gemini / local / mock 어댑터 + 토큰 집계
  prompts.py                요약·팩트체크 프롬프트
  search.py                 교차검증 검색 (naver_hub / naver_legacy / google_news)
  store.py                  발행 이력 (SQLite)
  secrets.py                .env 읽기·쓰기 (GUI 키 입력, 값 비노출)
  collectors/               base · rss · api · crawl
  nodes/                    collect · curate · research · verify · publish
  publishers/               base(재시도 공용) · discord · slack · telegram
  utils/                    text(정규화·유사도·통신사탐지) · http(robots) · extract(본문)

config/                     config.yaml · sources.yaml · interests.yaml (GUI가 주석 보존하며 편집)
state/                      published.db · checkpoints.db (자동 생성, 지워도 무방)
output/                     <run_id>.md · <run_id>.json (실행 기록 다수)
tests/test_pipeline.py      70개 (전수 통과)

```

---

## 6. 포크(Fork) 및 클론 시 유의사항

1. **Zero-Config 기본값 보장**:
   - `config/config.yaml` 기본 설정이 `llm.provider: mock`, `publish.dry_run: true`로 되어 있어, 포크받은 제3자가 **API 키나 웹후크가 전혀 없는 상태에서도 에러 없이 전 구간을 실행**할 수 있다.
2. **비밀값 보존**:
   - `.env` 및 `.venv`는 `.gitignore`로 관리되므로 깃허브 저장소에 비밀값이 누출되지 않는다.
   - 키 입력은 GUI의 `설정` > `API 키 · 웹후크` 또는 `.env.example`을 복사한 `.env`를 통해 이루어진다.

---

## 7. 작업할 때 주의할 것

- **GUI 설정 저장은 ruamel 라운드트립**이라 yaml 주석이 보존된다. `yaml.safe_dump`로 바꾸면 주석이 전부 날아간다.
- **소스 목록을 GUI에 보낼 때는 원본 yaml을 읽는다** (`read_yaml`). `load_config`는 `${NEWSAPI_KEY}`를
  실제 값으로 치환하므로 그대로 내보내면 **API 키가 브라우저로 샌다.**
- **API 키는 절대 브라우저로 돌려보내지 않는다.** 설정 여부와 끝 4자리만.
- **한글 IME**: 입력칸에서 Enter만으로 확정하는 UI를 만들면 안 된다. 첫 Enter는 조합 확정에 쓰인다.
  키워드 입력이 이것 때문에 동작하지 않았다. 버튼·blur·Enter(`isComposing` 가드) 3중으로 받게 해뒀다.
- **테스트 픽스처 함정**: 증인 관련 테스트에서 기사 문구를 똑같이 만들면 근접중복 필터(0.9)가 먼저 걸러
  의도한 경로를 검증하지 못한다. 실제로 두 번 당했다. 문구를 다르게 하되 사건 유사도는 0.35 이상 유지할 것.
- `[hidden]` 속성은 `display:flex` 같은 규칙에 진다. `web/style.css` 상단의
  `[hidden] { display: none !important; }` 를 지우면 숨김이 깨진다.

---

## 8. 바로 이어서 할 만한 일 (우선순위)

1. **네이버 API HUB 키 발급 → 응답 형식 확인** (4-1). 이게 4단계 품질의 병목이다.
2. **`dry_run: false`로 실제 디스코드 전송 검증** (4-2). 500 재시도가 실전에서 먹히는지.
3. [완료] 소스별 `lookback_hours` 지원 및 엔비디아(168시간) 소스 활성화 / NewsAPI 미사용 시 끄기 (4-3).
4. 보안 하드닝 3종 (4-4).
