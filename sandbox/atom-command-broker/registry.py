"""Executable registry for atom-command-broker.

Maps logical tool names to approved host-side executable paths.
The broker never executes arbitrary caller-provided paths — only
entries in this allowlisted registry.
"""
import logging
import os
import shutil

logger = logging.getLogger("atom-command-broker.registry")

# ---------------------------------------------------------------------------
# Well-known executable locations to search
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
        "/opt/kafka/bin/kafka-broker-api-versions.sh",
    ],
    "kafka-console-consumer": [
        "/usr/bin/kafka-console-consumer",
        "/opt/kafka/bin/kafka-console-consumer.sh",
    ],
    "kafka-console-share-consumer": [
        "/usr/bin/kafka-console-share-consumer",
        "/opt/kafka/bin/kafka-console-share-consumer.sh",
    ],
    "kafka-get-offsets": [
        "/usr/bin/kafka-get-offsets",
        "/opt/kafka/bin/kafka-get-offsets.sh",
    ],
    "kafka-log-dirs": [
        "/usr/bin/kafka-log-dirs",
        "/opt/kafka/bin/kafka-log-dirs.sh",
    ],
    "kafka-metadata-quorum": [
        "/usr/bin/kafka-metadata-quorum",
        "/opt/kafka/bin/kafka-metadata-quorum.sh",
    ],
    "kafka-replica-verification": [
        "/usr/bin/kafka-replica-verification",
        "/opt/kafka/bin/kafka-replica-verification.sh",
    ],
    "kafka-verifiable-consumer": [
        "/usr/bin/kafka-verifiable-consumer",
        "/opt/kafka/bin/kafka-verifiable-consumer.sh",
    ],
    "kafka-verifiable-share-consumer": [
        "/usr/bin/kafka-verifiable-share-consumer",
        "/opt/kafka/bin/kafka-verifiable-share-consumer.sh",
    ],
}


class ExecutableRegistry:
    """Registry mapping logical tool names to host-side executable paths.

    Auto-discovers executables on the host using well-known paths and $PATH.
    Additional executables can be registered manually or via config.
    """

    def __init__(self, overrides: dict[str, str] | None = None):
        self._registry: dict[str, str] = {}
        self._discover_all()
        if overrides:
            for tool, path in overrides.items():
                self.register(tool, path)

    def _discover_all(self):
        """Auto-discover executables for all known tools."""
        for tool, candidates in _SEARCH_PATHS.items():
            path = self._find_executable(tool, candidates)
            if path:
                self._registry[tool] = path
                logger.debug("Discovered %s -> %s", tool, path)
            else:
                logger.debug("Tool %s not found on host (will be unavailable)", tool)

    @staticmethod
    def _find_executable(tool: str, candidates: list[str]) -> str | None:
        """Find the first existing executable from candidates, or via $PATH."""
        # Check explicit candidates first
        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        # Fall back to $PATH lookup
        found = shutil.which(tool)
        return found

    def register(self, tool: str, path: str):
        """Manually register or override a tool → executable mapping."""
        if not os.path.isfile(path):
            logger.warning("Registering %s -> %s but file does not exist", tool, path)
        self._registry[tool] = path
        logger.info("Registered %s -> %s", tool, path)

    def get_executable(self, tool: str) -> str | None:
        """Get the host-side executable path for a tool. Returns None if unknown."""
        return self._registry.get(tool, None)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return sorted(self._registry.keys())

    def is_registered(self, tool: str) -> bool:
        return tool in self._registry
