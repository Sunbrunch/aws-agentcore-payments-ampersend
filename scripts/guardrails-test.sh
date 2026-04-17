#!/usr/bin/env bash
# =============================================================
# guardrails-test.sh — Prove AgentCore Payments enforces the
# documented limits from the Private Preview guide.
#
# Each test deliberately violates a limit and asserts the API
# responds with the expected error (AccessDenied, Validation,
# BUDGET_EXCEEDED, etc.).
#
# Tests:
#   G1. ProcessPaymentRole CANNOT create a session  (role boundary)
#   G2. ManagementRole     CANNOT ProcessPayment   (role boundary)
#   G3. expiryDuration < 15 min is rejected         (validation)
#   G4. Session budget is enforced                  (BUDGET_EXCEEDED)
#   G5. Cross-user isolation                        (user mismatch)
#   G6. Idempotent clientToken                      (no double-charge)
#
# Prereqs:
#   - scripts/.env populated (same as e2e-test.sh)
#   - quickstart already run: setup_roles.sh + setup_manager.sh
#   - At least one payment instrument exists for USER_ID
#
# Usage:
#   bash scripts/guardrails-test.sh
# =============================================================

set -uo pipefail   # NOTE: no `-e` — we expect many commands to fail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${BOLD}$*${RESET}"; }
pass()    { echo -e "${GREEN}✅ $*${RESET}"; PASSED=$((PASSED+1)); }
fail()    { echo -e "${RED}❌ $*${RESET}"; FAILURES=$((FAILURES+1)); }
warn()    { echo -e "${YELLOW}⚠️  $*${RESET}"; }
sep()     { echo -e "${CYAN}$(printf '─%.0s' {1..60})${RESET}"; }
banner()  { echo -e "\n${BOLD}$(printf '=%.0s' {1..60})${RESET}"; echo -e "${BOLD}  $*${RESET}"; echo -e "${BOLD}$(printf '=%.0s' {1..60})${RESET}\n"; }

ENV_FILE="$(dirname "$0")/.env"
[[ -f "$ENV_FILE" ]] && { set -o allexport; source "$ENV_FILE"; set +o allexport; }

REGION="${AWS_REGION:-us-west-2}"
ORIG_PROFILE="${AWS_PROFILE:-}"
DP_ENDPOINT="${DP_ENDPOINT:-https://bedrock-agentcore.us-west-2.amazonaws.com}"
MANAGER_ARN="${MANAGER_ARN:?Missing MANAGER_ARN}"
CONNECTOR_ID="${CONNECTOR_ID:?Missing CONNECTOR_ID}"
MANAGEMENT_ROLE_ARN="${MANAGEMENT_ROLE_ARN:?Missing MANAGEMENT_ROLE_ARN}"
PROCESS_PAYMENT_ROLE_ARN="${PROCESS_PAYMENT_ROLE_ARN:?Missing PROCESS_PAYMENT_ROLE_ARN}"
USER_ID="${USER_ID:-test-user-12345}"
PAY_TO="${PAY_TO:-0x312554704B5c47b992e876639B144e9B85431E44}"

PASSED=0; FAILURES=0

command -v jq &>/dev/null || { echo "❌ jq not found"; exit 1; }

# ── Helpers ──────────────────────────────────────────────────────
assume_role() {
    local role_arn="$1" session_name="$2"
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    local flags=()
    [[ -n "$ORIG_PROFILE" ]] && flags=(--profile "$ORIG_PROFILE")
    local creds
    creds=$(aws sts assume-role "${flags[@]}" \
        --role-arn "$role_arn" \
        --role-session-name "$session_name" \
        --output json) || { echo "❌ assume-role failed: $role_arn"; exit 1; }
    export AWS_ACCESS_KEY_ID=$(echo "$creds" | jq -r '.Credentials.AccessKeyId')
    export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r '.Credentials.SecretAccessKey')
    export AWS_SESSION_TOKEN=$(echo "$creds" | jq -r '.Credentials.SessionToken')
}

# Return 0 if $1 contains $2, non-zero otherwise (case-insensitive).
contains() { [[ "${1,,}" == *"${2,,}"* ]]; }

x402_payload() {
    # $1 = amount (micro-USDC, string)
    local amount="$1"
    jq -n --arg payTo "$PAY_TO" --arg amount "$amount" '{
      "cryptoX402": {
        "version": "2",
        "payload": {
          "scheme": "exact",
          "network": "eip155:84532",
          "amount": $amount,
          "maxAmountRequired": $amount,
          "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
          "payTo": $payTo,
          "maxTimeoutSeconds": 300,
          "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"}
        }
      }
    }'
}

banner "AgentCore Payments — Guardrails Test"
echo "  Manager    : $MANAGER_ARN"
echo "  DP         : $DP_ENDPOINT"
echo "  UserId     : $USER_ID"
echo ""

# ── Fetch an ACTIVE instrument to use in G3/G4/G5/G6 ────────────
info "Looking up an ACTIVE payment instrument for $USER_ID ..."
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-lookup-$(date +%s)"

LIST=$(aws bedrock-agentcore list-payment-instruments \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-connector-id "$CONNECTOR_ID" \
    --user-id "$USER_ID" --output json 2>&1)
INSTRUMENT_ID=$(echo "$LIST" | jq -r '.paymentInstruments[0].paymentInstrumentId // empty')
if [[ -z "$INSTRUMENT_ID" ]]; then
    echo "$LIST"
    fail "No payment instrument found for $USER_ID. Run e2e-test.sh first."
    exit 1
fi
echo "  Using instrument: $INSTRUMENT_ID"
echo ""

# =================================================================
# G1. ProcessPaymentRole CANNOT create a session
# =================================================================
sep
info "G1. ProcessPaymentRole tries to create-payment-session (should be denied)"
assume_role "$PROCESS_PAYMENT_ROLE_ARN" "guardrails-g1-$(date +%s)"

OUT=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 60 \
    --limits '{"maxSpendAmount":{"value":"0.01","currency":"USD"}}' \
    --user-id "$USER_ID" --output json 2>&1)

if contains "$OUT" "AccessDenied" || contains "$OUT" "not authorized"; then
    pass "G1: denied as expected (AccessDenied)"
else
    echo "$OUT"
    fail "G1: expected AccessDenied, got success or wrong error"
fi
echo ""

# =================================================================
# G2. ManagementRole CANNOT call ProcessPayment
# =================================================================
sep
info "G2. ManagementRole tries to process-payment (should be denied)"
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-g2-$(date +%s)"

# First spin up a quick throwaway session to target
SESS=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 15 \
    --limits '{"maxSpendAmount":{"value":"0.01","currency":"USD"}}' \
    --user-id "$USER_ID" --output json 2>&1)
THROWAWAY_SESS=$(echo "$SESS" | jq -r '.paymentSession.paymentSessionId // empty')

OUT=$(aws bedrock-agentcore process-payment \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --payment-session-id "$THROWAWAY_SESS" \
    --payment-instrument-id "$INSTRUMENT_ID" \
    --payment-type "CRYPTO_X402" \
    --payment-input "$(x402_payload 1000)" \
    --user-id "$USER_ID" --output json 2>&1)

if contains "$OUT" "AccessDenied" || contains "$OUT" "not authorized"; then
    pass "G2: denied as expected (AccessDenied)"
else
    echo "$OUT"
    fail "G2: ManagementRole was able to call ProcessPayment (policy gap!)"
fi
echo ""

# =================================================================
# G3. expiryDuration < 15 min must be rejected
# =================================================================
sep
info "G3. create-payment-session with expiryDuration=5 (should be rejected)"
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-g3-$(date +%s)"

OUT=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 5 \
    --limits '{"maxSpendAmount":{"value":"0.01","currency":"USD"}}' \
    --user-id "$USER_ID" --output json 2>&1)

if contains "$OUT" "Validation" || contains "$OUT" "expiryDuration" || contains "$OUT" "minimum"; then
    pass "G3: rejected as expected (ValidationException)"
else
    echo "$OUT"
    fail "G3: expected ValidationException, got something else"
fi
echo ""

# =================================================================
# G4. Session budget enforced (BUDGET_EXCEEDED)
# =================================================================
sep
info "G4. Create session with \$0.0001 budget, try a \$0.001 payment (should be rejected)"
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-g4-create-$(date +%s)"

TINY_SESS=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 15 \
    --limits '{"maxSpendAmount":{"value":"0.0001","currency":"USD"}}' \
    --user-id "$USER_ID" --output json 2>&1)
TINY_SESS_ID=$(echo "$TINY_SESS" | jq -r '.paymentSession.paymentSessionId // empty')

if [[ -z "$TINY_SESS_ID" ]]; then
    echo "$TINY_SESS"
    warn "G4: could not create tiny-budget session — skipping"
else
    echo "  Session: $TINY_SESS_ID (budget \$0.0001)"
    assume_role "$PROCESS_PAYMENT_ROLE_ARN" "guardrails-g4-pay-$(date +%s)"

    # 1000 micro-USDC = $0.001 — 10x the session budget
    OUT=$(aws bedrock-agentcore process-payment \
        --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
        --payment-manager-arn "$MANAGER_ARN" \
        --payment-session-id "$TINY_SESS_ID" \
        --payment-instrument-id "$INSTRUMENT_ID" \
        --payment-type "CRYPTO_X402" \
        --payment-input "$(x402_payload 1000)" \
        --user-id "$USER_ID" --output json 2>&1)

    if contains "$OUT" "BUDGET_EXCEEDED" || contains "$OUT" "budget" || \
       contains "$OUT" "LimitExceeded" || contains "$OUT" "exceed"; then
        pass "G4: rejected as expected (budget exceeded)"
    else
        echo "$OUT"
        fail "G4: budget NOT enforced — payment was allowed"
    fi
fi
echo ""

# =================================================================
# G5. Cross-user isolation
# =================================================================
sep
info "G5. userA's session, called by userB (should be rejected)"
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-g5-create-$(date +%s)"

USER_A="$USER_ID"
USER_B="guardrails-other-user-$(date +%s)"

SESS_A=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 15 \
    --limits '{"maxSpendAmount":{"value":"0.01","currency":"USD"}}' \
    --user-id "$USER_A" --output json 2>&1)
SESS_A_ID=$(echo "$SESS_A" | jq -r '.paymentSession.paymentSessionId // empty')

if [[ -z "$SESS_A_ID" ]]; then
    echo "$SESS_A"
    warn "G5: could not create session for userA — skipping"
else
    assume_role "$PROCESS_PAYMENT_ROLE_ARN" "guardrails-g5-pay-$(date +%s)"
    OUT=$(aws bedrock-agentcore process-payment \
        --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
        --payment-manager-arn "$MANAGER_ARN" \
        --payment-session-id "$SESS_A_ID" \
        --payment-instrument-id "$INSTRUMENT_ID" \
        --payment-type "CRYPTO_X402" \
        --payment-input "$(x402_payload 1000)" \
        --user-id "$USER_B" --output json 2>&1)

    if contains "$OUT" "AccessDenied" || contains "$OUT" "ResourceNotFound" || \
       contains "$OUT" "not authorized" || contains "$OUT" "does not belong" || \
       contains "$OUT" "Validation"; then
        pass "G5: cross-user call rejected"
    else
        echo "$OUT"
        fail "G5: userB was able to use userA's session (isolation gap!)"
    fi
fi
echo ""

# =================================================================
# G6. Idempotent clientToken
# =================================================================
sep
info "G6. Same clientToken twice on process-payment → same response"
assume_role "$MANAGEMENT_ROLE_ARN" "guardrails-g6-create-$(date +%s)"

IDEM_SESS=$(aws bedrock-agentcore create-payment-session \
    --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
    --payment-manager-arn "$MANAGER_ARN" \
    --expiry-duration 15 \
    --limits '{"maxSpendAmount":{"value":"1.0","currency":"USD"}}' \
    --user-id "$USER_ID" --output json 2>&1)
IDEM_SESS_ID=$(echo "$IDEM_SESS" | jq -r '.paymentSession.paymentSessionId // empty')

if [[ -z "$IDEM_SESS_ID" ]]; then
    echo "$IDEM_SESS"
    warn "G6: could not create session — skipping"
else
    CLIENT_TOKEN="guardrails-idem-$(date +%s)-$(python3 -c 'import uuid;print(uuid.uuid4())')"
    assume_role "$PROCESS_PAYMENT_ROLE_ARN" "guardrails-g6-pay-$(date +%s)"

    OUT1=$(aws bedrock-agentcore process-payment \
        --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
        --payment-manager-arn "$MANAGER_ARN" \
        --payment-session-id "$IDEM_SESS_ID" \
        --payment-instrument-id "$INSTRUMENT_ID" \
        --payment-type "CRYPTO_X402" \
        --payment-input "$(x402_payload 1000)" \
        --client-token "$CLIENT_TOKEN" \
        --user-id "$USER_ID" --output json 2>&1)

    OUT2=$(aws bedrock-agentcore process-payment \
        --region "$REGION" --endpoint-url "$DP_ENDPOINT" \
        --payment-manager-arn "$MANAGER_ARN" \
        --payment-session-id "$IDEM_SESS_ID" \
        --payment-instrument-id "$INSTRUMENT_ID" \
        --payment-type "CRYPTO_X402" \
        --payment-input "$(x402_payload 1000)" \
        --client-token "$CLIENT_TOKEN" \
        --user-id "$USER_ID" --output json 2>&1)

    ID1=$(echo "$OUT1" | jq -r '.processPaymentId // empty')
    ID2=$(echo "$OUT2" | jq -r '.processPaymentId // empty')
    SIG1=$(echo "$OUT1" | jq -r '.paymentOutput.cryptoX402.payload.signature // empty')
    SIG2=$(echo "$OUT2" | jq -r '.paymentOutput.cryptoX402.payload.signature // empty')

    if [[ -n "$ID1" && "$ID1" == "$ID2" && -n "$SIG1" && "$SIG1" == "$SIG2" ]]; then
        pass "G6: idempotent — same processPaymentId and signature on retry"
        echo "    processPaymentId: $ID1"
    else
        echo "--- call 1 ---"; echo "$OUT1"
        echo "--- call 2 ---"; echo "$OUT2"
        fail "G6: second call produced a different payment (double-charge risk)"
    fi
fi
echo ""

# =================================================================
# Summary
# =================================================================
TOTAL=$((PASSED + FAILURES))
banner "Guardrails Results"
echo "  Total: $TOTAL  |  Passed: $PASSED  |  Failed: $FAILURES"
echo ""
echo "  G1  ProcessPaymentRole → CreatePaymentSession should be denied"
echo "  G2  ManagementRole     → ProcessPayment        should be denied"
echo "  G3  expiryDuration < 15 min                     should be rejected"
echo "  G4  Session budget (maxSpendAmount)             should be enforced"
echo "  G5  Cross-user session use                      should be denied"
echo "  G6  Same clientToken                            should be idempotent"
echo ""

if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}✅ All guardrails enforced.${RESET}"
    exit 0
else
    echo -e "${RED}❌ $FAILURES guardrail(s) NOT enforced. Review above.${RESET}"
    exit 1
fi
