#!/usr/bin/env python3
"""
Feishu channel listener that mirrors chat messages into Bitable tables.

Each entry in `CHAT_BEHAVIORS` describes how a specific Feishu chat should be
transformed before being inserted into a Bitable record. Replace the placeholder
identifiers with the actual chat IDs, Bitable app tokens, and table IDs from
your environment before running the script.

Usage:
    python feishu_bot.py --app-id <APP_ID> --app-secret <APP_SECRET>
"""

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests
from requests import Response, Session

# ---------------------------------------------------------------------------
# Hard-coded configuration (replace the placeholders with real identifiers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatBehavior:
    """Mapping that describes how a chat should be mirrored into Bitable."""

    chat_id: str
    bitable_app_token: str
    bitable_table_id: str
    field_mapping: Dict[str, str]
    name: str = ""
    static_fields: Dict[str, str] = field(default_factory=dict)
    extra_field_builder: Optional[Callable[[Dict, str], Dict[str, str]]] = None


def _todo_extra_fields(_: Dict, text: str) -> Dict[str, str]:
    """Generate extra fields for todo-style chats."""
    return {"Status": "Pending", "Todo Detail": text.strip()}


def _accounting_extra_fields(_: Dict, text: str) -> Dict[str, str]:
    """Extract a numeric amount from the text if present."""
    match = re.search(r"([+-]?\d+(?:\.\d{1,2})?)", text)
    return {"Amount": match.group(1)} if match else {}


def _automation_extra_fields(message: Dict, _: str) -> Dict[str, str]:
    """Mirror the message type to highlight automation triggers."""
    message_type = message.get("message_type") or message.get("body", {}).get("type")
    return {"Trigger Type": message_type or "unknown"}


def _reminder_extra_fields(_: Dict, text: str) -> Dict[str, str]:
    """Use the first line of text as reminder title if available."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return {"Reminder Title": first_line}


CHAT_BEHAVIORS: Dict[str, ChatBehavior] = {
    # Todo chat
    "oc_xxxxxxxxxxxxxxxxxxxxx_todo": ChatBehavior(
        chat_id="oc_xxxxxxxxxxxxxxxxxxxxx_todo",
        bitable_app_token="bascnxxxxxxxxxxxxxxxxxxxx_todo",
        bitable_table_id="tblxxxxxxxxxxxxxxxxxxxx_todo",
        field_mapping={
            "message": "Task",
            "message_id": "Source Message ID",
            "sender": "Owner",
            "sent_at": "Created At",
        },
        name="Todo",
        static_fields={"Category": "Todo"},
        extra_field_builder=_todo_extra_fields,
    ),
    # Accounting chat
    "oc_xxxxxxxxxxxxxxxxxxxxx_accounting": ChatBehavior(
        chat_id="oc_xxxxxxxxxxxxxxxxxxxxx_accounting",
        bitable_app_token="bascnxxxxxxxxxxxxxxxxxxxx_accounting",
        bitable_table_id="tblxxxxxxxxxxxxxxxxxxxx_accounting",
        field_mapping={
            "message": "Description",
            "message_id": "Message ID",
            "sender": "Submitted By",
            "sent_at": "Submitted At",
        },
        name="Accounting",
        static_fields={"Category": "Accounting"},
        extra_field_builder=_accounting_extra_fields,
    ),
    # Automation chat
    "oc_xxxxxxxxxxxxxxxxxxxxx_automation": ChatBehavior(
        chat_id="oc_xxxxxxxxxxxxxxxxxxxxx_automation",
        bitable_app_token="bascnxxxxxxxxxxxxxxxxxxxx_automation",
        bitable_table_id="tblxxxxxxxxxxxxxxxxxxxx_automation",
        field_mapping={
            "message": "Instruction",
            "message_id": "Message ID",
            "sender": "Requested By",
            "sent_at": "Requested At",
        },
        name="Automation",
        static_fields={"Category": "Automation"},
        extra_field_builder=_automation_extra_fields,
    ),
    # Reminder chat
    "oc_xxxxxxxxxxxxxxxxxxxxx_reminder": ChatBehavior(
        chat_id="oc_xxxxxxxxxxxxxxxxxxxxx_reminder",
        bitable_app_token="bascnxxxxxxxxxxxxxxxxxxxx_reminder",
        bitable_table_id="tblxxxxxxxxxxxxxxxxxxxx_reminder",
        field_mapping={
            "message": "Reminder Body",
            "message_id": "Message ID",
            "sender": "Created By",
            "sent_at": "Reminder Time",
        },
        name="Reminder",
        static_fields={"Category": "Reminder"},
        extra_field_builder=_reminder_extra_fields,
    ),
}


class FeishuBot:
    """Poll Feishu chats and create Bitable records for every new message."""

    API_BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        poll_interval: float = 5.0,
        behaviors: Optional[Dict[str, ChatBehavior]] = None,
        session: Optional[Session] = None,
        auto_initialize: bool = True,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")

        self.app_id = app_id
        self.app_secret = app_secret
        self.poll_interval = poll_interval
        self.behaviors = behaviors or CHAT_BEHAVIORS
        if not self.behaviors:
            raise ValueError("At least one chat behavior must be configured.")

        self._session: Session = session or Session()
        self._tenant_access_token: Optional[str] = None
        self._tenant_token_expiry: float = 0.0
        self._cursors: Dict[str, int] = {chat_id: 0 for chat_id in self.behaviors}
        self._initialized = False

        if auto_initialize:
            self._initialize_all_cursors()

    def run(self) -> None:
        """Start polling all configured Feishu chats."""
        if not self._initialized:
            self._initialize_all_cursors()

        logging.info(
            "Starting polling loop for %d chats: %s",
            len(self.behaviors),
            ", ".join(sorted(self.behaviors)),
        )

        while True:
            for chat_id, behavior in self.behaviors.items():
                try:
                    last_seen = self._cursors.get(chat_id, 0)
                    new_last_seen = self._poll_behavior(behavior, last_seen)
                    self._cursors[chat_id] = max(last_seen, new_last_seen)
                except Exception:  # pylint: disable=broad-except
                    logging.exception("Unexpected error while polling chat %s", chat_id)

            time.sleep(self.poll_interval)

    def _initialize_all_cursors(self) -> None:
        """Seed cursors for all behaviors to avoid duplicating historical messages."""
        for chat_id, behavior in self.behaviors.items():
            self._cursors[chat_id] = self._initialize_cursor(behavior)
        self._initialized = True

    def _poll_behavior(self, behavior: ChatBehavior, last_seen: int) -> int:
        """Fetch new messages for a behavior and mirror them into Bitable."""
        start_time = last_seen + 1 if last_seen else None
        messages = self._fetch_messages(behavior, start_time=start_time)
        if not messages:
            return last_seen

        messages.sort(key=lambda msg: int(msg.get("create_time", "0")))
        for message in messages:
            create_time = int(message.get("create_time", "0"))
            last_seen = max(last_seen, create_time)
            if message.get("chat_id") and message["chat_id"] != behavior.chat_id:
                logging.debug(
                    "Skipping message %s from chat %s (expected %s)",
                    message.get("message_id"),
                    message.get("chat_id"),
                    behavior.chat_id,
                )
                continue

            text = self._extract_text(message)
            if not text:
                logging.debug(
                    "Skipping message %s due to unsupported type/content",
                    message.get("message_id"),
                )
                continue

            fields = self._build_record_fields(behavior, message, text)
            try:
                record_id = self._create_bitable_record(behavior, fields)
            except Exception:  # pylint: disable=broad-except
                logging.exception(
                    "Failed to create Bitable record for message %s",
                    message.get("message_id"),
                )
            else:
                logging.info(
                    "Created Bitable record %s for message %s in chat %s",
                    record_id,
                    message.get("message_id"),
                    behavior.chat_id,
                )
        return last_seen

    def _initialize_cursor(self, behavior: ChatBehavior) -> int:
        """Seed the cursor for a chat with the newest message timestamp."""
        try:
            messages = self._fetch_messages(behavior)
        except Exception:  # pylint: disable=broad-except
            logging.exception(
                "Initial fetch failed for chat %s; defaulting cursor to current time",
                behavior.chat_id,
            )
            return int(time.time() * 1000)

        if not messages:
            return int(time.time() * 1000)

        newest = max(int(msg.get("create_time", "0")) for msg in messages)
        return newest

    def _fetch_messages(
        self,
        behavior: ChatBehavior,
        start_time: Optional[int] = None,
    ) -> List[Dict]:
        """Retrieve messages from a specific chat."""
        params = {
            "container_id_type": "chat",
            "container_id": behavior.chat_id,
            "page_size": 50,
        }
        if start_time:
            params["start_time"] = str(start_time)

        response = self._session.get(
            f"{self.API_BASE_URL}/im/v1/messages",
            headers=self._auth_headers(),
            params=params,
            timeout=10,
        )
        data = self._process_response(response)
        items = data.get("items", [])
        return items

    def _create_bitable_record(
        self,
        behavior: ChatBehavior,
        fields: Dict[str, str],
    ) -> str:
        """Create a new record in the behavior's configured Bitable table."""
        payload = {"fields": fields}
        response = self._session.post(
            f"{self.API_BASE_URL}/bitable/v1/apps/{behavior.bitable_app_token}/tables/{behavior.bitable_table_id}/records",
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

        response = self._session.post(
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
    def _process_response(response: Response) -> Dict:
        """Validate a Feishu Open API response and return the data payload."""
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu API error: {payload.get('msg')} ({payload.get('code')})"
            )
        return payload.get("data", {})

    def _build_record_fields(
        self,
        behavior: ChatBehavior,
        message: Dict,
        text: str,
    ) -> Dict[str, str]:
        """Map message content into the configured Bitable fields."""
        fields: Dict[str, str] = {}

        mapping = behavior.field_mapping
        message_field = mapping.get("message")
        if message_field:
            fields[message_field] = text

        message_id_field = mapping.get("message_id")
        if message_id_field:
            fields[message_id_field] = message.get("message_id", "")

        sender_field = mapping.get("sender")
        if sender_field:
            fields[sender_field] = self._format_sender(message)

        sent_at_field = mapping.get("sent_at")
        if sent_at_field:
            fields[sent_at_field] = self._format_timestamp(message.get("create_time"))

        if behavior.static_fields:
            fields.update(behavior.static_fields)

        if behavior.extra_field_builder:
            try:
                fields.update(behavior.extra_field_builder(message, text))
            except Exception:  # pylint: disable=broad-except
                logging.exception(
                    "Extra field builder failed for message %s in chat %s",
                    message.get("message_id"),
                    behavior.chat_id,
                )

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

    placeholder_tokens = [
        value
        for behavior in CHAT_BEHAVIORS.values()
        for value in (behavior.chat_id, behavior.bitable_app_token, behavior.bitable_table_id)
    ]
    if any(token.count("x") > 3 for token in placeholder_tokens):
        logging.warning(
            "Update chat IDs, app tokens, and table IDs in CHAT_BEHAVIORS before running in production."
        )

    bot = FeishuBot(
        args.app_id,
        args.app_secret,
        poll_interval=args.poll_interval,
        behaviors=CHAT_BEHAVIORS,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logging.info("Shutting down Feishu bot.")


if __name__ == "__main__":
    main()
