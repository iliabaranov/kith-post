# Kith

A free, self-hosted, privacy-first digital invitation service. Upload a card,
pick your people, and send a personal invite **from your own Gmail** — with
open / accept / decline tracking. A non-commercial alternative to Punchbowl and
Paperless Post.

> **Status:** design phase. See [`DESIGN.md`](./DESIGN.md) for the full
> architecture, decisions, and roadmap.

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
- **Self-hosted & free** — one container on your own hardware, exposed via
  Tailscale Funnel with automatic HTTPS. No paid dependencies, no monetization.
- **Honest tracking, no pixel** — signals are Sent, Opened (= the recipient
  visited the invitation page), Accepted, and Declined. No hidden tracking pixel
  or beacons; "Opened" is an explicit page visit, never an inferred open.

## Stack

Python 3.12 · FastAPI · SQLite · Jinja2 + HTMX + Tailwind (dark) · Pillow ·
Google API client · Docker · Tailscale Funnel. Tested with pytest.

## License

MIT — provided **as is**, without warranty of any kind. See [`LICENSE`](./LICENSE).
