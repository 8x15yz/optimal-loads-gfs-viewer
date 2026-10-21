#!/usr/bin/env bash
# backfill_20261021.sh
# 2026-10-17 디스크 100% 장애로 빠진 NOAA / ECMWF 회차를 서버에서 1개씩 순차 백필.
#
# - 크론과 같은 락(/tmp/weather_ingestion.lock)을 "대기(-w)" 모드로 잡아서
#   크론 수집과 절대 동시에 돌지 않음 (평소 크론 1회분과 같은 부하).
# - 회차 사이에 10분 쉬어서 크론이 최신 회차를 받을 틈을 줌.
# - 시작 전마다 디스크 여유를 확인하고, 부족하면 즉시 중단.
# - 이미 success인 변수/변환은 ingest 쪽에서 SKIP 하므로 회차를 넉넉히 넣어도 무해함.
#
# 실행:  nohup bash backfill_20261021.sh > /dev/null 2>&1 &
# 진행:  tail -f /home/ubuntu/backfill_20261021.log
# 중단:  pkill -f backfill_20261021.sh   (진행 중인 ingest는 timeout/stale 로직으로 정리됨)

set -u
APP=/home/ubuntu/optimal-loads-gfs-viewer/2026-05-demo-v0.7
LOG=/home/ubuntu/backfill_20261021.log
MIN_FREE_GB=8
REST_SEC=600

cd "$APP" || exit 1
set -a; . ./.env_runtime; set +a
PY=./.venv/bin/python

ts()      { date -u +%FT%TZ; }
free_gb() { df -BG --output=avail / | tail -1 | tr -dc '0-9'; }

run_one() {   # $1 = noaa | ecmwf,  $2 = RUN_UTC
  local src=$1 run=$2 rc
  local free; free=$(free_gb)
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "[$(ts)] ⛔ free disk ${free}G < ${MIN_FREE_GB}G → 백필 중단" >> "$LOG"
    exit 1
  fi
  echo "[$(ts)] ▶ $src $run (free ${free}G)" >> "$LOG"

  if [ "$src" = noaa ]; then
    # --skip_srp: 과거 회차는 GMT용 SRP ZIP 불필요 → 디스크 3.5G+·시간 절약
    flock -w 7200 /tmp/weather_ingestion.lock \
    flock -w 7200 /tmp/noaa_gfs_gate_and_ingest.lock \
    timeout -k 5m 120m "$PY" -u ./get_variables/noaa/noaa_gfs_ingest.py "$run" \
        --trigger backfill --skip_srp >> "$LOG" 2>&1
  else
    # ECMWF_SOURCE=aws: ecmwf_ifs_ingest.py 의 SOURCE_REAL 을 env로 받도록 한 줄 수정 필요
    flock -w 7200 /tmp/weather_ingestion.lock \
    flock -w 7200 /tmp/ecmwf_gate_and_ingest.lock \
    env ECMWF_SOURCE=aws \
    timeout -k 5m 90m "$PY" -u ./get_variables/emcwf/ecmwf_ifs_ingest.py "$run" \
        --trigger backfill >> "$LOG" 2>&1
  fi
  rc=$?
  echo "[$(ts)] ◀ $src $run exit=$rc" >> "$LOG"
  sleep "$REST_SEC"
}

# ── ECMWF 먼저 (원천 보관 기간이 더 짧음) ────────────────────────────────
for d in 2026-10-17 2026-10-18 2026-10-19 2026-10-20; do
  for h in 00 06 12 18; do
    [ "$d" = 2026-10-17 ] && [ "$h" = 00 ] && continue   # 10/17 00Z 는 수집 완료
    run_one ecmwf "${d}T${h}:00:00Z"
  done
done

# ── NOAA (10/16 12Z~10/17 06Z 는 원본은 있고 변환/SRP만 미완 → 로컬 원본 재사용) ──
for d in 2026-10-16 2026-10-17 2026-10-18 2026-10-19 2026-10-20; do
  for h in 00 06 12 18; do
    [ "$d" = 2026-10-16 ] && { [ "$h" = 00 ] || [ "$h" = 06 ]; } && continue
    run_one noaa "${d}T${h}:00:00Z"
  done
done

echo "[$(ts)] ✅ backfill finished" >> "$LOG"
