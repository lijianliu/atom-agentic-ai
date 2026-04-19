"""Tool adapters for atom-command-broker.

Each tool family has an adapter that handles:
- Request validation
- Argument normalization
- Resource extraction
- Policy enforcement specific to that tool family
- Mapping logical tool name to real executable
- Selecting default/effective execution mode
- Applying execution constraints
"""
from .base import BaseAdapter
from .gsutil_adapter import GsutilAdapter
from .gcloud_adapter import GcloudAdapter
from .kafka_adapter import KafkaAdapter, KafkaToolAdapter
from .hadoop_adapter import HadoopAdapter

# Adapter registry: maps logical tool name → adapter instance
_ADAPTER_REGISTRY: dict[str, BaseAdapter] = {}


def _register_defaults():
    """Register the built-in adapters."""
    global _ADAPTER_REGISTRY
    gsutil = GsutilAdapter()
    _ADAPTER_REGISTRY["gsutil"] = gsutil

    gcloud = GcloudAdapter()
    _ADAPTER_REGISTRY["gcloud"] = gcloud

    # Register per-tool Kafka adapters for proper discovery metadata
    kafka = KafkaAdapter()
    for tool_name in kafka.supported_tools():
        _ADAPTER_REGISTRY[tool_name] = KafkaToolAdapter(tool_name, kafka)

    # Hadoop / HDFS — each gets its own adapter instance
    _ADAPTER_REGISTRY["hadoop"] = HadoopAdapter("hadoop")
    _ADAPTER_REGISTRY["hdfs"] = HadoopAdapter("hdfs")


def get_adapter(tool: str) -> BaseAdapter | None:
    """Get the adapter for a given tool name."""
    if not _ADAPTER_REGISTRY:
        _register_defaults()
    return _ADAPTER_REGISTRY.get(tool, None)


def register_adapter(tool: str, adapter: BaseAdapter):
    """Register a custom adapter for a tool."""
    _ADAPTER_REGISTRY[tool] = adapter


def list_adapters() -> dict[str, BaseAdapter]:
    """Return the full adapter registry."""
    if not _ADAPTER_REGISTRY:
        _register_defaults()
    return dict(_ADAPTER_REGISTRY)
