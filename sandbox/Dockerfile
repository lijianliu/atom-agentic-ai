# Hardened Sandbox Container
# ==========================
# Maximally locked-down container for running untrusted code.
# Supports interactive shells, one-off commands, and the MCP server.
#
# Security layers (applied at run time via sandbox.sh):
#   --cap-drop=ALL  |  --no-new-privileges  |  --read-only rootfs
#   --network=none  |  custom seccomp        |  PID/mem/cpu limits
#   --tmpfs /tmp,/run,/workspace             |  UID 1000 (non-root)

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

# 2. Workspace directory
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

# 6. Restrictive umask
RUN echo 'umask 0077' >> /home/sandbox/.bashrc

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
RUN mkdir -p /opt/mcp && chown sandbox:sandbox /opt/mcp
COPY mcp_server.py /opt/mcp/mcp_server.py

ENV MCP_SAFE_ROOT=/workspace
ENV MCP_CMD_TIMEOUT=120

# ---------------------------------------------------------------------------
# gsutil proxy wrapper
# Thin client that forwards gsutil commands to the host via Unix socket.
# Socket path auto-detected at runtime (see wrapper script).
# ---------------------------------------------------------------------------
COPY gsutil-wrapper.sh /usr/local/bin/gsutil
RUN chmod +x /usr/local/bin/gsutil

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
USER sandbox
WORKDIR /workspace
CMD ["/bin/bash"]
