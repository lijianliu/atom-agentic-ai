"""Hadoop CLI adapter for atom-command-broker.

Handles `hadoop` and `hdfs` commands. Both tools share the same adapter
because they have identical argument structures for filesystem operations
(`hadoop fs ...` / `hdfs dfs ...`).

On Dataproc-style VMs the GCS connector JAR exists but is not on the
default Hadoop classpath and the `fs.gs.impl` property is not set in
core-site.xml. The adapter auto-discovers the connector and injects the
required classpath and `-D` flags when `gs://` paths appear in the
command, so `hadoop fs -ls gs://bucket/` Just Works™.
"""
import glob
import os
from .base import BaseAdapter

# Subcommands that are strictly read-only / informational
_READ_ONLY_SUBCOMMANDS = {
    # fs / dfs shell commands (read-only)
    "cat", "checksum", "count", "df", "du", "find", "getfacl", "getfattr",
    "getmerge", "head", "ls", "lsr", "stat", "tail", "test", "text", "usage",
    # classpath / version / info
    "classpath", "version", "envvars",
}

# Subcommands that modify data — blocked unless policy allows
_WRITE_SUBCOMMANDS = {
    "appendToFile", "chmod", "chown", "chgrp", "copyFromLocal",
    "copyToLocal", "cp", "createSnapshot", "deleteSnapshot",
    "expunge", "get", "mkdir", "moveFromLocal", "moveToLocal",
    "mv", "put", "renameSnapshot", "rm", "rmdir", "rmr",
    "setfacl", "setfattr", "setrep", "touchz", "truncate",
}

# Top-level hadoop subcommand groups (the word after `hadoop`)
_HADOOP_TOP_COMMANDS = {
    "fs", "jar", "classpath", "version", "envvars", "checknative",
    "distcp", "archive", "credential", "key", "trace",
}

# Top-level hdfs subcommand groups (the word after `hdfs`)
_HDFS_TOP_COMMANDS = {
    "dfs", "fsck", "getconf", "groups", "lsSnapshottableDir",
    "snapshotDiff", "version", "envvars", "classpath",
}

# Dangerous top-level commands that should never be allowed
_BLOCKED_TOP_COMMANDS = {
    "namenode", "datanode", "secondarynamenode", "journalnode",
    "zkfc", "balancer", "mover", "oiv", "oev", "dfsadmin",
    "haadmin", "debug", "dfsrouter", "dfsrouteradmin",
}

# Well-known locations for the GCS connector JAR (Dataproc, manual installs)
_GCS_CONNECTOR_SEARCH = [
    "/usr/local/share/google/dataproc/lib/gcs-connector.jar",
    "/usr/local/share/google/dataproc/lib/gcs-connector-*.jar",
    "/usr/lib/hadoop/lib/gcs-connector-*.jar",
    "/usr/lib/hadoop/lib/gcs-connector.jar",
]

# -D flags required when the GCS connector is not configured in core-site.xml
_GCS_D_FLAGS = [
    "-Dfs.gs.impl=com.google.cloud.hadoop.fs.gcs.GoogleHadoopFileSystem",
    "-Dfs.AbstractFileSystem.gs.impl=com.google.cloud.hadoop.fs.gcs.GoogleHadoopFS",
]


def _find_gcs_connector() -> str | None:
    """Locate the GCS connector JAR on the host."""
    for pattern in _GCS_CONNECTOR_SEARCH:
        matches = glob.glob(pattern)
        if matches:
            return sorted(matches)[-1]
    return None


def _args_reference_gs(argv: list[str]) -> bool:
    """Return True if any argument contains a gs:// path."""
    return any("gs://" in a for a in argv)


class HadoopAdapter(BaseAdapter):
    """Adapter for Apache Hadoop CLI (`hadoop` and `hdfs` commands)."""

    def __init__(self, tool_name: str = "hadoop"):
        self._tool_name = tool_name
        # Discover GCS connector once at init
        self._gcs_connector = _find_gcs_connector()

    def description(self) -> str:
        if self._tool_name == "hdfs":
            return "Hadoop HDFS CLI (hdfs) via broker"
        return "Apache Hadoop CLI (hadoop) via broker"

    def supported_modes(self) -> list[str]:
        return ["buffered"]

    def default_mode(self) -> str:
        return "buffered"

    def discovery_metadata(self) -> dict | None:
        if self._tool_name == "hdfs":
            return {
                "examples": [
                    "hdfs dfs -ls /",
                    "hdfs dfs -ls gs://bucket-name/path/",
                    "hdfs dfs -cat gs://bucket/file.txt",
                    "hdfs dfs -du -s -h gs://bucket/path/",
                    "hdfs dfs -stat '%n %b' gs://bucket/file.txt",
                    "hdfs dfs -count gs://bucket/path/",
                    "hdfs fsck / -files",
                    "hdfs getconf -confKey fs.defaultFS",
                    "hdfs version",
                ],
            }
        return {
            "examples": [
                "hadoop fs -ls /",
                "hadoop fs -ls gs://bucket-name/path/",
                "hadoop fs -cat gs://bucket/file.txt",
                "hadoop fs -du -s -h gs://bucket/path/",
                "hadoop fs -stat '%n %b' gs://bucket/file.txt",
                "hadoop fs -count gs://bucket/path/",
                "hadoop fs -cp gs://bucket/src gs://bucket/dst",
                "hadoop version",
                "hadoop classpath",
            ],
        }

    def validate(self, argv: list[str]) -> str | None:
        """Structural validation — not policy (that's in policy.py)."""
        if not argv:
            return None  # no args = show help

        top_cmd = argv[0]

        # Block dangerous admin commands at the adapter level
        if top_cmd in _BLOCKED_TOP_COMMANDS:
            return (
                f"Command '{top_cmd}' is an admin/system command and is "
                f"not available via the broker."
            )

        return None

    def normalize_args(self, argv: list[str]) -> list[str]:
        """Strip accidental leading tool name."""
        args = list(argv)
        if args and args[0] in ("hadoop", "hdfs"):
            args = args[1:]
        return args

    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        # Hadoop CLI is always buffered — commands finish and exit
        return "buffered", "hadoop_always_buffered"

    def build_command(self, executable: str, argv: list[str]) -> list[str]:
        """Build the full command, injecting GCS -D flags when needed.

        If any argument references gs:// and the GCS connector was found
        on the host, inject -D flags right after the top-level subcommand
        (e.g. `hadoop fs -Dfs.gs.impl=... -ls gs://bucket/`).
        """
        cmd = [executable]

        if self._gcs_connector and _args_reference_gs(argv):
            # Insert -D flags after the top-level subcommand (fs/dfs)
            if argv and argv[0] in ("fs", "dfs"):
                cmd.append(argv[0])
                for d_flag in _GCS_D_FLAGS:
                    cmd.append(d_flag)
                cmd.extend(argv[1:])
            else:
                cmd.extend(argv)
        else:
            cmd.extend(argv)

        return cmd

    def build_env(self, tool_policy: dict | None) -> dict:
        """Include Hadoop/Java environment variables.

        Auto-injects the GCS connector into HADOOP_CLASSPATH if found on
        the host (common on Dataproc VMs where core-site.xml doesn't
        include it by default).
        """
        env = super().build_env(tool_policy)
        for key in [
            "HADOOP_HOME",
            "HADOOP_CONF_DIR",
            "HADOOP_CLASSPATH",
            "HADOOP_OPTS",
            "HADOOP_CLIENT_OPTS",
            "HADOOP_HEAPSIZE",
            "HADOOP_HEAPSIZE_MAX",
            "HADOOP_HEAPSIZE_MIN",
            "HADOOP_LOG_DIR",
            "JAVA_HOME",
            "JAVA_LIBRARY_PATH",
            # GCS connector vars (common on Dataproc)
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "CLOUDSDK_CONFIG",
        ]:
            val = os.environ.get(key)
            if val:
                env[key] = val

        # Auto-inject GCS connector into HADOOP_CLASSPATH if not already there
        if self._gcs_connector:
            existing_cp = env.get("HADOOP_CLASSPATH", "")
            if self._gcs_connector not in existing_cp:
                if existing_cp:
                    env["HADOOP_CLASSPATH"] = existing_cp + ":" + self._gcs_connector
                else:
                    env["HADOOP_CLASSPATH"] = self._gcs_connector

        return env
