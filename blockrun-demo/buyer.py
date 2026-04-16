#!/usr/bin/env python3
"""
AgentCore Payments Buyer — calls a paid LLM via x402.

This script follows the **Agent Execution (ProcessPaymentRole)** pattern from the
AgentCore Payments Private Preview guide:

  • Assumes **ProcessPaymentRole** (can only call **ProcessPayment** — not create
    sessions or instruments).
  • Uses **paymentSessionId** + **paymentInstrumentId** + **paymentManagerArn**
    supplied by the application backend (here: from `.env`, typically produced by
    ManagementRole via CreatePaymentSession / instrument provisioning).
  • On HTTP 402, passes the merchant **accepts[0]** payload to **process_payment**
    as **cryptoX402** (v1 or v2), then retries the HTTP request with **X-PAYMENT**
    or **PAYMENT-SIGNATURE** — same flow as `http_request` → `process_payment` →
    `http_request_with_payment_header` in ../strands-agent/agent.py.

Demonstrates step by step:
    [0] Assume AgentCore ProcessPaymentRole
    [1] POST to seller -> HTTP 402 (payment required)
    [2] AgentCore ProcessPayment -> payment proof (status PROOF_GENERATED)
    [3] Retry with proof -> LLM response from BlockRun

Usage:
    python buyer.py "What is the capital of France?"
    python buyer.py                                    # interactive mode

For an LLM-driven agent with the same payment tools, use ../strands-agent/agent.py
and point HTTP tools at the seller URL.

Environment:
    MANAGER_ARN, PAYMENT_SESSION_ID, PAYMENT_INSTRUMENT_ID,
    PROCESS_PAYMENT_ROLE_ARN — from quickstart + e2e-test / backend
    SELLER_URL — http://localhost:8002/v1/chat/completions (default)
"""

import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime

import boto3
import requests
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
USER_ID = os.environ.get("USER_ID", "demo-user")

# ── Seller Configuration ─────────────────────────────────────────
SELLER_URL = os.environ.get(
    "SELLER_URL", "http://localhost:8002/v1/chat/completions"
)
MODEL = os.environ.get("BLOCKRUN_MODEL", "openai/gpt-oss-20b")


# ── Helpers ──────────────────────────────────────────────────────


def get_dp_client():
    """Assume ProcessPaymentRole via STS and return a bedrock-agentcore client."""
    profile = os.environ.get("AWS_PROFILE")
    kw = {"region_name": AWS_REGION}
    if profile:
        kw["profile_name"] = profile

    session = boto3.Session(**kw)
    creds = session.client("sts").assume_role(
        RoleArn=PROCESS_PAYMENT_ROLE_ARN,
        RoleSessionName=f"blockrun-buyer-{int(datetime.now().timestamp())}",
    )["Credentials"]

    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION,
    ).client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)


def send_chat_request(
    prompt: str, extra_headers: dict | None = None
) -> dict:
    """POST an OpenAI-compatible chat request to the seller."""
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    resp = requests.post(SELLER_URL, json=body, headers=headers, timeout=30)
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
    """Call AgentCore ProcessPayment to generate a payment proof.

    Passes the merchant's x402 payload through to AgentCore, stripping
    metadata fields for v2.
    """
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


# ── Main Demo Flow ───────────────────────────────────────────────


def run_demo(prompt: str):
    """Run the full AgentCore -> Ampersend -> BlockRun payment flow."""
    print()
    print("=" * 60)
    print("  AgentCore Payments -> Ampersend x402 -> BlockRun LLM")
    print("=" * 60)
    print(f"  Seller : {SELLER_URL}")
    print(f"  Model  : {MODEL}")
    print(f"  Prompt : {prompt[:50]}{'...' if len(prompt) > 50 else ''}")
    print("=" * 60)

    # ── [0] Assume ProcessPaymentRole ────────────────────────────
    print("\n[0] Assuming AgentCore ProcessPaymentRole...")
    dp_client = get_dp_client()
    print(f"    Role: {PROCESS_PAYMENT_ROLE_ARN.split('/')[-1]}")

    # ── [1] Initial request → 402 ───────────────────────────────
    print(f"\n[1] POST {SELLER_URL}")
    resp = send_chat_request(prompt)
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
    print(f"    Pay to: {pay_to[:10]}...{pay_to[-6:]}" if len(pay_to) > 16 else f"    Pay to: {pay_to}")

    # ── [2] AgentCore ProcessPayment ─────────────────────────────
    print(f"\n[2] AgentCore ProcessPayment (x402 v{x402_version})...")
    print(f"    Manager : {MANAGER_ARN.split('/')[-1]}")
    print(f"    Session : {PAYMENT_SESSION_ID[:16]}...")
    pay_result = agentcore_process_payment(dp_client, x402_payload, x402_version)
    pay_result.pop("ResponseMetadata", None)
    status = pay_result.get("status", "UNKNOWN")
    print(f"    ProcessPayment -> {status}")

    if status != "PROOF_GENERATED":
        print(f"    Payment failed: {json.dumps(pay_result, indent=2)[:500]}")
        return

    crypto_output = pay_result["paymentOutput"]["cryptoX402"]
    print("    Payment proof generated")

    # ── [3] Retry with proof → LLM response ─────────────────────
    header_name, header_value = build_payment_header(
        x402_payload, crypto_output, x402_version
    )
    print(f"\n[3] Retrying with {header_name} header...")

    max_attempts = 6
    result = None
    for attempt in range(1, max_attempts + 1):
        print(f"    Attempt {attempt}/{max_attempts}...")
        result = send_chat_request(prompt, {header_name: header_value})
        if result["status_code"] != 402:
            break
        if attempt < max_attempts:
            wait = 2 * attempt
            print(f"    Settlement pending, waiting {wait}s...")
            time.sleep(wait)

    if result is None:
        print("    No response received")
        return

    print(f"    HTTP {result['status_code']}")

    if result["status_code"] == 200:
        _print_response(result["body"])
        print("\nDone: AgentCore -> Ampersend -> BlockRun end-to-end")
    else:
        print(f"    Error: {result['body'][:500]}")


def _print_response(body: str):
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


def main():
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        run_demo(prompt)
    else:
        print("AgentCore Payments -> Ampersend -> BlockRun Demo")
        print("Type a prompt or 'quit' to exit.\n")
        while True:
            try:
                prompt = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if not prompt or prompt.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break
            run_demo(prompt)
            print()


if __name__ == "__main__":
    main()
