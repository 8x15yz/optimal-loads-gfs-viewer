#!/usr/bin/env bash
# noaa_recheck_20261023.sh
# 2026-10-16 00Z ~ 10-20 18Z NOAA 20회차를 ingest 로 한 번 더 훑어서 빠진 것만 채움.
#
# NOAA ingest 는 이미 "실제 데이터 기준"으로 판단함:
#   - 변수 SKIP 조건 = ingestion_runs success  AND  Mongo 에 89스텝 전부 s3.key 존재
#   - 받은 GRIB 은 valid_field() 로 변수/런/스텝 검증
#   - S111/S413 변환이 안 된 회차는 원본을 다시 모아 변환까지 수행
# → 다 채워진 회차는 몇 초 만에 SKIP 으로 끝남.
#
# - 오래된 회차부터 (NOMADS 보관 기간이 짧아서 10/16 이 먼저 사라짐)
# - 크론과 같은 락 대기(-w), 회차당 최대 150분, 디스크 8G 체크
# - 실제로 받은 회차 뒤에만 10분 휴식 (SKIP 회차는 바로 다음으로)
#
# 실행:  nohup bash noaa_recheck_20261023.sh > /dev/null 2>&1 &
# 진행:  tr '\r' '\n' < ~/noaa_recheck_20261023.log | grep -E '^\[20' | tail -n 10
# 중단:  pkill -f noaa_recheck_20261023.sh

set -u
APP=/home/ubuntu/optimal-loads-gfs-viewer/2026-05-demo-v0.7
LOG=/home/ubuntu/noaa_recheck_20261023.log
MIN_FREE_GB=8
REST_SEC=600
TIMEOUT=150m

cd "$APP" || exit 1
set -a; . ./.env_runtime; set +a
PY=./.venv/bin/python

ts()      { date -u +%FT%TZ; }
free_gb() { df -BG --output=avail / | tail -1 | tr -dc '0-9'; }

for d in 2026-10-16 2026-10-17 2026-10-18 2026-10-19 2026-10-20; do
  for h in 00 06 12 18; do
    run="${d}T${h}:00:00Z"
    free=$(free_gb)
    if [ "$free" -lt "$MIN_FREE_GB" ]; then
      echo "[$(ts)] ⛔ free disk ${free}G < ${MIN_FREE_GB}G → 중단" >> "$LOG"
      exit 1
    fi
    echo "[$(ts)] ▶ noaa $run (free ${free}G)" >> "$LOG"
    t0=$(date +%s)

    flock -w 7200 /tmp/weather_ingestion.lock \
    flock -w 7200 /tmp/noaa_gfs_gate_and_ingest.lock \
    timeout -k 5m "$TIMEOUT" "$PY" -u ./get_variables/noaa/noaa_gfs_ingest.py "$run" \
        --trigger recheck --skip_srp >> "$LOG" 2>&1
    rc=$?

    el=$(( $(date +%s) - t0 ))
    echo "[$(ts)] ◀ noaa $run exit=$rc (${el}s)" >> "$LOG"
    # 락 대기 시간 포함 5분 넘게 걸린 회차(= 실제 수집/변환을 한 회차) 뒤에만 휴식
    [ "$el" -gt 300 ] && sleep "$REST_SEC"
  done
done

echo "[$(ts)] ✅ noaa recheck finished" >> "$LOG"
