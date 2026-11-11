# FLOP devnet miner: project notes (read this before any new miner work)

Facts, results and lessons from the first run (9 to 11 Nov 2026). Written so that the next session does not
have to rediscover them or repeat the mistakes listed below. Keep it factual: update it when something is
verified, and mark guesses as guesses.

## Scope and rules

- The miner is **separate from the three Technocore services** (`technocore-agent`, `technocore-pulse`,
  `technocore-tclk`). Those services were the first chain test (DIDs, technocore.chat). The miner is a
  different topic: FLOP **devnet**. Nothing Technocore (identity, `.env`, OpenAI key) goes on a miner VM.
- Devnet FLOP has no value, no rewards promised, no airdrop weight (official docs). Testnet or mainnet is a
  separate decision: do not anticipate or enable it without an explicit request, and read the official docs
  first (the testnet may involve real value).
- Never create accounts, pay, or handle the user's SSH key. Never connect to a rented VM on the user's behalf:
  give the user commands to paste, one per block. Never ask for or print `signer`, `miner.env`, or any private
  key. The miner address is public.
- Official Flop helper files (`decode-policy.mjs`, `retire.mjs`) are not redistributed here (license unknown);
  see README.md for how to fetch and verify them.

## What happened (first run)

- Rented one RTX 4090 VM on Vast.ai (full VM template, Ubuntu 22.04, about $0.72/h). Docker-in-container
  instances do not work: a full VM with systemd is required.
- `setup` succeeded (sponsor request accepted), `up` succeeded, calibration accepted: **130 GFLOP/s from 64
  verified jobs**, SOFT tier, 1,000,000 FLOP stake **delegated by the sponsor** (`self` stake 0).
- Ran about 14 h, then `down`, `retire` (deregistered in a finalized block), and the instance was deleted.
- Miner address (public): `5FFLqT9CmkJkRj3xgpo5yz7wzjPAYFQuMJkNrGMo8patRPZX`
  (<https://explorer.devnet.flop.network/account/5FFLqT9CmkJkRj3xgpo5yz7wzjPAYFQuMJkNrGMo8patRPZX>).
- On 11 Nov the explorer page of that address showed **many `computeChannel.registerSessionEvidence`,
  `submitToplocEvidence` and `settle` extrinsics, all successful, and a balance of 234.4425 devnet FLOP**
  (nonce 32). The key existed only on the VM and was lost with the instance: **that balance cannot be moved**.
  It is worth nothing on devnet, but the procedure below now backs the key up.

## Mistakes made, do not repeat

1. **"No jobs after calibration" was wrong.** `flop-watch.sh` only counted `answered task` lines (calibration
   "ghost" tasks, one burst after start). Paid-session / compute-channel traffic from the gateway arrives
   separately and was not counted, so the miner looked idle while it was working. Always check the explorer
   account page (transactions tab) before concluding anything about activity. `flop-watch.sh` now also counts
   `[session]` log lines; the exact wording after that prefix has **never been captured**, so verify it on the
   next run (`docker logs flop-miner-worker-1 | grep -i -E 'session|channel'`).
2. **The key was lost with the VM.** Run `backup-key` and copy the file to the PC before `retire` and before
   deleting the instance (`retire` now refuses without a backup unless `SKIP_KEY_BACKUP=1`).
3. **Retiring stopped a working miner.** Before retiring, look at the explorer: if channels are being served,
   decide knowingly (retire unbonds the stake over 7 days and the sponsored stake goes back to the sponsor).
4. The `[session] error: AbortError` line seen after startup was dismissed as harmless. It was most likely a
   gateway probe or channel attempt. Treat `[session]` lines as meaningful.
5. Calibration check failures logged as `disconnected from wss://rpc.devnet.flop.network: 1006` are RPC drops
   (about every 6 min on the Romania VM, MTU 1500 ruled out), not miner faults. The worker reconnects by itself.

## How the network works (verified on 11 Nov 2026, explorer `/mining`)

- 265 registered miners, 187 calibrated hosts, only 18 to 24 admitted by the gateway at a given moment
  ("Schedulable now"); many miners are registered but offline. Online status needs a worker report within
  15 minutes. Latest standard worker build listed: 0.1.323, minimum 0.1.172. **Not checked: which worker
  version our run used and whether it was admitted to the gateway.**
- Compute Channels tab: 27,820 channels, all seen opened by one agent towards many miners, tiny amounts
  (0 to 0.2 FLOP), mostly unsettled: looks like automated network testing, not paying customers.
- Calibration job burst comes right after start; the calibrated cap expires after 7 days and is refreshed
  with new jobs while the miner stays online. Unanswered jobs past 600 blocks slash stake.
- Paid sessions are officially not open to external miners (docs). A direct session test would need a
  throwaway wallet and explicit approval (at most 0.01 FLOP); end-to-end settlement is unconfirmed.

## Endpoints and constants (devnet)

- RPC `wss://rpc.devnet.flop.network`, mailbox `https://mailbox.devnet.flop.network`, faucet and sponsor
  `https://faucet.flop.network` (`GET /limits`, `POST /sponsor/miner`), explorer
  `https://explorer.devnet.flop.network`, release manifest `https://flop.network/releases.json`.
- Genesis `0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631`, model
  `Qwen/Qwen2.5-0.5B-Instruct` (BF16), CLI floor `v0.1.290` (the manifest pinned an older CLI that fails model
  verification on runtime 1014014).
- Sponsor rules: read `/limits` first; ask only when `sponsor.miner.status` is `open`; on `retry_later` wait at
  least `retryAfterSeconds`; on `fix_request` or `report_to_operator` stop; never loop; one sponsorship per
  account. The script implements this.
- Validators: admission is manual (FLOP operations). A request for an introduction was posted by the user on
  flop-labs/network issue #10 (marked duplicate of #15) on 11 Nov 2026; no answer yet. Do not install a
  validator before getting the official package and an operations contact.

## Procedure for the next run

1. User rents a full GPU VM (16 GB VRAM or more) and gives you nothing secret; you give commands to paste.
2. Copy `flop-miner-linux.sh`, `flop-watch.sh`, `decode-policy.mjs`, `retire.mjs` to the VM (`scp`, one command
   per line), then `prereqs`, `WORKER_NAME=... setup`, `up`.
3. Monitor with `CREDIT_USD=.. RATE_USD_H=.. ./flop-watch.sh` and the explorer account page. Watch rent: the VM
   keeps billing until the instance is **deleted** (Stop is not enough), and if credit runs out the miner stops
   without `down`.
4. End: `./flop-miner-linux.sh backup-key` (passphrase typed on the VM, copy the `.enc` file to the PC with
   `scp`, keep the passphrase), then `down`, `retire`, delete the instance. Decrypt on the PC with
   `openssl enc -d -aes-256-cbc -pbkdf2 -in flop-miner-key-backup.enc | tar x`. Never commit the backup.

## User preferences

French speaker, not a developer. Wants short explanations, one command per code block, no jargon. The user
decides what to run, post, commit or merge; propose and wait. Commits go through a branch and a PR.
