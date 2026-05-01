#!/usr/bin/env bash
# =============================================================
# e2e-test.sh — E2E test for AgentCore Payments
#
# All tests use the native AWS CLI SDK.
#
# Three-phase test reflecting real-world persona separation:
#
#   Part A — Control Plane (ControlPlaneRole, AWS CLI SDK):
#     A1. list-payment-managers
#     A2. get-payment-manager
#     A3. list-payment-connectors
#     A4. get-payment-connector
#
#   Part B — Data Plane: Application Backend (ManagementRole, AWS CLI SDK):
#     B1. create-payment-instrument
#     B2. get-payment-instrument
#     B3. list-payment-instruments
#     B4. create-payment-session
#
#   Part C — Data Plane: Agent Execution (ProcessPaymentRole, AWS CLI SDK):
#     C1. process-payment
#
#   Part D — Data Plane: Post-Payment Verification (ManagementRole, AWS CLI SDK):
#     D1. get-payment-session
#     D2. list-payment-sessions
#
# Prerequisites:
#   - AWS CLI v2 with service models installed (bash quickstart/setup_model.sh)
#   - jq
#   - .env file with config values
#   - Wallet funded with USDC on Base
#
# Usage:
#   cp .env.sample .env   # fill in values from quickstart output
#   bash e2e-test.sh
#
# After a successful run, copy the printed PAYMENT_SESSION_ID / PAYMENT_INSTRUMENT_ID /
# USER_ID into ../blockrun-demo/.env — each run creates new IDs.
# =============================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${BOLD}$*${RESET}"; }
success() { echo -e "${GREEN}✅ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${RESET}"; }
fail()    { echo -e "${RED}❌ $*${RESET}"; FAILURES=$((FAILURES+1)); }
sep()     { echo -e "${CYAN}$(printf '─%.0s' {1..60})${RESET}"; }
banner()  { echo -e "\n${BOLD}$(printf '=%.0s' {1..60})${RESET}"; echo -e "${BOLD}  $*${RESET}"; echo -e "${BOLD}$(printf '=%.0s' {1..60})${RESET}\n"; }

# ── Load .env ────────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
[[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

REGION="${AWS_REGION:-us-west-2}"
ORIG_PROFILE="${AWS_PROFILE:-}"
PROFILE_ARG=""
[[ -n "$ORIG_PROFILE" ]] && PROFILE_ARG="--profile $ORIG_PROFILE"

CP_ENDPOINT="${CP_ENDPOINT:-https://bedrock-agentcore-control.us-west-2.amazonaws.com}"
DP_ENDPOINT="${DP_ENDPOINT:-https://bedrock-agentcore.us-west-2.amazonaws.com}"
MANAGER_ARN="${MANAGER_ARN:?Missing MANAGER_ARN}"
CONNECTOR_ID="${CONNECTOR_ID:?Missing CONNECTOR_ID}"
MANAGEMENT_ROLE_ARN="${MANAGEMENT_ROLE_ARN:?Missing MANAGEMENT_ROLE_ARN}"
PROCESS_PAYMENT_ROLE_ARN="${PROCESS_PAYMENT_ROLE_ARN:?Missing PROCESS_PAYMENT_ROLE_ARN}"
CONTROL_PLANE_ROLE_ARN="${CONTROL_PLANE_ROLE_ARN:-}"
USER_ID="${USER_ID:-test-user-sdk}"
PAY_TO="${PAY_TO:-0x312554704B5c47b992e876639B144e9B85431E44}"
SESSION_LIMIT_USD="${SESSION_LIMIT_USD:-1000000.0}"
PAYMENT_AMOUNT="${PAYMENT_AMOUNT:-100000}"

MANAGER_ID="${MANAGER_ARN##*/}"

PASSED=0; FAILURES=0

# ── Dependency checks ────────────────────────────────────────────
command -v jq &>/dev/null || { echo "❌ jq not found."; exit 1; }

# ── Helper: assume role ──────────────────────────────────────────
assume_role() {
    local role_arn="$1" session_name="$2"
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    local assume_flags=()
    [[ -n "$ORIG_PROFILE" ]] && assume_flags=(--profile "$ORIG_PROFILE")
    local creds
    creds=$(aws sts assume-role ${assume_flags[@]+"${assume_flags[@]}"} \
        --role-arn "$role_arn" \
        --role-session-name "$session_name" \
        --output json) || { echo "❌ Failed to assume $role_arn"; exit 1; }
    export AWS_ACCESS_KEY_ID=$(echo "$creds" | jq -r '.Credentials.AccessKeyId')
    export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r '.Credentials.SecretAccessKey')
    export AWS_SESSION_TOKEN=$(echo "$creds" | jq -r '.Credentials.SessionToken')
}

uuid() { python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || echo "$(date +%s)-$"; }

banner "AgentCore Payments — SDK E2E Test"
echo "  CP Endpoint   : $CP_ENDPOINT"
echo "  DP Endpoint   : $DP_ENDPOINT"
echo "  ManagerArn   : $MANAGER_ARN"
echo "  ConnectorId   : $CONNECTOR_ID"
echo "  Method        : AWS CLI SDK"
echo ""

# ═════════════════════════════════════════════════════════════════
# Part A: Control Plane (AWS CLI SDK)
# ═════════════════════════════════════════════════════════════════
banner "Part A: Control Plane (AWS CLI SDK)"

if [[ -n "$CONTROL_PLANE_ROLE_ARN" ]]; then
    info "Assuming ControlPlaneRole..."
    assume_role "$CONTROL_PLANE_ROLE_ARN" "e2e-cp-$(date +%s)"
    success "Assumed ControlPlaneRole"
else
    info "Using base profile for CP operations"
fi
echo ""

# ── Test A1: list-payment-managers ─────────────────────────────
sep
info "Test A1: list-payment-managers"

LIST_MGR=$(aws bedrock-agentcore-control list-payment-managers \
    --region "$REGION" \
    --endpoint-url "$CP_ENDPOINT" \
    --output json 2>&1) || true

echo "$LIST_MGR" | jq . 2>/dev/null || echo "$LIST_MGR"

MGR_COUNT=$(echo "$LIST_MGR" | jq '.paymentManagers | length' 2>/dev/null || echo "0")
if [[ "$MGR_COUNT" -ge 1 ]]; then
    success "list-payment-managers (count: $MGR_COUNT)"
    PASSED=$((PASSED+1))
else
    fail "list-payment-managers (count: $MGR_COUNT)"
fi
echo ""

# ── Test A2: get-payment-manager ───────────────────────────────
sep
info "Test A2: get-payment-manager"

GET_MGR=$(aws bedrock-agentcore-control get-payment-manager \
    --region "$REGION" \
    --endpoint-url "$CP_ENDPOINT" \
    --payment-manager-id "$MANAGER_ID" \
    --output json 2>&1) || true

echo "$GET_MGR" | jq . 2>/dev/null || echo "$GET_MGR"

GOT_ARN=$(echo "$GET_MGR" | jq -r '.paymentManagerArn // empty' 2>/dev/null)
if [[ "$GOT_ARN" == "$MANAGER_ARN" ]]; then
    success "get-payment-manager (arn matches)"
    PASSED=$((PASSED+1))
else
    fail "get-payment-manager (expected $MANAGER_ARN, got $GOT_ARN)"
fi
echo ""

# ── Test A3: list-payment-connectors ─────────────────────────────
sep
info "Test A3: list-payment-connectors"

LIST_CONN=$(aws bedrock-agentcore-control list-payment-connectors \
    --region "$REGION" \
    --endpoint-url "$CP_ENDPOINT" \
    --payment-manager-id "$MANAGER_ID" \
    --output json 2>&1) || true

echo "$LIST_CONN" | jq . 2>/dev/null || echo "$LIST_CONN"

CONN_COUNT=$(echo "$LIST_CONN" | jq '.paymentConnectors | length' 2>/dev/null || echo "0")
if [[ "$CONN_COUNT" -ge 1 ]]; then
    success "list-payment-connectors (count: $CONN_COUNT)"
    PASSED=$((PASSED+1))
else
    fail "list-payment-connectors (count: $CONN_COUNT)"
fi
echo ""

# ── Test A4: get-payment-connector ───────────────────────────────
sep
info "Test A4: get-payment-connector"

GET_CONN=$(aws bedrock-agentcore-control get-payment-connector \
    --region "$REGION" \
    --endpoint-url "$CP_ENDPOINT" \
    --payment-manager-id "$MANAGER_ID" \
    --payment-connector-id "$CONNECTOR_ID" \
    --output json 2>&1) || true

echo "$GET_CONN" | jq . 2>/dev/null || echo "$GET_CONN"

GOT_CONN_ID=$(echo "$GET_CONN" | jq -r '.paymentConnectorId // empty' 2>/dev/null)
if [[ "$GOT_CONN_ID" == "$CONNECTOR_ID" ]]; then
    success "get-payment-connector (id matches)"
    PASSED=$((PASSED+1))
else
    fail "get-payment-connector (expected $CONNECTOR_ID, got $GOT_CONN_ID)"
fi
echo ""

# Clear assumed creds
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# ═════════════════════════════════════════════════════════════════
# Part B: Data Plane — ManagementRole (AWS CLI SDK)
# ═════════════════════════════════════════════════════════════════
banner "Part B: Data Plane — ManagementRole (AWS CLI SDK)"

info "Assuming ManagementRole..."
assume_role "$MANAGEMENT_ROLE_ARN" "e2e-mgmt-$(date +%s)"
success "Assumed ManagementRole"
echo ""

# ── Test B1: create-payment-instrument ───────────────────────────
sep
info "Test B1: create-payment-instrument (SDK)"

# AgentCore DP now expects EMBEDDED_CRYPTO_WALLET (CRYPTO_WALLET removed).
# Details use embeddedCryptoWallet; network ETHEREUM = all supported EVM chains per AWS.
CREATE_INST=$(aws bedrock-agentcore create-payment-instrument \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-connector-id "$CONNECTOR_ID" \
    --payment-instrument-type "EMBEDDED_CRYPTO_WALLET" \
    --payment-instrument-details '{"embeddedCryptoWallet":{"network":"ETHEREUM"}}' \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

if echo "$CREATE_INST" | jq -e . >/dev/null 2>&1; then
    echo "$CREATE_INST" | jq .
else
    echo "$CREATE_INST"
fi

INSTRUMENT_ID=$(echo "$CREATE_INST" | jq -r '.paymentInstrument.paymentInstrumentId // empty' 2>/dev/null || true)
WALLET_ADDR=$(echo "$CREATE_INST" | jq -r '
  (.paymentInstrument.paymentInstrumentDetails.embeddedCryptoWallet.walletAddress
   // .paymentInstrument.paymentInstrumentDetails.cryptoWallet.walletAddress
   // empty)
' 2>/dev/null || true)

if [[ -n "$INSTRUMENT_ID" && "$INSTRUMENT_ID" != "null" ]]; then
    success "create-payment-instrument (instrumentId: $INSTRUMENT_ID)"
    echo "  walletAddress: $WALLET_ADDR"
    PASSED=$((PASSED+1))
else
    fail "create-payment-instrument"
    echo "  Response: $CREATE_INST"
fi
echo ""

# ── Test B2: get-payment-instrument ──────────────────────────────
sep
info "Test B2: get-payment-instrument (SDK)"

GET_INST=$(aws bedrock-agentcore get-payment-instrument \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-connector-id "$CONNECTOR_ID" \
    --payment-instrument-id "$INSTRUMENT_ID" \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$GET_INST" | jq . 2>/dev/null || echo "$GET_INST"

INST_STATUS=$(echo "$GET_INST" | jq -r '.paymentInstrument.status // empty')
if [[ "$INST_STATUS" == "ACTIVE" ]]; then
    success "get-payment-instrument (status: ACTIVE)"
    PASSED=$((PASSED+1))
else
    fail "get-payment-instrument (status: $INST_STATUS)"
fi
echo ""

# ── Test B3: list-payment-instruments ────────────────────────────
sep
info "Test B3: list-payment-instruments (SDK)"

LIST_INST=$(aws bedrock-agentcore list-payment-instruments \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-connector-id "$CONNECTOR_ID" \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$LIST_INST" | jq . 2>/dev/null || echo "$LIST_INST"

INST_COUNT=$(echo "$LIST_INST" | jq '.paymentInstruments | length' 2>/dev/null || echo "0")
if [[ "$INST_COUNT" -ge 1 ]]; then
    success "list-payment-instruments (count: $INST_COUNT)"
    PASSED=$((PASSED+1))
else
    fail "list-payment-instruments (count: $INST_COUNT)"
fi
echo ""

# ── Test B4: create-payment-session ──────────────────────────────
sep
info "Test B4: create-payment-session (SDK)"

CREATE_SESS=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 300 \
    --limits '{"maxSpendAmount":{"value":"'"$SESSION_LIMIT_USD"'","currency":"USD"}}' \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$CREATE_SESS" | jq . 2>/dev/null || echo "$CREATE_SESS"

SESSION_ID=$(echo "$CREATE_SESS" | jq -r '.paymentSession.paymentSessionId // empty')
if [[ -n "$SESSION_ID" && "$SESSION_ID" != "null" ]]; then
    success "create-payment-session (sessionId: $SESSION_ID)"
    PASSED=$((PASSED+1))
else
    fail "create-payment-session"
    echo "  Response: $CREATE_SESS"
fi
echo ""

# ═════════════════════════════════════════════════════════════════
# Part C: Data Plane — ProcessPaymentRole (AWS CLI SDK)
# ═════════════════════════════════════════════════════════════════
banner "Part C: Data Plane — ProcessPaymentRole (AWS CLI SDK)"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
info "Assuming ProcessPaymentRole..."
assume_role "$PROCESS_PAYMENT_ROLE_ARN" "e2e-agent-$(date +%s)"
success "Assumed ProcessPaymentRole"
echo ""

# ── Test C1: process-payment ─────────────────────────────────────
sep
info "Test C1: process-payment (SDK)"

PAYMENT_INPUT=$(jq -n --arg payTo "$PAY_TO" --arg amount "$PAYMENT_AMOUNT" '{
    "cryptoX402": {
        "version": "2",
        "payload": {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": $amount,
            "maxAmountRequired": $amount,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "payTo": $payTo,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2"}
        }
    }
}')

PROCESS_PAY=$(aws bedrock-agentcore process-payment \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-session-id "$SESSION_ID" \
    --payment-instrument-id "$INSTRUMENT_ID" \
    --payment-type "CRYPTO_X402" \
    --payment-input "$PAYMENT_INPUT" \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$PROCESS_PAY" | jq . 2>/dev/null || echo "$PROCESS_PAY"

PAY_STATUS=$(echo "$PROCESS_PAY" | jq -r '.status // empty')
if [[ "$PAY_STATUS" == "PROOF_GENERATED" ]]; then
    success "process-payment (status: PROOF_GENERATED)"
    PASSED=$((PASSED+1))
else
    fail "process-payment (status: $PAY_STATUS)"
    echo "  Response: $PROCESS_PAY"
fi
echo ""

# ═════════════════════════════════════════════════════════════════
# Part D: Post-Payment Verification — ManagementRole (AWS CLI SDK)
# ═════════════════════════════════════════════════════════════════
banner "Part D: Post-Payment Verification (AWS CLI SDK)"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
info "Switching back to ManagementRole..."
assume_role "$MANAGEMENT_ROLE_ARN" "e2e-verify-$(date +%s)"
success "Assumed ManagementRole"
echo ""

# ── Test D1: get-payment-session ─────────────────────────────────
sep
info "Test D1: get-payment-session (SDK)"

GET_SESS=$(aws bedrock-agentcore get-payment-session \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-session-id "$SESSION_ID" \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$GET_SESS" | jq . 2>/dev/null || echo "$GET_SESS"

SESS_ID_CHECK=$(echo "$GET_SESS" | jq -r '.paymentSession.paymentSessionId // empty')
if [[ "$SESS_ID_CHECK" == "$SESSION_ID" ]]; then
    success "get-payment-session"
    PASSED=$((PASSED+1))
else
    fail "get-payment-session"
fi
echo ""

# ── Test D2: list-payment-sessions ───────────────────────────────
sep
info "Test D2: list-payment-sessions (SDK)"

LIST_SESS=$(aws bedrock-agentcore list-payment-sessions \
    --region "$REGION" \
    --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --user-id "$USER_ID" \
    --output json 2>&1) || true

echo "$LIST_SESS" | jq . 2>/dev/null || echo "$LIST_SESS"

SESS_COUNT=$(echo "$LIST_SESS" | jq '.paymentSessions | length' 2>/dev/null || echo "0")
if [[ "$SESS_COUNT" -ge 1 ]]; then
    success "list-payment-sessions (count: $SESS_COUNT)"
    PASSED=$((PASSED+1))
else
    fail "list-payment-sessions"
fi
echo ""

# ═════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════
TOTAL=$((PASSED + FAILURES))
banner "SDK E2E Test Results"
echo "  Total: $TOTAL  |  Passed: $PASSED  |  Failed: $FAILURES"
echo ""
echo "  Part A (CP — AWS CLI SDK):  4 tests"
echo "    A1: list-payment-managers"
echo "    A2: get-payment-manager"
echo "    A3: list-payment-connectors"
echo "    A4: get-payment-connector"
echo ""
echo "  Part B (DP — AWS CLI SDK, ManagementRole):  4 tests"
echo "    B1: create-payment-instrument"
echo "    B2: get-payment-instrument"
echo "    B3: list-payment-instruments"
echo "    B4: create-payment-session"
echo ""
echo "  Part C (DP — AWS CLI SDK, ProcessPaymentRole):  1 test"
echo "    C1: process-payment"
echo ""
echo "  Part D (DP — AWS CLI SDK, ManagementRole):  2 tests"
echo "    D1: get-payment-session"
echo "    D2: list-payment-sessions"
echo ""
echo "  All tests use the AWS CLI SDK."
echo ""

if [[ $FAILURES -eq 0 ]]; then
    success "All $TOTAL tests passed."
    echo ""
    sep
    info "blockrun-demo — paste into blockrun-demo/.env (this run’s session + instrument)"
    echo ""
    echo "  PAYMENT_SESSION_ID='${SESSION_ID}'"
    echo "  PAYMENT_INSTRUMENT_ID='${INSTRUMENT_ID}'"
    echo "  USER_ID='${USER_ID}'"
    if [[ -n "${WALLET_ADDR:-}" ]]; then
        echo ""
        echo "  Fund this payer on Base (USDC) if ProcessPayment tests need it:"
        echo "    ${WALLET_ADDR}"
    fi
    echo ""
    warn "Each e2e run creates NEW session + instrument IDs. buyer.py will fail with"
    warn "'Payment session not found' until .env matches the latest run (or an existing session)."
else
    fail "$FAILURES test(s) failed."
    exit 1
fi
