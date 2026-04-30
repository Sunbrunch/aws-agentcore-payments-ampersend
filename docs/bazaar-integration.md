# Bazaar Integration Guide

Connect an AI agent to the [Coinbase Bazaar](https://api.cdp.coinbase.com/platform/v2/x402/discovery/mcp) — a marketplace of paid tools accessible over the Model Context Protocol (MCP) — with payments routed through AgentCore Payments.

This guide targets **Base mainnet** (`eip155:8453`). See the `base-sepolia` branch for testnet configuration.

---

## How It Works

The Bazaar exposes paid tools via MCP (JSON-RPC). When you call a paid tool:

1. The Bazaar returns an x402 payment requirement (either HTTP 402 or wrapped in a JSON-RPC response)
2. Your code extracts the `accepts[0]` payload and passes it to AgentCore Payments `ProcessPayment`
3. ProcessPayment signs the transaction using the user's wallet (managed by AgentCore)
4. You retry the tool call with the payment proof in the `X-PAYMENT` header
5. The Bazaar validates the proof and returns the tool result

All payments go through AgentCore Payments, which means:
- IAM role separation (ProcessPaymentRole can only spend, not create sessions)
- Session budgets enforce spending limits
- Centralized wallet management via AgentCore

---

## Prerequisites

1. Complete the quickstart (`quickstart/setup_roles.sh` + `quickstart/setup_manager.sh`) to create your payment stack
2. Create a payment instrument and fund it with USDC on Base
3. Create a payment session with a budget
4. Service models installed (`bash quickstart/setup_model.sh`)

You can use `scripts/e2e-test.sh` to do steps 2-3 and grab the IDs from the output.

---

## Deterministic Test (bazaar-test/)

The `bazaar-test/` directory contains a standalone Python script that exercises the full Bazaar → AgentCore Payments flow deterministically:

```bash
cd bazaar-test
pip install -r requirements.txt
cp .env.sample .env    # fill in values from quickstart + e2e-test output
python x402_agentcore_test.py
```

This uses the official [Coinbase x402 Python client](https://pypi.org/project/x402/) with an `AgentCoreClientScheme` adapter, so the x402 client handles 402 detection and retry automatically while AgentCore handles signing.

---

## Agent Integration (strands-agent/)

The Strands agent in `strands-agent/` includes three Bazaar-specific tools:

| Tool | Description |
|------|-------------|
| `connect_to_bazaar` | Initialize an MCP session with the Bazaar |
| `discover_bazaar_tools` | Search for paid tools (filterable by network) |
| `call_bazaar_tool` | Call a tool by name — handles 402 → ProcessPayment → retry automatically |

```bash
cd strands-agent
pip install -r requirements.txt
cp .env.sample .env
python agent.py "Connect to the Bazaar, find tools on Base, and call one"
```

---

## Key Implementation Details

### x402 Payload Handling

The merchant's `accepts[0]` payload is passed as-is to ProcessPayment for v1. For v2, non-payment metadata fields are stripped. The `amount` field (used by some merchants) is normalized to `maxAmountRequired` (used by others):

```python
payload = dict(x402_payload)

# v2 only: strip non-payment metadata (v1 keeps the full merchant payload)
if x402_version >= 2:
    for key in ["description", "mimeType", "resource", "outputSchema"]:
        payload.pop(key, None)
```

For v1, the full merchant payload (including `resource`, `description`, `outputSchema`, etc.) is passed through to ProcessPayment. All fields including `version` and `extra` are required in the ProcessPayment request.

### Bazaar Proxy 402 Handling

The Bazaar proxy may return the 402 payment requirement in two ways:
- Direct HTTP 402 response
- HTTP 200 JSON-RPC response with `isError: true` and payment details in `structuredContent`

Both cases need to be handled. See `bazaar-test/x402_agentcore_test.py` for the implementation.

### Payment Proof Header

After ProcessPayment returns `PROOF_GENERATED`, the payment proof must be wrapped with x402 protocol fields before sending:

For v1, the `X-PAYMENT` header is a base64-encoded JSON with `x402Version`, `scheme`, and `network` at the top level (from the merchant's `accepts[0]`) alongside the `payload` (from `paymentOutput.cryptoX402.payload`):

```python
x_payment = {
    "x402Version": 1,
    "scheme": x402_payload["scheme"],       # from merchant's accepts[0]
    "network": x402_payload["network"],     # from merchant's accepts[0]
    "payload": crypto_output["payload"],    # from ProcessPayment response
}
encoded = base64.b64encode(json.dumps(x_payment).encode()).decode()
headers["X-PAYMENT"] = encoded
```

For v2, the `PAYMENT-SIGNATURE` header is a base64-encoded JSON with `x402Version`, `resource`, `accepted` (the full merchant payment requirements), `payload` (from ProcessPayment response), and `extension`:

```python
payment_signature = {
    "x402Version": 2,
    "resource": x402_payload.get("resource", ""),   # merchant resource URL
    "accepted": x402_payload,                       # full merchant accepts[0]
    "payload": crypto_output["payload"],            # from ProcessPayment response
    "extension": x402_payload.get("resource", ""),  # same as resource
}
encoded = base64.b64encode(json.dumps(payment_signature).encode()).decode()
headers["PAYMENT-SIGNATURE"] = encoded
```

> **Note:** `x402Version` must be a number (`1` or `2`), not a string. The Coinbase facilitator performs strict schema validation and rejects string values.

- For direct HTTP endpoints: sent as the `X-PAYMENT` header (v1) or `PAYMENT-SIGNATURE` header (v2)
- For Bazaar MCP tools: passed via `parameters.headers` in the `proxy_tool_call` arguments (the Bazaar proxy forwards these to the underlying merchant)

> **Important:** Do not base64-encode the raw ProcessPayment output directly. The facilitator expects `x402Version`, `scheme`, and `network` at the top level of the decoded header payload.

### Wallet Funding

The wallet managed by AgentCore Payments must have USDC on the target network (Base mainnet). The facilitator verifies on-chain balance before accepting the proof. You can find the wallet address in the payment instrument details.
