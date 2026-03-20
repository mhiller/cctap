#!/usr/bin/env python3
"""
cctap - Claude Code Tool Approval Proxy

Routes Claude Code PreToolUse hooks to Telegram or Slack for remote approval.
Falls through to the native Claude Code prompt if the user is active
at the machine (smart_routing mode).

First run: `python server.py` walks through setup interactively.
Telegram backend: no third-party dependencies.
Slack backend: requires `aiohttp` (`pip install aiohttp`).
"""

from __future__ import annotations

import abc
import argparse
import asyncio
import ctypes
import json
import logging
import platform
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

__version__ = "0.2.0"

IS_WINDOWS = platform.system() == "Windows"
IS_MAC     = platform.system() == "Darwin"
IS_LINUX   = platform.system() == "Linux"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG: dict = {
    "notification_backend": "telegram",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "slack_bot_token": "",
    "slack_app_token": "",
    "slack_channel_id": "",
    "server_port": 8765,
    "approval_timeout_seconds": 60,
    "smart_routing": False,
    "idle_threshold_seconds": 120,
    "auto_approve_readonly": True,
    "auto_approve_tools": ["Edit", "MultiEdit", "TodoWrite"],
    "auto_approve_mcp_prefixes": [],
    "auto_approve_patterns": [
        "^cargo (build|test|check|clippy|fmt)",
        "^git (status|log|diff|show)",
        "^ls ", "^dir ", "^find ", "^cat ", "^type ",
        "^grep ", "^echo ", "^pwd$", "^which ", "^where ",
    ],
}

_MAX_HEADER_SIZE = 16 * 1024
_MAX_BODY_SIZE   = 512 * 1024
_READ_TIMEOUT    = 30.0


# Config

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("config.json is invalid JSON: %s", exc)
        log.error("Fix the file or delete it to regenerate.")
        sys.exit(1)
    added = {k: v for k, v in DEFAULT_CONFIG.items() if k not in cfg}
    if added:
        cfg.update(added)
        save_config(cfg)
        log.info("config.json updated with new keys: %s", list(added))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# Idle detection

def _idle_seconds_windows() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    elapsed_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return max(elapsed_ms, 0) / 1000.0


def _idle_seconds_mac() -> float:
    try:
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        return float(cg.CGEventSourceSecondsSinceLastEventType(0, 0xFFFFFFFF))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"], stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                return int(line.split("=")[-1].strip()) / 1e9
    except Exception:
        pass
    return 0.0


def _idle_seconds_linux() -> float:
    try:
        out = subprocess.check_output(
            ["xprintidle"], stderr=subprocess.DEVNULL
        ).decode().strip()
        return int(out) / 1000.0
    except Exception:
        return 0.0


def idle_seconds() -> float:
    if IS_WINDOWS:
        return _idle_seconds_windows()
    if IS_MAC:
        return _idle_seconds_mac()
    if IS_LINUX:
        return _idle_seconds_linux()
    return 0.0


# Pending approvals

@dataclass
class PendingApproval:
    request_id: str
    tool_name:  str
    message_id: Optional[Any] = None  # int for Telegram, str for Slack
    decision:   Optional[str] = None
    event:      asyncio.Event = field(default_factory=asyncio.Event)


class ApprovalRegistry:
    def __init__(self) -> None:
        self._lock:    asyncio.Lock               = asyncio.Lock()
        self._pending: dict[str, PendingApproval] = {}

    async def add(self, approval: PendingApproval) -> None:
        async with self._lock:
            self._pending[approval.request_id] = approval

    async def remove(self, request_id: str) -> None:
        async with self._lock:
            self._pending.pop(request_id, None)

    async def resolve(self, request_id: str, decision: str) -> Optional[PendingApproval]:
        async with self._lock:
            approval = self._pending.get(request_id)
            if approval is None or approval.event.is_set():
                return None
            approval.decision = decision
            approval.event.set()
            return approval

    async def ids(self) -> list[str]:
        async with self._lock:
            return list(self._pending.keys())

    async def cancel_all(self) -> list[PendingApproval]:
        async with self._lock:
            approvals = list(self._pending.values())
            self._pending.clear()
        for a in approvals:
            if not a.event.is_set():
                a.decision = "deny"
                a.event.set()
        return approvals

    async def count(self) -> int:
        async with self._lock:
            return len(self._pending)


# Auto-approve

_READONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "TodoRead"})


def should_auto_approve(cfg: dict, tool_name: str, tool_input: dict) -> bool:
    if cfg.get("auto_approve_readonly") and tool_name in _READONLY_TOOLS:
        return True
    if tool_name in cfg.get("auto_approve_tools", []):
        return True
    if any(tool_name.startswith(p) for p in cfg.get("auto_approve_mcp_prefixes", [])):
        return True
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").strip()
        if any(re.match(p, cmd, re.IGNORECASE) for p in cfg.get("auto_approve_patterns", [])):
            return True
    return False


# Message formatting

def _short_path(cwd: str) -> str:
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else cwd


def _format_tool_details_plain(tool_name: str, tool_input: dict) -> str:
    """Plain-text tool details for Slack mrkdwn."""
    lines: list[str] = []
    if tool_name == "Bash":
        cmd  = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        lines.append("*Command:*\n```%s```" % cmd[:800])
        if desc:
            lines.append("*Why:* %s" % desc[:200])
    elif tool_name in ("Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path", tool_input.get("path", "?"))
        lines.append("*File:* `%s`" % path)
        if tool_name == "Write":
            lines.append("*Preview:*\n```%s```" % tool_input.get("content", "")[:400])
        elif tool_name == "Edit":
            lines.append("*Replace:*\n```%s```" % tool_input.get("old_str", "")[:200])
            lines.append("*With:*\n```%s```" % tool_input.get("new_str", "")[:200])
    elif tool_name == "WebFetch":
        lines.append("*URL:* `%s`" % tool_input.get("url", "")[:300])
    elif tool_name == "Task":
        lines.append("*Task:* %s" % str(tool_input)[:400])
    else:
        lines.append("```%s```" % json.dumps(tool_input, indent=2)[:600])
    return "\n".join(lines)


def format_approval_message_html(tool_name: str, tool_input: dict,
                                  request_id: str, session_id: str = "",
                                  cwd: str = "") -> str:
    """HTML-formatted message for Telegram."""
    ctx: list[str] = []
    if cwd:
        ctx.append("<code>%s</code>" % _short_path(cwd))
    if session_id:
        ctx.append("<code>%s</code>" % session_id[:8])

    lines = ["<b>Claude Code needs approval</b>"]
    if ctx:
        lines.append(" ".join(ctx))
    lines.append("<code>Tool: %s</code>" % tool_name)

    if tool_name == "Bash":
        cmd  = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        lines.append("\n<b>Command:</b>\n<pre>%s</pre>" % cmd[:800])
        if desc:
            lines.append("<b>Why:</b> %s" % desc[:200])
    elif tool_name in ("Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path", tool_input.get("path", "?"))
        lines.append("\n<b>File:</b> <code>%s</code>" % path)
        if tool_name == "Write":
            lines.append("<b>Preview:</b>\n<pre>%s</pre>" % tool_input.get("content", "")[:400])
        elif tool_name == "Edit":
            lines.append("<b>Replace:</b>\n<pre>%s</pre>" % tool_input.get("old_str", "")[:200])
            lines.append("<b>With:</b>\n<pre>%s</pre>" % tool_input.get("new_str", "")[:200])
    elif tool_name == "WebFetch":
        lines.append("\n<b>URL:</b> <code>%s</code>" % tool_input.get("url", "")[:300])
    elif tool_name == "Task":
        lines.append("\n<b>Task:</b> %s" % str(tool_input)[:400])
    else:
        lines.append("\n<pre>%s</pre>" % json.dumps(tool_input, indent=2)[:600])

    lines.append("\n<i>ID: %s</i>" % request_id)
    return "\n".join(lines)


def _allow(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason,
    }}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


# ---------------------------------------------------------------------------
# Notification backend interface
# ---------------------------------------------------------------------------

class NotificationBackend(abc.ABC):
    @abc.abstractmethod
    async def start(self, registry: ApprovalRegistry,
                    shutdown_event: asyncio.Event) -> None:
        """Start the polling/listening loop. Called as an asyncio task."""

    @abc.abstractmethod
    async def send_approval(self, tool_name: str, tool_input: dict,
                            request_id: str, session_id: str,
                            cwd: str) -> Any:
        """Send an approval request. Returns a message reference."""

    @abc.abstractmethod
    async def edit_after_decision(self, approval: PendingApproval,
                                  decision: str) -> None:
        """Update the message after a decision is made."""

    @abc.abstractmethod
    async def send_shutdown_notice(self, cancelled: list[PendingApproval]) -> None:
        """Notify that the server is shutting down."""

    @abc.abstractmethod
    async def validate(self) -> bool:
        """Validate credentials on startup. Returns True if OK."""

    @abc.abstractmethod
    def backend_name(self) -> str:
        """Human-readable name for log messages."""


# ---------------------------------------------------------------------------
# Telegram backend
# ---------------------------------------------------------------------------

def _tg_request(token: str, method: str, params: dict) -> dict:
    url  = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urlencode(params).encode()
    req  = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


def _tg_send_message(token: str, chat_id: str, text: str,
                     reply_markup: Optional[dict] = None) -> int:
    params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    result = _tg_request(token, "sendMessage", params)
    if not result.get("ok"):
        raise RuntimeError("sendMessage failed: %s" % result)
    return result["result"]["message_id"]


def _tg_edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
    result = _tg_request(token, "editMessageText", {
        "chat_id":      chat_id,
        "message_id":   message_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": json.dumps({"inline_keyboard": []}),
    })
    if not result.get("ok"):
        raise RuntimeError("editMessageText failed: %s" % result)


def _tg_answer_callback(token: str, callback_id: str) -> None:
    _tg_request(token, "answerCallbackQuery", {"callback_query_id": callback_id})


def _tg_get_updates(token: str, offset: int, timeout: int = 20) -> list:
    params: dict = {
        "timeout":         timeout,
        "allowed_updates": '["message","callback_query"]',
    }
    if offset:
        params["offset"] = offset
    result = _tg_request(token, "getUpdates", params)
    return result.get("result", []) if result.get("ok") else []


def _tg_get_me(token: str) -> dict:
    return _tg_request(token, "getMe", {})


class TelegramBackend(NotificationBackend):
    def __init__(self, cfg: dict) -> None:
        self._token   = cfg["telegram_bot_token"]
        self._chat_id = str(cfg["telegram_chat_id"])

    def backend_name(self) -> str:
        return "Telegram"

    async def validate(self) -> bool:
        loop = asyncio.get_running_loop()
        me = await loop.run_in_executor(None, lambda: _tg_get_me(self._token))
        if not me.get("ok"):
            log.error("Telegram token invalid: %s", me.get("description"))
            return False
        log.info("Bot: @%s", me["result"]["username"])
        return True

    async def send_approval(self, tool_name: str, tool_input: dict,
                            request_id: str, session_id: str,
                            cwd: str) -> int:
        msg_text = format_approval_message_html(
            tool_name, tool_input, request_id, session_id, cwd
        )
        keyboard = {"inline_keyboard": [[
            {"text": "\u2705 Approve", "callback_data": "approve:%s" % request_id},
            {"text": "\u274c Deny",    "callback_data": "deny:%s"    % request_id},
        ]]}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: _tg_send_message(self._token, self._chat_id, msg_text, keyboard)
        )

    async def edit_after_decision(self, approval: PendingApproval,
                                  decision: str) -> None:
        if not approval.message_id:
            return
        loop  = asyncio.get_running_loop()
        icon  = "OK" if decision == "approve" else "X"
        label = "Approved" if decision == "approve" else "Denied"
        stamp = time.strftime("%H:%M:%S")
        text  = "%s <b>%s</b> at %s\n<code>%s</code>  <i>%s</i>" % (
            icon, label, stamp, approval.tool_name, approval.request_id
        )
        mid = approval.message_id
        try:
            await loop.run_in_executor(
                None, lambda: _tg_edit_message(self._token, self._chat_id, mid, text)
            )
            log.info("Message updated [%s]", approval.request_id)
        except Exception as exc:
            log.warning("Could not edit Telegram message: %s", exc)

    async def send_shutdown_notice(self, cancelled: list[PendingApproval]) -> None:
        if not cancelled:
            return
        loop  = asyncio.get_running_loop()
        names = ", ".join(a.tool_name for a in cancelled)
        try:
            await loop.run_in_executor(
                None,
                lambda: _tg_send_message(
                    self._token, self._chat_id,
                    "<b>cctap stopped</b>\n"
                    "Pending cancelled: <code>%s</code>\n"
                    "Claude Code will fall through to native prompts." % names
                ),
            )
        except Exception:
            pass

    async def start(self, registry: ApprovalRegistry,
                    shutdown_event: asyncio.Event) -> None:
        offset  = 0
        backoff = 1.0
        loop    = asyncio.get_running_loop()
        log.info("Telegram polling started")

        while not shutdown_event.is_set():
            try:
                updates = await loop.run_in_executor(
                    None, lambda: _tg_get_updates(self._token, offset, timeout=20)
                )
                backoff = 1.0
            except Exception as exc:
                log.warning("Polling error: %s - retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            for update in updates:
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cq      = update["callback_query"]
                    sender  = str(cq.get("from", {}).get("id", ""))
                    cq_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if sender != self._chat_id and cq_chat != self._chat_id:
                        continue
                    cb_id = cq["id"]
                    await loop.run_in_executor(
                        None, lambda: _tg_answer_callback(self._token, cb_id)
                    )
                    data = cq.get("data", "")
                    log.info("Callback: %s", data)
                    if ":" in data:
                        action, req_id = data.split(":", 1)
                        decision = "approve" if action == "approve" else "deny"
                        approval = await registry.resolve(req_id, decision)
                        if approval:
                            asyncio.create_task(self.edit_after_decision(approval, decision))
                        else:
                            log.warning("No pending approval for [%s]", req_id)

                elif "message" in update:
                    msg = update["message"]
                    if str(msg.get("chat", {}).get("id")) != self._chat_id:
                        continue
                    text = msg.get("text", "").strip().lower()
                    ids  = await registry.ids()

                    for keywords, decision in [
                        ({"y", "yes", "approve", "ok", "a"}, "approve"),
                        ({"n", "no", "deny", "block", "d"},  "deny"),
                    ]:
                        if text in keywords and len(ids) == 1:
                            approval = await registry.resolve(ids[0], decision)
                            if approval:
                                asyncio.create_task(
                                    self.edit_after_decision(approval, decision)
                                )
                            break
                    else:
                        for prefix, decision in [
                            ("approve ", "approve"),
                            ("deny ",    "deny"),
                            ("block ",   "deny"),
                        ]:
                            if text.startswith(prefix):
                                req_id   = text[len(prefix):].strip()
                                approval = await registry.resolve(req_id, decision)
                                if approval:
                                    asyncio.create_task(
                                        self.edit_after_decision(approval, decision)
                                    )
                                break


# ---------------------------------------------------------------------------
# Slack backend (Socket Mode via aiohttp)
# ---------------------------------------------------------------------------

def _check_aiohttp() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def _slack_api_sync(token: str, method: str, payload: dict) -> dict:
    """Synchronous Slack Web API call using urllib (for setup validation)."""
    url  = "https://slack.com/api/%s" % method
    data = json.dumps(payload).encode()
    req  = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        return json.loads(exc.read())


class SlackBackend(NotificationBackend):
    def __init__(self, cfg: dict) -> None:
        self._bot_token = cfg["slack_bot_token"]
        self._app_token = cfg["slack_app_token"]
        self._channel   = cfg["slack_channel_id"]

    def backend_name(self) -> str:
        return "Slack"

    async def validate(self) -> bool:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: _slack_api_sync(self._bot_token, "auth.test", {})
        )
        if not result.get("ok"):
            log.error("Slack bot token invalid: %s", result.get("error"))
            return False
        log.info("Slack bot: %s (team: %s)", result.get("user"), result.get("team"))
        return True

    async def _slack_api(self, method: str, payload: dict,
                         token: Optional[str] = None) -> dict:
        """Async Slack Web API call using aiohttp."""
        import aiohttp
        tok = token or self._bot_token
        url = "https://slack.com/api/%s" % method
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer %s" % tok,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return await resp.json()

    async def send_approval(self, tool_name: str, tool_input: dict,
                            request_id: str, session_id: str,
                            cwd: str) -> str:
        ctx_parts: list[str] = []
        if cwd:
            ctx_parts.append("`%s`" % _short_path(cwd))
        if session_id:
            ctx_parts.append("`%s`" % session_id[:8])

        header = "*Claude Code needs approval*"
        if ctx_parts:
            header += "  %s" % " ".join(ctx_parts)

        detail = _format_tool_details_plain(tool_name, tool_input)

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "`Tool: %s`\n%s" % (tool_name, detail)},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "ID: `%s`" % request_id}],
            },
            {
                "type": "actions",
                "block_id": "approval_%s" % request_id,
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "\u2705 Approve"},
                        "action_id": "approve_%s" % request_id,
                        "style": "primary",
                        "value": "approve:%s" % request_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "\u274c Deny"},
                        "action_id": "deny_%s" % request_id,
                        "style": "danger",
                        "value": "deny:%s" % request_id,
                    },
                ],
            },
        ]

        fallback = "Claude Code needs approval: %s (ID: %s)" % (tool_name, request_id)
        result = await self._slack_api("chat.postMessage", {
            "channel": self._channel,
            "text":    fallback,
            "blocks":  blocks,
        })
        if not result.get("ok"):
            raise RuntimeError("Slack chat.postMessage failed: %s" % result.get("error"))
        return result["ts"]

    async def edit_after_decision(self, approval: PendingApproval,
                                  decision: str) -> None:
        if not approval.message_id:
            return
        icon  = "\u2705" if decision == "approve" else "\u274c"
        label = "Approved" if decision == "approve" else "Denied"
        stamp = time.strftime("%H:%M:%S")
        text  = "%s *%s* at %s\n`%s`  _%s_" % (
            icon, label, stamp, approval.tool_name, approval.request_id
        )
        try:
            await self._slack_api("chat.update", {
                "channel": self._channel,
                "ts":      approval.message_id,
                "text":    text,
                "blocks":  [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            })
            log.info("Slack message updated [%s]", approval.request_id)
        except Exception as exc:
            log.warning("Could not edit Slack message: %s", exc)

    async def send_shutdown_notice(self, cancelled: list[PendingApproval]) -> None:
        if not cancelled:
            return
        names = ", ".join(a.tool_name for a in cancelled)
        try:
            await self._slack_api("chat.postMessage", {
                "channel": self._channel,
                "text":    "*cctap stopped*\nPending cancelled: `%s`\n"
                           "Claude Code will fall through to native prompts." % names,
            })
        except Exception:
            pass

    async def start(self, registry: ApprovalRegistry,
                    shutdown_event: asyncio.Event) -> None:
        """Connect to Slack via Socket Mode and listen for interactive events."""
        import aiohttp

        backoff = 1.0
        log.info("Slack Socket Mode starting")

        while not shutdown_event.is_set():
            wss_url = None
            try:
                result = await self._slack_api(
                    "apps.connections.open", {}, token=self._app_token
                )
                if not result.get("ok"):
                    log.error("apps.connections.open failed: %s", result.get("error"))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                wss_url = result["url"]
                backoff = 1.0
            except Exception as exc:
                log.warning("Socket Mode connect error: %s - retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(wss_url) as ws:
                        log.info("Slack Socket Mode connected")
                        async for ws_msg in ws:
                            if shutdown_event.is_set():
                                break
                            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_socket_message(
                                    ws, ws_msg.data, registry
                                )
                            elif ws_msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning("WebSocket closed/error, reconnecting")
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Socket Mode error: %s - reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_socket_message(self, ws: Any, raw: str,
                                     registry: ApprovalRegistry) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Acknowledge the envelope immediately
        envelope_id = data.get("envelope_id")
        if envelope_id:
            await ws.send_json({"envelope_id": envelope_id})

        msg_type = data.get("type")

        if msg_type == "disconnect":
            log.info("Slack requested disconnect (reason: %s)", data.get("reason"))
            return

        if msg_type == "hello":
            log.info("Slack Socket Mode hello received")
            return

        # Interactive messages (button clicks)
        payload = data.get("payload", {})
        if payload.get("type") == "block_actions":
            actions = payload.get("actions", [])
            for action in actions:
                action_id = action.get("action_id", "")
                value     = action.get("value", "")

                if ":" not in value:
                    continue
                action_type, req_id = value.split(":", 1)
                decision = "approve" if action_type == "approve" else "deny"
                approval = await registry.resolve(req_id, decision)
                if approval:
                    asyncio.create_task(self.edit_after_decision(approval, decision))
                    log.info("Slack %s [%s] via button", decision, req_id)
                else:
                    log.warning("No pending approval for [%s]", req_id)

        # Thread message replies (text-based approval like Telegram)
        elif payload.get("type") == "event_callback":
            event = payload.get("event", {})
            if event.get("type") == "message" and event.get("channel") == self._channel:
                text = event.get("text", "").strip().lower()
                ids  = await registry.ids()
                for keywords, decision in [
                    ({"y", "yes", "approve", "ok", "a"}, "approve"),
                    ({"n", "no", "deny", "block", "d"},  "deny"),
                ]:
                    if text in keywords and len(ids) == 1:
                        approval = await registry.resolve(ids[0], decision)
                        if approval:
                            asyncio.create_task(self.edit_after_decision(approval, decision))
                        break


# ---------------------------------------------------------------------------
# HTTP hook server
# ---------------------------------------------------------------------------

async def handle_hook(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: dict,
    registry: ApprovalRegistry,
    backend: NotificationBackend,
) -> None:
    timeout = _READ_TIMEOUT + float(cfg.get("approval_timeout_seconds", 60)) + 5.0
    try:
        await asyncio.wait_for(
            _handle_hook_inner(reader, writer, cfg, registry, backend),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("Hook handler timed out")
        try:
            _write_response(writer, 200, {})
            await writer.drain()
        except Exception:
            pass
    except Exception as exc:
        log.exception("Unhandled error in hook handler: %s", exc)
        try:
            _write_response(writer, 500, {"error": "internal server error"})
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_hook_inner(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: dict,
    registry: ApprovalRegistry,
    backend: NotificationBackend,
) -> None:
    try:
        header_bytes = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=_READ_TIMEOUT,
        )
    except asyncio.LimitOverrunError:
        log.warning("Request headers too large")
        _write_response(writer, 400, {"error": "headers too large"})
        await writer.drain()
        return
    except asyncio.TimeoutError:
        log.warning("Timed out reading request headers")
        return

    header_text = header_bytes.decode(errors="replace")
    first_line  = header_text.splitlines()[0] if header_text else ""

    if first_line.startswith("GET /health"):
        pending_count = await registry.count()
        _write_response(writer, 200, {
            "status": "ok",
            "backend": backend.backend_name(),
            "pending": pending_count,
        })
        await writer.drain()
        return

    content_length = 0
    for line in header_text.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                content_length = min(int(line.split(":", 1)[1].strip()), _MAX_BODY_SIZE)
            except ValueError:
                pass
            break

    body_bytes = b"{}"
    if content_length:
        try:
            body_bytes = await asyncio.wait_for(
                reader.read(content_length),
                timeout=_READ_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Timed out reading request body")
            return

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        _write_response(writer, 400, {"error": "bad JSON"})
        await writer.drain()
        return

    if not isinstance(payload, dict):
        _write_response(writer, 400, {"error": "expected JSON object"})
        await writer.drain()
        return

    tool_name  = str(payload.get("tool_name", payload.get("tool", "Unknown")))[:256]
    tool_input = payload.get("tool_input", {}) if isinstance(payload.get("tool_input"), dict) else {}
    session_id = str(payload.get("session_id", ""))[:64]
    cwd        = str(payload.get("cwd", ""))[:512]
    request_id = uuid.uuid4().hex[:8]

    log.info("Hook: %-35s session=%-8s id=%s", tool_name, session_id[:8], request_id)

    if should_auto_approve(cfg, tool_name, tool_input):
        log.info("  auto-approve [%s]", request_id)
        _write_response(writer, 200, _allow("Auto-approved"))
        await writer.drain()
        return

    if cfg.get("smart_routing"):
        loop      = asyncio.get_running_loop()
        secs      = await loop.run_in_executor(None, idle_seconds)
        threshold = cfg.get("idle_threshold_seconds", 120)
        if secs < threshold:
            log.info("  native prompt (idle %.0fs) [%s]", secs, request_id)
            _write_response(writer, 200, {})
            await writer.drain()
            return
        log.info("  %s (idle %.0fs) [%s]", backend.backend_name().lower(), secs, request_id)

    try:
        msg_id = await backend.send_approval(
            tool_name, tool_input, request_id, session_id, cwd
        )
    except Exception as exc:
        log.error("Failed to send %s message: %s - falling through",
                  backend.backend_name(), exc)
        _write_response(writer, 200, {})
        await writer.drain()
        return

    approval            = PendingApproval(request_id, tool_name)
    approval.message_id = msg_id
    await registry.add(approval)

    timeout = float(cfg.get("approval_timeout_seconds", 60))
    try:
        await asyncio.wait_for(approval.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("  timeout [%s] - falling through", request_id)
        await registry.remove(request_id)
        _write_response(writer, 200, {})
        await writer.drain()
        return

    await registry.remove(request_id)

    bname = backend.backend_name()
    if approval.decision == "approve":
        log.info("  approved [%s]", request_id)
        _write_response(writer, 200, _allow("Approved via %s" % bname))
    else:
        log.info("  denied [%s]", request_id)
        _write_response(writer, 200, _deny("Denied via %s" % bname))

    await writer.drain()


def _write_response(writer: asyncio.StreamWriter, status: int, body: dict) -> None:
    data   = json.dumps(body).encode()
    reason = {200: "OK", 400: "Bad Request", 500: "Internal Server Error"}.get(status, "")
    writer.write(
        ("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (
            status, reason, len(data)
        )).encode() + data
    )


# Graceful shutdown

async def shutdown(
    registry: ApprovalRegistry,
    backend: NotificationBackend,
    server: asyncio.Server,
    shutdown_event: asyncio.Event,
) -> None:
    log.info("Shutting down...")
    shutdown_event.set()
    server.close()
    await server.wait_closed()

    cancelled = await registry.cancel_all()
    if cancelled:
        log.info("Cancelled %d pending approval(s)", len(cancelled))
        await backend.send_shutdown_notice(cancelled)

    log.info("Done")


# Hook installation

def _install_hook(settings_path: Path, port: int = 8765) -> None:
    hook_url   = "http://127.0.0.1:%d/approve" % port
    hook_block = {"matcher": "*", "hooks": [{"type": "http", "url": hook_url}]}

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    pre     = existing.setdefault("hooks", {}).setdefault("PreToolUse", [])
    already = any(
        h.get("type") == "http" and h.get("url") == hook_url
        for entry in pre
        for h in entry.get("hooks", [])
    )
    if not already:
        pre.append(hook_block)
        settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        log.info("Hook installed to %s", settings_path)
    else:
        log.info("Hook already present in %s", settings_path)


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------

def _setup_telegram() -> dict:
    """Interactive Telegram setup. Returns partial config."""
    print("\n--- Telegram Setup ---\n")

    print("Step 1: Paste your Telegram bot token.")
    print("  (Create one via @BotFather if you haven't already)\n")
    while True:
        token = input("  Bot token: ").strip()
        if not token:
            continue
        print("  Verifying...", end=" ", flush=True)
        me = _tg_get_me(token)
        if me.get("ok"):
            print("OK - @%s" % me["result"]["username"])
            break
        print("FAILED - %s" % me.get("description", "invalid token"))

    print("\nStep 2: Send any message to your bot in Telegram now.")
    input("  Press Enter once you have sent a message...")
    print("  Looking for your chat ID...", end=" ", flush=True)

    chat_id: Optional[str] = None
    for attempt in range(3):
        for update in _tg_get_updates(token, 0, timeout=5):
            if "message" in update:
                chat    = update["message"]["chat"]
                chat_id = str(chat["id"])
                name    = ("%s %s" % (chat.get("first_name", ""), chat.get("last_name", ""))).strip()
                print("OK - %s (%s)" % (chat_id, name))
                break
        if chat_id:
            break
        if attempt < 2:
            time.sleep(3)
            print("\n  Trying again...", end=" ", flush=True)

    if not chat_id:
        print("FAILED - no messages found. Send a message to your bot and run again.")
        sys.exit(1)

    return {
        "notification_backend": "telegram",
        "telegram_bot_token": token,
        "telegram_chat_id": chat_id,
    }


def _setup_slack() -> dict:
    """Interactive Slack setup. Returns partial config."""
    print("\n--- Slack Setup ---\n")

    # Check aiohttp
    if not _check_aiohttp():
        print("  Slack backend requires aiohttp. Install it now?")
        if input("  (y/n): ").strip().lower() in ("y", "yes"):
            print("  Installing aiohttp...", flush=True)
            subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp"])
            print("  Done.")
        else:
            print("  Cannot continue without aiohttp. Exiting.")
            sys.exit(1)

    print("Step 1: Create a Slack App with Socket Mode enabled.")
    print("  Visit https://api.slack.com/apps and create a new app.")
    print("  Enable Socket Mode under 'Socket Mode' in the sidebar.")
    print("  Under 'OAuth & Permissions', add these bot token scopes:")
    print("    - chat:write")
    print("    - chat:write.public  (if posting to channels the bot hasn't joined)")
    print("  Install the app to your workspace.\n")

    print("Step 2: Paste your Bot User OAuth Token (starts with xoxb-).")
    while True:
        bot_token = input("  Bot token: ").strip()
        if not bot_token:
            continue
        if not bot_token.startswith("xoxb-"):
            print("  Token should start with xoxb-. Try again.")
            continue
        print("  Verifying...", end=" ", flush=True)
        result = _slack_api_sync(bot_token, "auth.test", {})
        if result.get("ok"):
            print("OK - %s (team: %s)" % (result.get("user"), result.get("team")))
            break
        print("FAILED - %s" % result.get("error", "unknown error"))

    print("\nStep 3: Paste your App-Level Token (starts with xapp-).")
    print("  Generate one under 'Basic Information' -> 'App-Level Tokens'")
    print("  with the 'connections:write' scope.\n")
    while True:
        app_token = input("  App token: ").strip()
        if not app_token:
            continue
        if not app_token.startswith("xapp-"):
            print("  Token should start with xapp-. Try again.")
            continue
        print("  Verifying...", end=" ", flush=True)
        result = _slack_api_sync(app_token, "apps.connections.open", {})
        if result.get("ok"):
            print("OK")
            break
        print("FAILED - %s" % result.get("error", "unknown error"))

    print("\nStep 4: Enter the Slack channel ID where approvals should be posted.")
    print("  (Right-click a channel -> 'View channel details' -> copy the ID at the bottom)\n")
    while True:
        channel_id = input("  Channel ID: ").strip()
        if channel_id:
            break

    return {
        "notification_backend": "slack",
        "slack_bot_token": bot_token,
        "slack_app_token": app_token,
        "slack_channel_id": channel_id,
    }


def first_run_setup() -> dict:
    print("\ncctap - Setup\n")

    print("Which notification backend?")
    print("  1) Telegram (no dependencies)")
    print("  2) Slack    (requires aiohttp)\n")
    while True:
        choice = input("  Choice (1/2): ").strip()
        if choice in ("1", "telegram"):
            backend_cfg = _setup_telegram()
            break
        elif choice in ("2", "slack"):
            backend_cfg = _setup_slack()
            break
        print("  Enter 1 or 2.")

    settings_path = Path.home() / ".claude" / "settings.json"
    print("\nInstalling Claude Code hook to %s..." % settings_path)
    _install_hook(settings_path)

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(backend_cfg)
    save_config(cfg)
    print("  Config saved to %s" % CONFIG_PATH)

    print("\nSet up autostart so this runs on login? (optional)")
    if input("  (y/n): ").strip().lower() in ("y", "yes"):
        print_autostart_instructions()

    print("\nSetup complete. Starting server...\n")
    return cfg


# Autostart instructions

def print_autostart_instructions() -> None:
    script_path = Path(__file__).resolve()
    python_exe  = sys.executable

    if IS_WINDOWS:
        print("\nWindows: silent background service via NSSM")
        print("  1. Download nssm from https://nssm.cc/download and add to PATH")
        print("  2. Run in an admin PowerShell:")
        print('       nssm install cctap "%s" "%s"' % (python_exe, script_path))
        print('       nssm set cctap AppStdout "%%TEMP%%\\cctap.log"')
        print('       nssm set cctap AppStderr "%%TEMP%%\\cctap.log"')
        print('       nssm start cctap')
    elif IS_MAC:
        plist_path = Path.home() / "Library/LaunchAgents/com.cctap.plist"
        plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '  <key>Label</key><string>com.cctap</string>\n'
            '  <key>ProgramArguments</key>\n  <array>\n'
            '    <string>%s</string>\n'
            '    <string>%s</string>\n'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '  <key>StandardOutPath</key><string>/tmp/cctap.log</string>\n'
            '  <key>StandardErrorPath</key><string>/tmp/cctap.log</string>\n'
            '</dict>\n</plist>\n' % (python_exe, script_path),
            encoding="utf-8",
        )
        print("\nmacOS: launchd plist written to %s" % plist_path)
        print('  launchctl load "%s"' % plist_path)
        print("  tail -f /tmp/cctap.log")
    else:
        service_path = Path.home() / ".config/systemd/user/cctap.service"
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(
            "[Unit]\nDescription=cctap\nAfter=network.target\n\n"
            "[Service]\nExecStart=%s %s\nRestart=always\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n" % (python_exe, script_path),
            encoding="utf-8",
        )
        print("\nLinux: systemd service written to %s" % service_path)
        print("  systemctl --user daemon-reload")
        print("  systemctl --user enable --now cctap")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _create_backend(cfg: dict) -> NotificationBackend:
    backend_name = cfg.get("notification_backend", "telegram")
    if backend_name == "slack":
        if not _check_aiohttp():
            log.error("Slack backend requires aiohttp: pip install aiohttp")
            sys.exit(1)
        return SlackBackend(cfg)
    return TelegramBackend(cfg)


def _needs_setup(cfg: dict) -> bool:
    backend = cfg.get("notification_backend", "telegram")
    if backend == "telegram":
        return not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id")
    elif backend == "slack":
        return (not cfg.get("slack_bot_token") or not cfg.get("slack_app_token")
                or not cfg.get("slack_channel_id"))
    # No backend configured at all — needs setup
    return True


async def run(cfg: dict) -> None:
    backend        = _create_backend(cfg)
    registry       = ApprovalRegistry()
    loop           = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    if not await backend.validate():
        log.error("Delete config.json and run again to reconfigure.")
        sys.exit(1)

    settings_path = Path.home() / ".claude" / "settings.json"
    port          = cfg.get("server_port", 8765)
    hook_url      = "http://127.0.0.1:%d/approve" % port
    hook_present  = False
    if settings_path.exists():
        try:
            existing     = json.loads(settings_path.read_text(encoding="utf-8"))
            hook_present = any(
                h.get("type") == "http" and h.get("url") == hook_url
                for entry in existing.get("hooks", {}).get("PreToolUse", [])
                for h in entry.get("hooks", [])
            )
        except (json.JSONDecodeError, KeyError):
            pass
    if not hook_present:
        await loop.run_in_executor(None, lambda: _install_hook(settings_path, port))

    asyncio.create_task(backend.start(registry, shutdown_event))

    server = await asyncio.start_server(
        lambda r, w: handle_hook(r, w, cfg, registry, backend),
        host="127.0.0.1",
        port=port,
        limit=_MAX_HEADER_SIZE,
    )

    def _on_signal() -> None:
        asyncio.create_task(shutdown(registry, backend, server, shutdown_event))

    if not IS_WINDOWS:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)

    log.info("Backend: %s", backend.backend_name())
    log.info("Listening on http://127.0.0.1:%d", port)
    log.info("Health:   http://127.0.0.1:%d/health", port)
    log.info("Platform: %s %s", platform.system(), platform.release())
    log.info("Ready")

    async with server:
        await server.serve_forever()


def main() -> None:
    global CONFIG_PATH

    parser = argparse.ArgumentParser(description="cctap - Claude Code tool approval proxy")
    parser.add_argument("--install", action="store_true", help="Print autostart instructions")
    parser.add_argument("--config",  default=str(CONFIG_PATH), help="Path to config.json")
    args = parser.parse_args()

    CONFIG_PATH = Path(args.config)

    if args.install:
        print_autostart_instructions()
        return

    cfg = load_config()

    if _needs_setup(cfg):
        cfg = first_run_setup()

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
