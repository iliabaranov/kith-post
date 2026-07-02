# Contributing to Kith Post

Thanks for your interest! Kith Post is a small, self-hosted app with a deliberately
narrow scope: free, private invitations and holiday cards sent from your own Gmail.
Contributions that keep it simple, private, and easy to self-host are very welcome.

## Getting set up

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create .venv and install deps (incl. dev tools)
make dev                # run at http://localhost:8000 (dry-run: no real email)
```

In dry-run mode, "sent" mail is written to `data/outbox/` as `.eml` files instead
of going out over Gmail — so you can develop the full flow without credentials.

## Before you open a PR

Run the same checks CI runs:

```bash
make lint               # ruff
make test               # pytest
make typecheck          # mypy (optional but appreciated)
```

- **Add tests** for behavior changes. The suite is hermetic (no network, throwaway
  DB, fixed Fernet key) — mirror the patterns in `tests/`.
- **Keep the diff focused.** Match the surrounding style; ruff enforces formatting.
- **Don't commit secrets.** No real `.env`, keys, tokens, or personal emails.
  `.gitignore` already covers `.env` and `data/`.

## Scope and philosophy

- **Privacy first.** No trackers, no third-party analytics, no data sent anywhere
  except the host's own Gmail. PII and tokens stay Fernet-encrypted at rest.
- **Self-hostable by one person.** No new required infrastructure (the reminder
  sweep is an in-process task, storage is SQLite). Prefer a boring dependency-free
  solution over a clever one.
- **Small surface.** New features should earn their complexity. If unsure whether
  something fits, open an issue to discuss before building it.

## Reporting bugs and ideas

- Bugs / features: open a GitHub issue with steps to reproduce or a clear use case.
- Security issues: **do not** open a public issue — see [SECURITY.md](SECURITY.md).

By contributing you agree your work is licensed under the project's
[MIT License](LICENSE).
