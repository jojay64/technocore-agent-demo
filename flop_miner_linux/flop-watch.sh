#!/usr/bin/env bash
# flop-watch.sh — READ-ONLY health check for the FLOP devnet miner started by flop-miner-linux.sh.
#
# It only reads: container state, the tunnel service, the worker logs, nvidia-smi, and (unless --no-chain)
# the public devnet RPC through the official CLI image (`miner status`, a read). It sends nothing to anyone,
# changes nothing, and never prints secrets (only FLOP_CLI and the public miner address are read from $D).
#
#   ./flop-watch.sh               one check, one summary line, then reasons if any
#   ./flop-watch.sh --no-chain    skip the RPC read (containers/tunnel/logs/GPU only)
#   watch -n 300 ./flop-watch.sh  repeat every 5 minutes in a terminal
#
# Exit code: 0 = OK, 1 = WARN, 2 = CRIT. Each run is also appended to $WATCH_LOG
# (default /var/log/flop-watch.log).
#
# Optional rent reminder (the VM cannot see your Vast.ai balance, so tell it):
#   CREDIT_USD=25 RATE_USD_H=0.72 ./flop-watch.sh
# It estimates remaining hours from this VM's boot time (~ instance start). Below WARN_HOURS (default 3) it
# warns: run `./flop-miner-linux.sh down`, then `retire`, then DELETE the instance.
set -uo pipefail

D=/opt/flop-miner
RPC=wss://rpc.devnet.flop.network
LOGWIN="${WATCH_WINDOW:-2h}"          # how far back to look in the worker log
WARN_DISCONNECTS="${WARN_DISCONNECTS:-20}"
WARN_HOURS="${WARN_HOURS:-3}"
WATCH_LOG="${WATCH_LOG:-/var/log/flop-watch.log}"
CHAIN=1
[ "${1:-}" = "--no-chain" ] && CHAIN=0

crit=(); warn=()
now_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# --- containers and tunnel ---------------------------------------------------------------------
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = true ]; }
worker=down; backend=down
running flop-miner-worker-1  && worker=up  || crit+=("worker container is not running")
running flop-miner-backend-1 && backend=up || crit+=("backend container is not running")
tunnel=$(systemctl is-active flop-tunnel 2>/dev/null || true); tunnel=${tunnel:-unknown}
[ "$tunnel" = active ] || crit+=("tunnel service is $tunnel")

# --- worker log (calibration jobs, disconnects, calibration status) ----------------------------
answered=0; disc=0; calib="n/a"
if [ "$worker" = up ]; then
  logs=$(docker logs --since "$LOGWIN" flop-miner-worker-1 2>&1 || true)
  answered=$(grep -c 'answered task' <<<"$logs" || true)
  # One RPC drop logs several lines in the same second: count only the connection-level line.
  disc=$(grep -c 'API-WS: disconnected from wss' <<<"$logs" || true)
  calib=$(grep '\[calibration\]' <<<"$logs" | tail -n 1 | sed 's/^.*\[calibration\] *//' | cut -c1-80 || true)
  [ -n "$calib" ] || calib="none in last $LOGWIN"
  [ "${disc:-0}" -le "$WARN_DISCONNECTS" ] || warn+=("$disc RPC drops in the last $LOGWIN (the worker reconnects by itself; frequent drops point to the RPC or the network)")
  # A calibration check that failed only because the RPC dropped is transient: not worth a warning.
  if grep -q 'failed' <<<"$calib" && ! grep -q 'disconnected from wss' <<<"$calib"; then
    warn+=("last calibration line mentions a failure: $calib")
  fi
  [ "${answered:-0}" -gt 0 ] || warn+=("no test job answered in the last $LOGWIN (normal between calibration bursts)")
fi

# --- chain state (read-only RPC query via the official CLI image) ------------------------------
registered=skipped
if [ "$CHAIN" = 1 ]; then
  cli_img=$(grep '^FLOP_CLI=' "$D/miner.env" 2>/dev/null | head -n 1 | cut -d= -f2-)
  addr=$(cat "$D/address" 2>/dev/null || true)
  if [ -n "$cli_img" ] && [ -n "$addr" ] && command -v jq >/dev/null; then
    out=$(timeout 120 docker run --rm --network host "$cli_img" --network devnet --rpc "$RPC" miner status "$addr" --json 2>/dev/null || true)
    if jq -e . >/dev/null 2>&1 <<<"$out"; then
      registered=$(jq -r '(.registered // false) | tostring' <<<"$out")
      [ "$registered" = true ] || crit+=("miner is not registered on chain (registered=$registered)")
    else
      registered=unreadable
      warn+=("could not read the miner status from the RPC (network or RPC hiccup)")
    fi
  else
    registered=unreadable
    warn+=("miner address / CLI image / jq not found under $D")
  fi
fi

# --- GPU ---------------------------------------------------------------------------------------
gpu=$(nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -n 1 | sed 's/, */|/g' || true)
[ -n "$gpu" ] || { gpu="unavailable"; crit+=("nvidia-smi does not answer"); }

# --- rent reminder -----------------------------------------------------------------------------
credit_left="n/a"
if [ -n "${CREDIT_USD:-}" ] && [ -n "${RATE_USD_H:-}" ]; then
  boot=$(date -d "$(uptime -s 2>/dev/null)" +%s 2>/dev/null || echo "")
  if [ -n "$boot" ]; then
    credit_left=$(awk -v c="$CREDIT_USD" -v r="$RATE_USD_H" -v b="$boot" -v n="$(date +%s)" 'BEGIN{printf "%.1f", c/r-(n-b)/3600}')
    awk -v l="$credit_left" -v w="$WARN_HOURS" 'BEGIN{exit !(l<w)}' \
      && warn+=("about ${credit_left} h of rent left: run ./flop-miner-linux.sh down, then retire, then DELETE the instance")
  fi
fi

# --- verdict -----------------------------------------------------------------------------------
if [ "${#crit[@]}" -gt 0 ]; then verdict=CRIT; rc=2
elif [ "${#warn[@]}" -gt 0 ]; then verdict=WARN; rc=1
else verdict=OK; rc=0; fi

line="$(now_utc) $verdict worker=$worker backend=$backend tunnel=$tunnel registered=$registered answered_$LOGWIN=$answered disconnects=$disc gpu=[$gpu] credit_left_h=$credit_left"
echo "$line"
echo "  calibration: $calib"
for m in "${crit[@]+"${crit[@]}"}"; do echo "  CRIT: $m"; done
for m in "${warn[@]+"${warn[@]}"}"; do echo "  WARN: $m"; done
{ echo "$line"; } >> "$WATCH_LOG" 2>/dev/null || true
exit "$rc"
