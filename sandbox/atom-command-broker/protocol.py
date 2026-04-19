"""atom-command-broker protocol definitions.

Versioned protocol for communication between atom-command-proxy and
atom-command-broker. Supports buffered and streaming modes.

Protocol Version 1:
  - Buffered mode: single JSON request → single JSON response
  - Streaming mode: single JSON request → sequence of length-prefixed frames

Frame format (streaming):
  4 bytes: frame length (big-endian uint32)
  N bytes: JSON-encoded frame payload
"""
import enum
import json
import struct

# Protocol version — increment on breaking changes
PROTO_VERSION = 1


class FrameType(str, enum.Enum):
    """Frame types for streaming protocol."""
    START = "start"
    STDOUT = "stdout"
    STDERR = "stderr"
    EXIT = "exit"
    ERROR = "error"


class ErrorCategory(str, enum.Enum):
    """Error categories for structured error reporting."""
    POLICY_DENIED = "policy_denied"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    EXECUTION_FAILURE = "execution_failure"
    INTERNAL_ERROR = "internal_error"
    RATE_LIMITED = "rate_limited"


def decode_request(data: bytes) -> dict:
    """Decode a request from the proxy."""
    return json.loads(data.decode("utf-8"))


def encode_response(resp: dict) -> bytes:
    """Encode a response for the proxy."""
    return json.dumps(resp).encode("utf-8")


def encode_frame(frame_type: FrameType, payload: dict) -> bytes:
    """Encode a streaming frame: 4-byte length prefix + JSON payload.

    Frame format:
      [4 bytes: uint32 big-endian length][JSON payload]
    """
    payload["frame_type"] = frame_type.value
    data = json.dumps(payload).encode("utf-8")
    return struct.pack("!I", len(data)) + data


def decode_frame(data: bytes) -> tuple[dict, bytes]:
    """Decode one frame from a buffer. Returns (frame_dict, remaining_buffer).

    Raises ValueError if buffer doesn't contain a complete frame.
    """
    if len(data) < 4:
        raise ValueError("Incomplete frame header")
    length = struct.unpack("!I", data[:4])[0]
    if len(data) < 4 + length:
        raise ValueError("Incomplete frame body")
    frame_data = data[4:4 + length]
    remaining = data[4 + length:]
    frame = json.loads(frame_data.decode("utf-8"))
    return frame, remaining


def make_error_response(
    request_id: str,
    category: ErrorCategory,
    message: str,
) -> dict:
    """Create a standard error response."""
    return {
        "version": PROTO_VERSION,
        "request_id": request_id,
        "ok": False,
        "operation": "execute",
        "error_category": category.value,
        "error": message,
        "stdout": "",
        "stderr": message,
        "exit_code": _exit_code_for_category(category),
    }


def make_health_response(request_id: str) -> dict:
    """Create a health check response."""
    import time
    return {
        "version": PROTO_VERSION,
        "request_id": request_id,
        "ok": True,
        "operation": "health",
        "status": "healthy",
        "broker_version": PROTO_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _exit_code_for_category(category: ErrorCategory) -> int:
    """Map error category to a conventional exit code."""
    mapping = {
        ErrorCategory.POLICY_DENIED: 403,
        ErrorCategory.VALIDATION_ERROR: 400,
        ErrorCategory.TIMEOUT: 124,
        ErrorCategory.EXECUTION_FAILURE: 1,
        ErrorCategory.INTERNAL_ERROR: 500,
        ErrorCategory.RATE_LIMITED: 429,
    }
    return mapping.get(category, 1)
