# 05. 검증 체크리스트 — Verifier 전용

> Verifier 가 Analyzer 의 결과를 **회의주의자의 시선**으로 검증할 때 사용하는 체크리스트.
> 핵심 원칙: **거짓 양성 보다 거짓 음성이 안전**. 모호하면 `needs_refinement`.

---

## 0. 판정 (3 가지)

| 판정 | 의미 | 다음 단계 |
|---|---|---|
| `significant` | 강건한 통계적 유의 + 적절한 효과 크기 + 메커니즘 그럴듯 → **finding 후보** | Finding 등록 |
| `rejected` | 통계적 유의 X 또는 효과 미미 또는 가설 메커니즘 불일치 | Hypothesis 가 다른 각도 파생 검토 |
| `needs_refinement` | 모호 (방법이 부적절, 데이터 부족, 추가 robustness 필요) | Hypothesis/Collector/Analyzer 중 어디로 돌려보낼지 명시 |

> Verifier 는 finding 까지 만들 수 있는 권한 보유. `findings` 테이블 INSERT 도 Verifier 책임.

---

## 1. 1차 통계 체크리스트

### 1-1. p-value 단독 판정 금지
- p<0.05 만으론 부족. **다음 셋이 모두 통과** 해야 `significant` 후보:
  - p<0.05 (또는 다중 비교 보정 후)
  - 효과 크기 의미 있음 (단순 통계적 유의가 아니라 경제적 의미)
  - HAC/cluster 강건성 통과

### 1-2. 효과 크기 가이드
| 변수 단위 | "의미 있는" 최소 효과 |
|---|---|
| 1%p factor 변화 → 할인율 변화 | ≥0.05%p |
| 1σ factor 변화 → 할인율 변화 | ≥0.5%p |
| event 발생 → CAR | ≥1%p (10일 누적) |

이보다 작으면 통계적으로 유의해도 `rejected`.

### 1-3. 다중 비교 보정 (FDR)
모든 활성 가설의 raw p-value 를 모아 FDR (Benjamini-Hochberg, alpha=0.05) 보정 후 판정.

```python
from statsmodels.stats.multitest import multipletests
all_pvals = session.execute(
    "SELECT analysis_id, json_extract(metrics, '$.p_value') AS p "
    "FROM analyses WHERE status='complete'"
).all()
reject, p_corrected, _, _ = multipletests([p for _, p in all_pvals],
                                          alpha=0.05, method="fdr_bh")
```

가설 단위 판정 시 **보정 후** p-value 사용.

### 1-4. 단위근·자기상관
- ADF 검정 통과 (p<0.05) 또는 차분 사용 명시
- DW stat ∈ [1.5, 2.5] 또는 HAC 사용 명시

위반 시 `needs_refinement` (Analyzer 재호출).

---

## 2. 재현성 검증 (★ 가장 중요)

핸드오버 §3-9: "재현 실패가 가장 흔한 이유". 다음 7 단계로 검증:

### 2-1. 격리된 환경에서 재실행
```bash
cd workspace/intermediate/{task_ref}
uv venv .verify_venv
.verify_venv/bin/pip install -r requirements.txt 2>/dev/null || \
    .verify_venv/bin/pip install pandas numpy statsmodels scipy
.verify_venv/bin/python analyze.py
```

### 2-2. 결과 비교
```python
import json
orig = json.loads(Path("result.json").read_text())
rerun = json.loads(Path(".rerun.result.json").read_text())
for key in ["coefficient", "p_value", "n_obs"]:
    assert abs(orig[key] - rerun[key]) < 1e-6, f"{key} 불일치: {orig[key]} vs {rerun[key]}"
```

### 2-3. 재현 실패 처리
```python
session.add(Verdict(decision="rejected", reasoning="재현 불가 — orig vs rerun 차이",
                    reproduced=False, ...))
session.execute(update(Analysis).where(...).values(status="irreproducible"))
alert("재현 실패: " + analysis_id, level="warn")
```

### 2-4. 재현 가능 — 단순 통과 X
재현 가능 + 통계 통과 + 메커니즘 OK 셋 모두 충족 시에만 `significant`.

---

## 3. 메커니즘 / 외생성 점검

### 3-1. 인과 방향 확인
- Y → X 가능성 (역인과) 점검
- 예: "외국인 지분율 ↑ → NAV 할인율 ↓" vs "NAV 할인율 ↓ → 외국인 매수" — 후자 가능
- 가능하면 lag 변수 사용 결과 인용

### 3-2. 누락변수 (Confounder) 점검
- 거시변수 통제 (KOSPI 수익률, 환율, 금리) 했는가?
- 시기 더미 (분기/연도 fixed effects) 했는가?
- 안 했으면 `needs_refinement` (Analyzer 재호출, 추가 통제 변수 요청)

### 3-3. 메커니즘 그럴듯함
가설의 `causal_claim` 의 메커니즘이 결과 방향과 부호가 일치하는지 확인.
- 가설: 외국인 지분율 ↑ → 거버넌스 기대 ↑ → 할인율 ↓ (부호: 음)
- 결과 부호: 음 ✅ 일치
- 결과 부호: 양 ❌ 메커니즘 불일치 → `rejected` (또는 새 가설로 재해석)

---

## 4. 강건성 (Robustness)

다음 중 **3 개 이상 통과** 시에만 `significant` 후보:

| 강건성 항목 | 통과 기준 |
|---|---|
| 시기 분할 (pre/post 사건일 또는 절반) | 두 시기 모두 부호 동일 + 양쪽 p<0.10 |
| 영향 관측치 제거 (DFFITS > 2/sqrt(n)) | coefficient 부호·크기 ±20% 내 유지 |
| 표본 변경 (KOSPI200만, 시총 상위만) | 부호 동일 |
| 대안 정의 (factor 정의 변경) | 부호 동일 |
| 대안 모델 (lag 차이, 기간 차이) | p<0.10 |
| HAC vs OLS 표준오차 | 둘 다 p<0.10 |

---

## 5. Finding 으로 승격 (significant 시)

```python
session.add(Finding(
    finding_id=f"F-{ulid}",
    hypothesis_id=hypothesis.hypothesis_id,
    summary=(
        f"{factor_name} 1{unit} 증가 시 NAV 할인율 {coef:+.2f}%p "
        f"(p={p:.3f}, n={n}, 강건성 {robust_pass}/{robust_total})"
    ),
    significance=abs(coef) / std_error,  # t-stat 절대값
))
```

### Finding summary 작성 원칙
- 1~2 문장 (평서문)
- 부호 + 크기 + p + n + 강건성 카운트
- 추측·해석 금지 (사람이 finding_provenance 로 후속 해석)

---

## 6. 인접 행동 (rejected / needs_refinement 시)

### 6-1. rejected
```python
session.add(Verdict(decision="rejected", reasoning="...", ...))
session.execute(update(Hypothesis).where(...).values(
    status="rejected",
    rejection_reason="p=0.34 (보정 후 0.78), 효과 크기 미미 (-0.01%p/1%p)",
))
```

`rejection_reason` 은 **Hypothesis 가 다음 사이클에 파생 가설을 만들 때의 입력**이 됨. 구체적이어야 함:
- ❌ "유의하지 않음"
- ✅ "lag1~5 모두 p>0.3, 시기 분할 시 부호 반전, 별도 매크로 통제 필요"

### 6-2. needs_refinement
```python
session.execute(update(Hypothesis).where(...).values(
    status="needs_refinement",
    rejection_reason="추가 강건성 필요 (HAC 통과 X)" + "@ analyzer",
))
# `@ analyzer` 형식으로 어느 에이전트로 돌려보낼지 명시
```

| 라우팅 | 의미 |
|---|---|
| `@ collector` | 데이터 부족·품질 문제, 새 데이터 수집 필요 |
| `@ analyzer` | 분석 방법 부적절, 추가 강건성 필요 |
| `@ hypothesis` | 가설 자체가 모호, 변수 재정의 필요 |

---

## 7. Verifier 가 자기 자신을 점검 (메타 체크)

분기마다 (또는 50 verdict 마다) Verifier 는:
1. `verdicts` 의 decision 분포 확인 — 한쪽으로 너무 치우침 (모두 reject 또는 모두 significant) 시 자체 기준 점검
2. significant 판정한 finding 의 사후 재현 — 1주일 뒤 같은 데이터로 재확인
3. 본 체크리스트 갱신 제안을 `docs/decisions/` 에 ADR 로 작성

---

## 8. 비용·시간 한도

- Verifier 1 회 호출당 max-turns 20 (재현은 시간 걸림)
- 재현 실행 시 timeout 10분 — 초과 시 `needs_refinement` + `@ analyzer` (분석이 너무 느림)

---

## 9. 흔한 오판 패턴

| 패턴 | 대응 |
|---|---|
| p=0.049 만 보고 significant 처리 | 강건성 3개 통과 + 효과 크기 확인 |
| n<100 인데 유의 | 표본 부족 → needs_refinement (more data) |
| 다중 변수 추가 후 p=0.04 → significant | data snooping 의심 → 사전 등록 모델만 |
| event study 에서 사건 직전 (-1, 0) 부터 효과 보임 | 정보 누출 가능 → 메커니즘 재검토 |
| factor 결측이 50% 이상 | 데이터 품질 결함 → @ collector |

---

## 10. 마지막 한 줄

> **회의주의자처럼 보고, 명확한 증거에만 significant 라고 판정하라.**
> 본 시스템은 SK PR/IR 에 납품되며, **잘못된 finding 1 개가 다른 99 개의 신뢰를 무너뜨린다**.
