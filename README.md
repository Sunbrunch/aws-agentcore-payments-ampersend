# AgentCore Payments — Private Preview

Enable AI agents to make x402 cryptocurrency microtransactions using Amazon Bedrock AgentCore Payments.

📖 **[AgentCore Payments Private Beta Guide (PDF)](AgentCore-Payments-Private-Beta-Guide.pdf)** — Full testing guide, API walkthrough, and setup (if distributed with your preview).

🤖 **[`docs/getting-started.md`](docs/getting-started.md)** — Long-form guide for humans and coding assistants.

## Preview access

**Your AWS account must be allowlisted** for AgentCore Payments private preview. If `setup_manager` fails with `UnknownOperationException`, the Payments APIs are not enabled for that account or region — confirm with your AWS contact. See [`docs/getting-started.md`](docs/getting-started.md) (preview section).

## What's in this repo

| Directory | What it does |
|-----------|-------------|
| `quickstart/` | IAM roles, Coinbase credential provider, payment manager, connector |
| `scripts/` | E2E data plane test — exercises APIs with role separation |
| `strands-agent/` | Strands agent that pays x402 endpoints via AgentCore |
| `bazaar-test/` | Deterministic Bazaar MCP + AgentCore test |
| `blockrun-demo/` | AgentCore → Ampersend x402 → BlockRun LLM demo |
| `docs/` | Getting started, Bazaar integration, API HTML specs |

## Quick path

Use a **Python virtualenv** at the repo root (Homebrew Python blocks system `pip` — PEP 668):

```bash
cd agentcore-payments-beta-main
python3 -m venv .venv
source .venv/bin/activate
pip install -r quickstart/requirements.txt
```

**AWS CLI:** use a profile with **IAM admin** for `setup_roles.sh` (e.g. `AdministratorAccess` on the **same account** you want to onboard). If you use IAM Identity Center, configure a profile via `aws configure sso` and run `export AWS_PROFILE=your-profile`.

### 1. Quickstart (payment stack)

```bash
cd quickstart
cp .env.sample .env
# Edit .env: Coinbase CDP (API key id, secret, wallet secret from CDP portal)

bash setup_model.sh    # once: botocore models → ~/.aws/models
bash setup_roles.sh    # once: four IAM roles (needs iam:CreateRole)
bash setup_manager.sh  # credential provider + manager + connector (runs setup_manager.py)
```

Details, SSO trust-policy notes, and optional control-plane URLs: [`quickstart/README.md`](quickstart/README.md).

### 2. E2E test (instrument + session)

```bash
cd ../scripts
cp .env.sample .env
# Fill MANAGER_ARN, CONNECTOR_ID, role ARNs from quickstart output

bash e2e-test.sh
```

### 3. Strands agent

```bash
cd ../strands-agent
cp .env.sample .env
# session + instrument IDs from step 2

pip install -r requirements.txt
python agent.py
```

### 4. BlockRun demo (optional)

```bash
cd ../blockrun-demo
cp .env.sample .env
# AgentCore + Ampersend seller wallet

pip install -r requirements.txt
python seller.py                    # terminal 1
python buyer.py "What is 2+2?"      # terminal 2
```

See [`blockrun-demo/README.md`](blockrun-demo/README.md).

## Docs

- [`docs/getting-started.md`](docs/getting-started.md) — Prerequisites, APIs, architecture, agent patterns
- [`docs/bazaar-integration.md`](docs/bazaar-integration.md) — Coinbase Bazaar MCP + x402
- [`quickstart/README.md`](quickstart/README.md) — Roles, `setup_manager`, venv, troubleshooting
- [`blockrun-demo/README.md`](blockrun-demo/README.md) — AgentCore + Ampersend + BlockRun
- [`docs/PaymentsCPApiSpec.html`](docs/PaymentsCPApiSpec.html) — Control plane API
- [`docs/PaymentsDPApiSpec.html`](docs/PaymentsDPApiSpec.html) — Data plane API
- [`strands-agent/README.md`](strands-agent/README.md) — Agent tools and x402 v1/v2
