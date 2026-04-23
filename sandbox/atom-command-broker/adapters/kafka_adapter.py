"""Kafka CLI tools adapter for atom-command-broker.

A single adapter handles all Kafka CLI tool families. The adapter uses the
tool name to determine behavior (consumer vs metadata vs verification).
"""
import os
from .base import BaseAdapter

# Kafka tools that are consumer-like (long-running, support streaming)
_CONSUMER_TOOLS = {
    "kafka-console-consumer",
    "kafka-console-share-consumer",
    "kafka-verifiable-consumer",
    "kafka-verifiable-share-consumer",
}

# Kafka tools that are short metadata queries (always buffered)
_METADATA_TOOLS = {
    "kafka-broker-api-versions",
    "kafka-consumer-groups",
    "kafka-get-offsets",
    "kafka-log-dirs",
    "kafka-metadata-quorum",
    "kafka-topics",
}

# All known Kafka tools
_ALL_KAFKA_TOOLS = _CONSUMER_TOOLS | _METADATA_TOOLS | {
    "kafka-replica-verification",
}


class KafkaAdapter(BaseAdapter):
    """Adapter for Apache Kafka CLI tools."""

    def supported_tools(self) -> list[str]:
        """Return list of all Kafka tool names this adapter handles."""
        return sorted(_ALL_KAFKA_TOOLS)

    def description(self) -> str:
        return "Apache Kafka CLI tools via broker"

    def supported_modes(self) -> list[str]:
        return ["buffered", "streaming"]

    def default_mode(self) -> str:
        return "buffered"

    def discovery_metadata(self) -> dict | None:
        tools_info = []
        for tool in sorted(_ALL_KAFKA_TOOLS):
            modes = ["buffered", "streaming"] if tool in _CONSUMER_TOOLS else ["buffered"]
            default = "streaming" if tool in _CONSUMER_TOOLS else "buffered"
            tools_info.append({
                "name": tool,
                "supported_modes": modes,
                "default_mode": default,
            })
        return {"kafka_tools": tools_info}

    def validate(self, argv: list[str]) -> str | None:
        """Basic structural validation for Kafka commands."""
        # argv is the args after the tool name; no specific structural
        # requirements at the adapter level beyond non-empty for most tools
        return None

    def normalize_args(self, argv: list[str]) -> list[str]:
        """Strip tool name prefix if accidentally included."""
        args = list(argv)
        if args and args[0] in _ALL_KAFKA_TOOLS:
            args = args[1:]
        return args

    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        """Determine effective mode for a Kafka command.

        This is called on the adapter instance — the caller provides the
        tool name context. We infer from argv or use requested_mode.
        """
        # We need the tool name, which isn't directly passed here.
        # The adapter is shared across all kafka tools, so we check
        # if the command looks like a consumer by examining args.
        is_consumer = self._looks_like_consumer(argv)

        if requested_mode == "streaming":
            return "streaming", "caller_requested_streaming"

        if requested_mode == "buffered":
            return "buffered", "caller_requested_buffered"

        # auto mode
        if is_consumer:
            # Check if bounded
            has_max_messages = "--max-messages" in argv
            if has_max_messages:
                return "buffered", "consumer_with_bounded_messages"
            return "streaming", "consumer_command_without_bounded_message_limit"

        return "buffered", "metadata_tool_default_buffered"

    def _looks_like_consumer(self, argv: list[str]) -> bool:
        """Heuristic: does this look like a consumer command?"""
        consumer_indicators = ["--topic", "--group", "--consumer-property"]
        return any(arg in consumer_indicators for arg in argv)

    def build_env(self, tool_policy: dict | None) -> dict:
        """Include Kafka-relevant environment variables."""
        env = super().build_env(tool_policy)
        for key in [
            "KAFKA_HOME",
            "KAFKA_OPTS",
            "KAFKA_LOG4J_OPTS",
            "JAVA_HOME",
            "JMX_PORT",
            "KAFKA_HEAP_OPTS",
            "KAFKA_JVM_PERFORMANCE_OPTS",
        ]:
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env


class KafkaToolAdapter(BaseAdapter):
    """Per-tool adapter wrapper for discovery; delegates to KafkaAdapter.

    The KafkaAdapter handles all Kafka tools, but for discovery we want
    each tool to appear individually with correct metadata.
    """

    def __init__(self, tool_name: str, parent: KafkaAdapter):
        self._tool_name = tool_name
        self._parent = parent

    def description(self) -> str:
        descriptions = {
            "kafka-broker-api-versions": "Query Kafka broker API versions",
            "kafka-console-consumer": "Consume messages from a Kafka topic",
            "kafka-console-share-consumer": "Consume messages via Kafka share groups",
            "kafka-consumer-groups": "List, describe, or manage Kafka consumer groups",
            "kafka-get-offsets": "Get Kafka topic partition offsets",
            "kafka-log-dirs": "Query Kafka broker log directories",
            "kafka-metadata-quorum": "Query Kafka metadata quorum info",
            "kafka-replica-verification": "Verify Kafka replica consistency",
            "kafka-topics": "List, describe, create, delete, or alter Kafka topics",
            "kafka-verifiable-consumer": "Verifiable Kafka consumer for testing",
            "kafka-verifiable-share-consumer": "Verifiable Kafka share consumer for testing",
        }
        return descriptions.get(self._tool_name, f"{self._tool_name} via broker")

    def supported_modes(self) -> list[str]:
        if self._tool_name in _CONSUMER_TOOLS:
            return ["buffered", "streaming"]
        return ["buffered"]

    def default_mode(self) -> str:
        if self._tool_name in _CONSUMER_TOOLS:
            return "streaming"
        return "buffered"

    def discovery_metadata(self) -> dict | None:
        return None

    def validate(self, argv: list[str]) -> str | None:
        return self._parent.validate(argv)

    def normalize_args(self, argv: list[str]) -> list[str]:
        return self._parent.normalize_args(argv)

    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        # Use tool-specific default
        if requested_mode == "streaming":
            return "streaming", "caller_requested_streaming"
        if requested_mode == "buffered":
            return "buffered", "caller_requested_buffered"
        # auto
        if self._tool_name in _CONSUMER_TOOLS:
            has_max = "--max-messages" in argv
            if has_max:
                return "buffered", "consumer_with_bounded_messages"
            return "streaming", "consumer_command_without_bounded_message_limit"
        return "buffered", "metadata_tool_default_buffered"

    def build_command(self, executable: str, argv: list[str]) -> list[str]:
        return self._parent.build_command(executable, argv)

    def build_env(self, tool_policy: dict | None) -> dict:
        return self._parent.build_env(tool_policy)
