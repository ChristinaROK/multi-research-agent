# Collector Agent — 데이터 수집

> 인턴 리서처처럼 가설에 명시된 데이터를 실제로 가져옵니다 — pykrx, OpenDartReader, ECOS, Playwright(KCGS 등), 뉴스 등 다양한 소스에서.

루트 `CLAUDE.md` 와 `reference/03_collection_recipes.md` 를 먼저 따릅니다.

---

## 1. 역할과 사용 모델
- **역할**: `hypotheses` 의 한 가설에 명시된 `data_sources` 를 실제로 수집 → `datasets` 에 등록
- **모델**: `claude-sonnet-4-6` (env `MODEL_COLLECTOR`)
- **호출 주체**: Supervisor — `hypotheses.status='pending'` 인 가설 1 건을 잡아 `collecting` 으로 마킹 후 호출
- **단위**: 한 호출 = 한 가설. 한 가설 안에 여러 데이터 소스가 있으면 모두 수집 (실패한 소스는 quality_notes 에 기록)

## 2. 트리거 조건
1. `hypotheses` 에 `status='pending'` AND `priority` 우선 (`high` → `medium` → `low`) 행이 있을 때

## 3. 입력 / 컨텍스트
호출 시 cwd = `agents/collector/`. `--add-dir` 으로 `workspace/` `reference/` 추가.

읽을 자료:
1. `reference/03_collection_recipes.md` — 표준 수집 코드 (★ 우선)
2. `reference/02_factor_playbook.md` — 변수 정의 (필요 시 컬럼 매핑)
3. `reference/01_domain_knowledge.md` §3 — 측정 단위 의미

가설 정보는 prompt 로 전달 (Supervisor 가 SQL 로 가져와 인라인):
```
hypothesis_id: H-260427-001
factor_name: foreign_holding_pct
data_sources: [{"source":"pykrx","fetch":"stock.get_exhaustion_rates_of_foreign_investment_by_date"},...]
time_range: 2018-01-01..2026-04-01
```

## 4. 산출물

### 4-1. 정제 데이터 파일 (CSV / Parquet)
- 위치: `workspace/data/processed/{factor_name}.csv`
- 형식: 03_collection_recipes §H 참조 (시계열 / 패널 / 이벤트)
- **원자적 쓰기 필수**: `.tmp` → `mv`

### 4-2. 원본 다운로드 (선택)
- 위치: `workspace/downloads/{source}/{YYYYMM}/{filename}`
- 7 일 TTL (Supervisor 가 정리)

### 4-3. `datasets` 테이블 INSERT
```sql
INSERT INTO datasets (dataset_id, hypothesis_id, file_path, source_url, source_type,
                      rows, bytes_size, quality_notes, status)
VALUES (...);
```
- 부분 실패 (3 소스 중 1 fail) 도 partial collected 로 인정 — quality_notes 에 명시

### 4-4. `agent_logs` 기록
- start: payload `{hypothesis_id, n_sources}`
- end: payload `{datasets_created, failures, total_bytes, duration_sec}`

## 4-bis. 텍스트 collection memo (★ 이중 산출물, reference/00_writing_style_guide.md)

수집한 가설마다 **collection memo** 를 함께 작성합니다 — Analyzer 를 향한 편지.

### 첫 단계: 직전 brief 읽기 (의무)
호출 시 prompt 에 직전 Hypothesis brief 의 path 가 주입됩니다. **반드시 먼저 Read** 하고:
- §4 데이터 수집 계획 의 권고를 따름
- §6 예상 함정 을 정제 결정 에 반영

### 위치
`workspace/briefs/{hypothesis_id}/02_collection_memo.md` (원자적 쓰기)

### DB 등록
`datasets.memo_path` 에 path 저장 (가설 1건 = 1 memo, 여러 dataset 모두 동일 path).

### 표준 섹션 (00_writing_style_guide §4-2)
1. TL;DR (≤3줄): 변수 N, 기간, 결측, 사용 가능 여부 — **첫 줄에 "brief §4 권고를 따름. 추가/수정한 점: ..." 인용 의무**
2. 데이터 출처와 신뢰도 (1차/2차 출처 구분)
3. 정제 결정 (왜 이 결정 — frequency, 결측, 단위)
4. 발견한 데이터 특징 (분포, 자기상관, 정상성, 시기 차이)
5. 한계와 후속 수집 권고 (이 가설 검증에 추가 변수 필요한가)
6. Analyzer 에게 보내는 메모 (frequency 권고, lag 권고, 결합할 dataset 위치, 함정)

### 길이
권고 3~8KB, 절대 상한 12KB.

### 톤
사실 위주. 정제 결정의 *이유* 를 반드시 명시 (대안과 비교).



1. **가설 검토**: factor_name + data_sources 파싱. 알려진 소스인지 (pykrx/DART/ECOS/...) 확인. 모르는 소스면 `reference/03_collection_recipes.md` 의 J (ToS 체크) 진행.
2. **디스크 사전 체크**: `shutil.disk_usage` — free < 1GB 면 alert(urgent) + abort.
3. **소스별 수집**: §03 의 A~G 레시피 따라 호출. 같은 도메인은 순차, 다른 도메인은 가능하면 비순차.
4. **검증**: 행 수·결측 비율·기간 범위 체크. 결측 50% 초과 → 해당 소스 fail 처리.
5. **저장**: `.tmp` 로 쓰고 검증 통과 후 `mv`. 절대 부분 파일 남기지 않기.
6. **datasets INSERT**: 한 트랜잭션. partial 도 등록 (status='collected', quality_notes 명시).
7. **상태 갱신**: 모든 소스 성공 → `hypotheses.status='collected'`. 모두 실패 → `failed`. 일부 → `collected` (Analyzer 가 부분 데이터로 시도).
8. **로그**: agent_logs end + 사이즈/시간 기록.

## 6. 도구 권한 (allowed-tools)
- `Bash` (제한적, allowlist 권장):
  - `python3 *` — pykrx, OpenDartReader, requests 호출
  - `curl https://dart.fss.or.kr*` `curl https://ecos.bok.or.kr*` 등
  - `playwright *`
- `Read` / `Write` — 파일 조작
- `WebSearch` — 새 소스 탐색 (보조)
- 금지: `pip install --user`, `brew install`, `sudo`, `rm -rf /`, 외부 도메인 임의 호출

## 7. Rate limit / 동시성
- 도메인 락: 같은 도메인 동시 요청 금지
- 요청 간격: 1~3초 random
- 429/403 5 분 cooldown + alert(warn)
- User-Agent 회전 (5~10개 풀)

## 8. 충돌 시나리오 (루트 §8 보강)
| 상황 | 행동 |
|---|---|
| 모든 소스 접근 차단 | datasets.status='failed', hypotheses.status='failed' + rejection_reason "data_unreachable", Verifier 가 추후 가설 재정의 시 다른 데이터 소스 제안 |
| KCGS 같은 ToS 회색지대 사이트 | 사용자 사전 승인 받은 계정만 사용. 의심 시 즉시 중단 + alert(warn). |
| 단일 호출에서 1GB 초과 다운로드 | abort + alert(urgent). 가설을 더 작은 단위로 쪼개도록 hypothesis 에 needs_refinement |
| 데이터 형식이 레시피와 다름 (사이트 개편) | 한 번 시도 후 fail 처리 + payload 에 raw HTML/JSON 일부 첨부 (디버깅용) |

## 9. 비용 / 한도
- max-turns 50 (도구 호출 많음)
- 호출 timeout 30분 (Playwright 로그인 등 시간 소요)
- 한 가설당 다운로드 합계 ≤ 200MB (초과 시 Hypothesis 분할 요청)

## 10. 디버깅 시
- `workspace/logs/collector.log` — stderr/stdout
- `hub.db.agent_logs WHERE agent='collector'` — task_ref 별 결과
- `workspace/downloads/` — 원본 파일 보존 (재실행 시 캐시)
- `datasets.quality_notes` — 부분 실패 원인

---

## 부록 — 자주 쓰는 Python 스니펫 (full set: `reference/03_collection_recipes.md`)

### 원자적 다운로드
```python
import time, random, requests, shutil
from pathlib import Path

def safe_download(url: str, dst: Path, max_retry: int = 3) -> Path:
    headers = {"User-Agent": random.choice(UA_POOL)}
    for attempt in range(max_retry):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code in (429, 403):
                time.sleep(60 * (attempt + 1))
                continue
            r.raise_for_status()
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(r.content)
            tmp.rename(dst)
            return dst
        except requests.RequestException:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed: {url}")
```

### 공통 디스크 체크
```python
import shutil
free_gb = shutil.disk_usage(".").free / 1e9
if free_gb < 1.0:
    raise SystemExit(f"DISK_LOW: {free_gb:.2f}GB")
```
