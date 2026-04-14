#!/usr/bin/env bash
# =============================================================
# setup_model.sh
#
# Installs the bundled service models into ~/.aws/models so that
# boto3/botocore and AWS CLI recognise the Payment APIs.
#
# Models installed:
#   - bedrock-agentcore-control (Control Plane)
#   - bedrock-agentcore        (Data Plane)
#
# Run once before using setup_manager.py / boto3.
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="$SCRIPT_DIR/model"

# ── Control Plane Model ──────────────────────────────────────────
CP_MODEL_FILE="$MODEL_DIR/bedrock-agentcore-control-2023-06-05.normal.json"
CP_DEST_DIR="$HOME/.aws/models/bedrock-agentcore-control/2023-06-05"

if [[ ! -f "$CP_MODEL_FILE" ]]; then
    echo "❌ Control Plane model not found: $CP_MODEL_FILE" >&2
    exit 1
fi

echo "Installing Control Plane model..."
mkdir -p "$CP_DEST_DIR"
cp "$CP_MODEL_FILE" "$CP_DEST_DIR/service-2.json"
echo "  ✅ Installed at: $CP_DEST_DIR/service-2.json"

# ── Data Plane Model ────────────────────────────────────────────
DP_MODEL_FILE="$MODEL_DIR/bedrock-agentcore-2024-02-28.normal.json"
DP_DEST_DIR="$HOME/.aws/models/bedrock-agentcore/2024-02-28"

if [[ ! -f "$DP_MODEL_FILE" ]]; then
    echo "❌ Data Plane model not found: $DP_MODEL_FILE" >&2
    exit 1
fi

echo "Installing Data Plane model..."
mkdir -p "$DP_DEST_DIR"
cp "$DP_MODEL_FILE" "$DP_DEST_DIR/service-2.json"
echo "  ✅ Installed at: $DP_DEST_DIR/service-2.json"

# ── Verification (optional; requires boto3) ─────────────────────
echo ""
echo "Verifying Payment operations are available:"
if ! python3 -c "import boto3" 2>/dev/null; then
    echo "  ⚠️  boto3 not installed — skipping Python check."
    echo "     Models are installed under ~/.aws/models (this step succeeded)."
    echo "     Install boto3 before setup_manager:  pip install boto3 python-dotenv"
    exit 0
fi
python3 -c "
import boto3

# Control Plane
c = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
cp_ops = [m for m in dir(c) if 'payment' in m.lower()]
print('  Control Plane payment methods:', cp_ops)
if 'create_payment_manager' in cp_ops:
    print('  ✅ Control Plane model loaded')
else:
    print('  ❌ Control Plane model NOT loaded correctly')

# Data Plane
d = boto3.client('bedrock-agentcore', region_name='us-west-2')
dp_ops = [m for m in dir(d) if 'payment' in m.lower()]
print('  Data Plane payment methods:', dp_ops)
if 'process_payment' in dp_ops:
    print('  ✅ Data Plane model loaded')
else:
    print('  ❌ Data Plane model NOT loaded correctly')
"