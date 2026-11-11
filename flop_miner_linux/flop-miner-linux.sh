#!/usr/bin/env bash
# flop-miner-linux.sh — FLOP DEVNET GPU miner on a rented Ubuntu 22.04/24.04 VM with an NVIDIA GPU.
#
# Linux adaptation of the logic of https://github.com/Makabeez/flop-pc-miner (MIT, Windows/WSL2 only),
# which was read line by line before this adaptation. Differences: no WSL checks, no Windows
# shortcuts, no dashboard, no `harden` (that step only exists to isolate WSL from Windows files).
#
# Trust model:
#   - Container images: only ghcr.io/flop-labs/flop-core/*, pulled by sha256 digest exactly as listed in
#     Flop's official https://flop.network/releases.json (the script refuses any other reference).
#   - Sponsor request: POST https://faucet.flop.network/sponsor/miner, only {address, issuedAt, signature}
#     leave the machine; the script refuses to sign if the sponsor reports a different chain.
#   - The miner key is NEW and devnet-only, generated on this VM. It is not a Technocore identity.
#     Never put any Technocore identity, .env or API key on this VM.
#   - Nothing listens on a public interface. The only public entry is a Cloudflare quick tunnel to the
#     worker's session port (127.0.0.1:8645), which refuses requests without SESSION_AUTH_TOKEN.
#
#   ./flop-miner-linux.sh prereqs   once: Docker Engine + NVIDIA Container Toolkit (official apt repos)
#   ./flop-miner-linux.sh setup     once: verify release list, images, key, sponsor, model, tunnel
#   ./flop-miner-linux.sh up        start: tunnel -> backend -> worker
#   ./flop-miner-linux.sh status    chain status, tunnel, GPU, last worker lines
#   ./flop-miner-linux.sh logs      follow the worker log
#   ./flop-miner-linux.sh sponsor   ask the sponsor again (or top up stake)
#   ./flop-miner-linux.sh down      stop worker, backend and tunnel
#   ./flop-miner-linux.sh backup-key  encrypt the miner key to a file you copy to your PC (do this BEFORE retire)
#   ./flop-miner-linux.sh retire    stop the worker and deregister the miner on chain (do this before deleting the VM)
#
# Needs next to this script, unmodified (sha256 pinned below, from Flop's official miner pack
# devnet-ts-20261106): decode-policy.mjs and retire.mjs.
# Sponsor rules (Flop docs): the script reads https://faucet.flop.network/limits before every request, only
# asks while sponsor.miner.status is "open", never retries on fix_request / report_to_operator, and waits at
# least retryAfterSeconds on retry_later.
#
# Devnet only: no rewards. Unanswered network test jobs cut the sponsored stake, so run `down`
# before the VM is stopped or its credit runs out, then `retire` and DESTROY the instance to stop paying.
set -euo pipefail

D="${FLOP_MINER_DIR:-/opt/flop-miner}"
BACKUP_FILE="${BACKUP_FILE:-/root/flop-miner-key-backup.enc}"
RELEASES_URL=https://flop.network/releases.json
SPONSOR_URL=https://faucet.flop.network
RPC=wss://rpc.devnet.flop.network
MAILBOX=https://mailbox.devnet.flop.network
GENESIS=0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631
MODEL_HASH=0x1a52c1abe781db051e70ae357f72bb70161ca0c092df0b618f22a9292ba63306
MODEL_NAME=Qwen/Qwen2.5-0.5B-Instruct
MODEL_SLUG=qwen/qwen2.5-0.5b-instruct
GPU_UTIL_ARG="${GPU_UTIL:-}"; WORKER_NAME_ARG="${WORKER_NAME:-}"   # only override saved values when given
GPU_UTIL="${GPU_UTIL:-0.6}"
WORKER_NAME="${WORKER_NAME:-flop-linux-miner}"   # public label; choose yours: WORKER_NAME=alice-4090 ./flop-miner-linux.sh setup
VERSION=1.0.0-linux
# Floor for the CLI image (same official org): the chain moved to runtime spec 1014014 on 7 Nov 2026 while
# releases.json still listed an older CLI. The newer of the two wins.
MIN_CLI=ghcr.io/flop-labs/flop-core/cli:v0.1.290@sha256:24e32b034b0d3600ee28127dad30d4228e623235f2dc62da34c2c85ba5e21a88
# Official helper scripts shipped next to this one (Flop miner pack devnet-ts-20261106, sha256 verified).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHA_DECODE_POLICY=11092584b37910a8d90c6eb1e5799f3a9359ae85c2a5cfe916780aecf2cd3bf1
SHA_RETIRE=9989f39d99fef132fde852d8d541a510ab7b14abea736a1a627103656e058871

die()  { echo "error: $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

# Many rented VMs log in as root without sudo installed: make `sudo` a no-op wrapper in that case.
if [ "$(id -u)" -eq 0 ] && ! command -v sudo >/dev/null 2>&1; then sudo() { "$@"; }; fi
: "${USER:=$(id -un)}"
[ "$(uname -s)" = Linux ] || die "this script is for Linux only"
[ -d /run/systemd/system ] || die "systemd is required (use a full VM, not a container instance)"

need_docker() {
  command -v docker >/dev/null || die "docker is not installed yet: run ./flop-miner-linux.sh prereqs"
  docker info >/dev/null 2>&1 || die "cannot talk to docker: log in as root, or re-login after prereqs"
}

envget()  { grep "^$1=" "$D/miner.env" | head -n 1 | cut -d= -f2-; }
envset()  { if grep -q "^$1=" "$D/miner.env"; then sed -i "s|^$1=.*|$1=$2|" "$D/miner.env"; else echo "$1=$2" >> "$D/miner.env"; fi; }
cli()     { docker run --rm --network host -v "$D:/work" "$(envget FLOP_CLI)" "$@"; }
compose() { docker compose --env-file "$D/miner.env" -f "$D/docker-compose.yml" "$@"; }
address() { [ -f "$D/address" ] || die "no miner key yet: run ./flop-miner-linux.sh setup"; cat "$D/address"; }
backend_ready() {
  compose exec -T backend python3 -c 'import os, urllib.request as u; u.urlopen(u.Request("http://127.0.0.1:8000/health", headers={"Authorization": "Bearer " + os.environ["INFERENCE_API_KEY"]}), timeout=5)' >/dev/null 2>&1
}

write_compose() {
  # Backend: no published port. Worker: localhost-only port. Secrets mounted read-only.
  # No privileged mode, no Docker socket, no host paths beyond this folder.
  cat > "$D/docker-compose.yml" <<'EOF'
name: flop-miner

services:
  backend:
    image: ${FLOP_BACKEND_IMAGE:?set FLOP_BACKEND_IMAGE in miner.env}
    restart: unless-stopped
    environment:
      MODEL_NAME: ${MODEL_NAME}
      MODEL_HASH: ${MODEL_HASH}
      MODEL_DIR: /models/checkpoint
      GPU_MEMORY_UTILIZATION: ${GPU_MEMORY_UTILIZATION:-0.6}
      TOPLOC_PRECISION: ${TOPLOC_PRECISION}
      DECODE_POLICY: ${DECODE_POLICY}
      DECODE_POLICY_HASH: ${DECODE_POLICY_HASH}
      INFERENCE_API_KEY: ${INFERENCE_API_KEY:?set INFERENCE_API_KEY in miner.env}
    volumes:
      - ./models/qwen2.5-0.5b-instruct:/models/checkpoint:ro
    ipc: host
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  worker:
    image: ${FLOP_WORKER_IMAGE:?set FLOP_WORKER_IMAGE in miner.env}
    restart: unless-stopped
    depends_on:
      - backend
    command: ["--rpc", "${FLOP_RPC}"]
    working_dir: /data
    environment:
      MINER_BACKEND: openai
      OPENAI_BASE_URL: http://backend:8000/v1
      INFERENCE_API_KEY: ${INFERENCE_API_KEY}
      MINER_MODEL: ${MODEL_NAME}
      OPENROUTER_MODEL_SLUG: ${OPENROUTER_MODEL_SLUG}
      WORKER_NAME: ${WORKER_NAME}
      MINER_SIGNER_FILE: /run/flop/signer
      MINER_ENCLAVE_KEY_FILE: /run/flop/enclave.seed
      MODEL_HASH: ${MODEL_HASH}
      MODEL_PRECISION: ${MODEL_PRECISION}
      MODEL_CHECKPOINT_DIR: /models/checkpoint
      DECODE_POLICY_HASH: ${DECODE_POLICY_HASH}
      TOPLOC_ENABLED: "${TOPLOC_ENABLED:-1}"
      TOPLOC_PRECISION: ${TOPLOC_PRECISION}
      SESSION_PORT: "8645"
      SESSION_CHAIN: "1"
      SESSION_COMMIT_IDS: "${SESSION_COMMIT_IDS:-1}"
      SESSION_AUTH_TOKEN: ${SESSION_AUTH_TOKEN:?set SESSION_AUTH_TOKEN in miner.env}
      SESSION_PUBLIC_HOST: ${SESSION_PUBLIC_HOST}
      GHOST_MAILBOX_URL: ${GHOST_MAILBOX_URL}
      GHOST_KEY_FILE: /data/ghost-x25519.json
    volumes:
      - ./signer:/run/flop/signer:ro
      - ./session-keys/enclave.seed:/run/flop/enclave.seed:ro
      - ./models/qwen2.5-0.5b-instruct:/models/checkpoint:ro
      - worker-data:/data
    ports:
      - "127.0.0.1:8645:8645"

volumes:
  worker-data:

# Match the miner network's packet size to the VM's uplink so TLS/WebSocket connections do not stall.
networks:
  default:
    driver_opts:
      com.docker.network.driver.mtu: "${NET_MTU:-1500}"
EOF
}

write_sponsor() {
  # Signs the documented message with the miner seed and POSTs only {address, issuedAt, signature}
  # to the official sponsor. Never prints or sends the seed; refuses a different chain.
  cat > "$D/sponsor.mjs" <<'EOF'
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire('/app/');
const { Keyring } = require('@polkadot/keyring');
const { cryptoWaitReady } = require('@polkadot/util-crypto');
const SPONSOR = process.env.SPONSOR_URL;
const GENESIS = process.env.EXPECTED_GENESIS;
const seed = readFileSync('/work/signer', 'utf8').trim();
await cryptoWaitReady();
const pair = new Keyring({ type: 'sr25519', ss58Format: 42 }).addFromUri(seed);
const chain = await (await fetch(`${SPONSOR}/chain`)).json();
if (chain.genesisHash !== GENESIS) { console.log(`refusing: sponsor reports genesis ${chain.genesisHash}`); process.exit(1); }
const issuedAt = String(Date.now());
const message = `flop-sponsor/v1|miner|${chain.genesisHash}|${pair.address}|${issuedAt}`;
const signature = `0x${Buffer.from(pair.sign(new Uint8Array(Buffer.from(message, 'utf8')))).toString('hex')}`;
console.log(`Miner address: ${pair.address}`);
const res = await fetch(`${SPONSOR}/sponsor/miner`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ address: pair.address, issuedAt, signature }),
});
const result = await res.json().catch(() => ({}));
// Machine-readable line for the shell wrapper: status, action (retry_later | fix_request | report_to_operator).
const action = result.action ?? ((res.status === 503 || res.status === 429) ? 'retry_later' : 'report_to_operator');
console.log('RESULT ' + JSON.stringify({ status: res.status, action, reason: result.reason ?? null,
  error: result.error ?? null, retryAfterSeconds: result.retryAfterSeconds ?? 0 }));
// 200/201 registered; 409 already sponsored (treated as done). Exit 10 = retry later, 2 = stop and report.
if (res.ok || res.status === 409) process.exit(0);
process.exit(action === 'retry_later' ? 10 : 2);
EOF
  chmod 644 "$D/sponsor.mjs"
}

is_registered() {
  cli --network devnet --rpc "$RPC" miner status "$(address)" --json 2>/dev/null \
    | jq -e '(.registered // false) | if type == "boolean" then . else (. != null and . != 0) end' >/dev/null 2>&1
}

sponsor_once() {
  docker run --rm -v "$D:/work" -e SPONSOR_URL="$SPONSOR_URL" -e EXPECTED_GENESIS="$GENESIS" \
    --entrypoint node "$(envget FLOP_CLI)" /work/sponsor.mjs
}

# Read-only check of the public limits page (docs: read it before any request and before any retry).
sponsor_limits_open() {
  local st
  st=$(curl -fsS -m 20 --proto '=https' "$SPONSOR_URL/limits" 2>/dev/null | jq -r '.sponsor.miner.status // "unreadable"' 2>/dev/null) || st=unreadable
  echo "  sponsor.miner.status = ${st:-unreadable}"
  [ "$st" = open ]
}

# Ask the sponsor, within the Flop rules: only when the limits page says "open"; wait at least
# retryAfterSeconds on retry_later; stop at once on fix_request / report_to_operator (no loop on those).
# $1 = maximum number of attempts or status checks (each separated by at least 5 minutes: rent runs meanwhile).
sponsor_until_ok() {
  local tries="$1" i out rc line wait
  for i in $(seq 1 "$tries"); do
    if ! sponsor_limits_open; then
      [ "$i" -lt "$tries" ] || { echo "sponsor is not open: not asking. Rerun ./flop-miner-linux.sh sponsor later."; return 1; }
      echo "  not open: checking again in 5 min (Ctrl+C is safe; nothing is registered yet)"; sleep 300; continue
    fi
    out=$(sponsor_once 2>&1) && rc=0 || rc=$?
    line=$(grep '^RESULT ' <<<"$out" | tail -n 1 | cut -c8- || true)
    if [ "$rc" -eq 0 ]; then echo "  sponsor ok: ${line:-done}"; return 0; fi
    echo "$(date '+%H:%M') sponsor answered: ${line:-$(tail -n 3 <<<"$out" | cut -c1-200)}"
    if [ "$rc" -ne 10 ]; then
      echo "  not retryable (fix the request or report the reason above to Flop): stopping, no retry."
      return 1
    fi
    [ -n "$line" ] || line='{}'
    wait=$(jq -r '.retryAfterSeconds // 0' <<<"$line" 2>/dev/null || echo 0)
    [ "${wait:-0}" -ge 300 ] 2>/dev/null || wait=300
    [ "$i" -lt "$tries" ] || return 1
    echo "  retry_later: next try in $((wait / 60)) min, after rereading the limits (Ctrl+C is safe)"; sleep "$wait"
  done
  return 1
}

# Verify the vendored official helper (sha256) before using it.
vendored() {
  local f="$HERE/$1"
  [ -f "$f" ] || die "missing $1 next to this script: copy it from the same folder"
  [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$2" ] || die "$1 does not match the pinned sha256 (Flop miner pack devnet-ts-20261106): do not use it"
}

# Official check (Flop miner pack): the decode policy in miner.env must hash to DECODE_POLICY_HASH and be
# allowlisted for MODEL_HASH by the FINALIZED chain. Fail closed.
check_decode_policy() {
  vendored decode-policy.mjs "$SHA_DECODE_POLICY"
  local out
  out=$(docker run --rm --network host -e FLOP_RPC="$RPC" \
    -v "$D/miner.env:/work/miner.env:ro" -v "$HERE/decode-policy.mjs:/app/decode-policy.mjs:ro" \
    --entrypoint node "$(envget FLOP_CLI)" /app/decode-policy.mjs /work/miner.env 2>&1) \
    || { echo "$out" | grep -v -e 'API/INIT' -e 'RPC-CORE' | tail -n 8 >&2; die "decode policy is not allowlisted by the chain: not starting"; }
  echo "  decode policy: $(grep -v -e 'API/INIT' -e 'RPC-CORE' <<<"$out" | tail -n 1)"
}

cmd_prereqs() {
  grep -q 'Ubuntu' /etc/os-release || die "this expects Ubuntu 22.04/24.04"
  command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 \
    || die "no working NVIDIA driver (nvidia-smi fails): pick a VM image with the driver installed"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
  local vram; vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n 1 | tr -d ' ')
  [ "${vram:-0}" -ge 15500 ] || die "GPU has ${vram:-?} MiB of VRAM; Flop's miner pack asks for at least 16 GB"
  step "Docker Engine (official apt repository)"
  sudo apt-get update -qq && sudo apt-get install -y -qq ca-certificates curl gnupg jq openssl >/dev/null
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  local codename; codename=$(sed -n 's/^UBUNTU_CODENAME=//p' /etc/os-release)
  [ -n "$codename" ] || codename=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release)
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  step "NVIDIA Container Toolkit (official apt repository)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit >/dev/null
  sudo nvidia-ctk runtime configure --runtime=docker >/dev/null
  sudo systemctl restart docker
  step "GPU inside Docker"
  sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L || die "Docker cannot see the GPU"
  echo
  echo "prereqs done. Next: ./flop-miner-linux.sh setup"
}

cmd_setup() {
  step "packages (Ubuntu official)"
  sudo apt-get update -qq && sudo apt-get install -y -qq jq openssl curl ca-certificates >/dev/null

  step "official release list: $RELEASES_URL"
  local rel cli_img worker_img backend_img policy_hash policy img
  rel=$(curl -fsS --proto '=https' "$RELEASES_URL") || die "cannot read $RELEASES_URL"
  cli_img=$(jq -r '.cli.image' <<<"$rel")
  worker_img=$(jq -r '."miner-worker".image' <<<"$rel")
  backend_img=$(jq -r '."miner-vllm-toploc".image' <<<"$rel")
  for img in "$cli_img" "$worker_img" "$backend_img"; do
    [[ "$img" =~ ^ghcr\.io/flop-labs/flop-core/[a-z0-9-]+:v[0-9.]+@sha256:[0-9a-f]{64}$ ]] \
      || die "release list returned an unexpected image reference: $img"
    echo "  $img"
  done
  jq -e --arg m "$MODEL_HASH" '.models[$m].name' <<<"$rel" >/dev/null || die "model $MODEL_HASH is not in the official release list"
  policy_hash=$(jq -r --arg m "$MODEL_HASH" '.models[$m].decode_policies | keys[0]' <<<"$rel")
  [[ "$policy_hash" =~ ^0x[0-9a-f]{64}$ ]] || die "no decode policy for the model in the release list"
  policy=$(jq -c --arg m "$MODEL_HASH" --arg p "$policy_hash" '.models[$m].decode_policies[$p]' <<<"$rel")
  echo "  model $MODEL_HASH ($MODEL_NAME), decode policy $policy_hash"
  local rel_v min_v
  rel_v=$(sed -E 's/.*:v([0-9.]+)@.*/\1/' <<<"$cli_img"); min_v=$(sed -E 's/.*:v([0-9.]+)@.*/\1/' <<<"$MIN_CLI")
  if [ "$(printf '%s\n%s\n' "$rel_v" "$min_v" | sort -V | tail -n 1)" != "$rel_v" ]; then
    echo "  cli v$rel_v in the release list is older than the chain needs; using official $MIN_CLI"
    cli_img="$MIN_CLI"
  fi

  step "settings in $D"
  sudo install -d -o "$USER" -g "$USER" -m 700 "$D"
  install -d -m 700 "$D/models" "$D/session-keys"
  [ -f "$D/miner.env" ] || install -m 600 /dev/null "$D/miner.env"
  envset FLOP_CLI "$cli_img"
  envset FLOP_WORKER_IMAGE "$worker_img"
  envset FLOP_BACKEND_IMAGE "$backend_img"
  envset FLOP_RPC "$RPC"
  envset GHOST_MAILBOX_URL "$MAILBOX"
  { [ -n "$WORKER_NAME_ARG" ] || ! grep -q "^WORKER_NAME=" "$D/miner.env"; } && envset WORKER_NAME "$WORKER_NAME"
  envset MODEL_NAME "$MODEL_NAME"
  envset OPENROUTER_MODEL_SLUG "$MODEL_SLUG"
  envset MODEL_HASH "$MODEL_HASH"
  envset MODEL_PRECISION BF16
  envset TOPLOC_PRECISION bf16
  envset DECODE_POLICY_HASH "$policy_hash"
  envset DECODE_POLICY "'$policy'"
  { [ -n "$GPU_UTIL_ARG" ] || ! grep -q "^GPU_MEMORY_UTILIZATION=" "$D/miner.env"; } && envset GPU_MEMORY_UTILIZATION "$GPU_UTIL"
  grep -q '^TOPLOC_ENABLED=' "$D/miner.env" || envset TOPLOC_ENABLED 1
  grep -q '^INFERENCE_API_KEY=[0-9a-f]\{64\}$' "$D/miner.env" || envset INFERENCE_API_KEY "$(openssl rand -hex 32)"
  grep -q '^SESSION_AUTH_TOKEN=[0-9a-f]\{64\}$' "$D/miner.env" || envset SESSION_AUTH_TOKEN "$(openssl rand -hex 32)"
  grep -q '^SESSION_PUBLIC_HOST=' "$D/miner.env" || envset SESSION_PUBLIC_HOST ""
  chmod 600 "$D/miner.env"
  write_compose
  write_sponsor
  compose config -q || die "docker-compose.yml did not validate"

  step "images by digest from ghcr.io/flop-labs (backend is ~23 GB)"
  docker pull -q "$cli_img"
  compose pull -q

  step "decode policy (official allowlist check against the finalized chain)"
  check_decode_policy

  step "miner key (new devnet-only account)"
  if ! sudo test -s "$D/signer"; then
    local tmp; tmp=$(mktemp "$D/.key.XXXXXX"); chmod 600 "$tmp"
    cli keys generate --type sr25519 --show-secret --json > "$tmp"
    (umask 077; jq -r .mnemonic "$tmp" > "$D/signer"; jq -r .address "$tmp" > "$D/address")
    shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
    [ -s "$D/address" ] || die "key generation failed"
  fi
  sudo test -s "$D/session-keys/enclave.seed" || openssl rand -hex 32 | sudo tee "$D/session-keys/enclave.seed" >/dev/null
  # The worker refuses a signer that is not root-owned with mode 0600.
  sudo chown root:root "$D/signer" "$D/session-keys/enclave.seed"
  sudo chmod 600 "$D/signer" "$D/session-keys/enclave.seed"
  echo "miner address: $(address)"

  step "stake sponsor (official faucet.flop.network)"
  if is_registered; then
    echo "already registered on chain: skipping the sponsor"
  else
    sponsor_until_ok "${SPONSOR_TRIES:-6}" || die "no sponsorship now; setup is safe to rerun (it keeps the key). Check https://faucet.flop.network/limits first"
  fi

  step "model (the CLI checks every file against the on-chain record)"
  local i
  for i in 1 2 3; do
    if cli --network devnet --rpc "$RPC" model fetch "$MODEL_HASH" --precision BF16 \
         --output-dir /work/models/qwen2.5-0.5b-instruct --json \
         | jq -e --arg r "${MODEL_HASH#0x}" '(.ok == true) or (.verified_root == $r)' >/dev/null; then echo "model ok (verified root matches the chain)"; break; fi
    [ "$i" -lt 3 ] || die "model fetch failed 3 times"
    echo "model fetch failed; retrying"; sleep 10
  done

  step "tunnel (cloudflared from Cloudflare's signed apt repository)"
  if ! command -v cloudflared >/dev/null; then
    sudo install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y -qq cloudflared >/dev/null
  fi
  sudo tee /etc/systemd/system/flop-tunnel.service >/dev/null <<'EOF'
[Unit]
Description=FLOP miner HTTPS tunnel (forwards only to the worker session port)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8645
Restart=always
RestartSec=5
DynamicUser=yes
StateDirectory=flop-tunnel
Environment=HOME=/var/lib/flop-tunnel
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
EOF
  sudo systemctl daemon-reload
  echo
  echo "setup done. Next: ./flop-miner-linux.sh up"
}

default_mtu() {
  local iface mtu
  iface=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
  mtu=$(cat "/sys/class/net/${iface:-eth0}/mtu" 2>/dev/null || echo 1500)
  [ "$mtu" -le 1500 ] || mtu=1500
  echo "$mtu"
}

cmd_up() {
  address >/dev/null
  envset NET_MTU "$(default_mtu)"
  grep -q '^SESSION_COMMIT_IDS=' "$D/miner.env" || envset SESSION_COMMIT_IDS 1
  write_compose
  compose config -q || die "docker-compose.yml did not validate"
  step "decode policy (re-checked on every start)"
  check_decode_policy
  if docker network inspect flop-miner_default >/dev/null 2>&1 && \
     [ "$(docker network inspect flop-miner_default -f '{{index .Options "com.docker.network.driver.mtu"}}')" != "$(envget NET_MTU)" ]; then
    echo "recreating the miner network with MTU $(envget NET_MTU)"
    compose down
  fi
  step "tunnel"
  local since host="" i signer
  since=$(date '+%Y-%m-%d %H:%M:%S')
  sudo systemctl restart flop-tunnel
  for i in $(seq 1 30); do
    host=$(sudo journalctl -u flop-tunnel --since "$since" --no-pager | grep -o '[a-z0-9-]*\.trycloudflare\.com' | tail -n 1 || true)
    [ -n "$host" ] && break; sleep 2
  done
  [ -n "$host" ] || die "no tunnel address after 60 s: sudo journalctl -u flop-tunnel -n 30"
  envset SESSION_PUBLIC_HOST "$host"
  echo "public address: https://$host (answers 401 without the session token)"

  step "backend (model on the GPU)"
  compose up -d backend
  for i in $(seq 1 120); do
    backend_ready && break
    if compose logs --since 15m backend 2>/dev/null | grep -qiE 'out of memory|less than desired GPU memory|Traceback'; then
      compose logs --tail 25 backend; die "backend failed to start (see above); try: GPU_UTIL=0.5 ./flop-miner-linux.sh setup"
    fi
    sleep 5
  done
  backend_ready || die "backend not ready after 10 min: ./flop-miner-linux.sh status"
  echo "backend ready"

  step "worker"
  compose up -d --force-recreate worker
  sleep 20
  compose logs --tail 40 worker | grep -E 'Signer:|model-gate|Session host|Endpoint ad|answered task|calibration|rror' || true
  signer=$(compose logs worker | grep -o 'Signer: [0-9A-Za-z]*' | tail -n 1 | cut -d' ' -f2 || true)
  if [ -n "$signer" ] && [ "$signer" != "$(address)" ]; then
    compose stop worker; die "worker signed as $signer, not your miner $(address); stopped it"
  fi
  echo
  echo "mining. The miner keeps running until ./flop-miner-linux.sh down. Check it with: ./flop-miner-linux.sh status"
}

cmd_status() {
  local a; a=$(address)
  step "miner $a"
  cli --network devnet --rpc "$RPC" miner status "$a" --json | jq '{registered, stake, calibration}' || true
  echo "explorer: https://explorer.devnet.flop.network/account/$a"
  step "tunnel"; echo "https://$(envget SESSION_PUBLIC_HOST)  ($(systemctl is-active flop-tunnel || true))"
  step "containers"; compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
  step "GPU"; nvidia-smi --query-gpu=name,power.draw,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv
  step "worker (last test jobs / calibration)"; compose logs worker 2>/dev/null | grep -E 'answered task|calibration' | tail -n 5 || true
}

cmd_sponsor() { if is_registered; then echo "already registered; asking for a stake top-up"; fi; sponsor_until_ok "${SPONSOR_TRIES:-6}"; }

# Deregister the miner on chain (official retire.mjs): sponsored stake unbonds over 7 days and returns to the
# sponsor. An open session or dispute refuses it until it settles. Stop the worker first.
cmd_retire() {
  # The miner can earn devnet FLOP from compute channels (the 9 Nov 2026 test miner ended with a balance on its
  # address). The key lives only on this VM and is lost when the instance is deleted, so keep an encrypted copy.
  [ -f "$BACKUP_FILE" ] || [ "${SKIP_KEY_BACKUP:-}" = 1 ] || \
    die "no key backup at $BACKUP_FILE. Run ./flop-miner-linux.sh backup-key and copy the file to your PC first (or SKIP_KEY_BACKUP=1 ./flop-miner-linux.sh retire to give the key up)."
  vendored retire.mjs "$SHA_RETIRE"
  step "stopping the worker, then deregistering $(address)"
  compose stop worker || true
  local out rc
  out=$(docker run --rm --network host -e DRY_RUN -e FLOP_RPC="$RPC" \
    -v "$D/signer:/work/signer:ro" -v "$HERE/retire.mjs:/app/retire.mjs:ro" \
    --entrypoint node "$(envget FLOP_CLI)" /app/retire.mjs /work/signer 2>&1) && rc=0 || rc=$?
  grep -v -e 'API/INIT' -e 'RPC-CORE' <<<"$out" || true
  [ "$rc" -eq 0 ] || die "not retired (see above). Do not delete the VM until it is, or the stake stays bonded."
  echo "retired. You can now delete the rented instance (stopping it is not enough: billing continues)."
}
# Encrypt the miner key (the `signer` file) and the public address with a passphrase you type (asked twice).
# The key is never printed. Copy the result to your PC (scp), then decrypt there with:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in flop-miner-key-backup.enc | tar x
cmd_backup_key() {
  [ "$(id -u)" -eq 0 ] || die "run as root (the key file is root-only)"
  [ -f "$D/signer" ] || die "no key at $D/signer"
  command -v openssl >/dev/null || die "openssl not found"
  [ -t 0 ] || die "needs a terminal to ask for the passphrase"
  step "encrypting the miner key for $(address)"
  echo "Choose a passphrase and keep it somewhere safe; without it the backup is useless."
  ( umask 077; tar -C "$D" -cf - signer address | openssl enc -aes-256-cbc -pbkdf2 -salt -out "$BACKUP_FILE" )
  [ -s "$BACKUP_FILE" ] || die "backup is empty; try again"
  echo "backup written: $BACKUP_FILE ($(stat -c %s "$BACKUP_FILE") bytes)"
  echo "Copy it to your PC now. From PowerShell on the PC (adapt port and IP):"
  echo "  scp -i \"\$env:USERPROFILE\\.ssh\\vast_flop\" -P <port> root@<ip>:$BACKUP_FILE ."
  echo "Never commit this file or share it."
}
cmd_logs()    { compose logs -f --tail 50 worker; }
cmd_down() {
  compose stop worker backend || true
  sudo systemctl stop flop-tunnel || true
  echo "stopped. Start again with: ./flop-miner-linux.sh up   (or destroy the rented instance to stop paying)"
  echo "Before deleting the instance: ./flop-miner-linux.sh backup-key (copy the file to your PC), then ./flop-miner-linux.sh retire."
}

usage() { sed -n '2,37p' "$0"; }
case "${1:-}" in
  prereqs) cmd_prereqs; exit ;;
  version) echo "flop-miner-linux.sh $VERSION"; exit ;;
  backup-key) cmd_backup_key; exit ;;
  setup|up|status|logs|sponsor|down|retire) ;;
  *)       usage; exit 1 ;;
esac
need_docker
case "${1:-}" in
  setup)   cmd_setup ;;
  up)      cmd_up ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  sponsor) cmd_sponsor ;;
  down)    cmd_down ;;
  retire)  cmd_retire ;;
esac
