# Technocore Multi-Agent DID Demo

A small experimental project showing how autonomous AI agents can coordinate through [Technocore](https://technocore.chat) using shared rooms, incremental reads, long-polling, persistent state, and signed `did:key` identities.

The current demo uses three autonomous agents:

- **Research Agent** — produces and improves a technical proposal.
- **Critic Agent** — challenges unsupported assumptions and weak reasoning.
- **Judge Agent** — reviews the final proposal and returns an independent verdict.

Each agent has its own persistent Ed25519 `did:key` identity and sends signed messages through Technocore.

---

## Architecture

```text
             Technocore Room
            flop-agent-lab

     ┌─────────────────────────┐
     │                         │
     ▼                         │
Research Agent ───────► Critic Agent
     │                         │
     ◄─────────────────────────┘
     │
     │ FINAL proposal
     ▼
 Judge Agent
     │
     ▼
 Signed verdict

The agents do not communicate directly with each other locally.

Every message is published to the Technocore room and then read back by the other agents.

Signed Agent Identities

The demo currently uses three separate persistent identities.

Research Agent
did:key:z6MkkPtvJEneCieb8AVphWVuEcxihMs2BK9HCETMtRQjFuAv

Critic Agent
did:key:z6MkiLc24vTBQwtXCWw5gAfwHL7Nvy3xszK7rUpgPnaWictM

Judge Agent
did:key:z6Mkpb4AReHGkq88YXG5JDW6rC4dvzb3mPToXieZUf3XjoRG

Each agent only accepts messages from the DID it expects.

This avoids relying on unsigned nicknames for agent identity.

Workflow

The demo follows this sequence:
Research
   │
   ▼
Initial proposal
   │
   ▼
Critic
   │
   ▼
Critique
   │
   ▼
Research
   │
   ▼
Improved proposal
   │
   ▼
Critic
   │
   ▼
Further critique
   │
   ▼
Research
   │
   ▼
FINAL proposal
   │
   ▼
Judge
   │
   ▼
JUDGE verdict

The conversation is intentionally bounded to avoid uncontrolled autonomous loops.

Technocore Primitives Used

The experiment intentionally stays close to the primitives used by Technocore:

Shared rooms
Incremental message reads
Long-polling
Persistent notes / KV state
Signed Ed25519 did:key identities

The agents are explicitly instructed not to assume capabilities such as:

consensus
distributed locks
leader election
schedulers
transactions
exactly-once delivery
confidentiality

unless those capabilities are explicitly established.

This is important because application-level coordination patterns should not be confused with guarantees provided by the communication layer itself.

Signed Messages

Messages are signed using Ed25519.

The signed payload follows this structure:

room|nonce|text

The signature is encoded using base64url and sent through Technocore's signed-message endpoint.

Each agent keeps its private key locally and reuses the same identity between runs.

Files

agent.py

Basic Technocore agent example.

gpt_agent.py

GPT-powered Technocore agent.

research_agent.py
critic_agent.py

Earlier unsigned two-agent experiment.

signed_agent.py

Single-agent signed DID experiment.

research_agent_signed.py

Signed Research Agent.

critic_agent_signed.py

Signed Critic Agent.

judge_agent_signed.py

Signed Judge Agent and final reviewer.

Running the Three-Agent Demo

Open three terminals.

1. Start the Judge Agent
python judge_agent_signed.py

The Judge waits for a signed FINAL: message from the Research Agent.

2. Start the Critic Agent
python critic_agent_signed.py
The Critic waits for signed messages from the Research Agent.

3. Start the Research Agent
python research_agent_signed.py

The Research Agent automatically generates the first proposal and starts the autonomous discussion.

The complete flow then happens through Technocore.

Example Final Result

A successful run ends with a signed Judge verdict similar to:

JUDGE:
SUPPORTED: proposal stays within the documented primitives.
LIMITATION: stronger coordination guarantees are not assumed.
VERDICT: PASS.

Security

Private keys are stored locally in files such as:

research_identity.json
critic_identity.json
judge_identity.json
flop_agent_identity.json

These files contain private key material and must never be committed to GitHub.

They are excluded through .gitignore.

Only the public did:key identifiers should be shared publicly.

AI Model

The agents currently use the OpenAI API for reasoning.

The API key is loaded from the local environment and is not stored in the repository.

Example:

OPENAI_API_KEY

The communication layer itself remains Technocore.

Goal

The goal of this project is to explore a simple pattern for authenticated autonomous agent coordination:
AI reasoning
      +
persistent DID identity
      +
signed messages
      +
shared Technocore communication
      =
verifiable multi-agent interaction

This repository is experimental and intended as a practical proof of concept for building autonomous agents on top of Technocore.

