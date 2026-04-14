#!/usr/bin/env bash
# =============================================================
# setup_manager.sh — AgentCore Payments resource setup (CoinbaseCDP)
#
# Assumes IAM roles already exist (run setup_roles.sh first).
# Creates:
#   1. PaymentCredentialProvider  (stores Coinbase keys in Secrets Manager)
#   2. PaymentManager             (AWS_IAM authorizer, uses ResourceRetrievalRole)
#   3. PaymentConnector           (links manager + credential provider)
#
# Requirements:
#   - aws CLI, awscurl, jq
#   - IAM roles created via: bash setup_roles.sh
#   - .env file with credentials (copy from .env.sample)
#   - Service model installed via: bash setup_model.sh
#
# Usage:
#   bash setup_roles.sh   # one-time
#   bash setup_manager.sh
# =============================================================

set -euo pipefail

# ── Colour helpers ───────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${BOLD}$*${RESET}"; }
success() { echo -e "${GREEN}$*${RESET}"; }
warn()    { echo -e "${YELLOW}$*${RESET}"; }
die()     { echo -e "${RED}$*${RESET}" >&2; exit 1; }
sep()     { echo -e "${BOLD}$(printf '=%.0s' {1..60})${RESET}"; }

# ── Load .env ───────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    die ".env not found. Run: cp .env.sample .env  then fill in your credentials."
fi
set -o allexport
# shellcheck disable=SC1090
source "$ENV_FILE"
set +o allexport

# ── Validate required variables ─────────────────────────────────
check_var() {
    local key="$1" val="${!1:-}"
    if [[ -z "$val" || "$val" == \<* ]]; then
        die "Missing or placeholder value for $key in .env"
    fi
}
check_var COINBASE_API_KEY_ID
check_var COINBASE_API_KEY_SECRET
check_var COINBASE_WALLET_SECRET
check_var CREDENTIAL_PROVIDER_ENDPOINT
check_var PAYMENTS_CP_ENDPOINT

AWS_REGION="${AWS_REGION:-us-west-2}"

# ── Dependency checks ────────────────────────────────────────────
for cmd in aws awscurl jq; do
    command -v "$cmd" &>/dev/null || die "$cmd not found."
done

# ── Verify AWS credentials ──────────────────────────────────────
CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1) || \
    die "AWS credentials not configured or expired."
ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | jq -r '.Account')
info "AWS Account: $ACCOUNT_ID"

# ── Fixed IAM role names ─────────────────────────────────────────
CONTROL_PLANE_ROLE_NAME="AgentCorePaymentsControlPlaneRole"
MANAGEMENT_ROLE_NAME="AgentCorePaymentsManagementRole"
PROCESS_PAYMENT_ROLE_NAME="AgentCorePaymentsProcessPaymentRole"
RESOURCE_RETRIEVAL_ROLE_NAME="AgentCorePaymentsResourceRetrievalRole"

# ── Check IAM roles exist ────────────────────────────────────────
MISSING_ROLES=()
for ROLE in "$CONTROL_PLANE_ROLE_NAME" "$MANAGEMENT_ROLE_NAME" "$PROCESS_PAYMENT_ROLE_NAME" "$RESOURCE_RETRIEVAL_ROLE_NAME"; do
    aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text &>/dev/null || MISSING_ROLES+=("$ROLE")
done

if [[ ${#MISSING_ROLES[@]} -gt 0 ]]; then
    die "Missing IAM roles: ${MISSING_ROLES[*]}\n   Run first:  bash setup_roles.sh"
fi

CONTROL_PLANE_ROLE_ARN=$(aws iam get-role --role-name "$CONTROL_PLANE_ROLE_NAME" --query 'Role.Arn' --output text)
MANAGEMENT_ROLE_ARN=$(aws iam get-role --role-name "$MANAGEMENT_ROLE_NAME" --query 'Role.Arn' --output text)
PROCESS_PAYMENT_ROLE_ARN=$(aws iam get-role --role-name "$PROCESS_PAYMENT_ROLE_NAME" --query 'Role.Arn' --output text)
RESOURCE_RETRIEVAL_ROLE_ARN=$(aws iam get-role --role-name "$RESOURCE_RETRIEVAL_ROLE_NAME" --query 'Role.Arn' --output text)

success "All 4 IAM roles found"

# ── Validate name pattern: [a-zA-Z][a-zA-Z0-9_]{0,47} ──────────
SUFFIX="$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c8 || true)"
[[ -z "$SUFFIX" ]] && SUFFIX="$(date +%s | sha256sum | head -c8)"

validate_name() {
    local name="$1"
    if [[ ! "$name" =~ ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ ]]; then
        die "Invalid name '$name': must match [a-zA-Z][a-zA-Z0-9_]{0,47} (no hyphens, max 48 chars)"
    fi
}

MANAGER_NAME="${DEFAULT_PAYMENT_MANAGER_NAME:-PaymentManager${SUFFIX}}"
CONNECTOR_NAME="${DEFAULT_PAYMENT_CONNECTOR_NAME:-CoinbaseConnector${SUFFIX}}"
CRED_PROVIDER_NAME="CoinbaseCdp${SUFFIX}"

validate_name "$MANAGER_NAME"
validate_name "$CONNECTOR_NAME"
validate_name "$CRED_PROVIDER_NAME"

# ── Banner ───────────────────────────────────────────────────────
echo ""
sep
info "  AgentCore Payments — Resource Setup"
sep
echo "  Region    : $AWS_REGION"
echo "  Account   : $ACCOUNT_ID"
echo "  Manager   : $MANAGER_NAME"
echo "  Connector : $CONNECTOR_NAME"
sep
echo ""

# ── Assume ControlPlaneRole for CP API calls ─────────────────────
info "Assuming ControlPlaneRole for API calls..."
echo "  Role ARN: $CONTROL_PLANE_ROLE_ARN"
CP_CREDS=$(aws sts assume-role \
    --role-arn "$CONTROL_PLANE_ROLE_ARN" \
    --role-session-name "setup-script" \
    --output json)
export AWS_ACCESS_KEY_ID=$(echo "$CP_CREDS" | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo "$CP_CREDS" | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo "$CP_CREDS" | jq -r '.Credentials.SessionToken')

# Verify assumed identity
ASSUMED_ID=$(aws sts get-caller-identity 2>&1)
echo "  Assumed as: $(echo "$ASSUMED_ID" | jq -r '.Arn')"
success "Now operating as ControlPlaneRole"
echo ""

# ── Helper: signed POST ─────────────────────────────────────────
acurl() {
    local url="$1" body="$2"
    local response stderr_output exit_code

    stderr_output=$(mktemp)
    set +e
    response=$(awscurl \
        --service bedrock-agentcore \
        --region "$AWS_REGION" \
        -X POST \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        --data "$body" \
        "$url" 2>"$stderr_output")
    exit_code=$?
    set -e

    if [[ $exit_code -ne 0 ]]; then
        local errmsg
        errmsg=$(cat "$stderr_output")
        rm -f "$stderr_output"
        die "awscurl command failed (exit code $exit_code)\n  URL: $url\n  Body: $body\n  Stderr: $errmsg\n  Stdout: $response"
    fi
    rm -f "$stderr_output"

    local err
    err=$(echo "$response" | jq -r '.message // .Message // .errorMessage // .error // empty' 2>/dev/null || true)
    if [[ -n "$err" && "$err" != "null" ]]; then
        die "API error: $err\n  URL: $url\n  Request body: $body\n  Full response: $response"
    fi
    echo "$response"
}

# ── Step 1: CreatePaymentCredentialProvider ──────────────────────
info "[1/3] Creating PaymentCredentialProvider '$CRED_PROVIDER_NAME' ..."
echo "      Endpoint: $CREDENTIAL_PROVIDER_ENDPOINT"

CRED_BODY=$(jq -n \
    --arg name "$CRED_PROVIDER_NAME" \
    --arg keyId "$COINBASE_API_KEY_ID" \
    --arg keySecret "$COINBASE_API_KEY_SECRET" \
    --arg walletSecret "$COINBASE_WALLET_SECRET" \
    '{
        name: $name,
        credentialProviderVendor: "CoinbaseCDP",
        providerConfigurationInput: {
            coinbaseCdpConfiguration: {
                apiKeyId: $keyId,
                apiKeySecret: $keySecret,
                walletSecret: $walletSecret
            }
        }
    }')

CRED_RESPONSE=$(acurl \
    "${CREDENTIAL_PROVIDER_ENDPOINT}/identities/CreatePaymentCredentialProvider" \
    "$CRED_BODY")

echo ""
echo "$CRED_RESPONSE" | jq .
CREDENTIAL_PROVIDER_ARN=$(echo "$CRED_RESPONSE" | jq -r '.credentialProviderArn')
[[ -z "$CREDENTIAL_PROVIDER_ARN" || "$CREDENTIAL_PROVIDER_ARN" == "null" ]] && \
    die "Failed to get credentialProviderArn from response.\n  URL: ${CREDENTIAL_PROVIDER_ENDPOINT}/identities/CreatePaymentCredentialProvider\n  Request body: $CRED_BODY\n  Full response: $CRED_RESPONSE"

success "PaymentCredentialProvider created"
echo "  credentialProviderArn: $CREDENTIAL_PROVIDER_ARN"
echo ""

# ── Step 2: CreatePaymentManager ─────────────────────────────────
info "[2/3] Creating PaymentManager '$MANAGER_NAME' ..."
echo "      Endpoint: $PAYMENTS_CP_ENDPOINT"

MANAGER_BODY=$(jq -n \
    --arg name "$MANAGER_NAME" \
    --arg roleArn "$RESOURCE_RETRIEVAL_ROLE_ARN" \
    '{
        name: $name,
        authorizerType: "AWS_IAM",
        roleArn: $roleArn
    }')

MANAGER_RESPONSE=$(acurl \
    "${PAYMENTS_CP_ENDPOINT}/payments/managers" \
    "$MANAGER_BODY")

echo ""
echo "$MANAGER_RESPONSE" | jq .
PAYMENT_MANAGER_ID=$(echo "$MANAGER_RESPONSE" | jq -r '.paymentManagerId')
PAYMENT_MANAGER_ARN=$(echo "$MANAGER_RESPONSE" | jq -r '.paymentManagerArn')
[[ -z "$PAYMENT_MANAGER_ID" || "$PAYMENT_MANAGER_ID" == "null" ]] && \
    die "Failed to get paymentManagerId from response.\n  URL: ${PAYMENTS_CP_ENDPOINT}/payments/managers\n  Request body: $MANAGER_BODY\n  Full response: $MANAGER_RESPONSE"

success "PaymentManager created"
echo "  paymentManagerId: $PAYMENT_MANAGER_ID"
echo ""

# ── Step 3: CreatePaymentConnector ──────────────────────────────
info "[3/3] Creating PaymentConnector '$CONNECTOR_NAME' ..."
echo "      Endpoint: $PAYMENTS_CP_ENDPOINT"

CONN_BODY=$(jq -n \
    --arg name "$CONNECTOR_NAME" \
    --arg credArn "$CREDENTIAL_PROVIDER_ARN" \
    '{
        name: $name,
        type: "CoinbaseCDP",
        credentialProviderConfigurations: [
            {
                coinbaseCDP: {
                    credentialProviderArn: $credArn
                }
            }
        ]
    }')

CONN_RESPONSE=$(acurl \
    "${PAYMENTS_CP_ENDPOINT}/payments/managers/${PAYMENT_MANAGER_ID}/connectors" \
    "$CONN_BODY")

echo ""
echo "$CONN_RESPONSE" | jq .
PAYMENT_CONNECTOR_ID=$(echo "$CONN_RESPONSE" | jq -r '.paymentConnectorId')
[[ -z "$PAYMENT_CONNECTOR_ID" || "$PAYMENT_CONNECTOR_ID" == "null" ]] && \
    die "Failed to get paymentConnectorId from response.\n  URL: ${PAYMENTS_CP_ENDPOINT}/payments/managers/${PAYMENT_MANAGER_ID}/connectors\n  Request body: $CONN_BODY\n  Full response: $CONN_RESPONSE"

success "PaymentConnector created"
echo "  paymentConnectorId: $PAYMENT_CONNECTOR_ID"
echo ""

# ── Summary ──────────────────────────────────────────────────────
sep
success "Setup complete!"
sep
echo "  credentialProviderArn : $CREDENTIAL_PROVIDER_ARN"
echo "  paymentManagerArn     : $PAYMENT_MANAGER_ARN"
echo "  paymentManagerId      : $PAYMENT_MANAGER_ID"
echo "  paymentConnectorId    : $PAYMENT_CONNECTOR_ID"
sep
echo ""

