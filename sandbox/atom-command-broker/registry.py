"""Executable registry for atom-command-broker.

Maps logical tool names to approved host-side executable paths.
The broker never executes arbitrary caller-provided paths — only
entries in this allowlisted registry.
"""
import logging
import os
import shutil
import time

logger = logging.getLogger("atom-command-broker.registry")

# ---------------------------------------------------------------------------
# Well-known executable locations to search
#
# Search order matters: entries are tried first-to-last and the first
# existing executable wins.  For Kafka tools the Bigtop distro package
# provides both /usr/bin wrappers (which call bigtop-detect-javahome to
# select the correct JDK) and the underlying scripts at /usr/lib/kafka/bin.
# ---------------------------------------------------------------------------
_SEARCH_PATHS = {
    "gsutil": [
        "/usr/bin/gsutil",
        "/usr/local/bin/gsutil",
        "/snap/bin/gsutil",
    ],
    "gcloud": [
        "/usr/bin/gcloud",
        "/usr/local/bin/gcloud",
        "/snap/bin/gcloud",
    ],
    "hadoop": [
        "/usr/bin/hadoop",
        "/usr/local/bin/hadoop",
        "/usr/lib/hadoop/bin/hadoop",
        "/opt/hadoop/bin/hadoop",
    ],
    "hdfs": [
        "/usr/bin/hdfs",
        "/usr/local/bin/hdfs",
        "/usr/lib/hadoop-hdfs/bin/hdfs",
        "/opt/hadoop/bin/hdfs",
    ],
    "kafka-broker-api-versions": [
        "/usr/bin/kafka-broker-api-versions",
        "/usr/bin/kafka-broker-api-versions.sh",
        "/usr/lib/kafka/bin/kafka-broker-api-versions.sh",
    ],
    "kafka-console-consumer": [
        "/usr/bin/kafka-console-consumer",
        "/usr/bin/kafka-console-consumer.sh",
        "/usr/lib/kafka/bin/kafka-console-consumer.sh",
    ],
    "kafka-consumer-groups": [
        "/usr/bin/kafka-consumer-groups",
        "/usr/bin/kafka-consumer-groups.sh",
        "/usr/lib/kafka/bin/kafka-consumer-groups.sh",
    ],
    "kafka-get-offsets": [
        "/usr/bin/kafka-get-offsets",
        "/usr/bin/kafka-get-offsets.sh",
        "/usr/lib/kafka/bin/kafka-get-offsets.sh",
    ],
    "kafka-log-dirs": [
        "/usr/bin/kafka-log-dirs",
        "/usr/bin/kafka-log-dirs.sh",
        "/usr/lib/kafka/bin/kafka-log-dirs.sh",
    ],
    "kafka-replica-verification": [
        "/usr/bin/kafka-replica-verification",
        "/usr/bin/kafka-replica-verification.sh",
        "/usr/lib/kafka/bin/kafka-replica-verification.sh",
    ],
    "kafka-topics": [
        "/usr/bin/kafka-topics",
        "/usr/bin/kafka-topics.sh",
        "/usr/lib/kafka/bin/kafka-topics.sh",
    ],
    "kafka-verifiable-consumer": [
        "/usr/bin/kafka-verifiable-consumer",
        "/usr/bin/kafka-verifiable-consumer.sh",
        "/usr/lib/kafka/bin/kafka-verifiable-consumer.sh",
    ],
}

# How often to re-scan for not-yet-found tools (seconds).
_REDISCOVER_INTERVAL = 120


class ExecutableRegistry:
    """Registry mapping logical tool names to host-side executable paths.

    Auto-discovers executables on the host using well-known paths and $PATH.
    Additional executables can be registered manually or via config.

    Tools that are not found at startup are periodically re-scanned so that
    executables installed *after* the broker starts (e.g. Kafka packages
    deployed later) are picked up automatically without a broker restart.
    """

    def __init__(self, overrides: dict[str, str] | None = None):
        self._registry: dict[str, str] = {}
        self._overrides: dict[str, str] = {}
        self._last_rediscover: float = 0.0
        self._discover_all()
        if overrides:
            for tool, path in overrides.items():
                self.register(tool, path)
                self._overrides[tool] = path

    def _discover_all(self):
        """Auto-discover executables for all known tools."""
        for tool, candidates in _SEARCH_PATHS.items():
            # Don't overwrite manual overrides
            if tool in self._overrides:
                continue
            path = self._find_executable(tool, candidates)
            if path:
                if tool not in self._registry:
                    logger.info("Discovered %s -> %s", tool, path)
                self._registry[tool] = path
            else:
                logger.debug("Tool %s not found on host (will be unavailable)", tool)
        self._last_rediscover = time.monotonic()

    def _maybe_rediscover(self):
        """Re-scan for missing tools if enough time has passed.

        This handles the case where tools (e.g. Kafka) are installed
        after the broker has already started.
        """
        # Only re-scan if there are missing tools
        known = set(self._registry.keys())
        all_tools = set(_SEARCH_PATHS.keys())
        missing = all_tools - known
        if not missing:
            return

        now = time.monotonic()
        if now - self._last_rediscover < _REDISCOVER_INTERVAL:
            return

        logger.debug("Re-discovering %d missing tools: %s", len(missing), ", ".join(sorted(missing)))
        newly_found = []
        for tool in missing:
            if tool in self._overrides:
                continue
            candidates = _SEARCH_PATHS.get(tool, [])
            path = self._find_executable(tool, candidates)
            if path:
                self._registry[tool] = path
                newly_found.append(tool)
                logger.info("Late-discovered %s -> %s", tool, path)

        self._last_rediscover = now
        if newly_found:
            logger.info(
                "Re-discovery found %d new tools: %s (total now: %d)",
                len(newly_found),
                ", ".join(sorted(newly_found)),
                len(self._registry),
            )

    @staticmethod
    def _find_executable(tool: str, candidates: list[str]) -> str | None:
        """Find the first existing executable from candidates, or via $PATH."""
        # Check explicit candidates first
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        # Fall back to $PATH lookup (try both bare name and .sh variant)
        found = shutil.which(tool)
        if found:
            return found
        found = shutil.which(tool + ".sh")
        return found

    def register(self, tool: str, path: str):
        """Manually register or override a tool → executable mapping."""
        if not os.path.isfile(path):
            logger.warning("Registering %s -> %s but file does not exist", tool, path)
        self._registry[tool] = path
        logger.info("Registered %s -> %s", tool, path)

    def get_executable(self, tool: str) -> str | None:
        """Get the host-side executable path for a tool. Returns None if unknown."""
        exe = self._registry.get(tool, None)
        if exe is None:
            # Tool not found — trigger lazy re-discovery in case it was
            # installed after the broker started.
            self._maybe_rediscover()
            exe = self._registry.get(tool, None)
        return exe

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        # Trigger re-discovery before listing so discover responses are fresh
        self._maybe_rediscover()
        return sorted(self._registry.keys())

    def is_registered(self, tool: str) -> bool:
        return tool in self._registry
