#!/usr/bin/env python3
"""
x402_agentcore_test.py — Coinbase x402 MCP client + AgentCore Payments

Validates that the official Coinbase x402 Python client can use AgentCore
Payments as the signing backend instead of a local private key.

We implement:
1. AgentCoreClientScheme — a SchemeNetworkClient that calls ProcessPayment
2. SimpleMCPClient — a minimal sync MCP client for the Bazaar endpoint

Then plug both into x402MCPClientSync for automatic payment handling.

Usage:
    bazaar-test/.venv312/bin/python3.12 bazaar-test/x402_agentcore_test.py
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import boto3
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from x402.client_base import PaymentRequirements
from x402.client import x402ClientSync
from x402.mcp.client import x402MCPClientSync, MCPToolCallResult

# ── Config ───────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DP_ENDPOINT = os.environ.get("DP_ENDPOINT")
MANAGER_ARN = os.environ["MANAGER_ARN"]
SESSION_ID = os.environ["PAYMENT_SESSION_ID"]
INSTRUMENT_ID = os.environ["PAYMENT_INSTRUMENT_ID"]
PAY_ROLE_ARN = os.environ["PROCESS_PAYMENT_ROLE_ARN"]
USER_ID = os.environ.get("USER_ID", "test-user-bazaar")
BAZAAR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/mcp"


# ═══════════════════════════════════════════════════════════════
# 1. AgentCore ClientScheme adapter
# ═══════════════════════════════════════════════════════════════
class AgentCoreClientScheme:
    """x402 SchemeNetworkClient backed by AgentCore ProcessPayment."""

    scheme = "exact"

    def __init__(self, dp_client, manager_arn, session_id, instrument_id, user_id):
        self._dp = dp_client
        self._manager_arn = manager_arn
        self._sess = session_id
        self._inst = instrument_id
        self._uid = user_id

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        payload = {
            "scheme": requirements.scheme,
            "network": requirements.network,
            "amount": str(requirements.amount),
            "maxAmountRequired": str(requirements.amount),
            "asset": requirements.asset,
            "payTo": requirements.pay_to,
            "maxTimeoutSeconds": requirements.max_timeout_seconds,
            "extra": requirements.extra or {"name": "USDC", "version": "2"},
        }

        print(f"    🔗 AgentCore ProcessPayment → {requirements.network}")
        resp = self._dp.process_payment(
            userId=self._uid,
            paymentManagerArn=self._manager_arn,
            paymentSessionId=self._sess,
            paymentInstrumentId=self._inst,
            paymentType="CRYPTO_X402",
            paymentInput={"cryptoX402": {"version": "2", "payload": payload}},
            clientToken=str(uuid.uuid4()),
        )
        status = resp.get("status", "UNKNOWN")
        print(f"    💳 ProcessPayment → {status}")
        if status != "PROOF_GENERATED":
            raise Exception(f"ProcessPayment failed: {status}")
        return resp["paymentOutput"]["cryptoX402"]["payload"]


# ═══════════════════════════════════════════════════════════════
# 2. Minimal sync MCP client for the Bazaar
# ═══════════════════════════════════════════════════════════════
@dataclass
class MCPContent:
    type: str = "text"
    text: str = ""

@dataclass
class MCPResult:
    content: list = field(default_factory=list)
    isError: bool = False
    structuredContent: dict = field(default_factory=dict)
    _meta: dict = field(default_factory=dict)

class SimpleMCPClient:
    """Minimal sync MCP client that talks JSON-RPC to the Bazaar."""

    def __init__(self, url: str):
        self._url = url
        self._session_id = None
        self._headers = {"Content-Type": "application/json", "Accept": "application/json"}
        self._initialize()

    def _initialize(self):
        resp = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "x402-agentcore-test", "version": "0.1.0"},
        })
        self._rpc("notifications/initialized")

    def _rpc(self, method, params=None):
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params:
            payload["params"] = params
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = requests.post(self._url, headers=headers, json=payload, timeout=30)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        return resp.json() if resp.status_code == 200 else {"error": resp.text}

    def call_tool(self, params, **kwargs):
        """Called by x402MCPClientSync. params = {"name": ..., "arguments": ...}"""
        body = self._rpc("tools/call", params)
        result = body.get("result", {})
        content = result.get("content", [])
        is_error = result.get("isError", False)
        structured = result.get("structuredContent", {})
        meta = result.get("_meta", {})

        # Normalize Bazaar's camelCase to snake_case for x402 client compatibility
        if structured and "accepts" in structured:
            for acc in structured["accepts"]:
                if "maxAmountRequired" in acc and "amount" not in acc:
                    acc["amount"] = acc["maxAmountRequired"]
                if "payTo" in acc and "pay_to" not in acc:
                    acc["pay_to"] = acc["payTo"]
                if "maxTimeoutSeconds" in acc and "max_timeout_seconds" not in acc:
                    acc["max_timeout_seconds"] = acc["maxTimeoutSeconds"]

        content_items = []
        for c in content:
            content_items.append({"type": c.get("type", "text"), "text": c.get("text", "")})

        return type("MCPResult", (), {
            "content": content_items,
            "isError": is_error,
            "is_error": is_error,
            "structuredContent": structured if structured else None,
            "_meta": meta,
        })()


# ═══════════════════════════════════════════════════════════════
# 3. Setup helpers
# ═══════════════════════════════════════════════════════════════
def get_dp_client():
    profile = os.environ.get("AWS_PROFILE")
    kw = {"region_name": AWS_REGION}
    if profile:
        kw["profile_name"] = profile
    session = boto3.Session(**kw)
    creds = session.client("sts").assume_role(
        RoleArn=PAY_ROLE_ARN,
        RoleSessionName=f"x402-{int(datetime.now().timestamp())}",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION,
    ).client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)


# ═══════════════════════════════════════════════════════════════
# 4. Main test
# ═══════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print("  x402 MCP Client + AgentCore Payments Test")
    print("=" * 60)

    # Setup AgentCore
    print("\n[1/4] AgentCore ProcessPayment client...")
    dp = get_dp_client()
    scheme = AgentCoreClientScheme(dp, MANAGER_ARN, SESSION_ID, INSTRUMENT_ID, USER_ID)
    print("  ✅ Ready")

    # Setup x402 client with AgentCore signer
    print("\n[2/4] x402 client with AgentCore signer...")
    x402 = x402ClientSync()
    x402.register("eip155:8453", scheme)
    x402.register("eip155:84532", scheme)
    x402.register("base", scheme)
    x402.register("base-sepolia", scheme)  # legacy testnet name
    # Also register v1 schemes
    x402.register_v1("eip155:8453", scheme)
    x402.register_v1("eip155:84532", scheme)
    x402.register_v1("base", scheme)
    x402.register_v1("base-sepolia", scheme)
    print("  ✅ Registered for Base Mainnet + Base Sepolia (v1 + v2)")

    # Connect to Bazaar
    print("\n[3/4] Connecting to Bazaar + discovering tools...")
    mcp = SimpleMCPClient(BAZAAR_URL)
    x402_mcp = x402MCPClientSync(mcp, x402, auto_payment=True)
    print("  ✅ Connected")

    # Discover tools
    discovery = x402_mcp.call_tool("search_resources", {"query": "", "network": "eip155:8453"})
    if discovery.content:
        first = discovery.content[0]
        text = first.get("text", str(first)) if isinstance(first, dict) else (first.text if hasattr(first, 'text') else str(first))
        parsed = json.loads(text)
        tools = parsed.get("tools", [])
        print(f"  Found {len(tools)} tools")

        def cost(t):
            a = t.get("_meta", {}).get("x402/payment-required", {}).get("accepts", [{}])
            return int((a[0].get("maxAmountRequired") or a[0].get("amount", "0")) if a else "0")

        sorted_t = sorted(tools, key=cost)
        # Pick nickeljoke if available (known to work), otherwise first cheap tool
        target_name = None
        for t in sorted_t:
            if "nickeljoke" in t["name"]:
                target_name = t["name"]
                break
        if not target_name:
            target_name = sorted_t[0]["name"]
        for t in sorted_t[:5]:
            marker = " ←" if t["name"] == target_name else ""
            print(f"    - {t['name'][:55]} (${cost(t)/1_000_000:.4f}){marker}")
    else:
        print("  No tools found")
        return

    # Call a paid tool — x402 client handles 402 → AgentCore → retry
    target = target_name
    print(f"\n[4/4] Calling paid tool via x402 auto-payment...")
    print(f"  Tool: {target}")
    print(f"  (x402 client intercepts 402 → calls AgentCoreClientScheme → retries)")

    result = x402_mcp.call_tool("proxy_tool_call", {"toolName": target, "parameters": {}})

    print(f"\n  Content:")
    if result.content:
        for c in result.content:
            if isinstance(c, dict):
                t = c.get("text", str(c))
            elif hasattr(c, 'text'):
                t = c.text
            else:
                t = str(c)
            # Try to parse and show the body
            try:
                j = json.loads(t)
                body = j.get("body", t)
                print(f"    {body[:200]}")
            except:
                print(f"    {t[:200]}")

    print(f"  Payment made: {result.payment_made}")
    if result.payment_response:
        pr = result.payment_response
        tx = getattr(pr, 'transaction', None) or (pr.get('transaction') if isinstance(pr, dict) else 'N/A')
        print(f"  Transaction: {tx}")

    print(f"\n{'=' * 60}")
    if result.payment_made:
        print("  ✅ SUCCESS — x402 client + AgentCore Payments works end-to-end")
    elif result.content and not result.is_error:
        print("  ✅ Tool returned content (may have been free)")
    else:
        print("  ❌ FAILED — check output above")
    print("=" * 60)


if __name__ == "__main__":
    main()
