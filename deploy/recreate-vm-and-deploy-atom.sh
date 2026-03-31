#!/usr/bin/env bash
# recreate-vm-and-deploy-atom.sh
# Creates a fresh VM and deploys the atom-agentic-ai project onto it.
# Mirrors the style of step-1-recreate-vm.sh.
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[90m'
BOLD='\033[1m'
NC='\033[0m'

TAIL_LINES=5

step()  { echo -e "${CYAN}$1${NC}  $2"; }
warn()  { echo -e "   ${YELLOW}⚠️  $*${NC}"; }
error() { echo -e "${RED}❌ $*${NC}"; exit 1; }
ok()    { echo -e "   ${GREEN}✅ $*${NC}"; }

# ── run_with_tail: run a command while tailing its output ─────────────────────
run_with_tail() {
    local logfile lastsize cursize lc pid exit_code term_width
    logfile=$(mktemp)
    lastsize=0
    term_width=$(tput cols 2>/dev/null || echo 120)

    "$@" > "$logfile" 2>&1 &
    pid=$!

    echo -e "${GRAY}────────────────────────────────────${NC}"
    for ((i=0; i<TAIL_LINES; i++)); do echo ""; done

    while kill -0 "$pid" 2>/dev/null; do
        cursize=$(wc -c < "$logfile" 2>/dev/null || echo 0)
        if [ "$cursize" -ne "$lastsize" ]; then
            lastsize=$cursize
            printf "\033[%dA" "$TAIL_LINES"
            lc=$(wc -l < "$logfile" 2>/dev/null || echo 0)
            [ "$lc" -gt "$TAIL_LINES" ] && lc=$TAIL_LINES
            while IFS= read -r line; do
                printf "\033[2K${GRAY}  %s${NC}\n" "${line:0:$((term_width - 3))}"
            done < <(tail -n "$TAIL_LINES" "$logfile" 2>/dev/null)
            for ((i=lc; i<TAIL_LINES; i++)); do printf "\033[2K\n"; done
        fi
        sleep 0.2
    done

    exit_code=0
    wait "$pid" || exit_code=$?

    for ((i=0; i<TAIL_LINES; i++)); do printf "\033[2K\033[A"; done
    printf "\033[2K\033[A"
    printf "\033[2K"

    rm -f "$logfile"
    return $exit_code
}

# ── GCP project config (sourced from ~/.config/atom-agentic-ai/gcp.sh) ────────
GCP_CONFIG="$HOME/.config/atom-agentic-ai/gcp.sh"
[ ! -f "$GCP_CONFIG" ] && { echo -e "${RED}❌ Missing GCP config: ${GCP_CONFIG}${NC}"; exit 1; }
source "$GCP_CONFIG"

# ── Configuration ─────────────────────────────────────────────────────────────
REGION="us-central1"
ZONE="us-central1-c"
ENV="qa"
NAME_SUFFIX="atom-vm"
CPU=4
VM_SEQ_START=1
VM_SEQ_END=1
LABEL_APP="atom"

# Derived — always consistent with the name create_vm() produces
VM_PREFIX="${ENV}-${NAME_SUFFIX}-$(whoami)"

# Resolve subnet via get_subnet() defined in gcp.sh
SUBNET="$(get_subnet "$REGION")"

create_vm() {
  local seq
  for seq in $(seq "$VM_SEQ_START" "$VM_SEQ_END"); do
    local vm_name="${VM_PREFIX}-${seq}"
    echo "Creating instance ${vm_name}..."
    gcloud compute instances create "${vm_name}" \
      --subnet="projects/shared-vpc-admin/regions/${REGION}/subnetworks/${SUBNET}" \
      --zone="${ZONE}" \
      --machine-type="e2-standard-${CPU}" \
      --boot-disk-size=80 \
      --image="projects/wmt-pcloud-trusted-images/global/images/${OS_IMAGE}" \
      --tags="${VM_PREFIX}" \
      --labels="applicationname=${LABEL_APP}" \
      --project="${GCP_PROJECT}" \
      --no-address
  done
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REMOTE_USER="${REMOTE_USER:-$(whoami)}"
REMOTE_DIR="/home/${REMOTE_USER}/atom-agentic-ai"
SSH_TIMEOUT=120   # seconds to wait for SSH
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes"

# ── Flags ────────────────────────────────────────────────────────────────────
DELETE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --delete-only|-d) DELETE_ONLY=true ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--delete-only|-d]"
      echo ""
      echo "  (no flags)      Delete existing VM, create fresh one, deploy atom"
      echo "  --delete-only   Delete existing VM(s) and exit — no new VM created"
      exit 0 ;;
    *) error "Unknown flag: $arg  (try --help)" ;;
  esac
done

# ── Header ────────────────────────────────────────────────────────────────────
MODE_LABEL="recreate + deploy"
$DELETE_ONLY && MODE_LABEL="delete only"
echo -e "${BOLD}${CYAN}Atom VM Deployer${NC} ${GRAY}(${VM_PREFIX} • ${MODE_LABEL})${NC}"
echo ""

# ═════════════════════════════════════════════════════════════════════════════
# Phase 1 — VM lifecycle (mirrors step-1-recreate-vm.sh)
# ═════════════════════════════════════════════════════════════════════════════

# ── Step 1: Find existing VMs ─────────────────────────────────────────────────
step "1️⃣" "Searching for existing VMs..."
VMS=$(gcloud compute instances list \
    --filter="name~^${VM_PREFIX}" \
    --format="csv[no-heading](name,zone)" 2>/dev/null || true)

if [ -z "$VMS" ]; then
    echo -e "   ${GRAY}No existing VMs found — nothing to delete${NC}"
else
    VM_COUNT=0
    while IFS=',' read -r name zone; do
        [ -z "$name" ] && continue
        if [[ ! "$name" =~ ^${VM_PREFIX} ]]; then
            error "SAFETY STOP: '$name' doesn't match prefix '${VM_PREFIX}'!"
        fi
        echo -e "   ${YELLOW}└─ ${name}${NC} ${GRAY}(${zone})${NC}"
        ((VM_COUNT++)) || true
    done <<< "$VMS"

    # ── Step 2: Delete ────────────────────────────────────────────────────────
    while IFS=',' read -r name zone; do
        [[ -z "$name" || -z "$zone" ]] && continue
        [[ ! "$name" =~ ^${VM_PREFIX} ]] && continue
        step "2️⃣" "Deleting ${name}..."
        run_with_tail bash -c "yes | gcloud compute instances delete '${name}' --zone='${zone}' --verbosity=debug"
        ok "Deleted ${name}"
    done <<< "$VMS"
fi

if $DELETE_ONLY; then
    echo ""
    echo -e "${GREEN}${BOLD}Done — VM(s) deleted. 👋${NC}"
    exit 0
fi

# ── Step 3: Create new VM ─────────────────────────────────────────────────────
step "3️⃣" "Creating new VM..."
run_with_tail create_vm
ok "VM created"

# ── Step 4: Fetch IP + update ~/.zshrc ───────────────────────────────────────
step "4️⃣" "Fetching VM IP and updating ~/.zshrc..."
INTERNAL_IP=$(gcloud compute instances list \
    --filter="name~^${VM_PREFIX}" \
    --format="value(networkInterfaces[0].networkIP)" | head -1)
[ -z "$INTERNAL_IP" ] && error "Could not retrieve internal IP"

ZSHRC="$HOME/.zshrc"
if [ -f "$ZSHRC" ]; then
    sed -i '' '/^export ATOM_IP=/d'         "$ZSHRC"
    sed -i '' '/^alias ssh-atom=/d'          "$ZSHRC"
    sed -i '' '/^# Atom VM (auto-generated/d' "$ZSHRC"
fi
echo "# Atom VM (auto-generated at $(date '+%Y-%m-%d %H:%M:%S'))" >> "$ZSHRC"
echo "export ATOM_IP=${INTERNAL_IP}"                               >> "$ZSHRC"
echo "alias ssh-atom='ssh ${INTERNAL_IP}'"                         >> "$ZSHRC"
ok "~/.zshrc updated  →  ATOM_IP=${INTERNAL_IP}"

# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 — Deploy atom to the VM
# ═════════════════════════════════════════════════════════════════════════════

# ── Step 5: Wait for SSH ──────────────────────────────────────────────────────
step "5️⃣" "Waiting for SSH on ${INTERNAL_IP} (timeout ${SSH_TIMEOUT}s)..."
elapsed=0
until ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" 'exit 0' 2>/dev/null; do
    sleep 5
    elapsed=$((elapsed + 5))
    if [ "$elapsed" -ge "$SSH_TIMEOUT" ]; then
        error "Timed out waiting for SSH on ${INTERNAL_IP}"
    fi
    echo -e "   ${GRAY}still waiting... (${elapsed}s)${NC}"
done
ok "SSH is up!"

# ── Step 6: Ensure uv is installed on the VM ─────────────────────────────────
step "6️⃣" "Installing uv on VM..."
ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" bash <<'REMOTE_UV'
    if command -v uv >/dev/null 2>&1; then
        echo "uv already installed: $(uv --version)"
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # Ensure uv is on PATH for subsequent commands
        export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
        uv --version
    fi
REMOTE_UV
ok "uv ready on VM"

# ── Step 7: Copy atom-agentic-ai to VM via scp ───────────────────────────────
step "7️⃣" "Packaging and copying atom-agentic-ai → ${INTERNAL_IP}:${REMOTE_DIR}..."
TARBALL=$(mktemp /tmp/atom-agentic-ai-XXXXXX.tar.gz)
# Pack locally, excluding noise
COPYFILE_DISABLE=1 tar -czf "$TARBALL" \
    --exclude='./.venv' \
    --exclude='./__pycache__' \
    --exclude='./.git' \
    --exclude='./sandbox/report.html' \
    --exclude='./**/*.pyc' \
    -C "${PROJECT_ROOT}" .
# Ship it
scp $SSH_OPTS "$TARBALL" "${REMOTE_USER}@${INTERNAL_IP}:/tmp/atom-agentic-ai.tar.gz"
rm -f "$TARBALL"
# Unpack on VM (blow away old dir for a clean slate)
ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" bash <<REMOTE_UNPACK
    rm -rf '${REMOTE_DIR}'
    mkdir -p '${REMOTE_DIR}'
    tar -xzf /tmp/atom-agentic-ai.tar.gz -C '${REMOTE_DIR}' 2>&1 | grep -v 'Ignoring unknown extended header' >&2 || true
    rm -f /tmp/atom-agentic-ai.tar.gz
REMOTE_UNPACK
ok "Codebase copied and unpacked"

# ── Step 8: Copy config files if present ─────────────────────────────────────
step "8️⃣" "Copying config files to VM..."
ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" \
    "mkdir -p \$HOME/.config/atom-agentic-ai"

LOCAL_ENV="$HOME/.config/atom-agentic-ai/env.sh"
if [ -f "$LOCAL_ENV" ]; then
    scp $SSH_OPTS \
        "$LOCAL_ENV" \
        "${REMOTE_USER}@${INTERNAL_IP}:~/.config/atom-agentic-ai/env.sh"
    ok "env.sh copied"
else
    warn "No local env config found at ${LOCAL_ENV} — skipping"
fi

LOCAL_PROMPT="$HOME/.config/atom-agentic-ai/system_prompt.md"
if [ -f "$LOCAL_PROMPT" ]; then
    scp $SSH_OPTS \
        "$LOCAL_PROMPT" \
        "${REMOTE_USER}@${INTERNAL_IP}:~/.config/atom-agentic-ai/system_prompt.md"
    ok "system_prompt.md copied"
else
    warn "No system_prompt.md found at ${LOCAL_PROMPT} — agent will use default prompt"
fi

# ── Step 9: Install Python deps on VM ──────────────────────────────────────────────────
step "9️⃣" "Installing Python dependencies on VM..."
ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" bash <<REMOTE_DEPS
    # Ensure uv is on PATH whether it was just installed or already present
    export PATH="\$HOME/.cargo/bin:\$HOME/.local/bin:\$PATH"
    # Also source uv's own env file if it exists (covers fresh installs)
    [ -f "\$HOME/.local/bin/env" ] && source "\$HOME/.local/bin/env"
    cd '${REMOTE_DIR}'
    uv venv --python 3.13
    uv sync --all-groups
REMOTE_DEPS
ok "Dependencies installed"

# ── Step 10: Install + start Docker on VM ───────────────────────────────────────
step "🔟" "Installing and starting Docker on VM..."
ssh $SSH_OPTS "${REMOTE_USER}@${INTERNAL_IP}" bash <<'REMOTE_DOCKER'
    set -euo pipefail
    if docker info >/dev/null 2>&1; then
        echo "Docker already running, skipping install."
        exit 0
    fi
    echo "--- Installing docker.io ..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io
    echo "--- Enabling + starting Docker daemon ..."
    sudo systemctl enable docker
    sudo systemctl start docker
    echo "--- Adding user to docker group ..."
    sudo usermod -aG docker "$USER"
    echo "--- Verifying Docker ..."
    # Use sudo since group membership won't apply until next login
    sudo docker info --format 'Docker {{.ServerVersion}} is running ✅'
REMOTE_DOCKER
ok "Docker installed and running"

# ── Step 11: Setup gcloud auth on VM ─────────────────────────────────────────
step "1️⃣ 1️⃣" "Setting up gcloud auth on VM..."
"${SCRIPT_DIR}/setup-gcloud.sh" "${INTERNAL_IP}"
ok "gcloud auth configured on VM"

# ═════════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}${BOLD}All done! 🐾${NC}"
echo -e "  VM IP      ${BOLD}${INTERNAL_IP}${NC}"
echo -e "  Atom dir   ${BOLD}${REMOTE_DIR}${NC}"
echo -e "  SSH        ${BOLD}ssh ${INTERNAL_IP}${NC}   (or: source ~/.zshrc && ssh-atom)"
echo -e "  Run atom   ${GRAY}cd ${REMOTE_DIR} && ./run.sh${NC}"
echo ""
