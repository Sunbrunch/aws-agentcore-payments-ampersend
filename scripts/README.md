# E2E Test

Exercises all AgentCore Payments APIs end-to-end with proper IAM role separation.

## What it tests

| Phase | Role | APIs |
|-------|------|------|
| A — Control Plane | ControlPlaneRole | ListPaymentManagers, GetPaymentManager, ListPaymentConnectors, GetPaymentConnector |
| B — App Backend | ManagementRole | CreatePaymentInstrument, GetPaymentInstrument, ListPaymentInstruments, CreatePaymentSession |
| C — Agent | ProcessPaymentRole | ProcessPayment (x402) |
| D — Verification | ManagementRole | GetPaymentSession, ListPaymentSessions |

## Usage

```bash
cp .env.sample .env   # fill in values from quickstart output
bash e2e-test.sh
```

The script **sources** `.env` with bash. Every value must be safe for that: **do not** use unquoted `VAR=<text>` — the `<` starts input redirection. Use **single quotes**, e.g. `MANAGER_ARN='arn:aws:bedrock-agentcore:...'` (see `.env.sample`).

## Prerequisites

- AWS CLI v2 with service models installed (`bash quickstart/setup_model.sh`)
- `jq` — `brew install jq`
- Completed quickstart setup (manager + connector created)
- Wallet funded with USDC on Base
