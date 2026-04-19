"""Base adapter interface for atom-command-broker tool adapters."""
import os
from abc import ABC, abstractmethod

# Path mapping: container path → host path.
# The container sees /workspace, but the broker on the host needs to
# use the actual host directory that is bind-mounted as /workspace.
# Set via ATOM_SHARED_WORKSPACE env var (default: /tmp/atom-workspace).
CONTAINER_WORKSPACE = "/workspace"
HOST_WORKSPACE = os.environ.get("ATOM_SHARED_WORKSPACE", "/tmp/atom-workspace")


def rewrite_container_paths(argv: list[str]) -> list[str]:
    """Rewrite container-side paths to host-side paths in command arguments.

    The container's /workspace is bind-mounted from the host's
    ATOM_SHARED_WORKSPACE directory. When the broker executes commands
    on the host, local file paths must point to the host-side location.
    """
    result = []
    for arg in argv:
        if arg == CONTAINER_WORKSPACE:
            result.append(HOST_WORKSPACE)
        elif arg.startswith(CONTAINER_WORKSPACE + "/"):
            result.append(HOST_WORKSPACE + arg[len(CONTAINER_WORKSPACE):])
        else:
            result.append(arg)
    return result


class BaseAdapter(ABC):
    """Abstract base class for tool adapters.

    Each tool family (gsutil, gcloud, kafka, aws, kubectl, etc.) should
    subclass this and implement the required methods. The broker uses
    the adapter to validate, normalize, and execute commands.
    """

    @abstractmethod
    def description(self) -> str:
        """Human-readable description for discovery."""
        ...

    @abstractmethod
    def supported_modes(self) -> list[str]:
        """Return list of supported execution modes: 'buffered', 'streaming'."""
        ...

    @abstractmethod
    def default_mode(self) -> str:
        """Return the default execution mode."""
        ...

    def discovery_metadata(self) -> dict | None:
        """Optional extra metadata to include in discover response."""
        return None

    @abstractmethod
    def validate(self, argv: list[str]) -> str | None:
        """Validate request arguments.

        Returns None if valid, or an error message string if invalid.
        This is adapter-level structural validation, not policy enforcement.
        """
        ...

    def normalize_args(self, argv: list[str]) -> list[str]:
        """Normalize/transform arguments before policy check.

        Default implementation returns argv unchanged.
        """
        return list(argv)

    @abstractmethod
    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        """Determine the effective execution mode.

        Returns (effective_mode, reason_string).
        """
        ...

    def build_command(self, executable: str, argv: list[str]) -> list[str]:
        """Build the full command to execute.

        Default: [executable] + argv, with container paths rewritten
        to host paths.
        """
        return [executable] + rewrite_container_paths(argv)

    def build_env(self, tool_policy: dict | None) -> dict:
        """Build a controlled environment for command execution.

        Default: minimal safe environment (no credential leaking).
        """
        safe_keys = [
            "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM",
            "TMPDIR", "TMP", "TEMP",
        ]
        env = {}
        for key in safe_keys:
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env
