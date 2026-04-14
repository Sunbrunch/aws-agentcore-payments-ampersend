# AgentCore Payments — Private Preview

Enable AI agents to make x402 cryptocurrency microtransactions using Amazon Bedrock AgentCore Payments.

📖 **[AgentCore Payments Private Beta Guide (PDF)](AgentCore-Payments-Private-Beta-Guide.pdf)** — Start here for the full testing guide, API walkthrough, and setup instructions.

🤖 **[`docs/getting-started.md`](docs/getting-started.md)** — Machine-readable guide optimized for AI agents and coding assistants.

## What's in this repo

| Directory | What it does |
|-----------|-------------|
| `quickstart/` | One-click setup: creates IAM roles, credential provider, manager, and connector |
| `scripts/` | E2E data plane test — exercises all APIs with role separation |
| `strands-agent/` | Python Strands agent that handles x402 payments autonomously |
| `bazaar-test/` | Deterministic Bazaar MCP test — pays via AgentCore Payments |
| `blockrun-demo/` | AgentCore → Ampersend x402 → BlockRun LLM inference demo |
| `docs/` | Getting started guide and Bazaar integration guide |

## Quick path

```bash
# 1. Set up the payment stack
cd quickstart && cp .env.sample .env  # fill in Coinbase CDP creds
bash setup_roles.sh       # one-time: create IAM roles
bash setup_manager.sh     # create credential provider, manager, connector

# 2. Run the E2E test
cd ../scripts && cp .env.sample .env  # fill in values from quickstart output
bash e2e-test.sh

# 3. Run the agent
cd ../strands-agent && cp .env.sample .env  # fill in session + instrument IDs
pip install -r requirements.txt
python agent.py

# 4. (Optional) Run the BlockRun demo — AgentCore + Ampersend + BlockRun
cd ../blockrun-demo && cp .env.sample .env  # fill in AgentCore + Ampersend values
pip install -r requirements.txt
python seller.py   # Terminal 1: start seller
python buyer.py "What is 2+2?"  # Terminal 2: run buyer
```

## Docs

- [`docs/getting-started.md`](docs/getting-started.md) — Prerequisites, API walkthrough, reference architecture, agent instructions
- [`docs/bazaar-integration.md`](docs/bazaar-integration.md) — Connecting to the Coinbase Bazaar MCP marketplace
- [`blockrun-demo/README.md`](blockrun-demo/README.md) — AgentCore + Ampersend + BlockRun integration demo
- [`docs/PaymentsCPApiSpec.html`](docs/PaymentsCPApiSpec.html) — Control Plane API specification
- [`docs/PaymentsDPApiSpec.html`](docs/PaymentsDPApiSpec.html) — Data Plane API specification
- [`quickstart/README.md`](quickstart/README.md) — Quickstart script details
- [`strands-agent/README.md`](strands-agent/README.md) — Agent architecture, tools, and x402 v1/v2 support
