#!/usr/bin/env python3
"""
AgentCore Payments Buyer — pays per-intelligence for a tiered LLM via x402.

This script follows the **Agent Execution (ProcessPaymentRole)** pattern from the
AgentCore Payments Private Preview guide. For the post-payment budget readout
it briefly assumes **ManagementRole** (ReadOnly on sessions) — in a real app the
backend would do that step and push the number back to the UI.

  • Assumes **ProcessPaymentRole** (can only call **ProcessPayment** — not create
    sessions or instruments).
  • Reads the seller's model catalog (`GET /v1/models`) and picks a tier
    (fast / balanced / premium) based on the prompt's complexity.
  • On HTTP 402, passes the merchant **accepts[0]** payload to **process_payment**
    as **cryptoX402** (v1 or v2), then retries the HTTP request with **X-PAYMENT**
    or **PAYMENT-SIGNATURE**.
  • After a successful LLM response, calls **GetPaymentSession** via
    **ManagementRole** (if `MANAGEMENT_ROLE_ARN` is set) and prints the session
    budget / current spend so the guardrails story is tangible.

Demonstrates step by step:
    [0] Assume AgentCore ProcessPaymentRole
    [1] GET  /v1/models                → pick tier for this prompt
    [2] POST /v1/chat/completions      → HTTP 402 (tiered)
    [3] AgentCore ProcessPayment       → payment proof (PROOF_GENERATED)
    [4] Retry with proof               → LLM response from BlockRun
    [5] GetPaymentSession (Management) → show budget decreasing

Usage:
    python buyer.py                                # interactive
    python buyer.py "Summarize EIP-4844 in 3 bullets."
    python buyer.py --example eip                  # canned EIP summary prompt
    python buyer.py --example proposal             # L2 governance proposal
    python buyer.py --example audit                # Solidity audit
    python buyer.py --tier premium "deep analysis" # force a tier

Environment:
    MANAGER_ARN, PAYMENT_SESSION_ID, PAYMENT_INSTRUMENT_ID,
    PROCESS_PAYMENT_ROLE_ARN    — from quickstart + e2e-test / backend
    MANAGEMENT_ROLE_ARN         — optional, enables post-payment budget readout
    SELLER_URL                  — http://localhost:8002/v1/chat/completions
"""

import argparse
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime

import boto3
import requests
from botocore.exceptions import ClientError, NoCredentialsError, TokenRetrievalError
from dotenv import load_dotenv

_ENV_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ENV_DIR, ".env"))

# ── AgentCore Configuration ──────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DP_ENDPOINT = os.environ.get(
    "DP_ENDPOINT", "https://bedrock-agentcore.us-west-2.amazonaws.com"
)
MANAGER_ARN = os.environ["MANAGER_ARN"]
PAYMENT_SESSION_ID = os.environ["PAYMENT_SESSION_ID"]
PAYMENT_INSTRUMENT_ID = os.environ["PAYMENT_INSTRUMENT_ID"]
PROCESS_PAYMENT_ROLE_ARN = os.environ["PROCESS_PAYMENT_ROLE_ARN"]
MANAGEMENT_ROLE_ARN = os.environ.get("MANAGEMENT_ROLE_ARN", "")
USER_ID = os.environ.get("USER_ID", "demo-user")

# ── Seller Configuration ─────────────────────────────────────────
SELLER_URL = os.environ.get(
    "SELLER_URL", "http://localhost:8002/v1/chat/completions"
)


def _derive_catalog_url(chat_url: str) -> str:
    """Given the chat endpoint, derive the catalog endpoint (/v1/models).

    The chat URL is typically .../v1/chat/completions — so we strip back to
    the /v1/ root and append /models. Falls back to a sibling path if the
    URL doesn't contain /v1/.
    """
    override = os.environ.get("CATALOG_URL")
    if override:
        return override
    if "/v1/" in chat_url:
        base = chat_url.split("/v1/", 1)[0]
        return f"{base}/v1/models"
    return chat_url.rsplit("/", 1)[0] + "/models"


CATALOG_URL = _derive_catalog_url(SELLER_URL)


# ── Preset on-brand prompts ──────────────────────────────────────
# Tells the "pay-per-intelligence" story with real-world use cases
# instead of "What is 1+1?" — see the AWS feedback on the v1 demo.
PRESETS: dict[str, dict] = {
    "eip": {
        "tier": "fast",
        "description": "Short EIP summary — cheap tier is enough.",
        "prompt": (
            "In exactly 3 bullet points, summarize the key goals and trade-offs "
            "of EIP-4844 (proto-danksharding) for Ethereum L2 rollups. Keep each "
            "bullet to one sentence."
        ),
    },
    "proposal": {
        "tier": "balanced",
        "description": "Summarize an L2 governance proposal — balanced tier.",
        "prompt": (
            "You are reviewing an L2 governance proposal. Summarize the proposal "
            "below for a busy voter: (1) what it changes, (2) who benefits, "
            "(3) the main risks, (4) how you would vote and why. Be concise "
            "but specific.\n\n"
            "=== PROPOSAL ===\n"
            "Title: Allocate 50M ARB from the DAO treasury to a new 'Incentives "
            "Program' managed by a 5-of-9 multisig, with a 12-month vesting "
            "cliff and monthly milestone reports published on-chain. Unspent "
            "funds at month 18 return to the treasury. The multisig signers "
            "are drawn from existing delegates with >1M ARB voting power. "
            "The program targets DeFi liquidity, gaming, and RWA projects."
        ),
    },
    "audit": {
        "tier": "premium",
        "description": "Smart contract audit — premium reasoning tier.",
        "prompt": (
            "You are a Solidity security auditor. Analyze the function below "
            "for: reentrancy, integer over/underflow, access-control bugs, "
            "checks-effects-interactions violations, and any other concrete "
            "vulnerabilities. For each issue, state the severity (High / "
            "Medium / Low), the exact line of code, and a concrete fix.\n\n"
            "=== CONTRACT ===\n"
            "pragma solidity ^0.8.0;\n"
            "contract Vault {\n"
            "    mapping(address => uint256) public balances;\n"
            "    function deposit() external payable {\n"
            "        balances[msg.sender] += msg.value;\n"
            "    }\n"
            "    function withdraw(uint256 amount) external {\n"
            "        require(balances[msg.sender] >= amount, \"low bal\");\n"
            "        (bool ok, ) = msg.sender.call{value: amount}(\"\");\n"
            "        require(ok, \"xfer failed\");\n"
            "        balances[msg.sender] -= amount;\n"
            "    }\n"
            "    function emergencyWithdraw(address to) external {\n"
            "        payable(to).transfer(address(this).balance);\n"
            "    }\n"
            "}"
        ),
    },
    "subgraph": {
        "tier": "balanced",
        "description": "Explain a subgraph schema snippet — balanced tier.",
        "prompt": (
            "Explain to a developer new to The Graph what the following "
            "subgraph schema snippet does, what it would be good at querying, "
            "and one common mistake authors make when indexing this shape "
            "of data.\n\n"
            "=== schema.graphql ===\n"
            "type Transfer @entity(immutable: true) {\n"
            "  id: Bytes!\n"
            "  from: Bytes! # address\n"
            "  to:   Bytes! # address\n"
            "  amount: BigInt!\n"
            "  block:  BigInt!\n"
            "  tx:     Bytes!\n"
            "}\n"
            "type DailyVolume @entity {\n"
            "  id: ID! # yyyy-mm-dd\n"
            "  totalAmount: BigInt!\n"
            "  transferCount: Int!\n"
            "}"
        ),
    },
}


# ── Helpers ──────────────────────────────────────────────────────


def _boto_session(profile: str | None) -> boto3.Session:
    kw = {"region_name": AWS_REGION}
    if profile:
        kw["profile_name"] = profile
    return boto3.Session(**kw)


def _assume_role(role_arn: str, session_suffix: str) -> boto3.Session:
    profile = os.environ.get("AWS_PROFILE")
    try:
        base = _boto_session(profile)
        creds = base.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"blockrun-{session_suffix}-{int(datetime.now().timestamp())}",
        )["Credentials"]
    except TokenRetrievalError as e:
        prof_hint = (
            f"aws sso login --profile {profile}"
            if profile
            else "aws sso login   # or: aws sso login --profile YOUR_PROFILE"
        )
        print(
            "\nAWS SSO token expired or could not be refreshed.\n"
            f"  {e}\n"
            "  Renew credentials, then retry:\n"
            f"    {prof_hint}\n"
            "  If you use a named profile, set AWS_PROFILE in blockrun-demo/.env.\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    except NoCredentialsError as e:
        print(
            "\nNo AWS credentials found. Configure SSO or ~/.aws/credentials "
            "and set AWS_PROFILE in .env if needed.\n"
            f"  {e}\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION,
    )


def get_dp_client():
    """Assume ProcessPaymentRole via STS and return a bedrock-agentcore client."""
    session = _assume_role(PROCESS_PAYMENT_ROLE_ARN, "buyer")
    return session.client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)


def get_management_client():
    """Assume ManagementRole for read-only session introspection (optional)."""
    if not MANAGEMENT_ROLE_ARN:
        return None
    session = _assume_role(MANAGEMENT_ROLE_ARN, "buyer-mgmt")
    return session.client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)


def fetch_catalog() -> list[dict]:
    """Fetch the seller's model catalog. Free endpoint — no payment."""
    try:
        resp = requests.get(CATALOG_URL, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"    (catalog unavailable at {CATALOG_URL}: {e})")
        return []


def pick_tier(prompt: str, catalog: list[dict], override: str | None = None) -> dict | None:
    """Route a prompt to the cheapest tier that can plausibly handle it.

    This is the "dynamically route to optimal AI model" narrative. A real
    router would use a classifier or a cheap LLM call; here we use a
    transparent keyword + length heuristic so viewers can see *why*
    each prompt lands on each tier.
    """
    if not catalog:
        return None

    by_id = {t["tier"]: t for t in catalog}

    if override:
        if override in by_id:
            return by_id[override]
        for t in catalog:
            if t["id"] == override:
                return t
        print(f"    (unknown tier '{override}', falling back to auto)")

    p = prompt.lower()
    length = len(prompt)

    PREMIUM = ("audit", "vulnerability", "reentran", "exploit", "solidity",
               "pragma ", "function ", "prove", "formal verif")
    BALANCED = ("summarize", "explain", "analy", "compare", "review",
                "proposal", "governance", "eip-", "schema", "whitepaper")

    if any(k in p for k in PREMIUM) or length > 2000:
        chosen = by_id.get("premium") or catalog[-1]
    elif any(k in p for k in BALANCED) or length > 400:
        chosen = by_id.get("balanced") or catalog[min(1, len(catalog) - 1)]
    else:
        chosen = by_id.get("fast") or catalog[0]

    return chosen


def send_chat_request(
    prompt: str,
    model: str | None = None,
    extra_headers: dict | None = None,
) -> dict:
    """POST an OpenAI-compatible chat request to the seller."""
    body: dict = {
        "messages": [{"role": "user", "content": prompt}],
    }
    if model:
        body["model"] = model
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    resp = requests.post(SELLER_URL, json=body, headers=headers, timeout=60)
    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text,
    }


def extract_x402_requirements(response: dict) -> tuple[dict, int]:
    """Extract x402 payment requirements from a 402 response.

    Supports both v2 (PAYMENT-REQUIRED header) and v1 (body) formats.
    Returns (accepts[0] payload, x402_version).
    """
    pr_header = response["headers"].get("PAYMENT-REQUIRED", "")
    if pr_header:
        info = json.loads(base64.b64decode(pr_header))
        version = info.get("x402Version", 2)
        return info["accepts"][0], version

    info = json.loads(response["body"])
    version = info.get("x402Version", 1)
    return info["accepts"][0], version


def agentcore_process_payment(
    dp_client, x402_payload: dict, x402_version: int
) -> dict:
    """Call AgentCore ProcessPayment to generate a payment proof."""
    payload = dict(x402_payload)
    if x402_version >= 2:
        for key in ["description", "mimeType", "resource", "outputSchema"]:
            payload.pop(key, None)

    return dp_client.process_payment(
        userId=USER_ID,
        paymentManagerArn=MANAGER_ARN,
        paymentSessionId=PAYMENT_SESSION_ID,
        paymentInstrumentId=PAYMENT_INSTRUMENT_ID,
        paymentType="CRYPTO_X402",
        paymentInput={
            "cryptoX402": {"version": str(x402_version), "payload": payload}
        },
        clientToken=str(uuid.uuid4()),
    )


def _payer_from_crypto_output(crypto_output: dict) -> str | None:
    """USDC `from` address in the EIP-3009 authorization (fund this on Base Sepolia)."""
    try:
        pl = crypto_output.get("payload") or crypto_output
        if isinstance(pl, dict):
            auth = pl.get("authorization") or {}
            return auth.get("from")
    except Exception:
        pass
    return None


def build_payment_header(
    x402_payload: dict, crypto_output: dict, x402_version: int
) -> tuple[str, str]:
    """Build the x402 payment header (base64-encoded) for the retry request."""
    if x402_version >= 2:
        value = {
            "x402Version": 2,
            "resource": x402_payload.get("resource", ""),
            "accepted": x402_payload,
            "payload": crypto_output.get("payload", crypto_output),
            "extension": x402_payload.get("resource", ""),
        }
        header_name = "PAYMENT-SIGNATURE"
    else:
        value = {
            "x402Version": 1,
            "scheme": x402_payload.get("scheme", "exact"),
            "network": x402_payload.get("network", "base-sepolia"),
            "payload": crypto_output.get("payload", crypto_output),
        }
        header_name = "X-PAYMENT"

    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    return header_name, encoded


def show_session_budget(label: str) -> None:
    """Print the current payment session's budget and spend.

    Requires ManagementRole — if it's not configured, emits a friendly
    hint instead of failing (the ProcessPaymentRole cannot see this).
    """
    if not MANAGEMENT_ROLE_ARN:
        print(
            f"    [{label}] (skipped — set MANAGEMENT_ROLE_ARN in .env "
            "to see budget)"
        )
        return

    try:
        mgmt = get_management_client()
        resp = mgmt.get_payment_session(
            paymentManagerArn=MANAGER_ARN,
            paymentSessionId=PAYMENT_SESSION_ID,
            userId=USER_ID,
        )
        sess = resp.get("paymentSession", {})
        status = sess.get("status", "?")
        limits = sess.get("limits", {}) or {}
        max_spend = (limits.get("maxSpendAmount", {}) or {}).get("value", "?")
        currency = (limits.get("maxSpendAmount", {}) or {}).get("currency", "USD")
        spent = sess.get("currentSpendAmount", {}) or {}
        spent_val = spent.get("value", "0")
        try:
            remaining = float(max_spend) - float(spent_val)
            remaining_str = f"{remaining:.6f}"
        except (TypeError, ValueError):
            remaining_str = "?"

        print(f"    [{label}] session  : {PAYMENT_SESSION_ID[:16]}...")
        print(f"    [{label}] status   : {status}")
        print(f"    [{label}] budget   : {max_spend} {currency}")
        print(f"    [{label}] spent    : {spent_val} {currency}")
        print(f"    [{label}] remaining: {remaining_str} {currency}")
    except Exception as e:
        print(f"    [{label}] (GetPaymentSession failed: {e})")


# ── Main Demo Flow ───────────────────────────────────────────────


def run_demo(prompt: str, tier_override: str | None = None) -> None:
    """Run the full AgentCore -> Ampersend -> BlockRun payment flow."""
    print()
    print("=" * 60)
    print("  AgentCore Payments -> Ampersend x402 -> BlockRun LLM")
    print("=" * 60)
    print(f"  Seller : {SELLER_URL}")
    preview = prompt if len(prompt) <= 80 else prompt[:77] + "..."
    print(f"  Prompt : {preview}")
    print("=" * 60)

    # ── [0] Assume ProcessPaymentRole ────────────────────────────
    print("\n[0] Assuming AgentCore ProcessPaymentRole...")
    dp_client = get_dp_client()
    print(f"    Role: {PROCESS_PAYMENT_ROLE_ARN.split('/')[-1]}")

    # ── [1] Fetch catalog and pick a tier ────────────────────────
    print(f"\n[1] Fetching model catalog: GET {CATALOG_URL}")
    catalog = fetch_catalog()
    for t in catalog:
        price = t.get("x402", {}).get("price_usdc", "?")
        print(f"    - [{t.get('tier'):>8}] ${price}  {t.get('id')}")

    tier = pick_tier(prompt, catalog, tier_override)
    if tier:
        how = "forced" if tier_override else "auto-routed"
        price = tier.get("x402", {}).get("price_usdc", "?")
        print(
            f"\n    {how} -> tier={tier['tier']} "
            f"model={tier['id']} price=${price}"
        )
        target_model = tier["id"]
    else:
        print("\n    (no catalog — letting the seller choose a default tier)")
        target_model = None

    # ── [2] Initial request → 402 ────────────────────────────────
    print(f"\n[2] POST {SELLER_URL}")
    resp = send_chat_request(prompt, model=target_model)
    print(f"    HTTP {resp['status_code']}")

    if resp["status_code"] != 402:
        if resp["status_code"] == 200:
            print("    (No payment required — endpoint is free)")
            _print_response(resp["body"])
            return
        print(f"    Unexpected response: {resp['body'][:200]}")
        return

    x402_payload, x402_version = extract_x402_requirements(resp)
    amount_raw = x402_payload.get("maxAmountRequired") or x402_payload.get(
        "amount", "0"
    )
    amount_usdc = int(amount_raw) / 1_000_000
    network = x402_payload.get("network", "?")
    pay_to = x402_payload.get("payTo", "?")
    print(f"    Payment required: ${amount_usdc:.4f} USDC on {network}")
    print(
        f"    Pay to: {pay_to[:10]}...{pay_to[-6:]}"
        if len(pay_to) > 16
        else f"    Pay to: {pay_to}"
    )

    # ── [3] AgentCore ProcessPayment ─────────────────────────────
    print(f"\n[3] AgentCore ProcessPayment (x402 v{x402_version})...")
    print(f"    Manager : {MANAGER_ARN.split('/')[-1]}")
    print(f"    Session : {PAYMENT_SESSION_ID[:16]}...")
    try:
        pay_result = agentcore_process_payment(dp_client, x402_payload, x402_version)
    except ClientError as e:
        err = e.response.get("Error", {}) if e.response else {}
        code = err.get("Code", "")
        msg = err.get("Message", str(e))
        print(f"    ProcessPayment failed ({code}): {msg}")
        if code == "ValidationException" and (
            "session" in msg.lower() or "instrument" in msg.lower()
        ):
            print(
                "    Your .env IDs don’t match an active session for this manager/user. "
                "Re-run `scripts/e2e-test.sh` — when it passes, copy the printed "
                "PAYMENT_SESSION_ID / PAYMENT_INSTRUMENT_ID / USER_ID lines into "
                "blockrun-demo/.env (each e2e run creates new IDs)."
            )
            return
        raise
    pay_result.pop("ResponseMetadata", None)
    status = pay_result.get("status", "UNKNOWN")
    print(f"    ProcessPayment -> {status}")

    if status != "PROOF_GENERATED":
        print(f"    Payment failed: {json.dumps(pay_result, indent=2)[:500]}")
        return

    crypto_output = pay_result["paymentOutput"]["cryptoX402"]
    print("    Payment proof generated")
    payer_addr = _payer_from_crypto_output(crypto_output)
    if payer_addr:
        print(f"    Payer (USDC on Base Sepolia): {payer_addr}")
        print(
            "    If the seller returns HTTP 402 *for your payment proof*, this wallet may need "
            f"≥ ${amount_usdc:.4f} USDC — https://faucet.circle.com/"
            "\n    If you see HTTP 502 instead, that is BlockRun/Ampersend (seller→upstream), "
            "not a signal to fund this payer address."
        )

    # ── [4] Retry with proof → LLM response ──────────────────────
    header_name, header_value = build_payment_header(
        x402_payload, crypto_output, x402_version
    )
    print(f"\n[4] Retrying with {header_name} header...")

    max_attempts = 3
    result = None
    for attempt in range(1, max_attempts + 1):
        print(f"    Attempt {attempt}/{max_attempts}...")
        result = send_chat_request(
            prompt, model=target_model, extra_headers={header_name: header_value}
        )
        if result["status_code"] != 402:
            break
        if attempt == 1:
            ph = payer_addr or "(see seller log)"
            print(
                "\n    Still HTTP 402 from seller — usually means *our* payment proof was rejected "
                "(facilitator /settle failed or malformed proof). Check seller terminal."
                f"\n    Payer: {ph} — only fund if settlement failed with insufficient_balance."
                "\n    (If BlockRun/Ampersend fails, the seller now returns HTTP 502 instead — not 402.)"
            )
        if attempt < max_attempts:
            time.sleep(2)

    if result is None:
        print("    No response received")
        return

    print(f"    HTTP {result['status_code']}")

    if result["status_code"] == 200:
        _print_response(result["body"])
        # ── [5] Post-payment budget readout ──────────────────────
        print("\n[5] GetPaymentSession (guardrails) — ManagementRole read-only:")
        show_session_budget("after")
        print("\nDone: AgentCore -> Ampersend -> BlockRun end-to-end")
    elif result["status_code"] == 502:
        print(
            "\n    HTTP 502 — upstream (BlockRun / Ampersend) error after your payment was accepted."
            "\n    This is not a 'fund the buyer wallet' issue. See seller terminal BlockRun debug block."
        )
        print(f"    Body: {result['body'][:1200]}")
    else:
        print(f"    Error: {result['body'][:500]}")


def _print_response(body: str) -> None:
    """Pretty-print an OpenAI-compatible chat response."""
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        model = data.get("model", "?")
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", "?")
        print(f"\n{'─' * 60}")
        print(f"  Model  : {model}")
        print(f"  Tokens : {tokens}")
        print(f"{'─' * 60}")
        print(f"  {content}")
        print(f"{'─' * 60}")
    except Exception:
        print(f"  Response: {body[:500]}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AgentCore Payments -> Ampersend -> BlockRun buyer demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Preset prompts (--example):\n"
            + "\n".join(
                f"  {k:<10} [{v['tier']:>8}]  {v['description']}"
                for k, v in PRESETS.items()
            )
        ),
    )
    parser.add_argument(
        "prompt", nargs="*", help="Prompt to send (omit for interactive mode)"
    )
    parser.add_argument(
        "--example",
        choices=list(PRESETS.keys()),
        help="Use a canned on-brand prompt from the preset library",
    )
    parser.add_argument(
        "--tier",
        choices=["fast", "balanced", "premium"],
        help="Force a specific tier (default: auto-route by prompt complexity)",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.example:
        preset = PRESETS[args.example]
        tier = args.tier or preset["tier"]
        print(f"(preset: {args.example} — tier={tier})")
        run_demo(preset["prompt"], tier_override=tier)
        return

    if args.prompt:
        prompt = " ".join(args.prompt)
        run_demo(prompt, tier_override=args.tier)
        return

    # Interactive mode
    print("AgentCore Payments -> Ampersend -> BlockRun Demo")
    print("Type a prompt, or one of: " + ", ".join(f"/{k}" for k in PRESETS))
    print("Type 'quit' to exit.\n")
    while True:
        try:
            prompt = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not prompt or prompt.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        tier_override = args.tier
        if prompt.startswith("/"):
            key = prompt[1:].split()[0]
            if key in PRESETS:
                preset = PRESETS[key]
                tier_override = args.tier or preset["tier"]
                prompt = preset["prompt"]
                print(f"(preset: {key} — tier={tier_override})")
            else:
                print(f"Unknown preset '{key}'. Available: {', '.join(PRESETS)}")
                continue

        run_demo(prompt, tier_override=tier_override)
        print()


if __name__ == "__main__":
    main()
