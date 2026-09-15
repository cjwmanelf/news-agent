# 뉴스레터 에이전트

LangGraph 기반 5단계 뉴스 파이프라인. **수집 → 선별 → 취재 → 검수 → 발행**.

일반적인 "RSS 요약 봇"과 다른 점은 4단계다.
기사 하나만 보고 LLM에게 "이거 사실이야?"라고 물으면 검증이 아니라 추측을 받는다.
그래서 같은 사건을 다룬 **다른 매체 기사를 먼저 찾고**, 그 기사들을 근거로 주장별 판정을 받는다.
발행되는 모든 항목에 신뢰도 등급과 근거 출처가 붙는다.

설계 근거와 실측 데이터는 [PRD.md](PRD.md)에 있다.

---

## 빠른 시작

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

### GUI로 쓰기 (권장)

```bash
.venv/Scripts/python.exe app.py
```

브라우저가 열리면서 <http://127.0.0.1:8765> 에 콘솔이 뜬다. 여기서 전부 된다.

- **실행** — 버튼 하나로 전 구간 실행. 단계별 진행과 로그가 실시간으로 보이고,
  결과는 신뢰도 배지가 달린 카드로 나온다. 발행 전에 멈춰 확인하는 모드도 있다.
- **설정** — 관심사 키워드, 소스 on/off, 임계값, **API 키**, 발행 채널을 화면에서 바꾼다.
  저장하면 `config/*.yaml`과 `.env`에 바로 반영되고 파일의 주석은 그대로 남는다.
- **이력** — 지난 실행 기록을 열어본다.

서버는 `127.0.0.1`에만 바인딩된다. 로컬 파일과 `.env` 비밀값을 다루므로 외부에 노출하면 안 된다.

### 터미널로 쓰기

```bash
.venv/Scripts/python.exe run.py --check
```

`--check`는 설정만 검증하고 끝난다. 문제가 없으면 그대로 돌린다.

```bash
.venv/Scripts/python.exe run.py
```

기본값은 `llm.provider=mock`, `publish.dry_run=true`라 **API 키도 웹후크도 없이** 전 구간이 돈다.
결과는 콘솔과 `output/<run_id>.md`에 남는다.

---

## 5단계

| 단계 | 하는 일 | LLM |
|---|---|---|
| 1. 수집 `collect` | RSS/Atom · JSON API · 크롤링에서 기사 후보 수집, 중복 제거 | — |
| 2. 선별 `curate` | 관심사 키워드(IDF 가중) + TF-IDF 벡터 유사도로 점수화, 상위 N건 | — |
| 3. 취재 `research` | 본문 추출 후 구조화 요약 + 검증 대상 `key_facts` 생성 | ✔ |
| 4. 검수 `verify` | 같은 사건 기사를 찾아 본문까지 읽고, 독립 출처 수와 주장별 판정으로 신뢰도 산출 | ✔ |
| 5. 발행 `publish` | Discord 웹후크로 embed 전송, 마크다운·JSON 아카이브 | — |

선별 결과가 0건이면 LLM을 부르기 전에 조기 종료한다.

### 4단계가 실제로 하는 일

1. 기사 제목으로 뉴스 검색을 걸어 **같은 사건을 다룬 다른 매체 기사**를 찾는다
2. 단어 단위 TF-IDF로 "같은 사건"인지 판정하고, 시간대가 맞지 않는 보도는 버린다
3. 상위 증인의 **원문 본문을 실제로 읽어온다** (제목만으로는 수치를 검증할 수 없다)
4. 통신사 전재를 걸러 진짜 독립 출처만 센다 — 연합뉴스 기사를 세 매체가 받아썼으면 1곳이다
5. 그 기사들을 근거로 LLM이 주장별 `supported / contradicted / unverified` 판정

### 신뢰도 등급

| 등급 | 조건 | 표시 |
|---|---|---|
| `VERIFIED` | 독립 출처 3곳 이상 + 신뢰도 0.7 이상 | ✅ 교차검증됨 |
| `LIKELY` | 독립 출처 2곳 이상 + 신뢰도 0.5 이상 | 🔵 사실로 추정 |
| `SINGLE_SOURCE` | 그 외 | ⚪ 단일 출처 |
| `DISPUTED` | 다른 매체와 내용이 상충 | ⚠️ 내용 상충 |

```
independence = min(독립_출처_수 / 3, 1.0)     # 출처 = 도메인, 단 통신사 전재는 통신사 단위
agreement    = supported / 전체_주장_수
confidence   = 0.55 × independence + 0.45 × agreement − (0.4 if 상충 else 0)
```

`DISPUTED`는 숨기지 않고 경고 배지를 달아 발행한다. 매체 간 내용이 엇갈린다는 것 자체가 정보다.

---

## 설정

| 파일 | 내용 |
|---|---|
| `config/config.yaml` | LLM 프로바이더, 동시성, 검수 임계값, 발행 옵션 |
| `config/sources.yaml` | 수집 소스 목록 |
| `config/interests.yaml` | 관심사·키워드·선별 임계값 |
| `.env` | API 키, 디스코드 웹후크, 네이버 검색 키 (`.env.example` 참고) |

### 소스 추가하기

**GUI 설정 탭에서 직접 추가·삭제할 수 있다.** `+ 소스 추가`로 새 카드를 만들고,
종류(RSS/API/크롤링)를 고르면 그에 맞는 입력칸이 나타난다. 각 카드의 `편집`으로 펼치고
`삭제`로 없앤다. 저장을 눌러야 파일에 반영된다.

> API 키는 소스 헤더에 직접 적지 말고 `${환경변수명}` 으로 적은 뒤
> 실제 값은 `API 키 · 웹후크` 에 넣는다. 그래야 설정 파일에 키가 남지 않는다.
> GUI 도 이 자리표시자를 그대로 보여주고 그대로 저장한다 — 실제 키 값은 브라우저로 가지 않는다.

아래는 같은 내용을 yaml 로 직접 쓸 때의 형식이다.

**RSS / Atom** — 둘 다 같은 방식으로 처리된다.

```yaml
- name: AI타임스
  type: rss
  enabled: true
  url: "https://www.aitimes.com/rss/allArticle.xml"
  max_items: 40
```

**JSON API** — 응답 구조를 매핑으로 알려주면 어떤 API든 붙는다.

```yaml
- name: NewsAPI
  type: api
  enabled: true
  url: "https://newsapi.org/v2/top-headlines"
  params: {country: kr, pageSize: 50}
  headers: {X-Api-Key: "${NEWSAPI_KEY}"}   # ${VAR} 는 .env 에서 치환
  items_path: articles                      # 기사 배열 위치 (점 표기 지원)
  field_map:
    title: title
    url: url
    published_at: publishedAt
    snippet: description
    source: source.name
```

**크롤링** — RSS도 API도 없는 사이트용. `robots.txt`를 확인하고 막혀 있으면 건너뛴다.

```yaml
- name: Hacker News
  type: crawl
  enabled: true
  url: "https://news.ycombinator.com/"
  list_selector: "span.titleline > a"
  link_attr: href
  base_url: "https://news.ycombinator.com/"
```

#### `role` — 뉴스레터에 실을 소스 vs 검증에만 쓸 소스

```yaml
role: corroboration    # 기본값은 content
```

구글뉴스 같은 **애그리게이터는 `corroboration`으로 둬야 한다.**
링크가 암호화 리다이렉트라 원문 본문을 가져올 수 없어 요약이 제목 재탕이 된다.
대신 한 사건을 다룬 매체를 폭넓게 끌어오므로 4단계 증인으로는 가장 값지다.

### 관심사 조정

```yaml
mode: hybrid        # keyword | vector | hybrid
threshold: 0.40     # 이 점수 이상만 통과
max_articles: 8     # 통과분 중 상위 N건만 취재

interests:
  - name: AI 인프라·반도체
    weight: 1.0
    keywords: [GPU, NPU, HBM, 반도체, 데이터센터, 엔비디아]
    exclude: [주가 전망, 매수 추천]    # 있으면 점수와 무관하게 탈락
```

GUI 설정 탭에서 **관심사를 새로 만들거나 삭제**할 수 있고, 이름·가중치·키워드를 바로 고칠 수 있다.
`+ 관심사 추가`로 새 항목을 만들고, 각 카드의 `삭제`로 없앤다. 저장을 눌러야 파일에 반영된다.
관심사를 전부 지우면 파이프라인이 돌지 않으므로 최소 하나는 남겨야 저장된다.

**키워드 추가는 세 가지 다 된다** — 입력 후 `추가` 버튼, Enter, 또는 다른 곳 클릭.
쉼표로 구분해 `GPU, NPU, HBM` 처럼 한 번에 여러 개를 넣을 수도 있다.
칩의 `×` 로 지운다. (한글 입력 중의 Enter 는 글자 확정에 쓰이므로 가로채지 않는다.)

기사가 너무 적게/많이 걸리면 `threshold`를 먼저 움직인다.
`--verbose`로 돌리면 기사별 `kw`/`vec` 점수가 보여 어느 쪽을 조일지 판단할 수 있다.

**키워드는 구체적일수록 좋다.** `AI` 같은 범용어는 IDF 가중 때문에 점수 기여가 자동으로 낮아진다.
`HBM`, `TSMC`, `AI 기본법`처럼 특정적인 단어가 선별을 좌우한다.

---

## 교차검증 검색 백엔드 (중요)

4단계의 품질은 **증인 기사의 본문을 읽을 수 있느냐**로 갈린다.

| 백엔드 | 키 | 증인 본문 | 결과 |
|---|---|---|---|
| `google_news` | 불필요 | ❌ 못 가져옴 | 제목 대조까지만 가능 |
| `naver_hub` | 무료 발급 | ✅ 원문 URL + 발췌 | 수치·세부 주장까지 검증 |
| `naver_legacy` | 기존 키 | ✅ 동일 | 2027-06-30 까지만 지원 |

구글뉴스 RSS는 링크가 암호화 리다이렉트라 원문을 받아올 수 없다.
실측하면 증인 20건이 전부 `content=0자`, `snippet=41자`(제목+매체명)다.
즉 LLM이 헤드라인만 보고 팩트체크하게 되고, `key_facts`의 수치는 영원히 `unverified`로 남는다.

네이버 검색 API는 `originallink`(원문 URL)와 실제 본문 발췌를 준다.

### 키 발급 — 개발자센터가 아니라 API HUB

**2026년 7월 31일부로 검색 API 신규 신청이 NAVER API HUB로 이관됐다.**
네이버 개발자센터(`developers.naver.com`)의 애플리케이션 등록 화면에는
`사용 API` 드롭다운에 **검색 항목이 더 이상 없다.** 거기서 찾지 말 것.

1. [네이버클라우드 플랫폼](https://www.ncloud.com) 콘솔에 로그인
2. **NAVER API HUB** 에서 검색(뉴스) API 이용 신청
3. 발급된 **Key ID / Key** 를 GUI 설정 탭의 `API 키 · 웹후크 → 교차검증 검색 (신규)` 에 입력

`.env`에 직접 넣으려면:

```bash
NAVER_API_KEY_ID=...
NAVER_API_KEY=...
```

개발자센터에서 **예전에 받아둔 키가 이미 있다면** 그것도 그대로 쓸 수 있다
(`NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET`). 다만 2027-06-30 까지만 지원되므로
설정 화면에 이전 권고 경고가 뜬다.

`provider: auto`(기본값)면 있는 키에 맞춰 자동으로 고른다 — HUB 키 > 구 키 > 구글뉴스 순.
쇼핑·책·전문자료 검색은 종료됐지만 **뉴스 검색은 계속 지원된다.**

---

## 중복 발행 방지

발행에 성공한 기사와 사건을 `state/published.db`에 기록해 두 곳에서 거른다.

| 시점 | 기준 |
|---|---|
| 2단계 선별 | 기사 ID 완전 일치 → 후보에서 제외 |
| 4단계 검수 | 지난 발행분과의 사건 유사도 → 후속·중복 보도 제외 |

```yaml
history:
  enabled: true
  lookback_days: 7          # 이 기간 안에 나간 기사·사건은 다시 내보내지 않는다
  record_on_dry_run: false  # dry_run 이 이력을 남기면 진짜 발행 때 빈 뉴스레터가 나간다
```

`--no-history`로 한 번만 끌 수 있다.

---

## LLM 연결하기

기본은 `mock`이다. 규칙 기반이라 API 키 없이 돌아가지만 의미 판단은 못 한다.
실제 요약·검수 품질을 보려면 키를 넣는다.

| 백엔드 | 필요한 키 | 발급처 |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | console.anthropic.com |
| `openai` | `OPENAI_API_KEY` | platform.openai.com/api-keys |
| `gemini` | `GOOGLE_API_KEY` | aistudio.google.com/apikey (무료 등급 있음) |
| `local` | 보통 불필요 | Ollama · LM Studio · vLLM 등 |
| `mock` | 없음 | — |

**키는 GUI 설정 탭의 'API 키 · 웹후크' 에서 넣는 게 가장 쉽다.** `.env`에 저장되고
브라우저로 되돌려 보내지 않으며, 저장 즉시 반영되므로 서버를 다시 띄울 필요가 없다.
직접 편집하려면 `.env.example`을 `.env`로 복사해 채운다.

### 로컬 LLM

OpenAI 호환 엔드포인트를 부른다. Ollama·LM Studio·vLLM·llama.cpp가 모두 이 규격을 내주므로
백엔드 하나로 전부 커버된다. 주소만 맞춰주면 된다.

```yaml
llm:
  provider: local
  model:
    local: llama3.1
  base_url:
    local: http://localhost:11434/v1   # Ollama. LM Studio 는 :1234/v1, vLLM 은 :8000/v1
```

인증이 없는 서버라면 키는 비워둔다. 필요하면 `LOCAL_LLM_API_KEY`를 쓴다.

한 번만 다른 백엔드로 시험하려면 `--provider`로 덮어쓴다 (GUI에서는 실행 탭의 LLM 선택).

```bash
.venv/Scripts/python.exe run.py --provider gemini
```

키가 없는데 해당 프로바이더를 지정하면 **실행 전에** 명확한 메시지로 멈춘다.

LLM 호출 횟수는 1회 실행당 `선별 건수(요약) + 증인이 있는 건수(검수)`다. 기본 설정에서 최대 16회.
실행 요약에 호출 수와 입출력 토큰이 찍힌다.

---

## 발행 채널

디스코드·슬랙·텔레그램을 지원한다. **여러 곳에 동시에 보낼 수 있다.**

```yaml
publish:
  targets: [discord, slack]   # 원하는 만큼
```

GUI 설정 탭의 **발행** 카드에서 체크하면 된다. 채널마다 준비 여부(키가 있는지)가 함께 표시된다.

| 채널 | 필요한 값 | 만드는 법 |
|---|---|---|
| 디스코드 | `DISCORD_WEBHOOK_URL` | 채널 편집 → 연동 → 웹후크 → 새 웹후크 → URL 복사 |
| 슬랙 | `SLACK_WEBHOOK_URL` | Slack 앱 → Incoming Webhooks 에서 발급 |
| 텔레그램 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather 로 봇 생성 → 봇과 대화 시작 → `api.telegram.org/bot<토큰>/getUpdates` 에서 chat id 확인 |

값은 GUI 설정 탭의 'API 키 · 웹후크' 에서 넣는다. 셋 다 봇 상주 프로세스가 필요 없다.

실제로 보내려면 연습 실행을 끈다.

```bash
.venv/Scripts/python.exe run.py --send
```

기사마다 카드 하나가 나가고 색상·이모지가 신뢰도 등급을 나타낸다(초록/파랑/회색/빨강).
채널별 길이 제한(디스코드 embed 10개·6,000자, 슬랙 블록 50개, 텔레그램 4,096자)을 넘으면
자동으로 나눠 보내고, rate limit은 각 API가 알려주는 대기 시간만큼 기다렸다 재시도한다.

**채널 하나가 실패해도 나머지는 계속 보낸다.** 한 곳이라도 성공해야 발행 이력에 기록된다.

---

## 실행 옵션

```bash
run.py --check                  # 설정 검증만
run.py --stage collect          # 1단계까지만 (curate/research/verify/publish 도 가능)
run.py --send                   # dry_run 무시하고 실제 발행
run.py --dry-run                # 강제로 dry_run
run.py --approve --send         # 발행 직전에 멈춰 사람 확인을 받는다
run.py --resume 20260914_130226 # 중단된 실행을 이어서 재개
run.py --provider mock          # LLM 백엔드 임시 지정
run.py --max-articles 3         # 선별 상한 임시 지정
run.py --no-history             # 발행 이력 확인·기록을 끈다
run.py -v                       # 상세 로그
```

단계별 실행은 그래프 대신 노드를 직접 순차 호출한다. 튜닝할 때 유용하다.

### 승인 후 발행

```bash
.venv/Scripts/python.exe run.py --approve --send
```

`publish` 직전에 멈추고 발행 목록을 보여준다. `n`을 누르면 실행 상태가 체크포인트에 남고,
나중에 `--resume <run_id>`로 이어서 발행할 수 있다. **재개할 때 수집·요약은 다시 돌지 않는다** —
LLM 호출 0회로 발행 단계만 실행된다.

### 매일 자동 실행 (Windows 작업 스케줄러)

```powershell
schtasks /create /tn "NewsAgent" /tr "'C:\경로\NEWS AGENT\.venv\Scripts\python.exe' 'C:\경로\NEWS AGENT\run.py' --send" /sc daily /st 08:00
```

---

## 출력물

```
output/
  20260914_125710.md      # 사람이 읽는 뉴스레터 (사실 확인 내역 접힘 포함)
  20260914_125710.json    # 전체 실행 상태 — 통계, 선별 점수, 판정 근거
state/
  published.db            # 발행 이력 (중복 방지)
  checkpoints.db          # LangGraph 체크포인트 (재개용)
```

`state/`는 자동 생성된다. 통째로 지우면 이력과 재개 지점이 사라질 뿐 동작에는 문제없다.

JSON에는 각 기사의 선별 점수와 사실별 판정이 그대로 들어 있다. 임계값을 조정할 때 근거 자료가 된다.

---

## 테스트

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

네트워크를 타지 않는 순수 로직만 검증한다 — 점수 공식, 증인 선별 규칙(도메인·전재 필터),
디스코드 embed 제한과 재시도, URL/시각 정규화, 사건 유사도의 변별력.

실제 통신은 `run.py --stage collect`로 확인한다.

---

## 구조

```
src/newsagent/
  graph.py        LangGraph 조립 (체크포인터·재시도·승인 인터럽트)
  state.py        노드 간 상태
  models.py       Article / Brief / VerifiedBrief
  config.py       설정 로딩·검증
  llm.py          anthropic / openai / mock 어댑터 (토큰 집계 포함)
  prompts.py      요약·팩트체크 프롬프트
  search.py       교차검증용 기사 검색 (naver / google_news)
  store.py        발행 이력 (SQLite)
  secrets.py      .env 읽기·쓰기 (GUI 키 입력용)
  runner.py       단계 실행 (CLI·GUI 공용)
  collectors/     rss · api · crawl
  nodes/          collect · curate · research · verify · publish
  publishers/     discord · slack · telegram
  utils/          text(정규화·유사도) · http(robots) · extract(본문)

app.py            로컬 GUI 서버 (Flask)
web/              GUI 프런트엔드 (index.html · style.css · app.js)
```

새 발행처를 붙이려면 `publishers/base.py`의 `Publisher`를 구현하고
`publishers/__init__.py`의 레지스트리에 등록하면 된다.

---

## 알아둘 것

- **저작권** — 원문을 전재하지 않는다. 요약과 원문 링크만 발행한다.
- **예의** — User-Agent를 명시하고 `robots.txt`를 따른다. 크롤 소스를 늘릴 때는 대상 사이트 부담을 고려한다.
- **검증의 한계** — "여러 매체가 같은 말을 한다"는 것이 "사실"과 같지는 않다.
  통신사 전재는 바이라인으로 걸러내지만, 바이라인 없이 받아쓴 기사나 모두가 같은 오보를
  인용하는 상황까지 막지는 못한다. 등급은 참고 지표이지 보증이 아니다.
- **재시도는 `collect`에만 걸려 있다.** `publish`는 절대 재시도하지 않는다 — 전송 도중 끊기면
  같은 글을 두 번 올릴 수 있기 때문이다. `research`/`verify`는 노드 안에서 기사별 실패를
  이미 격리하므로, 노드째 재시도하면 이미 성공한 기사까지 LLM을 다시 부르게 된다.
- **단독 보도가 `SINGLE_SOURCE`로 나오는 건 정상이다.** 낮은 등급이 곧 거짓이라는 뜻은 아니다.
