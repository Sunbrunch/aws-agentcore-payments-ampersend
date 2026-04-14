#!/usr/bin/env bash
# =============================================================
# setup_manager.sh — AgentCore Payments resource setup (CoinbaseCDP)
#
# Delegates to setup_manager.py (boto3). The previous awscurl-based
# implementation was unreliable (SigV4 scope vs host, empty {"message":null}
# bodies). The official SDK signs requests correctly.
#
# Prerequisites:
#   - bash setup_roles.sh
#   - bash setup_model.sh
#   - cp .env.sample .env  (Coinbase + endpoints)
#   - pip install boto3 python-dotenv
#
# Usage:
#   bash setup_manager.sh
#   # or:  python3 setup_manager.py
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 &>/dev/null; then
    echo "python3 not found. Install Python 3." >&2
    exit 1
fi

if ! python3 -c "import boto3, dotenv" 2>/dev/null; then
    echo "Missing dependencies. Run:" >&2
    echo "  pip install boto3 python-dotenv" >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/setup_manager.py"
