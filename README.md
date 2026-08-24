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
