"""gsutil adapter for atom-command-broker."""
import os
from .base import BaseAdapter


class GsutilAdapter(BaseAdapter):
    """Adapter for Google Cloud Storage gsutil commands."""

    def description(self) -> str:
        return "Google Cloud Storage CLI (gsutil) via broker"

    def supported_modes(self) -> list[str]:
        return ["buffered"]

    def default_mode(self) -> str:
        return "buffered"

    def discovery_metadata(self) -> dict | None:
        return {
            "examples": [
                "gsutil ls gs://bucket-name",
                "gsutil cat gs://bucket/file.txt",
                "gsutil stat gs://bucket/file.txt",
                "gsutil cp gs://bucket/file.txt /workspace/file.txt",
            ],
        }

    def validate(self, argv: list[str]) -> str | None:
        if not argv:
            return "gsutil requires at least a subcommand"
        # Find the subcommand (first non-flag arg)
        subcmd = None
        for a in argv:
            if not a.startswith("-"):
                subcmd = a
                break
        if subcmd is None:
            return "No subcommand found in gsutil arguments"
        return None

    def normalize_args(self, argv: list[str]) -> list[str]:
        """Strip any leading 'gsutil' if the caller included it."""
        if argv and argv[0] == "gsutil":
            return argv[1:]
        return list(argv)

    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        # gsutil is always buffered — short-lived commands
        return "buffered", "gsutil_always_buffered"

    def build_env(self, tool_policy: dict | None) -> dict:
        """Include gcloud/gsutil credential environment variables."""
        env = super().build_env(tool_policy)
        # Allow gcloud credential paths through
        for key in [
            "CLOUDSDK_CONFIG",
            "CLOUDSDK_PYTHON",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "CLOUDSDK_CORE_PROJECT",
            "BOTO_CONFIG",
            "BOTO_PATH",
        ]:
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env
