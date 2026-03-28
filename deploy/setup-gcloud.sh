#!/usr/bin/env bash
# setup-gcloud.sh — Copy the active gcloud service-account credential
#                   to a remote VM, activate it, then clean up.
# Usage: ./setup-gcloud.sh <remote-vm-ip>
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[90m'
NC='\033[0m'

step()  { echo -e "${CYAN}$1${NC}  $2"; }
ok()    { echo -e "   ${GREEN}✅ $*${NC}"; }
error() { echo -e "${RED}❌ $*${NC}"; exit 1; }

# ── Args ──────────────────────────────────────────────────────────────────────
REMOTE_IP="${1:-}"
[ -z "$REMOTE_IP" ] && error "Usage: $(basename "$0") <remote-vm-ip>"

REMOTE_USER="${REMOTE_USER:-$(whoami)}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"
REMOTE_TMP="/tmp/adc.json"

# ── Step 1: Detect the active gcloud account ──────────────────────────────────
step "1️⃣" "Detecting active gcloud account..."
ACTIVE_ACCOUNT=$(gcloud auth list \
    --filter="status:ACTIVE" \
    --format="value(account)" 2>/dev/null | head -1)

[ -z "$ACTIVE_ACCOUNT" ] && error "No active gcloud account found. Run 'gcloud auth login' first."
ok "Active account: ${ACTIVE_ACCOUNT}"

# ── Step 2: Locate the local ADC key file ─────────────────────────────────────
step "2️⃣" "Locating local ADC credentials..."
LOCAL_ADC="$HOME/.config/gcloud/legacy_credentials/${ACTIVE_ACCOUNT}/adc.json"
[ ! -f "$LOCAL_ADC" ] && error "ADC file not found at ${LOCAL_ADC}"
ok "Found ${LOCAL_ADC}"

# ── Step 3: SCP the key to the remote VM ──────────────────────────────────────
step "3️⃣" "Copying credentials to ${REMOTE_IP}:${REMOTE_TMP}..."
scp $SSH_OPTS "$LOCAL_ADC" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_TMP}"
ok "Credentials copied"

# ── Step 4: Activate service account + clean up on remote VM ──────────────────
step "4️⃣" "Activating service account on remote VM..."
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_IP}" bash <<REMOTE_ACTIVATE
    set -euo pipefail
    gcloud auth activate-service-account '${ACTIVE_ACCOUNT}' \
        --key-file='${REMOTE_TMP}'
    rm -f '${REMOTE_TMP}'
    echo "Activated and key file removed."
REMOTE_ACTIVATE
ok "Service account activated on ${REMOTE_IP}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Done! 🐾  ${ACTIVE_ACCOUNT} is now active on ${REMOTE_IP}${NC}"
