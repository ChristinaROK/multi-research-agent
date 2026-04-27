# multi-research-agent

SK㈜ 지주사 NAV 할인율에 영향을 미치는 팩터를 밤새 무인으로 탐색하는 자율 멀티에이전트 시스템.

가설 생성 → 데이터 수집 → 분석 → 검증 → 새 가설 루프를 Hypothesis / Collector / Analyzer / Verifier 4 에이전트가 자율 수행, Python Supervisor가 SQLite `hub.db`로 조정한다.

## 시작하기
1. `.env.example` 을 `.env` 로 복사하고 키 채우기 (Anthropic, DART, Telegram).
2. `uv sync` (의존성 설치).
3. `uv run python scripts/supervisor.py --dry-run` (실비용 0 검증).
4. 실행 가이드와 설계 상세는 `CLAUDE.md`, `docs/`, `reference/` 참조.
