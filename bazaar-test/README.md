# Bazaar MCP Marketplace Test

Connects to the [Coinbase Bazaar](https://docs.cdp.coinbase.com/bazaar/docs/welcome) — a marketplace of paid tools accessible over MCP — and calls a paid tool with payment routed through AgentCore Payments.

All payments go through the ProcessPayment API (boto3 SDK), which means:
- IAM role separation (ProcessPaymentRole)
- Session budgets and spending guardrails
- Centralized wallet management via AgentCore

## Prerequisites

- Service models installed (`bash quickstart/setup_model.sh`)
- Quickstart completed (manager + connector created)
- Payment instrument created and funded with testnet USDC ([faucet.circle.com](https://faucet.circle.com/))
- Payment session created with a budget

You can use `scripts/e2e-test.sh` to create the instrument + session and grab the IDs.

## Usage

```bash
pip install -r requirements.txt
cp .env.sample .env    # fill in values from quickstart + e2e-test output
python x402_agentcore_test.py
```

## What it does

Validates that the official [Coinbase x402 Python client](https://pypi.org/project/x402/) can use AgentCore Payments as the signing backend instead of a local private key.

It plugs an `AgentCoreClientScheme` (which calls ProcessPayment) into `x402MCPClientSync`, so the x402 client handles 402 detection and retry automatically while AgentCore handles signing.

1. Assumes ProcessPaymentRole via STS
2. Creates an AgentCore DP client pointed at the testing endpoint
3. Registers an `AgentCoreClientScheme` with the x402 client for Base Sepolia and Base Mainnet (v1 + v2)
4. Connects to the Bazaar MCP endpoint (JSON-RPC)
5. Discovers available paid tools on Base Sepolia
6. Picks a known-good tool (nickeljoke) or the cheapest available
7. Calls the tool via `x402MCPClientSync` — the x402 client intercepts the 402, calls AgentCore ProcessPayment, and retries with the payment proof automatically
8. Verifies the payment was made and content was returned

## How it relates to strands-agent/

The strands agent is an LLM-driven agent that decides when to pay. This test is a deterministic script that exercises the same payment flow — useful for validating the Bazaar and x402 integration end-to-end.
