# 00. Writing Style Guide — 에이전트 간 텍스트 메모 작성 규칙

> 본 문서는 4 에이전트가 만드는 **텍스트 메모** (brief / memo / report / verdict / finding) 의 톤·구조·길이를 표준화합니다.
> 메모는 다음 에이전트(혹은 SK PR/IR 사람) 를 향한 **편지**. SQL row 의 단순 반복이 아니라 **추론·판단의 근거**를 담는 곳.

---

## 0. 왜 텍스트 메모가 필요한가 (배경)

LLM 에이전트의 가치는 **방대한 텍스트에서 맥락과 핵심을 잡아내는 능력**. 하지만 산출물을 SQL row + 1줄 summary 로만 압축하면 다음 에이전트가 그 맥락을 잃고 매번 사고를 처음부터 다시 시작하게 됩니다.

해결책: **이중 산출물 모델**
- **구조화 (DB row, JSON metrics)**: 자동 처리·집계·쿼리용
- **텍스트 메모 (마크다운)**: 다음 에이전트가 컨텍스트로 읽음. 추론의 chain.

PoC 1차 (multi-agent-test) 에서 `v1.md → v2.md → v3.md` 마크다운 텍스트가 그 자체로 풍부한 컨텍스트를 담아 다음 reviewer 가 충분한 정보로 의사결정한 패턴을 그대로 재사용합니다.

---

## 1. 표준 위치

```
workspace/briefs/{hypothesis_id}/
  01_hypothesis_brief.md       # Hypothesis 가 작성    — 다음 = Collector
  02_collection_memo.md        # Collector 가 작성     — 다음 = Analyzer
  03_analysis_report.md        # Analyzer 가 작성      — 다음 = Verifier
  04_verdict.md                # Verifier 가 작성      — 다음 = Hypothesis (또는 끝)
  05_finding.md                # Verifier (significant 시만) — 납품용
```

원자적 쓰기: 임시 `.tmp` → mv 패턴.
DB의 `*_path` 컬럼에 path 등록.

---

## 2. 길이 가이드 (절대 한도 X, 권고)

| 메모 | 권고 길이 | 절대 상한 | 비고 |
|---|---|---|---|
| 01 brief | 3~5 KB | 8 KB | TL;DR ≤ 3줄 필수 |
| 02 memo | 3~8 KB | 12 KB | 데이터 출처 신뢰도 + 정제 결정 + 발견 패턴 |
| 03 report | 5~12 KB | 20 KB | 모델 결과 표 + 강건성 6항목 + 한계 |
| 04 verdict | 3~8 KB | 12 KB | 판정 근거 + 재현 결과 + 후속 권고 |
| 05 finding | 2~4 KB | 6 KB | 납품용 — 1 page printable |

**상한 초과 시**: 부록(appendix)으로 분리 + 본문에 포인터.
**TL;DR 없는 메모는 다음 에이전트가 무시**할 수 있음. 항상 §1 에 TL;DR 1~3줄.

---

## 3. 톤·문체

### 3-1. 필수 톤
- **회의주의자 + 학술 리뷰어**. "이 가설은 좋다" 가 아니라 "이 가설이 성립하려면 다음 조건이 필요하다".
- **사실 vs 판단 분리**. Analyzer 는 사실, Verifier 는 판단. Hypothesis 는 추론·가설, 단정 X.
- **다음 에이전트를 향한 호명**. "Analyzer 에게: 이 lag 부터 시도하세요" 처럼 명시적 지시.

### 3-2. 금지 톤
- ❌ 자기 칭찬 ("This shows clearly that...") — 평가는 다음 에이전트
- ❌ 모호한 일반론 ("일반적으로 외국인 투자자는...") — 출처·수치 없으면 빼기
- ❌ 단정 ("외국인 지분율은 영향을 미친다") — Analyzer 는 "음의 부호 관측", Verifier 는 "유의 판정" 처럼 수준 분리

### 3-3. 한국어/영어 혼용
- 변수명·수식·통계 용어: 영어 (`p_value`, `OLS_HAC`, `coefficient = -0.18`)
- 메커니즘·맥락·해석: 한국어
- 인용 (선행 연구): 원어 그대로 (영문 논문은 영문 인용)

---

## 4. 메모 별 표준 섹션

### 4-1. 01_hypothesis_brief.md (Hypothesis 작성)
```markdown
# {hypothesis_id}: {factor_name} → discount_rate

## 1. TL;DR (≤3줄)
부호·크기·근거 한 줄 + 카테고리·priority + 데이터 가용성

## 2. 인과 메커니즘 (3~5문단)
왜 이 변수가 NAV 할인율을 움직일 만한가. 부호 명시.
선행 메커니즘 모델 (예: 거버넌스 디스카운트, 정보 비대칭) 어디에 속하는지.

## 3. 선행 연구 (2~5건, 효과 크기·표본·한계)
- 권재민 (2019, 한국재무학회 39(2)): 한국 지주사 패널, 외국인 1%p ↑ → 할인율 -0.12%p (n=87, 2010~2018)
- ...

## 4. 데이터 수집 계획 (Collector 에게)
어떤 소스, 어떤 빈도, 왜. reference/03_collection_recipes.md §X 참조.

## 5. 분석 설계 권고 (Analyzer 에게)
1차 모델 / 2차 robustness / 3차 sensitivity 명시.
reference/04_analysis_methods.md §X 참조.

## 6. 예상되는 함정 (사전 인정)
동시성·역인과·역사 변동·생존편향 등 — 이 가설에 해당되는 것만.

## 7. 해석 가이드 (Verifier 에게)
- 부호 음 → significant 후보
- 부호 양 → 메커니즘 반대, rejected
- p>0.10 → rejected (단, 거시 통제 안 했으면 needs_refinement @ analyzer)
```

### 4-2. 02_collection_memo.md (Collector 작성)
```markdown
# Collection Memo — {hypothesis_id}

## 1. TL;DR (무엇을 모았나, 결측 있나)
3줄: 변수 N개, 기간 X~Y, 결측 Z%, 1차 사용 가능 여부

## 2. 데이터 출처와 신뢰도
- 소스 (라이브러리/사이트 + 버전)
- 1차/2차 출처 구분
- 알려진 한계

## 3. 정제 결정 (왜 이 결정을 했나)
- frequency (일/주/월)
- 결측 처리 (drop / forward-fill / 보간)
- 단위 통일
- 이상치 처리

## 4. 발견한 데이터 특징
- 분포 (mean, std, range)
- 시계열 특성 (자기상관, 정상성, 구조변동)
- 시기별 차이

## 5. 한계와 후속 수집 권고
- 이 가설 검증을 위해 추가로 필요한 변수가 있나?
- 본 데이터의 가정이 다른 가설에도 적용 가능한가?

## 6. Analyzer 에게 보내는 메모
- 추천 frequency / 권고 lag / 차분 필요 여부
- 결합할 다른 dataset 의 위치
- 알려진 함정
```

### 4-3. 03_analysis_report.md (Analyzer 작성)
```markdown
# Analysis Report — {hypothesis_id} / {analysis_id}

## 1. TL;DR (≤3줄)
부호·크기·p·n·robustness — 사실만

## 2. 분석 설계 요약
사전 등록 모델 (data snooping 회피)

## 3. 데이터 결합 결과
inner join 결과 n, 결측 비율, 시기 범위

## 4. 단위근/자기상관 진단
ADF, KPSS, DW + 결정 (차분 vs 수준)

## 5. 모델 결과 (표)
| Lag | β | SE | t | p | R² |
|...|

## 6. 시기 분할
pre/post 구간별 결과 비교

## 7. 통제 변수 (매크로)
KOSPI, 환율, 금리 통제 후 변화

## 8. 강건성 6 항목 (각각 ✅/⚠️/❌)
| 항목 | 결과 |

## 9. 한계
가정·표본·통제·인과 식별의 약점 — 정직하게

## 10. Verifier 에게 보내는 메모
- 사전 등록 모델만 보고 (data snooping 없음 명시)
- raw p / 다중비교 보정 필요 여부
- 메커니즘 부호 일치 여부
- 후속 분석 권고 (별도 가설로 분리할 만한 sensitivity)
```

### 4-4. 04_verdict.md (Verifier 작성)
```markdown
# Verdict — {hypothesis_id}

## 1. 판정: significant / rejected / needs_refinement
한 줄 + 핵심 사유

## 2. 판정 근거 체크리스트
| 항목 | 통과 |

## 3. 재현 결과
re-run 결과 vs 원본, 차이 < 1e-6 확인

## 4. 다중 비교 보정
활성 가설 N개의 raw p → FDR(BH) → corrected p

## 5. 메커니즘 검토
가설 부호 vs 결과 부호, 일치/불일치

## 6. 한계 인정
이 finding 의 약점 — significant 라도 정직하게

## 7. Finding 요약 (significant 시 — 다음 §05 finding.md 의 첫 줄로 이동)
> F-{NNN}: {factor} 1{unit} ↑ → 할인율 {coef:+.2f}%p (corrected p={p:.3f}, n={n}, robustness {pass}/6)

## 8. 후속 가설 권고 (Hypothesis 에게)
파생 가설 후보 2~3개 — 어떤 각도에서 추가 검증할 수 있는가
```

### 4-5. 05_finding.md (Verifier, significant 시만 — 납품용)
```markdown
# F-{NNN}: {Factor Name} → NAV Discount Rate

## 1. Headline
> {1~2 문장 finding 요약 — SK PR/IR 사람이 첫 30초에 보는 부분}

## 2. 효과 크기와 통계
표 1개 — coefficient, p, n, robustness pass count

## 3. 메커니즘 (왜 이 결과가 그럴듯한가)
2~3 문단

## 4. 데이터 / 분석 / 검증 요약
- 데이터: N개 변수, 기간 X~Y, 출처 Z
- 분석: 모델 종류, 통제 변수
- 검증: 강건성 K/6, FDR 보정 후 p

## 5. 한계 (정직하게)
- 본 finding 의 가정 (예: NAV 추정 방법)
- 일반화 한계 (예: SK 단독 vs 패널)
- 후속 검증 필요 (예: IV 분석)

## 6. Provenance (자세히 알고 싶으면 어디로)
- Hypothesis brief: workspace/briefs/{H-id}/01_hypothesis_brief.md
- Collection memo: ...
- Analysis report: ...
- Verdict: ...

## 7. 시사점 (최소한만, 사람이 해석)
"이 결과는 X를 시사할 수 있으나, Y를 검증한 것은 아님" 정도. 정책 제안 X.
```

---

## 5. 인용 의무 (다음 에이전트가 직전 메모를 무시 못 하게)

각 에이전트의 메모 §1 TL;DR 첫 줄에 **직전 메모의 어느 §를 따랐는지 명시**:
- Collector → "Hypothesis brief §4 (데이터 수집 계획) 의 pykrx + DART 권고를 따름. 추가로 ECOS 환율을 추가했음 (이유: §6 함정 중 매크로 통제 필요)."
- Analyzer → "Collection memo §6 의 권고대로 차분 시계열 사용. brief §5 의 lag 1~5 모델 채택."
- Verifier → "Analysis report §10 의 사전 등록 모델 확인. 추가 모델 시도 없음 — data snooping 없음 검증."

이 인용이 빠지면 Verifier 가 needs_refinement (`@ <agent>: 직전 메모 §X 인용 누락`) 으로 돌려보냄.

---

## 6. 좋은 예 vs 나쁜 예

### 좋은 brief §2 (인과 메커니즘)
> 외국인 투자자는 한국 지주사를 평가할 때 통상 두 단계 디스카운트를 적용한다 — (1) 자회사 가치 합산 시 정보 비대칭 디스카운트, (2) 거버넌스 risk 디스카운트. 외국인 지분율이 높아지면 이 중 (2) 가 약화될 가능성이 있다 — 외국인이 IR · 거버넌스 개선 압력을 가하는 행동주의적 행위를 통해. 단, (1) 의 정보 비대칭 디스카운트는 외국인 지분율과 무관하게 작동할 수 있어 본 가설은 (2) 만 검증한다. 따라서 매크로 통제 후에도 부호 음이 유지되어야 강건한 메커니즘.

### 나쁜 brief §2 (피하기)
> 외국인 투자자가 많아지면 NAV 할인율이 줄어들 것이다.

(메커니즘 없음, 단정, 다른 가설과 구분 안 됨)

### 좋은 memo §3 (정제 결정)
> 일별 frequency 사용. 분기 평균도 가능했지만 가설(brief §4)이 일별 반응을 보겠다고 했음. 결측 처리는 forward-fill 대신 drop — 외국인 지분율은 변동이 느려 휴장일 forward-fill 이 자기상관을 인위적으로 1로 만듦. 단위 % 통일 (KRX 가 0.xxx 로 주는 것을 ×100).

### 나쁜 memo §3
> 일별로 정제하고 결측은 처리했음.

(왜 일별인지, 어떻게 처리했는지, 대안은 무엇인지 누락)

---

## 7. 메모 작성에 드는 비용

호출당 input/output 토큰 영향:
- Hypothesis brief 5KB ≈ output 1.5K tokens (~$0.02 추가, Opus 기준)
- 다음 에이전트 input 에 메모 5KB ≈ +1.5K tokens (~$0.005 추가, Sonnet 기준)
- prompt caching (system + references) 으로 일부 완화

**total 가설 풀 사이클 비용**: $0.30~$0.50 → $0.60~$0.80 추정 (메모 + 캐싱 적용 후)

비용 증가는 **finding 의 신뢰도와 납품성** 으로 회수. SK PR/IR 사람이 finding.md 1장 읽으면 5분 내 이해 — provenance 추적·재사용 가능.

---

## 8. 메모를 쓰지 않는 경우 (예외)

- Monitor 는 메모 안 씀 (STATUS.md 가 기능 동등)
- 같은 가설을 같은 에이전트가 재호출하는 경우 (예: needs_refinement @ analyzer 에서 재분석) → 메모 *덮어쓰지 말고* 새 버전 (`03_analysis_report.v2.md`) 으로 추가. 이전 버전 보존.
- Hypothesis 가 파생 가설 5개를 한 사이클에 만들 때 → 각 hypothesis_id 별로 brief 분리 (하나에 5개 묶지 X)

---

## 9. 메모를 사람이 읽는 경우 (Verifier 자체 점검 / 사용자 검토)

50 verdict 마다 (또는 사용자 요청 시) 다음 점검:
- significant 판정한 finding 의 메모를 다시 읽어 메커니즘이 여전히 그럴듯한지
- rejected 판정의 rejection_reason 이 다음 Hypothesis 에 의미 있는 입력이 됐는지
- 메모 길이가 절대 상한을 자주 넘는가 → 본 가이드 갱신 (ADR `docs/decisions/`)
