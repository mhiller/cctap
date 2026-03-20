# cctap

Routes Claude Code permission prompts to Telegram or Slack when you are away from your machine.

Routine operations auto-approve silently. Anything that needs a human gets sent to you with approve/deny buttons. If `smart_routing` is on and you are active at the machine, Claude Code's native prompt is used instead.

Works on Windows, macOS, and Linux.

---

## How it works

Claude Code fires a hook before any tool use. cctap receives it, decides whether to auto-approve, fall through to the native prompt, or send to your chosen backend, then returns the decision.

```
Claude Code
  -> PreToolUse hook -> cctap (localhost:8765)
    -> auto-approve?       -> allow immediately
    -> smart_routing on?
        -> user active     -> fall through to native Claude Code prompt
        -> user idle       -> send to Telegram/Slack with approve/deny buttons
    -> smart_routing off   -> always send to Telegram/Slack
```

---

## Setup

Python 3.8+ (uses `from __future__ import annotations` for compatibility).

- **Telegram backend**: no pip installs required.
- **Slack backend**: requires `aiohttp` (`pip install aiohttp`).

### Option A: Telegram

**1. Create a Telegram bot**

Message **@BotFather** on Telegram, send `/newbot`, copy the token it gives you.

**2. Clone and run**

```bash
git clone https://github.com/mhiller/cctap
cd cctap
python server.py
```

On first run, choose Telegram, paste your bot token, send a message to the bot so it can find your chat ID, and it handles the rest — writes `config.json`, installs the Claude Code hook into `~/.claude/settings.json`, and starts.

### Option B: Slack

**1. Create a Slack App**

- Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app.
- Enable **Socket Mode** in the sidebar.
- Under **OAuth & Permissions**, add bot token scopes: `chat:write`, `chat:write.public`.
- Under **Interactivity & Shortcuts**, toggle Interactivity on (Socket Mode handles the URL).
- Install the app to your workspace.

**2. Generate tokens**

- **Bot token** (`xoxb-...`): Found under **OAuth & Permissions** after installing.
- **App-level token** (`xapp-...`): Generate under **Basic Information** → **App-Level Tokens** with the `connections:write` scope.

**3. Clone and run**

```bash
git clone https://github.com/mhiller/cctap
cd cctap
pip install aiohttp
python server.py
```

On first run, choose Slack, paste both tokens and your channel ID. The rest is automatic.

### macOS SSL fix

If you installed Python from python.org and hit an SSL error on first run:

```bash
open /Applications/Python\ 3.12/
```

Double-click `Install Certificates.command`, then run again.

---

## Configuration

`config.json` is created on first run next to `server.py`. Any keys missing from an existing config are added automatically on next start.

| Key | Default | Description |
|-----|---------|-------------|
| `notification_backend` | `"telegram"` | `"telegram"` or `"slack"` |
| `telegram_bot_token` | `""` | Telegram bot token (Telegram only) |
| `telegram_chat_id` | `""` | Telegram chat ID (Telegram only) |
| `slack_bot_token` | `""` | Slack bot token, `xoxb-...` (Slack only) |
| `slack_app_token` | `""` | Slack app-level token, `xapp-...` (Slack only) |
| `slack_channel_id` | `""` | Slack channel ID for approvals (Slack only) |
| `server_port` | `8765` | Local port |
| `approval_timeout_seconds` | `60` | Seconds to wait for a response before falling through to the native prompt |
| `smart_routing` | `false` | Route to native prompt when active, notification backend when idle |
| `idle_threshold_seconds` | `120` | Inactivity threshold for smart routing |
| `auto_approve_readonly` | `true` | Auto-approve Read, Glob, Grep, LS |
| `auto_approve_tools` | `["Edit", "MultiEdit", "TodoWrite"]` | Tool names to always auto-approve |
| `auto_approve_mcp_prefixes` | `[]` | MCP tool prefixes to auto-approve, e.g. `"mcp__Claude_Preview__"` |
| `auto_approve_patterns` | see below | Regex patterns matched against Bash commands |

Default Bash auto-approve patterns:

```json
[
  "^cargo (build|test|check|clippy|fmt)",
  "^git (status|log|diff|show)",
  "^ls ", "^dir ", "^find ", "^cat ", "^type ",
  "^grep ", "^echo ", "^pwd$", "^which ", "^where "
]
```

See `config.example.json` for a full reference.

### Smart routing

When `smart_routing` is `true`, cctap checks time since last keyboard or mouse input. Under `idle_threshold_seconds` (default 2 minutes) it falls through to Claude Code's native prompt. Over it, the request goes to your notification backend.

Idle detection is platform-native with no extra dependencies:
- **Windows** - `GetLastInputInfo` via ctypes
- **macOS** - `CGEventSourceSecondsSinceLastEventType` via CoreGraphics ctypes, falls back to `ioreg`
- **Linux** - `xprintidle` (install separately: `apt install xprintidle`)

---

## Approving requests

### Telegram

You get a DM with inline buttons:

```
Claude Code needs approval
my-project/src  4466e39e
Tool: Bash

Command:
rm -rf dist/

Why: Clean build artifacts

ID: a1b2c3d4
```

Tap **Approve** or **Deny**. The message updates and the buttons disappear.

Plain text also works when only one request is pending: `y` / `yes` / `approve` or `n` / `no` / `deny`.

### Slack

You get a message in your chosen channel with Block Kit buttons. Click **Approve** or **Deny**. The message updates to show the result.

With multiple sessions running simultaneously, use the buttons — they are tied to the specific request ID.

---

## Health check

```bash
curl http://127.0.0.1:8765/health
# {"status": "ok", "backend": "telegram", "pending": 0}
```

---

## Moving to a new machine

Copy `config.json` next to `server.py` and run `python server.py`. The hook installs itself into `~/.claude/settings.json` on first start if it is not already there.

---

## Autostart (optional)

```bash
python server.py --install
```

Prints platform-specific instructions: NSSM on Windows, launchd on macOS, systemd on Linux. Nothing is installed automatically.

---

## Disclaimer

This runs a local HTTP server on your machine. Personal tooling — not for use on corporate or enterprise machines.
