"""Centralized policy engine for atom-command-broker.

Loads policy from JSON (or YAML if PyYAML available) and evaluates
per-tool rules. The broker is the single source of truth for policy.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("atom-command-broker.policy")

# ---------------------------------------------------------------------------
# Read-only fs/dfs subcommands (used by hadoop/hdfs policy evaluation)
# ---------------------------------------------------------------------------
_HADOOP_READ_ONLY_FS_SUBCOMMANDS = {
    "cat", "checksum", "count", "df", "du", "find", "getfacl", "getfattr",
    "getmerge", "head", "ls", "lsr", "stat", "tail", "test", "text", "usage",
}

# ---------------------------------------------------------------------------
# Default built-in policy (used when no file is provided)
# ---------------------------------------------------------------------------
DEFAULT_POLICY: dict = {
    "global": {
        "rate_limit_per_minute": 120,
        "max_timeout_sec": 300,
        "max_output_bytes": 10485760,
    },
    "tools": {
        "gsutil": {
            "enabled": True,
            "allowed_subcommands": ["ls", "cat", "stat", "cp", "du", "hash", "version"],
            "allowed_buckets": [],
            "blocked_flags": ["-d", "--delete", "-D"],
            "cp_download_only": True,
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "gcloud": {
            "enabled": True,
            "allowed_command_prefixes": [
                "storage buckets list",
                "storage buckets describe",
                "storage objects list",
                "storage ls",
                "config list",
                "config get",
                "info",
                "version",
                "projects list",
                "projects describe",
                "compute instances list",
                "compute instances describe",
                "compute zones list",
                "compute regions list",
            ],
            "blocked_flags": ["--impersonate-service-account"],
            "force_flags": ["--quiet"],
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "hadoop": {
            "enabled": True,
            "allowed_top_commands": ["fs", "classpath", "version", "envvars", "checknative"],
            "allowed_fs_subcommands": [
                "cat", "checksum", "count", "df", "du", "find", "getfacl", "getfattr",
                "getmerge", "head", "ls", "lsr", "stat", "tail", "test", "text", "usage",
                "cp", "get", "copyToLocal", "mkdir", "put", "copyFromLocal",
                "appendToFile", "mv", "rm", "rmdir", "touchz",
                "chmod", "chown", "chgrp", "setrep", "setfacl", "setfattr",
            ],
            "read_only": False,
            "allowed_paths": [],
            "blocked_flags": [],
            "max_timeout_sec": 300,
            "max_output_bytes": 10485760,
        },
        "hdfs": {
            "enabled": True,
            "allowed_top_commands": [
                "dfs", "fsck", "getconf", "groups", "lsSnapshottableDir",
                "snapshotDiff", "version", "envvars", "classpath",
            ],
            "allowed_fs_subcommands": [
                "cat", "checksum", "count", "df", "du", "find", "getfacl", "getfattr",
                "getmerge", "head", "ls", "lsr", "stat", "tail", "test", "text", "usage",
                "cp", "get", "copyToLocal", "mkdir", "put", "copyFromLocal",
                "appendToFile", "mv", "rm", "rmdir", "touchz",
                "chmod", "chown", "chgrp", "setrep", "setfacl", "setfattr",
            ],
            "read_only": False,
            "allowed_paths": [],
            "blocked_flags": [],
            "max_timeout_sec": 300,
            "max_output_bytes": 10485760,
        },
        "kafka-broker-api-versions": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "max_timeout_sec": 30,
            "max_output_bytes": 1048576,
        },
        "kafka-console-consumer": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "allowed_topics": [],
            "allowed_groups": [],
            "require_bounded": False,
            "max_messages_limit": 1000,
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "kafka-console-share-consumer": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "allowed_topics": [],
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "kafka-get-offsets": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "max_timeout_sec": 30,
            "max_output_bytes": 1048576,
        },
        "kafka-log-dirs": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "max_timeout_sec": 30,
            "max_output_bytes": 1048576,
        },
        "kafka-metadata-quorum": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "max_timeout_sec": 30,
            "max_output_bytes": 1048576,
        },
        "kafka-replica-verification": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "kafka-verifiable-consumer": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "allowed_topics": [],
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
        "kafka-verifiable-share-consumer": {
            "enabled": True,
            "allowed_bootstrap_servers": [],
            "allowed_topics": [],
            "max_timeout_sec": 120,
            "max_output_bytes": 10485760,
        },
    },
}


class PolicyEngine:
    """Loads and evaluates broker policy."""

    def __init__(self, policy_path: str | None = None):
        if policy_path and os.path.isfile(policy_path):
            self.policy = self._load_file(policy_path)
            logger.info("Policy loaded from %s", policy_path)
        else:
            self.policy = dict(DEFAULT_POLICY)
            logger.info("Using built-in default policy")

    def _load_file(self, path: str) -> dict:
        """Load policy from JSON or YAML file."""
        with open(path) as f:
            raw = f.read()
        if path.endswith((".yaml", ".yml")):
            try:
                import yaml
                return yaml.safe_load(raw)
            except ImportError:
                raise RuntimeError(
                    "PyYAML required for YAML policy files: pip install pyyaml"
                )
        return json.loads(raw)

    def get_global_policy(self) -> dict:
        return self.policy.get("global", {})

    def get_tool_policy(self, tool: str) -> dict | None:
        """Get policy for a specific tool.  Returns None if no policy defined."""
        tools = self.policy.get("tools", {})
        return tools.get(tool, None)

    def evaluate(
        self, tool: str, argv: list[str], tool_policy: dict | None
    ) -> dict:
        """Evaluate whether a command is allowed.

        Returns {"allowed": bool, "reason": str}.
        The adapter-specific validation should already have run; this does
        the policy-level checks (enabled, allowed subcommands, buckets, etc).
        """
        if tool_policy is None:
            return {"allowed": False, "reason": f"No policy defined for tool: {tool}"}

        if not tool_policy.get("enabled", True):
            return {"allowed": False, "reason": f"Tool '{tool}' is disabled by policy"}

        # No args = tool will show its own usage/help, allow it
        if not argv:
            return {"allowed": True, "reason": "ok"}

        # --- gsutil ---
        if tool == "gsutil":
            return self._evaluate_gsutil(argv, tool_policy)

        # --- gcloud ---
        if tool == "gcloud":
            return self._evaluate_gcloud(argv, tool_policy)

        # --- hadoop / hdfs ---
        if tool in ("hadoop", "hdfs"):
            return self._evaluate_hadoop(tool, argv, tool_policy)

        # --- kafka tools ---
        if tool.startswith("kafka-"):
            return self._evaluate_kafka(tool, argv, tool_policy)

        # Generic: allowed if enabled
        return {"allowed": True, "reason": "ok"}

    # ---- gsutil policy ----
    def _evaluate_gsutil(self, argv: list[str], policy: dict) -> dict:
        # Find the subcommand (first non-flag arg)
        subcmd = None
        for a in argv:
            if not a.startswith("-"):
                subcmd = a
                break
        if not subcmd:
            return {"allowed": False, "reason": "No subcommand found in gsutil args"}

        allowed = policy.get("allowed_subcommands", [])
        if allowed and subcmd not in allowed:
            return {
                "allowed": False,
                "reason": f"gsutil subcommand '{subcmd}' not allowed. Permitted: {allowed}",
            }

        # cp directionality check
        if subcmd == "cp" and policy.get("cp_download_only", True):
            non_flag = [a for a in argv if not a.startswith("-") and a != "cp"]
            if len(non_flag) >= 2 and non_flag[-1].startswith("gs://"):
                return {"allowed": False, "reason": "Uploads (local→gs://) blocked by policy"}

        # Bucket restrictions
        allowed_buckets = policy.get("allowed_buckets", [])
        if allowed_buckets:
            gs_paths = [a for a in argv if a.startswith("gs://")]
            for gp in gs_paths:
                match = re.match(r"(gs://[^/]+)", gp)
                bucket = match.group(1) if match else gp
                if bucket not in allowed_buckets:
                    return {
                        "allowed": False,
                        "reason": f"Bucket '{bucket}' not in allowed list: {allowed_buckets}",
                    }

        # Blocked flags
        blocked = policy.get("blocked_flags", [])
        for a in argv:
            if a in blocked:
                return {"allowed": False, "reason": f"Flag '{a}' blocked by policy"}

        return {"allowed": True, "reason": "ok"}

    # ---- gcloud policy ----
    def _evaluate_gcloud(self, argv: list[str], policy: dict) -> dict:
        non_flag = [a for a in argv if not a.startswith("-")]
        cmd_str = " ".join(non_flag)

        allowed_prefixes = policy.get("allowed_command_prefixes", [])
        if allowed_prefixes:
            matched = any(cmd_str.startswith(prefix) for prefix in allowed_prefixes)
            if not matched:
                return {
                    "allowed": False,
                    "reason": (
                        f"gcloud command '{cmd_str}' does not match any allowed prefix. "
                        f"Permitted prefixes: {allowed_prefixes}"
                    ),
                }

        blocked = policy.get("blocked_flags", [])
        for a in argv:
            if a in blocked:
                return {"allowed": False, "reason": f"Flag '{a}' blocked by policy"}

        return {"allowed": True, "reason": "ok"}

    # ---- hadoop / hdfs policy ----
    def _evaluate_hadoop(self, tool: str, argv: list[str], policy: dict) -> dict:
        """Evaluate hadoop / hdfs commands against policy.

        Expected argv shapes:
          hadoop: ["fs", "-ls", "/path"]  or  ["version"]
          hdfs:   ["dfs", "-ls", "/path"] or  ["fsck", "/path"]
        """
        if not argv:
            return {"allowed": True, "reason": "ok"}

        top_cmd = argv[0]

        # Check allowed top-level commands
        allowed_top = policy.get("allowed_top_commands", [])
        if allowed_top and top_cmd not in allowed_top:
            return {
                "allowed": False,
                "reason": (
                    f"{tool} top-level command '{top_cmd}' not allowed. "
                    f"Permitted: {allowed_top}"
                ),
            }

        # For fs/dfs subcommands, extract and validate the operation
        fs_commands = {"fs", "dfs"}
        if top_cmd in fs_commands and len(argv) > 1:
            return self._evaluate_hadoop_fs(tool, argv[1:], policy)

        # Blocked flags (global)
        blocked = policy.get("blocked_flags", [])
        for a in argv:
            if a in blocked:
                return {"allowed": False, "reason": f"Flag '{a}' blocked by policy"}

        return {"allowed": True, "reason": "ok"}

    def _evaluate_hadoop_fs(
        self, tool: str, fs_argv: list[str], policy: dict
    ) -> dict:
        """Evaluate fs/dfs sub-commands like -ls, -cat, -cp, etc.

        fs_argv is everything after 'fs' or 'dfs', e.g. ["-ls", "-R", "/path"].
        """
        # Find the fs subcommand (first arg starting with -)
        fs_subcmd = None
        for a in fs_argv:
            if a.startswith("-"):
                # Strip leading dash(es) to get the subcommand name
                candidate = a.lstrip("-")
                if candidate:
                    fs_subcmd = candidate
                    break

        if not fs_subcmd:
            # No subcommand — hadoop fs will show help
            return {"allowed": True, "reason": "ok"}

        # Check allowed fs subcommands
        allowed_fs = policy.get("allowed_fs_subcommands", [])
        if allowed_fs and fs_subcmd not in allowed_fs:
            return {
                "allowed": False,
                "reason": (
                    f"{tool} fs subcommand '-{fs_subcmd}' not allowed. "
                    f"Permitted: {allowed_fs}"
                ),
            }

        # read_only mode: block write subcommands
        if policy.get("read_only", False):
            if fs_subcmd not in _HADOOP_READ_ONLY_FS_SUBCOMMANDS:
                return {
                    "allowed": False,
                    "reason": (
                        f"{tool} fs subcommand '-{fs_subcmd}' is a write operation "
                        f"and policy is set to read_only"
                    ),
                }

        # Path restrictions
        allowed_paths = policy.get("allowed_paths", [])
        if allowed_paths:
            # Collect all path-like arguments (non-flag args after subcmd)
            paths = [a for a in fs_argv if not a.startswith("-")]
            for p in paths:
                matched = any(
                    p.startswith(ap) for ap in allowed_paths
                )
                if not matched:
                    return {
                        "allowed": False,
                        "reason": (
                            f"Path '{p}' not under any allowed path prefix. "
                            f"Permitted: {allowed_paths}"
                        ),
                    }

        # Blocked flags
        blocked = policy.get("blocked_flags", [])
        for a in fs_argv:
            if a in blocked:
                return {"allowed": False, "reason": f"Flag '{a}' blocked by policy"}

        return {"allowed": True, "reason": "ok"}

    # ---- kafka policy ----
    def _evaluate_kafka(self, tool: str, argv: list[str], policy: dict) -> dict:
        # Bootstrap server restriction
        allowed_servers = policy.get("allowed_bootstrap_servers", [])
        if allowed_servers:
            bs = self._extract_kafka_flag(argv, "--bootstrap-server")
            if bs and bs not in allowed_servers:
                return {
                    "allowed": False,
                    "reason": f"Bootstrap server '{bs}' not in allowed list",
                }

        # Topic restriction
        allowed_topics = policy.get("allowed_topics", [])
        if allowed_topics:
            topic = self._extract_kafka_flag(argv, "--topic")
            if topic and topic not in allowed_topics:
                return {
                    "allowed": False,
                    "reason": f"Topic '{topic}' not in allowed list",
                }

        # Group restriction
        allowed_groups = policy.get("allowed_groups", [])
        if allowed_groups:
            group = self._extract_kafka_flag(argv, "--group")
            if group and group not in allowed_groups:
                return {
                    "allowed": False,
                    "reason": f"Consumer group '{group}' not in allowed list",
                }

        # Bounded message requirement for consumers
        if policy.get("require_bounded", False):
            if tool in ("kafka-console-consumer", "kafka-console-share-consumer"):
                if "--max-messages" not in argv:
                    return {
                        "allowed": False,
                        "reason": "Policy requires --max-messages for consumer commands",
                    }
                max_limit = policy.get("max_messages_limit")
                if max_limit:
                    val = self._extract_kafka_flag(argv, "--max-messages")
                    if val:
                        try:
                            if int(val) > max_limit:
                                return {
                                    "allowed": False,
                                    "reason": (
                                        f"--max-messages {val} exceeds policy limit of {max_limit}"
                                    ),
                                }
                        except ValueError:
                            pass

        return {"allowed": True, "reason": "ok"}

    @staticmethod
    def _extract_kafka_flag(argv: list[str], flag: str) -> str | None:
        """Extract value for a --flag value pair from argv."""
        for i, a in enumerate(argv):
            if a == flag and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
        return None
