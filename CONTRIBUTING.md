# Contributing to XianyuAutoReply

Thanks for helping improve XianyuAutoReply. This project is a local-first automation assistant for Xianyu/Goofish sellers, based on `shaxiu/XianyuAutoAgent` and released under GPL-3.0.

## Safety first

This project can handle real marketplace conversations, so contributions must protect user and buyer data:

- Do not commit `.env`, cookies, browser profiles, logs, SQLite databases, runtime state, screenshots with buyer data, or real product/customer identifiers.
- Keep prompt examples generic. Personal business rules should stay in ignored local files.
- Avoid changes that automatically send or replay messages without an explicit operator decision.
- When adding reply logic, prefer conservative defaults for off-platform payment, private chat, transfer, and other risky marketplace behavior.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Recommended local entrypoint:

```bash
python3 start_all.py
```

The local console defaults to:

```text
http://127.0.0.1:8766
```

## Before opening a pull request

- Run the narrowest relevant test or smoke check for your change.
- For JSON rule changes, validate the file with `python3 -m json.tool`.
- For frontend changes under `web_admin/`, run a syntax check where possible.
- Confirm that generated runtime files are still excluded by `.gitignore`.
- Explain the user impact of the change, especially if it affects reply timing, manual handoff, cookie recovery, or safety filtering.

## Project direction

Good contributions make the tool safer and easier to operate: clearer setup, better local status visibility, safer reply policies, smaller recovery steps when login expires, and documentation that helps maintainers reason about real runtime behavior.
