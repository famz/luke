#!/usr/bin/env python3
"""
Feishu channel listener that mirrors messages into a Bitable table.

Update the constants below (`CHAT_ID`, `BITABLE_APP_TOKEN`, `BITABLE_TABLE_ID`,
and `BITABLE_FIELD_MAPPING`) with the identifiers from your own workspace before
running the script.

Usage:
    python feishu_bot.py --app-id <APP_ID> --app-secret <APP_SECRET>
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Hard-coded configuration (replace the placeholders with real identifiers)
# ---------------------------------------------------------------------------

CHAT_ID = "oc_xxxxxxxxxxxxxxxxxxxxxxxxx"
BITABLE_APP_TOKEN = "bascnxxxxxxxxxxxxxxxxxxxx"
BITABLE_TABLE_ID = "tblxxxxxxxxxxxxxxxxxxxx"

# Map internal keys to the actual field names in your Bitable table.
BITABLE_FIELD_MAPPING = {
    "message": "Message",
    "message_id": "Message ID",
    "sender": "Sender",
    "sent_at": "Sent At",
}


class FeishuBot:
    """Poll a Feishu chat and create Bitable records for every new message."""

    API_BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, poll_interval: float = 5.0) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.poll_interval = poll_interval

        self.chat_id = CHAT_ID
        self.bitable_app_token = BITABLE_APP_TOKEN
        self.bitable_table_id = BITABLE_TABLE_ID
        self.field_mapping = BITABLE_FIELD_MAPPING

        self._tenant_access_token: Optional[str] = None
        self._tenant_token_expiry: float = 0.0

    def run(self) -> None:
        """Start polling the target Feishu chat."""
        last_seen = self._initialize_cursor()
        logging.info("Starting polling loop. Initial cursor at %s", last_seen)

        while True:
            try:
                new_messages = self._fetch_messages(start_time=last_seen + 1)
                if new_messages:
                    new_messages.sort(key=lambda msg: int(msg.get("create_time", "0")))
                    for message in new_messages:
                        create_time = int(message.get("create_time", "0"))
                        last_seen = max(last_seen, create_time)
                        if message.get("chat_id") != self.chat_id:
                            continue

                        text = self._extract_text(message)
                        if not text:
                            logging.debug(
                                "Skipping message %s due to unsupported type/content",
                                message.get("message_id"),
                            )
                            continue

                        fields = self._build_record_fields(message, text)
                        try:
                            record_id = self._create_bitable_record(fields)
                        except Exception:  # pylint: disable=broad-except
                            logging.exception(
                                "Failed to create Bitable record for message %s",
                                message.get("message_id"),
                            )
                        else:
                            logging.info(
                                "Created Bitable record %s for message %s",
                                record_id,
                                message.get("message_id"),
                            )
            except Exception:  # pylint: disable=broad-except
                logging.exception("Unexpected error in polling loop")

            time.sleep(self.poll_interval)

    def _initialize_cursor(self) -> int:
        """Seed the cursor with the newest message timestamp (avoid duplicates)."""
        try:
            messages = self._fetch_messages()
        except Exception:  # pylint: disable=broad-except
            logging.exception("Initial fetch failed; defaulting cursor to current time")
            return int(time.time() * 1000)

        if not messages:
            return int(time.time() * 1000)

        newest = max(int(msg.get("create_time", "0")) for msg in messages)
        return newest

    def _fetch_messages(self, start_time: Optional[int] = None) -> List[Dict]:
        """Retrieve messages from the configured chat."""
        params = {
            "container_id_type": "chat",
            "container_id": self.chat_id,
            "page_size": 50,
        }
        if start_time:
            params["start_time"] = str(start_time)

        response = requests.get(
            f"{self.API_BASE_URL}/im/v1/messages",
            headers=self._auth_headers(),
            params=params,
            timeout=10,
        )
        data = self._process_response(response)
        items = data.get("items", [])
        return items

    def _create_bitable_record(self, fields: Dict[str, str]) -> str:
        """Create a new record in the configured Bitable table."""
        payload = {"fields": fields}
        response = requests.post(
            f"{self.API_BASE_URL}/bitable/v1/apps/{self.bitable_app_token}/tables/{self.bitable_table_id}/records",
            headers=self._auth_headers(),
            json=payload,
            timeout=10,
        )
        data = self._process_response(response)
        record_id = data.get("record", {}).get("record_id")
        if not record_id:
            raise RuntimeError("Bitable record creation did not return a record_id")
        return record_id

    def _auth_headers(self) -> Dict[str, str]:
        """Return headers with a valid tenant access token."""
        token = self._ensure_tenant_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _ensure_tenant_access_token(self) -> str:
        """Acquire or refresh the tenant access token as needed."""
        now = time.time()
        if self._tenant_access_token and now < self._tenant_token_expiry - 60:
            return self._tenant_access_token

        response = requests.post(
            f"{self.API_BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(
                f"Failed to obtain tenant access token: {payload.get('msg')}"
            )

        tenant_access_token = payload.get("tenant_access_token")
        if not tenant_access_token:
            raise RuntimeError("Tenant access token missing from response")

        expire_in = payload.get("expire", payload.get("expire_in", 0))
        self._tenant_access_token = tenant_access_token
        self._tenant_token_expiry = now + float(expire_in)
        return tenant_access_token

    @staticmethod
    def _process_response(response: requests.Response) -> Dict:
        """Validate a Feishu Open API response and return the data payload."""
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {payload.get('msg')} ({payload.get('code')})")
        return payload.get("data", {})

    def _build_record_fields(self, message: Dict, text: str) -> Dict[str, str]:
        """Map message content into the configured Bitable fields."""
        fields = {}

        message_field = self.field_mapping.get("message")
        if message_field:
            fields[message_field] = text

        message_id_field = self.field_mapping.get("message_id")
        if message_id_field:
            fields[message_id_field] = message.get("message_id", "")

        sender_field = self.field_mapping.get("sender")
        if sender_field:
            fields[sender_field] = self._format_sender(message)

        sent_at_field = self.field_mapping.get("sent_at")
        if sent_at_field:
            fields[sent_at_field] = self._format_timestamp(message.get("create_time"))

        return fields

    @staticmethod
    def _format_timestamp(timestamp_ms: Optional[str]) -> str:
        """Convert Feishu millisecond timestamps to ISO-8601 strings."""
        try:
            millis = int(timestamp_ms or "0")
        except ValueError:
            millis = 0
        dt = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
        return dt.isoformat()

    @staticmethod
    def _format_sender(message: Dict) -> str:
        """Extract a human-readable identifier for the message sender."""
        sender = message.get("sender", {})
        if isinstance(sender, dict):
            sender_id = sender.get("sender_id") or {}
            if isinstance(sender_id, dict):
                for key in ("name", "open_id", "union_id", "user_id"):
                    value = sender_id.get(key)
                    if value:
                        return str(value)
            for key in ("name", "display_name", "id"):
                value = sender.get(key)
                if value:
                    return str(value)
        return "unknown"

    @staticmethod
    def _extract_text(message: Dict) -> Optional[str]:
        """Pull the textual representation out of a Feishu message."""
        body = message.get("body", {})
        message_type = body.get("type") or message.get("message_type")
        raw_content = body.get("content")

        if not raw_content:
            return None

        try:
            content = json.loads(raw_content)
        except (TypeError, ValueError):
            logging.debug("Message content is not valid JSON: %s", raw_content)
            return None

        if message_type == "text":
            return content.get("text")

        if message_type == "post":
            return FeishuBot._flatten_post_content(content)

        # Fallback: keep the raw JSON content for unsupported types.
        return json.dumps(content, ensure_ascii=False)

    @staticmethod
    def _flatten_post_content(content: Dict) -> str:
        """Convert a rich-text (post) message into plain text."""
        lines: List[str] = []
        rich_lines = content.get("content", [])
        if not isinstance(rich_lines, list):
            return json.dumps(content, ensure_ascii=False)

        for rich_line in rich_lines:
            fragments: List[str] = []
            if not isinstance(rich_line, list):
                continue
            for element in rich_line:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag")
                if tag == "text":
                    fragments.append(str(element.get("text", "")))
                elif tag == "a":
                    text = str(element.get("text", ""))
                    href = element.get("href")
                    fragments.append(f"{text} ({href})" if href else text)
                elif tag == "at":
                    fragments.append(f"@{element.get('user_name', element.get('text', ''))}")
                else:
                    fragments.append(json.dumps(element, ensure_ascii=False))
            if fragments:
                lines.append("".join(fragments))

        title = content.get("title")
        if title:
            lines.insert(0, f"# {title}")

        return "\n".join(lines)


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="Feishu app ID (a.k.a. App ID)")
    parser.add_argument("--app-secret", required=True, help="Feishu app secret")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds to wait between message polls (default: 5.0)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (e.g. DEBUG, INFO, WARNING)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    """Entry point for the Feishu bot."""
    args = parse_args(argv or sys.argv[1:])

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    placeholders = {
        "CHAT_ID": CHAT_ID,
        "BITABLE_APP_TOKEN": BITABLE_APP_TOKEN,
        "BITABLE_TABLE_ID": BITABLE_TABLE_ID,
    }

    if any(value.startswith(("oc_x", "bascn", "tbl")) and "x" in value for value in placeholders.values()):
        logging.warning("Update CHAT_ID, BITABLE_APP_TOKEN, and BITABLE_TABLE_ID before running in production.")

    bot = FeishuBot(args.app_id, args.app_secret, poll_interval=args.poll_interval)
    try:
        bot.run()
    except KeyboardInterrupt:
        logging.info("Shutting down Feishu bot.")


if __name__ == "__main__":
    main()
