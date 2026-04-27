# SK 기업가치 팩터 분석 — 자율 멀티에이전트 시스템 설계서

## 1. 설계 목표

밤새 무인으로 돌아가면서, 에이전트들이 스스로 가설을 세우고, 데이터를 수집하고, 분석하고, 검증하고, 실패하면 판단해서 복구하는 시스템.
각 에이전트는 최소 "인턴/주니어 리서처" 수준으로 구글 검색, PDF 다운로드 및 읽기, 웹사이트 로그인, API 호출, DB 구축 등을 자율적으로 수행할 수 있어야 함.

핵심 요구사항:
- Skip permission (모든 에이전트가 인간 승인 없이 자율 실행)
- Self-healing (에이전트 죽으면 감지 + 자동 재시작 + 실패 지점부터 재개)
- 루프형 작업 (가설 → 수집 → 분석 → 검증 → 피드백 → 새 가설... 무한 반복)
- 도구 사용 (Playwright, 파일시스템, DB 등 실제 도구를 에이전트가 직접 조작)

---

## 2. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│                    SUPERVISOR (Python)                    │
│  프로세스 감시 / 자동 재시작 / 비용 추적 / 알림          │
│  ← systemd 또는 pm2로 Supervisor 자체도 보호            │
└────┬────────────┬────────────┬────────────┬──────────────┘
     │            │            │            │
     ▼            ▼            ▼            ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│ Hypothe-│ │ Collect-│ │ Analy-  │ │ Verifi- │
│ sis     │ │ or      │ │ zer     │ │ er      │
│ Agent   │ │ Agent   │ │ Agent   │ │ Agent   │
│         │ │         │ │         │ │         │
│ Claude  │ │ Claude  │ │ Claude  │ │ Claude  │
│ Code -p │ │ Code -p │ │ Code -p │ │ Code -p │
│ --yes   │ │ --yes   │ │ --yes   │ │ --yes   │
└────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘
     │            │            │            │
     ▼            ▼            ▼            ▼
┌─────────────────────────────────────────────────────────┐
│                    TOOL LAYER                            │
│                                                          │
│  MCP Servers:                                            │
│  ├── Playwright MCP (웹 브라우징, 로그인, 크롤링)        │
│  ├── Filesystem MCP (파일 읽기/쓰기)                     │
│  ├── SQLite MCP (DB 조회/입력)                           │
│  └── Fetch MCP (HTTP API 호출)                           │
│                                                          │
│  Native Tools (Claude Code 내장):                        │
│  ├── bash (Python 스크립트 실행, pip install 등)         │
│  ├── read/write (파일 조작)                              │
│  └── web search (Anthropic 내장 웹검색)                  │
│                                                          │
│  Python Libraries (bash를 통해 실행):                    │
│  ├── pykrx (한국 주식 데이터)                            │
│  ├── OpenDartReader (DART 공시)                          │
│  ├── pandas, statsmodels (분석)                          │
│  ├── pdfplumber (PDF 텍스트 추출)                       │
│  └── playwright (프로그래밍 방식 브라우저)                │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    SHARED STATE                          │
│                                                          │
│  SQLite DB: hub.db                                       │
│  ├── hypotheses (가설 큐 + 상태 + 히스토리)              │
│  ├── collection_tasks (수집 작업 + 상태)                 │
│  ├── datasets (수집된 데이터셋 메타데이터)               │
│  ├── analyses (분석 결과)                                │
│  ├── verdicts (검증 판정)                                │
│  ├── findings (최종 발견사항)                            │
│  ├── agent_logs (에이전트별 실행 로그)                   │
│  └── cost_tracker (API 비용 누적)                        │
│                                                          │
│  File System: /workspace/shared/                         │
│  ├── data/raw/           (수집된 원시 데이터)            │
│  ├── data/processed/     (정제된 데이터)                 │
│  ├── data/downloads/     (다운로드한 PDF, Excel 등)      │
│  ├── reports/            (분석 리포트)                   │
│  └── agent_workspaces/   (에이전트별 임시 작업 공간)     │
│      ├── hypothesis/                                     │
│      ├── collector/                                      │
│      ├── analyzer/                                       │
│      └── verifier/                                       │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 각 에이전트 상세 설계

### 3.1 Hypothesis Agent (가설 생성 에이전트)

**역할**: "SK㈜ NAV 할인율에 영향을 미칠 수 있는 변수는 무엇인가?"를 끊임없이 탐색하고 가설을 생성

**인간 비유**: 시니어 리서치 애널리스트가 학술논문, 증권사 리포트, 뉴스를 읽으면서 "이 변수가 영향을 주지 않을까?" 하고 아이디어를 내는 것

**실행 방식**:
```bash
claude -p \
  "$(cat prompts/hypothesis_system.md)

현재 상태:
$(python3 scripts/get_context.py --agent hypothesis)

지시사항:
1. hub.db에서 이전 가설들과 검증 결과를 읽어라
2. 아직 탐색하지 않은 새로운 팩터 가설을 생성하라
3. 기각된 가설의 피드백을 반영해 파생 가설도 만들어라
4. 결과를 hub.db의 hypotheses 테이블에 INSERT 하라
5. 작업 완료 후 /workspace/shared/signals/hypothesis_done 파일을 생성하라" \
  --allowedTools bash,Read,Write,mcp__sqlite \
  --max-turns 30 \
  2>&1 | tee logs/hypothesis_$(date +%Y%m%d_%H%M).log
```

**도구 접근권한**:
- bash: Python 스크립트 실행 (웹검색 결과 파싱, 논문 검색 등)
- Read/Write: 파일 읽기/쓰기
- mcp__sqlite: hub.db 직접 조회/입력
- Web search: Claude Code 내장 웹검색으로 학술논문, 증권사 리포트 검색

**시스템 프롬프트 핵심 내용** (`prompts/hypothesis_system.md`):
```markdown
당신은 SK㈜ 지주사의 NAV 할인율에 영향을 미치는 팩터를 발굴하는 
리서치 애널리스트입니다.

## 가설 생성 원칙
- 기존에 검증된/기각된 가설과 중복되지 않는 새로운 변수를 탐색
- 가설은 반드시 "측정 가능한 변수"로 구체화해야 함
  (나쁜 예: "시장 심리가 영향" → 좋은 예: "VIX 지수 변동이 NAV 할인율과 상관")
- 카테고리별 균형 유지: ESG, 거버넌스, 포트폴리오사 업황, 
  거시경제, 브랜드/PR, 내부 활동

## 가설 우선순위 기준
- 데이터 수집 가능성 (공개 데이터가 있는가?)
- 인과관계의 논리적 타당성
- 기존 학술/실무 근거 유무
- 이전 분석에서 발견된 단서와의 연관성

## 출력 형식
모든 가설은 hub.db의 hypotheses 테이블에 다음 형식으로 INSERT:
- hypothesis_id: H-{순번}
- factor_name: 변수명 (영문+한글)
- factor_category: 카테고리
- causal_claim: 인과관계 주장 (1-2문장)
- data_sources: 데이터 수집 방법/출처 (JSON array)
- time_range: 분석 기간
- priority: high/medium/low
- rationale: 이 가설을 세운 근거
- status: pending

## 피드백 활용
- "rejected" 가설의 rejection_reason을 읽고, 해당 방향은 피하되
  관련된 다른 각도의 가설을 파생시킬 것
- "significant" 판정을 받은 가설의 factor_category와 유사한 
  영역에서 추가 가설을 생성할 것
```

**자율 판단이 필요한 상황과 대응**:
| 상황 | 판단 기준 | 행동 |
|------|-----------|------|
| 가설이 더 이상 안 떠오름 | 최근 5개 연속 기각 | 완전히 새로운 카테고리로 전환 |
| 학술논문을 참고하고 싶음 | 항상 허용 | 웹검색으로 Google Scholar 탐색 |
| 이전 가설과 너무 유사함 | hub.db에서 유사도 체크 | 차별화 포인트 명시하거나 스킵 |
| 분석 불가능한 변수 | 데이터 접근 불가 판단 | priority를 low로, reason 기록 |

---

### 3.2 Collector Agent (데이터 수집 에이전트)

**역할**: Hypothesis Agent가 생성한 가설에 필요한 데이터를 실제로 수집

**인간 비유**: 인턴 리서처가 "이 데이터 찾아와" 지시를 받고, 구글 검색하고, 사이트 돌아다니고, Excel 다운받고, PDF에서 표 뽑고, API 호출해서 데이터를 정리해오는 것

**실행 방식**:
```bash
claude -p \
  "$(cat prompts/collector_system.md)

수집할 가설:
$(python3 scripts/get_pending_collection.py)

지시사항:
1. 가설에 명시된 data_sources를 참고하여 실제 데이터를 수집하라
2. 수집한 데이터를 CSV/JSON으로 저장하라
3. 데이터 품질 메타데이터를 기록하라
4. 수집 불가능한 경우 그 이유를 기록하라
5. 결과를 hub.db에 업데이트하라" \
  --allowedTools bash,Read,Write,mcp__playwright,mcp__sqlite,mcp__fetch \
  --max-turns 50 \
  2>&1 | tee logs/collector_$(date +%Y%m%d_%H%M).log
```

**도구 접근권한** (가장 많은 도구가 필요):
- bash: Python 스크립트 실행의 핵심. pykrx, OpenDartReader, requests, BeautifulSoup, pdfplumber 등
- mcp__playwright: 웹 브라우징 자동화. 로그인이 필요한 사이트, JavaScript 렌더링 사이트, 다운로드 버튼 클릭 등
- mcp__fetch: 단순 HTTP GET/POST (API 호출, JSON 데이터 수집)
- mcp__sqlite: hub.db에 수집 상태 기록
- Read/Write: 수집한 파일 저장

**시스템 프롬프트 핵심** (`prompts/collector_system.md`):
```markdown
당신은 데이터 수집 전문 리서처입니다.
가설에 필요한 데이터를 실제로 수집하는 것이 임무입니다.

## 수집 전략 (순서대로 시도)

### 1순위: 프로그래밍 API
- pykrx: 한국 주식 시세, 시가총액, PER, PBR 등
  ```python
  from pykrx import stock
  df = stock.get_market_ohlcv("20150101", "20251231", "034730")  # SK㈜
  ```
- OpenDartReader: DART 공시 (지분율 변동, 사업보고서 등)
  ```python
  import OpenDartReader
  dart = OpenDartReader(API_KEY)
  df = dart.list("SK", start="2015-01-01")
  ```
- FRED API: 거시경제 지표 (금리, 환율, VIX 등)
- 한국은행 API: 국내 거시 지표

### 2순위: 웹 크롤링
- requests + BeautifulSoup: 정적 페이지
- Playwright MCP: 동적 페이지, 로그인 필요 사이트
- 대상: 뉴스 기사, ESG 등급 사이트, 증권사 리포트

### 3순위: 파일 다운로드 + 파싱
- PDF 다운로드 → pdfplumber로 텍스트/표 추출
- Excel 다운로드 → pandas로 파싱
- 대상: 한국기업지배구조원(KCGS) ESG 보고서, 
  한국IR협의회 자료, SK 연차보고서

### 4순위: 수동 데이터 입력 요청
- 위 방법으로 수집 불가능한 경우
- status를 "needs_manual_input"으로 표시
- 필요한 데이터의 정확한 명세를 기록

## 데이터 저장 규칙
- 원시 데이터: /workspace/shared/data/raw/{factor_name}/
- 정제 데이터: /workspace/shared/data/processed/{factor_name}.csv
- 다운로드 파일: /workspace/shared/data/downloads/
- 모든 CSV는 첫 번째 컬럼이 date (YYYY-MM-DD 형식)
- 메타데이터를 hub.db datasets 테이블에 기록:
  - dataset_id, hypothesis_id, file_path
  - source_url, collection_method
  - date_range, row_count, column_list
  - quality_score (0-100), quality_notes

## 오류 처리
- 사이트 접속 불가 → 3회 재시도 후 실패 기록
- 데이터 형식 불일치 → 가능한 범위까지 파싱 후 quality_notes에 기록
- 로그인 필요 → 환경변수에서 credentials 확인, 없으면 needs_credentials 표시
- Rate limit → 지수 백오프 적용
```

**Playwright MCP 활용 시나리오**:
```
시나리오 1: KCGS ESG 등급 수집
1. Playwright로 kcgs.or.kr 접속
2. ESG 평가 결과 페이지 네비게이션
3. SK㈜ 검색
4. 연도별 등급 테이블 스크레이핑
5. CSV로 저장

시나리오 2: 증권사 리포트 수집
1. 증권사 리서치 페이지 접속
2. "SK 지주" 키워드 검색
3. PDF 리포트 다운로드
4. pdfplumber로 텍스트 추출
5. 목표주가, 투자의견 파싱

시나리오 3: 뉴스 톤 데이터 수집
1. 네이버 뉴스 검색 ("SK그룹" OR "SK㈜")
2. 기간별 기사 크롤링
3. 제목 + 본문 추출
4. 날짜별로 정리하여 CSV 저장
```

---

### 3.3 Analyzer Agent (분석 에이전트)

**역할**: 수집된 데이터로 통계 분석을 수행하여 가설을 검정

**인간 비유**: 퀀트 애널리스트가 데이터를 받아서 상관분석, 회귀분석, 이벤트 스터디를 돌리고 결과를 리포트로 정리하는 것

**실행 방식**:
```bash
claude -p \
  "$(cat prompts/analyzer_system.md)

분석할 가설과 데이터:
$(python3 scripts/get_pending_analysis.py)

지시사항:
1. 수집된 데이터를 로드하고 전처리하라
2. 가설에 적합한 분석 방법을 선택하여 실행하라
3. 분석 결과를 구조화된 JSON으로 hub.db에 저장하라
4. 분석 코드를 재현 가능하게 Python 스크립트로 저장하라" \
  --allowedTools bash,Read,Write,mcp__sqlite \
  --max-turns 40 \
  2>&1 | tee logs/analyzer_$(date +%Y%m%d_%H%M).log
```

**도구 접근권한**:
- bash: pandas, statsmodels, scipy, matplotlib 등 Python 분석 라이브러리 실행
- Read/Write: 데이터 파일 읽기, 분석 스크립트/차트 저장
- mcp__sqlite: hub.db에 분석 결과 기록

**시스템 프롬프트 핵심** (`prompts/analyzer_system.md`):
```markdown
당신은 계량분석 전문가입니다.
수집된 데이터로 가설을 통계적으로 검정하는 것이 임무입니다.

## 분석 방법 선택 기준

가설 유형에 따라 적절한 분석 방법을 자율적으로 선택:

### 연속 변수 간 관계 (예: VIX vs NAV 할인율)
- Pearson/Spearman 상관계수
- 시차 교차상관 (lag 0~12개월)
- Granger 인과성 검정
- VAR 모델

### 이벤트 효과 (예: ESG 등급 변동 이벤트)
- 이벤트 스터디: 이벤트 전후 CAR (누적 비정상 수익률)
- 이벤트 윈도우: [-5, +20] 거래일
- 시장 모델 또는 시장 조정 모델

### 다변량 회귀 (여러 팩터 동시 분석)
- OLS 회귀분석
- VIF로 다중공선성 체크
- Heteroscedasticity 검정 (White test)
- Autocorrelation 검정 (Durbin-Watson)

### 구조 변화 (예: 지배구조 개편 전후)
- Chow test
- CUSUM test
- 구간별 회귀 비교

## 전처리 규칙
- 결측치: 먼저 보간 가능 여부 판단, 불가능하면 해당 기간 제외
- 이상치: IQR 기반 탐지, 제거 전후 결과 모두 보고
- 정상성: ADF test로 단위근 검정, 필요시 차분
- 빈도 통일: 모든 시계열을 동일 빈도(일/주/월)로 맞춤

## 출력 형식
hub.db의 analyses 테이블에 INSERT:
- analysis_id: A-{순번}
- hypothesis_id: 연결된 가설
- method: 사용한 분석 방법
- results_json: 구조화된 결과
  {
    "coefficient": 0.42,
    "p_value": 0.003,
    "r_squared": 0.18,
    "confidence_interval": [0.15, 0.69],
    "sample_size": 120,
    "interpretation": "1문장 해석"
  }
- script_path: 재현용 Python 스크립트 경로
- chart_path: 시각화 차트 경로
- status: completed/failed
- notes: 분석 과정에서의 특이사항
```

---

### 3.4 Verifier Agent (검증 에이전트)

**역할**: 분석 결과의 통계적/논리적 타당성을 검증하고 최종 판정

**인간 비유**: 시니어 리서처가 주니어의 분석 결과를 리뷰하면서 "이거 다중공선성 체크했어?", "샘플 크기가 너무 작지 않아?", "이건 허위상관 아니야?" 하고 까다롭게 검증하는 것

**실행 방식**:
```bash
claude -p \
  "$(cat prompts/verifier_system.md)

검증할 분석 결과:
$(python3 scripts/get_pending_verification.py)

지시사항:
1. 분석 코드를 직접 실행하여 결과를 재현하라
2. 통계적 검증 체크리스트를 수행하라
3. 판정(significant/rejected/needs_refinement)을 내려라
4. 기각 시 구체적인 이유와 개선 방향을 기록하라" \
  --allowedTools bash,Read,Write,mcp__sqlite \
  --max-turns 30 \
  2>&1 | tee logs/verifier_$(date +%Y%m%d_%H%M).log
```

**시스템 프롬프트 핵심** (`prompts/verifier_system.md`):
```markdown
당신은 통계 분석 결과를 검증하는 시니어 리뷰어입니다.
분석이 올바른지, 결론이 타당한지를 까다롭게 검증하는 것이 임무입니다.

## 검증 체크리스트 (모든 항목 필수)

### 1. 재현성 검증
- [ ] 분석 스크립트를 직접 실행하여 동일 결과가 나오는지 확인
- [ ] 사용된 데이터 파일이 존재하고 무결한지 확인

### 2. 통계적 타당성
- [ ] p-value < 0.05 여부 (유의수준)
- [ ] 샘플 크기 충분성 (최소 30개, 권장 60개 이상)
- [ ] 다중공선성 검정 (VIF > 10이면 문제)
- [ ] 잔차의 정규성 검정
- [ ] 이분산성 검정
- [ ] 자기상관 검정

### 3. 논리적 타당성
- [ ] 상관관계 ≠ 인과관계: 제3변수(confounding) 가능성 검토
- [ ] 시간 순서: 원인이 결과보다 시간적으로 선행하는가?
- [ ] 허위상관(spurious correlation) 가능성
- [ ] 경제적/비즈니스적으로 설명 가능한 메커니즘이 있는가?

### 4. 강건성 검정
- [ ] 분석 기간을 바꿔도 결과가 유지되는가?
- [ ] 이상치를 제거해도 결과가 유지되는가?
- [ ] 다른 분석 방법을 적용해도 유사한 결론인가?

## 판정 기준

### "significant" (유의미)
- 통계적 유의 (p < 0.05)
- 경제적 유의 (효과 크기가 실질적으로 의미 있음)
- 재현 가능
- 논리적 메커니즘 존재
→ findings 테이블에 등록

### "rejected" (기각)
- 통계적 비유의 (p >= 0.05) 또는
- 허위상관으로 판단 또는
- 재현 불가 또는
- 경제적으로 무의미한 효과 크기
→ rejection_reason과 함께 가설 상태를 rejected로 변경

### "needs_refinement" (수정 필요)
- 부분적으로 유의하지만 추가 검증 필요
- 분석 기간이나 변수 정의를 수정하면 개선 가능성 있음
→ feedback을 작성하여 hypothesis agent에 전달
→ 가설 상태를 refining으로 변경
```

---

## 4. Supervisor (자율 운영 및 자가 치유)

Supervisor는 에이전트가 아니라 Python 데몬 프로세스.
모든 에이전트의 생사를 감시하고, 죽으면 살리고, 비용을 추적하고, 알림을 보냄.

### 4.1 프로세스 관리

```python
# supervisor.py 핵심 로직 (의사코드)

class AgentSupervisor:
    """
    에이전트 프로세스의 생존을 감시하고
    실패 시 자동 복구하는 데몬
    """
    
    AGENT_SEQUENCE = ["hypothesis", "collector", "analyzer", "verifier"]
    MAX_CONSECUTIVE_FAILURES = 3
    COOLDOWN_SECONDS = 60
    MAX_COST_USD = 100  # 1회 실행 비용 한도
    
    def run_loop(self):
        """메인 루프: 에이전트를 순차적으로 실행"""
        while True:
            for agent_name in self.AGENT_SEQUENCE:
                # 해당 에이전트가 처리할 작업이 있는지 확인
                if not self.has_pending_work(agent_name):
                    self.log(f"{agent_name}: 대기 중인 작업 없음, 스킵")
                    continue
                
                # 에이전트 실행
                success = self.run_agent_with_retry(agent_name)
                
                if not success:
                    self.handle_persistent_failure(agent_name)
                
                # 비용 체크
                if self.get_total_cost() > self.MAX_COST_USD:
                    self.alert("비용 한도 도달, 일시 중단")
                    self.wait_for_human()
                    return
                
                # 쿨다운 (rate limit 방지)
                time.sleep(self.COOLDOWN_SECONDS)
            
            # 한 사이클 완료 후 상태 리포트
            self.send_cycle_report()
            
            # 새 가설이 없으면 hypothesis agent 다시 호출
            if not self.has_pending_hypotheses():
                self.run_agent_with_retry("hypothesis")
    
    def run_agent_with_retry(self, agent_name, max_retries=3):
        """에이전트 실행 + 실패 시 재시도"""
        for attempt in range(max_retries):
            try:
                result = self.execute_agent(agent_name)
                
                # 성공 판단: 시그널 파일이 생성되었는가?
                if self.check_completion_signal(agent_name):
                    self.log(f"{agent_name}: 성공 (시도 {attempt+1})")
                    self.reset_failure_count(agent_name)
                    return True
                
                # 시그널 없이 종료 = 비정상 종료
                self.log(f"{agent_name}: 비정상 종료 (시도 {attempt+1})")
                
            except subprocess.TimeoutExpired:
                self.log(f"{agent_name}: 타임아웃 (시도 {attempt+1})")
                self.kill_agent(agent_name)
                
            except Exception as e:
                self.log(f"{agent_name}: 예외 발생 - {e} (시도 {attempt+1})")
            
            # 재시도 전 대기 (지수 백오프)
            time.sleep(30 * (attempt + 1))
        
        return False
    
    def handle_persistent_failure(self, agent_name):
        """연속 실패 시 판단"""
        self.increment_failure_count(agent_name)
        
        if self.get_failure_count(agent_name) >= self.MAX_CONSECUTIVE_FAILURES:
            # 해당 에이전트의 현재 태스크를 "failed"로 마킹
            self.mark_current_task_failed(agent_name)
            # 다음 태스크로 넘어감
            self.skip_to_next_task(agent_name)
            # 알림
            self.alert(f"{agent_name}: {self.MAX_CONSECUTIVE_FAILURES}회 연속 실패. "
                      f"현재 태스크 스킵하고 다음으로 진행.")
            self.reset_failure_count(agent_name)
    
    def execute_agent(self, agent_name):
        """Claude Code를 subprocess로 실행"""
        cmd = self.build_agent_command(agent_name)
        
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=f"/workspace/shared/agent_workspaces/{agent_name}"
        )
        
        # 타임아웃: 에이전트당 최대 15분
        stdout, _ = process.communicate(timeout=900)
        
        # 로그 저장
        self.save_log(agent_name, stdout.decode())
        
        return process.returncode
```

### 4.2 자가 치유 메커니즘

```
실패 유형별 자동 대응:

┌────────────────────────┬──────────────────────────────────┐
│ 실패 유형              │ 자동 대응                         │
├────────────────────────┼──────────────────────────────────┤
│ 에이전트 프로세스 크래시│ 최대 3회 재시작                   │
│                        │ 3회 실패 시 해당 태스크 스킵       │
├────────────────────────┼──────────────────────────────────┤
│ 타임아웃 (15분 초과)   │ 프로세스 kill → 재시작            │
│                        │ 태스크를 더 작은 단위로 분할 시도  │
├────────────────────────┼──────────────────────────────────┤
│ API rate limit         │ 지수 백오프 대기 (30s→60s→120s)  │
│                        │ 3회 후에도 안 되면 다음 태스크     │
├────────────────────────┼──────────────────────────────────┤
│ 웹사이트 접속 불가     │ 3회 재시도 → 대체 소스 탐색       │
│                        │ 대체 소스도 없으면 "data_unavail" │
├────────────────────────┼──────────────────────────────────┤
│ 데이터 파싱 오류       │ 에러 로그 저장 → 스킵 → 계속      │
├────────────────────────┼──────────────────────────────────┤
│ SQLite lock            │ 대기 후 재시도 (WAL 모드 사용)    │
├────────────────────────┼──────────────────────────────────┤
│ 디스크 공간 부족       │ 알림 → 오래된 로그 자동 삭제      │
├────────────────────────┼──────────────────────────────────┤
│ 비용 한도 도달         │ 즉시 중단 → 알림 → 인간 대기      │
└────────────────────────┴──────────────────────────────────┘
```

### 4.3 완료 신호 (Signal) 메커니즘

에이전트가 정상 완료되었는지 판단하는 방법:

```
각 에이전트는 작업 완료 시 시그널 파일을 생성:

/workspace/shared/signals/
├── hypothesis_done      ← Hypothesis Agent 완료 시 생성
├── collector_done       ← Collector Agent 완료 시 생성
├── analyzer_done        ← Analyzer Agent 완료 시 생성
└── verifier_done        ← Verifier Agent 완료 시 생성

시그널 파일 내용 (JSON):
{
  "agent": "collector",
  "timestamp": "2026-04-27T03:42:00Z",
  "tasks_completed": 3,
  "tasks_failed": 1,
  "next_action": "analyzer에 3개 분석 태스크 준비됨"
}

Supervisor는 시그널 파일을 확인한 후 삭제하고 다음 에이전트를 호출.
시그널 파일이 없이 에이전트가 종료되면 → 비정상 종료로 판단 → 재시작.
```

---

## 5. 에이전트 간 데이터 흐름

```
Hypothesis Agent                    Collector Agent
    │                                    │
    │  hypotheses 테이블에 INSERT         │
    │  status: "pending"                 │
    │                                    │
    ├───────────────────────────────────→ │
    │                                    │  hypotheses에서 pending 읽기
    │                                    │  데이터 수집 수행
    │                                    │  datasets 테이블에 INSERT
    │                                    │  hypotheses.status → "collected"
    │                                    │
    │                              Analyzer Agent
    │                                    │
    │                                    │  "collected" 상태 가설 읽기
    │                                    │  datasets에서 데이터 로드
    │                                    │  분석 수행
    │                                    │  analyses 테이블에 INSERT
    │                                    │  hypotheses.status → "analyzed"
    │                                    │
    │                              Verifier Agent
    │                                    │
    │                                    │  "analyzed" 상태 가설 읽기
    │                                    │  analyses 결과 검증
    │                                    │  verdicts 테이블에 INSERT
    │                                    │
    │           ┌────────────────────────┤
    │           │                        │
    │    [significant]             [rejected]
    │           │                        │
    │    findings에 등록          가설 아카이브
    │                                    │
    │           │                        │
    │    [needs_refinement]              │
    │           │                        │
    │    feedback과 함께          rejection_reason 기록
    │    hypotheses에 파생가설 생성       │
    │           │                        │
    ◄───────────┘                        │
    │                                    │
    │  피드백을 읽고 새 가설 생성         │
    │  (루프 계속)                        │
```

---

## 6. 디렉토리 구조

```
/workspace/
├── supervisor.py                    ← 메인 데몬 (이것만 실행하면 전체 시작)
├── hub.db                           ← SQLite 공유 상태
├── .env                             ← API 키, 크리덴셜
│
├── prompts/                         ← 에이전트별 시스템 프롬프트
│   ├── hypothesis_system.md
│   ├── collector_system.md
│   ├── analyzer_system.md
│   └── verifier_system.md
│
├── scripts/                         ← 헬퍼 스크립트
│   ├── init_db.py                   ← hub.db 스키마 초기화
│   ├── get_context.py               ← 에이전트에 전달할 컨텍스트 추출
│   ├── get_pending_collection.py    ← 수집 대기 태스크 추출
│   ├── get_pending_analysis.py      ← 분석 대기 태스크 추출
│   ├── get_pending_verification.py  ← 검증 대기 태스크 추출
│   ├── cost_tracker.py              ← API 비용 추적
│   └── alert.py                     ← Telegram/Teams 알림
│
├── schemas/                         ← JSON 스키마 (데이터 계약)
│   ├── hypothesis.schema.json
│   ├── dataset.schema.json
│   ├── analysis.schema.json
│   └── verdict.schema.json
│
├── shared/                          ← 에이전트 간 공유 파일시스템
│   ├── data/
│   │   ├── raw/                     ← 원시 수집 데이터
│   │   ├── processed/               ← 정제 데이터
│   │   └── downloads/               ← PDF, Excel 등
│   ├── reports/                     ← 분석 리포트, 차트
│   ├── signals/                     ← 완료 시그널 파일
│   └── agent_workspaces/            ← 에이전트별 작업 디렉토리
│       ├── hypothesis/
│       ├── collector/
│       ├── analyzer/
│       └── verifier/
│
├── logs/                            ← 에이전트 실행 로그
│   ├── supervisor.log
│   ├── hypothesis_20260427_0100.log
│   ├── collector_20260427_0115.log
│   └── ...
│
├── configs/                         ← 설정
│   ├── agent_config.yaml            ← 에이전트별 설정
│   └── sk_portfolio.yaml            ← SK 포트폴리오사 목록, 지분율
│
└── mcp_servers/                     ← MCP 서버 설정
    ├── playwright_config.json
    ├── sqlite_config.json
    └── fetch_config.json
```

---

## 7. MCP 서버 설정

### 7.1 Claude Code 설정 파일

`.claude/settings.json` 또는 프로젝트별 `.mcp.json`:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@anthropic/mcp-playwright"],
      "env": {
        "PLAYWRIGHT_HEADLESS": "true"
      }
    },
    "sqlite": {
      "command": "npx",
      "args": ["@anthropic/mcp-sqlite", "/workspace/hub.db"]
    },
    "fetch": {
      "command": "npx",
      "args": ["@anthropic/mcp-fetch"]
    },
    "filesystem": {
      "command": "npx",
      "args": [
        "@anthropic/mcp-filesystem",
        "/workspace/shared"
      ]
    }
  }
}
```

### 7.2 에이전트별 도구 접근 제어

```yaml
# configs/agent_config.yaml

agents:
  hypothesis:
    allowed_tools:
      - bash
      - Read
      - Write
      - mcp__sqlite      # hub.db 접근
    max_turns: 30
    timeout_seconds: 600
    description: "가설 생성 — 웹검색, DB 읽기/쓰기만 허용"

  collector:
    allowed_tools:
      - bash
      - Read
      - Write
      - mcp__playwright   # 웹 브라우징
      - mcp__sqlite       # hub.db 접근
      - mcp__fetch        # HTTP API 호출
      - mcp__filesystem   # 파일 저장
    max_turns: 50
    timeout_seconds: 900
    description: "데이터 수집 — 모든 도구 접근 허용 (가장 위험, 로그 필수)"

  analyzer:
    allowed_tools:
      - bash
      - Read
      - Write
      - mcp__sqlite
    max_turns: 40
    timeout_seconds: 900
    description: "분석 수행 — Python 통계 라이브러리 + DB만 허용"

  verifier:
    allowed_tools:
      - bash
      - Read
      - Write
      - mcp__sqlite
    max_turns: 30
    timeout_seconds: 600
    description: "검증 — 분석 코드 재실행 + DB 판정 기록만 허용"

supervisor:
  max_cost_usd: 100
  cooldown_between_agents: 60
  cooldown_between_cycles: 300
  max_cycles: 50
  alert_channel: "telegram"  # or "teams"
  alert_on:
    - cycle_complete
    - finding_discovered
    - persistent_failure
    - cost_threshold_80pct
    - cost_limit_reached
```

---

## 8. 실행 방법

### 8.1 초기 세팅

```bash
# 1. 프로젝트 클론 및 환경 설정
cd /workspace
pip install pykrx opendartreader pandas statsmodels scipy \
            pdfplumber matplotlib plotly

# 2. MCP 서버 설치
npm install -g @anthropic/mcp-playwright @anthropic/mcp-sqlite \
               @anthropic/mcp-fetch @anthropic/mcp-filesystem

# 3. Playwright 브라우저 설치
npx playwright install chromium

# 4. DB 초기화
python3 scripts/init_db.py

# 5. 환경변수 설정
cp .env.example .env
# ANTHROPIC_API_KEY, DART_API_KEY, TELEGRAM_BOT_TOKEN 등 입력

# 6. Y변수 기초 데이터 사전 수집 (SK㈜ 10년 주가)
python3 scripts/seed_baseline.py
```

### 8.2 밤새 실행

```bash
# tmux 세션에서 Supervisor 실행
tmux new-session -d -s sk-agents \
  'python3 supervisor.py 2>&1 | tee logs/supervisor.log'

# 모니터링 (다른 터미널)
tmux new-session -d -s sk-monitor \
  'watch -n 10 python3 scripts/status_dashboard.py'

# 또는 Telegram 알림만으로 모니터링
```

### 8.3 Supervisor 자체의 보호

```bash
# systemd로 Supervisor가 죽어도 자동 재시작
# /etc/systemd/system/sk-agents.service

[Unit]
Description=SK Value Factor Analysis Agents
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/workspace
ExecStart=/usr/bin/python3 supervisor.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 9. hub.db 스키마

```sql
-- 가설 테이블
CREATE TABLE hypotheses (
    hypothesis_id   TEXT PRIMARY KEY,
    factor_name     TEXT NOT NULL,
    factor_category TEXT NOT NULL,
    causal_claim    TEXT NOT NULL,
    data_sources    TEXT,          -- JSON array
    time_range      TEXT,
    priority        TEXT DEFAULT 'medium',
    rationale       TEXT,
    status          TEXT DEFAULT 'pending',
    -- pending → collected → analyzed → significant/rejected/refining
    parent_hypothesis TEXT,        -- 파생된 경우 부모 가설 ID
    feedback        TEXT,          -- verifier의 피드백
    rejection_reason TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 수집 데이터셋 테이블
CREATE TABLE datasets (
    dataset_id      TEXT PRIMARY KEY,
    hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
    file_path       TEXT NOT NULL,
    source_url      TEXT,
    collection_method TEXT,       -- api/crawl/download/manual
    date_range      TEXT,
    row_count       INTEGER,
    column_list     TEXT,         -- JSON array
    quality_score   INTEGER,     -- 0-100
    quality_notes   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 분석 결과 테이블
CREATE TABLE analyses (
    analysis_id     TEXT PRIMARY KEY,
    hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
    dataset_id      TEXT REFERENCES datasets(dataset_id),
    method          TEXT NOT NULL,
    results_json    TEXT NOT NULL, -- 구조화된 분석 결과
    script_path     TEXT,         -- 재현용 Python 스크립트
    chart_path      TEXT,
    status          TEXT DEFAULT 'completed',
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 검증 판정 테이블
CREATE TABLE verdicts (
    verdict_id      TEXT PRIMARY KEY,
    analysis_id     TEXT REFERENCES analyses(analysis_id),
    hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
    decision        TEXT NOT NULL, -- significant/rejected/needs_refinement
    checklist_json  TEXT,          -- 체크리스트 결과
    reasoning       TEXT,
    feedback        TEXT,          -- needs_refinement일 때 개선 방향
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 최종 발견사항 테이블
CREATE TABLE findings (
    finding_id      TEXT PRIMARY KEY,
    hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
    factor_name     TEXT NOT NULL,
    factor_category TEXT NOT NULL,
    effect_size     REAL,
    p_value         REAL,
    confidence      TEXT,         -- high/medium/low
    summary         TEXT,         -- 1-2문장 요약
    detail_report   TEXT,         -- 상세 설명
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 에이전트 실행 로그
CREATE TABLE agent_logs (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    action          TEXT,          -- started/completed/failed/retried
    details         TEXT,
    cost_usd        REAL DEFAULT 0,
    duration_sec    INTEGER,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 비용 추적
CREATE TABLE cost_tracker (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 10. 한 사이클 실행 예시 (시나리오)

```
[00:00] Supervisor 시작
[00:01] Hypothesis Agent 호출
        → hub.db에서 이전 결과 확인: 아직 없음 (첫 실행)
        → 웹검색: "한국 지주사 NAV 할인율 결정요인 논문"
        → 가설 5개 생성:
          H-001: ESG 등급 변동 → NAV 할인율 (high)
          H-002: 계열사 실적 발표 → 지주사 주가 (high)
          H-003: 자사주 매입 → 할인율 축소 (medium)
          H-004: 외국인 지분율 → NAV 할인율 (medium)
          H-005: KOSPI 변동성 → 할인율 확대 (low)
        → hub.db에 INSERT
        → hypothesis_done 시그널 생성

[00:08] Collector Agent 호출 (H-001: ESG 등급)
        → pykrx로 SK㈜ 시가총액 10년 데이터 수집
        → 웹검색: "KCGS ESG 등급 SK"
        → Playwright로 kcgs.or.kr 접속 → 등급 히스토리 크롤링
        → ESG 등급 변동 날짜 정리 → CSV 저장
        → datasets 테이블에 메타데이터 기록
        → H-001 status → "collected"

[00:20] Collector Agent 호출 (H-002: 계열사 실적)
        → SK하이닉스, SK텔레콤, SK이노베이션 실적발표 날짜 수집
        → DART API로 분기 실적 데이터 수집
        → CSV 저장, status → "collected"

[00:35] Analyzer Agent 호출 (H-001)
        → ESG 등급 변동 이벤트 스터디 수행
        → 등급 상향/하향 전후 CAR 계산
        → 결과: 등급 하향 시 CAR -2.3%, p=0.04
        → 분석 스크립트 + 차트 저장
        → analyses 테이블에 INSERT
        → H-001 status → "analyzed"

[00:50] Verifier Agent 호출 (H-001 검증)
        → 분석 스크립트 재실행 → 결과 일치 ✓
        → 샘플 크기: 8개 이벤트 → "경계선" (최소 요건 미달)
        → 판정: needs_refinement
        → 피드백: "ESG 등급 대신 E/S/G 개별 등급 변동으로 
          이벤트 수를 늘려 재분석 권장"
        → H-001 status → "refining"
        → 파생 가설 H-006 생성 큐에 등록

[01:05] Hypothesis Agent 재호출
        → H-001의 피드백을 읽고 파생 가설 H-006 생성:
          "E/S/G 개별 등급 변동이 NAV 할인율에 미치는 
           차별적 영향 (이벤트 수 확대)"
        → 추가 가설 2개 생성 (H-007, H-008)
        → 루프 계속...

[01:15] Collector Agent (H-002 분석 결과 수집 + H-006 수집 시작)
        ...
        
[이후 밤새 반복]

[07:00] Supervisor: 50사이클 완료 리포트
        - 총 23개 가설 생성
        - 15개 수집 완료, 12개 분석 완료, 10개 검증 완료
        - 3개 significant findings 발견
        - 5개 기각, 2개 수정 중
        - 총 비용: $47.30
        → Telegram/Teams로 요약 알림 발송
```

---

## 11. 구현 우선순위

### Phase 0: 인프라 (2-3일)
1. hub.db 스키마 생성 (`init_db.py`)
2. Supervisor 기본 골격 (프로세스 실행/감시/재시작)
3. MCP 서버 설치 및 연결 테스트
4. 시그널 메커니즘 구현
5. Telegram/Teams 알림 연동

### Phase 1: 단일 에이전트 검증 (3-5일)
1. Hypothesis Agent 프롬프트 작성 + 테스트
2. Collector Agent 프롬프트 + pykrx/DART 수집 테스트
3. Analyzer Agent 프롬프트 + 기본 통계분석 테스트
4. Verifier Agent 프롬프트 + 체크리스트 테스트
5. 각 에이전트를 개별로 실행하여 입출력 검증

### Phase 2: 루프 통합 (3-5일)
1. Supervisor에서 4개 에이전트 순차 호출
2. 에이전트 간 상태 전달 (hypotheses.status 기반)
3. 피드백 루프 (verifier → hypothesis) 구현
4. 자가 치유 메커니즘 테스트

### Phase 3: 오버나이트 테스트 (1주)
1. 실제 밤새 돌려보기
2. 실패 패턴 수집 및 프롬프트 개선
3. 비용 최적화 (불필요한 턴 줄이기)
4. 결과 품질 검증

### Phase 4: 대시보드 + 납품 준비
1. Streamlit 대시보드에 findings 실시간 표시
2. SK PR팀용 리포팅 포맷 정리
3. 시스템 운영 매뉴얼 작성
