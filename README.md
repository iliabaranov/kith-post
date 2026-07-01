# Kith Post

A free, self-hosted, privacy-first digital invitation service. Upload a card,
pick your people, and send a personal invite **from your own Gmail** — with
open / accept / decline tracking. A non-commercial alternative to Punchbowl and
Paperless Post.

> **Status:** G1 — Google sign-in (with a dev-login fallback), encrypted user
> storage, account export/delete. See [`DESIGN.md`](./DESIGN.md) for the
> architecture/roadmap and [`DESIGN-LANGUAGE.md`](./DESIGN-LANGUAGE.md) for the
> visual system.

## Run it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
make install      # create .venv + install deps (uv)
make dev          # http://localhost:8000  (hot reload)
make test         # pytest
make lint         # ruff
# or the container:
make docker       # docker compose up --build
```

The `Makefile` clears `PYTHONPATH` so a sourced ROS (or other system Python)
can't leak packages into the venv. Copy `config.example.toml` → `config.toml`
and `.env.example` → `.env` to override defaults; `KITH_SEND_MODE` defaults to
`dry-run` (no mail is sent — composed messages are written to `data/outbox/`).

**Signing in:** until you configure Google, `/auth/login` offers a local
**dev sign-in** so you can test the signed-in app. To enable real
"Sign in with Google", follow [`docs/google-oauth-setup.md`](./docs/google-oauth-setup.md)
and set `KITH_GOOGLE_CLIENT_ID`, `KITH_GOOGLE_CLIENT_SECRET`, and `KITH_FERNET_KEY`.

## What it does

- Sign in with Google (Gmail SSO).
- Upload an image to use as a holiday / birthday / event card.
- Build a list of family and friends.
- Send a personal email **from your own Gmail account** (via the Gmail API), so
  it looks like it came directly from you.
- Track who was sent an invite, who opened it (visited the invitation page), and
  who accepted or declined — on a simple dashboard.
- Automatically follow up with non-responders on a sane, configurable schedule
  (halfway to the date, 1 week out, 3 days out) — reminders stop the moment
  someone engages.

## Principles

- **Privacy first** — minimal data, PII encrypted at rest, one-click export &
  delete, heavy assets auto-purged. We use the `gmail.send` scope only and can
  never read your mailbox.
- **Self-hosted & free** — one container on your own hardware, exposed via a
  Cloudflare Tunnel with automatic HTTPS (no open router ports, works behind
  CGNAT). No paid dependencies, no monetization. See [`docs/deploy.md`](./docs/deploy.md).
- **Honest tracking, no pixel** — signals are Sent, Opened (= the recipient
  visited the invitation page), Accepted, and Declined. No hidden tracking pixel
  or beacons; "Opened" is an explicit page visit, never an inferred open.

## Stack

Python 3.12 · FastAPI · SQLite · Jinja2 + HTMX + hand-written token CSS (light/warm) · Pillow ·
Google API client · Docker · Cloudflare Tunnel. Tested with pytest.

## License

MIT — provided **as is**, without warranty of any kind. See [`LICENSE`](./LICENSE).
