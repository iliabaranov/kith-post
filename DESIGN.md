# Kith — Design Document

> **Status:** Draft v0.1 · **Date:** 2026-06-29 · **Owner:** ilia
>
> *Kith* (n.) — one's friends, acquaintances, and neighbors. As in "kith and kin."
> A free, self-hosted, privacy-first digital invitation service: upload a card,
> pick your people, and send a personal invite **from your own Gmail** with
> open / accept / decline tracking. A non-commercial alternative to Punchbowl
> and Paperless Post.
>
> *(Name is a placeholder — trivially renameable. Everything below is the
> architecture; the name is not load-bearing.)*

---

## 1. Summary & Vision

A single small container, hosted on a home server and exposed to the internet
via **Tailscale Funnel** (which also gives us free, auto-renewing HTTPS). A user
signs in with their Google account (Gmail SSO), uploads an image to use as a
holiday/birthday/event card, builds a list of family and friends, and clicks
send. Each recipient receives an email that **genuinely originates from the
user's own Gmail** (sent via the Gmail API on their behalf), so it reads as
personal — not as a blast from a third-party marketing platform.

Every email carries opaque per-recipient tracking so the sender can see, on a
dashboard, who was sent an invite, who opened it (best-effort), and who accepted
or declined (reliable). The service stores as little as it can get away with,
encrypts the personal data it does keep, and gives every user a one-click
export-and-delete. It is free, offered as-is with no warranty.

---

## 2. Goals & Non-Goals

### Goals
- **Personal-feeling delivery.** Mail leaves the *user's* Gmail account, lands in
  their Sent folder, and inherits Google's SPF/DKIM/DMARC reputation.
- **Dead-simple authoring.** Upload an image, add recipients (paste, type, or
  pick from saved contacts), write a short message, send.
- **Trustworthy tracking.** Reliable accept/decline + sent/delivered status;
  best-effort open tracking with honest caveats.
- **Privacy by default.** Minimal data, encrypted PII at rest, user-owned
  export/delete, heavy assets auto-purged.
- **Cheap & self-hostable.** One container, one volume, no paid dependencies.
  Free TLS via Tailscale Funnel.
- **Clean architecture.** Pure-logic core, thin framework glue, testable with
  pytest.

### Non-Goals (for v1)
- Public open signup at internet scale (see §5, OAuth verification cost).
- Payments, premium tiers, ads, or any monetization.
- Rich drag-and-drop card *designer* (we accept a finished image; we are not
  Canva). A future "templates" feature is out of scope for MVP.
- SMS / WhatsApp / push delivery. Email only.
- Mobile native apps. Responsive web only.
- Multi-language / i18n in v1 (English only).

---

## 3. Locked Decisions

These were decided up front and constrain everything downstream.

| # | Decision | Choice | Consequence |
|---|----------|--------|-------------|
| 1 | **Send mechanism** | Gmail API on the user's behalf (OAuth `gmail.send`) | Needs a Google Cloud OAuth app; "unverified app" warning is acceptable for a whitelisted circle. |
| 2 | **User scope** | Trusted circle / whitelist | Stays under Google's 100 test-user cap → **no OAuth verification required.** Smallest abuse & privacy surface. |
| 3 | **Data retention** | Persistent until the user deletes | Users get a dashboard, history, and a reusable address book. Reconciled with privacy in §11. |
| 4 | **Image storage** | Local container, auto-purged after the event **+** a resized "sane resolution" copy inlined into each email (CID) | Landing page shows full-res while it exists; email renders offline and survives the purge. |

---

## 4. Personas & Primary Flows

### Sender (the authenticated user)
1. **Sign in** with Google (consent to SSO + `gmail.send`, one time).
2. **Create an event/card:** upload image, set title, optional date/location,
   write a short personal message, choose whether to ask for RSVP.
3. **Build recipient list:** type/paste emails, or pick from saved contacts.
4. **Preview** the exact email (rendered with their name as sender).
5. **Send.** Kith builds one personalized message per recipient, injects tracking
   tokens, and sends each via the Gmail API throttled within quota.
6. **Track** on a dashboard: per-recipient status — Queued → Sent → Opened (≈) →
   Accepted / Declined.

### Recipient (no account, no app)
1. Receives a normal-looking personal email from their friend, with the card
   inlined.
2. Email contains a **"View invitation & RSVP"** button → opens the Kith landing
   page (full-res card + details).
3. Clicks **Accept** or **Decline** (optionally a +1 count / short note).
4. Sees a friendly confirmation. No login, ever. One opaque token = one recipient.

---

## 5. Authentication & Google OAuth

- **Identity / SSO:** Google OAuth 2.0 (OpenID Connect). We use the verified email
  as the account key. No passwords stored, ever.
- **Scopes requested:**
  - `openid`, `email`, `profile` — SSO identity.
  - `https://www.googleapis.com/auth/gmail.send` — send-only (cannot read the
    user's mailbox). This is the *minimum* sensitive scope and the most
    privacy-respecting choice.
  - *(Optional, deferred)* `contacts.readonly` for import — off by default; the
    contact list works fine with manual entry.
- **App posture:** A single Google Cloud project with an OAuth consent screen in
  **"Testing"** mode. Each permitted user is added as a **test user** (≤100).
  - Test users see a "Google hasn't verified this app" interstitial → "Advanced"
    → "Go to Kith (unsafe)". Acceptable for a trusted circle; documented in
    onboarding.
  - **No Google verification / security assessment needed** while we stay in
    Testing with the whitelist. This is the key payoff of Decision #2.
  - *Caveat:* refresh tokens issued by a Testing-mode app can expire after 7 days
    in some configurations. Mitigation: the app detects an invalid refresh token
    and silently re-prompts consent. (If this proves annoying, the path forward
    is "Publishing status: In production" without verification — still allowed
    for sensitive scopes with a warning — which we'll evaluate during the auth
    milestone.)
- **Token handling:** Store the **refresh token encrypted at rest** (see §12).
  Access tokens are short-lived and kept in memory / refreshed on demand. Tokens
  are revocable by the user and deleted on account deletion.

---

## 6. Sending Pipeline

```
create event ─► build recipient rows ─► [Send]
                                          │
                    ┌─────────────────────┴───────────────────────┐
                    ▼                                              ▼
            per recipient:                                   throttle loop
            - mint opaque token (≥128-bit)                   (respect quota,
            - render MIME (multipart/related):               batch + backoff)
              · HTML body w/ inlined CID image                     │
              · 1×1 open-pixel  → /t/o/{token}.gif                 ▼
              · RSVP buttons    → /t/rsvp/{token}?a=yes|no   Gmail API users.
              · plaintext fallback                           messages.send
            - personalize greeting (recipient name)          (user's creds)
                    │                                              │
                    └──────────────► message row: status=Sent ◄───┘
```

- **One message per recipient** (required anyway for per-recipient tokens). This
  also avoids exposing the whole list in a giant `To:`/`Bcc:` and looks personal.
- **Deliverability:** Because send goes through Gmail, Google applies the user's
  SPF/DKIM/DMARC automatically — far better inbox placement than a self-hosted
  SMTP server on a home IP (which would be blocklisted instantly).
- **Quota & throttling (important constraint):**
  - Consumer `@gmail.com`: ~**500 recipients / rolling 24 h**.
  - Workspace accounts: ~**2,000 / day**.
  - Gmail API per-user rate limit (~250 quota units/user/sec; `messages.send`
    ≈ 100 units) → realistically a few sends/sec.
  - **Implication:** large lists are sent as a throttled queue and may span
    multiple days for free Gmail accounts. The UI must surface this honestly
    ("Sending 320 of 500 today; remaining tomorrow"). A small background worker
    drains the send queue with exponential backoff on `429`/quota errors.
- **Idempotency:** each message row has a unique token; the worker only sends rows
  in `Queued` state and flips to `Sent` atomically, so a crash/restart never
  double-sends.

---

## 7. Tracking Design

Two signals, with honest reliability tiers:

| Signal | Mechanism | Reliability |
|--------|-----------|-------------|
| **Sent** | Gmail API returns a message id | High |
| **Opened** | 1×1 transparent GIF at `/t/o/{token}.gif` | **Best-effort only** |
| **Clicked** | "View invitation" link → `/i/{token}` | High |
| **Accepted / Declined** | RSVP buttons → `/t/rsvp/{token}?a=yes\|no` then confirm | High (explicit user action) |

**Why opens are unreliable (stated plainly in the UI):** Gmail routes all images
through `googleusercontent.com`, *caching and sometimes pre-fetching* them. This
means an "open" can register before the human looks, can be deduped/hidden, or can
fail to register if images are blocked. We therefore treat the pixel as a soft
hint and lead the dashboard with the **reliable** signals: Sent, Clicked, and
RSVP. (We also log a "viewed landing page" event, which is a stronger open proxy
than the pixel.)

- **Tokens** are opaque, single-purpose, high-entropy (`secrets.token_urlsafe`),
  and map to exactly one `(event, recipient)` pair. They reveal nothing about the
  recipient. RSVP actions require a confirm step (so a security scanner
  pre-fetching the link doesn't silently RSVP).
- **No third-party trackers**, no analytics SDKs, no cookies for recipients. All
  tracking is first-party and self-hosted.

---

## 8. Data Model (initial sketch)

SQLite (see §10 for rationale). PII columns marked 🔒 are encrypted at rest.

```
User
  id (uuid)              google_sub (unique)   email 🔒
  display_name           created_at            last_login_at
  oauth_refresh_token 🔒  settings (json)

Contact                  # the reusable address book (Decision #3)
  id  user_id→User       name 🔒   email 🔒   notes 🔒   created_at

Event                    # one card / occasion
  id  user_id→User       title     message_md    event_date?  location? 🔒
  rsvp_enabled (bool)    asset_id→Asset  purge_after  status   created_at

Asset                    # the uploaded image
  id  user_id→User       sha256    mime    full_path (local, purgeable)
  inline_path (resized)  width height bytes   purged_at?   created_at

Recipient                # one row per (event, person)
  id  event_id→Event     contact_id→Contact?   name 🔒   email 🔒
  token (unique, opaque) status[queued|sent|opened|accepted|declined|bounced]
  plus_one_count?        note? 🔒    sent_at?  first_open_at?  rsvp_at?

TrackingEvent            # append-only audit of signals
  id  recipient_id→Recipient   kind[sent|open_pixel|landing_view|accept|decline|bounce]
  at   user_agent?   ip_hash?   (raw IP never stored; hashed + truncated)
```

---

## 9. Tech Stack & Rationale

| Concern | Choice | Why |
|---------|--------|-----|
| Language | **Python 3.12** | Your wheelhouse; great Google client libs; fast to ship. |
| Web framework | **FastAPI** + Uvicorn | Async (good for the send-queue + I/O to Gmail), typed, clean DI, easy testing. |
| Templating / UI | **Jinja2 + HTMX + a little Alpine.js**, **Tailwind CSS (dark theme)** | Server-rendered = simple, SEO-irrelevant, no SPA build complexity. HTMX gives snappy dashboard updates. Dark theme per house style. |
| Persistence | **SQLite** (WAL mode) via SQLAlchemy | Relational tracking data with queries/joins; single-file, zero-ops, trivially backed up. *(Deviates from the usual "JSON for state" preference — flat JSON can't express the recipient/event/tracking relations or query them; SQLite is the right tool here. Config still TOML.)* |
| Config | **TOML** (`config.toml`) + env for secrets | House style; secrets via env so they never hit git. |
| Email build | `email.message.EmailMessage` (stdlib) | Full control over MIME/CID/multipart; no heavy deps. |
| Google | `google-auth`, `google-auth-oauthlib`, `google-api-python-client` | Official, well-maintained. |
| Image | **Pillow** | Resize to the inline "sane resolution," strip EXIF (privacy + size). |
| Crypto at rest | **`cryptography` (Fernet)** keyed from an env secret | Encrypt PII + refresh tokens. Simple, audited. |
| Background work | **In-process asyncio task** + a `Queued` table (no Celery/Redis) | One container, low volume; a DB-backed queue drained by an async worker is plenty. |
| Tests | **pytest** | House style. Pure-logic core is unit-tested; Gmail/OAuth mocked. |
| Container | **Docker** + `docker-compose` | One service + one volume. |
| Ingress / TLS | **Tailscale Funnel** | Exposes the container to the internet **with automatic Let's Encrypt HTTPS** on `<machine>.<tailnet>.ts.net`. This *is* our "free open SSL." |

---

## 10. Image Handling

1. **Upload** (authenticated, size + mime validated; only common raster types).
2. **Sanitize:** Pillow re-encodes and **strips EXIF/GPS metadata** (privacy).
3. **Two derivatives:**
   - **Full-res hosted copy** → served on the RSVP landing page; lives under the
     event's `purge_after` window, then deleted.
   - **Inline copy** resized to a sane resolution (target: long edge ≈ 1000–1200 px,
     re-encoded to keep each email well under typical clipping limits) → embedded
     per email as a `multipart/related` CID part. This renders without any external
     fetch and **survives the purge** so old emails still look right.
4. **Auto-purge:** a periodic sweep deletes hosted assets (and optionally the
   whole event) after `purge_after`. Default window: configurable, e.g. event
   date + 30 days; events with no date use created_at + N days.

---

## 11. Privacy & Data Handling

We chose **persistent** storage (Decision #3) for usability, which is in tension
with the "store little to no data" goal. We resolve it deliberately:

- **Scope of persistence:** only the *user's own* account, their saved contacts,
  their events, and the per-recipient tracking needed to power the dashboard.
- **Encrypt PII at rest:** recipient names/emails, contact entries, location,
  notes, the user's own email, and OAuth refresh token are Fernet-encrypted with
  a key from env (never in git, never in the DB). DB-at-rest leak ≠ PII leak.
- **Minimize third-party data:** recipients get no account and no cookie; raw IPs
  are never stored (only a salted, truncated hash for basic abuse signals); no
  external analytics or trackers.
- **Auto-purge heavy/ephemeral data:** hosted images and (optionally) whole past
  events expire on a schedule.
- **User control:** one-click **Export** (JSON of everything we hold on them) and
  **Delete account** (hard delete of user, contacts, events, assets, tokens; we
  do *not* keep tombstones beyond what's legally trivial for a free hobby
  service). Revoking Google access is linked from settings.
- **Gmail scope minimization:** `gmail.send` only — we can send but can never read
  the user's mailbox.

---

## 12. Security

- **Secrets** (OAuth client secret, Fernet key, session secret) via env only;
  `.env` git-ignored; documented in `.env.example`.
- **Session:** signed, httpOnly, `Secure`, `SameSite=Lax` cookies for the sender
  app. **CSRF tokens** on all state-changing forms.
- **Tokens:** ≥128-bit opaque, single-purpose; RSVP requires an explicit confirm
  click to defeat link-prefetch scanners.
- **Input:** strict upload validation (mime sniff + size cap), HTML in user
  messages is escaped/sanitized; Markdown rendered through a safe renderer.
- **Container hardening:** non-root user, read-only FS except the data volume,
  minimal base image, pinned deps.
- **Exposure:** Tailscale Funnel terminates TLS and forwards only chosen ports;
  the admin/dashboard requires auth; only `/i/...` and `/t/...` (recipient-facing,
  token-gated) are effectively public.
- **Rate limiting** on tracking + RSVP endpoints to blunt token-guessing/abuse.

---

## 13. Deployment

```
home server
└── docker-compose
    ├── kith        (FastAPI app + async send-worker, one image)
    │     volume: ./data  → sqlite db + uploaded/derived images
    │     env:    OAuth creds, FERNET_KEY, SESSION_SECRET, BASE_URL
    └── (tailscale) — Funnel enabled, forwarding 443 → kith:8000
```

- **TLS / domain:** Tailscale Funnel publishes
  `https://<machine>.<tailnet>.ts.net` with an auto-managed cert. `BASE_URL` is
  set to this so tracking/RSVP links are correct.
  - *Consideration:* recipient-facing links live on a `*.ts.net` domain, which is
    unfamiliar and *might* dent click trust slightly. v1 ships on `ts.net`; a
    later option is fronting with a Cloudflare Tunnel for a custom domain if link
    trust/deliverability warrants it. Logged as a risk, not a blocker.
- **Backups:** the SQLite file + `data/` volume; a nightly copy is sufficient for
  a hobby service.
- **Config:** `config.toml` for non-secret tunables (purge windows, daily send
  caps, inline image dimensions); env for secrets.

---

## 14. Legal / Trust

Static pages, linked in the footer and shown at signup:

- **Privacy Policy** — what we store, encryption, retention/purge, export/delete,
  Google scope usage (`gmail.send` only, no mailbox reading), no selling/sharing.
- **Terms of Service** — free, personal/non-commercial use only, no spam, sender
  is responsible for having consent to email their recipients.
- **Fitness-for-use disclaimer** — provided **"AS IS", without warranty of any
  kind**; no guarantee of deliverability or uptime; not liable for missed events.
  (Aligns with the MIT-style spirit; the code itself will be MIT-licensed.)

---

## 15. Proposed Project Layout

```
kith/
├── DESIGN.md                ← this file
├── README.md
├── LICENSE                  (MIT)
├── pyproject.toml
├── config.example.toml
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── kith/
│   ├── core/                ← pure logic, no framework imports (unit-tested)
│   │   ├── tracking.py      tokens, status transitions
│   │   ├── mailbuild.py     MIME assembly, CID inlining, personalization
│   │   ├── images.py        resize/strip-exif/derive
│   │   └── crypto.py        Fernet helpers for PII
│   ├── services/            ← side-effecting glue
│   │   ├── google_auth.py   OAuth flow + token refresh
│   │   ├── gmail.py         messages.send wrapper + quota/backoff
│   │   └── sendqueue.py     async worker draining Queued rows
│   ├── web/                 ← FastAPI app: routes, deps, templates
│   │   ├── app.py
│   │   ├── routes_auth.py / routes_app.py / routes_track.py
│   │   ├── templates/       (Jinja2, dark theme)
│   │   └── static/
│   ├── db/                  models.py (SQLAlchemy), migrations
│   └── config.py
└── tests/                   pytest (core fully covered; services mocked)
```

---

## 16. Roadmap / Milestones

Each gate is a working, committed, tested increment.

- **G0 — Scaffold.** Repo, pyproject, config, Docker, FastAPI "hello", dark-theme
  base template, pytest harness. *(this commit + next)*
- **G1 — Google SSO.** OAuth login/logout, encrypted refresh-token storage,
  test-user onboarding, account export/delete stubs.
- **G2 — Compose a card.** Image upload → sanitize → derive inline + full-res;
  event create/edit; recipient list entry; live email preview.
- **G3 — Send (the hard part).** MIME build w/ CID + tokens, Gmail API send,
  async throttled queue, quota handling, Sent status.
- **G4 — Track & RSVP.** Open pixel, landing page, accept/decline w/ confirm,
  dashboard with reliable-first status. Auto-purge sweep.
- **G5 — Polish & legal.** Privacy/ToS/disclaimer pages, contacts address book,
  export/delete fully wired, deploy behind Tailscale Funnel, backups.

---

## 17. Open Questions & Risks

1. **Refresh-token longevity in Testing mode** (§5) — may force periodic
   re-consent. Validate early in G1; fall back to "In production (unverified)" if
   it bites.
2. **Free-Gmail 500/day cap** — fine for family-scale lists; the UI must set
   expectations for big sends. Workspace users get 2,000.
3. **`*.ts.net` link trust** (§13) — monitor whether recipients hesitate to click;
   custom-domain path is ready if needed.
4. **Open-tracking accuracy** — accept it's a soft signal; lead with clicks/RSVP.
5. **Spam heuristics** — a tracking pixel + identical-ish bodies *could* nudge
   spam scoring even via Gmail. Mitigations: per-recipient personalization,
   plaintext alternative, modest image weight, and making the pixel optional.
6. **Abuse** (whitelisted users only, but still) — rate limits + the
   "non-commercial, you must have consent" ToS.

---

*End of v0.1. Next step on approval: lock the project name, then build G0
(scaffold) and commit.*
