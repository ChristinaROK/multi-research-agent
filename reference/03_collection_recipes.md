# 03. 데이터 수집 레시피 — Collector 전용

> Collector 가 가설을 받아 실제 데이터를 수집할 때 따르는 표준 레시피.
> 각 레시피는 **(목적, 도구, 표준 호출, 출력 위치, 빈도, 주의)** 5 요소로 구성.
> 새로운 데이터 소스가 필요하면 본 문서에 새 레시피로 추가합니다 (PR 형식).

## 0. 공통 원칙

### 0-1. 출력 디렉토리 표준
| 종류 | 위치 |
|---|---|
| 원시 다운로드(PDF/Excel/HTML 그대로) | `workspace/downloads/{source}/{YYYYMM}/{filename}` |
| 정제 CSV/Parquet | `workspace/data/processed/{factor_name}.csv` |
| 중간 가공 | `workspace/intermediate/{task_ref}/...` |

### 0-2. 원자적 쓰기
모든 다운로드 + 정제 파일은 `.tmp` 로 먼저 쓰고 검증 후 `mv` (핸드오버 §1-4):
```python
tmp = Path(f".{path.name}.tmp")
tmp.write_bytes(data)
tmp.rename(path)
```

### 0-3. Rate limit & 도메인 락
- 같은 도메인에 대한 동시 요청 금지 (한 collector 호출 안에서 순차)
- 요청 간 1~3 초 random sleep (`time.sleep(random.uniform(1, 3))`)
- HTTP 429/403 받으면 5분 cooldown + alert (warn)
- User-Agent 회전: 5~10 개 풀에서 무작위

### 0-4. 디스크 모니터링
- 다운로드 전 `shutil.disk_usage` 로 free 공간 확인. 1GB 미만이면 alert(urgent) + 거부
- `workspace/downloads/` 의 7일 이상 파일은 매 24시간마다 정리 (Supervisor 보조 작업)

### 0-5. 데이터셋 등록
모든 정제 산출물은 `datasets` 테이블에 INSERT:
```python
session.add(Dataset(
    dataset_id=f"D-{ulid}",
    hypothesis_id=task.hypothesis_id,
    file_path="workspace/data/processed/foreign_holding_pct.csv",
    source_url="...",
    source_type="api",         # api / scrape / download
    rows=len(df),
    bytes_size=path.stat().st_size,
    quality_notes="2018-01~2026-04, 결측 0건",
    status="collected",
))
```

---

## A. pykrx — 한국거래소 시계열

**설치**: `uv pip install pykrx` (Phase 2 collect extras 에 포함됨)

### A-1. SK㈜ 일별 시가총액·주가
```python
from pykrx import stock
df = stock.get_market_cap_by_date("20180101", "20260427", "034730")  # SK㈜ 종목코드
# 컬럼: 시가총액, 거래량, 거래대금, 상장주식수
df.to_csv("workspace/data/processed/sk_market_cap.csv", index_label="date")
```

### A-2. 외국인 지분율
```python
df = stock.get_exhaustion_rates_of_foreign_investment_by_date(
    "20180101", "20260427", "034730"
)
# 컬럼: 상장주식수, 보유수량, 지분율, 한도수량, 한도소진률
```

### A-3. KOSPI / KOSDAQ 인덱스
```python
df = stock.get_index_ohlcv_by_date("20180101", "20260427", "1001")  # KOSPI
```

### A-4. 자회사 시가총액 일괄
```python
TICKERS = {"SK이노베이션": "096770", "SK텔레콤": "017670",
           "SK스퀘어": "402340", "SK하이닉스": "000660", "SK바이오팜": "326030"}
for name, t in TICKERS.items():
    df = stock.get_market_cap_by_date("20180101", "20260427", t)
    df.to_csv(f"workspace/data/processed/subsidiary_{name}.csv", index_label="date")
```

### A-5. 주의
- 종목코드는 사명 변경 시 바뀜 (예: SK스퀘어는 2021년 인적분할로 신규)
- 인적분할/액면분할 시 시가총액 시계열 단절 — 어드저스트 필요
- 휴장일은 자동 제외, 결측 없음

---

## B. OpenDartReader — DART 공시 / 재무제표

**설치**: `uv pip install OpenDartReader`. 환경변수 `DART_API_KEY` 필요.

### B-1. 사업보고서 / 반기보고서 / 분기보고서
```python
import OpenDartReader
dart = OpenDartReader(os.environ["DART_API_KEY"])
df = dart.list("SK", start="2018-01-01", kind="A")  # 정기공시
# 각 보고서의 rcept_no 로 본문 다운로드
```

### B-2. 재무제표 (연결)
```python
fs = dart.finstate_all("SK", year=2024, reprt_code="11014")  # 11014 = 사업보고서
# 자산총계, 자본총계, 매출, 영업이익, 순이익 등
```

### B-3. 임원 현황 / 사외이사 비율
```python
# 사업보고서 본문에서 추출 — DART API 직접 제공 X
# rcept_no 기반 HTML 다운로드 → BeautifulSoup 으로 표 추출
```

### B-4. 주요사항 / 자사주
```python
df = dart.list("SK", kind="B", kind_detail="B001")  # 주요사항보고서 일부
# 주요 거래, 자사주 매입/소각, 합병/분할 등
```

### B-5. 주의
- API 호출 한도 (분당 100, 일별 약 20,000)
- HTML 파싱은 보고서 양식 변경에 취약 — 표 식별 시 헤더 텍스트 매칭
- 재무제표 항목 코드는 IFRS 계정 체계 (account_id 또는 account_nm)

---

## C. KOSIS / 한국은행 ECOS — 거시 통계

### C-1. ECOS API
- `https://ecos.bok.or.kr/api` — 환경변수 `ECOS_API_KEY` (선택)
- 주요 통계: 환율, 기준금리, 국고채 수익률, 통화량
- 응답: JSON 또는 XML

```python
import requests
params = {
    "auth": os.environ["ECOS_API_KEY"], "lang": "kr", "type": "json",
    "stat": "722Y001", "freq": "D",  # 환율 일별
    "start": "20180101", "end": "20260427",
}
r = requests.get("https://ecos.bok.or.kr/api/StatisticSearch", params=params)
```

### C-2. KOSIS
- `KOSIS_API_KEY` 필요
- 통계청 인구·고용·물가 등 — SK NAV 분석엔 보조적

---

## D. KCGS — 지배구조 등급

**계정 필요** (`KCGS_USERNAME`, `KCGS_PASSWORD`)

### D-1. Playwright 로그인 후 PDF 다운로드
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.cgs.or.kr/login")
    page.fill('input[name="userid"]', os.environ["KCGS_USERNAME"])
    page.fill('input[name="passwd"]', os.environ["KCGS_PASSWORD"])
    page.click('button[type="submit"]')
    page.wait_for_url("**/main")
    page.goto("https://www.cgs.or.kr/business/esg_grade.jsp?company=SK")
    # 등급 페이지에서 표 추출 또는 PDF 다운로드
    pdf_url = page.locator("a.pdf-download").get_attribute("href")
    # download_path 에 저장
    browser.close()
```

### D-2. PDF 표 추출 (pdfplumber)
```python
import pdfplumber
with pdfplumber.open(pdf_path) as pdf:
    for page in pdf.pages:
        for table in page.extract_tables():
            # 등급 테이블 식별 후 정제
```

### D-3. 주의
- KCGS 사이트는 ToS 검토 필요. **업무용 사용 가능 여부 사전 확인** (담당자: 사용자)
- 등급은 연 1회 발표 + 분기 업데이트
- robots.txt 준수, captcha 만나면 즉시 중단

---

## E. 빅카인즈 — 뉴스 데이터

**계정 필요**: 회원가입 후 API 키 또는 웹 검색

### E-1. 빅카인즈 API (가능한 경우)
- 키워드: "SK㈜", "SK주식회사", "SK 지주"
- 일별 기사 수, 매체별 분포

### E-2. 백업: Google News + RSS
- 키워드 RSS 구독: `https://news.google.com/rss/search?q=SK주식회사&hl=ko`
- 일별 카운트는 헤드라인만으로 가능

### E-3. 감성 분석
- LLM 분류 (기사 헤드라인을 Claude Haiku 로 batch 분류)
- 비용: 기사 1건당 약 $0.0001 (Haiku 4.5 기준)

---

## F. 학술 검색 — 가설 정당화 근거

### F-1. Google Scholar (Hypothesis 만 사용 추천)
- 키워드: "Korean holding company discount", "지주사 할인율", "NAV discount Korea"
- 직접 다운로드 어려움 — 제목·초록만 수집해도 충분

### F-2. KISS / RISS
- 회원가입 + Playwright 로 검색
- 한국 학술자료 (한국재무학회 등)

### F-3. SSRN
- 무료 다운로드 가능한 워킹 페이퍼
- API 또는 검색 결과 HTML 파싱

---

## G. 회사 IR / 재무 사이트

### G-1. SK㈜ IR 사이트
- `https://www.sk.com/ir/`
- 분기 IR 자료 (PDF), 재무 highlights

### G-2. 네이버 금융 / FnGuide (무료 부분)
- `https://navercomp.wisereport.co.kr/` — Player 식 데이터
- 무료 부분: 분기 손익 요약, 외국인 지분율, 컨센서스 일부

---

## H. 표준 산출물 형식 (정제 CSV)

### 시계열
```csv
date,value
2018-01-02,42500000000
2018-01-03,42600000000
...
```

### 패널 (다회사 동일 변수)
```csv
date,company,value
2018-01-02,SK,42500
2018-01-02,LG,18000
...
```

### 이벤트
```csv
event_date,event_type,description,source_url
2024-03-15,share_buyback_announcement,"자기주식 1000억원 매입 결의","https://dart.fss.or.kr/..."
```

---

## I. 실패 처리 (Collector 가 fail 할 때)

1. `datasets.status='failed'` + `quality_notes` 에 사유 (HTTP 코드, 메시지)
2. `agent_logs` 에 `event='error'` + `payload={"error": "...", "retry_count": N}`
3. 재시도 정책: 지수 백오프 (1m → 5m → 30m), 최대 3 회
4. 3 회 실패 시 Supervisor 가 `hypotheses.status='needs_refinement'` 로 변경 + reason 명시
5. Hypothesis 가 다음 사이클에 다른 데이터 소스로 파생 가설 시도

---

## J. ToS / 윤리 체크리스트

각 새 데이터 소스 사용 전 다음을 확인 (Collector 의 _self-check_):
- [ ] robots.txt 위배 없음
- [ ] 약관에 자동 수집 금지 조항 없음 (또는 명시적 허용)
- [ ] API 가 있으면 API 우선 사용 (스크레이핑 회피)
- [ ] 회원만 접근 가능 시 본 시스템에 등록된 계정 사용 (계정 공유 X)
- [ ] 대량 다운로드 (예: 일일 1000+ 요청) 는 사용자 사전 승인

위배 의심 시 Collector 는 즉시 중단 + alert(warn).
