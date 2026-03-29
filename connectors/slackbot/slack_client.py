"""Simple Slack API client using polling.

This is a self-contained, dependency-light wrapper around Slack's Web API.
It handles message polling, sending, chunking, file uploads, and caching.
"""

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Slack's max message length before it auto-splits unpredictably
_SLACK_MAX_CHARS = 3500


def _split_message(text: str, max_len: int = _SLACK_MAX_CHARS) -> list[str]:
    """Split a long message into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at a double-newline (paragraph break)
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")

    return chunks


class SlackClient:
    """Minimal Slack client for polling messages and sending responses."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token
        self.headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        }
        self._bot_user_id: str | None = None
        self._last_ts: dict[str, str] = {}
        self._processed_msgs: set[str] = set()
        self._user_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}
        # Bypass env proxy vars — Slack is a direct call
        self._session = requests.Session()
        self._session.trust_env = False

    # ── Core API ────────────────────────────────────────────────────────

    def _request(
        self, method: str, endpoint: str, log_errors: bool = True, **kwargs,
    ) -> dict[str, Any]:
        """Make a request to the Slack API."""
        url = f"{self.BASE_URL}/{endpoint}"
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", 30)

        response = self._session.request(method, url, **kwargs)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if log_errors:
                logger.error("Slack API error: %s", error)
            raise RuntimeError(f"Slack API error: {error}")

        return data

    # ── Identity ────────────────────────────────────────────────────────

    def get_bot_user_id(self) -> str:
        """Get the bot's own user ID to ignore its messages."""
        if self._bot_user_id is None:
            data = self._request("GET", "auth.test")
            self._bot_user_id = data.get("user_id", "")
            logger.info("Bot user ID: %s", self._bot_user_id)
        return self._bot_user_id

    def get_user_name(self, user_id: str) -> str:
        """Resolve a Slack user ID to a display name (cached)."""
        if not user_id:
            return "unknown"
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            data = self._request(
                "GET", "users.info", log_errors=False, params={"user": user_id},
            )
            user_obj = data.get("user", {})
            profile = user_obj.get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_obj.get("real_name")
                or user_obj.get("name")
                or user_id
            )
        except Exception as exc:
            logger.info("Could not resolve user %s: %s", user_id, exc)
            name = user_id
        self._user_cache[user_id] = name
        return name

    def get_channel_name(self, channel_id: str) -> str:
        """Resolve a Slack channel ID to its name (cached)."""
        if not channel_id:
            return "unknown"
        if channel_id in self._channel_name_cache:
            return self._channel_name_cache[channel_id]
        try:
            data = self._request(
                "GET", "conversations.info", params={"channel": channel_id},
            )
            name = data.get("channel", {}).get("name") or channel_id
        except Exception as exc:
            logger.warning("Could not resolve channel %s: %s", channel_id, exc)
            name = channel_id
        self._channel_name_cache[channel_id] = name
        return name

    def get_channel_id(self, channel_name: str) -> str | None:
        """Convert channel name to ID. Handles both names and IDs."""
        if channel_name.startswith(("C", "D")):
            return channel_name

        name = channel_name.lstrip("#")

        for types in ["public_channel", "public_channel,private_channel"]:
            cursor = None
            while True:
                params: dict[str, Any] = {"limit": 200, "types": types}
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self._request("GET", "conversations.list", params=params)
                except RuntimeError as e:
                    if "missing_scope" in str(e):
                        break
                    raise

                for channel in data.get("channels", []):
                    if channel.get("name") == name:
                        return channel.get("id")

                cursor = data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        logger.warning("Channel not found: %s", channel_name)
        return None

    # ── Messages ────────────────────────────────────────────────────────

    def get_new_messages(self, channel_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch new messages from a channel since last poll."""
        params: dict[str, Any] = {"channel": channel_id, "limit": limit}

        last_ts = self._last_ts.get(channel_id)
        if last_ts:
            params["oldest"] = last_ts
        else:
            self._last_ts[channel_id] = str(time.time())
            logger.info("First poll for %s — skipping historical messages", channel_id)
            return []

        try:
            data = self._request("GET", "conversations.history", params=params)
        except RuntimeError as e:
            if "not_in_channel" in str(e):
                logger.warning("Bot not in channel %s, joining...", channel_id)
                self.join_channel(channel_id)
                return []
            raise

        messages = data.get("messages", [])
        if messages:
            self._last_ts[channel_id] = messages[0].get("ts", "")

        bot_id = self.get_bot_user_id()
        filtered = []
        for msg in reversed(messages):
            msg_ts = msg.get("ts", "")
            msg_id = f"{channel_id}:{msg_ts}"

            if msg_id in self._processed_msgs:
                continue
            if msg.get("user") == bot_id:
                continue
            if msg.get("type") != "message":
                continue
            if "subtype" in msg and msg["subtype"] != "file_share":
                continue

            self._processed_msgs.add(msg_id)
            filtered.append(msg)

        # Cap the processed set to prevent unbounded growth
        if len(self._processed_msgs) > 1000:
            excess = len(self._processed_msgs) - 500
            for _ in range(excess):
                self._processed_msgs.pop()

        return filtered

    def join_channel(self, channel_id: str) -> bool:
        """Join a channel."""
        try:
            self._request("POST", "conversations.join", json={"channel": channel_id})
            logger.info("Joined channel: %s", channel_id)
            return True
        except RuntimeError as e:
            logger.error("Failed to join channel %s: %s", channel_id, e)
            return False

    def send_message(
        self, channel_id: str, text: str, thread_ts: str | None = None,
    ) -> str | None:
        """Send a message. Auto-chunks long messages. Returns ts of first chunk."""
        chunks = _split_message(text)
        first_ts = None

        for chunk in chunks:
            payload: dict[str, Any] = {"channel": channel_id, "text": chunk}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            try:
                resp = self._request("POST", "chat.postMessage", json=payload)
                if first_ts is None:
                    first_ts = resp.get("ts")
            except RuntimeError as e:
                logger.error("Failed to send message: %s", e)
                return first_ts

        return first_ts

    def update_message(self, channel_id: str, ts: str, text: str) -> bool:
        """Update an existing message."""
        try:
            self._request(
                "POST", "chat.update",
                json={"channel": channel_id, "ts": ts, "text": text},
            )
            return True
        except RuntimeError as e:
            logger.error("Failed to update message: %s", e)
            return False

    def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        """Add a reaction to a message."""
        try:
            self._request(
                "POST", "reactions.add",
                json={"channel": channel_id, "timestamp": timestamp, "name": emoji},
            )
            return True
        except RuntimeError:
            return False

    # ── File operations ─────────────────────────────────────────────────

    def upload_file(
        self,
        channel_id: str,
        file_path: str,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """Upload a file using the modern 3-step Slack API flow."""
        try:
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)

            res = self._request(
                "GET", "files.getUploadURLExternal",
                params={"filename": filename, "length": file_size},
            )
            upload_url = res.get("upload_url")
            file_id = res.get("file_id")
            if not upload_url or not file_id:
                logger.error("Failed to get upload URL: %s", res)
                return False

            with open(file_path, "rb") as f:
                upload_res = requests.post(upload_url, data=f, timeout=120)
                upload_res.raise_for_status()

            comment = initial_comment
            if comment and len(comment) > _SLACK_MAX_CHARS:
                comment = comment[:_SLACK_MAX_CHARS] + "\n\n_…(truncated)_"

            completion_payload: dict[str, Any] = {
                "files": [{"id": file_id, "title": filename}],
                "channel_id": channel_id,
            }
            if comment:
                completion_payload["initial_comment"] = comment
            if thread_ts:
                completion_payload["thread_ts"] = thread_ts

            self._request("POST", "files.completeUploadExternal", json=completion_payload)
            return True

        except Exception as e:
            logger.error("Failed to upload file: %s", e)
            return False

    def download_file(self, url: str, save_path: str) -> bool:
        """Download a file from Slack (requires bot token auth)."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            headers = {"Authorization": f"Bearer {self.bot_token}"}

            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            logger.info("Downloaded file to: %s", save_path)
            return True
        except Exception as e:
            logger.error("Failed to download file from %s: %s", url, e)
            return False

    # ── Diagnostics ─────────────────────────────────────────────────────

    def check_scopes(self) -> dict[str, bool | str]:
        """Check which required scopes are available."""
        results: dict[str, bool | str] = {}

        try:
            data = self._request("GET", "auth.test")
            results["auth.test"] = True
            results["_bot_name"] = data.get("user", "unknown")
            results["_team"] = data.get("team", "unknown")
        except Exception as e:
            results["auth.test"] = str(e)
            return results

        try:
            self._request("GET", "conversations.list", params={"limit": 1})
            results["channels:read"] = True
        except RuntimeError as e:
            results["channels:read"] = str(e)

        results["channels:history"] = "(needs channel to test)"
        results["channels:join"] = "(needs channel to test)"
        results["chat:write"] = "(needs channel to test)"
        results["reactions:write"] = "(needs channel to test)"

        return results
