# AgentCore Payments → Ampersend → BlockRun (POC)

**Proof-of-concept** that stitches three payment-aware services into a single demo:

- **AWS AgentCore Payments** — signs x402 payments on behalf of an agent under a budget-capped session
- **Ampersend SDK** — x402 merchant/proxy helper; signs outgoing x402 payments with a smart-account session key
- **BlockRun** — x402-gated OpenAI-compatible LLM API

> Scope: a minimal reference for integrators. Everything runs on **Base Sepolia** testnet with USDC. Not production-ready.

---

## Quick Start

Assumes you've run `../quickstart/setup_roles.sh` + `setup_manager.sh` and have an Ampersend seller wallet.

```bash
cd blockrun-demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env       # fill in values (see "Configuration" below)

# Terminal 1
python seller.py          # starts x402 gate on http://localhost:8002

# Terminal 2
python buyer.py "What is the capital of France?"
```

Fund both wallets with Base Sepolia USDC ([Circle faucet](https://faucet.circle.com/)):

- **Buyer wallet** — the `walletAddress` from your AgentCore payment instrument
- **Seller wallet** — your Ampersend smart account

Need the instrument + session? Run `../scripts/e2e-test.sh` (ManagementRole path) and copy `paymentInstrumentId` / `paymentSessionId` into `.env`.

---

## Architecture

```
┌──────────────────┐   1. POST /chat         ┌──────────────────┐   auto x402   ┌──────────────────┐
│ Buyer (buyer.py) │────────────────────────>│ Seller (seller.py)│──────────────>│ BlockRun         │
│                  │   2. HTTP 402 + reqs    │                  │               │ testnet.blockrun │
│ AgentCore Runtime│<────────────────────────│ Starlette + x402 │<──────────────│ .ai/api/v1       │
│  ProcessPayment  │   3. ProcessPayment     │  facilitator     │  LLM response │  (x402-gated)    │
│  (ProcessPayment │      -> USDC sig proof  │  verify          │               │                  │
│   Role + SigV4)  │                         │                  │               │                  │
│                  │   4. POST + PAYMENT-SIG │ Ampersend SDK    │               │                  │
│                  │─────(retry w/ proof)───>│ auto-pays BlockRun│               │                  │
│                  │<──── LLM response ──────│                  │               │                  │
└──────────────────┘                         └──────────────────┘               └──────────────────┘
```

**Two x402 hops, one request:**

1. **Buyer → Seller** — AgentCore signs a USDC `transferWithAuthorization` proof. Seller verifies via the x402 facilitator.
2. **Seller → BlockRun** — Ampersend's httpx transport detects BlockRun's 402, signs, and retries automatically.

**Roles (from AgentCore Payments Private Preview):**

| Role | Who | What |
|------|-----|------|
| **ManagementRole** | app backend (`../scripts/e2e-test.sh`) | creates instrument + session, sets budget |
| **ProcessPaymentRole** | `buyer.py` | can **only** call `ProcessPayment` within the session budget |
| **Seller** | `seller.py` | off-AgentCore x402 merchant (Ampersend) |

---

## Integration Snippets

Copy-paste starting points for integrating each component into your own agent, API, or gateway.

### 1. AgentCore `ProcessPayment` (buyer side)

The agent assumes `ProcessPaymentRole` and calls the **data plane** `bedrock-agentcore` client. It passes the merchant's `accepts[0]` payload through as `cryptoX402`. This is the only AgentCore call the agent is allowed to make.

```python
import boto3, uuid

sts = boto3.Session(profile_name="kevin").client("sts")
creds = sts.assume_role(
    RoleArn=PROCESS_PAYMENT_ROLE_ARN,
    RoleSessionName="agent-pay",
)["Credentials"]

dp = boto3.Session(
    aws_access_key_id=creds["AccessKeyId"],
    aws_secret_access_key=creds["SecretAccessKey"],
    aws_session_token=creds["SessionToken"],
    region_name="us-west-2",
).client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)

resp = dp.process_payment(
    userId=USER_ID,
    paymentManagerArn=MANAGER_ARN,
    paymentSessionId=PAYMENT_SESSION_ID,
    paymentInstrumentId=PAYMENT_INSTRUMENT_ID,
    paymentType="CRYPTO_X402",
    paymentInput={"cryptoX402": {"version": "2", "payload": accepts_payload}},
    clientToken=str(uuid.uuid4()),
)
assert resp["status"] == "PROOF_GENERATED"
proof = resp["paymentOutput"]["cryptoX402"]  # {"version":"2","payload":{...}}
```

See `buyer.py` `agentcore_process_payment()`.

### 2. Build the x402 proof header (buyer side)

After `ProcessPayment`, wrap the proof in the x402 header the merchant expects and retry the original request.

```python
import base64, json, requests

if x402_version >= 2:
    header_name = "PAYMENT-SIGNATURE"
    value = {
        "x402Version": 2,
        "resource": accepts_payload["resource"],
        "accepted": accepts_payload,
        "payload":  proof["payload"],
        "extension": accepts_payload["resource"],
    }
else:
    header_name = "X-PAYMENT"
    value = {
        "x402Version": 1,
        "scheme":  accepts_payload.get("scheme",  "exact"),
        "network": accepts_payload.get("network", "base-sepolia"),
        "payload": proof["payload"],
    }

encoded = base64.b64encode(json.dumps(value).encode()).decode()
resp = requests.post(SELLER_URL, json=body, headers={header_name: encoded})
```

See `buyer.py` `build_payment_header()`.

### 3. x402 seller gate — `402 Payment Required` (seller side)

Return a base64 `PAYMENT-REQUIRED` header (v2) or JSON body (v1). `amount` is in the token's smallest unit (6-decimal USDC → `1000` = $0.001). `amount` **and** `maxAmountRequired` should both be set to satisfy older and newer clients.

```python
from starlette.responses import Response
import base64, json

def return_402(resource: str, pay_to: str):
    reqs = {
        "x402Version": 2,
        "accepts": [{
            "scheme":  "exact",
            "network": "eip155:84532",                 # CAIP-2 for Base Sepolia
            "amount":  "1000",                          # required by facilitator
            "maxAmountRequired": "1000",
            "asset":   "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # USDC
            "payTo":   pay_to,
            "maxTimeoutSeconds": 30,
            "extra":   {"name": "USDC", "version": "2",
                        "assetTransferMethod": "eip3009"},
            "resource": resource,
            "mimeType": "application/json",
            "description": "Pay-per-request LLM",
            "outputSchema": {},
        }],
    }
    enc = base64.b64encode(json.dumps(reqs).encode()).decode()
    return Response(
        content=json.dumps(reqs),
        status_code=402,
        headers={"PAYMENT-REQUIRED": enc, "Content-Type": "application/json"},
    )
```

See `seller.py` `_payment_requirements()` / `_return_402()`.

### 4. x402 facilitator — verify/settle (seller side)

Post the decoded `paymentPayload` plus `paymentRequirements` to the facilitator `/settle` endpoint. **No `/{network}/` in the path** — network info travels inside the JSON.

```python
import httpx

FACILITATOR_URL = "https://www.x402.org/facilitator"   # testnet, Base Sepolia

async def settle(proof: dict, requirements: dict) -> dict:
    async with httpx.AsyncClient(follow_redirects=True) as c:
        resp = await c.post(
            f"{FACILITATOR_URL}/settle",
            json={
                "x402Version": proof.get("x402Version", 2),
                "paymentPayload": proof,
                "paymentRequirements": requirements["accepts"][0],
            },
            timeout=30,
        )
        return resp.json()   # {"success": bool, "transaction": "0x…", …}
```

See `seller.py` `_settle_payment()`.

### 5. Ampersend SDK — seller pays its upstream via x402

The seller only needs an Ampersend smart-account address + session key. Every HTTP call through this client automatically handles 402 → sign → retry.

```python
from ampersend_sdk import create_ampersend_http_client

blockrun = create_ampersend_http_client(
    smart_account_address=SELLER_ADDRESS,
    session_key_private_key=SELLER_SESSION_KEY,
    api_url="https://api.ampersend.ai",
)

resp = await blockrun.post(
    "https://testnet.blockrun.ai/api/v1/chat/completions",
    json={"model": "openai/gpt-oss-20b",
          "messages": [{"role": "user", "content": "hi"}]},
)
```

See `seller.py` `_get_blockrun_client()`.

### 6. BlockRun — the paid upstream

OpenAI-compatible HTTP API, gated with x402. You only interact with it through the Ampersend client above, but the request/response shape is the standard OpenAI format.

```
POST https://testnet.blockrun.ai/api/v1/chat/completions
{
  "model": "openai/gpt-oss-20b",
  "messages": [{"role": "user", "content": "Hello"}]
}
```

---

## Configuration

`.env` values (see `.env.sample`):

| Variable | Used by | Purpose |
|----------|---------|---------|
| `AWS_REGION` / `AWS_PROFILE` | buyer | SSO profile + region |
| `DP_ENDPOINT` | buyer | `https://bedrock-agentcore.<region>.amazonaws.com` |
| `MANAGER_ARN` | buyer | Payment manager (from `setup_manager.sh`) |
| `PROCESS_PAYMENT_ROLE_ARN` | buyer | Role to assume (from `setup_roles.sh`) |
| `PAYMENT_SESSION_ID` | buyer | Pre-provisioned session (from `e2e-test.sh`) |
| `PAYMENT_INSTRUMENT_ID` | buyer | Pre-provisioned instrument/wallet |
| `USER_ID` | buyer | Must match the instrument/session owner |
| `SELLER_SMART_ACCOUNT_ADDRESS` | seller | Ampersend smart account (pay-to) |
| `SELLER_SESSION_KEY` | seller | Ampersend session key (signs outgoing x402) |
| `AMPERSEND_API_URL` | seller | `https://api.ampersend.ai` |
| `NETWORK` | seller | `base-sepolia` or `eip155:84532` |
| `PRICE_MICRO_USDC` | seller | `1000` = $0.001 per request |
| `FACILITATOR_URL` | seller | Defaults to `https://www.x402.org/facilitator` |
| `SKIP_VERIFY` | seller | `true` disables facilitator (local dev only) |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Expecting value: line 1 column 1 (char 0)` from facilitator | Wrong URL — must be `POST {FACILITATOR_URL}/settle` with no `/{network}/` path |
| `Cannot convert undefined to a BigInt` | Add `amount` (not just `maxAmountRequired`) to `accepts[0]` |
| `invalid_exact_evm_insufficient_balance` | Fund the **payer** address (from the error) with Base Sepolia USDC |
| Buyer loops on “Settlement pending” | Facilitator is rejecting — check seller logs for the real `errorReason` |
| 402 with no payment logs | `NETWORK` / CAIP-2 mismatch, or missing `PAYMENT-SIGNATURE` header |

---

## Files

| File | Purpose |
|------|---------|
| `buyer.py` | Deterministic ProcessPayment → retry-with-proof flow |
| `seller.py` | Starlette x402 gate + Ampersend → BlockRun proxy |
| `.env.sample` | Copy to `.env` and fill in |
| `requirements.txt` | Python deps (boto3, httpx, starlette, ampersend-sdk, …) |

For an **LLM-driven** buyer, point `../strands-agent/agent.py` at `SELLER_URL`.

---

## References

- [AgentCore Payments — Private Preview Guide](../docs/getting-started.md)
- [x402 spec — Exact EVM](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)
- [x402 networks / facilitators](https://docs.x402.org/core-concepts/network-and-token-support)
- [Ampersend SDK](https://github.com/edgeandnode/ampersend-sdk)
- [BlockRun API](https://github.com/BlockRunAI/awesome-blockrun)
- [Circle USDC faucet](https://faucet.circle.com/) · [Base Sepolia ETH faucet](https://www.alchemy.com/faucets/base-sepolia)
