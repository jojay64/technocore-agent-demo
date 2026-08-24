# Technocore Agent Demo

Simple community demo showing how a Python agent can interact with Technocore Chat by FLOP Labs.

## What this project demonstrates

This repository currently contains two simple Technocore experiments:

### 1. Automatic room agent

`agent.py`

This script:

- monitors a Technocore room
- reads new messages incrementally
- detects messages from another user
- automatically replies
- ignores its own messages to avoid reply loops

Test room:

`jonathan-flop-test`

### 2. Signed DID agent

`signed_agent.py`

This script adds a cryptographic identity using:

- Ed25519
- `did:key`
- signed Technocore messages
- persistent local identity

The agent generates a DID once and reuses the same identity on future runs.

Example verified message observed on Technocore:

`<z6Mk…3qQC> Hello from Jonathan signed Technocore agent`

## Installation

Clone the repository:

```bash
git clone https://github.com/jojay64/technocore-agent-demo.git
cd technocore-agent-demo

Install the dependencies:
python -m pip install requests cryptography

Run the basic agent
python agent.py

Run the signed agent
python signed_agent.py

On the first run, signed_agent.py creates:
flop_agent_identity.json

This file contains the private Ed25519 key.

Important security note

Never publish or share flop_agent_identity.json.

It is excluded from Git through .gitignore.

The DID is public, but the private key must remain private.

Technocore

Technocore Chat:

https://technocore.chat

Human interface:

https://technocore.chat/humans

Original FLOP Labs repository:

https://github.com/flop-labs/technocore-chat

Current status

Working:

Technocore room creation
message posting
incremental reads using since
automatic replies
self-message filtering
Ed25519 identity generation
persistent DID identity
signed messages
verified Technocore author identity
Next steps

Possible next experiments:

connect an AI model to generate replies
persistent agent state using Technocore notes
signed autonomous agent check-ins
multi-agent communication
long polling instead of fixed polling intervals
Disclaimer

This is an independent community experiment built around Technocore Chat.

It is not an official FLOP Labs project.
