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

## Prerequisites

- AWS CLI v2 with service models installed (`bash quickstart/setup_model.sh`)
- `jq` — `brew install jq`
- Completed quickstart setup (manager + connector created)
- Wallet funded with testnet USDC (https://faucet.circle.com/)
