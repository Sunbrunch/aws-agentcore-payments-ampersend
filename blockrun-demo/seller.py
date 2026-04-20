#!/usr/bin/env python3
"""
Ampersend x402 Seller — Pay-per-intelligence LLM router via BlockRun.

HTTP server that gates access to BlockRun's LLM API with x402 payments.
Incoming requests must include an x402 payment proof. The seller exposes
a *catalog* of models at different price tiers — the buyer picks the tier
that matches the complexity of the task (fast/balanced/premium).

Verified requests are proxied to BlockRun using ampersend-sdk's
X402Transport, which handles the seller's outgoing payment to BlockRun
automatically.

Architecture:
    Buyer (AgentCore) → GET  /v1/models                 (catalog, free)
                      → POST /v1/chat/completions       (tiered price)
                          ↳ 402 with payment requirements for that tier
                          ↳ Seller verifies on-chain via facilitator
                          ↳ ampersend httpx client pays BlockRun via x402
                          ↳ BlockRun LLM response returned to buyer

Start:
    python seller.py

Environment:
    SELLER_SMART_ACCOUNT_ADDRESS  Seller's Ampersend smart account
    SELLER_SESSION_KEY            Seller's session key for signing
    MODEL_CATALOG                 Optional JSON override for the tier map
"""

import base64
import json
import os

import httpx
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

load_dotenv()

# ── Configuration ────────────────────────────────────────────────
SELLER_ADDRESS = os.environ["SELLER_SMART_ACCOUNT_ADDRESS"]
SELLER_SESSION_KEY = os.environ["SELLER_SESSION_KEY"]
NETWORK = os.environ.get("NETWORK", "base-sepolia")
AMPERSEND_API_URL = os.environ.get("AMPERSEND_API_URL", "https://api.staging.ampersend.ai")
PORT = int(os.environ.get("SELLER_PORT", "8002"))

def _is_base_sepolia(env_network: str) -> bool:
    n = (env_network or "").strip().lower()
    return n in ("base-sepolia", "base_sepolia", "eip155:84532")


BLOCKRUN_API_URL = (
    "https://testnet.blockrun.ai/api/v1"
    if _is_base_sepolia(NETWORK)
    else "https://blockrun.ai/api/v1"
)

USDC_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia USDC

# Public x402.org facilitator: POST {FACILITATOR_URL}/settle (no /{network}/ in path).
# Use www host — bare x402.org often 308-redirects; wrong paths return HTML/empty → JSON errors.
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://www.x402.org/facilitator")
SKIP_VERIFY = os.environ.get("SKIP_VERIFY", "false").lower() == "true"
# When BlockRun's upstream x402 payment also fails (same facilitator outage),
# return a synthetic LLM response so the demo flow is still visible end-to-end.
MOCK_ON_UPSTREAM_FAILURE = os.environ.get("MOCK_ON_UPSTREAM_FAILURE", "false").lower() == "true"

# ── Model Catalog (the "pay-per-intelligence" story) ─────────────

DEFAULT_CATALOG = [
    {
        "id": "fast",
        "model": "openai/gpt-oss-20b",
        "price_micro_usdc": 2000,   # $0.002
        "description": "Fast, cheap. Good for short answers, quick summaries, classification.",
    },
    {
        "id": "balanced",
        "model": "openai/gpt-oss-120b",
        "price_micro_usdc": 3000,   # $0.003
        "description": "Balanced quality. Good for multi-paragraph analysis, governance summaries.",
    },
    {
        "id": "premium",
        "model": "deepseek/deepseek-v3",
        "price_micro_usdc": 8000,   # $0.008
        "description": "Premium reasoning. Good for smart-contract audits, deep technical review.",
    },
]


def _load_catalog() -> list[dict]:
    override = os.environ.get("MODEL_CATALOG")
    if not override:
        legacy_model = os.environ.get("BLOCKRUN_MODEL")
        legacy_price = os.environ.get("PRICE_MICRO_USDC")
        catalog = [dict(t) for t in DEFAULT_CATALOG]
        if legacy_model:
            catalog[0]["model"] = legacy_model
        if legacy_price:
            try:
                catalog[0]["price_micro_usdc"] = int(legacy_price)
            except ValueError:
                pass
        return catalog
    try:
        parsed = json.loads(override)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("MODEL_CATALOG must be a non-empty JSON array")
        for entry in parsed:
            if not {"id", "model", "price_micro_usdc"} <= entry.keys():
                raise ValueError("Each MODEL_CATALOG entry needs id, model, price_micro_usdc")
            entry["price_micro_usdc"] = int(entry["price_micro_usdc"])
            entry.setdefault("description", "")
        return parsed
    except (ValueError, json.JSONDecodeError) as e:
        raise SystemExit(f"Invalid MODEL_CATALOG: {e}")


CATALOG = _load_catalog()
CATALOG_BY_MODEL: dict[str, dict] = {t["model"]: t for t in CATALOG}
CATALOG_BY_ID: dict[str, dict] = {t["id"]: t for t in CATALOG}
DEFAULT_TIER = CATALOG[0]


def _resolve_tier(requested: str | None) -> dict:
    if not requested:
        return DEFAULT_TIER
    return (
        CATALOG_BY_MODEL.get(requested)
        or CATALOG_BY_ID.get(requested)
        or DEFAULT_TIER
    )


def _caip2_network(env_network: str) -> str:
    n = (env_network or "").strip().lower()
    if n in ("base-sepolia", "base_sepolia"):
        return "eip155:84532"
    return env_network


CAIP2_NETWORK = _caip2_network(NETWORK)

# ── Ampersend HTTP client (auto-pays BlockRun via x402) ──────────
_blockrun_client: httpx.AsyncClient | None = None


def _get_blockrun_client() -> httpx.AsyncClient:
    global _blockrun_client
    if _blockrun_client is None:
        from ampersend_sdk import create_ampersend_http_client

        _blockrun_client = create_ampersend_http_client(
            smart_account_address=SELLER_ADDRESS,
            session_key_private_key=SELLER_SESSION_KEY,
            api_url=AMPERSEND_API_URL,
        )
    return _blockrun_client


# ── x402 Payment Requirements ───────────────────────────────────
# IMPORTANT: The shape of accepts[0] MUST match what main uses, because this
# exact dict goes to the facilitator /settle as paymentRequirements. The
# facilitator compares fields. Keep extra={name,version,assetTransferMethod},
# maxTimeoutSeconds=30, and include description/mimeType/outputSchema.


def _payment_requirements(resource: str, tier: dict) -> dict:
    price = str(tier["price_micro_usdc"])
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": CAIP2_NETWORK,
                "amount": price,
                "maxAmountRequired": price,
                "asset": USDC_ASSET,
                "payTo": SELLER_ADDRESS,
                "maxTimeoutSeconds": 30,
                "extra": {
                    "name": "USDC",
                    "version": "2",
                    "assetTransferMethod": "eip3009",
                },
                "resource": resource,
                "description": "BlockRun LLM inference via Ampersend",
                "mimeType": "application/json",
                "outputSchema": {},
            }
        ],
    }


def _return_402(resource: str, tier: dict) -> Response:
    reqs = _payment_requirements(resource, tier)
    encoded = base64.b64encode(json.dumps(reqs).encode()).decode()
    return Response(
        content=json.dumps(reqs, indent=2),
        status_code=402,
        headers={
            "PAYMENT-REQUIRED": encoded,
            "Content-Type": "application/json",
            "X-Tier": tier["id"],
            "X-Model": tier["model"],
        },
    )


def _extract_payment_proof(request: Request) -> dict | None:
    raw = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:
        return None


# ── Settlement — IDENTICAL to main branch ────────────────────────
# The settle payload shape must exactly match what worked on main.
# No field stripping, no retry loop, no diagnostics — just the same
# POST to {FACILITATOR_URL}/settle that main uses.


async def _settle_payment(proof: dict, requirements: dict) -> dict:
    if SKIP_VERIFY:
        if "payload" not in proof:
            return {"success": False, "error": "Malformed proof: missing payload"}
        print("  [skip-verify] Accepted payment proof (verification disabled)")
        return {"success": True, "transaction": "skip-verify"}

    settle_url = f"{FACILITATOR_URL.rstrip('/')}/settle"
    payload = {
        "x402Version": proof.get("x402Version", 2),
        "paymentPayload": proof,
        "paymentRequirements": requirements["accepts"][0],
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(settle_url, json=payload, timeout=30)
            text = (resp.text or "").strip()
            if not text:
                err = f"empty response (HTTP {resp.status_code}) from {settle_url}"
                print(f"  Facilitator error: {err}")
                return {"success": False, "error": err}
            try:
                result = resp.json()
            except json.JSONDecodeError:
                err = f"non-JSON (HTTP {resp.status_code}): {text[:300]}"
                print(f"  Facilitator error: {err}")
                return {"success": False, "error": err}
            if result.get("success"):
                tx = result.get("transaction", "")
                print(f"  Settled on-chain: {tx[:20]}..." if tx else "  Settled")
            else:
                er = result.get("errorReason") or result.get("error")
                payer = result.get("payer", "")
                print(f"  Settlement failed: {er}  payer={payer}")
                _rj = json.dumps(result, default=str)
                print(f"  Facilitator JSON: {_rj[:500]}")
            return result
    except Exception as e:
        print(f"  Facilitator error: {e}")
        return {"success": False, "error": str(e)}


def _mock_chat_response(body: dict, tier: dict) -> JSONResponse:
    """Synthetic OpenAI-shaped response when BlockRun is unreachable."""
    import time
    prompt = ""
    if isinstance(body, dict):
        msgs = body.get("messages") or []
        if msgs and isinstance(msgs[-1], dict):
            prompt = msgs[-1].get("content", "")
    text = (
        f"[Mock response — BlockRun upstream unavailable]\n\n"
        f"Tier: {tier['id']} | Model: {tier['model']} | "
        f"Price: ${tier['price_micro_usdc']/1_000_000:.4f} USDC\n\n"
        f"Your prompt ({len(prompt)} chars) was received and payment was verified. "
        f"In production, this would be answered by {tier['model']} via BlockRun. "
        f"The x402 facilitators on Base Sepolia are currently experiencing settlement "
        f"failures — once they recover, both the buyer→seller and seller→BlockRun "
        f"payment legs will settle on-chain and this mock will not be needed."
    )
    return JSONResponse(
        {
            "id": f"mock-{int(time.time())}",
            "object": "chat.completion",
            "model": tier["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4, "total_tokens": (len(prompt) + len(text)) // 4},
            "x_tier": tier["id"],
            "x_mock": True,
        },
        headers={"X-Tier": tier["id"], "X-Model": tier["model"], "X-Mock": "true"},
    )


# ── Routes ───────────────────────────────────────────────────────


async def list_models(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": tier["model"],
                    "tier": tier["id"],
                    "description": tier["description"],
                    "object": "model",
                    "owned_by": "blockrun",
                    "x402": {
                        "network": CAIP2_NETWORK,
                        "asset": USDC_ASSET,
                        "price_micro_usdc": tier["price_micro_usdc"],
                        "price_usdc": tier["price_micro_usdc"] / 1_000_000,
                    },
                }
                for tier in CATALOG
            ],
        }
    )


async def chat_completions(request: Request) -> Response:
    resource = "/v1/chat/completions"

    try:
        body = await request.json()
    except Exception:
        body = {}

    requested = body.get("model") if isinstance(body, dict) else None
    tier = _resolve_tier(requested)

    proof = _extract_payment_proof(request)
    if not proof:
        print(
            f"  402 -> {request.client.host} (no payment)  "
            f"tier={tier['id']} model={tier['model']} "
            f"price=${tier['price_micro_usdc']/1_000_000:.4f}"
        )
        return _return_402(resource, tier)

    requirements = _payment_requirements(resource, tier)
    settlement = await _settle_payment(proof, requirements)
    if not settlement.get("success"):
        err = settlement.get("error") or settlement.get("errorMessage") or settlement.get("invalidMessage")
        print(f"  Payment rejected: {err or settlement}")
        return _return_402(resource, tier)

    print(
        f"  Payment verified from {request.client.host}  "
        f"tier={tier['id']} price=${tier['price_micro_usdc']/1_000_000:.4f}"
    )

    if isinstance(body, dict):
        body["model"] = tier["model"]

    print(f"  Proxying to BlockRun ({tier['model']})...")

    client = _get_blockrun_client()
    try:
        resp = await client.post(
            f"{BLOCKRUN_API_URL}/chat/completions",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        print(f"  BlockRun -> {resp.status_code}")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={
                "Content-Type": resp.headers.get("content-type", "application/json"),
                "X-Tier": tier["id"],
                "X-Model": tier["model"],
            },
        )
    except Exception as e:
        print(f"  BlockRun error: {e}")
        if not MOCK_ON_UPSTREAM_FAILURE:
            return JSONResponse({"error": f"Upstream error: {e}"}, status_code=502)
        print("  [mock] Returning synthetic response (MOCK_ON_UPSTREAM_FAILURE=true)")
        return _mock_chat_response(body, tier)


async def health(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "seller": SELLER_ADDRESS,
            "network": CAIP2_NETWORK,
            "blockrun": BLOCKRUN_API_URL,
            "tiers": [
                {
                    "id": t["id"],
                    "model": t["model"],
                    "price_usdc": t["price_micro_usdc"] / 1_000_000,
                }
                for t in CATALOG
            ],
            "skip_verify": SKIP_VERIFY,
        }
    )


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["GET", "POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/v1/chat/models", list_models, methods=["GET"]),
        Route("/health", health),
    ],
)


if __name__ == "__main__":
    print("=" * 60)
    print("  Ampersend x402 Seller -> BlockRun LLM (tiered)")
    print("=" * 60)
    print(f"  Wallet  : {SELLER_ADDRESS}")
    print(f"  Network : {NETWORK} (x402: {CAIP2_NETWORK})")
    print(f"  BlockRun: {BLOCKRUN_API_URL}")
    print(f"  Verify  : {'facilitator' if not SKIP_VERIFY else '*** DISABLED ***'}")
    print(f"  Port    : {PORT}")
    print()
    print("  Model catalog (pay-per-intelligence):")
    for t in CATALOG:
        price = t["price_micro_usdc"] / 1_000_000
        default_marker = "  (default)" if t is DEFAULT_TIER else ""
        print(f"    [{t['id']:>8}] ${price:.4f}  {t['model']}{default_marker}")
        if t.get("description"):
            print(f"               {t['description']}")
    print()
    print(f"  Endpoint: http://localhost:{PORT}/v1/chat/completions")
    print(f"  Catalog : http://localhost:{PORT}/v1/models")
    print(f"  Health  : http://localhost:{PORT}/health")
    print("=" * 60)

    if SKIP_VERIFY:
        print()
        print("  " + "!" * 56)
        print("  !!  WARNING: SKIP_VERIFY=true — on-chain verification   !!")
        print("  !!  is DISABLED. Payment proofs are accepted without    !!")
        print("  !!  contacting the x402 facilitator.                    !!")
        print("  !!                                                      !!")
        print("  !!  This is LOCAL DEVELOPMENT ONLY. Do NOT demo or      !!")
        print("  !!  deploy with this flag set.                          !!")
        print("  " + "!" * 56)
        print()
    else:
        print()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
