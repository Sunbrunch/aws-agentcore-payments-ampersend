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

# ── Model Catalog (the "pay-per-intelligence" story) ─────────────
# Buyers pick a tier based on task complexity. Each tier maps to a
# different BlockRun model at a different price point. This is the
# "dynamically route to optimal AI model" narrative: the agent pays
# more only when the task demands it.
#
# Override by setting MODEL_CATALOG to a JSON array of
# {id, model, price_micro_usdc, description} entries.

DEFAULT_CATALOG = [
    {
        "id": "fast",
        "model": "openai/gpt-oss-20b",
        "price_micro_usdc": 1000,   # $0.001
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
        # Backward-compat: allow the old single-model envs to override the fast tier.
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
DEFAULT_TIER = CATALOG[0]  # first entry is the default/fallback


def _resolve_tier(requested: str | None) -> dict:
    """Resolve the requested model (or tier id) to a catalog entry."""
    if not requested:
        return DEFAULT_TIER
    return (
        CATALOG_BY_MODEL.get(requested)
        or CATALOG_BY_ID.get(requested)
        or DEFAULT_TIER
    )


def _caip2_network(env_network: str) -> str:
    """Map friendly names to CAIP-2 ids (x402.org + AgentCore use eip155:84532 for Base Sepolia)."""
    n = (env_network or "").strip().lower()
    if n in ("base-sepolia", "base_sepolia"):
        return "eip155:84532"
    return env_network


CAIP2_NETWORK = _caip2_network(NETWORK)

# Optional tweaks for picky facilitators — **off by default** so `accepts[0]` matches
# the proven x402.org + AgentCore shape (adding gasLimit or changing maxTimeoutSeconds
# changes what ProcessPayment signs; that can break settlement if anything is mismatched).
# See coinbase/x402#418 / #1065 if verify works but settle fails.
_X402_EXTRA: dict = {
    "name": "USDC",
    "version": "2",
    "assetTransferMethod": "eip3009",
}
_gl = os.environ.get("X402_USDC_GAS_LIMIT", "").strip()
if _gl:
    _X402_EXTRA = {**_X402_EXTRA, "gasLimit": _gl}
_MAX_TIMEOUT = int(os.environ.get("MAX_TIMEOUT_SECONDS", "30"))

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


def _payment_requirements(resource: str, tier: dict) -> dict:
    """Build x402 v2 payment requirements for the given resource and tier."""
    price = str(tier["price_micro_usdc"])
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": CAIP2_NETWORK,
                # Facilitators (x402.org Exact EVM) use `amount` for BigInt; some clients use maxAmountRequired only.
                "amount": price,
                "maxAmountRequired": price,
                "asset": USDC_ASSET,
                "payTo": SELLER_ADDRESS,
                "maxTimeoutSeconds": _MAX_TIMEOUT,
                "extra": dict(_X402_EXTRA),
                "resource": resource,
                "description": (
                    f"BlockRun LLM inference via Ampersend — tier={tier['id']} "
                    f"model={tier['model']}"
                ),
                "mimeType": "application/json",
                # Keep empty — tier info is in description; non-empty outputSchema was
                # stripped by AgentCore before signing but we used to POST full accepts
                # to /settle, breaking facilitators (see _payment_requirements_for_settle).
                "outputSchema": {},
            }
        ],
    }


def _return_402(resource: str, tier: dict) -> Response:
    """Return HTTP 402 with x402 payment requirements in both body and header."""
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
    """Extract and decode x402 payment proof from request headers."""
    raw = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:
        return None


def _payment_requirements_for_settle(accepts0: dict, x402_version: int) -> dict:
    """Align with buyer.py / AgentCore ProcessPayment: v2 strips metadata before signing.

    If we POST the full `accepts[0]` (with description, outputSchema, …) to /settle
    while the proof was produced from the stripped payload, facilitators can reject
    with invalid_exact_evm_transaction_failed even when USDC balance is fine.
    """
    req = dict(accepts0)
    if x402_version >= 2:
        for key in ("description", "mimeType", "resource", "outputSchema"):
            req.pop(key, None)
    return req


# ── Direct EIP-3009 simulation (diagnostic) ──────────────────────
# When a facilitator returns opaque `transaction_failed` / `invalid_exact_evm_transaction_failed`,
# we can independently simulate the USDC.transferWithAuthorization call against Base Sepolia
# to reveal the actual revert reason (e.g. "FiatTokenV2: authorization is used or canceled",
# "authorization is not yet valid", "invalid signature", etc.). This is the single most useful
# thing when the facilitator is behaving as a black box.
BASE_SEPOLIA_RPC_URL = os.environ.get("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
_TWA_SELECTOR = "0xe3ee160e"


def _hex_to_bytes(value: str, length: int | None = None) -> bytes:
    if value is None:
        raise ValueError("missing value")
    s = value[2:] if isinstance(value, str) and value.startswith("0x") else str(value)
    b = bytes.fromhex(s)
    if length is not None and len(b) != length:
        raise ValueError(f"expected {length} bytes, got {len(b)}")
    return b


def _split_signature(sig_hex: str) -> tuple[int, bytes, bytes]:
    """Split a 65-byte compact EIP-2098/EIP-712 signature into (v, r, s)."""
    raw = _hex_to_bytes(sig_hex, 65)
    r, s, v = raw[:32], raw[32:64], raw[64]
    if v < 27:
        v += 27
    return v, r, s


def _encode_twa_calldata(auth: dict, signature: str) -> str:
    """ABI-encode USDC.transferWithAuthorization using the proof's EIP-3009 auth + signature."""
    try:
        from eth_abi import encode as abi_encode
    except ImportError as e:
        raise RuntimeError(
            "eth_abi not installed — cannot simulate EIP-3009 directly."
        ) from e
    value = int(auth["value"])
    valid_after = int(auth["validAfter"])
    valid_before = int(auth["validBefore"])
    nonce = _hex_to_bytes(auth["nonce"], 32)
    v, r, s = _split_signature(signature)
    encoded = abi_encode(
        [
            "address", "address", "uint256", "uint256", "uint256",
            "bytes32", "uint8", "bytes32", "bytes32",
        ],
        [
            auth["from"], auth["to"], value, valid_after, valid_before,
            nonce, v, r, s,
        ],
    )
    return _TWA_SELECTOR + encoded.hex()


def _decode_revert(data_hex: str) -> str:
    """Decode a Solidity Error(string) revert blob into a readable reason."""
    if not data_hex or not data_hex.startswith("0x") or len(data_hex) < 10:
        return data_hex or "(no revert data)"
    selector = data_hex[:10]
    # Error(string) selector = 0x08c379a0; Panic(uint256) = 0x4e487b71
    try:
        from eth_abi import decode as abi_decode
    except ImportError:
        return data_hex
    body = bytes.fromhex(data_hex[10:]) if len(data_hex) > 10 else b""
    try:
        if selector == "0x08c379a0":
            (reason,) = abi_decode(["string"], body)
            return reason
        if selector == "0x4e487b71":
            (code,) = abi_decode(["uint256"], body)
            return f"Panic(0x{code:x})"
    except Exception:
        pass
    return data_hex


async def _facilitator_verify_diagnostic(payload: dict) -> str:
    """POST the same body as /settle to /verify — distinguishes signature vs relay failure."""
    verify_url = f"{FACILITATOR_URL.rstrip('/')}/verify"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(verify_url, json=payload, timeout=30)
        text = (resp.text or "").strip()
        if not text:
            return f"empty HTTP {resp.status_code} from {verify_url}"
        try:
            vr = resp.json()
        except json.JSONDecodeError:
            return f"non-JSON HTTP {resp.status_code}: {text[:200]}"
        valid = vr.get("isValid")
        ir = vr.get("invalidReason") or vr.get("error")
        im = vr.get("invalidMessage") or vr.get("message")
        payer = vr.get("payer", "")
        return (
            f"isValid={valid} payer={payer} invalidReason={ir}"
            + (f" ({im})" if im and str(im) != str(ir) else "")
        )
    except Exception as e:
        return f"request failed: {e}"


async def _simulate_eip3009(proof: dict) -> str:
    """Return a one-line diagnosis from a direct eth_call to USDC.transferWithAuthorization.

    This bypasses the facilitator entirely and asks Base Sepolia itself why the
    transfer would revert. Returns a human-readable string for logging.
    """
    try:
        payload = proof.get("payload") or proof
        auth = (payload or {}).get("authorization") or {}
        signature = (payload or {}).get("signature") or ""
        if not auth or not signature:
            return "proof missing authorization/signature — cannot simulate"

        data = _encode_twa_calldata(auth, signature)
        call = {"to": USDC_ASSET, "data": data}
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [call, "latest"],
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(BASE_SEPOLIA_RPC_URL, json=body, timeout=15)
            js = resp.json()

        if "result" in js:
            return "direct eth_call succeeded — USDC would accept this authorization"
        err = js.get("error") or {}
        revert_data = err.get("data") or ""
        if isinstance(revert_data, dict):
            revert_data = revert_data.get("data", "") or revert_data.get("originalError", {}).get("data", "")
        reason = _decode_revert(revert_data) if revert_data else err.get("message", "unknown error")
        return f"RPC revert: {reason}  (raw: {err.get('message', '')})"
    except Exception as e:
        return f"simulation skipped ({e})"


async def _settle_payment(proof: dict, requirements: dict) -> dict:
    """Verify x402 payment via facilitator.

    Set SKIP_VERIFY=true for quick local testing without on-chain settlement.
    In production, always verify via the facilitator.
    """
    if SKIP_VERIFY:
        if "payload" not in proof:
            return {"success": False, "error": "Malformed proof: missing payload"}
        print("  [skip-verify] Accepted payment proof (verification DISABLED)")
        return {"success": True, "transaction": "skip-verify"}

    x402_ver = int(proof.get("x402Version", 2))
    settle_url = f"{FACILITATOR_URL.rstrip('/')}/settle"
    payload = {
        "x402Version": x402_ver,
        "paymentPayload": proof,
        "paymentRequirements": _payment_requirements_for_settle(
            requirements["accepts"][0], x402_ver
        ),
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
                print(f"  Facilitator settlement failed: {er}  payer={payer}")
                em = result.get("errorMessage") or result.get("message")
                if em and str(em) != str(er):
                    print(f"  errorMessage: {em}")
                # Full JSON often includes fields the short errorReason omits (gas, revert data).
                _rj = json.dumps(result, default=str)
                print(f"  Facilitator JSON: {_rj[:900]}{'…' if len(_rj) > 900 else ''}")

                # The facilitator hides the actual EVM revert. Ask Base Sepolia directly
                # via eth_call to USDC.transferWithAuthorization — this almost always
                # tells us the real cause (insufficient balance, nonce reused, bad signature,
                # validAfter window, etc.).
                if isinstance(er, str) and (
                    "transaction_failed" in er or "insufficient" in er.lower()
                ):
                    diag = await _simulate_eip3009(proof)
                    print(f"  Direct USDC simulation: {diag}")
                    vsum = await _facilitator_verify_diagnostic(payload)
                    print(f"  Facilitator /verify: {vsum}")

                    sim_ok = (
                        "succeeded" in diag.lower()
                        or "would accept" in diag.lower()
                    )
                    if sim_ok:
                        print(
                            "  → EIP-3009 is valid on Base Sepolia (eth_call), but "
                            "/settle failed. The problem is this facilitator's "
                            "relayer/broadcast step (their gas wallet, RPC, or internal "
                            "simulation), not your proof or USDC balance. Try "
                            "FACILITATOR_URL=https://www.x402.org/facilitator, or "
                            "Coinbase CDP's facilitator (API key). "
                            "SKIP_VERIFY=true is local-only."
                        )
                    else:
                        print(
                            "  → If the simulation shows 'authorization is used or "
                            "canceled': the EIP-3009 nonce was already consumed; "
                            "re-run scripts/e2e-test.sh for a new session/instrument. "
                            "Other causes: 0 USDC on Base Sepolia, validAfter in the "
                            "future, or invalid signature. SKIP_VERIFY=true is local-only."
                        )
            return result
    except Exception as e:
        print(f"  Facilitator error: {e}")
        return {"success": False, "error": str(e)}


# ── Routes ───────────────────────────────────────────────────────


async def list_models(request: Request) -> JSONResponse:
    """Publish the model catalog so buyers can pick a tier before paying.

    Free endpoint — no payment required. Returned shape is OpenAI-ish
    plus an `x402` block with the tier pricing.
    """
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
    """OpenAI-compatible /v1/chat/completions with tiered x402 payment gate."""
    resource = "/v1/chat/completions"

    # Peek at the request body so we can price by tier before the payment step.
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

    # Upstream to BlockRun always uses the catalog's canonical model id.
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
        return JSONResponse({"error": f"Upstream error: {e}"}, status_code=502)


async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
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
        # Back-compat: older buyer builds derived .../v1/chat/models by mistake.
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
