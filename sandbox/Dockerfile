# Hardened Sandbox Container
# ==========================
# Maximally locked-down container for running untrusted code.
# Supports interactive shells, one-off commands, and the MCP server.
#
# Security layers (applied at run time via sandbox.sh):
#   --cap-drop=ALL  |  --no-new-privileges  |  --read-only rootfs
#   --network=none  |  custom seccomp        |  PID/mem/cpu limits
#   --tmpfs /tmp,/run  |  bind-mount /workspace  |  UID 1000 (non-root)

FROM python:3.13-slim-bookworm

LABEL maintainer="atom-security"
LABEL description="Hardened sandbox — no jailbreak possible"

# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    gcc \
    g++ \
    make \
    curl \
    wget \
    git \
    vim-tiny \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Security hardening
# ---------------------------------------------------------------------------

# 1. Non-root user
RUN groupadd -g 1000 sandbox && \
    useradd -u 1000 -g sandbox -m -s /bin/bash sandbox

# 2. Workspace mount point (actual storage is a bind-mount from host
#    at /workspace, shared with the broker for file operations)
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

# 3. Strip all setuid/setgid bits
RUN find / -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# 4. MCP server dependencies (must happen before package managers are removed)
RUN pip install --no-cache-dir \
    "fastmcp>=2.0" \
    "uvicorn[standard]>=0.27" \
    "starlette>=0.36" \
    "httpx>=0.24"

# 5. Remove package managers
RUN rm -f /usr/bin/apt-get /usr/bin/apt /usr/bin/dpkg

# 6. Restrictive umask + command aliases via broker
RUN echo 'umask 0077' >> /home/sandbox/.bashrc && \
    echo 'alias gsutil="atom-command-proxy gsutil"' >> /home/sandbox/.bashrc && \
    echo 'alias gcloud="atom-command-proxy gcloud"' >> /home/sandbox/.bashrc

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/mcp && chown sandbox:sandbox /opt/mcp
COPY mcp_server.py /opt/mcp/mcp_server.py

ENV MCP_SAFE_ROOT=/workspace
ENV MCP_CMD_TIMEOUT=120

# ---------------------------------------------------------------------------
# atom-command-proxy — generic thin command relay
# Sends structured requests to atom-command-broker on the host via Unix socket.
# Socket path auto-detected at runtime (mounted socket directory).
# ---------------------------------------------------------------------------
COPY atom-command-proxy.py /usr/local/bin/atom-command-proxy
RUN chmod +x /usr/local/bin/atom-command-proxy

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
USER sandbox
WORKDIR /workspace
CMD ["/bin/bash"]
