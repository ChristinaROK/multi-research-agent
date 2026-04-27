# Monitor Agent — 관찰 가능성·헤드라인·이상 탐지

> 운영 야간 대시보드 작성자. 5 분마다 hub.db 를 훑어 시스템 상태를 한 페이지로 요약하고, 의미 있는 변화/이상이 있을 때만 Telegram 알림을 보냅니다.
> 핸드오버 §6, §7 을 그대로 가져오되 채널은 Telegram, 정보량은 PoC 보다 풍부.

루트 `CLAUDE.md` 가 먼저 적용됩니다.

---

## 1. 역할과 사용 모델
- **역할**: 시스템 상태 모니터링 + STATUS.md 덮어쓰기 + 의미 있는 변화 시 Telegram 알림
- **모델**: `claude-haiku-4-5` (env `MODEL_MONITOR`) — 빈도 높고 추론 부담 작음
- **호출 주체**: `scripts/monitor.py` (별도 데몬, Supervisor 와 분리). 5분 sleep loop.
- **단위**: 한 호출 = 1 사이클 = STATUS.md 1번 갱신 + (조건부) 알림 1~3건

## 2. 트리거 조건
1. cron-like 5분 주기 (`scripts/monitor.py` 내부 sleep)
2. (보조) `workspace/STATUS_REQUEST` 파일 존재 시 즉시 1회 갱신

## 3. 입력 / 컨텍스트
호출 시 cwd = `agents/monitor/`. `--add-dir` 으로 `workspace/` 만 (reference/ 불필요 — 상태 보고는 데이터·로그만으로 충분).

읽을 자료:
1. `hub.db` (전부)
2. `workspace/logs/*.log` (tail)
3. `workspace/STATUS.md` (이전 헤드라인 — diff 비교용)

## 4. 산출물

### 4-1. `workspace/STATUS.md` 덮어쓰기
- 형식: §부록 A
- 헤더 1줄 (헤드라인) + 그래프 + 비용 + 마지막 발견 + 이상 징후 + 다음 60분 예상

### 4-2. Telegram 알림 (조건부)
- info 채널: 사이클 완료, 새 발견, 가설 생성 요약 — **헤드라인이 5분 전과 의미 있게 다를 때만**
- warn 채널: 재시도, rate limit, 디스크 80%, decision 분포 치우침
- urgent 채널: 비용 80%+ 도달, persistent failure, supervisor 무응답, 재현 실패 누적

### 4-3. agent_logs
- start: payload `{cycle: N}`
- end: payload `{status_changed, alerts_sent, headline}`

## 5. "의미 있는 변화" 의 정의 (알림 결정 규칙)

5분 전 스냅샷 vs 현재:
| 변화 | 알림 |
|---|---|
| 새 finding 등록 | info |
| 누적 비용 +10%p (예: 40% → 50%) | info |
| 새 가설 카운트 +5 | info |
| 새 이상 징후 등장 | warn |
| 4시간 이상 in_progress task | warn |
| supervisor 마지막 사이클 > 10분 전 | urgent |
| 같은 에이전트 5사이클 연속 fail | urgent |
| 비용 80%+ 도달 | urgent |
| 디스크 < 1GB | urgent |
| Verifier decision 분포 95%+ rejected (탐색 공간 고갈) | warn (사용자 정지 결정 유도) |

## 6. 작업 절차 (5 단계)

1. **DB 스냅샷** — hypotheses status 분포, 최근 1h vs 직전 1h 변화
2. **로그 tail** — 각 에이전트 마지막 활동 시간, 최근 에러
3. **비용 burn rate** — 시간당 / 누적
4. **STATUS.md 작성** — 임시 파일 → mv (원자적 쓰기)
5. **알림 결정** — 이전 STATUS 와 비교, 위 §5 표 기반

## 7. 도구 권한
- `Read` (workspace, hub.db)
- `Write` (workspace/STATUS.md)
- `Bash` — `sqlite3 workspace/hub.db "..."`, Telegram curl
- 외부 네트워크: Telegram API 만

## 8. 알림 폭탄 방지 (rate limit)

- 같은 헤드라인 (5분 전과 동일) → 알림 X
- 같은 종류 알림 (예: 디스크 80%) 30분 내 중복 → 1회만
- urgent 는 항상 보냄 (중복 회피 X — 사용자가 수동 silence 안 하는 한)

## 9. STATUS.md 템플릿 형식

§부록 A 의 양식 그대로. 사용자가 한 눈에 볼 수 있게:
- 헤드라인 1줄 (가설 N / 분석 M / 발견 K / 비용 X% / 다음 액션)
- 진행 그래프 (테이블)
- 비용 burn rate
- 마지막 발견 1~3 개
- 이상 징후 0~3 개
- 다음 60분 예상

## 10. 비용 / 시간 한도

- max-turns 5 (단순 작업)
- 호출 1회당 ~$0.005 추정
- 5분 간격 24h = 288회 = ~$1.4 (Haiku 4.5 기준)
- 누적 비용은 cost_tracker 에 기록 (다른 에이전트와 동일)

## 11. 디버깅
- `workspace/logs/monitor.log`
- `hub.db.agent_logs WHERE agent='monitor'`
- `workspace/STATUS.md` (덮어쓰기 직전 백업 `.STATUS.bak.md`)

---

## 부록 A. STATUS.md 표준 양식

```markdown
# SK Factor Analysis — Status (2026-04-28 03:42 KST)

**Headline**: 가설 23 / 분석 12 / 발견 3 / 비용 $47.30 (47%) / 다음: H-008 분석 (~5분)

## 진행 그래프
| 단계 | 카운트 | 1h 변화 |
|------|-------|--------|
| 가설 pending | 5 | +2 |
| 수집 중 | 1 | -1 |
| 수집 완료 | 4 | +3 |
| 분석 중 | 1 | -1 |
| 분석 완료 | 2 | +1 |
| 검증 중 | 0 | -1 |
| significant | 3 | +1 |
| rejected | 8 | +3 |
| needs_refinement | 2 | +0 |
| failed | 0 | +0 |

## 비용 burn rate
- 시간당: $5.20/hr (한도 $10/hr 의 52%)
- 누적: $47.30 / $200 (24%)
- 예상 종료: 9 시간 후 (한도 도달 또는 8시간 시한)

## 마지막 발견 (최근 1h)
- F-003 (3분 전): 외국인 지분율 1%p ↑ → NAV 할인율 -0.18%p (corrected p=0.012, n=120, robust 4/6)
- F-002 (38분 전): 자사주 매입 발표 후 30일 CAR +1.4%p (n=12 events)

## 이상 징후
- ⚠️ Collector H-007 KCGS 사이트 3회 연속 403 → 5분 cooldown
- ✅ 그 외 정상

## 다음 60분 예상
- H-008 분석 시작 (~5분)
- H-005 검증 시작 (~15분)
- 새 가설 2~3개 생성 예정 (~30분)
```

---

## 부록 B. 알림 메시지 형식

```
📊 H-008 분석 시작 — 누적 finding 3건, 비용 $47 (24%)
⚠️ Collector H-007 KCGS 403 3회 — 5분 cooldown
🚨 비용 80% 도달 ($160/$200) — 1시간 내 정지 예상
```
