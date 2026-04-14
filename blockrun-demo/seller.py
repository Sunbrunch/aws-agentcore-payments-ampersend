#!/usr/bin/env python3
"""
Ampersend x402 Seller — Pay-per-request LLM via BlockRun.

HTTP server that gates access to BlockRun's LLM API with x402 payments.
Incoming requests must include an x402 payment proof. Verified requests
are proxied to BlockRun using ampersend-sdk's X402Transport, which handles
the seller's outgoing payment to BlockRun automatically.

Architecture:
    Buyer (AgentCore) → POST /v1/chat/completions (+ x402 proof)
        → Seller verifies payment
        → ampersend httpx client auto-pays BlockRun via x402
        → BlockRun LLM response returned to buyer

Start:
    python seller.py

Environment:
    SELLER_SMART_ACCOUNT_ADDRESS  Seller's Ampersend smart account
    SELLER_SESSION_KEY            Seller's session key for signing
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

BLOCKRUN_API_URL = (
    "https://testnet.blockrun.ai/api/v1"
    if NETWORK == "base-sepolia"
    else "https://blockrun.ai/api/v1"
)
BLOCKRUN_MODEL = os.environ.get("BLOCKRUN_MODEL", "openai/gpt-oss-20b")

USDC_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia USDC
PRICE_MICRO_USDC = int(os.environ.get("PRICE_MICRO_USDC", "2000"))  # 0.002 USDC

FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
SKIP_VERIFY = os.environ.get("SKIP_VERIFY", "false").lower() == "true"

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


def _payment_requirements(resource: str) -> dict:
    """Build x402 v2 payment requirements for the given resource."""
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK,
                "maxAmountRequired": str(PRICE_MICRO_USDC),
                "asset": USDC_ASSET,
                "payTo": SELLER_ADDRESS,
                "maxTimeoutSeconds": 30,
                "extra": {"name": "USDC", "version": "2"},
                "resource": resource,
                "description": "BlockRun LLM inference via Ampersend",
                "mimeType": "application/json",
                "outputSchema": {},
            }
        ],
    }


def _return_402(resource: str) -> Response:
    """Return HTTP 402 with x402 payment requirements in both body and header."""
    reqs = _payment_requirements(resource)
    encoded = base64.b64encode(json.dumps(reqs).encode()).decode()
    return Response(
        content=json.dumps(reqs, indent=2),
        status_code=402,
        headers={"PAYMENT-REQUIRED": encoded, "Content-Type": "application/json"},
    )


def _extract_payment_proof(request: Request) -> dict | None:
    """Extract and decode x402 payment proof from request headers."""
    raw = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:
        return None


async def _settle_payment(proof: dict, requirements: dict) -> dict:
    """Verify x402 payment via facilitator.

    Set SKIP_VERIFY=true for quick local testing without on-chain settlement.
    In production, always verify via the facilitator.
    """
    if SKIP_VERIFY:
        if "payload" not in proof:
            return {"success": False, "error": "Malformed proof: missing payload"}
        print("  [skip-verify] Accepted payment proof (verification disabled)")
        return {"success": True, "transaction": "skip-verify"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{FACILITATOR_URL}/{NETWORK}/settle",
                json={
                    "x402Version": proof.get("x402Version", 2),
                    "paymentPayload": proof,
                    "paymentRequirements": requirements["accepts"][0],
                },
                timeout=30,
            )
            result = resp.json()
            if result.get("success"):
                tx = result.get("transaction", "")
                print(f"  Settled on-chain: {tx[:20]}..." if tx else "  Settled")
            return result
    except Exception as e:
        print(f"  Facilitator error: {e}")
        return {"success": False, "error": str(e)}


# ── Routes ───────────────────────────────────────────────────────


async def chat_completions(request: Request) -> Response:
    """OpenAI-compatible /v1/chat/completions with x402 payment gate."""
    resource = "/v1/chat/completions"

    proof = _extract_payment_proof(request)
    if not proof:
        print(f"  402 -> {request.client.host} (no payment)")
        return _return_402(resource)

    requirements = _payment_requirements(resource)
    settlement = await _settle_payment(proof, requirements)
    if not settlement.get("success"):
        print(f"  Payment rejected: {settlement.get('error')}")
        return _return_402(resource)

    print(f"  Payment verified from {request.client.host}")

    try:
        body = await request.json()
    except Exception:
        body = {}

    if "model" not in body:
        body["model"] = BLOCKRUN_MODEL

    print(f"  Proxying to BlockRun ({body.get('model')})...")

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
                "Content-Type": resp.headers.get("content-type", "application/json")
            },
        )
    except Exception as e:
        print(f"  BlockRun error: {e}")
        return JSONResponse({"error": f"Upstream error: {e}"}, status_code=502)


async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse(
        {
            "status": "ok",
            "seller": SELLER_ADDRESS,
            "network": NETWORK,
            "model": BLOCKRUN_MODEL,
            "blockrun": BLOCKRUN_API_URL,
            "price_usdc": PRICE_MICRO_USDC / 1_000_000,
        }
    )


app = Starlette(
    routes=[
        Route(
            "/v1/chat/completions", chat_completions, methods=["GET", "POST"]
        ),
        Route("/health", health),
    ],
)


if __name__ == "__main__":
    print("=" * 60)
    print("  Ampersend x402 Seller -> BlockRun LLM")
    print("=" * 60)
    print(f"  Wallet  : {SELLER_ADDRESS}")
    print(f"  Network : {NETWORK}")
    print(f"  Model   : {BLOCKRUN_MODEL}")
    print(f"  BlockRun: {BLOCKRUN_API_URL}")
    print(f"  Price   : ${PRICE_MICRO_USDC / 1_000_000:.4f} USDC")
    print(f"  Verify  : {'facilitator' if not SKIP_VERIFY else 'DISABLED'}")
    print(f"  Port    : {PORT}")
    print(f"\n  Endpoint: http://localhost:{PORT}/v1/chat/completions")
    print(f"  Health : http://localhost:{PORT}/health")
    print("=" * 60)
    print()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
