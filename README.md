# Technocore Agent Demo

Simple Python agent demo using Technocore Chat by FLOP Labs.

This project demonstrates how a basic agent can:

- monitor a Technocore room
- detect new messages
- ignore its own messages
- automatically reply to another user

## Test room

Room used for this demo:

`jonathan-flop-test`

Technocore:

https://technocore.chat

Original project:

https://github.com/flop-labs/technocore-chat

## How it works

The Python script checks the Technocore room every 5 seconds.

When it detects a message from `jonathan`, it automatically replies as:

`python-agent`

Current test reply:

`message received by python agent`

## Installation

Clone the repository:

```bash
git clone https://github.com/jojay64/technocore-agent-demo.git
cd technocore-agent-demo
