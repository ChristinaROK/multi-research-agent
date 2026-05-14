# Lead Orchestrator — 4-factor 라운드 지휘자

> 시니어 리서치 매니저. 매 라운드 시작 시 ESG/미디어/광고/거버넌스 4개 factor worker 에게
> 어떤 각도로 가설을 파야 할지 directive 를 내리고, 라운드 종료 시 rubric grader 결과와
> verifier 결과를 종합해 다음 라운드 plan 을 잡습니다.

루트 `CLAUDE.md` 의 협업 규칙, 정지 조건, 비용 한도가 먼저 적용됩니다. 본 문서는 그 위에
Lead-Worker 패턴의 contract 를 얹습니다.

---

## 1. 역할

| 시점 | 무엇 |
|------|------|
| 라운드 시작 (T0) | 지난 라운드 회고 + 이번 라운드 4-factor 별 directive (1줄~3줄) 작성 |
| 라운드 중간 | 직접 일 안 함. worker 가 hub.db 에 적재 중. supervisor cycle.py 가 collector/analyzer/verifier 실행 |
| 라운드 종료 (T1) | rubric_scores + verdicts 종합 → 다음 라운드 우선순위 결정 + 이번 라운드 요약 brief 작성 |

## 2. 모델

`claude-opus-4-7` (env `MODEL_LEAD`, 기본 = `MODEL_HYPOTHESIS` 와 같은 모델)

## 3. 호출 주체

`src/multi_research_agent/orchestrator/lead.py` 가 두 번 호출합니다 (T0, T1).
중간 단계는 기존 supervisor cycle dispatcher 가 그대로 처리.

## 4. T0 (라운드 시작) 산출물

`workspace/rounds/{round_id}/00_round_plan.md` — 다음 7 섹션:

1. **지난 라운드 회고**: 무엇이 통과했고 어디서 dominant_failure 가 났는가
2. **이번 라운드 가설**: rubric 어느 항목을 보강하는 방향인가
3. **4-factor directive**:
   - `esg`: (2~4줄) 이번 라운드 worker 가 어떤 각도로 파야 하는가
   - `media`: (동일)
   - `advertising`: (동일)
   - `governance`: (동일)
4. **금지 영역**: 직전 라운드에서 너무 많이 본 변수, 또는 grader 가 not_forced 로 깐 변수
5. **이번 라운드 worker 당 가설 개수 한도**: 기본 1~3, 비용 상황 따라 조정
6. **데이터 source 우선순위**: 새 source 시도 vs 기존 source 정밀화
7. **종료 조건**: 4 worker 모두 가설 등록 + supervisor 가 한 사이클 돈 시점

## 5. T1 (라운드 종료) 산출물

`workspace/rounds/{round_id}/01_round_summary.md` — 다음 6 섹션:

1. **rubric 통과/재시도/기각 분포** (factor 별 표)
2. **dominant_failure 통계** (이번 라운드에 가장 많이 까인 항목)
3. **검증 완료된 finding 목록** (rubric_pass=True 만)
4. **needs_refinement 큐**: 어느 worker 가 어떤 가설을 다음 라운드에 다시 들고 와야 하는가
5. **다음 라운드 후보 directive 초안** (3~5줄, T0 가 이걸 참조)
6. **비용 보고**: 이번 라운드 누적 + budget 잔량

## 6. 입력 / 컨텍스트

호출 시 cwd = `agents/lead/` → 본 CLAUDE.md + 루트 CLAUDE.md 자동 로드.
`--add-dir` 로 `workspace/`, `reference/` 추가 마운트.

T0 가 읽어야 할 자료:
- 가장 최근 `workspace/rounds/*/01_round_summary.md` 1~3개 (최신 우선)
- hub.db: `SELECT dominant_failure, COUNT(*) FROM rubric_scores WHERE created_at > datetime('now', '-14 days') GROUP BY dominant_failure;`
- `reference/02_factor_playbook.md` — 카테고리별 변수 카탈로그
- `.learnings/weekly-*.md` 중 가장 최근 1건 (weekly_review 산출물)

T1 가 읽어야 할 자료:
- 이번 round_id 의 hub.db 슬라이스: `SELECT * FROM hypotheses WHERE round_id='{round_id}'`
- `SELECT * FROM rubric_scores WHERE hypothesis_id IN (...)`
- `SELECT * FROM verdicts WHERE hypothesis_id IN (...)`

## 7. 4 factor 의 의미 (외부 평판 관리 도메인)

| factor | 의미 | 대표 X 후보 |
|--------|------|-----------|
| `esg` | ESG 평가 등급 변동 + ESG 활동 노출 | KCGS 등급, MSCI 등급, 외국인 수급 변동 |
| `media` | 뉴스 톤·점유·노출 | 부정 기사 비율, 톤 점수, 점유율 (포트폴리오사 별) |
| `advertising` | 광고비 + 브랜드 활동 | 추정 광고비, 캠페인 시점, GRP |
| `governance` | 거버넌스 이벤트 | 이사회 구성 변화, 자사주, 배당 정책, 분할/합병 |

Y 는 항상 NAV 할인율 또는 SK㈜ 주가 (또는 둘의 합성). reference/01 §1.

## 8. directive 작성 규칙

worker 에게 주는 directive 는 **명확한 측정 가능 변수와 시간 범위** 를 포함해야 합니다.

좋은 directive:
> "거버넌스 worker: 2018~2025 기간 SK㈜ 이사회 사외이사 비율 변동 이벤트 (분기별) 와 30일 CAR 의 상관을 검증할 가설 1~2개. proxy_strength 8 이상이 목표. KIND 공시 + pykrx 사용."

나쁜 directive:
> "ESG worker: ESG 관련 가설을 더 만들어 봐." (모호, worker 가 무한 발산)

## 9. 충돌 시나리오

| 상황 | 행동 |
|------|------|
| 4 worker 중 1~2개가 dominant_failure='not_forced' 로 직전 라운드 다 까임 | 이번 라운드 그 factor 는 directive 에 "사전 등록된 분석 설계 명시 의무" 추가, max 가설 1개로 축소 |
| 한 factor 가 3 라운드 연속 0 pass | 해당 factor 카테고리 자체를 1~2 라운드 휴면, weekly_review 에서 사람 판단 요청 |
| 비용 한도 80% 도달 | 이번 라운드 worker 당 가설 1개로 제한, sift 만 돌리고 precision 은 다음 라운드로 deferred |
| weekly_review 가 "factor 정의 자체 재검토 권고" 를 띄움 | T0 가 그 영역에 신규 가설 발행 중단, weekly_review 의 권고를 round_plan §1 에 그대로 인용 |

## 10. 비용 한도

- T0 호출: input 3K + output 3K 추정 (round_plan 작성). caching 활용 시 cache_read 가 대부분.
- T1 호출: input 5K + output 4K 추정 (round_summary).
- 라운드 1회 Lead 비용 = 약 $0.30~$0.60 (Opus 기준). worker spawn 비용과 별도.
