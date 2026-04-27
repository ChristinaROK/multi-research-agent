# 04. 분석 방법 카탈로그 — Analyzer 전용

> Analyzer 가 수집된 데이터셋을 분석할 때 따르는 방법 카탈로그.
> 각 방법은 **(언제 쓰는가, 코드 템플릿, 출력 metrics, 한계)** 로 구성.
> 모든 분석 스크립트는 **재현 가능** 해야 합니다 — Verifier 가 같은 시드 + 같은 입력으로 다시 돌렸을 때 동일 결과가 나와야 함.

---

## 0. 공통 원칙

### 0-1. 스크립트 위치
- 분석 스크립트: `workspace/intermediate/{task_ref}/analyze.py`
- 결과 metrics JSON: `workspace/intermediate/{task_ref}/result.json`
- Verifier 가 다시 돌릴 때 같은 위치에서 실행

### 0-2. 절대경로 사용
임시 파일 (`/tmp/...`) 금지. 모든 입력·출력은 `workspace/` 절대경로 (핸드오버 §3-9):
```python
from pathlib import Path
WS = Path(__file__).resolve().parents[2] / "workspace"
df = pd.read_csv(WS / "data" / "processed" / "foreign_holding_pct.csv")
```

### 0-3. 결정성 (Determinism)
- 모든 random 함수에 seed: `np.random.seed(42)`, `random.seed(42)`
- pandas dtype 명시 (`dtype={"date": str}`)
- 정렬 후 처리 (`df.sort_values("date")`)

### 0-4. metrics JSON 표준
```json
{
  "method": "ols",
  "n_obs": 1832,
  "n_params": 3,
  "coefficient": -0.18,
  "std_error": 0.07,
  "t_stat": -2.57,
  "p_value": 0.0102,
  "r2": 0.043,
  "adj_r2": 0.041,
  "test_robustness": {"hac_p_value": 0.018, "white_p_value": 0.012},
  "diagnostics": {"adf_p_value": 0.001, "dw_stat": 1.92}
}
```

### 0-5. 분석 결과 등록
```python
session.add(Analysis(
    analysis_id=f"A-{ulid}",
    hypothesis_id=task.hypothesis_id,
    method="ols",
    script_path="workspace/intermediate/{task_ref}/analyze.py",
    result_summary="외국인 지분율 1%p 증가 시 NAV 할인율 -0.18%p (p=0.010, n=1832)",
    metrics={...},
    status="complete",
))
```

---

## A. OLS 회귀 — 단순 인과 추정

**언제**: 단일 변수 ~ NAV 할인율, 시계열 일별/월별 빈도.

```python
import pandas as pd, numpy as np, statsmodels.api as sm

df = pd.read_csv(...)  # date, factor_value, discount_rate
df = df.dropna().sort_values("date")

X = sm.add_constant(df["factor_value"])
y = df["discount_rate"]
model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})

metrics = {
    "method": "ols_hac",
    "coefficient": float(model.params.iloc[1]),
    "std_error": float(model.bse.iloc[1]),
    "t_stat": float(model.tvalues.iloc[1]),
    "p_value": float(model.pvalues.iloc[1]),
    "r2": float(model.rsquared),
    "n_obs": int(model.nobs),
}
```

### 한계
- 동시성 문제 — lag 변수 사용으로 완화
- 비정상성 → ADF 검정 + 차분 고려

## B. 차분/변화율 회귀 — 비정상성 처리

```python
df["d_factor"] = df["factor_value"].diff()
df["d_discount"] = df["discount_rate"].diff()
# OLS 와 동일하게 진행
```

**언제**: ADF 검정 결과 단위근 존재 (p > 0.05).

## C. 분포 lag 모델 (lag 1~5)

```python
for lag in range(1, 6):
    df[f"factor_lag{lag}"] = df["factor_value"].shift(lag)
X = sm.add_constant(df[[f"factor_lag{i}" for i in range(1, 6)]].dropna())
y = df.loc[X.index, "discount_rate"]
model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
```

**언제**: 즉시 효과보다 지연 효과를 보는 가설 (정책, 등급 변동).

## D. Event Study — 이벤트 효과 (자사주 매입, 등급 변동)

```python
def event_window(df, event_dates, pre=10, post=30):
    rows = []
    for ed in event_dates:
        window = df[(df.date >= ed - pd.Timedelta(days=pre*2)) &
                    (df.date <= ed + pd.Timedelta(days=post*2))]
        # 거래일 기준 -pre ~ +post
        ...
        rows.append(window)
    return pd.concat(rows)

# 평균 누적 초과수익률 (CAR) 계산
# 시장 모델: ret_i = α + β * ret_market + ε
# CAR = Σ ε_t (t = 0..post)
```

**Metrics**: `car_mean`, `car_std`, `car_t_stat`, `car_p_value`, `n_events`.

**언제**: 자사주 매입 발표, 등급 변동, 정책 발표 등 이산 이벤트.

## E. Panel 회귀 — 다회사 비교 단면

```python
import linearmodels as lm
panel = df.set_index(["company", "date"])
model = lm.PanelOLS(panel["discount"], panel[["factor"]],
                    entity_effects=True, time_effects=True).fit()
```

**언제**: 한국 지주사 5~10 개 패널 비교. SK 단독으론 변동만 보지만 패널은 단면 차이도 본다.

## F. 그랜저 인과성 검정

```python
from statsmodels.tsa.stattools import grangercausalitytests
result = grangercausalitytests(df[["discount", "factor"]], maxlag=10)
```

**언제**: "X 가 Y 를 인과한다" 의 시간적 선행성 입증 시도.
**한계**: 인과 자체를 입증 못 함. 상관·시간선행성만.

## G. 비선형 / 임계 효과

```python
# 사외이사 비율 30% 이상에서 효과가 다른가?
df["high_indep"] = (df["independent_director_ratio"] > 0.3).astype(int)
df["interaction"] = df["high_indep"] * df["factor"]
X = sm.add_constant(df[["factor", "high_indep", "interaction"]])
model = sm.OLS(df["discount"], X).fit()
```

**언제**: 가설이 비대칭/임계 효과를 주장.

## H. 다중 비교 보정 (Bonferroni / FDR)

본 시스템은 50+ 가설을 병렬 테스트하므로 **개별 가설의 p<0.05 만으론 유의 판정 못 함**.

```python
from statsmodels.stats.multitest import multipletests
pvals = [...]  # 모든 활성 가설의 p value
reject, pvals_corrected, _, _ = multipletests(pvals, alpha=0.05, method="fdr_bh")
```

Verifier 가 가설 묶음 단위로 적용. Analyzer 는 raw p_value 만 보고.

---

## I. 분석 절차 표준 (Analyzer 가 따라야 할 7 단계)

각 가설에 대해:

1. **데이터 로드** — `datasets.file_path` 에서 read_csv. dtype 명시.
2. **결합** — 가설이 다중 변수를 요구하면 date 키로 merge. inner join 후 결측 비율 보고.
3. **EDA** — 분포 (mean, std, min, max), 시계열 plot 저장 (`workspace/intermediate/{task_ref}/plots/*.png`)
4. **단위근 검정** — ADF, KPSS. 결과에 따라 차분 여부 결정.
5. **회귀/이벤트** — A~G 중 가설에 맞는 방법 선택.
6. **강건성** — HAC 표준오차, 시기 분할 (pre-2022 / post-2022), 영향 관측치 제거 후 재실행.
7. **결과 저장** — metrics JSON + 1줄 result_summary + Analysis 테이블 INSERT.

---

## J. 결과 해석 규칙 (Analyzer → Verifier 핸드오프)

Analyzer 는 **판단하지 않습니다** — 사실만 보고합니다.

❌ "외국인 지분율은 NAV 할인율에 유의한 영향을 미친다" (판단)
✅ "외국인 지분율 1%p 증가에 NAV 할인율 -0.18%p, p=0.010, n=1832, HAC 강건성 통과, ADF 통과" (사실)

판정(`significant` / `rejected`)은 Verifier 가 함.

---

## K. 흔한 함정 (피하세요)

### K-1. 자기상관 무시
일별 시계열에서 표준 OLS 의 t-stat 은 과장됨. **HAC (Newey-West) 또는 클러스터드 standard error** 필수.

### K-2. data snooping
같은 데이터로 여러 모델 시도 후 best 만 보고 → p-hacking. Analyzer 는 **사전에 등록한 모델만** 시도. 추가 모델은 새 분석으로 별도 등록.

### K-3. 파일 경로 하드코딩
`/Users/heechang/...` 같은 절대경로 직접 사용 금지. `Path(__file__).resolve().parents[2]` 사용.

### K-4. `print()` 만 출력
Verifier 가 다시 돌릴 때 결과를 못 잡음. **항상 result.json 으로 저장**.

### K-5. NaN 처리 무시
inner join 후 결측 행 비율을 result_summary 에 명시. 50% 이상이면 분석 자체 fail.

---

## L. 시각화 (선택)

`matplotlib` 으로 plot 저장. Analyzer 는 출력 안 함, 파일로만 저장.
- 시계열 line plot
- scatter + regression line
- residual plot
- event study CAR plot

```python
import matplotlib.pyplot as plt
plt.figure(figsize=(10, 4))
plt.plot(df["date"], df["discount_rate"])
plt.savefig(WS / "intermediate" / task_ref / "plots" / "discount_ts.png", dpi=120)
plt.close()
```

---

## M. 분석 비용 가이드

| 케이스 | 토큰 추정 | 비용 추정 |
|---|---|---|
| 단순 OLS, 1 변수 | input ~5K, output ~3K | $0.05~$0.10 |
| Event study | input ~10K, output ~5K | $0.15~$0.25 |
| Panel + robustness | input ~20K, output ~8K | $0.30~$0.50 |
| PDF 한 권 통째 | input ~80K | $1.20+  ⚠️ 분할 필수 |

비용 추정은 호출 전에 cost 모듈로 사전 추정 (§8-4).

---

## N. 더 읽을 것
- 검증 체크리스트: `05_verification_checklist.md`
- 변수 후보: `02_factor_playbook.md`
- 도메인 함정: `01_domain_knowledge.md` §4
