#!/usr/bin/env python3
"""
setup_manager.py — AgentCore Payments resource setup (CoinbaseCDP)

Assumes IAM roles already exist (run setup_roles.sh first).
Creates:
  1. PaymentCredentialProvider  (CoinbaseCDP credentials)
  2. PaymentManager             (AWS_IAM authorizer, uses ResourceRetrievalRole)
  3. PaymentConnector           (links manager + credential provider)

Prerequisites:
  1. Run setup_roles.sh once to create IAM roles.
  2. Run setup_model.sh once to install the botocore service model.
  3. Copy .env.sample -> .env and fill in your credentials.
  4. pip install boto3 python-dotenv

Usage:
  bash setup_roles.sh   # one-time
  python3 setup_manager.py
"""

import json
import os
import re
import sys
import uuid

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("boto3 is not installed. Run: pip install boto3 python-dotenv")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv is not installed. Run: pip install boto3 python-dotenv")

# ── Load configuration ──────────────────────────────────────────
_ENV_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ENV_DIR, ".env"))

def require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val or val.startswith("<"):
        sys.exit(f"Missing or placeholder value for {key} in .env")
    return val


def optional_endpoint(key: str) -> str | None:
    """HTTPS endpoint override from .env; omit key or use placeholder → use boto3 default."""
    val = os.environ.get(key, "").strip()
    if not val or val.startswith("<"):
        return None
    return val


COINBASE_API_KEY_ID          = require_env("COINBASE_API_KEY_ID")
COINBASE_API_KEY_SECRET      = require_env("COINBASE_API_KEY_SECRET")
COINBASE_WALLET_SECRET       = require_env("COINBASE_WALLET_SECRET")
AWS_REGION                   = os.environ.get("AWS_REGION", "us-west-2")
# Optional: if unset, boto3 uses its regional default for bedrock-agentcore-control
CREDENTIAL_PROVIDER_ENDPOINT = optional_endpoint("CREDENTIAL_PROVIDER_ENDPOINT")
PAYMENTS_CP_ENDPOINT         = optional_endpoint("PAYMENTS_CP_ENDPOINT")
if CREDENTIAL_PROVIDER_ENDPOINT and not PAYMENTS_CP_ENDPOINT:
    PAYMENTS_CP_ENDPOINT = CREDENTIAL_PROVIDER_ENDPOINT
if PAYMENTS_CP_ENDPOINT and not CREDENTIAL_PROVIDER_ENDPOINT:
    CREDENTIAL_PROVIDER_ENDPOINT = PAYMENTS_CP_ENDPOINT


def _cp_client_kwargs(endpoint: str | None) -> dict:
    if endpoint:
        return {"endpoint_url": endpoint}
    return {}

# Resource names
NAME_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,47}$')
suffix = uuid.uuid4().hex[:8]

_mgr_name  = os.environ.get("DEFAULT_PAYMENT_MANAGER_NAME", "").strip()
_conn_name = os.environ.get("DEFAULT_PAYMENT_CONNECTOR_NAME", "").strip()
MANAGER_NAME   = _mgr_name if (_mgr_name and NAME_PATTERN.match(_mgr_name)) \
    else f"PaymentManager{suffix}"
CONNECTOR_NAME = _conn_name if (_conn_name and NAME_PATTERN.match(_conn_name)) \
    else f"CoinbaseConnector{suffix}"
CRED_PROVIDER_NAME = f"CoinbaseCdp{suffix}"

# ── Fixed IAM role names ────────────────────────────────────────
CONTROL_PLANE_ROLE_NAME      = "AgentCorePaymentsControlPlaneRole"
MANAGEMENT_ROLE_NAME         = "AgentCorePaymentsManagementRole"
PROCESS_PAYMENT_ROLE_NAME    = "AgentCorePaymentsProcessPaymentRole"
RESOURCE_RETRIEVAL_ROLE_NAME = "AgentCorePaymentsResourceRetrievalRole"

# ── Verify AWS credentials ──────────────────────────────────────
sts = boto3.client("sts", region_name=AWS_REGION)
try:
    identity = sts.get_caller_identity()
    ACCOUNT_ID = identity["Account"]
except Exception as e:
    sys.exit(f"AWS credentials not configured or expired. Error: {e}")

# Verify service model
cred_client_test = boto3.client(
    "bedrock-agentcore-control",
    region_name=AWS_REGION,
    **_cp_client_kwargs(CREDENTIAL_PROVIDER_ENDPOINT),
)
if not hasattr(cred_client_test, "create_payment_credential_provider"):
    sys.exit(
        "create_payment_credential_provider not found on boto3 client.\n"
        "   Run setup_model.sh first to install the botocore service model."
    )

# ── Check IAM roles exist ───────────────────────────────────────
iam_client = boto3.client("iam", region_name=AWS_REGION)

ROLE_NAMES = [
    CONTROL_PLANE_ROLE_NAME,
    MANAGEMENT_ROLE_NAME,
    PROCESS_PAYMENT_ROLE_NAME,
    RESOURCE_RETRIEVAL_ROLE_NAME,
]

missing_roles = []
role_arns = {}
for role_name in ROLE_NAMES:
    try:
        arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        role_arns[role_name] = arn
    except ClientError:
        missing_roles.append(role_name)

if missing_roles:
    sys.exit(
        f"Missing IAM roles: {', '.join(missing_roles)}\n"
        f"   Run first:  bash setup_roles.sh"
    )

CONTROL_PLANE_ROLE_ARN      = role_arns[CONTROL_PLANE_ROLE_NAME]
MANAGEMENT_ROLE_ARN         = role_arns[MANAGEMENT_ROLE_NAME]
PROCESS_PAYMENT_ROLE_ARN    = role_arns[PROCESS_PAYMENT_ROLE_NAME]
RESOURCE_RETRIEVAL_ROLE_ARN = role_arns[RESOURCE_RETRIEVAL_ROLE_NAME]

print("  All 4 IAM roles found")


def pp(label: str, response: dict):
    response.pop("ResponseMetadata", None)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(response, indent=2, default=str))


print("\n" + "="*60)
print("  AgentCore Payments — Resource Setup")
print("="*60)
print(f"  Region    : {AWS_REGION}")
print(f"  Account   : {ACCOUNT_ID}")
print(f"  Manager   : {MANAGER_NAME}")
print(f"  Connector : {CONNECTOR_NAME}")
print("="*60)

# ── Assume ControlPlaneRole for CP API calls ─────────────────────
print(f"\n  Assuming ControlPlaneRole for API calls...")
print(f"  Role ARN: {CONTROL_PLANE_ROLE_ARN}")
cp_creds = sts.assume_role(
    RoleArn=CONTROL_PLANE_ROLE_ARN,
    RoleSessionName="setup-script",
)["Credentials"]

cp_session = boto3.Session(
    aws_access_key_id=cp_creds["AccessKeyId"],
    aws_secret_access_key=cp_creds["SecretAccessKey"],
    aws_session_token=cp_creds["SessionToken"],
    region_name=AWS_REGION,
)

cred_client = cp_session.client(
    "bedrock-agentcore-control",
    region_name=AWS_REGION,
    **_cp_client_kwargs(CREDENTIAL_PROVIDER_ENDPOINT),
)
payments_client = cp_session.client(
    "bedrock-agentcore-control",
    region_name=AWS_REGION,
    **_cp_client_kwargs(PAYMENTS_CP_ENDPOINT),
)
print(f"  Control plane client endpoint: {cred_client.meta.endpoint_url}")
# Verify assumed identity
cp_sts = cp_session.client("sts", region_name=AWS_REGION)
assumed_id = cp_sts.get_caller_identity()
print(f"  Assumed as: {assumed_id['Arn']}")
print(f"  Now operating as ControlPlaneRole")


def _payments_preview_help(account_id: str, region: str) -> str:
    return (
        "\n  AgentCore Payments returned UnknownOperationException — the API exists in your\n"
        "  local botocore model, but this AWS account/region is not serving Payment APIs.\n\n"
        "  Typical cause: this account is not allowlisted for the AgentCore Payments **private preview**.\n"
        f"  • Account: {account_id}\n"
        f"  • Region:  {region}\n\n"
        "  Ask your AWS contact to confirm **AgentCore Payments** preview access for this account\n"
        "  (see docs/getting-started.md). If you have multiple accounts, use the one that was allowlisted.\n"
    )


# ── Step 1: CreatePaymentCredentialProvider ─────────────────────
print(f"\n[1/3] Creating PaymentCredentialProvider '{CRED_PROVIDER_NAME}' ...")
try:
    cred_response = cred_client.create_payment_credential_provider(
        name=CRED_PROVIDER_NAME,
        credentialProviderVendor="CoinbaseCDP",
        providerConfigurationInput={
            "coinbaseCdpConfiguration": {
                "apiKeyId": COINBASE_API_KEY_ID,
                "apiKeySecret": COINBASE_API_KEY_SECRET,
                "walletSecret": COINBASE_WALLET_SECRET,
            }
        },
    )
except ClientError as e:
    code = e.response.get("Error", {}).get("Code", "")
    if code == "UnknownOperationException":
        sys.exit(_payments_preview_help(ACCOUNT_ID, AWS_REGION) + f"  Raw error: {e}")
    raise
pp("CreatePaymentCredentialProvider", dict(cred_response))
credential_provider_arn = cred_response["credentialProviderArn"]
print(f"\n  credentialProviderArn: {credential_provider_arn}")


# ── Step 2: CreatePaymentManager ───────────────────────────────
print(f"\n[2/3] Creating PaymentManager '{MANAGER_NAME}' ...")
try:
    mgr_response = payments_client.create_payment_manager(
        name=MANAGER_NAME,
        authorizerType="AWS_IAM",
        roleArn=RESOURCE_RETRIEVAL_ROLE_ARN,
    )
except ClientError as e:
    code = e.response.get("Error", {}).get("Code", "")
    if code == "UnknownOperationException":
        sys.exit(_payments_preview_help(ACCOUNT_ID, AWS_REGION) + f"  Raw error: {e}")
    raise
pp("CreatePaymentManager", dict(mgr_response))
payment_manager_id = mgr_response["paymentManagerId"]
print(f"\n  paymentManagerId: {payment_manager_id}")


# ── Step 3: CreatePaymentConnector ─────────────────────────────
print(f"\n[3/3] Creating PaymentConnector '{CONNECTOR_NAME}' ...")
try:
    conn_response = payments_client.create_payment_connector(
        paymentManagerId=payment_manager_id,
        name=CONNECTOR_NAME,
        type="CoinbaseCDP",
        credentialProviderConfigurations=[
            {"coinbaseCDP": {"credentialProviderArn": credential_provider_arn}}
        ],
    )
except ClientError as e:
    code = e.response.get("Error", {}).get("Code", "")
    if code == "UnknownOperationException":
        sys.exit(_payments_preview_help(ACCOUNT_ID, AWS_REGION) + f"  Raw error: {e}")
    raise
pp("CreatePaymentConnector", dict(conn_response))

# ── Summary ─────────────────────────────────────────────────────
print("\n" + "="*60)
print("  Setup complete!")
print("="*60)
print(f"  credentialProviderArn : {credential_provider_arn}")
print(f"  paymentManagerArn     : {mgr_response['paymentManagerArn']}")
print(f"  paymentManagerId      : {payment_manager_id}")
print(f"  paymentConnectorId    : {conn_response['paymentConnectorId']}")
print("="*60 + "\n")