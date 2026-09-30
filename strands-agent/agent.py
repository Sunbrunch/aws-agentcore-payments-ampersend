#!/usr/bin/env python3
"""
AgentCore Payments — Strands Agent

A Strands-based AI agent that autonomously handles x402 payments using
Amazon Bedrock AgentCore Payments. The agent runs under ProcessPaymentRole
and can ONLY execute payments within the budget set by the application backend.

Flow:
  1. App backend (ManagementRole) creates instrument + session with budget
  2. App backend passes sessionId + instrumentId to the agent
  3. Agent encounters paid endpoints (HTTP 402) and pays automatically
  4. Agent can only spend within the session budget — no escalation possible

Usage:
  cp .env.sample .env   # fill in values
  pip install -r requirements.txt
  python agent.py
"""

import json
import os
import sys
import uuid
from datetime import datetime

import boto3
import requests
from dotenv import load_dotenv
from strands import Agent, tool

load_dotenv()

# ── Configuration ────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
DP_ENDPOINT = os.environ.get("DP_ENDPOINT", "https://bedrock-agentcore.us-west-2.amazonaws.com")
MANAGER_ARN = os.environ["MANAGER_ARN"]
PAYMENT_SESSION_ID = os.environ["PAYMENT_SESSION_ID"]
PAYMENT_INSTRUMENT_ID = os.environ["PAYMENT_INSTRUMENT_ID"]
PROCESS_PAYMENT_ROLE_ARN = os.environ["PROCESS_PAYMENT_ROLE_ARN"]
USER_ID = os.environ.get("USER_ID", "test-user-12345")
PAY_TO = os.environ.get("PAY_TO", "0x312554704B5c47b992e876639B144e9B85431E44")


def get_process_payment_client():
    """Assume ProcessPaymentRole via STS and return a boto3 bedrock-agentcore client.

    The SDK handles SigV4 signing automatically. We just need to assume the
    role and create a client with those credentials.
    """
    profile = os.environ.get("AWS_PROFILE")
    session_kwargs = {"region_name": AWS_REGION}
    if profile:
        session_kwargs["profile_name"] = profile

    session = boto3.Session(**session_kwargs)
    sts = session.client("sts")

    resp = sts.assume_role(
        RoleArn=PROCESS_PAYMENT_ROLE_ARN,
        RoleSessionName=f"strands-agent-{int(datetime.now().timestamp())}",
    )
    creds = resp["Credentials"]
    agent_session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION,
    )
    return agent_session.client("bedrock-agentcore", endpoint_url=DP_ENDPOINT)


# ── Assume ProcessPaymentRole at startup ─────────────────────────
print(f"Assuming ProcessPaymentRole: {PROCESS_PAYMENT_ROLE_ARN}")
_dp_client = get_process_payment_client()
print("✅ Role assumed successfully\n")

# ── Internal state: last payment context ─────────────────────────
_last_payment_context: dict = {}


def _call_process_payment(x402_payload: dict, x402_version: int = 1) -> dict:
    """Internal helper: call ProcessPayment API via boto3 SDK.

    The x402_payload is the merchant's payment requirement object (typically
    accepts[0] from the 402 response). For v1 it is passed through as-is. For
    v2, metadata keys that belong on PaymentPayload.resource (resource,
    description, mimeType, outputSchema) are omitted from the DP payload so
    the signed proof matches the facilitator and the PAYMENT-SIGNATURE body.

    The ``version`` field in the ProcessPayment request matches the x402
    protocol version: "1" for v1, "2" for v2.
    """
    payload = dict(x402_payload)
    if x402_version >= 2:
        for key in ("description", "mimeType", "resource", "outputSchema"):
            payload.pop(key, None)

    response = _dp_client.process_payment(
        userId=USER_ID,
        paymentManagerArn=MANAGER_ARN,
        paymentSessionId=PAYMENT_SESSION_ID,
        paymentInstrumentId=PAYMENT_INSTRUMENT_ID,
        paymentType="CRYPTO_X402",
        paymentInput={
            "cryptoX402": {
                "version": str(x402_version),
                "payload": payload,
            }
        },
        clientToken=str(uuid.uuid4()),
    )
    response.pop("ResponseMetadata", None)
    print(f"  💳 ProcessPayment version={x402_version}, status={response.get('status', 'UNKNOWN')}")
    return response


# ── Tools ────────────────────────────────────────────────────────

@tool
def process_payment(x402_payload: dict, x402_version: int = 1) -> dict:
    """Execute an x402 crypto payment via AgentCore Payments ProcessPayment API.

    Pass the ENTIRE x402 payment requirement object from the merchant as-is.
    This is typically accepts[0] from the HTTP 402 response. Do NOT parse
    individual fields — the API accepts the raw merchant payload directly.

    Args:
        x402_payload: The raw x402 payment requirement from the merchant.
        x402_version: The x402 protocol version (1 or 2). Use the x402_version
            returned by http_request. Defaults to 1.

    Returns:
        The ProcessPayment API response including payment proof if successful.
    """
    result = _call_process_payment(x402_payload, x402_version=x402_version)

    status = result.get("status", "UNKNOWN")
    print(f"  💳 ProcessPayment → {status}")

    # Store proof internally so http_request_with_payment_header can use it
    if status == "PROOF_GENERATED":
        crypto_output = result.get("paymentOutput", {}).get("cryptoX402", {})
        _last_payment_context["proof"] = crypto_output
        print(f"  ✅ Payment proof stored")

    return result


@tool
def http_request(url: str, method: str = "POST", headers: dict = None, body: str = None) -> dict:
    """Make an HTTP request to any URL. If the response is HTTP 402 (Payment Required),
    the x402 payment details are extracted and returned so you can pay with process_payment.

    Supports both x402 v1 (body-based) and v2 (PAYMENT-REQUIRED header) formats.

    Args:
        url: The URL to request.
        method: HTTP method (GET, POST, etc.). Defaults to POST.
        headers: Optional dict of HTTP headers.
        body: Optional request body string.

    Returns:
        A dict with status_code, headers, body, and optionally x402_payment_details
        if the endpoint requires payment. When a 402 is detected, the full accepts[0]
        object is returned as x402_payload — pass it directly to process_payment.
    """
    import base64 as b64

    req_headers = headers or {}
    resp = requests.request(method, url, headers=req_headers, data=body, timeout=30)

    result = {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text[:4000],
    }

    # Detect x402 Payment Required
    if resp.status_code == 402:
        print(f"  🔒 HTTP 402 — Payment required for {url}")

        payment_info = None
        x402_version = 1

        # v2: payment details in PAYMENT-REQUIRED header (base64-encoded JSON)
        pr_header = resp.headers.get("PAYMENT-REQUIRED", "")
        if pr_header:
            try:
                payment_info = json.loads(b64.b64decode(pr_header))
                x402_version = payment_info.get("x402Version", 2)
            except Exception:
                pass

        # v1 fallback: payment details in response body
        if not payment_info:
            try:
                payment_info = resp.json()
                x402_version = payment_info.get("x402Version", 1)
            except Exception:
                payment_info = {"raw": resp.text[:2000]}

        result["x402_payment_details"] = payment_info
        result["x402_version"] = x402_version

        # Extract the full accepts[0] as the payload to pass to process_payment
        accepts = payment_info.get("accepts", [])
        if accepts:
            x402_payload = accepts[0]

            # The merchant's facilitator validates the proof against the
            # payment requirement registered with GET.  When we POST, the
            # merchant mirrors "method":"POST" in outputSchema, but the
            # facilitator expects "method":"GET".  Re-fetch with GET to get
            # the canonical payment requirement if we originally used POST.
            if method.upper() != "GET":
                try:
                    get_resp = requests.get(url, headers=req_headers, timeout=30)
                    if get_resp.status_code == 402:
                        get_info = None
                        get_pr = get_resp.headers.get("PAYMENT-REQUIRED", "")
                        if get_pr:
                            try:
                                get_info = json.loads(b64.b64decode(get_pr))
                            except Exception:
                                pass
                        if not get_info:
                            try:
                                get_info = get_resp.json()
                            except Exception:
                                pass
                        if get_info:
                            get_accepts = get_info.get("accepts", [])
                            if get_accepts:
                                x402_payload = get_accepts[0]
                                payment_info = get_info
                                print(f"  🔄 Re-fetched payment requirements via GET (canonical outputSchema)")
                except Exception:
                    pass  # Fall back to original payload

            result["x402_payload"] = x402_payload
            if x402_version >= 2:
                result["accepted_requirements"] = x402_payload

            # Store 402 context for http_request_with_payment_header
            # Retry the documented method; a GET-only merchant rejects POST.
            _last_payment_context.update({
                "url": url,
                "method": method.upper(),
                "x402_version": x402_version,
                "x402_payload": x402_payload,
                "resource": payment_info.get("resource"),
                "extensions": payment_info.get("extensions", {}),
            })

    return result


@tool
def http_request_with_payment_header(
    url: str,
    method: str = None,
    headers: dict = None,
    body: str = None,
) -> dict:
    """Retry an HTTP request with the x402 payment proof from the last process_payment call.

    The payment proof and HTTP method are automatically retrieved from internal
    state — you do NOT need to pass them manually. Just pass the URL.

    The tool retries with backoff if the merchant still returns 402 (waiting
    for on-chain transaction settlement).

    For v1: sends X-PAYMENT header.
    For v2: sends PAYMENT-SIGNATURE header with accepted requirements.

    Args:
        url: The URL to request (same as the one that returned 402).
        method: HTTP method override. If not specified, uses the method from the original request.
        headers: Optional additional headers.
        body: Optional request body.

    Returns:
        The response from the paid endpoint.
    """
    import base64 as b64
    import time

    ctx = _last_payment_context
    proof = ctx.get("proof")
    if not proof:
        return {"error": "No payment proof available. Call process_payment first."}

    # Use stored method if not explicitly provided
    if method is None:
        method = ctx.get("method", "POST")
    method = method.upper()

    x402_version = ctx.get("x402_version", 1)
    x402_payload = ctx.get("x402_payload", {})

    req_headers = headers or {}

    if x402_version >= 2 and x402_payload:
        # x402 v2 PaymentPayload: ResourceInfo on ``resource``; ``accepted`` without
        # resource/description/mimeType/outputSchema (see coinbase/x402 spec).
        resource = ctx.get("resource") or {"url": url, "description": "", "mimeType": "application/json"}
        accepted = {
            k: v
            for k, v in x402_payload.items()
            if k not in ("description", "mimeType", "outputSchema", "resource")
        }
        payment_signature = {
            "x402Version": 2,
            "resource": resource,
            "accepted": accepted,
            "payload": proof.get("payload", proof),
            "extensions": ctx.get("extensions", {}),
        }
        encoded = b64.b64encode(json.dumps(payment_signature).encode()).decode()
        req_headers["PAYMENT-SIGNATURE"] = encoded
        header_name = "PAYMENT-SIGNATURE"
    else:
        # v1: X-PAYMENT header must include x402Version, scheme, and network
        # at the top level alongside the payload (authorization + signature).
        # The facilitator needs these to know how to validate the proof.
        x_payment = {
            "x402Version": 1,
            "scheme": x402_payload.get("scheme", "exact"),
            "network": x402_payload.get("network", "base"),
            "payload": proof.get("payload", proof),
        }
        encoded = b64.b64encode(json.dumps(x_payment).encode()).decode()
        req_headers["X-PAYMENT"] = encoded
        header_name = "X-PAYMENT"

    # Retry with backoff — merchant returns 402 until on-chain tx settles
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        print(f"  🔄 Attempt {attempt}/{max_attempts}: {method} {url} (x402 v{x402_version}, {header_name})")
        resp = requests.request(method, url, headers=req_headers, data=body, timeout=30)
        print(f"  📥 Response: {resp.status_code}")

        if resp.status_code != 402:
            break

        if attempt < max_attempts:
            wait = 2 * attempt
            print(f"  ⏳ Transaction pending — waiting {wait}s...")
            time.sleep(wait)

    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": resp.text[:4000],
    }


BAZAAR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/mcp"

# MCP session state for Bazaar connection
_mcp_session_id: str | None = None


def _mcp_request(method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC request to the Bazaar MCP endpoint."""
    global _mcp_session_id

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
    }
    if params:
        payload["params"] = params

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if _mcp_session_id:
        headers["Mcp-Session-Id"] = _mcp_session_id

    resp = requests.post(BAZAAR_URL, headers=headers, json=payload, timeout=30)

    sid = resp.headers.get("Mcp-Session-Id")
    if sid:
        _mcp_session_id = sid

    return {"status_code": resp.status_code, "headers": dict(resp.headers), "body": resp.json() if resp.status_code == 200 else resp.text}


@tool
def connect_to_bazaar() -> dict:
    """Initialize a connection to the Coinbase Bazaar MCP marketplace.
    Must be called before discover_bazaar_tools or call_bazaar_tool.

    Returns:
        Connection status and server capabilities.
    """
    global _mcp_session_id
    _mcp_session_id = None

    init_resp = _mcp_request("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "agentcore-strands-agent", "version": "0.1.0"},
    })

    if init_resp["status_code"] != 200:
        return {"error": "Failed to initialize MCP connection", "details": init_resp}

    _mcp_request("notifications/initialized")

    print(f"  🔗 Connected to Bazaar (session: {_mcp_session_id})")
    return {"status": "connected", "session_id": _mcp_session_id, "server_info": init_resp["body"]}


@tool
def discover_bazaar_tools(network: str = "eip155:8453", query: str = "") -> dict:
    """Search for available paid tools on the Bazaar marketplace.
    Call connect_to_bazaar first.

    Args:
        network: Blockchain network filter in CAIP-2 format. Defaults to Base.
        query: Optional search query to filter tools.

    Returns:
        List of available tools with their names, costs, and the full x402_payload
        needed for process_payment.
    """
    tools_resp = _mcp_request("tools/list")
    if tools_resp["status_code"] != 200:
        return {"error": "Failed to list tools", "details": tools_resp}

    search_resp = _mcp_request("tools/call", {
        "name": "search_resources",
        "arguments": {"query": query, "network": network},
    })

    if search_resp["status_code"] != 200:
        return {"error": "search_resources failed", "details": search_resp}

    result_content = search_resp["body"].get("result", {}).get("content", [])
    text = result_content[0].get("text", "{}") if result_content else "{}"
    parsed = json.loads(text)

    tools_list = []
    for t in parsed.get("tools", []):
        accepts = t.get("_meta", {}).get("x402/payment-required", {}).get("accepts", [])
        cost_info = accepts[0] if accepts else {}
        tools_list.append({
            "name": t.get("name"),
            "description": t.get("description", ""),
            "x402_payload": cost_info,
            "cost": cost_info.get("maxAmountRequired") or cost_info.get("amount", "unknown"),
            "network": cost_info.get("network", ""),
        })

    print(f"  🔍 Found {len(tools_list)} tools on {network}")
    return {"tools": tools_list, "total": parsed.get("pagination", {}).get("total", len(tools_list))}


@tool
def call_bazaar_tool(tool_name: str, parameters: dict = None) -> dict:
    """Call a paid tool on the Bazaar. If the tool requires x402 payment,
    this function automatically pays via AgentCore Payments and retries.

    The merchant's payment requirement (accepts[0]) is passed as-is to
    ProcessPayment — no field parsing needed.

    Call connect_to_bazaar and discover_bazaar_tools first to find available tools.

    Args:
        tool_name: The name of the Bazaar tool to call (from discover_bazaar_tools).
        parameters: Optional parameters to pass to the tool.

    Returns:
        The tool's response after payment (if required).
    """
    import base64

    params = {"name": "proxy_tool_call", "arguments": {"toolName": tool_name, "parameters": {"method": "POST", **(parameters or {})}}}

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if _mcp_session_id:
        headers["Mcp-Session-Id"] = _mcp_session_id

    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call", "params": params}

    print(f"  📡 Calling Bazaar tool: {tool_name}")
    resp = requests.post(BAZAAR_URL, headers=headers, json=payload, timeout=30)

    if resp.status_code == 402:
        print(f"  🔒 HTTP 402 — Payment required for {tool_name}")

        try:
            payment_info = resp.json()
        except Exception:
            payment_info = {}

        x402_version = payment_info.get("x402Version", 1)
        accepts = payment_info.get("accepts", [])
        if not accepts:
            return {"error": "402 received but no payment details found", "raw": resp.text[:2000]}

        # Pass the merchant's accepts[0] as-is to ProcessPayment (via boto3 SDK)
        x402_payload = accepts[0]
        print(f"  💰 Payment: {x402_payload.get('maxAmountRequired') or x402_payload.get('amount', '?')} on {x402_payload.get('network', '?')}")

        pay_result = _call_process_payment(x402_payload, x402_version=x402_version)

        status = pay_result.get("status", "UNKNOWN")
        print(f"  💳 ProcessPayment → {status}")

        if status != "PROOF_GENERATED":
            return {"error": f"Payment failed with status: {status}", "details": pay_result}

        # Construct proper x402 header with top-level protocol fields
        crypto_output = pay_result["paymentOutput"]["cryptoX402"]
        if x402_version >= 2:
            resource = payment_info.get("resource") or {"url": BAZAAR_URL, "description": "", "mimeType": "application/json"}
            accepted = {
                k: v
                for k, v in x402_payload.items()
                if k not in ("description", "mimeType", "outputSchema", "resource")
            }
            payment_header_value = {
                "x402Version": 2,
                "resource": resource,
                "accepted": accepted,
                "payload": crypto_output.get("payload", crypto_output),
                "extensions": payment_info.get("extensions", {}),
            }
            header_name = "PAYMENT-SIGNATURE"
        else:
            payment_header_value = {
                "x402Version": 1,
                "scheme": x402_payload.get("scheme", "exact"),
                "network": x402_payload.get("network", "base"),
                "payload": crypto_output.get("payload", crypto_output),
            }
            header_name = "X-PAYMENT"
        proof_b64 = base64.b64encode(json.dumps(payment_header_value).encode()).decode()

        # The Bazaar MCP proxy forwards parameters.headers to the underlying
        # merchant. Passing the header on the MCP request itself does NOT reach the merchant.
        print(f"  🔄 Retrying {tool_name} with payment proof (POST, {header_name})...")

        import time
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            retry_params = {
                "name": "proxy_tool_call",
                "arguments": {
                    "toolName": tool_name,
                    "parameters": {
                        "headers": {header_name: proof_b64},
                        "method": "POST",
                        **(parameters or {}),
                    },
                },
            }
            retry_payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call", "params": retry_params}

            print(f"  🔄 Bazaar retry attempt {attempt}/{max_attempts}...")
            retry_resp = requests.post(BAZAAR_URL, headers=headers, json=retry_payload, timeout=30)

            if retry_resp.status_code == 200:
                retry_body = retry_resp.json()
                retry_result = retry_body.get("result", {})
                retry_is_error = retry_result.get("isError", False)
                if not retry_is_error:
                    print(f"  ✅ {tool_name} succeeded after payment")
                    return {"status": "paid_and_completed", "payment_status": status, "result": retry_body}
                else:
                    retry_structured = retry_result.get("structuredContent", {})
                    if "accepts" in retry_structured:
                        # Still 402 — transaction hasn't settled yet
                        if attempt < max_attempts:
                            wait = 2 * attempt
                            print(f"  ⏳ Transaction pending — waiting {wait}s...")
                            time.sleep(wait)
                            continue
                        return {"status": "paid_but_still_402", "payment_status": status, "details": "Transaction may not have settled yet or wallet USDC balance insufficient"}
                    else:
                        retry_content = retry_result.get("content", [])
                        return {"status": "paid_but_merchant_error", "payment_status": status, "result": retry_content}
            elif retry_resp.status_code == 402:
                if attempt < max_attempts:
                    wait = 2 * attempt
                    print(f"  ⏳ Transaction pending — waiting {wait}s...")
                    time.sleep(wait)
                    continue
                return {"status": "paid_but_still_402", "payment_status": status, "retry_status_code": 402}
            else:
                return {"status": "paid_but_retry_failed", "payment_status": status, "retry_status_code": retry_resp.status_code, "retry_body": retry_resp.text[:2000]}

    elif resp.status_code == 200:
        print(f"  ✅ {tool_name} succeeded (no payment required)")
        return {"status": "completed_free", "result": resp.json()}
    else:
        return {"error": f"Unexpected status {resp.status_code}", "body": resp.text[:2000]}


# ── System Prompt ────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an AI agent with the ability to make HTTP requests and pay for
paid endpoints using x402 cryptocurrency payments via Amazon Bedrock AgentCore Payments.

## Your Capabilities
- Make HTTP requests to any URL using the http_request tool
- When you encounter an HTTP 402 (Payment Required) response, you can pay for access
  using the process_payment tool, then retry with http_request_with_payment_header
- Connect to the Coinbase Bazaar MCP marketplace to discover and call paid tools
- You operate under a ProcessPaymentRole with a pre-set budget — you can only spend
  within the limits set by the application backend

## Payment Flow (x402) — Generic HTTP
When an endpoint returns HTTP 402:
1. The http_request tool returns x402_payload and x402_version — the merchant's
   payment requirement (accepts[0] from the 402 response)
2. Pass x402_payload AS-IS to process_payment along with x402_version.
   Do NOT parse individual fields.
3. After process_payment succeeds (status: PROOF_GENERATED), simply call
   http_request_with_payment_header with just the URL. The payment proof,
   HTTP method, and x402 version are all stored automatically.

That's it — three tool calls: http_request → process_payment → http_request_with_payment_header(url)

The retry tool automatically retries with backoff if the merchant still returns
402 while the on-chain transaction settles.

IMPORTANT: Use the merchant's documented HTTP method for both the initial request and retry.
IMPORTANT: The x402 payment requirement from the merchant contains all the fields
the ProcessPayment API needs (scheme, network, amount, asset, payTo, extra, etc.).
Pass it through as-is — do not reconstruct or cherry-pick fields.

## Bazaar Flow — Paid MCP Tools
To use paid tools from the Coinbase Bazaar marketplace:
1. Call connect_to_bazaar to establish an MCP session
2. Call discover_bazaar_tools to find available tools (defaults to Base mainnet)
3. Call call_bazaar_tool with the tool name — it handles x402 payment automatically
   (detects 402, passes merchant payload as-is to ProcessPayment, retries with proof)

## Current Configuration
- Session ID: {PAYMENT_SESSION_ID}
- Instrument ID: {PAYMENT_INSTRUMENT_ID}
- Default pay-to address: {PAY_TO}
- Network: Base (eip155:8453)
- Asset: USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)

## Important
- Always check the payment amount before paying — report it to the user
- You CANNOT create new sessions or instruments — only the app backend can do that
- Process payments from deterministic code paths when possible
"""


# ── Agent Setup ──────────────────────────────────────────────────

def create_agent() -> Agent:
    """Create and return the Strands agent with payment tools."""
    return Agent(
        system_prompt=SYSTEM_PROMPT,
        tools=[
            process_payment,
            http_request,
            http_request_with_payment_header,
            connect_to_bazaar,
            discover_bazaar_tools,
            call_bazaar_tool,
        ],
        model="us.anthropic.claude-sonnet-4-20250514-v1:0",
    )


# ── Main ─────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  AgentCore Payments — Strands Agent")
    print("=" * 60)
    print(f"  Manager   : {MANAGER_ARN}")
    print(f"  Session   : {PAYMENT_SESSION_ID}")
    print(f"  Instrument: {PAYMENT_INSTRUMENT_ID}")
    print(f"  Endpoint  : {DP_ENDPOINT}")
    print("=" * 60)
    print()

    agent = create_agent()

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        print(f"Prompt: {prompt}\n")
        result = agent(prompt)
        print(f"\n{'=' * 60}")
        print("Agent response:")
        print(result)
    else:
        print("Enter prompts (type 'quit' to exit):\n")
        while True:
            try:
                prompt = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if not prompt or prompt.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break
            result = agent(prompt)
            print()


if __name__ == "__main__":
    main()
