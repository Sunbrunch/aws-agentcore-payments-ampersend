# AgentCore Payments → Ampersend → BlockRun  (v2 — tiered)

A tiny, end‑to‑end demo showing how an AI agent **pays per‑intelligence** for a paid LLM API using:

- **AWS AgentCore Payments** — signs x402 payments for the agent, inside a budget‑capped session
- **x402** — the open protocol for pay‑per‑request APIs
- **Ampersend SDK** — lets the seller auto‑pay *its* upstream with x402
- **BlockRun** — the paid OpenAI‑compatible LLM endpoint

One command from the agent. Two x402 payments under the hood. **Three model tiers.** All on **Base** with USDC.

---

## What's new in v2

This branch lands the feedback from AWS:

- **Multi‑model routing.** The seller publishes a `/v1/models` catalog with three price tiers (*fast / balanced / premium*), each mapped to a different BlockRun model. The buyer fetches the catalog and routes the prompt to the cheapest tier that can handle it — the "dynamically route to optimal AI model" narrative.
- **On‑brand prompts.** `--example eip | proposal | audit | subgraph` runs real‑world prompts: summarizing EIP‑4844, reviewing an L2 governance proposal, auditing a Solidity vault, explaining a subgraph schema.
- **Post‑payment budget readout.** After a successful payment, the buyer briefly assumes `ManagementRole` and calls `GetPaymentSession` to print the session's **budget → spent → remaining**. The guardrails story you can see ticking down in real time.
- **Loud `SKIP_VERIFY` warning.** When `SKIP_VERIFY=true` is set, the seller prints a large banner at startup — no more accidental unverified demos.
- **BlockRun debug logging.** Structured request/response/error output for the seller→BlockRun leg, including timing, full headers, and tracebacks — useful for reporting upstream issues.

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

Fund the two wallets on Base:

1. **Buyer wallet** — `walletAddress` from `CreatePaymentInstrument` — fund with USDC on Base
2. **Seller wallet** — your Ampersend smart account — fund with USDC on Base

Run it:

```bash
# terminal 1 — seller gate
python seller.py

# terminal 2 — agent (pick any of these)
python buyer.py --example eip                      # fast tier
python buyer.py --example proposal                 # balanced tier
python buyer.py --example audit                    # premium tier
python buyer.py "Summarize EIP-4844 in 3 bullets." # auto‑routed
python buyer.py --tier premium "deep analysis..."  # force a tier
```

You should see: `/v1/models` catalog → `HTTP 402` (with the matching tier price) → `ProcessPayment → PROOF_GENERATED` → `HTTP 200` with the LLM answer → `GetPaymentSession` showing the session budget decrease.

---

## 2. Architecture

### The picture

```
         ┌────────────────────────┐
         │        Agent           │
         │      (buyer.py)        │   ① GET /v1/models (catalog, free)
         │                        │ ───────────────────────────────────┐
         │ pick tier (fast/       │                                    ▼
         │   balanced/premium)    │                        ┌────────────────────────┐
         │                        │   ② POST /chat (+tier) │        Seller          │
         │ ProcessPaymentRole     │ ─────────────────────▶ │     (seller.py)        │
         │  → bedrock-agentcore   │                        │                        │
         │    .ProcessPayment     │   ③ 402 + x402 reqs    │  • tiered x402 gate    │
         │                        │ ◀───────────────────── │  • facilitator verify  │
         │        ④ ProcessPayment              (Base)     │  • Ampersend SDK       │──▶ ┌─────────────┐
         │           ┌─────────────────────────────────────│    (pays BlockRun)     │    │  BlockRun   │
         │           ▼                                     └────────────────────────┘    │  x402 LLM   │
         │  ┌─────────────────┐                                       ▲                  └─────────────┘
         │  │ AgentCore       │                                       │
         │  │ Payments (DP)   │   ⑤ PAYMENT-SIGNATURE header          │
         │  └─────────────────┘ ─────────────────────────────────────┘
         │                        │   ⑥ 200 + LLM answer
         │                        │ ◀─────────────────────────────────
         │                        │
         │ ManagementRole (ro)    │   ⑦ GetPaymentSession
         │  → bedrock-agentcore   │                                    (AgentCore CP/DP)
         │    .GetPaymentSession  │     shows budget → spent → remaining
         └────────────────────────┘
```

### The two hops

| Hop | Who pays | Who receives | How it's signed |
|-----|----------|--------------|------------------|
| **Buyer → Seller** | Agent (AgentCore Payments) | Seller's Ampersend smart account | `bedrock-agentcore:ProcessPayment` returns an EIP‑3009 `transferWithAuthorization` proof |
| **Seller → BlockRun** | Seller's Ampersend smart account | BlockRun | Ampersend SDK intercepts 402 and signs automatically |

### Roles (from the AgentCore Payments guide)

| IAM Role | Who uses it | What it can do |
|----------|-------------|----------------|
| **ControlPlaneRole** | `quickstart/setup_manager.py` | One‑time: create credential provider, manager, connector |
| **ManagementRole** | `scripts/e2e-test.sh`; **v2 buyer for `GetPaymentSession` only** | Create instruments + sessions, read sessions, set budgets |
| **ProcessPaymentRole** | `buyer.py` (agent) | **Only** `ProcessPayment` — can't create/modify sessions or read them |
| **ResourceRetrievalRole** | AgentCore service (internal) | Retrieves wallet secrets at runtime |

The role split is the whole point: the agent can **spend** inside a budget, but can never **raise** that budget or even read it — only the application backend (ManagementRole) can.

---

## 3. Multi‑model routing (v2)

The seller publishes a catalog. The buyer picks a tier. The seller prices the 402 accordingly.

### 3.1 Seller — `/v1/models` catalog

Defaults (override via `MODEL_CATALOG` JSON or the legacy `PRICE_MICRO_USDC` / `BLOCKRUN_MODEL` env vars for the fast tier):

| Tier | Model | Price | Good for |
|------|-------|-------|----------|
| **fast** | `openai/gpt-oss-20b` | **$0.002** | short answers, classification, cheap summaries |
| **balanced** | `openai/gpt-oss-120b` | **$0.003** | multi‑paragraph analysis, governance summaries |
| **premium** | `deepseek/deepseek-v3` | **$0.008** | smart‑contract audits, deep technical review |

```bash
curl -s http://localhost:8002/v1/models | jq
# {
#   "object": "list",
#   "data": [
#     { "id": "openai/gpt-oss-20b",   "tier": "fast",     "x402": {"price_usdc": 0.002, ...} },
#     { "id": "openai/gpt-oss-120b",  "tier": "balanced", "x402": {"price_usdc": 0.003, ...} },
#     { "id": "deepseek/deepseek-v3", "tier": "premium",  "x402": {"price_usdc": 0.008, ...} }
#   ]
# }
```

### 3.2 Buyer — pick the cheapest viable tier

A transparent, auditable heuristic so viewers can see *why* each prompt lands on each tier. Production routers would use a classifier or a cheap LLM call.

```python
def pick_tier(prompt, catalog, override=None):
    p = prompt.lower()
    length = len(prompt)

    PREMIUM  = ("audit", "vulnerability", "reentran", "solidity", "pragma ", ...)
    BALANCED = ("summarize", "explain", "analy", "proposal", "governance", ...)

    if any(k in p for k in PREMIUM) or length > 2000:
        return by_id["premium"]
    if any(k in p for k in BALANCED) or length > 400:
        return by_id["balanced"]
    return by_id["fast"]
```

### 3.3 Preset prompts — the "pay‑per‑intelligence" story

| `--example` | Tier | What it does |
|-------------|------|--------------|
| `eip` | fast | Summarize EIP‑4844 in 3 bullets |
| `proposal` | balanced | Summarize an L2 governance proposal for a busy voter |
| `subgraph` | balanced | Explain a subgraph schema to a new Graph developer |
| `audit` | premium | Full Solidity vault audit (reentrancy, access control, CEI) |

```bash
python buyer.py --example audit
#   tier=premium  price=$0.008
#   ProcessPayment -> PROOF_GENERATED
#   HTTP 200 → "High severity: classic reentrancy in `withdraw`…"
#   [after] budget   : 1.0000  USD
#   [after] spent    : 0.008   USD
#   [after] remaining: 0.992   USD
```

---

## 4. Implementation

### 4.1 Seller returns tiered `402 Payment Required`

The seller inspects the request body's `model` field, resolves it to a catalog tier, and returns 402 with that tier's price.

```python
# seller.py — _payment_requirements()
tier = _resolve_tier(body.get("model"))          # fast | balanced | premium
reqs = {
    "x402Version": 2,
    "accepts": [{
        "scheme":  "exact",
        "network": "eip155:8453",
        "amount":  str(tier["price_micro_usdc"]),      # e.g. "8000" = $0.008
        "maxAmountRequired": str(tier["price_micro_usdc"]),
        "asset":   "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "payTo":   SELLER_ADDRESS,
        "maxTimeoutSeconds": 30,
        "extra":   {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"},
        "resource": "/v1/chat/completions",
        "description": "BlockRun LLM inference via Ampersend",
        "mimeType": "application/json",
        "outputSchema": {},
    }],
}
# Returned as HTTP 402 with PAYMENT-REQUIRED header (base64-encoded JSON)
# and X-Tier / X-Model response headers.
```

### 4.2 Agent assumes `ProcessPaymentRole` and calls `ProcessPayment`

Same as v1 — pass `accepts[0]` through as‑is.

```python
# buyer.py
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
proof = resp["paymentOutput"]["cryptoX402"]
```

### 4.3 Post‑payment budget readout (v2)

After the LLM response comes back, the buyer briefly assumes **ManagementRole** (a *different* role than the agent uses) and calls `GetPaymentSession`. This is exactly the role separation from the guide: the agent **spends**, the backend **reads**.

```python
# buyer.py
mgmt = _assume_role(MANAGEMENT_ROLE_ARN, "buyer-mgmt") \
         .client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)
resp = mgmt.get_payment_session(
    paymentManagerArn=MANAGER_ARN,
    paymentSessionId=PAYMENT_SESSION_ID,
    userId=USER_ID,
)
sess  = resp["paymentSession"]
limit = sess["limits"]["maxSpendAmount"]["value"]
spent = sess.get("currentSpendAmount", {}).get("value", "0")
remaining = float(limit) - float(spent)
print(f"budget={limit} spent={spent} remaining={remaining}")
```

If `MANAGEMENT_ROLE_ARN` is not set, the buyer prints a friendly hint and skips — the rest of the flow still works.

### 4.4 `SKIP_VERIFY` safety banner (v2)

`SKIP_VERIFY=true` lets you demo without a facilitator round‑trip. It's easy to forget it's on — so the seller now prints a hard‑to‑miss warning at startup:

```
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  !!  WARNING: SKIP_VERIFY=true — on-chain verification   !!
  !!  is DISABLED. Payment proofs are accepted without    !!
  !!  contacting the x402 facilitator.                    !!
  !!                                                      !!
  !!  This is LOCAL DEVELOPMENT ONLY. Do NOT demo or      !!
  !!  deploy with this flag set.                          !!
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

### 4.5 BlockRun debug logging (v2)

When the seller proxies to BlockRun, it prints structured debug blocks:

```
  ┌─── BlockRun Debug ─────────────────────────────────────
  │ URL      : https://blockrun.ai/api/v1/chat/completions
  │ Model    : openai/gpt-oss-20b
  │ Timeout  : Timeout(connect=15, read=120, write=120, pool=120)
  │ Seller   : 0x07f7...d658
  │ Ampersend: https://api.ampersend.ai
  │ Messages : 1
  │   [0] user: Tell me how x402 works
  └────────────────────────────────────────────────────────

  ┌─── BlockRun Response ──────────────────────────────────
  │ Status   : 402
  │ Elapsed  : 7.15s
  │ Headers  : (all response headers)
  │ Body     : (full body on errors, summary on success)
  └────────────────────────────────────────────────────────
```

On exceptions (e.g. `ReadTimeout`), the error block includes the full traceback — suitable for pasting into a bug report.

---

## 5. Configuration

`.env` — copy from `.env.sample`.

| Variable | Used by | Comes from |
|----------|---------|-----------|
| `AWS_REGION`, `AWS_PROFILE` | buyer | your SSO setup |
| `DP_ENDPOINT` | buyer | `https://bedrock-agentcore.<region>.amazonaws.com` |
| `MANAGER_ARN` | buyer, seller | `quickstart/setup_manager.py` output |
| `PROCESS_PAYMENT_ROLE_ARN` | buyer | `quickstart/setup_roles.sh` output |
| `MANAGEMENT_ROLE_ARN` *(v2, optional)* | buyer | `quickstart/setup_roles.sh` output — enables `GetPaymentSession` readout |
| `PAYMENT_SESSION_ID`, `PAYMENT_INSTRUMENT_ID`, `USER_ID` | buyer | `scripts/e2e-test.sh` output |
| `SELLER_SMART_ACCOUNT_ADDRESS`, `SELLER_SESSION_KEY` | seller | Ampersend dashboard |
| `NETWORK` | seller | `base` |
| `AMPERSEND_API_URL` | seller | `https://api.ampersend.ai` (default) |
| `MODEL_CATALOG` *(v2, optional)* | seller | JSON array — overrides the default fast/balanced/premium tiers |
| `BLOCKRUN_MODEL`, `PRICE_MICRO_USDC` *(legacy)* | seller | Still honored — overrides the **fast** tier only |
| `CDP_API_KEY_ID` | seller | CDP API key ID from [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/) — required for CDP facilitator |
| `CDP_API_KEY_SECRET` | seller | CDP API key secret (EC private key) |
| `FACILITATOR_URL` | seller | `https://api.cdp.coinbase.com/platform/v2/x402` (default) |
| `SKIP_VERIFY` | seller | Set `true` for local dev only (loud banner) |
| `MOCK_ON_UPSTREAM_FAILURE` | seller | Set `true` to return a synthetic LLM response when BlockRun is unreachable |

---

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Expecting value: line 1 column 1 (char 0)` from facilitator | Ensure `FACILITATOR_URL` points to `https://api.cdp.coinbase.com/platform/v2/x402` and CDP API keys are set |
| `Cannot convert undefined to a BigInt` | Include both `amount` and `maxAmountRequired` in `accepts[0]` |
| `invalid_exact_evm_insufficient_balance` | Fund the `payer` address (shown in the error) with USDC on Base |
| `invalid_exact_evm_transaction_failed` with funded payer | Settlement failed on-chain — the proof may be valid but the relayer broadcast failed. Check CDP API key auth, or use `SKIP_VERIFY=true` for local dev. Check the seller log line **Facilitator JSON:** for the full error |
| `SETTLEMENT_FAILED` from BlockRun (HTTP 402 with `code: SETTLEMENT_FAILED`) | BlockRun's own x402 settlement is failing — same facilitator infrastructure issue. The debug block in the seller logs will show the full error from BlockRun. Contact `@bc1max` on Telegram |
| `ReadTimeout` from BlockRun | The Ampersend SDK client timeout may be too low. v2 sets it to 120s; if still timing out, BlockRun's settlement + inference is taking longer than 2 minutes |
| `HTTP 502` with `Upstream BlockRun x402 did not complete` | BlockRun returned HTTP 402 to the Ampersend client (settlement failed, or still asking for payment). Your AgentCore payment to the seller already succeeded — this is the **seller→BlockRun** leg. Check seller logs and contact BlockRun if `SETTLEMENT_FAILED` persists |
| `HTTP 502` from seller with `Upstream error:` | Same leg failed with an exception (e.g. `ReadTimeout`). See the `BlockRun ERROR` debug block in the seller terminal |
| Buyer loops on *"Still HTTP 402"* | The **seller** rejected *your* proof (facilitator `/settle` failed or bad proof). If the failure is actually BlockRun, the seller maps that to **502** so you are not told to fund the buyer wallet by mistake |
| `AccessDenied` on `ProcessPayment` | You didn't assume `ProcessPaymentRole` first |
| `TokenRetrievalError: Token has expired` | Re-authenticate: `aws sso login --profile <your-profile>` |
| `Payment session not found` | Re-run `scripts/e2e-test.sh` to create a fresh session and instrument, then update `.env` |
| `[after] (skipped — set MANAGEMENT_ROLE_ARN …)` | Add `MANAGEMENT_ROLE_ARN` to `.env` to see the budget readout |
| `[after] (GetPaymentSession failed: AccessDenied)` | Your `MANAGEMENT_ROLE_ARN` doesn't have `bedrock-agentcore:GetPaymentSession` — check `quickstart/setup_roles.sh` |
| Buyer routes everything to `fast` | The catalog heuristic looks at keywords and length; use `--tier balanced|premium` to force |

---

## 7. Known Issues

**Facilitator:** This demo defaults to the **CDP facilitator** (`api.cdp.coinbase.com`) which supports Base mainnet settlement with 1,000 free transactions/month. Requires `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` from [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/). The `x402.org` facilitator is testnet-only and does not support Base mainnet.

- Set `SKIP_VERIFY=true` to bypass the buyer→seller facilitator check (local dev only).
- The seller→BlockRun leg depends on BlockRun / Ampersend settlement — no client-side workaround.
- `MOCK_ON_UPSTREAM_FAILURE=true` returns a synthetic LLM response so you can demo the full flow shape without a live BlockRun response.

---

## 8. Files

| File | What it is |
|------|------------|
| `buyer.py` | Deterministic agent: catalog → route → 402 → `ProcessPayment` → retry → `GetPaymentSession` |
| `seller.py` | Starlette tiered x402 gate + Ampersend → BlockRun proxy + catalog endpoint + debug logging |
| `.env.sample` | Copy to `.env`, fill in |
| `requirements.txt` | `boto3`, `httpx`, `starlette`, `ampersend-sdk`, … |

For an **LLM‑driven** agent (instead of the deterministic one here), see `../strands-agent/`.

---

## References

- [AgentCore Payments — Private Preview Guide](../docs/getting-started.md)
- [x402 Exact EVM spec](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md)
- [Ampersend SDK](https://github.com/edgeandnode/ampersend-sdk)
- [BlockRun API](https://github.com/BlockRunAI/awesome-blockrun)
- [Base Explorer](https://basescan.org/)
