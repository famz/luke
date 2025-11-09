import json
import pathlib
import sys
import time
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import requests

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

import feishu_bot
from feishu_bot import (
    ChatBehavior,
    FeishuBot,
    _accounting_extra_fields,
    _automation_extra_fields,
    _reminder_extra_fields,
    _todo_extra_fields,
)


class DummyResponse:
    def __init__(self, payload: Dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"Status code {self.status_code}")

    def json(self) -> Dict:
        return self._payload


def make_behavior(extra=None) -> ChatBehavior:
    return ChatBehavior(
        chat_id="oc_test_chat",
        bitable_app_token="bascn_test_token",
        bitable_table_id="tbl_test_table",
        field_mapping={
            "message": "Message",
            "message_id": "Message ID",
            "sender": "Sender",
            "sent_at": "Sent At",
        },
        name="Test",
        static_fields={"Category": "General"},
        extra_field_builder=extra,
    )


class StubSession:
    def __init__(self, messages: Optional[List[Dict]] = None) -> None:
        self.messages = messages or []
        self.get_calls: List[Dict] = []
        self.post_calls: List[Dict] = []
        self.token_requests = 0

    def get(self, url: str, headers: Dict, params: Dict, timeout: int) -> DummyResponse:
        self.get_calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        payload = {"code": 0, "data": {"items": list(self.messages)}}
        return DummyResponse(payload)

    def post(
        self,
        url: str,
        json: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        timeout: int = 10,
    ) -> DummyResponse:
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if "tenant_access_token" in url:
            self.token_requests += 1
            return DummyResponse({"code": 0, "tenant_access_token": "token123", "expire": 3600})
        return DummyResponse({"code": 0, "data": {"record": {"record_id": "rec-123"}}})


def test_process_response_success() -> None:
    response = DummyResponse({"code": 0, "data": {"items": [1, 2, 3]}})
    assert FeishuBot._process_response(response) == {"items": [1, 2, 3]}


def test_process_response_error_code() -> None:
    response = DummyResponse({"code": 999, "msg": "boom"})
    with pytest.raises(RuntimeError) as exc:
        FeishuBot._process_response(response)
    assert "boom" in str(exc.value)


def test_build_record_fields_merges_static_and_extra() -> None:
    def extra_fields(message: Dict, text: str) -> Dict[str, str]:
        return {"Summary": f"{text}-{message['message_id']}"}

    behavior = make_behavior(extra_fields)
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )

    message = {
        "message_id": "msg-123",
        "create_time": "1700000000000",
        "sender": {"sender_id": {"name": "Alice"}},
    }
    fields = bot._build_record_fields(behavior, message, "hello world")
    assert fields == {
        "Message": "hello world",
        "Message ID": "msg-123",
        "Sender": "Alice",
        "Sent At": "2023-11-14T22:13:20+00:00",
        "Category": "General",
        "Summary": "hello world-msg-123",
    }


def test_extract_text_text_message() -> None:
    message = {
        "body": {
            "type": "text",
            "content": json.dumps({"text": "simple"}),
        },
    }
    assert FeishuBot._extract_text(message) == "simple"


def test_extract_text_post_message() -> None:
    message = {
        "body": {
            "type": "post",
            "content": json.dumps(
                {
                    "title": "Daily",
                    "content": [
                        [{"tag": "text", "text": "Line1 "}],
                        [{"tag": "a", "text": "link", "href": "https://example.com"}],
                    ],
                }
            ),
        },
    }
    expected = "# Daily\nLine1 \nlink (https://example.com)"
    assert FeishuBot._extract_text(message) == expected


def test_extract_text_invalid_json_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    message = {
        "body": {
            "type": "text",
            "content": "{not json}",
        },
    }
    assert FeishuBot._extract_text(message) is None
    assert any("not valid JSON" in record.message for record in caplog.records)


def test_format_sender_prefers_sender_id() -> None:
    message = {
        "sender": {
            "sender_id": {"open_id": "ou_123"},
            "display_name": "Alias",
        }
    }
    assert FeishuBot._format_sender(message) == "ou_123"


def test_poll_behavior_creates_records(monkeypatch: pytest.MonkeyPatch) -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        poll_interval=0.1,
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )

    message = {
        "message_id": "omi-1",
        "chat_id": behavior.chat_id,
        "create_time": "1700000001000",
        "body": {"type": "text", "content": json.dumps({"text": "do work"})},
        "sender": {"sender_id": {"name": "Bob"}},
    }

    bot._fetch_messages = MagicMock(return_value=[message])  # type: ignore[method-assign]
    bot._create_bitable_record = MagicMock(return_value="rec-1")  # type: ignore[method-assign]

    last_seen = bot._poll_behavior(behavior, last_seen=0)

    assert last_seen == int(message["create_time"])
    bot._create_bitable_record.assert_called_once()
    fields = bot._create_bitable_record.call_args.args[1]
    assert fields["Message"] == "do work"
    assert fields["Sender"] == "Bob"
    assert fields["Category"] == "General"


def test_poll_behavior_skips_wrong_chat() -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )

    wrong_chat_message = {
        "message_id": "omi-2",
        "chat_id": "oc_other_chat",
        "create_time": "1700000002000",
        "body": {"type": "text", "content": json.dumps({"text": "ignore me"})},
    }

    bot._fetch_messages = MagicMock(return_value=[wrong_chat_message])  # type: ignore[method-assign]
    bot._create_bitable_record = MagicMock()  # type: ignore[method-assign]

    last_seen = bot._poll_behavior(behavior, last_seen=0)

    assert last_seen == int(wrong_chat_message["create_time"])
    bot._create_bitable_record.assert_not_called()


def test_initialize_all_cursors_uses_latest_message() -> None:
    messages = [{"create_time": "10"}, {"create_time": "20"}]
    session = StubSession(messages=messages)
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=session,
        auto_initialize=False,
    )

    bot._initialize_all_cursors()

    assert bot._cursors[behavior.chat_id] == 20
    assert session.token_requests == 1  # token fetched once
    assert session.get_calls[0]["params"]["container_id"] == behavior.chat_id


def test_fetch_messages_passes_start_time() -> None:
    session = StubSession()
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=session,
        auto_initialize=False,
    )
    bot._tenant_access_token = "token123"
    bot._tenant_token_expiry = time.time() + 3600

    bot._fetch_messages(behavior, start_time=12345)

    assert session.get_calls
    assert session.get_calls[0]["params"]["start_time"] == "12345"


def test_ensure_tenant_access_token_caches() -> None:
    session = StubSession()
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=session,
        auto_initialize=False,
    )

    first = bot._ensure_tenant_access_token()
    session.token_requests = 0
    second = bot._ensure_tenant_access_token()

    assert first == "token123"
    assert second == "token123"
    assert session.token_requests == 0


def test_create_bitable_record_reuses_token() -> None:
    session = StubSession()
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=session,
        auto_initialize=False,
    )
    bot._ensure_tenant_access_token()
    session.token_requests = 0

    record_id = bot._create_bitable_record(behavior, {"Message": "hi"})

    assert record_id == "rec-123"
    assert session.token_requests == 0
    assert any("bitable" in call["url"] for call in session.post_calls)


def test_extra_field_helpers() -> None:
    assert _todo_extra_fields({}, " Task ") == {"Status": "Pending", "Todo Detail": "Task"}
    assert _accounting_extra_fields({}, "paid 12.34") == {"Amount": "12.34"}
    assert _accounting_extra_fields({}, "no amount") == {}
    message = {"message_type": "text"}
    assert _automation_extra_fields(message, "") == {"Trigger Type": "text"}
    assert _reminder_extra_fields({}, "Line1\nLine2") == {"Reminder Title": "Line1"}


def test_poll_behavior_skips_when_text_missing() -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )
    message = {
        "message_id": "omi-skip",
        "chat_id": behavior.chat_id,
        "create_time": "1700000003000",
    }
    bot._fetch_messages = MagicMock(return_value=[message])  # type: ignore[method-assign]
    bot._extract_text = MagicMock(return_value=None)  # type: ignore[method-assign]
    bot._create_bitable_record = MagicMock()  # type: ignore[method-assign]

    bot._poll_behavior(behavior, last_seen=0)

    bot._create_bitable_record.assert_not_called()


def test_poll_behavior_logs_failure() -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )
    message = {
        "message_id": "omi-error",
        "chat_id": behavior.chat_id,
        "create_time": "1700000004000",
        "body": {"type": "text", "content": json.dumps({"text": "boom"})},
    }
    bot._fetch_messages = MagicMock(return_value=[message])  # type: ignore[method-assign]
    bot._create_bitable_record = MagicMock(side_effect=RuntimeError("fail"))  # type: ignore[method-assign]

    bot._poll_behavior(behavior, last_seen=0)

    bot._create_bitable_record.assert_called_once()


def test_initialize_cursor_exception_path(monkeypatch: pytest.MonkeyPatch) -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )
    bot._fetch_messages = MagicMock(side_effect=RuntimeError("network"))  # type: ignore[method-assign]

    result = bot._initialize_cursor(behavior)

    assert isinstance(result, int)


def test_initialize_cursor_empty_messages() -> None:
    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )
    bot._fetch_messages = MagicMock(return_value=[])  # type: ignore[method-assign]

    assert isinstance(bot._initialize_cursor(behavior), int)


def test_ensure_tenant_access_token_error_paths() -> None:
    class ErrorSession(StubSession):
        def post(self, url: str, json: Optional[Dict] = None, headers: Optional[Dict] = None, timeout: int = 10):
            if "tenant_access_token" in url:
                return DummyResponse({"code": 400, "msg": "bad"}, status_code=200)
            return super().post(url, json=json, headers=headers, timeout=timeout)

    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=ErrorSession(),
        auto_initialize=False,
    )

    with pytest.raises(RuntimeError):
        bot._ensure_tenant_access_token()

    class MissingTokenSession(StubSession):
        def post(self, url: str, json: Optional[Dict] = None, headers: Optional[Dict] = None, timeout: int = 10):
            if "tenant_access_token" in url:
                return DummyResponse({"code": 0})
            return super().post(url, json=json, headers=headers, timeout=timeout)

    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MissingTokenSession(),
        auto_initialize=False,
    )
    with pytest.raises(RuntimeError):
        bot._ensure_tenant_access_token()


def test_create_bitable_record_missing_record() -> None:
    class MissingRecordSession(StubSession):
        def post(self, url: str, json: Optional[Dict] = None, headers: Optional[Dict] = None, timeout: int = 10):
            if "tenant_access_token" in url:
                return DummyResponse({"code": 0, "tenant_access_token": "token123", "expire": 3600})
            return DummyResponse({"code": 0, "data": {}})

    behavior = make_behavior()
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MissingRecordSession(),
        auto_initialize=False,
    )

    with pytest.raises(RuntimeError):
        bot._create_bitable_record(behavior, {"Message": "hi"})


def test_extra_field_builder_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_extra(message: Dict, text: str) -> Dict[str, str]:
        raise ValueError("bad extra")

    behavior = make_behavior(extra=bad_extra)
    bot = FeishuBot(
        "app",
        "secret",
        behaviors={behavior.chat_id: behavior},
        session=MagicMock(),
        auto_initialize=False,
    )
    message = {
        "message_id": "msg",
        "create_time": "1700000000000",
        "sender": {"sender_id": {"name": "Alice"}},
    }

    fields = bot._build_record_fields(behavior, message, "text")

    assert "Message" in fields


def test_main_invokes_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class DummyBot:
        def __init__(self, app_id, app_secret, poll_interval, behaviors):
            captured["app_id"] = app_id
            captured["app_secret"] = app_secret
            captured["poll_interval"] = poll_interval
            captured["behaviors"] = behaviors

        def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(feishu_bot, "FeishuBot", DummyBot)

    feishu_bot.main(["--app-id", "id", "--app-secret", "secret", "--poll-interval", "0.2", "--log-level", "INFO"])

    assert captured["app_id"] == "id"
    assert captured["poll_interval"] == pytest.approx(0.2)
