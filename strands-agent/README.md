# AgentCore Payments — Strands Agent

A [Strands Agents](https://github.com/strands-agents/sdk-python) AI agent that autonomously handles x402 cryptocurrency payments using Amazon Bedrock AgentCore Payments.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Application Backend (ManagementRole)                │
│  Creates instrument + session with budget            │
│  Passes sessionId + instrumentId to agent            │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌───────────────────────────────────────────────────────┐
│  Strands Agent (ProcessPaymentRole)                   │
│                                                       │
│  Tools:                                               │
│    • http_request          — fetch any URL            │
│    • process_payment       — call ProcessPayment API  │
│    • http_request_with_payment_header — retry w/proof │
│    • connect_to_bazaar     — init MCP session         │
│    • discover_bazaar_tools — search paid tools        │
│    • call_bazaar_tool      — invoke + auto-pay        │
│                                                       │
│  x402 Payment Flow:                                   │
│    1. Agent calls a paid endpoint                     │
│    2. Gets HTTP 402 with x402 payment details         │
│    3. Calls ProcessPayment via AgentCore              │
│    4. Retries with payment proof                      │
│       • v1: X-PAYMENT header                          │
│       • v2: PAYMENT-SIGNATURE header (with resource,  │
│            accepted, extension)                        │
│    5. Gets the paid content                           │
│                                                       │
│  Bazaar Flow:                                         │
│    1. connect_to_bazaar (init MCP session)            │
│    2. discover_bazaar_tools (search marketplace)      │
│    3. call_bazaar_tool (auto-pays via AgentCore)      │
│                                                       │
│  Constraints:                                         │
│    ✗ Cannot create sessions or instruments            │
│    ✗ Cannot override spending limits                  │
│    ✓ Can only spend within the session budget         │
└───────────────────────────────────────────────────────┘
```

## Setup

```bash
pip install -r requirements.txt
cp .env.sample .env
# Fill in values — sessionId and instrumentId come from the app backend
```

### Pre-requisites

The application backend (using ManagementRole) must first:
1. Create a payment instrument (`CreatePaymentInstrument`)
2. Fund the wallet with USDC on Base
3. Create a payment session with a budget (`CreatePaymentSession`)
4. Pass the `sessionId` and `instrumentId` to the agent

You can use `scripts/e2e-test.sh` to do steps 1-3 and grab the IDs from the output.

## Usage

```bash
# Single prompt
python agent.py "Fetch https://some-paid-api.example.com/data and pay if needed"

# Interactive mode
python agent.py
```

### Example: Pay for a generic HTTP endpoint

```bash
python agent.py "Make a POST request to https://nickeljoke.vercel.app/api/joke and pay if needed"
```

The agent will:
1. Make the HTTP request (POST by default for paid endpoints)
2. Detect the 402 response — `http_request` returns `x402_payload` (the merchant's `accepts[0]`)
3. Pass `x402_payload` as-is to `process_payment`
4. Retry with the payment proof (v1: `X-PAYMENT` header, v2: `PAYMENT-SIGNATURE` header with `accepted` field)
5. Return the paid content

### Example: Inspect an external Base mainnet merchant without paying

Agent Intelligence Platform (AIP) exposes a source-backed industrial-project answer at
`https://agent-intelligence-platform.fhochard.workers.dev/v1/signals/premium?country=FR`.
It costs 0.01 USDC on Base mainnet. This agent already accepts arbitrary merchant URLs;
use **GET** for both the unsigned challenge and any authorized retry:

```bash
python agent.py "Use http_request with method GET for https://agent-intelligence-platform.fhochard.workers.dev/v1/signals/premium?country=FR. Show the HTTP 402 payment terms. Do not call process_payment."
```

The command above does not request a signature or payment. To buy the answer, the
agent's owner must independently configure a funded Base mainnet payment instrument
and session, approve the 0.01 USDC purchase, then have the agent process the 402
and retry the **same GET URL**. AIP's [buyer guide](https://agent-intelligence-platform.fhochard.workers.dev/buyer-agent-guide.md)
describes the exact network, token, amount and response contract. Testnet funds
cannot buy from this mainnet merchant.

### Example: Use Bazaar paid MCP tools

```bash
python agent.py "Connect to the Bazaar, find available tools on Base, and call one"
```

The agent will:
1. Call `connect_to_bazaar` to establish an MCP session
2. Call `discover_bazaar_tools` to list available paid tools
3. Call `call_bazaar_tool` which handles the full 402 → ProcessPayment → retry flow automatically

## Tools

| Tool | Description |
|------|-------------|
| `http_request` | Make HTTP requests; detects x402 402 responses and returns `x402_payload` (the merchant's `accepts[0]` object) to pass directly to `process_payment` |
| `process_payment` | Call AgentCore Payments ProcessPayment API — accepts the raw merchant x402 payload as-is (SigV4 signed, uses ProcessPaymentRole) |
| `http_request_with_payment_header` | Retry a request with the x402 payment proof attached (v1: `X-PAYMENT`, v2: `PAYMENT-SIGNATURE` with `resource`, `accepted`, `extension` fields) |
| `connect_to_bazaar` | Initialize an MCP session with the Coinbase Bazaar marketplace |
| `discover_bazaar_tools` | Search for available paid tools on the Bazaar (filterable by network) |
| `call_bazaar_tool` | Call a Bazaar tool by name; automatically handles x402 payment if required |

## x402 Version Support

The agent supports both x402 v1 and v2:

| | v1 | v2 |
|---|---|---|
| Detection | Payment details in 402 response body | `PAYMENT-REQUIRED` header (base64 JSON) |
| Payment header | `X-PAYMENT` | `PAYMENT-SIGNATURE` (base64 JSON with `resource`, `accepted`, `extension` fields) |
| Network format | `base` | `eip155:8453` (CAIP-2) |

The `http_request` tool auto-detects the version and returns `x402_version` + `accepted_requirements` so the agent knows which format to use on retry.

### X-PAYMENT Header Format (v1)

The v1 `X-PAYMENT` header is a base64-encoded JSON object with `x402Version`, `scheme`, and `network` at the top level alongside the `payload` (authorization + signature from ProcessPayment):

```json
{
  "x402Version": 1,
  "scheme": "exact",
  "network": "base",
  "payload": {
    "signature": "0x...",
    "authorization": {
      "from": "0x...",
      "to": "0x...",
      "value": "5000",
      "validAfter": "...",
      "validBefore": "...",
      "nonce": "0x..."
    }
  }
}
```

The `scheme` and `network` values come from the merchant's payment requirement (`accepts[0]`), while `payload` comes from the ProcessPayment response (`paymentOutput.cryptoX402.payload`). The agent constructs this wrapper automatically — the LLM does not need to assemble it.

### GET vs POST for Payment Discovery

Paid endpoints may mirror the HTTP method back in their `outputSchema.input.method` field. The x402 facilitator validates the proof against the payment requirement registered with GET. When the agent POSTs to a paid endpoint and gets a 402, `http_request` automatically re-fetches with GET to obtain the canonical payment requirement for ProcessPayment. The actual paid request is always sent as POST.
