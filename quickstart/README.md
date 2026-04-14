# AgentCore Payments — Setup Guide (Control Plane + Data Plane)

Sets up an AgentCore Payments stack (CoinbaseCDP) in your AWS account:

1. **IAM Roles** (4 roles) — created via `setup_roles.sh`
2. **PaymentCredentialProvider** — stores your Coinbase CDP API keys in AWS Secrets Manager
3. **PaymentManager** — creates an AWS_IAM-authorized payment manager
4. **PaymentConnector** — links the manager to the credential provider

---

## IAM Roles

Created by `setup_roles.sh`. Role names are fixed and not configurable.

| Role | Inline Policy | Purpose |
|------|--------------|---------|
| `AgentCorePaymentsControlPlaneRole` | Allow CP operations (Create/Get/List/Delete/Update Manager, Connector, CredentialProvider) + `iam:PassRole` for ResourceRetrievalRole | Control plane management: managers, connectors, credential providers |
| `AgentCorePaymentsManagementRole` | Allow DP management (Create/Get/List/Delete Instrument, Create/Get/List/Update Session) + **Deny** `ProcessPayment` | Data plane management: instruments and sessions with human oversight. Cannot process payments. |
| `AgentCorePaymentsProcessPaymentRole` | Allow `bedrock-agentcore:ProcessPayment` | Data plane execution: process payments only. For deterministic code paths, not direct LLM access. |
| `AgentCorePaymentsResourceRetrievalRole` | Allow token-vault + workload-identity access, `secretsmanager:GetSecretValue`, `sts:SetContext` | Service role assumed by AgentCore Payments at runtime |

### Trust Policies

**Roles 1-3** (client roles) — assumed by your AWS account:
```json
{
  "Principal": { "AWS": "arn:aws:iam::<accountId>:root" },
  "Action": "sts:AssumeRole"
}
```

**Role 4** (service role) — assumed by AgentCore Payments service:
```json
{
  "Statement": [
    { "Principal": { "Service": "bedrock-agentcore.amazonaws.com" }, "Action": "sts:AssumeRole" },
    { "Principal": { "Service": "preprod.genesis-service.aws.internal" }, "Action": "sts:AssumeRole" }
  ]
}
```

### Merged Policy Details

**ControlPlaneRole** (single inline policy):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowControlPlaneOperations",
      "Effect": "Allow",
      "Action": [
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
      "Resource": "*"
    },
    {
      "Sid": "AllowPassResourceRetrievalRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<accountId>:role/AgentCorePaymentsResourceRetrievalRole"
    }
  ]
}
```

**ManagementRole** (single inline policy):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowPaymentManagement",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreatePaymentInstrument",
        "bedrock-agentcore:GetPaymentInstrument",
        "bedrock-agentcore:ListPaymentInstruments",
        "bedrock-agentcore:DeletePaymentInstrument",
        "bedrock-agentcore:CreatePaymentSession",
        "bedrock-agentcore:GetPaymentSession",
        "bedrock-agentcore:ListPaymentSessions",
        "bedrock-agentcore:UpdatePaymentSession"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyProcessPayment",
      "Effect": "Deny",
      "Action": "bedrock-agentcore:ProcessPayment",
      "Resource": "*"
    }
  ]
}
```

**ProcessPaymentRole**:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowProcessPayment",
    "Effect": "Allow",
    "Action": "bedrock-agentcore:ProcessPayment",
    "Resource": "*"
  }]
}
```

**ResourceRetrievalRole**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAgentCoreGetResourcePaymentToken",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:GetResourcePaymentToken"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:*:<accountId>:token-vault/default",
        "arn:aws:bedrock-agentcore:*:<accountId>:token-vault/default/paymentcredentialprovider/*",
        "arn:aws:bedrock-agentcore:*:<accountId>:token-vault/default/*",
        "arn:aws:bedrock-agentcore:*:<accountId>:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:*:<accountId>:workload-identity-directory/default/workload-identity/*"
      ]
    },
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:*:<accountId>:secret:*"
    },
    {
      "Sid": "StsAddTokenContext",
      "Effect": "Allow",
      "Action": "sts:SetContext",
      "Resource": "arn:aws:sts::<accountId>:self"
    }
  ]
}
```

---

## Prerequisites

- `awscurl` — signs AWS API requests: `pip install awscurl`
- `jq` — parses JSON responses: `brew install jq` / `sudo yum install jq` / `sudo apt install jq`
- **AWS credentials** — run `aws sts get-caller-identity` to verify
- Your AWS account must be allowlisted for AgentCore Payments APIs

---

## Quick Start

### Step 1 — Configure credentials

```bash
cp .env.sample .env
# Edit .env and fill in your Coinbase CDP credentials
```

| Variable | Required | Description |
|---|---|---|
| `COINBASE_API_KEY_ID` | Yes | Coinbase CDP API key ID |
| `COINBASE_API_KEY_SECRET` | Yes | Coinbase CDP API key secret |
| `COINBASE_WALLET_SECRET` | Yes | Coinbase CDP wallet secret |
| `DEFAULT_PAYMENT_MANAGER_NAME` | No | Manager name — `[a-zA-Z][a-zA-Z0-9_]{0,47}` (auto-generated if blank) |
| `DEFAULT_PAYMENT_CONNECTOR_NAME` | No | Connector name — same pattern (auto-generated if blank) |

### Step 2 — Create IAM roles (one-time)

```bash
bash setup_roles.sh
```

### Step 3 — Create payment resources

```bash
bash setup_manager.sh
```

Output:
```
============================================================
  Setup complete!
============================================================
  credentialProviderArn : arn:aws:bedrock-agentcore:us-west-2:<account>:token-vault/...
  paymentManagerArn     : arn:aws:bedrock-agentcore:us-west-2:<account>:payment-manager/...
  paymentManagerId      : mymanager-xxxxxxxxxx
  paymentConnectorId    : myconnector-xxxxxxxxxx
============================================================
```

---

## Alternative: Python

```bash
bash setup_model.sh              # one-time: install botocore service model
pip install boto3 python-dotenv  # one-time
bash setup_roles.sh              # one-time
python3 setup_manager.py
```

---

## File Reference

| File | Purpose |
|---|---|
| `setup_roles.sh` | Creates/updates the 4 IAM roles (one-time, requires IAM permissions) |
| `setup_manager.sh` | Creates credential provider, manager, and connector (bash + awscurl) |
| `setup_manager.py` | Same as above but Python (requires boto3 + `setup_model.sh`) |
| `setup_model.sh` | Installs bundled service models into `~/.aws/models` (needed for Python only) |
| `model/bedrock-agentcore-control-2023-06-05.normal.json` | Bundled Control Plane service model |
| `model/bedrock-agentcore-2024-02-28.normal.json` | Bundled Data Plane service model |
| `manual/PaymentsCpSpecPaymentManager.html` | Control Plane API spec for PaymentManager, Connector, CredentialProvider |
| `manual/PaymentsDpSpecPaymentManager.html` | Data Plane API spec for PaymentInstrument, PaymentSession, ProcessPayment, GetResourcePaymentToken |
| `.env.sample` | Template for credentials — copy to `.env` |
| `.env` | Your actual credentials (git-ignored) |