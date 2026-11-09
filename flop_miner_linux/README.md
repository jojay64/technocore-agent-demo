# FLOP devnet miner, Linux (rented GPU VM)

`flop-miner-linux.sh` runs a **FLOP devnet** GPU miner on a rented Ubuntu 22.04/24.04 VM with an NVIDIA GPU
(16 GB VRAM or more). It is a Linux adaptation of the community project
[Makabeez/flop-pc-miner](https://github.com/Makabeez/flop-pc-miner) (Windows/WSL2 only), hardened with
checks from Flop's official miner pack.

This is **separate from the three Technocore services**. It never touches a Technocore identity, `.env`
or the OpenAI key. It generates a new devnet-only miner key on the VM. Devnet FLOP has no value; do not use
it for testnet or mainnet.

## What it does

- Pulls only `ghcr.io/flop-labs/flop-core/*` images, pinned by sha256 digest from
  <https://flop.network/releases.json> (it refuses any other reference).
- Asks the sponsor (`faucet.flop.network`) only when `/limits` shows `sponsor.miner.status = open`, waits at
  least `retryAfterSeconds` on `retry_later`, and stops at once on any other refusal (no retry loop).
- Checks the decode policy against the finalized chain with Flop's official `decode-policy.mjs`.
- Exposes the worker only through a Cloudflare quick tunnel to `127.0.0.1:8645` (token-protected).
- `retire` deregisters the miner on chain before you delete the VM.

## Official helper files (not committed here)

`decode-policy.mjs` and `retire.mjs` come from Flop's official miner pack and are **not redistributed** in
this repository. Download and verify the pack, then copy the two files next to the script. The script checks
their sha256 and refuses any other version.

```sh
B=https://flop.network/releases/cli/miner-pack-devnet-ts-20261106
curl -fLO "$B/flop-miner-pack-devnet-ts-20261106.tar.gz"
echo "35f222f748447a8aabdd015d94260d8facfd29305691c3cf7ed033cbb2b514b1  flop-miner-pack-devnet-ts-20261106.tar.gz" | sha256sum -c -
tar xzf flop-miner-pack-devnet-ts-20261106.tar.gz
cp flop-miner-pack-devnet-ts-20261106/decode-policy.mjs flop-miner-pack-devnet-ts-20261106/retire.mjs .
sha256sum decode-policy.mjs retire.mjs
# expected: 11092584b37910a8d90c6eb1e5799f3a9359ae85c2a5cfe916780aecf2cd3bf1  decode-policy.mjs
#           9989f39d99fef132fde852d8d541a510ab7b14abea736a1a627103656e058871  retire.mjs
```

## Use

Run on the rented VM (a full VM, not a container instance), as root:

```sh
chmod +x flop-miner-linux.sh
./flop-miner-linux.sh prereqs                        # Docker + NVIDIA Container Toolkit
WORKER_NAME=your-name ./flop-miner-linux.sh setup    # images, key, sponsor, model, tunnel
./flop-miner-linux.sh up
./flop-miner-linux.sh status
./flop-miner-linux.sh down && ./flop-miner-linux.sh retire   # then DELETE the instance
```

Stopping Compose does not stop VM billing: delete the instance in the provider console. Unanswered test
jobs cut the sponsored stake, so run `down` before the VM is stopped or its credit runs out.

## Status

Tested once on a rented RTX 4090 VM (calibration accepted on the FLOP devnet, 9 Nov 2026). The script has no
automated tests (it needs Docker, a GPU and the live devnet); only its sponsor retry logic was exercised
offline with mocked answers.
