# Contributing

Thanks for your interest in cctap.

## Getting started

1. Fork and clone the repo
2. Copy `config.example.json` to `config.json` and fill in your Telegram bot details
3. Run `python server.py`

## Guidelines

- No third-party dependencies. The stdlib-only design is intentional.
- Keep it simple. This is a single-file tool on purpose.
- Test on your platform before submitting. Cross-platform behavior matters (Windows, macOS, Linux).

## Reporting bugs

Open an issue with:
- Your OS and Python version
- What you expected vs what happened
- Server log output if relevant

## Pull requests

- Keep PRs focused on a single change
- Describe what and why in the PR description
- Make sure `python server.py --help` still works
