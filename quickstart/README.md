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

- **Python 3** with `boto3` + `python-dotenv` — for `setup_manager.py` / `setup_manager.sh`
  - On **Homebrew Python** (and other PEP 668 environments), use a **virtualenv** — do not `pip install` system-wide:
    ```bash
    cd ..   # repo root: agentcore-payments-beta-main
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r quickstart/requirements.txt
    ```
  - Then run quickstart commands with this venv activated (or use `.venv/bin/python` / `.venv/bin/pip` explicitly).
- `jq` — parses JSON responses: `brew install jq` / `sudo yum install jq` / `sudo apt install jq`
- **AWS credentials** — `aws sts get-caller-identity` must show the account you intend to onboard. **AgentCore Payments preview is allowlisted per account** — if you have several accounts (e.g. org management vs. workload), use the profile for the **allowlisted** account (see *SSO: pick the right account* below)
- **IAM permissions** — `setup_roles.sh` needs `iam:CreateRole` (and related). Use an **Administrator** or **IAM-full** profile, not a read-only role. With **IAM Identity Center**, use `aws configure sso`, pick the **target account**, and `export AWS_PROFILE=...` before running scripts
- **Preview allowlist** — Your AWS account must be allowlisted for AgentCore Payments. If `setup_manager` fails with **`UnknownOperationException`**, Payments APIs are not enabled for that account/region — contact your AWS representative. Run all setup in the **allowlisted** account (not only the org management account unless that account is onboarded)
- **`setup_roles.sh` and SSO** — The script adds your caller IAM role to each client role’s trust policy. For SSO permission-set roles, the script resolves the **canonical role ARN** via `iam:GetRole` (roles with a path under `aws-reserved/sso.amazonaws.com/...` cannot use a short `role/Name-only` ARN)

---

## Quick Start

### Step 0 — Botocore service models (Python / boto3)

Installs bundled Control + Data Plane models into `~/.aws/models` so `bedrock-agentcore-control` and `bedrock-agentcore` expose Payment APIs.

```bash
bash setup_model.sh
```

Requires `boto3` only for the optional verification step at the end (install the venv first, or the script still copies models and exits successfully).

### Step 1 — Configure credentials

```bash
cp .env.sample .env
# Edit .env and fill in your Coinbase CDP credentials
```

| Variable | Required | Description |
|---|---|---|
| `COINBASE_API_KEY_ID` | Yes | Coinbase CDP API key ID |
| `COINBASE_API_KEY_SECRET` | Yes | Coinbase CDP API key secret |
| `COINBASE_WALLET_SECRET` | Yes | Coinbase CDP wallet secret (Server Wallets → Generate secret — not the same screen as API keys) |
| `DEFAULT_PAYMENT_MANAGER_NAME` | No | Manager name — `[a-zA-Z][a-zA-Z0-9_]{0,47}` (auto-generated if blank) |
| `DEFAULT_PAYMENT_CONNECTOR_NAME` | No | Connector name — same pattern (auto-generated if blank) |
| `CREDENTIAL_PROVIDER_ENDPOINT` | No | Optional. Omit or comment out to let boto3 use the default regional URL for `bedrock-agentcore-control` |
| `PAYMENTS_CP_ENDPOINT` | No | Same — optional override for control-plane URL |

Quote values in `.env` that contain `+`, `=`, or `/` (e.g. `COINBASE_API_KEY_SECRET='...'`).

### Step 2 — Create IAM roles (one-time)

```bash
bash setup_roles.sh
```

### Step 3 — Create payment resources

With the repo **venv** activated (`pip install -r quickstart/requirements.txt` from repo root):

```bash
bash setup_manager.sh
```

This runs **`setup_manager.py`** (boto3). Do not rely on system Python without a venv on Homebrew Python (PEP 668).

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

## Alternative: Python (same as `setup_manager.sh`)

```bash
# From repo root, with .venv activated (see Prerequisites)
bash quickstart/setup_model.sh   # one-time: install botocore service model
bash quickstart/setup_roles.sh   # one-time
python3 quickstart/setup_manager.py
```

---

## File Reference

| File | Purpose |
|---|---|
| `setup_roles.sh` | Creates/updates the 4 IAM roles (one-time, requires IAM permissions) |
| `setup_manager.sh` | Creates credential provider, manager, and connector (runs `setup_manager.py` / boto3) |
| `setup_manager.py` | Same as above but Python (requires boto3 + `setup_model.sh`) |
| `setup_model.sh` | Installs bundled service models into `~/.aws/models` (needed for Python only) |
| `requirements.txt` | `boto3`, `python-dotenv` — install into a venv (see Prerequisites) |
| `model/bedrock-agentcore-control-2023-06-05.normal.json` | Bundled Control Plane service model |
| `model/bedrock-agentcore-2024-02-28.normal.json` | Bundled Data Plane service model |
| `manual/PaymentsCpSpecPaymentManager.html` | Control Plane API spec for PaymentManager, Connector, CredentialProvider |
| `manual/PaymentsDpSpecPaymentManager.html` | Data Plane API spec for PaymentInstrument, PaymentSession, ProcessPayment, GetResourcePaymentToken |
| `.env.sample` | Template for credentials — copy to `.env` |
| `.env` | Your actual credentials (git-ignored) |

---

## SSO: pick the right account

IAM Identity Center often lists **multiple accounts**. A profile configured for account `A` will create IAM roles and Bedrock resources **in account `A` only**.

1. Run `aws sts get-caller-identity` — the `"Account"` field must match the account where Payments preview was granted (e.g. your workload account).
2. If it shows a different account, add another profile: `aws configure sso`, choose the **correct account ID** in the portal, assign **AdministratorAccess** (or equivalent), name the profile e.g. `payments-workload`.
3. `export AWS_PROFILE=payments-workload` and re-run `setup_roles.sh` / `setup_manager.sh` in that account.

If roles were already created only in the wrong account, run **`setup_roles.sh`** again after switching profiles so the four roles exist in the allowlisted account (role names are the same; ARNs will contain the other account ID).

---

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| `AccessDenied` on `iam:CreateRole` | Use a profile with IAM admin; not every SSO role can create roles |
| `MalformedPolicyDocument` / invalid principal when creating roles | SSO roles need the full IAM ARN — `setup_roles.sh` uses `iam:GetRole` to resolve it. Ensure `setup_roles.sh` is current |
| `UnknownOperationException` on `CreatePaymentCredentialProvider` (or other Payment CP calls) | Preview not enabled for this **account** and **region**, or wrong account (e.g. org payer vs workload account). Ask AWS to confirm allowlisting |
| `ModuleNotFoundError: boto3` / PEP 668 pip errors | Create repo-root `.venv`, `pip install -r quickstart/requirements.txt`, activate before `setup_model.sh` verification and `setup_manager.sh` |
| Coinbase `.env` parse errors in bash | Single-quote secrets that contain `+`, `=`, `/` |
| `syntax error near unexpected token newline` when **sourcing** `.env` | Lines like `VAR=<placeholder>` are parsed as **shell redirection**, not assignment. Use `VAR='value'` or `VAR="value"` (see `.env.sample` files) |