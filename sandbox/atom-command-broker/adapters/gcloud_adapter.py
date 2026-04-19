"""gcloud adapter for atom-command-broker."""
import os
from .base import BaseAdapter, rewrite_container_paths


class GcloudAdapter(BaseAdapter):
    """Adapter for Google Cloud CLI (gcloud) commands."""

    def description(self) -> str:
        return "Google Cloud CLI (gcloud) via broker"

    def supported_modes(self) -> list[str]:
        return ["buffered"]

    def default_mode(self) -> str:
        return "buffered"

    def discovery_metadata(self) -> dict | None:
        return {
            "examples": [
                "gcloud storage buckets list",
                "gcloud storage buckets describe gs://bucket-name",
                "gcloud config list",
                "gcloud info",
                "gcloud version",
            ],
        }

    def validate(self, argv: list[str]) -> str | None:
        # No args is fine — gcloud prints its own usage/help
        return None

    def normalize_args(self, argv: list[str]) -> list[str]:
        """Strip leading 'gcloud' if present."""
        args = list(argv)
        if args and args[0] == "gcloud":
            args = args[1:]
        return args

    def effective_mode(
        self, argv: list[str], requested_mode: str
    ) -> tuple[str, str]:
        return "buffered", "gcloud_always_buffered"

    def build_command(self, executable: str, argv: list[str]) -> list[str]:
        """Build gcloud command, injecting policy-required flags."""
        cmd = [executable] + rewrite_container_paths(argv)
        # Ensure --format=json is not stripped; add --quiet if not present
        if "--quiet" not in argv and "-q" not in argv:
            cmd.append("--quiet")
        return cmd

    def build_env(self, tool_policy: dict | None) -> dict:
        """Include gcloud credential environment variables."""
        env = super().build_env(tool_policy)
        for key in [
            "CLOUDSDK_CONFIG",
            "CLOUDSDK_PYTHON",
            "CLOUDSDK_CORE_PROJECT",
            "CLOUDSDK_CORE_ACCOUNT",
            "CLOUDSDK_COMPUTE_REGION",
            "CLOUDSDK_COMPUTE_ZONE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        ]:
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env
