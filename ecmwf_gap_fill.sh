#!/usr/bin/env bash
# ecmwf_gap_fill.sh
# 2026-10-16 00Z ~ 10-20 18Z ECMWF 를 실제 데이터(S3 + Mongo) 기준으로 점검하고,
# 빠진 변수/스텝만 aws 미러에서 다시 받아 채움.
#
#   점검만 (몇 분, 수집 없음):  bash ecmwf_gap_fill.sh check
#   채우기 (백그라운드):        nohup bash ecmwf_gap_fill.sh fill > /dev/null 2>&1 &
#   진행 확인:                  grep -E '^\[20|▶▶|◀◀|◀ |⏹️' ~/ecmwf_gap_fill.log | tail -n 20
#   중단:                       pkill -f ecmwf_gap_fill
#
# - 크론과 같은 락을 대기(-w) 모드로 잡음 → 크론 수집과 동시에 돌지 않음
# - 회차 1개씩, 회차당 최대 180분, 회차 사이 10분 휴식
# - 시작 전마다 디스크 여유 8G 확인
# - 끝나면 같은 범위를 다시 점검해서 결과를 로그 끝에 남김

set -u
MODE=${1:-check}
APP=/home/ubuntu/optimal-loads-gfs-viewer/2026-05-demo-v0.7
LOG=/home/ubuntu/ecmwf_gap_fill.log
START=2026-10-16T00:00:00Z
END=2026-10-20T18:00:00Z
MIN_FREE_GB=8
REST_SEC=600
TIMEOUT=180m
RUNS_FILE=/home/ubuntu/ecmwf_gap_runs.txt

cd "$APP" || exit 1
set -a; . ./.env_runtime; set +a
PY=./.venv/bin/python
GAP=./get_variables/emcwf/ecmwf_gap_fill.py

ts()      { date -u +%FT%TZ; }
free_gb() { df -BG --output=avail / | tail -1 | tr -dc '0-9'; }

if [ ! -f "$GAP" ]; then
  echo "❌ $APP/$GAP 가 없음 (ecmwf_gap_fill.py 를 get_variables/emcwf/ 에 올려야 함)"
  exit 1
fi

if [ "$MODE" = check ]; then
  "$PY" -u "$GAP" --start_utc "$START" --end_utc "$END" 2>&1 | tee -a "$LOG"
  exit 0
fi

if [ "$MODE" != fill ]; then
  echo "사용법: bash $0 check | fill"; exit 1
fi

echo "[$(ts)] ===== gap fill start ($START ~ $END) =====" >> "$LOG"
"$PY" -u "$GAP" --start_utc "$START" --end_utc "$END" --runs_out "$RUNS_FILE" >> "$LOG" 2>&1

while read -r run <&3; do
  [ -z "$run" ] && continue
  free=$(free_gb)
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "[$(ts)] ⛔ free disk ${free}G < ${MIN_FREE_GB}G → 중단" >> "$LOG"
    exit 1
  fi
  echo "[$(ts)] ▶ gapfill $run (free ${free}G)" >> "$LOG"

  flock -w 7200 /tmp/weather_ingestion.lock \
  flock -w 7200 /tmp/ecmwf_gate_and_ingest.lock \
  timeout -k 5m "$TIMEOUT" "$PY" -u "$GAP" --run "$run" --fill --rebuild_all_steps >> "$LOG" 2>&1
  rc=$?

  echo "[$(ts)] ◀ gapfill $run exit=$rc" >> "$LOG"
  sleep "$REST_SEC"
done 3< "$RUNS_FILE"

echo "[$(ts)] ===== 재점검 =====" >> "$LOG"
"$PY" -u "$GAP" --start_utc "$START" --end_utc "$END" >> "$LOG" 2>&1
echo "[$(ts)] ✅ gap fill finished" >> "$LOG"
