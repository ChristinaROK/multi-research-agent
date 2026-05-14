# multi-research-agent

> SK㈜ 지주사의 **기업가치(주가, NAV 할인율)에 뭐가 영향 주는지** 데이터 기반으로 찾아내는 무인 멀티에이전트 시스템.
>
> 사람이 한 줄 명령하면 → 시스템이 가설을 만들고 검증하고 채점 → 통과한 것만 사람이 검토.

---

## 무엇을 측정하나

**Y 변수 2종 (둘 다 1순위)**

| 변수 | 정의 |
|---|---|
| 주가 | SK㈜ 시가총액 (또는 종가 × 발행주식수) |
| NAV 할인율 | `(멤버사 지분가치 합 - SK㈜ 시총) / 멤버사 지분가치 합 × 100` |

**X 변수 6 카테고리**

| 카테고리 | 의미 |
|---|---|
| `esg` | ESG 등급 + 외국인 패시브 자금 반응 |
| `media_pr` | 언론 톤, 노출, 이슈 분류 |
| `advertising` | 광고 집행 + 브랜드 활동 |
| `governance` | 지배구조 + 주주환원 (자사주, 배당, 이사회) |
| `portfolio` | 멤버사 업황 (SK하이닉스 / 텔레콤 / 이노베이션 등) |
| `macro` | 거시 환경 (금리, 환율, 외국인 패시브) |

자세한 정의 + 변수 카탈로그: `reference/01_domain_knowledge.md`, `reference/02_factor_playbook.md`.

---

## 한 라운드의 흐름

```
  ① 라운드 계획        ② 가설 생성             ③ 자동 검증
  ──────────────       ─────────────           ──────────────
  Lead (Opus)          6 Worker 병렬           Collector
      │                ESG / 언론 / 광고            ↓
      │                거버넌스 / 포트폴리오       Analyzer
      ▼                  / 거시                     ↓
  plan.md                  │                      Verifier
                           │ 가설 18 개            (통계 재현 + 강건성)
                           ▼                        │
                       hub.db                       ▼
                                                통과한 finding


  ④ 메타 채점                              ⑤ 라운드 정리
  ──────────────                           ──────────────
  Grader (Haiku → Opus)                    Lead (Opus)
      │                                        │
      │ 5 항목 점수                              ▼
      │  · 인과 명확한가                        summary.md
      │  · proxy 측정 맞나                          │
      │  · 외부 검증 되나                          ▼
      │  · 억지 논리 아닌가                     SK 담당자 검토
      │  · 비교 baseline 있나                  (납품 후보)
      ▼
  7점 이상만 통과
```

**매주 금요일**: `weekly_review.py` 가 지난 7일 통계 + 패턴 추출 → SK 담당자 30분 검토.

---

## 모델 분담

| 모델 | 어디 |
|---|---|
| Opus (창의 / 심층) | Lead, Analyzer, Grader 정밀 |
| Sonnet (도구 사용) | 6 Worker, Collector, Verifier |
| Haiku (저비용 폴링) | Supervisor, Monitor, Grader 1차 |

라운드 1회 약 **\$3**. 하루 5 라운드 약 **\$15**. 한 달 약 **\$450**.

---

## 실행 방법

### 1. 환경 setup (한 번만)

```bash
# Python venv
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[runtime,dev]"

# .env 파일 — secret
cp .env.example .env
# 에디터로 열어서 ANTHROPIC_API_KEY, DART_API_KEY, TELEGRAM_BOT_TOKEN 등 채움

# DB 마이그레이션 (workspace/hub.db 생성)
alembic upgrade head
```

### 2. 검증 (실비용 0)

```bash
# 27/27 테스트 통과 확인
pytest tests/

# Lint 통과 확인
ruff check src/ scripts/ tests/

# dry-run: Claude 호출 없이 cycle 로직만 검증
RUN_MODE=dry_run python scripts/supervisor.py --once
```

### 3. Lead-Worker 라운드 1회 (실비용 발생 ~\$3)

```bash
# .env 에 RUN_MODE=live 또는 환경변수로 직접
RUN_MODE=live python -c "
from multi_research_agent.db import get_session
from multi_research_agent.orchestrator import run_round

with get_session() as session:
    result = run_round(session, wait_for_completion=False)
    print(f'round_id: {result.round_id}')
    print(f'plan: {result.plan_path}')
    for w in result.workers:
        print(f'  {w.factor}: {len(w.hypothesis_ids)} hypotheses, \${w.cost_usd:.2f}')
"
```

`workspace/rounds/{round_id}/00_round_plan.md` 가 생성되고 6 worker 가 각자 가설을 `hub.db.hypotheses` 에 INSERT.

### 4. 4-stage 파이프라인 가동 (Supervisor)

```bash
# 폴링 데몬으로 무한 가동
RUN_MODE=live python scripts/supervisor.py

# 또는 1 사이클만
RUN_MODE=live python scripts/supervisor.py --once
```

Supervisor 가 30초마다 `hub.db` 폴링하면서 `pending → collecting → collected → analyzing → analyzed → verifying → significant/rejected` 진행.

상태는 `workspace/STATUS.md` 와 Telegram 으로 보고.

### 5. Grader 채점 (Verifier 통과 finding 만)

Phase 1 현재 미연결 (다음 작업). 연결 후:

```bash
# (자동) Verifier 가 significant 판정한 finding 마다
# cycle.py 가 grade_and_persist() 호출 → rubric_scores 테이블 + findings.rubric_pass
```

수동 호출 (테스트용):

```bash
python -c "
from pathlib import Path
from multi_research_agent.db import get_session
from multi_research_agent.grader import Grader, FindingPayload, grade_and_persist

payload = FindingPayload(
    finding_id='F-TEST',
    hypothesis_id='H-TEST',
    factor_category='governance',
    factor_name='foreign_holding_pct',
    causal_claim='외국인 지분율 +1%p → NAV 할인율 -0.2%p',
    brief_text=Path('workspace/briefs/H-TEST/01_hypothesis_brief.md').read_text(),
    verdict_text=Path('workspace/briefs/H-TEST/04_verdict.md').read_text(),
    finding_text=Path('workspace/briefs/H-TEST/05_finding.md').read_text(),
)
with get_session() as session:
    score = grade_and_persist(session, payload)
    print(f'{score.decision.value}: {score.weighted_score:.2f}')
"
```

### 6. 주간 리뷰 (매주 금요일)

```bash
# 통계만 (LLM 호출 X)
python scripts/weekly_review.py --dry-run

# LLM 메타 리뷰 + markdown 작성
python scripts/weekly_review.py
# → workspace/learnings/weekly-{YYYY-MM-DD}.md 생성
```

SK 담당자가 30분 검토 후 승인 항목만 `CLAUDE.md` 의 `core_findings` 섹션에 수동 반영.

### 7. 정지

```bash
# 파일 신호로 graceful shutdown
touch workspace/STOP
# Supervisor 가 다음 폴링 사이클에서 감지하고 종료
```

---

## 디렉토리 구조 (핵심만)

```
multi-research-agent/
├─ CLAUDE.md                      협업 규칙 + 정지 조건 (모든 에이전트 공통)
├─ configs/rubric.yaml            Grader 의 5 항목 정의 + reference cases
├─ reference/                     에이전트 공통 입력 (도메인, 변수 카탈로그)
│   ├─ 01_domain_knowledge.md    Y / X 변수 정의 + NAV 할인율 메커니즘
│   ├─ 02_factor_playbook.md     6 카테고리별 변수 카탈로그
│   ├─ 03_collection_recipes.md
│   ├─ 04_analysis_methods.md
│   └─ 05_verification_checklist.md
├─ agents/                        각 에이전트 페르소나 (cwd 자동 진입)
│   ├─ lead/                     라운드 지휘자 (T0 plan, T1 summary)
│   ├─ factor_worker/            6 factor 중 1개를 깊이 파는 worker
│   ├─ hypothesis/               (구조 보존) 단일 가설 생성기
│   ├─ collector/                데이터 수집
│   ├─ analyzer/                 통계 분석
│   ├─ verifier/                 검증 + 판정
│   └─ monitor/                  Haiku 5분 STATUS.md
├─ src/multi_research_agent/
│   ├─ supervisor/runner.py      claude-agent-sdk 래퍼
│   ├─ supervisor/cycle.py       4-stage 디스패처
│   ├─ orchestrator/lead.py      6 worker 병렬 spawn
│   ├─ grader/rubric.py          Haiku sift → Opus precision 채점
│   └─ db/models.py              hub.db ORM (9 테이블)
├─ scripts/
│   ├─ supervisor.py             메인 데몬 entry
│   ├─ monitor.py                Haiku 5분 폴링
│   └─ weekly_review.py          DIY Weekly Review
├─ workspace/                     런타임 (gitignore)
│   ├─ hub.db                    SQLite SoT (WAL 모드)
│   ├─ briefs/{hid}/             가설 1건의 산출물 5종
│   ├─ rounds/{rid}/             라운드 plan + summary
│   └─ learnings/weekly-*.md     주간 리뷰 산출물
└─ tests/                         27/27 passed
```

---

## 안전장치

| 조건 | 동작 |
|---|---|
| 시간당 비용 초과 | 30분 cooldown |
| 일 비용 초과 | 다음 날까지 정지 |
| 누적 비용 초과 | 영구 정지 (인간 승인) |
| 최근 10 verdict 중 8+ rejected | 자동 정지 (수익체감) |
| `workspace/STOP` 파일 | 즉시 graceful shutdown |
| 같은 에이전트 5 사이클 연속 fail | 해당 에이전트 정지 |

한도는 `.env` 의 `MAX_COST_USD_PER_*`, `MAX_RUN_HOURS` 등으로 조정.

---

## 자세한 문서

- `CLAUDE.md` — 루트 협업 규칙, 정지 조건, Phase 진화
- `docs/sk-multi-agent-design.md` — 전체 설계서
- `docs/06_handover_to_multi_research_agent.md` — PoC 핸드오버 (검증된 패턴 + 회피할 함정)
- `reference/01~05_*.md` — 에이전트 도메인 입력
- `agents/{name}/CLAUDE.md` — 에이전트별 페르소나

---

## 라이선스

Proprietary (AIPM 리서치팀 / SK 그룹 납품용).
