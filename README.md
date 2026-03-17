# cctap

Routes Claude Code permission prompts to Telegram when you are away from your machine.

Routine operations auto-approve silently. Anything that needs a human gets DMed to you with ✅/❌ buttons. If `smart_routing` is on and you are active at the machine, Claude Code's native prompt is used instead.

Works on Windows, macOS, and Linux. No pip installs required.

---

## How it works

Claude Code fires a hook before any tool use. cctap receives it, decides whether to auto-approve, fall through to the native prompt, or send to Telegram, then returns the decision.

```
Claude Code
  -> PreToolUse hook -> cctap (localhost:8765)
    -> auto-approve?       -> allow immediately
    -> smart_routing on?
        -> user active     -> fall through to native Claude Code prompt
        -> user idle       -> DM to Telegram with approve/deny buttons
    -> smart_routing off   -> always send to Telegram
```

---

## Setup

Python 3.8+ (uses `from __future__ import annotations` for compatibility). No pip installs.

**1. Create a Telegram bot**

Message **@BotFather** on Telegram, send `/newbot`, copy the token it gives you.

**2. Clone and run**

```bash
git clone https://github.com/mhiller/cctap
cd cctap
python server.py
```

On first run it will ask for your bot token, find your chat ID, write `config.json`, install the Claude Code hook into `~/.claude/settings.json`, and start. That is the entire setup.

**3. macOS SSL fix**

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
| `server_port` | `8765` | Local port |
| `approval_timeout_seconds` | `60` | Seconds to wait for a Telegram response before falling through to the native prompt |
| `smart_routing` | `false` | Route to native prompt when active, Telegram when idle |
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

When `smart_routing` is `true`, cctap checks time since last keyboard or mouse input. Under `idle_threshold_seconds` (default 2 minutes) it falls through to Claude Code's native prompt. Over it, the request goes to Telegram.

Idle detection is platform-native with no extra dependencies:
- **Windows** - `GetLastInputInfo` via ctypes
- **macOS** - `CGEventSourceSecondsSinceLastEventType` via CoreGraphics ctypes, falls back to `ioreg`
- **Linux** - `xprintidle` (install separately: `apt install xprintidle`)

---

## Approving from Telegram

When a request needs approval you get a DM:

```
Claude Code needs approval
my-project/src  4466e39e
Tool: Bash

Command:
rm -rf dist/

Why: Clean build artifacts

ID: a1b2c3d4
```

Tap **✅ Approve** or **❌ Deny**. The message updates and the buttons disappear.

Plain text also works when only one request is pending: `y` / `yes` / `approve` or `n` / `no` / `deny`.

With multiple sessions running simultaneously, use the buttons — they are tied to the specific request ID.

---

## Health check

```bash
curl http://127.0.0.1:8765/health
# {"status": "ok", "pending": 0}
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
