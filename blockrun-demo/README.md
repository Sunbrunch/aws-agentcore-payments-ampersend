# AgentCore Payments → Ampersend → BlockRun Demo

End-to-end demo showing AWS AgentCore Payments as the buyer-side payment backend, Ampersend SDK as the seller-side x402 gateway, and BlockRun as the upstream LLM inference provider.

## Alignment with AgentCore Payments (Private Preview)

This demo matches the **reference architecture** in the AgentCore Payments Private Preview guide:

| Guide concept | In this demo |
|---------------|----------------|
| **ProcessPaymentRole** — only `bedrock-agentcore:ProcessPayment` | `buyer.py` calls `sts.assume_role(ProcessPaymentRole)` then uses the **data plane** client `bedrock-agentcore` with `DP_ENDPOINT` (`https://bedrock-agentcore.<region>.amazonaws.com`). |
| **ManagementRole** creates instruments & sessions; agent does **not** | `PAYMENT_INSTRUMENT_ID` and `PAYMENT_SESSION_ID` come from your environment — provision them with **ManagementRole** (`../scripts/e2e-test.sh` or your app), not from `buyer.py`. |
| **ProcessPayment** with `CRYPTO_X402` and merchant payload as-is | `buyer.py` passes `accepts[0]` from the 402 response into `process_payment` (stripping v2 metadata only, same as Strands). |
| **clientToken** on ProcessPayment | `buyer.py` sends `clientToken=str(uuid.uuid4())` on each call (idempotency). |
| **Retry with proof** (v1 `X-PAYMENT`, v2 `PAYMENT-SIGNATURE`) | Same header construction as `strands-agent/agent.py`. |
| **Spending guardrails** | Enforced by the **payment session** budget you created; the agent cannot raise limits. |

**Seller (`seller.py`)** is *not* part of AgentCore Payments — it is a merchant-style x402 gateway (Ampersend + BlockRun) so you can show **buyer → merchant → upstream LLM** in one stack.

For the **full Strands** experience (LLM chooses when to pay), use `../strands-agent/agent.py` and aim its HTTP tools at `SELLER_URL`.

See also: `../docs/getting-started.md`, `../strands-agent/README.md`, and the Private Preview PDF.

## Architecture

```
┌─────────────────────────────────────┐
│  Buyer  (buyer.py)                  │
│                                     │
│  AgentCore ProcessPayment API       │
│  (SigV4 → ProcessPaymentRole)      │
│                                     │
│  1. POST /v1/chat/completions       │
│  2. Gets HTTP 402 + x402 payload    │
│  3. Calls ProcessPayment → proof    │
│  4. Retries with proof header       │
└──────────────┬──────────────────────┘
               │  x402 payment proof
               ▼
┌─────────────────────────────────────┐
│  Seller  (seller.py)                │
│                                     │
│  Ampersend SDK (x402 transport)     │
│  Starlette HTTP server              │
│                                     │
│  • Gates /v1/chat/completions       │
│  • Verifies payment via facilitator │
│  • Proxies to BlockRun              │
│    (ampersend httpx client          │
│     auto-pays BlockRun via x402)    │
└──────────────┬──────────────────────┘
               │  x402 payment (auto)
               ▼
┌─────────────────────────────────────┐
│  BlockRun  (testnet.blockrun.ai)    │
│                                     │
│  LLM inference API                  │
│  OpenAI-compatible                  │
│  x402-gated                         │
│  Models: gpt-oss-20b, gpt-oss-120b │
└─────────────────────────────────────┘
```

**Two x402 payment hops:**
1. **Buyer → Seller** — AgentCore ProcessPayment signs a USDC transfer authorization. The seller verifies via the x402 facilitator.
2. **Seller → BlockRun** — ampersend-sdk's httpx transport handles BlockRun's x402 gate transparently (auto-detects 402, signs, retries).

## Prerequisites

1. **AgentCore Payments** set up via `../quickstart/`:
   - IAM roles created (`setup_roles.sh`)
   - Payment manager + connector provisioned (`setup_manager.sh`)
   - Payment instrument + session created (via `../scripts/e2e-test.sh` or API)
   - Instrument funded with testnet USDC ([Circle faucet](https://faucet.circle.com/))
   - Botocore service models installed (`setup_model.sh`)

2. **Ampersend smart account** for the seller:
   - Create at [ampersend.ai](https://ampersend.ai) or via the SDK
   - Fund with Base Sepolia USDC ([Circle faucet](https://faucet.circle.com/))
   - Get testnet ETH for gas ([Alchemy faucet](https://www.alchemy.com/faucets/base-sepolia))

3. **Python 3.11+** with pip or uv

## Setup

```bash
cd blockrun-demo

# Install dependencies
pip install -r requirements.txt
# or: uv pip install -r requirements.txt

# Configure
cp .env.sample .env
# Fill in AgentCore values (from quickstart output)
# Fill in Ampersend values (seller wallet + session key)
```

## Demo

### Terminal 1 — Start the seller

```bash
python seller.py
```

Expected output:
```
============================================================
  Ampersend x402 Seller -> BlockRun LLM
============================================================
  Wallet  : 0x...
  Network : base-sepolia
  Model   : openai/gpt-oss-20b
  BlockRun: https://testnet.blockrun.ai/api/v1
  Price   : $0.0020 USDC
  Port    : 8002

  Endpoint: http://localhost:8002/v1/chat/completions
  Health : http://localhost:8002/health
============================================================
```

### Terminal 2 — Run the buyer

```bash
# One-shot
python buyer.py "What is the capital of France?"

# Interactive
python buyer.py
```

Expected output:
```
============================================================
  AgentCore Payments -> Ampersend x402 -> BlockRun LLM
============================================================
  Seller : http://localhost:8002/v1/chat/completions
  Model  : openai/gpt-oss-20b
  Prompt : What is the capital of France?
============================================================

[0] Assuming AgentCore ProcessPaymentRole...
    Role: AgentCorePaymentsProcessPaymentRole

[1] POST http://localhost:8002/v1/chat/completions
    HTTP 402
    Payment required: $0.0020 USDC on base-sepolia
    Pay to: 0x312554...431E44

[2] AgentCore ProcessPayment (x402 v2)...
    Manager : my-payment-manager
    Session : ps-abc123...
    ProcessPayment -> PROOF_GENERATED
    Payment proof generated

[3] Retrying with PAYMENT-SIGNATURE header...
    Attempt 1/6...
    HTTP 200

────────────────────────────────────────────────────────────
  Model  : gpt-oss-20b
  Tokens : 42
────────────────────────────────────────────────────────────
  The capital of France is Paris.
────────────────────────────────────────────────────────────

Done: AgentCore -> Ampersend -> BlockRun end-to-end
```

### Using the Strands Agent as buyer

You can also point the existing Strands agent (`../strands-agent/agent.py`) at the seller:

```bash
cd ../strands-agent
python agent.py "Make a POST request to http://localhost:8002/v1/chat/completions with body {\"model\": \"openai/gpt-oss-20b\", \"messages\": [{\"role\": \"user\", \"content\": \"What is 2+2?\"}]} and pay if needed"
```

The Strands agent will autonomously detect the 402, pay via AgentCore ProcessPayment, and retry — showing autonomous agent payment in action.

## Configuration

### Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `AWS_REGION` | buyer | AWS region (default: `us-west-2`) |
| `DP_ENDPOINT` | buyer | AgentCore data plane endpoint |
| `MANAGER_ARN` | buyer | Payment manager ARN |
| `PROCESS_PAYMENT_ROLE_ARN` | buyer | IAM role for ProcessPayment |
| `PAYMENT_SESSION_ID` | buyer | Pre-provisioned payment session |
| `PAYMENT_INSTRUMENT_ID` | buyer | Pre-provisioned payment instrument |
| `SELLER_SMART_ACCOUNT_ADDRESS` | seller | Ampersend smart account address |
| `SELLER_SESSION_KEY` | seller | Ampersend session key for signing |
| `NETWORK` | seller | `base-sepolia` (testnet) or `base` (mainnet) |
| `BLOCKRUN_MODEL` | both | LiteLLM model string (default: `openai/gpt-oss-20b`) |
| `SELLER_URL` | buyer | Seller endpoint URL |
| `SELLER_PORT` | seller | HTTP port (default: `8002`) |
| `PRICE_MICRO_USDC` | seller | Price per request in micro-USDC (default: `2000` = $0.002) |
| `SKIP_VERIFY` | seller | Set `true` to skip facilitator verification |

### BlockRun Testnet

| Setting | Value |
|---------|-------|
| API | `https://testnet.blockrun.ai/api/v1` |
| Network | Base Sepolia (Chain 84532) |
| Models | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` |

### Funding

Both the AgentCore instrument and Ampersend seller wallet need testnet USDC:
- **USDC**: [Circle Faucet](https://faucet.circle.com/) (select Base Sepolia)
- **ETH** (gas): [Alchemy Faucet](https://www.alchemy.com/faucets/base-sepolia)

## How It Works

### Payment Flow Detail

```
Buyer                        Seller                  Facilitator         BlockRun
  │                            │                         │                  │
  │─── POST /chat ────────────>│                         │                  │
  │<── 402 + PAYMENT-REQUIRED ─│                         │                  │
  │                            │                         │                  │
  │─── AgentCore ──> ProcessPayment API (AWS)            │                  │
  │<── proof ──────────────────│                         │                  │
  │                            │                         │                  │
  │─── POST + PAYMENT-SIG ────>│                         │                  │
  │                            │─── settle + proof ─────>│                  │
  │                            │<── tx hash ─────────────│                  │
  │                            │                         │                  │
  │                            │─── POST /chat (x402 auto-pay) ──────────>│
  │                            │<── LLM response ─────────────────────────│
  │<── LLM response ──────────│                         │                  │
```

### What each component does

- **AgentCore Payments** (buyer): Signs x402 USDC transfer authorizations within a budget-capped session. The agent can only spend what the app backend allocated — no escalation possible.

- **Ampersend SDK** (seller): Provides the `create_ampersend_http_client` which wraps httpx with automatic x402 payment handling. When the seller proxies to BlockRun and gets a 402, the SDK signs and retries transparently.

- **BlockRun** (upstream): x402-gated LLM inference API. OpenAI-compatible. Accepts USDC payments on Base (Sepolia for testnet).

## Resources

- [AgentCore Payments Guide](../docs/getting-started.md)
- [Ampersend SDK](https://github.com/edgeandnode/ampersend-sdk)
- [BlockRun](https://blockrun.ai) / [API Docs](https://github.com/BlockRunAI/awesome-blockrun)
- [x402 Protocol](https://www.x402.org/)
- [Ampersend × BlockRun Demo](https://github.com/edgeandnode/ampersend-blockrun-agentops-demo)
