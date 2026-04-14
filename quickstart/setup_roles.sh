#!/usr/bin/env bash
# =============================================================
# setup_roles.sh — Create IAM roles for AgentCore Payments
#
# Creates 4 IAM roles:
#   - AgentCorePaymentsControlPlaneRole    (CP: managers, connectors, credential providers + iam:PassRole)
#   - AgentCorePaymentsManagementRole      (DP management: instruments, sessions - no ProcessPayment)
#   - AgentCorePaymentsProcessPaymentRole  (DP: ProcessPayment only)
#   - AgentCorePaymentsResourceRetrievalRole (service role: token-vault + workload-identity)
#
# Requirements:
#   - aws CLI with IAM permissions (Admin or IAM-capable role)
#   - jq
#
# Usage:
#   bash setup_roles.sh
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

# ── Dependency checks ────────────────────────────────────────────
if ! command -v aws &>/dev/null; then
    die "aws CLI not found. Install from: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
fi
if ! command -v jq &>/dev/null; then
    die "jq not found. Install with: brew install jq  OR  sudo yum install jq  OR  sudo apt install jq"
fi

# ── Verify AWS credentials ──────────────────────────────────────
CALLER_IDENTITY=$(aws sts get-caller-identity 2>&1) || \
    die "AWS credentials not configured or expired.\nRun: aws configure  OR  ada credentials update --account <id> --provider isengard --role Admin\nError: $CALLER_IDENTITY"
ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | jq -r '.Account')

# ── Fixed IAM role names ─────────────────────────────────────────
CONTROL_PLANE_ROLE_NAME="AgentCorePaymentsControlPlaneRole"
MANAGEMENT_ROLE_NAME="AgentCorePaymentsManagementRole"
PROCESS_PAYMENT_ROLE_NAME="AgentCorePaymentsProcessPaymentRole"
RESOURCE_RETRIEVAL_ROLE_NAME="AgentCorePaymentsResourceRetrievalRole"

echo ""
sep
info "  AgentCore Payments — IAM Role Setup"
sep
echo "  Account : $ACCOUNT_ID"
sep
echo ""

# ── Trust policies ───────────────────────────────────────────────
# Detect caller's IAM role to add to trust policy
CALLER_ARN=$(echo "$CALLER_IDENTITY" | jq -r '.Arn')
CALLER_ROLE_ARN=""
if [[ "$CALLER_ARN" == *":assumed-role/"* ]]; then
    CALLER_ROLE_NAME=$(echo "$CALLER_ARN" | sed 's/.*:assumed-role\///' | cut -d/ -f1)
    CALLER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${CALLER_ROLE_NAME}"
fi

if [[ -n "$CALLER_ROLE_ARN" ]]; then
    CLIENT_TRUST_POLICY=$(jq -n --arg accountId "$ACCOUNT_ID" --arg callerRole "$CALLER_ROLE_ARN" '{
        Version: "2012-10-17",
        Statement: [
            {
                Sid: "AllowAccountAssume",
                Effect: "Allow",
                Principal: { AWS: [("arn:aws:iam::" + $accountId + ":root"), $callerRole] },
                Action: "sts:AssumeRole"
            }
        ]
    }')
else
    CLIENT_TRUST_POLICY=$(jq -n --arg accountId "$ACCOUNT_ID" '{
        Version: "2012-10-17",
        Statement: [
            {
                Sid: "AllowAccountAssume",
                Effect: "Allow",
                Principal: { AWS: ("arn:aws:iam::" + $accountId + ":root") },
                Action: "sts:AssumeRole"
            }
        ]
    }')
fi

SERVICE_TRUST_POLICY=$(jq -n '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "AllowAccessToBedrockAgentcore",
            Effect: "Allow",
            Principal: { Service: "bedrock-agentcore.amazonaws.com" },
            Action: "sts:AssumeRole"
        },
        {
            Sid: "AllowPreprodAccessToBedrockAgentcore",
            Effect: "Allow",
            Principal: { Service: "preprod.genesis-service.aws.internal" },
            Action: "sts:AssumeRole"
        }
    ]
}')

# ── Helper: create-or-update an IAM role ─────────────────────────
upsert_role() {
    local role_name="$1" description="$2" trust_policy="$3"

    ROLE_RESPONSE=$(aws iam create-role \
        --role-name "$role_name" \
        --assume-role-policy-document "$trust_policy" \
        --description "$description" \
        2>&1) || {
            if echo "$ROLE_RESPONSE" | grep -q "EntityAlreadyExists"; then
                warn "  Role '$role_name' already exists - updating."
            else
                die "Failed to create IAM role '$role_name': $ROLE_RESPONSE"
            fi
        }
    aws iam update-assume-role-policy \
        --role-name "$role_name" \
        --policy-document "$trust_policy"
}

# ── 1. AgentCorePaymentsControlPlaneRole ─────────────────────────
info "[1/4] Creating '$CONTROL_PLANE_ROLE_NAME' ..."

upsert_role "$CONTROL_PLANE_ROLE_NAME" \
    "AgentCore Payments: control plane operations (managers, connectors, credential providers)" \
    "$CLIENT_TRUST_POLICY"

aws iam attach-role-policy \
    --role-name "$CONTROL_PLANE_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess" 2>/dev/null || \
    warn "  Could not attach BedrockAgentCoreFullAccess (may not exist in this partition)"

CP_ALLOW_POLICY=$(jq -n '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "AllowControlPlaneOperations",
            Effect: "Allow",
            Action: [
                "bedrock-agentcore:CreatePaymentManager",
                "bedrock-agentcore:GetPaymentManager",
                "bedrock-agentcore:ListPaymentManagers",
                "bedrock-agentcore:DeletePaymentManager",
                "bedrock-agentcore:UpdatePaymentManager",
                "bedrock-agentcore:CreatePaymentConnector",
                "bedrock-agentcore:GetPaymentConnector",
                "bedrock-agentcore:ListPaymentConnectors",
                "bedrock-agentcore:DeletePaymentConnector",
                "bedrock-agentcore:UpdatePaymentConnector",
                "bedrock-agentcore:CreatePaymentCredentialProvider",
                "bedrock-agentcore:GetPaymentCredentialProvider",
                "bedrock-agentcore:ListPaymentCredentialProviders",
                "bedrock-agentcore:DeletePaymentCredentialProvider",
                "bedrock-agentcore:UpdatePaymentCredentialProvider"
            ],
            Resource: "*"
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$CONTROL_PLANE_ROLE_NAME" \
    --policy-name "AllowControlPlaneOperations" \
    --policy-document "$CP_ALLOW_POLICY"

PASS_ROLE_POLICY=$(jq -n --arg accountId "$ACCOUNT_ID" --arg rrRole "$RESOURCE_RETRIEVAL_ROLE_NAME" '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "AllowPassResourceRetrievalRole",
            Effect: "Allow",
            Action: "iam:PassRole",
            Resource: ("arn:aws:iam::" + $accountId + ":role/" + $rrRole)
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$CONTROL_PLANE_ROLE_NAME" \
    --policy-name "AllowPassRole" \
    --policy-document "$PASS_ROLE_POLICY"

CONTROL_PLANE_ROLE_ARN=$(aws iam get-role --role-name "$CONTROL_PLANE_ROLE_NAME" --query 'Role.Arn' --output text)
success "  $CONTROL_PLANE_ROLE_NAME: $CONTROL_PLANE_ROLE_ARN"

# ── 2. AgentCorePaymentsManagementRole ───────────────────────────
info "[2/4] Creating '$MANAGEMENT_ROLE_NAME' ..."

upsert_role "$MANAGEMENT_ROLE_NAME" \
    "AgentCore Payments: data plane management (instruments, sessions) - no ProcessPayment" \
    "$CLIENT_TRUST_POLICY"

MANAGEMENT_ALLOW_POLICY=$(jq -n '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "AllowPaymentManagement",
            Effect: "Allow",
            Action: [
                "bedrock-agentcore:CreatePaymentInstrument",
                "bedrock-agentcore:GetPaymentInstrument",
                "bedrock-agentcore:ListPaymentInstruments",
                "bedrock-agentcore:DeletePaymentInstrument",
                "bedrock-agentcore:CreatePaymentSession",
                "bedrock-agentcore:GetPaymentSession",
                "bedrock-agentcore:ListPaymentSessions",
                "bedrock-agentcore:UpdatePaymentSession"
            ],
            Resource: "*"
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$MANAGEMENT_ROLE_NAME" \
    --policy-name "AllowPaymentManagement" \
    --policy-document "$MANAGEMENT_ALLOW_POLICY"

MANAGEMENT_DENY_POLICY=$(jq -n '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "DenyProcessPayment",
            Effect: "Deny",
            Action: "bedrock-agentcore:ProcessPayment",
            Resource: "*"
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$MANAGEMENT_ROLE_NAME" \
    --policy-name "DenyProcessPayment" \
    --policy-document "$MANAGEMENT_DENY_POLICY"

MANAGEMENT_ROLE_ARN=$(aws iam get-role --role-name "$MANAGEMENT_ROLE_NAME" --query 'Role.Arn' --output text)
success "  $MANAGEMENT_ROLE_NAME: $MANAGEMENT_ROLE_ARN"

# ── 3. AgentCorePaymentsProcessPaymentRole ───────────────────────
info "[3/4] Creating '$PROCESS_PAYMENT_ROLE_NAME' ..."

upsert_role "$PROCESS_PAYMENT_ROLE_NAME" \
    "AgentCore Payments: ProcessPayment only" \
    "$CLIENT_TRUST_POLICY"

PROCESS_PAYMENT_POLICY=$(jq -n '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "AllowProcessPayment",
            Effect: "Allow",
            Action: "bedrock-agentcore:ProcessPayment",
            Resource: "*"
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$PROCESS_PAYMENT_ROLE_NAME" \
    --policy-name "AllowProcessPayment" \
    --policy-document "$PROCESS_PAYMENT_POLICY"

PROCESS_PAYMENT_ROLE_ARN=$(aws iam get-role --role-name "$PROCESS_PAYMENT_ROLE_NAME" --query 'Role.Arn' --output text)
success "  $PROCESS_PAYMENT_ROLE_NAME: $PROCESS_PAYMENT_ROLE_ARN"

# ── 4. AgentCorePaymentsResourceRetrievalRole ────────────────────
info "[4/4] Creating '$RESOURCE_RETRIEVAL_ROLE_NAME' ..."

upsert_role "$RESOURCE_RETRIEVAL_ROLE_NAME" \
    "AgentCore Payments: resource retrieval for payment processing" \
    "$SERVICE_TRUST_POLICY"

RESOURCE_RETRIEVAL_POLICY=$(jq -n --arg accountId "$ACCOUNT_ID" '{
    Version: "2012-10-17",
    Statement: [
        {
            Sid: "BedrockAgentCoreGetResourcePaymentToken",
            Effect: "Allow",
            Action: [
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:CreateWorkloadIdentity",
                "bedrock-agentcore:GetResourcePaymentToken"
            ],
            Resource: [
                ("arn:aws:bedrock-agentcore:*:" + $accountId + ":token-vault/default"),
                ("arn:aws:bedrock-agentcore:*:" + $accountId + ":token-vault/default/paymentcredentialprovider/*"),
                ("arn:aws:bedrock-agentcore:*:" + $accountId + ":token-vault/default/*"),
                ("arn:aws:bedrock-agentcore:*:" + $accountId + ":workload-identity-directory/default"),
                ("arn:aws:bedrock-agentcore:*:" + $accountId + ":workload-identity-directory/default/workload-identity/*")
            ]
        },
        {
            Sid: "SecretsManagerAccess",
            Effect: "Allow",
            Action: ["secretsmanager:GetSecretValue"],
            Resource: ("arn:aws:secretsmanager:*:" + $accountId + ":secret:*")
        },
        {
            Sid: "StsAddTokenContext",
            Effect: "Allow",
            Action: "sts:SetContext",
            Resource: ("arn:aws:sts::" + $accountId + ":self")
        }
    ]
}')

aws iam put-role-policy \
    --role-name "$RESOURCE_RETRIEVAL_ROLE_NAME" \
    --policy-name "AgentCorePaymentsResourceRetrievalPolicy" \
    --policy-document "$RESOURCE_RETRIEVAL_POLICY"

RESOURCE_RETRIEVAL_ROLE_ARN=$(aws iam get-role --role-name "$RESOURCE_RETRIEVAL_ROLE_NAME" --query 'Role.Arn' --output text)
success "  $RESOURCE_RETRIEVAL_ROLE_NAME: $RESOURCE_RETRIEVAL_ROLE_ARN"

# ── Summary ──────────────────────────────────────────────────────
echo ""
sep
success "  IAM roles created successfully!"
sep
echo "  $CONTROL_PLANE_ROLE_NAME      : $CONTROL_PLANE_ROLE_ARN"
echo "  $MANAGEMENT_ROLE_NAME         : $MANAGEMENT_ROLE_ARN"
echo "  $PROCESS_PAYMENT_ROLE_NAME    : $PROCESS_PAYMENT_ROLE_ARN"
echo "  $RESOURCE_RETRIEVAL_ROLE_NAME : $RESOURCE_RETRIEVAL_ROLE_ARN"
sep
echo ""
echo "  Next step: bash setup_manager.sh"
echo ""