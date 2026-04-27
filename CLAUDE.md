# multi-research-agent — SK NAV 팩터 자율 탐색

> 밤새 무인으로 가설→수집→분석→검증 루프를 도는 멀티에이전트.
> 4 에이전트(Hypothesis/Collector/Analyzer/Verifier)를 Python Supervisor가 SQLite `hub.db` 기반으로 조정.
> 사용자: AIPM 리서치팀 — 산출물은 SK PR/IR 및 내부 분석.

## 1. 목적과 사용자
- **목적**: SK㈜ 지주사 NAV 할인율(또는 기업가치) 변동 요인을 자율 탐색·검증
- **사용자**: AIPM 리서치팀 (1차) → SK PR/IR (납품)
- **산출물**: 검증된 finding 목록 + provenance (가설→데이터→분석→판정 추적 가능)

## 2. 런타임
- **모델 분담** (`.env` `MODEL_*` 로 오버라이드)
  - Supervisor / Monitor: Haiku 4.5 (저비용 폴링)
  - Hypothesis / Analyzer: Opus 4.7 (창의·심층 추론)
  - Collector / Verifier: Sonnet 4.6 (도구 사용·검증)
- **실행 방식**: Claude Code CLI 헤드리스 (`claude --print --dangerously-skip-permissions`)
- **트리거**: SQLite polling (Supervisor 메인 루프, 30초 간격) — 핸드오버 §3-1, fswatch 사용 금지
- **상태 채널**: `workspace/hub.db` (단일 source of truth, WAL 모드)

## 3. 호스팅 / 배포
- **환경**: 로컬 macOS → Phase 0.5에서 Docker 격리(필수, 핸드오버 §5)
- **격리**: read-only 루트 + tmpfs + cap-drop=ALL + egress allowlist
- **데몬 보호**: `pm2` 또는 `launchd`로 Supervisor 자체도 보호 (Phase 3에서 결정)

## 4. 인터페이스
- **알림**: Telegram bot — 등급 분리(`info`/`warn`/`urgent`), 핸드오버 §6-2
- **상태**: `workspace/STATUS.md` (Monitor 에이전트가 5분마다 덮어씀)
- **수동 정지**: `workspace/STOP` 파일 생성 시 graceful shutdown

## 5. 비밀값
- `.env` 사용. **절대 커밋 금지**. `.env.example` 만 커밋.
- 에이전트는 `os.environ`을 통해서만 접근. `.env` 파일 직접 읽기 금지.
- Phase 0.5 Docker에선 환경변수로만 주입(`docker run -e ...`), 파일 마운트 X.
- 키별 사용 에이전트:
  - `ANTHROPIC_API_KEY`: 모든 에이전트
  - `DART_API_KEY`, `KCGS_*`, `KOSIS_API_KEY`: Collector 전용
  - `TELEGRAM_*`: Monitor / Supervisor 전용

## 6. 시스템 아키텍처

```
        ┌──────────────────────────────────┐
        │   SUPERVISOR (Python, polling)   │
        │  cycle dispatch / cost / stop    │
        └──┬──────┬──────┬──────┬──────┬───┘
           ▼      ▼      ▼      ▼      ▼
     Hypothesis Collec  Analy  Verifi  Monitor
       (Opus)  (Sonnet) (Opus) (Sonnet)(Haiku)
           │      │      │      │      │
           └──────┴──────┴──────┴──────┘
                       │
         ┌─────────────┴─────────────┐
         ▼                           ▼
   workspace/hub.db          workspace/data/{raw,processed,downloads}
   (WAL, 단일 SoT)            (수집 산출물, TTL 7일)
         │
         ▼  Telegram / STATUS.md
```

핵심 흐름: Hypothesis → `hub.db.hypotheses(status=pending)` → Collector → `datasets(collected)` → Analyzer → `analyses(complete)` → Verifier → `verdicts(significant|rejected|needs_refinement)` → 피드백 → Hypothesis.

## 7. 디렉토리 구조 핵심
- `agents/{name}/CLAUDE.md` — 에이전트별 페르소나·트리거·산출물(자동 cwd 로드)
- `reference/` — **에이전트 입력** 자료 (`00_writing_style_guide`, `01~05_*.md`, 6 파일). 매 호출 시 `--add-dir` 으로 마운트
- `source/` — 가공 안 된 원본(PDF/DOCX/Excel) — **수정·삭제 금지**
- `workspace/` — 런타임 동적, 통째로 삭제·재생성 가능
  - `outputs/` 정식 산출물(사용자에게 보여줄 것) ↔ `logs/` 디버그 로그 — **분리 필수**
  - `briefs/{hypothesis_id}/` 텍스트 메모 (이중 산출물 모델)
  - `hub.db` SQLite hub
- `scripts/` — Supervisor + 헬퍼 (Python)
- `configs/` — YAML/JSON 설정 (env에 안 담을 것)
- `docs/` — **사람용 문서**: `06_handover_to_multi_research_agent.md` (PoC 핸드오버), `sk-multi-agent-design.md` (설계서), `decisions/` ADR, `runbooks/` 운영 매뉴얼. 에이전트는 안 봄

## 8. 협업 규칙

### 정지 조건 (4중 안전장치, 핸드오버 §6)
| 조건 | 임계값 | 동작 |
|------|--------|------|
| Hourly cost | `MAX_COST_USD_PER_HOUR` | 30분 cooldown |
| Daily cost | `MAX_COST_USD_PER_DAY` | 다음 날까지 정지 |
| Cumulative | `MAX_COST_USD_CUMULATIVE` | 영구 정지, 인간 승인 |
| Time | `MAX_RUN_HOURS` | 자동 정지 |
| Diminishing returns | 최근 10 verdict 중 8+ rejected | 자동 정지 |
| Stop signal | `workspace/STOP` 존재 | 즉시 graceful |
| Persistent failure | 같은 에이전트 5사이클 연속 fail | 해당 에이전트 정지 |

### 가설 폭발 방지 (§3-5)
- `hypotheses.depth` 컬럼 (root=0, 파생 +1), `>= MAX_HYPOTHESIS_DEPTH` 거부
- 사이클당 신규 ≤ `MAX_NEW_HYPOTHESES_PER_CYCLE`

### 에이전트 충돌 우선순위
- Verifier `needs_refinement` ≻ Hypothesis 신규 → 같은 가설 정제 우선
- Collector "데이터 부족" → Verifier가 "가설 자체 기각" vs "수집 재시도" 결정
- 충돌 시나리오 카탈로그는 각 에이전트 CLAUDE.md `§충돌` 섹션에 명시(Phase 1)

### 재시도 정책
- 에이전트 호출 실패 시 지수 백오프 (1m → 5m → 30m), 최대 3회
- 4시간 이상 `in_progress` 상태인 task는 Supervisor가 자동으로 `failed` 마킹(§3-8)

## 9. 이 시스템에서 작업할 때 (Claude를 위한 메타 지침)

### 절대 하지 말 것
- `workspace/outputs/` 의 기존 사용자 산출물 임의 삭제·덮어쓰기
- `source/` 의 원본 자료 수정
- `.env` 직접 읽기·복사·외부 전송 시도 (감사 로그에 잡히고 즉시 alert)
- 에이전트 정의 한꺼번에 4개 만들기 — **반드시 하나씩**, 동작 확인 후 다음

### 작업 흐름
- 새 에이전트 추가 시: `agents/{name}/CLAUDE.md` 작성 → 단독 dry-run → 통과 후 Supervisor 등록
- DB 스키마 변경: 반드시 Alembic 마이그레이션 (직접 ALTER 금지) — 핸드오버 §8-3
- 모든 LLM 호출에 비용 추정 로깅 (input/output 토큰 추정 vs 실제) — §8-4
- CLAUDE.md 길어지면 5~15KB 안에서 자르고 디테일은 `reference/`로 분리

### 디버깅 시 보아야 할 곳
- `workspace/logs/{agent}.log` — 에이전트별 실행 로그
- `workspace/hub.db` (`agent_logs`, `cost_tracker` 테이블)
- `workspace/STATUS.md` — Monitor 최신 헤드라인
- `docs/decisions/` — 왜 이 방식으로 했는지 ADR

### 진행 단계 (Phase)
- ✅ **Phase 0**: 골격 (디렉토리·.gitignore·README·.env.example·pyproject·CLAUDE.md)
- ⏭️ **Phase 0.5**: Docker 격리 + dry-run 모드 + recovery test
- **Phase 1**: Hypothesis 1개 에이전트 + Supervisor 골격 + hub.db 스키마(Alembic)
- **Phase 1.5**: 첫 LLM 호출 dry-run → live (단일 에이전트)
- **Phase 2**: Collector 추가 → 2 에이전트 통신 검증
- **Phase 3**: Analyzer + Verifier + Monitor → 풀 루프
- **Phase 3.5**: 오버나이트 테스트 + 재현성 보고서
- **Phase 4**: 운영(데몬 보호 + 알림 튜닝)

각 Phase 종료 시 사용자 승인 게이트.

---
참조: `docs/06_handover_to_multi_research_agent.md` (검증된 패턴 + 회피할 함정), `docs/sk-multi-agent-design.md` (도메인 설계). 에이전트 입력은 `reference/00~05_*.md`.
