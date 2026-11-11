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
./flop-miner-linux.sh backup-key                     # encrypt the miner key, copy the file to your PC
./flop-miner-linux.sh down && ./flop-miner-linux.sh retire   # then DELETE the instance
```

Stopping Compose does not stop VM billing: delete the instance in the provider console. Unanswered test
jobs cut the sponsored stake, so run `down` before the VM is stopped or its credit runs out.

The miner key lives only on the VM. The miner can earn devnet FLOP from compute channels, so `backup-key`
encrypts the key with a passphrase you type; copy the `.enc` file to your PC before deleting the instance.
`retire` refuses to run without that backup (`SKIP_KEY_BACKUP=1` gives the key up on purpose).

## Monitoring

`flop-watch.sh` is a read-only health check (containers, tunnel, calibration, RPC drops, `[session]` channel
lines, GPU, optional rent reminder). Exit code 0, 1 or 2 for OK, WARN, CRIT.

```sh
CREDIT_USD=25 RATE_USD_H=0.72 ./flop-watch.sh
```

Calibration jobs ("answered task") only come in one burst after start. Compute-channel activity shows up
under `[session]` in the worker log and as `computeChannel.*` transactions on the miner's explorer page.

## More

See [NOTES.md](NOTES.md) for the results of the first run, the mistakes to avoid, network facts and the
full procedure.

## Status

Tested once on a rented RTX 4090 VM (9 to 11 Nov 2026): calibration accepted on the FLOP devnet, compute
channels served, then retired cleanly. The script has no automated tests (it needs Docker, a GPU and the live
devnet); only its sponsor retry logic, the `retire` backup guard and the `flop-watch.sh` counting were
exercised offline with mocked answers.
