# Technocore Multi-Agent DID Demo

Experimental multi-agent system for authenticated AI-to-AI coordination on [Technocore](https://technocore.chat).

The project started as a simple signed-agent demo and has evolved into a guarded autonomous workflow where external Technocore messages can be evaluated by multiple AI roles before a response is signed and published.

## Current direction

The current development architecture is:

```text
Technocore room
      |
      v
External-message filters
      |
      v
Research Agent
      |
      v
Critic Agent
      |
      v
Judge Agent
      |
      v
Signed Research response
      |
      v
Local interaction log
```

A response is only eligible for autonomous publication when the Research Agent chooses to respond and both Critic and Judge approve it.

The goal is not to generate activity for its own sake. The system is being designed to prefer concrete technical questions, debugging, protocol review and explicit agent-to-agent collaboration while filtering repetitive check-ins and low-value room noise.

## Agents

### Research Agent

Produces a concise technical response or decides that an external message should be ignored.

Public DID:

```text
did:key:z6MkkPtvJEneCieb8AVphWVuEcxihMs2BK9HCETMtRQjFuAv
```

### Critic Agent

Reviews the Research response for unsupported assumptions, protocol mistakes and weak reasoning.

Public DID:

```text
did:key:z6MkiLc24vTBQwtXCWw5gAfwHL7Nvy3xszK7rUpgPnaWictM
```

### Judge Agent

Performs the final independent review before an autonomous signed response can be sent.

The three roles intentionally separate generation, criticism and final approval rather than allowing a single model call to publish directly.

## Signed Technocore messages

Signed messages use persistent Ed25519 `did:key` identities.

The signed payload is:

```text
room|nonce|text
```

The signature is base64url encoded and sent through Technocore's signed-message endpoint.

For a signing identity in a room, the nonce must advance. The current implementation uses a high-resolution time-based nonce and keeps the private signing key local.

## Reliability and safety guardrails

The listener treats every external Technocore message as untrusted data. External room content is never treated as a command to execute.

Current guardrails include:

- persistent DID identity verification before signing;
- exact Ed25519 signing of `room|nonce|text`;
- relay/wrapper detection;
- exact duplicate filtering;
- similarity filtering for repeated messages;
- noise/check-in filtering;
- topic and interaction pre-filtering;
- Research -> Critic -> Judge approval before autonomous posting;
- bounded response length;
- local JSONL logging of signed interactions;
- no private keys or API secrets in logs or GitHub.

The agents are also instructed not to invent coordination guarantees that are not provided by the underlying communication layer, such as consensus, distributed locks, leader election, transactions, exactly-once delivery or confidentiality.

## Real signed interactions

The Research DID has already published successful signed responses during live Technocore testing.

Examples of observed successful sends include:

```text
seq 7565262
Research DID ...FuAv
Topic: did:key identity changes and signed-message verification
```

```text
seq 7573787
Research DID ...FuAv
Topic: Ed25519 verification, exact payload matching and nonce replay protection
```

A later live test also detected an explicit agent-to-agent coordination request and passed it through Research, Critic and Judge approval. That run exposed an important limitation in the first autonomous policy: a fixed per-session send cap could consume capacity before higher-value collaboration opportunities appeared.

That observation is now guiding the next rate-control iteration.

## Development status

### Implemented

- unsigned Technocore agent experiments;
- persistent Ed25519 `did:key` identities;
- signed Research, Critic and Judge agents;
- bounded Research -> Critic -> Judge workflow;
- live Technocore room listener;
- external-message safety prompt;
- duplicate, relay, noise and similarity filtering;
- autonomous signed posting after multi-agent approval;
- local signed-interaction journal.

### In progress

- replacing the fixed per-session send cap with persistent rate control;
- cooldown between signed responses;
- rolling 24-hour successful-send quota;
- prioritization of explicit collaboration and concrete technical requests;
- persistence of anti-similarity state across restarts;
- deployment on an always-on host for 24/7 operation.

## Repository files

`agent.py`  
Basic Technocore agent example.

`gpt_agent.py`  
GPT-powered Technocore agent experiment.

`research_agent.py` / `critic_agent.py`  
Earlier unsigned multi-agent experiments.

`signed_agent.py`  
Single signed DID experiment.

`research_agent_signed.py`  
Persistent signed Research Agent.

`critic_agent_signed.py`  
Persistent signed Critic Agent.

`judge_agent_signed.py`  
Persistent signed Judge Agent and final reviewer.

The autonomous room listener is under active local development and will be published once its persistent rate-control policy is finalized.

## Security

Private identity files contain private key material and must never be committed:

```text
research_identity.json
critic_identity.json
judge_identity.json
flop_agent_identity.json
```

The OpenAI API key is loaded from the local environment and is never stored in the repository.

Only public `did:key` identifiers and non-secret interaction metadata should be shared publicly.

## Why this experiment

The project explores a practical pattern for verifiable autonomous coordination:

```text
AI reasoning
     +
separate review roles
     +
persistent DID identity
     +
signed messages
     +
shared Technocore communication
     =
guarded agent-to-agent interaction
```

The emphasis is on useful, authenticated interaction rather than repetitive autonomous posting.

## Status

Active development. Live signed Technocore tests are ongoing and the next milestone is persistent autonomous rate control followed by 24/7 deployment.
