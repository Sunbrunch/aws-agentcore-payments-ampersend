# AgentCore Payments → Ampersend → BlockRun

A tiny, end‑to‑end demo showing how an AI agent **pays for a paid LLM API** using:

- **AWS AgentCore Payments** — signs x402 payments for the agent, inside a budget‑capped session
- **x402** — the open protocol for pay‑per‑request APIs
- **Ampersend SDK** — lets the seller auto‑pay *its* upstream with x402
- **BlockRun** — the paid OpenAI‑compatible LLM endpoint

One command from the agent. Two x402 payments under the hood. All on **Base Sepolia** with testnet USDC.

---

## 1. Quick Start

**Prereqs:** Python 3.10+, AWS CLI logged in to an allow‑listed account, and the quickstart already run:

```bash
# one‑time — creates IAM roles, payment manager, connector
cd quickstart && ./setup_roles.sh && ./setup_manager.sh

# one‑time — creates the buyer's wallet (instrument) + budget (session)
cd ../scripts && ./e2e-test.sh
# copy paymentInstrumentId + paymentSessionId from the output
```

Then run the demo:

```bash
cd blockrun-demo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env     # paste values from the steps above
```

Fund the two wallets on Base Sepolia:

1. **Buyer wallet** — `walletAddress` from `CreatePaymentInstrument` → [Circle USDC faucet](https://faucet.circle.com/)
2. **Seller wallet** — your Ampersend smart account → same faucet

Run it:

```bash
# terminal 1 — seller gate
python seller.py

# terminal 2 — agent
python buyer.py "What is the capital of France?"
```

You should see: `HTTP 402` → `ProcessPayment → PROOF_GENERATED` → `HTTP 200` with the LLM answer.

---

## 2. Architecture

### The picture

```
         ┌────────────────────────┐
         │        Agent           │
         │      (buyer.py)        │        ① POST /chat/completions
         │                        │ ─────────────────────────────────┐
         │ ProcessPaymentRole     │                                  ▼
         │  → bedrock-agentcore   │                      ┌────────────────────────┐
         │    .ProcessPayment     │                      │        Seller          │
         │                        │   ② 402 + x402 reqs  │     (seller.py)        │
         │                        │ ◀─────────────────── │                        │
         │         ③ ProcessPayment       (Base Sepolia) │  • x402 gate           │
         │            ┌──────────────────────────────────│  • facilitator verify  │
         │            ▼                                  │  • Ampersend SDK       │──▶ ┌─────────────┐
         │  ┌─────────────────┐                          │    (pays BlockRun)     │    │  BlockRun   │
         │  │ AgentCore       │                          └────────────────────────┘    │  x402 LLM   │
         │  │ Payments        │                                      ▲                 └─────────────┘
         │  │  (Data Plane)   │     ④ PAYMENT-SIGNATURE header       │
         │  └─────────────────┘ ──────────────────────────────────── ┘
         │                        │   ⑤ 200 + LLM answer
         │                        │ ◀────────────────────────────────
         └────────────────────────┘
```

### The two hops

| Hop | Who pays | Who receives | How it’s signed |
|-----|----------|--------------|------------------|
| **Buyer → Seller** | Agent (AgentCore Payments) | Seller’s Ampersend smart account | `bedrock-agentcore:ProcessPayment` returns an EIP‑3009 `transferWithAuthorization` proof |
| **Seller → BlockRun** | Seller’s Ampersend smart account | BlockRun | Ampersend SDK intercepts 402 and signs automatically |

### Roles (from the AgentCore Payments guide)

| IAM Role | Who uses it | What it can do |
|----------|-------------|----------------|
| **ControlPlaneRole** | `quickstart/setup_manager.py` | One‑time: create credential provider, manager, connector |
| **ManagementRole** | `scripts/e2e-test.sh` (app backend) | Create payment instruments + sessions, set budgets |
| **ProcessPaymentRole** | `buyer.py` (agent) | **Only** `ProcessPayment` — can’t create/modify sessions |
| **ResourceRetrievalRole** | AgentCore service (internal) | Retrieves wallet secrets at runtime |

The role split is the whole point: the agent can **spend** inside a budget, but can never **raise** that budget.

---

## 3. Implementation

Everything the agent does fits on one page. Here are the four moving parts.

### 3.1 Seller returns `402 Payment Required`

x402 says: *"here’s what you owe, signed however you like."* `seller.py` builds this on every unpaid request.

```python
# seller.py
reqs = {
    "x402Version": 2,
    "accepts": [{
        "scheme":  "exact",
        "network": "eip155:84532",                              # Base Sepolia (CAIP‑2)
        "amount":  "1000",                                      # 1000 = $0.001 USDC (6dp)
        "maxAmountRequired": "1000",
        "asset":   "0x036CbD53842c5426634e7929541eC2318f3dCF7e", # USDC
        "payTo":   SELLER_ADDRESS,
        "maxTimeoutSeconds": 30,
        "extra":   {"name": "USDC", "version": "2",
                    "assetTransferMethod": "eip3009"},
        "resource": "http://localhost:8002/v1/chat/completions",
        "mimeType": "application/json",
    }],
}
enc = base64.b64encode(json.dumps(reqs).encode()).decode()
return Response(json.dumps(reqs), status_code=402,
                headers={"PAYMENT-REQUIRED": enc})
```

### 3.2 Agent assumes `ProcessPaymentRole`

The agent **cannot** use its own AWS credentials directly. It assumes a dedicated, tightly‑scoped role. That role only allows `bedrock-agentcore:ProcessPayment`.

```python
# buyer.py
creds = boto3.client("sts").assume_role(
    RoleArn=PROCESS_PAYMENT_ROLE_ARN,
    RoleSessionName="agent-pay",
)["Credentials"]

dp = boto3.Session(
    aws_access_key_id=creds["AccessKeyId"],
    aws_secret_access_key=creds["SecretAccessKey"],
    aws_session_token=creds["SessionToken"],
    region_name="us-west-2",
).client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)
```

### 3.3 Agent calls `ProcessPayment`

The guide is clear: **pass the merchant’s `accepts[0]` through as‑is**. Don’t parse individual fields. AgentCore signs the payment against the session’s budget.

```python
# buyer.py
resp = dp.process_payment(
    userId=USER_ID,
    paymentManagerArn=MANAGER_ARN,
    paymentSessionId=PAYMENT_SESSION_ID,
    paymentInstrumentId=PAYMENT_INSTRUMENT_ID,
    paymentType="CRYPTO_X402",
    paymentInput={"cryptoX402": {"version": "2", "payload": accepts_payload}},
    clientToken=str(uuid.uuid4()),   # idempotency
)
assert resp["status"] == "PROOF_GENERATED"
proof = resp["paymentOutput"]["cryptoX402"]   # signed USDC authorization
```

### 3.4 Agent retries with the proof header

For x402 **v2** the header is `PAYMENT-SIGNATURE`, and `accepted` must deep‑equal the merchant’s original `accepts[0]`.

```python
# buyer.py
value = {
    "x402Version": 2,
    "resource":   accepts_payload["resource"],
    "accepted":   accepts_payload,    # MUST deep-equal the 402 response
    "payload":    proof["payload"],
    "extension":  accepts_payload["resource"],
}
header = base64.b64encode(json.dumps(value).encode()).decode()

resp = requests.post(SELLER_URL, json=body,
                     headers={"PAYMENT-SIGNATURE": header})
# → HTTP 200 + LLM answer
```

*(For x402 v1, the header is `X-PAYMENT` with `{x402Version, scheme, network, payload}`.)*

### 3.5 Seller verifies + settles, then pays BlockRun via Ampersend

On the way back down, the seller does two things:

**(a) Verify the buyer’s proof with the x402 facilitator:**

```python
# seller.py
async with httpx.AsyncClient(follow_redirects=True) as c:
    r = await c.post(
        "https://www.x402.org/facilitator/settle",
        json={
            "x402Version": 2,
            "paymentPayload":      proof,               # from buyer
            "paymentRequirements": accepts_payload,     # what the seller asked for
        },
        timeout=30,
    )
    settled = r.json()   # {"success": True, "transaction": "0x…"}
```

**(b) Proxy the LLM call through Ampersend, which auto‑pays BlockRun:**

```python
# seller.py
from ampersend_sdk import create_ampersend_http_client

blockrun = create_ampersend_http_client(
    smart_account_address=SELLER_ADDRESS,
    session_key_private_key=SELLER_SESSION_KEY,
    api_url="https://api.ampersend.ai",
)

# Ampersend intercepts BlockRun's 402, signs, and retries automatically
r = await blockrun.post(
    "https://testnet.blockrun.ai/api/v1/chat/completions",
    json={"model": "openai/gpt-oss-20b",
          "messages": [{"role": "user", "content": prompt}]},
)
return r.json()   # OpenAI-shaped response back to the agent
```

That’s the whole demo: **one agent request, two x402 payments, one LLM answer.**

---

## 4. Configuration

`.env` — copy from `.env.sample`.

| Variable | Used by | Comes from |
|----------|---------|-----------|
| `AWS_REGION`, `AWS_PROFILE` | buyer | your SSO setup |
| `DP_ENDPOINT` | buyer | `https://bedrock-agentcore.<region>.amazonaws.com` |
| `MANAGER_ARN` | buyer | `quickstart/setup_manager.py` output |
| `PROCESS_PAYMENT_ROLE_ARN` | buyer | `quickstart/setup_roles.sh` output |
| `PAYMENT_SESSION_ID`, `PAYMENT_INSTRUMENT_ID`, `USER_ID` | buyer | `scripts/e2e-test.sh` output |
| `SELLER_SMART_ACCOUNT_ADDRESS`, `SELLER_SESSION_KEY` | seller | Ampersend dashboard |
| `NETWORK` | seller | `base-sepolia` |
| `PRICE_MICRO_USDC` | seller | e.g. `1000` = $0.001 per request |
| `FACILITATOR_URL` | seller | `https://www.x402.org/facilitator` |

---

## 5. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Expecting value: line 1 column 1 (char 0)` from facilitator | Use `POST {FACILITATOR_URL}/settle` — **no** `/{network}/` in the path |
| `Cannot convert undefined to a BigInt` | Include both `amount` and `maxAmountRequired` in `accepts[0]` |
| `invalid_exact_evm_insufficient_balance` | Fund the `payer` address (shown in the error) with Base Sepolia USDC |
| Buyer loops on *“Settlement pending”* | Facilitator is rejecting — check seller logs for the real `errorReason` |
| `AccessDenied` on `ProcessPayment` | You didn’t assume `ProcessPaymentRole` first |

---

## 6. Files

| File | What it is |
|------|------------|
| `buyer.py` | Deterministic agent: 402 → `ProcessPayment` → retry with proof |
| `seller.py` | Starlette x402 gate + Ampersend → BlockRun proxy |
| `.env.sample` | Copy to `.env`, fill in |
| `requirements.txt` | `boto3`, `httpx`, `starlette`, `ampersend-sdk`, … |

For an **LLM‑driven** agent (instead of the deterministic one here), see `../strands-agent/`.

---

## References

- [AgentCore Payments — Private Preview Guide](../docs/getting-started.md)
- [x402 Exact EVM spec](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)
- [Ampersend SDK](https://github.com/edgeandnode/ampersend-sdk)
- [BlockRun API](https://github.com/BlockRunAI/awesome-blockrun)
- [Circle USDC faucet](https://faucet.circle.com/) · [Base Sepolia ETH faucet](https://www.alchemy.com/faucets/base-sepolia)
