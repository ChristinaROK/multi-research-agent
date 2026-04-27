# multi-research-agent 구축 — 사전 팁/피드백 핸드오버

> **출처**: multi-agent-test 1차 PoC (2026-04-27, 사업계획서 자동작성 v1~v5)
> **대상 프로젝트**: SK 기업가치 팩터 분석 자율 멀티에이전트 시스템
> **목적**: 우리가 직접 부딪혀 깨진 부분 + 검증된 패턴을 새 프로젝트에 그대로 이식 가능하게 정리

---

## 0. TL;DR — 5줄 요약

1. **트리거 메커니즘 바꿔라**: fswatch 대신 SQLite polling. 이유는 §3-1.
2. **데몬 언어 바꿔라**: bash 대신 Python (당신 설계 그대로). bash 데몬은 silent failure 너무 많음. 우리가 다 겪음.
3. **`--dangerously-skip-permissions` 그대로 두되 Docker 격리 필수**: 우리 프로젝트는 텍스트 파일만 썼지만 새 프로젝트는 임의 bash + 웹 로그인 + 다운로드. blast radius가 1000배 다름.
4. **정지 조건 명시적으로 설계**: 우리는 `MAX_ITER=5`로 안전했지만 새 프로젝트는 "infinite loop"가 디폴트. cost/time/diminishing-returns 3중 안전장치 필수.
5. **Monitor 에이전트는 그대로 가져가되 강화**: 우리가 만든 헤드라인 diff + macOS 알림 패턴은 그대로 OK. 단 채널은 Telegram(밤새 무인이니까), 정보량은 우리보다 훨씬 풍부해야 함 (가설 N개 / 수집 M개 / 발견 K개 그래프 형태).

---

## 1. 프로젝트 폴더 세팅 — 일반 템플릿

멀티에이전트 프로젝트의 폴더 구조는 작업 첫날 결정 → 이후 6개월 내내 영향. multi-agent-test PoC와 multi-research-agent 설계를 함께 본 결과, 어떤 멀티에이전트 프로젝트에도 적용 가능한 일반 템플릿:

### 1-1. 표준 디렉토리 구조

```
project-root/
├── README.md                     ← 사람을 위한 입구. 5줄 안에 "이게 뭔지 + 어떻게 시작하나"
├── CLAUDE.md                     ← 시스템 전체 개요 (Claude가 cwd부터 walk하며 자동 로드)
├── .env.example                  ← 시크릿 템플릿 (실제 값 없음, 키 이름만)
├── .gitignore
├── pyproject.toml / package.json ← 의존성 (단일 도구로 통일)
│
├── docs/                         ← 사람이 읽는 문서 (디자인 결정·회의록·핸드오버)
│   ├── architecture.md
│   ├── decisions/                ← ADR (Architecture Decision Record)
│   └── runbooks/                 ← 운영 매뉴얼 (일일 점검, 장애 대응)
│
├── source/                       ← 가공 안 된 원본 자료 (PDF/DOCX/이미지)
│   └── 절대 수정·삭제 금지. references/는 source/에서 추출·정제한 것
│
├── references/                   ← 에이전트가 입력으로 읽는 자료 (정적, 사람이 미리 정제)
│   ├── 01_도메인_지식.md         ← 숫자 prefix로 읽기 순서 명시
│   ├── 02_요구사항.md
│   └── 03_전략_플레이북.md       ← Writer/Hypothesis 같은 "원천" 에이전트의 핵심 가이드
│
├── agents/                       ← 에이전트 정의 (정적)
│   └── {agent-name}/
│       ├── CLAUDE.md             ← 페르소나·트리거·절차·산출물 형식
│       ├── prompts/              ← 추가 프롬프트 단편 (옵션)
│       └── examples/             ← few-shot 예시 (옵션)
│
├── workspace/                    ← 런타임 산출물 (동적, 통째로 삭제·재생성 가능)
│   ├── inputs/                   ← 사이클 시작 시 외부에서 받은 입력 (옵션)
│   ├── outputs/                  ← 정식 산출물 (사용자에게 보여줄 것)
│   ├── intermediate/             ← 중간 데이터셋 (다음 에이전트 입력용)
│   ├── logs/                     ← 에이전트별 실행 로그
│   └── STATUS.md                 ← Monitor 에이전트가 덮어쓰는 현재 상태
│
├── scripts/                      ← 실행 스크립트
│   ├── lib/                      ← 공통 함수 (log, lock, run_agent)
│   ├── start-all.sh / stop-all.sh
│   ├── run-{agent}.sh
│   └── status.sh / bootstrap.sh
│
├── configs/                      ← YAML/JSON 설정 (env에 안 넣을 것)
│   ├── agent_config.yaml         ← 에이전트별 도구 권한, 타임아웃, max_turns
│   └── prod.yaml / dev.yaml
│
├── tests/                        ← 자동 테스트 (옵션이지만 권장)
│   ├── dry_run/                  ← 비용 0 통합 시나리오 테스트
│   └── unit/
│
└── .claude/                      ← Claude Code 프로젝트 설정 (선택)
    ├── settings.json             ← MCP 서버 등록, 권한 정책
    └── skills/                   ← 프로젝트 전용 skill
```

### 1-2. 분리 원칙 — 왜 이렇게 나누는가

**3단 분리** (가장 중요):
- **입력** (`source/`, `references/`, `configs/`): 사람이 만든 정적 자료. 에이전트는 읽기만.
- **도구·정의** (`agents/`, `scripts/`, `.claude/`): 시스템의 정적 행동 정의. git으로 추적.
- **산출물** (`workspace/`): 런타임 동적 결과. 언제든 통째로 삭제·재생성 가능해야 함.

이 분리가 무너지면 발생하는 사고:
- 산출물이 references/에 섞이면 → 다음 사이클이 자기 출력을 입력으로 또 읽음 (오염 루프)
- 에이전트 정의가 workspace/에 들어가면 → git 추적 안 되어 나중에 "왜 그렇게 동작했지?" 답 못함
- 시크릿이 configs/에 들어가면 → git에 커밋되어 유출

**추가 분리**:
- **사람용 문서 vs 에이전트용 문서**: `docs/` (사람) ↔ `references/` (에이전트). 에이전트가 docs를 읽으면 컨텍스트 폭발 — 사람용 문서는 길어지기 쉬움.
- **원본 vs 가공본**: `source/` (원본 PDF) ↔ `references/` (정제 markdown). 우리 multi-agent-test는 `docx/` + `references/` 로 했음, 동일 패턴.
- **정식 산출물 vs 디버그 로그**: `workspace/outputs/` ↔ `workspace/logs/`. 사용자에게 보여줄 것 vs 디버깅용. 우리는 처음에 안 나누고 logs/에 모든 걸 넣었다가 "성공한 산출물이 어디 있지?" 헷갈림.

### 1-3. 명명 규칙

- **버전화 산출물**: 선형이면 `v{N}.md` (v1, v2…). 병렬이면 `{type}-{id}.md` (예: H-001, A-042).
  - 자릿수 패딩(`v01` vs `v1`) 트레이드오프: 파일 정렬엔 패딩 유리, 정수 비교엔 불리. 우리는 무패딩으로 했음.
- **임시 파일**: 점(.) prefix (`.{name}.tmp`). 우리는 fswatch 정규식 매칭이 자동 무시하도록 활용.
- **시그널 파일**: `{agent}_done`, `{agent}_failed`. JSON 본문에 메타데이터 (`{tasks_completed: 3, next: "..."}`) .
- **로그 파일**: `{agent}.log` (전체 누적) + `{agent}-{YYYYMMDD-HHMM}.log` (사이클별, 옵션).
- **스크립트**: `run-{agent}.sh`, `start-all.sh`, `stop-all.sh`, `status.sh`. **동사로 시작**.
- **참고자료**: `01_*.md`, `02_*.md` 숫자 prefix로 읽기 순서 명시 — Claude도 사람도 동일 순서로 읽음.

### 1-4. CLAUDE.md 계층 전략

Claude Code는 cwd부터 위로 walk하며 모든 CLAUDE.md를 자동 로드. 이를 활용한 **3계층 모델**:

```
~/.claude/CLAUDE.md                ← 사용자 글로벌 (모든 프로젝트 공통, 사용자 프로필)
project-root/CLAUDE.md             ← 시스템 전체 개요 (모든 에이전트가 봄)
project-root/agents/{X}/CLAUDE.md  ← 해당 에이전트 페르소나 (X 호출 시 cwd가 그 디렉토리이므로 추가 로드)
```

각 계층의 역할:
- **글로벌**: 사용자 프로필, 일반 작업 스타일 (이번 프로젝트와 무관)
- **루트**: 다른 에이전트의 존재, 협업 규칙, 채널, 정지 조건. **각 에이전트가 자기 위치를 자동 인식**하도록 함.
- **에이전트별**: 그 에이전트만의 페르소나·트리거·절차·산출물 형식.

**길이 가이드**: 각 CLAUDE.md는 5~15KB가 적정. 이보다 길면 매 호출마다 비용↑·정확도↓. 길어지면 references/로 빼고 "필요할 때 Read하라" 패턴으로 분리.

**호출 시 cd 패턴**:
```bash
# Agent 호출 시 해당 agent 디렉토리로 이동 → 자동으로 두 CLAUDE.md 로드
cd "$AGENTS/$agent_name"
printf '%s' "$prompt" | claude --print --model "$model" \
  --dangerously-skip-permissions \
  --add-dir "$WORKSPACE" "$REFERENCES" "$PROJECT_ROOT"
```

### 1-5. .gitignore 표준 (멀티에이전트 프로젝트)

```gitignore
# 시크릿 — 절대 커밋 금지
.env
.env.local
*.key
*.pem
credentials.json

# 런타임 산출물 — 재생성 가능
workspace/outputs/
workspace/intermediate/
workspace/logs/
workspace/downloads/
workspace/STATUS.md
*.lock
*.lock.d

# DB — 환경마다 다름 (스키마는 schemas/ 또는 마이그레이션으로 별도 관리)
*.db
*.sqlite
*.sqlite-wal
*.sqlite-shm

# OS / 에디터
.DS_Store
.vscode/
.idea/

# Python / Node
__pycache__/
*.pyc
node_modules/
.venv/
venv/

# Claude Code 캐시
.claude/sessions/
.claude/cache/
```

**예외**: `.gitkeep`으로 빈 디렉토리는 유지 (`workspace/logs/.gitkeep`처럼) — 신규 클론 시 디렉토리가 없어 스크립트가 깨지는 것 방지.

### 1-6. .env / 시크릿 관리

원칙:
1. **`.env`는 절대 커밋 금지**. 이름만 적힌 `.env.example`만 커밋.
2. **에이전트가 `.env`를 직접 못 읽게** — 환경변수로 주입 후 코드에서 `os.environ.get(...)` 으로만 접근. `cat .env` 같은 시도 자체가 sandbox에서 차단되도록.
3. **시크릿별 권한 분리** — 데이터 수집 에이전트만 외부 API 키 필요, 분석 에이전트는 불필요. 가능하면 에이전트별 env 분리하거나 ENV에서 명시적으로 unset.
4. **로테이션 가능하게** — 키 발급/폐기 절차를 `docs/runbooks/`에 명시.

`.env.example` 표준 양식:
```bash
# Claude API
ANTHROPIC_API_KEY=

# 외부 API (필요한 에이전트만)
# DART_API_KEY=
# KOSIS_API_KEY=

# 알림 채널
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_INFO=
TELEGRAM_CHAT_WARN=
TELEGRAM_CHAT_URGENT=

# 운영 한도 (cost circuit breaker)
MAX_COST_USD_PER_HOUR=10
MAX_COST_USD_PER_DAY=50
MAX_RUN_HOURS=8
```

### 1-7. 만드는 순서 — 실전 체크리스트

작업 첫날 ~30분이면 골격 완성. **에이전트는 한 번에 하나씩만** 추가하는 게 핵심.

```bash
# 1. 디렉토리 골격 (단일 명령)
mkdir -p {docs,references,source,agents,workspace/{inputs,outputs,intermediate,logs},scripts/lib,configs,tests/dry_run}
touch workspace/{outputs,intermediate,logs}/.gitkeep

# 2. 파일 골격
touch README.md CLAUDE.md .env.example .gitignore

# 3. .gitignore 채우기 (위 §1-5 복붙)

# 4. 의존성 매니저 초기화
# Python: poetry init  /  uv init  /  pip-tools
# Node:   npm init -y

# 5. README.md 5줄: "이게 뭔지 + 어떻게 시작하나" — 더 길면 docs/로

# 6. CLAUDE.md 골격 (시스템 개요만, 에이전트 정의 전):
#    - 1. 목적과 사용자
#    - 2. 런타임 (어떤 모델·SDK·트리거)
#    - 3. 호스팅 / 배포
#    - 4. 인터페이스 (CLI/Slack/Telegram)
#    - 5. 비밀값 위치
#    - 6. (작성 후) 시스템 아키텍처 다이어그램
#    - 7. 협업 규칙 (충돌·정지 조건·비용 한도)

# 7. 첫 에이전트 1개부터 — 절대 4개 동시에 X
mkdir -p agents/{first-agent-name}
touch agents/{first-agent-name}/CLAUDE.md
# 그 에이전트 CLAUDE.md를 채움 (페르소나, 트리거, 산출물)

# 8. 단일 에이전트 dry-run (claude -p 가 cwd의 CLAUDE.md를 잘 읽는지 확인)
cd agents/{first-agent-name}
printf 'test 호출' | claude --print --dangerously-skip-permissions

# 9. 잘 동작하면 두 번째 에이전트 추가 → 두 에이전트 간 통신 테스트
#    → 점진 확장 (2개 안정 → 3개 → 4개)

# 10. references/ 채우기 (에이전트가 입력으로 읽을 자료부터)

# 11. scripts/lib/common.sh 작성 (log, lock, run_agent 공통 함수)

# 12. 단일 에이전트 데몬화 → 통합 트리거 (DB polling 또는 fswatch) → start-all.sh

# 13. Monitor 에이전트 추가 (관찰 가능성)
```

### 1-8. 우리가 깨진 부분 (피하세요)

multi-agent-test에서 직접 겪은 폴더 구조 관련 실수:
- ❌ 에이전트 4개를 동시에 정의 → 디버깅 시 어떤 에이전트 문제인지 식별 어려움. **하나씩** 추가 권장.
- ❌ `references/` 와 `source/` 를 안 나누고 `docx/` 안에 모두 둠 → 어떤 게 원본이고 어떤 게 가공본인지 헷갈림. 다음 프로젝트에선 분리.
- ❌ `workspace/` 와 `outputs/` 를 안 나눔 → 사용자 정식 산출물이 디버그 로그와 섞임. 분리 필수.
- ❌ `logs/`에 `.gitkeep` 안 둠 → 신규 클론 시 디렉토리 부재로 데몬 시작 실패. 모든 빈 워크스페이스 디렉토리에 `.gitkeep`.
- ❌ `CLAUDE.md`에 작성 전략·체크리스트·예시까지 다 넣어 매우 길어짐 → 비용↑. 길어지는 부분은 `references/04_작성전략.md` 처럼 외부 파일로 빼고 CLAUDE.md에선 "이걸 읽어라"만 명시.

### 1-9. CLAUDE.md 루트 템플릿 (복붙용)

```markdown
# {project-name} — {1줄 설명}

> {3줄 시스템 개요 — 무엇을, 누구를 위해, 어떻게}

## 1. 목적과 사용자
- 목적: ...
- 사용자: ...

## 2. 런타임
- 모델: {Opus / Sonnet / Haiku 분담 — 어느 에이전트에 어떤 모델}
- 실행 방식: Claude Code CLI 헤드리스 (`claude --print`) / SDK / API
- 트리거: {fswatch / SQLite polling / cron / 수동}
- 상태 채널: {파일시스템 / SQLite / Redis}

## 3. 호스팅 / 배포
- 환경: {로컬 macOS / Docker / cloud}
- 보호: {systemd / pm2 / supervisord 등 데몬 자체의 죽음 보호}

## 4. 인터페이스
- {CLI / Slack / Telegram / 웹 / 데스크톱 알림}

## 5. 비밀값
- `.env` 사용. 절대 커밋 금지. `.env.example` 참조.
- 시크릿별 사용 에이전트 매핑.

## 6. 시스템 아키텍처
{ASCII 다이어그램 — 에이전트들의 역할과 통신 채널}

## 7. 디렉토리 구조 핵심
- agents/ — 에이전트 정의
- references/ — 에이전트 입력 자료
- workspace/ — 런타임 산출물
- scripts/ — 실행

## 8. 협업 규칙
- 에이전트 충돌 시 우선순위: ...
- 정지 조건: cost / time / diminishing returns / STOP 파일
- 비용 한도: ...
- 재시도 정책: ...

## 9. 이 시스템에서 작업할 때 (Claude를 위한 메타 지침)
- 프로젝트 자체를 수정할 때의 규칙 (예: workspace/ 의 기존 파일 임의 삭제 금지)
- 어떤 파일을 수정하면 어떤 영향이 있는지
- 디버깅 시 보아야 할 로그 위치
```

---

## 2. 그대로 재사용 가능한 패턴 (검증 완료)

### 1-1. mkdir-기반 atomic lock
```bash
LOCK_DIR="/tmp/aipm-mra-${task_id}.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "이미 실행 중 — 스킵"
  exit 0
fi
trap "rmdir '$LOCK_DIR'" EXIT
```
- 우리가 쓴 패턴. flock보다 단순하고 macOS/Linux 둘 다 동작.
- **단, 새 프로젝트는 lock 단위가 다름**: 우리는 per-agent (writer 1개, reviewer-strategist 1개), 새 프로젝트는 per-task (collector가 동시에 여러 hypothesis 처리해야 하니까 per-hypothesis-id).
- 변경 권고: `LOCK_DIR="/tmp/aipm-mra-${agent}-${task_id}.lock.d"` 처럼 task ID까지 포함.

### 1-2. Agent CLAUDE.md 분리 + 자동 로드
- `agents/{agent_name}/CLAUDE.md` 패턴은 매우 깔끔하게 작동했음.
- Claude Code가 cwd부터 위로 walk하며 CLAUDE.md를 자동 로드. agent 디렉토리에 cd하고 `claude -p`만 호출하면 됨.
- 새 프로젝트도 동일 구조로 — `agents/hypothesis/CLAUDE.md`, `agents/collector/CLAUDE.md` 등.
- **추가 팁**: 프로젝트 루트 CLAUDE.md에는 시스템 전체 개요 (다른 에이전트의 존재, 협업 규칙)를 넣어두면 각 에이전트가 자기 위치를 자동으로 인식.

### 1-3. Prompt를 stdin으로 넘기기 (★중요★)
```bash
# ❌ 안 됨: --add-dir 가 variadic이라 prompt를 디렉토리로 흡수
claude --print --add-dir A B C "$prompt"
# Error: Input must be provided either through stdin or as a prompt argument

# ✅ 됨
printf '%s' "$prompt" | claude --print --add-dir A B C
```
당신 설계서의 `claude -p "..." --allowedTools ...` 형태는 동작하지만, 만약 `--add-dir`나 `--allowedTools`처럼 variadic 옵션을 마지막에 두고 그 뒤에 prompt를 두면 동일 함정에 빠짐. **prompt는 stdin으로 통일**하면 옵션 순서 신경 안 써도 됨.

### 1-4. 원자적 쓰기 (`.tmp` → `mv`)
```bash
# Agent CLAUDE.md에 명시
1. 임시 파일에 먼저 작성: workspace/data/.{output}.tmp
2. 검증 통과 후 mv로 최종 경로로 이동
```
- 새 프로젝트에선 collector가 다운로드 받은 파일 처리 시 필수. 다운로드 중 fail → 부분 파일이 남아서 다음 에이전트가 corrupted CSV를 분석하는 사고 방지.
- 다운로드 받을 때도 `wget -O .file.csv.tmp && mv .file.csv.tmp file.csv` 패턴.

### 1-5. Monitor 에이전트 (Haiku, sleep loop, 헤드라인 diff)
- 우리 패턴: Haiku 4.5, 5분 sleep loop, `STATUS.md` 덮어쓰기, 헤드라인 변화 시에만 macOS 알림.
- 새 프로젝트: **그대로 가져가되 알림 채널만 Telegram으로 교체**. 밤새 무인이니 데스크톱 알림은 무용지물.
- 비용: Haiku 1회 ~$0.005. 5분 간격 8시간 = 96회 = $0.48. 무시 수준.
- 우리가 깨진 부분: monitor 에이전트의 정보 출력이 단조로워지면 사용자가 "다 같은 메시지" 라고 느낌. **상태 변화 + 다음 예상 액션 + 이상 징후** 3가지를 매번 포함하도록 CLAUDE.md에 강제.

### 1-6. log 함수 표준화
```bash
log() {
  local agent="$1"; shift
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$agent] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOGS/$agent.log"
}
```
- Python으로 옮기면 logging.basicConfig + per-agent FileHandler.
- 모든 에이전트 호출에 START/END 마커를 찍으면 사후 비용 reconciliation에 결정적.

---

## 3. 이번엔 다르게 가야 하는 것 (구조 차이)

### 2-1. 트리거: fswatch → SQLite polling
**우리 경험**: fswatch는 함정이 너무 많음.
- `--event=Created --event=Updated` 필터가 `mv` (rename) 이벤트를 못 잡음 → 디버깅 1시간 날림
- 파이프 우측 while 루프 안의 `local` + `set -e` 가 silently 죽음 → 디버깅 1시간 날림
- macOS 슬립 시 이벤트 누락

**새 프로젝트엔 부적합**: 어차피 에이전트 간 통신이 SQLite 기반인데 굳이 파일시스템 이벤트를 둘 이유 없음.

**권고**:
```python
# Supervisor 메인 루프
while True:
    pending = db.execute("SELECT * FROM hypotheses WHERE status = 'pending' LIMIT 1")
    if pending:
        run_collector(pending)
    elif db.has_collected_not_analyzed():
        run_analyzer(...)
    elif ...:
        ...
    else:
        time.sleep(POLL_INTERVAL_SEC)
```
- Polling 간격 30~60초면 충분. fswatch 함정 다 회피.
- DB가 single source of truth이므로 race condition도 트랜잭션으로 깔끔하게 해결.

### 2-2. State channel: 파일명 컨벤션 → SQLite
**우리는 `v{N}.md`, `v{N}-{role}.md` 같은 파일명 컨벤션으로 상태를 표현**. 단순한 워크플로엔 OK였지만:
- 가설 23개, 데이터셋 30개, 분석 50개로 늘어나면 파일명만으로 안 됨
- "이 가설의 파생 가설들 다 보여줘" 같은 쿼리는 SQL이 자연

**새 프로젝트는 SQLite hub.db 설계 그대로 OK**. 단:
- **WAL 모드 필수**: `PRAGMA journal_mode=WAL;` — 여러 에이전트 동시 쓰기 시 lock 회피
- **`status` 컬럼에 enum 강제**: CHECK constraint로 잘못된 상태 방지 (`pending|collecting|collected|analyzing|...`)
- **`updated_at` 트리거**: 모든 테이블에 자동 갱신
- **인덱스**: `(status, priority)` 복합 인덱스 — supervisor가 매번 폴링하니까

### 2-3. 데몬 언어: bash → Python
당신 설계서대로 Python supervisor가 정답. 우리가 bash로 짠 이유는 단순 시스템이라 가능했던 것이고, 이번엔 무리.
- bash로는 cost tracking, 비동기 task 관리, Telegram API 호출, JSON 파싱 모두 고통
- Python `subprocess.Popen` + `asyncio` + `sqlite3` + `pydantic` 조합이 자연

**우리가 bash로 깨진 부분 (Python에선 안 만남)**:
- `set -e` + 서브쉘 + `local` 조합의 silent failure
- `pkill`로 자식 프로세스 정리하다가 자식이 또 자식을 spawn하면 누락
- macOS와 Linux의 `stat` 옵션 차이 (`-f` vs `--format`)
- shell 에서 SQLite 호출하면 lock 처리 어렵고 트랜잭션 못 씀

### 2-4. 알림: macOS osascript → Telegram bot
**우리**: `osascript -e "display notification ..."` — 데스크톱 팝업.
**새 프로젝트**: 밤새 무인이라 데스크톱 알림 무용. **Telegram bot이 정답**.

```python
def alert_telegram(message, urgent=False):
    emoji = "🚨" if urgent else "📊"
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": f"{emoji} {message}", "parse_mode": "Markdown"}
    )
```
- 알림 등급 분리: `info` (사이클 완료, 새 발견) / `warn` (3회 재시도 중) / `urgent` (cost 80%, persistent failure, supervisor 죽음)
- **알림 폭탄 방지**: monitor 에이전트가 헤드라인 diff 패턴 그대로 적용. 단 cost·error는 매번 보고 (변화 없어도).

### 2-5. 권한 모델: 같은 `--dangerously-skip-permissions`이지만 blast radius 1000배
- 우리 프로젝트: 텍스트 파일만 작성. 최악의 시나리오 = workspace에 이상한 마크다운이 쌓임.
- 새 프로젝트: 임의 bash 실행 + Playwright 웹 로그인 + 파일 다운로드 + API 호출. **최악의 시나리오 = .env 읽고 외부 전송, 시스템 깨뜨리는 패키지 install, 사이트 ToS 위반**.

**§5 권한·격리 섹션 별도 분리** — 진지하게 읽을 것.

---

## 4. 새로 만나게 될 함정 (우리는 안 만났지만 예상되는 것)

### 3-1. SQLite 동시성
- WAL 모드 안 켜면 동시 쓰기 시 `database is locked` 에러
- 트랜잭션 길게 잡으면 다른 에이전트 블록
- **권고**: 모든 INSERT/UPDATE는 짧은 트랜잭션으로, SELECT는 BEGIN IMMEDIATE 회피

### 3-2. Playwright 세션·메모리 누수
- Chromium 인스턴스가 collector 호출마다 새로 뜨고 안 닫히면 메모리 폭발
- 다운로드 파일이 OS 임시 디렉토리에 누적
- **권고**:
  - Collector CLAUDE.md에 "작업 후 반드시 `browser.close()`" 명시
  - Supervisor가 매 N 사이클마다 잔여 Chromium PID kill
  - `--single-process` 옵션 + 타임아웃 강제

### 3-3. 웹사이트 rate limit / 차단
- 같은 IP에서 빠르게 많이 요청하면 403 또는 captcha
- KCGS, DART, 네이버 뉴스 등 한국 사이트는 특히 민감
- **권고**:
  - User-Agent 회전 + 요청 간 1~3초 random sleep
  - 429/403 받으면 지수 백오프 + 다음 사이클로 미루기
  - 동일 도메인 동시 요청 금지 (collector 큐에 domain lock)

### 3-4. 다운로드 디스크 폭발
- PDF/Excel을 무한정 받다 보면 며칠 만에 수 GB
- **권고**:
  - `data/downloads/` 에 TTL: 7일 지난 파일 자동 삭제
  - 다운로드 전 free disk 체크, 1GB 미만이면 alert
  - 동일 URL 재다운로드 방지 (URL → file 매핑 테이블)

### 3-5. 무한 가설 생성 루프 + 가설 폭발
- Hypothesis agent가 "더 만들어"라고 매번 호출되면 가설이 기하급수적으로 증가
- 파생 가설의 파생의 파생...
- **권고**:
  - Hypotheses 테이블에 `depth` 컬럼 추가 (root=0, 파생마다 +1), depth ≥ 3 거부
  - 한 사이클당 신규 가설 수 cap (예: 5개)
  - Pending 큐 길이 N개 도달 시 hypothesis agent 휴면 (일단 처리부터)

### 3-6. "충분"의 정의 부재 — 정지 조건
우리는 `MAX_ITER=5`로 단순했음. 새 프로젝트는 "발견할 때까지" 가 디폴트인데 **이게 정의가 안 되어 있으면 무한 진행**.

**3중 안전장치 권고**:
1. **Cost ceiling**: hourly cap (예: $10/hr) + daily cap (예: $50/day). 둘 중 먼저 도달하면 정지.
2. **Time ceiling**: supervisor 시작 후 8시간 후 자동 정지 (overnight 가정).
3. **Diminishing returns**: 최근 10개 verdict 중 8개 이상 `rejected`면 자동 정지 (탐색 공간 고갈로 판단).
4. **Stop signal file**: `/workspace/STOP` 파일 존재 시 즉시 정지 (수동 stop kill switch).

### 3-7. 비용 폭주
- collector + analyzer가 매 사이클마다 수만 토큰을 쓸 수 있음
- 특히 PDF 한 개 통째로 컨텍스트에 넣으면 단일 호출 $1+
- **권고**:
  - 호출 전 prompt + 첨부 파일 토큰 사전 추정 (`tiktoken` 또는 추정치)
  - 토큰 한도 초과 예상 시 분할 또는 거부
  - hourly burn rate가 예산 초과 페이스면 cooldown 길이를 동적으로 늘림

### 3-8. 크로스 에이전트 데드락
- Hypothesis가 "collector 끝날 때까지 대기" / collector가 "verifier가 hypothesis 보강할 때까지 대기" 식으로 의존이 꼬일 수 있음
- **권고**:
  - 모든 에이전트 task에 `assigned_at` 타임스탬프 + `MAX_TASK_AGE_HOURS=4`
  - 4시간 이상 `in_progress` 상태인 task는 supervisor가 자동으로 `failed`로 마킹
  - 의존 그래프 사이클 검증: hypothesis ← collector ← analyzer ← verifier → hypothesis 의 단방향 보장

### 3-9. 재현성 — Verifier가 실패하는 가장 흔한 이유
- Analyzer가 `/tmp/data.csv` 같은 임시 경로 쓰면 verifier 실행 시 파일이 없음
- 패키지 버전 차이로 결과 약간 다름
- **권고**:
  - 분석 스크립트는 항상 `/workspace/data/processed/` 절대경로
  - `requirements.txt` lock + verifier가 venv 새로 만들어 실행
  - Verifier가 재현 실패 감지 시 즉시 `analyses.status = 'irreproducible'` + alert

### 3-10. Provenance (유래 추적)
- Finding 1개가 나왔을 때 → 어떤 분석에서? → 어떤 데이터로? → 어떤 가설에서? → 그 가설은 어떤 피드백으로 파생?
- 우리 프로젝트는 v{N} 선형이라 자동으로 추적됐지만 새 프로젝트는 그래프
- **권고**: SQL view 하나 만들어두기
```sql
CREATE VIEW finding_provenance AS
SELECT f.finding_id, f.summary,
       v.decision, v.reasoning,
       a.method, a.script_path,
       d.file_path, d.source_url,
       h.causal_claim, h.parent_hypothesis
FROM findings f
JOIN hypotheses h ON f.hypothesis_id = h.hypothesis_id
JOIN verdicts v ON v.hypothesis_id = h.hypothesis_id
JOIN analyses a ON v.analysis_id = a.analysis_id
JOIN datasets d ON d.hypothesis_id = h.hypothesis_id;
```
SK PR팀 납품 시 "이 발견의 근거가 뭐냐"는 첫 질문에 즉답 가능.

---

## 5. 권한·격리 — 진지한 경고

이 부분만큼은 우리 경험에서 **유일하게 적용 안 되는** 영역. 우리는 텍스트 파일만 다뤘기 때문에 `--dangerously-skip-permissions`가 사실상 안전했음. 새 프로젝트는 다름.

### 4-1. 위험 시나리오 (실제 가능)
| 위험 | 시나리오 | 결과 |
|------|---------|------|
| 시크릿 유출 | 에이전트가 `cat .env` → fetch MCP로 외부 전송 | API 키 유출 |
| 시스템 오염 | `pip install --user` 무차별 실행 | 패키지 conflict, 환경 망가짐 |
| ToS 위반 | Playwright로 자동 로그인 후 대량 스크레이핑 | KCGS/DART 계정 차단, 법적 리스크 |
| 데이터 외부 전송 | 분석 결과 디버깅한다고 외부 pastebin에 업로드 | SK 내부 분석 외부 노출 |
| 무한 fork bomb | bash로 `:(){ :|:& };:` 같은 거 실행 | 시스템 다운 |

### 4-2. 격리 권고 (필수, 권장 X)
1. **Docker 컨테이너 내부에서만 실행**
   - `--read-only` 루트 파일시스템 + `tmpfs`로 쓰기 가능 영역만 명시
   - `--cap-drop=ALL` 후 필요한 capability만 add
   - `--memory=4g --cpus=2` 자원 한도
2. **네트워크 egress allowlist**
   - iptables 또는 docker network로 허용 도메인만 outbound 허용
   - 허용 목록: `api.anthropic.com`, `dart.fss.or.kr`, `kcgs.or.kr`, `pykrx 의존 도메인`, Telegram API
   - 그 외 모두 차단 → 시크릿 유출 시도 자체가 fail
3. **`.env` 분리**
   - 컨테이너에 `.env` 마운트하지 말 것
   - 환경변수로 직접 주입 (`docker run -e ANTHROPIC_API_KEY=...`)
   - 컨테이너 내부에서 `cat /proc/1/environ`도 막으려면 entrypoint에서 unset
4. **Bash 명령 allowlist** (Anthropic SDK의 `--allowedTools` 활용)
   - Collector에 bash 전체 허용은 위험
   - `Bash(python3 *), Bash(pip install pykrx), Bash(pip install pandas), Bash(curl https://dart.*)` 식으로 화이트리스트
   - 또는 사전 정의된 헬퍼 스크립트만 호출하게 강제

### 4-3. 감사 로그 (audit trail)
- 모든 bash 실행, 웹 요청, 파일 다운로드를 별도 audit log에 기록
- 의심스러운 패턴(외부 도메인 요청, .env 접근, sudo 시도) 발견 시 즉시 supervisor가 kill + alert
- Telegram에 "Collector가 외부 도메인 X로 요청 시도 → 차단함" 같은 메시지

---

## 6. 정지 조건 — "언제 멈추나" 명시적 설계

당신 설계서 §4.1에 `MAX_COST_USD=100`만 있는데, **이것만으론 부족**.

### 5-1. 4중 안전장치 (모두 적용 권고)
| 조건 | 임계값 (예시) | 동작 |
|------|------------|------|
| Cost - hourly | $10/hr 초과 | 30분 cooldown |
| Cost - daily | $50/day 초과 | 다음 날 새벽까지 정지 |
| Cost - cumulative | $200 초과 | 영구 정지, 인간 승인 대기 |
| Time | 시작 후 8시간 | 자동 정지 |
| Diminishing returns | 최근 10 verdict 중 8+ rejected | 자동 정지 (탐색 공간 고갈) |
| Stop signal | `/workspace/STOP` 파일 존재 | 즉시 graceful shutdown |
| Persistent failure | 같은 에이전트 5사이클 연속 fail | 해당 에이전트 정지 |

### 5-2. Graceful shutdown 패턴
- `SIGTERM` 받으면 현재 진행 중인 task 완료까지 대기 (kill -9 ❌)
- DB의 `in_progress` task들을 `pending`으로 되돌림 (다음 실행 시 재개 가능)
- 마지막 사이클 리포트 + 누적 비용 Telegram 송신 후 종료

---

## 7. 관찰 가능성 — Monitor 에이전트 강화판

우리 프로젝트의 Monitor는 단순 헤드라인 + STATUS.md였음. 새 프로젝트는 정보 차원이 더 많음.

### 6-1. STATUS.md 구조 권고
```markdown
# SK Factor Analysis — Status (2026-04-28 03:42)

**Headline**: 가설 23개 / 분석 12개 / 발견 3개 / 비용 $47.30 (47%) / 다음: H-008 분석 시작 예정 (~5분)

## 진행 그래프
| 단계 | 카운트 | 비율 | 변화 (1h) |
|------|-------|------|----------|
| 가설 생성 (pending) | 5 | | +2 |
| 수집 중 | 1 | | -1 |
| 수집 완료 | 4 | | +3 |
| 분석 중 | 1 | | -1 |
| 분석 완료 | 2 | | +1 |
| 검증 중 | 0 | | -1 |
| Significant | 3 | | +1 |
| Rejected | 8 | | +3 |
| Refining | 2 | | +0 |

## 비용 burn rate
- 시간당: $5.20/hr (한도 $10/hr 의 52%)
- 누적: $47.30 / $100 (47%)
- 예상 종료: 9시간 후 (한도 도달 또는 8시간 시한 도달)

## 마지막 발견
- F-003 (3분 전): "외국인 지분율 1% 증가 시 NAV 할인율 0.18%p 감소 (p=0.012, n=120)"

## 이상 징후
- ⚠️ Collector가 H-007 처리 시 KCGS 사이트 접속 3회 연속 실패 → 다음 사이클로 연기
- ✅ 그 외 정상

## 다음 60분 예상
- H-008 분석 시작 (~5분)
- H-005 검증 시작 (~15분)
- 새 가설 2~3개 생성 예정 (~30분)
```

### 6-2. Telegram 알림 채널 권고
- `#sk-agent-info`: 사이클 완료, 새 발견, 가설 생성 요약 (조용한 채널)
- `#sk-agent-warn`: 재시도 중, rate limit 도달, 디스크 80% (사용자 참고용)
- `#sk-agent-urgent`: 비용 80% 도달, persistent failure, supervisor 죽음 (밤에 깨워도 OK)

### 6-3. 우리 경험 기반 추가 팁
- 헤드라인이 매번 바뀌면 사용자가 알림 피로 → "5분 전과 의미 있는 변화"만 알림
- "의미 있는 변화"의 정의: 새 발견, 비용 10%p 증가, 새 이상 징후, 사이클 +N
- monitor agent CLAUDE.md에 위 정의를 명시

---

## 8. Phase 0에 추가하면 좋은 것 (당신 설계서에 빠져있음)

### 7-1. Dry-run 모드
- `--dry-run` 플래그로 supervisor 실행 시 실제 claude 호출 없이 로직만 검증
- DB 트랜잭션, 시그널 핸들링, 알림 채널 모두 mock으로 검증
- **이유**: 첫 실행이 실제 비용 발생이면 디버깅 비싸짐. 우리도 첫 v1 부트스트랩에서 `--add-dir` 함정 때문에 즉시 실패 → 다행히 비용 0이었지만 새 프로젝트는 더 복잡해서 fail-and-pay가 잦을 것.

### 7-2. Recovery test
- "supervisor를 강제로 kill했을 때 다음 시작에서 in_progress task를 정확히 재개하는가?"
- "에이전트가 mid-write 중 죽었을 때 corrupted 파일을 verifier가 거르는가?"
- 시뮬레이션 시나리오를 미리 만들어두면 자가 치유가 진짜 작동하는지 검증 가능

### 7-3. Schema 진화 전략
- hub.db 스키마는 분석 도중 수정 욕구가 무조건 생김 (새 컬럼, 새 제약)
- Alembic 같은 마이그레이션 도구를 처음부터 도입 권고
- 우리도 도중에 logs 컬럼 하나 추가하려다 데이터 백업하고 재생성하느라 시간 날림

### 7-4. 비용 추정 유닛 테스트
- 각 에이전트 호출 시 input/output 토큰 추정 vs 실제를 비교 로깅
- 이상하게 많이 쓴 호출을 사후 식별 가능 (어떤 prompt가 비용 폭주의 주범인지)

---

## 9. 우리가 헛수고한 부분 (피하세요)

### 8-1. fswatch 디버깅에 1시간+
- 위에서 언급한 두 가지 함정 (`--event=` 필터, `local`+서브쉘) 때문
- **회피**: 새 프로젝트는 polling 기반이라 해당 없음 ✓

### 8-2. start-all.sh가 set -e + glob no-match로 죽음
- `rm -rf /tmp/aipm-mat-*.lock.d` 가 매치 0개일 때 zsh에서 `no matches found` 에러
- bash로 짜도 동일 패턴 자주 발생
- **회피**: Python supervisor라면 해당 없음 ✓

### 8-3. atomic write 패턴이 fswatch와 충돌
- Claude Write 도구는 내부적으로 `.tmp.tmp.{pid}.{ts}` → `.tmp` → 최종 이름의 3단계 rename
- fswatch 이벤트 필터 OFF로 해결했지만 처음엔 왜 안 되는지 모름
- **회피**: SQLite polling이면 파일 이벤트 자체에 의존 안 하므로 해당 없음 ✓

### 8-4. CLAUDE.md를 너무 장황하게 작성
- 처음에 Writer CLAUDE.md를 매우 상세하게 적었더니 매 호출마다 큰 컨텍스트 소비
- 핵심은 **"이 에이전트가 호출되는 시점에 이미 알 수 있는 것"은 다 빼고, 매번 달라지는 부분만 prompt로 넘기기**
- 우리 Writer CLAUDE.md ~10KB 정도가 적정선이었음. 이보다 길면 비용↑·정확도↓.

### 8-5. 리뷰어 충돌 해결 규칙을 늦게 만듦
- Strategist와 Evaluator가 상충하는 권고를 했을 때 Writer가 갈팡질팡
- 결국 "Evaluator 우선" 규칙을 Writer CLAUDE.md §4-2에 명시하고 해결
- **새 프로젝트**: Verifier가 "needs_refinement" 했을 때 Hypothesis가 어떻게 반응할지, Collector가 데이터 부족이라고 했을 때 누가 결정할지 — **충돌 시나리오를 처음부터 카탈로그화**해서 각 에이전트 CLAUDE.md에 명시.

---

## 10. 추가 권고 (구현 우선순위 보정)

당신 설계서의 Phase 0~4 순서는 합리적이지만 우리 경험상 다음을 추가:

### Phase 0.5 (Phase 0 직후, Phase 1 직전)
1. **Skip permission 격리 환경 구축** — Docker + 네트워크 egress allowlist + secret 분리
2. **Dry-run 모드 동작 확인** — 실비용 0으로 전체 루프 1회 검증
3. **Recovery test 스크립트** — supervisor kill → 재개 시나리오 자동 검증

### Phase 1.5 (각 에이전트 단위 검증 후)
- 두 에이전트 간 통신을 별도로 테스트 (예: Hypothesis만 돌려서 hub.db 채우고, 그 다음 Collector만 그걸 처리)
- 우리는 처음부터 4개 다 동시에 띄웠다가 디버깅 어려웠음. 새 프로젝트는 더 복잡하니 두 개씩 끊어서 검증 권고.

### Phase 3.5 (오버나이트 테스트 직후)
- "재현 가능성 보고서" 작성 — 동일 시드로 재실행 시 같은 결과 나오는가?
- 안 나오면 비결정성 원인(LLM temperature, 웹 데이터 변동, 시간 의존 코드) 식별

---

## 11. Cheat sheet (즉시 적용 가능한 코드 스니펫)

### 10-1. Python supervisor의 graceful shutdown
```python
import signal, sys
shutdown_requested = False

def handle_sigterm(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("SIGTERM 수신 — 현재 task 완료 대기 중")

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

while not shutdown_requested:
    if Path("/workspace/STOP").exists():
        log("STOP 파일 감지 — 종료")
        break
    cycle()
    time.sleep(POLL_INTERVAL)

# in_progress task를 pending으로 롤백
db.execute("UPDATE hypotheses SET status='pending' WHERE status='in_progress'")
log("Graceful shutdown 완료")
```

### 10-2. SQLite WAL 모드 + 동시성
```python
conn = sqlite3.connect("hub.db", timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=30000")  # 30초 대기 후 lock 에러
conn.execute("PRAGMA foreign_keys=ON")
```

### 10-3. Telegram 알림 with 등급
```python
def alert(message, level="info"):
    emoji = {"info": "📊", "warn": "⚠️", "urgent": "🚨"}[level]
    chat_id = {"info": INFO_CHAT, "warn": WARN_CHAT, "urgent": URGENT_CHAT}[level]
    requests.post(TELEGRAM_API, json={"chat_id": chat_id, "text": f"{emoji} {message}"})
```

### 10-4. Cost circuit breaker
```python
class CostBreaker:
    def __init__(self, hourly_cap, daily_cap):
        self.hourly_cap = hourly_cap
        self.daily_cap = daily_cap
        self.calls = []  # [(timestamp, cost_usd), ...]

    def can_proceed(self):
        now = time.time()
        last_hour = sum(c for ts, c in self.calls if now - ts < 3600)
        last_day = sum(c for ts, c in self.calls if now - ts < 86400)
        if last_hour > self.hourly_cap:
            alert(f"Hourly cap 도달 ({last_hour:.2f}/{self.hourly_cap})", "warn")
            return False
        if last_day > self.daily_cap:
            alert(f"Daily cap 도달 ({last_day:.2f}/{self.daily_cap})", "urgent")
            return False
        return True

    def record(self, cost_usd):
        self.calls.append((time.time(), cost_usd))
```

---

## 12. 마지막 한 줄

> **우리 프로젝트(multi-agent-test)에서 가장 큰 교훈은 "트리거와 동시성 처리는 디테일에서 깨진다"**.
> 새 프로젝트는 이 디테일이 10배 더 많을 것 (DB 락, 웹 rate limit, Playwright 세션, 다운로드 디스크, 무한 가설 생성, 비용 폭주 등). 이걸 처음부터 의식하고 짜면 우리가 1시간씩 날린 디버깅을 안 해도 됨.

---

## 부록 A. 우리 프로젝트 산출물 위치 참고

새 프로젝트에서 비슷한 패턴이 필요할 때 직접 코드를 가져다 쓸 수 있는 위치:

- `/Users/heechang/Projects/AIPM-agent/multi-agent-test/scripts/lib/common.sh` — `log()`, `run_agent()`, mkdir lock 패턴 (Python으로 포팅 시 참고)
- `/Users/heechang/Projects/AIPM-agent/multi-agent-test/scripts/run-monitor.sh` — sleep loop 데몬 패턴
- `/Users/heechang/Projects/AIPM-agent/multi-agent-test/agents/monitor/CLAUDE.md` — 모니터 에이전트 프롬프트 (Telegram 채널만 바꾸면 그대로 재사용)
- `/Users/heechang/Projects/AIPM-agent/multi-agent-test/agents/writer/CLAUDE.md` §6 출력 검증 체크리스트 — Verifier 에이전트 설계 시 참고
- `/Users/heechang/.claude/projects/-Users-heechang-Projects-AIPM-agent-multi-agent-test/memory/feedback_shell_daemon_gotchas.md` — 함정 3종 정리 (Python 데몬 짜면 회피 가능하지만 알아두면 좋음)
