# Kith Post — Design Document

> **Status:** Draft v0.1 · **Date:** 2026-06-29 · **Owner:** iliabaranov
>
> **Kith Post** — from *kith* (n.): one's friends, acquaintances, and
> neighbors, as in "kith and kin."
> A free, self-hosted, privacy-first digital invitation service: upload a card,
> pick your people, and send a personal invite **from your own Gmail** with
> open / accept / decline tracking. A non-commercial alternative to Punchbowl
> and Paperless Post.
>
> *(Name locked: **Kith Post**. That's the brand / display name; the code
> package stays `kith` for a short import path.)*

---

## 1. Summary & Vision

A single small container, hosted on a home server and exposed to the internet
via a **Cloudflare Tunnel** (free, auto-renewing HTTPS on a custom domain, no
open router ports; Tailscale Funnel is a viable alternative). A user
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
- **Trustworthy, honest tracking.** Reliable sent + accept/decline status, plus
  "opened" = the recipient visited the invitation page. **No tracking pixel** —
  no hidden beacons, ever.
- **Gentle automatic follow-ups.** Nudge non-responders on a sane, configurable
  schedule (halfway / 1 wk / 3 days out) — and stop the instant they engage.
- **Privacy by default.** Minimal data, encrypted PII at rest, user-owned
  export/delete, heavy assets auto-purged.
- **Cheap & self-hostable.** One container and one volume (a second of each only
  if you opt into WhatsApp), no paid dependencies.
  Free TLS via a Cloudflare Tunnel.
- **Clean architecture.** Pure-logic core, thin framework glue, testable with
  pytest.

### Non-Goals (for v1)
- Public open signup at internet scale (see §5, OAuth verification cost).
- Payments, premium tiers, ads, or profit-seeking monetization. *(One subtle,
  optional "tip jar" to offset hosting — e.g. Buy Me a Coffee / Ko-fi — is the
  lone exception, tracked as a later work item in §17. It links out to an external
  service, so no payment data ever touches Kith Post. It appears **only in the
  signed-in sender app, after the user has created their first event** — never to
  invited guests, and never in a sent email.)*
- Rich drag-and-drop card *designer* (we accept a finished image; we are not
  Canva). A future "templates" feature is out of scope for MVP.
- SMS or push delivery. Email is the default channel; **WhatsApp** was added
  later as an opt-in second channel (§6a) — off unless the operator turns it on,
  because it needs an unofficial client and carries real account risk.
- Mobile native apps. Responsive web only.
- Multi-language / i18n in v1 (English only).

---

## 3. Locked Decisions

These were decided up front and constrain everything downstream.

| # | Decision | Choice | Consequence |
|---|----------|--------|-------------|
| 1 | **Send mechanism** | Gmail API on the user's behalf (OAuth `gmail.send`) | Needs a Google Cloud OAuth app; "unverified app" warning is acceptable for a whitelisted circle. |
| 2 | **User scope** | Trusted circle / whitelist | Stays under Google's 100 test-user cap → **no OAuth verification required.** Smallest abuse & privacy surface. |
| 3 | **Data retention** | Persistent until the user deletes | Users get a dashboard, history, and a reusable address book. Reconciled with privacy in §12. |
| 4 | **Image storage** | Local container, auto-purged after the event **+** a resized "sane resolution" copy inlined into each email (CID) | Landing page shows full-res while it exists; email renders offline and survives the purge. |
| 5 | **Tracking** | **No open pixel.** Signals = Sent · Opened (= visited invitation page) · Accepted · Declined | Zero hidden beacons → cleaner privacy story. "Opened" undercounts (inlined card is viewable in-email without clicking), stated honestly. |
| 6 | **Run target** | Same container runs **locally on the laptop first**, then unchanged on the home server | One image, env-driven config, a `dry-run` send mode → full local test loop before exposing anything publicly. |
| 7 | **Automated reminders** | **On by default** for dated events: nudge non-clickers at halfway · 1 wk · 3 days out; fully configurable | Drives RSVPs without nagging — stops the moment they engage, hard cap of 3, sent as a "Re:" follow-up (sender-side threaded; see §8 caveat), Decline = opt-out, none for dateless events. See §8. |

---

## 4. Personas & Primary Flows

### Sender (the authenticated user)
1. **Sign in** with Google (consent to SSO + `gmail.send`, one time).
2. **Create an event/card:** upload image, set title, and turn on **only the
   blocks you want** — each is independently optional: date, time, location,
   message, RSVP buttons, and (if RSVP is on) a headcount/party-size question.
   With everything off it's a **plain holiday card** — just the image (and maybe a
   message). The recipient page renders only the enabled blocks.
3. **Build recipient list:** type/paste emails, or pick from saved contacts.
4. **Preview** the exact email (rendered with their name as sender).
5. **Send.** Kith Post builds one personalized message per recipient, injects tracking
   tokens, and sends each via the Gmail API throttled within quota.
6. **Track** on a dashboard: per-recipient status — Queued → Sent → Opened (≈) →
   Accepted / Declined.

### Recipient (no account, no app)
1. Receives a normal-looking personal email from their friend, with the card
   inlined.
2. Email contains a **"View invitation & RSVP"** button → opens the Kith Post
   landing page, which opens with a brief **envelope fade-in that crossfades into
   the invitation** (~1s; skipped under reduced-motion). Full-res card + details.
3. Can **click the card image to enlarge it** to fill the screen (click / ✕ / Esc
   to shrink back). If RSVP is on, clicks **Accept** or **Decline**; if the host
   enabled headcount, Accept gracefully asks **"how many of you are coming?"** (a
   simple stepper) before confirming.
4. Sees a friendly confirmation, and can **return to the same link anytime to
   change their response** (see §7). No login, ever. One opaque token = one recipient.
5. The only Kith Post branding a guest sees is one small, subtle footer link —
   *"Sent with Kith Post"* — to the signup page (the growth loop; a tooltip adds
   "make your own free invite"). **No donation prompt ever reaches a guest.**

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
    → "Go to Kith Post (unsafe)". Acceptable for a trusted circle; documented in
    onboarding.
  - **No Google verification / security assessment needed** while we stay in
    Testing with the whitelist. This is the key payoff of Decision #2.
  - *Caveat:* refresh tokens issued by a Testing-mode app can expire after 7 days
    in some configurations. Mitigation: the app detects an invalid refresh token
    and silently re-prompts consent. (If this proves annoying, the path forward
    is "Publishing status: In production" without verification — still allowed
    for sensitive scopes with a warning — which we'll evaluate during the auth
    milestone.)
- **Token handling:** Store the **refresh token encrypted at rest** (see §13).
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
              · "View invitation" → /i/{token}                     ▼
              · RSVP buttons    → /t/rsvp/{token}?a=yes|no   Gmail API users.
              · plaintext fallback (NO tracking pixel)       messages.send
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

## 6a. WhatsApp Channel (opt-in, via WAHA)

A second delivery channel alongside Gmail, added after v1. Same shape as the
email model — the host links **their own** WhatsApp account once and the server
sends on their behalf, so guests see a message from someone they know, not from a
service.

```
kith ──HTTP (compose network, X-Api-Key)──► WAHA container ──► WhatsApp
                                            one session per kith user
                                            /app/.sessions (named volume)
```

**Recipients get the card itself as a picture**, captioned with a short text and
their same `/i/{token}` link (text-only when the card has no image, or its inline
file has been purged). The picture travels as base64 over the compose network
rather than as a URL, so WhatsApp is never handed a link to the recipient's own
invitation page — but it does mean the image reaches Meta's servers, which an
emailed card never does. Tracking is
therefore *unchanged*: Opened, RSVP, headcount and allergies all come from the
invitation page, which is channel-agnostic. Nothing is added to the message —
no parameter, no redirect, no shortener.

### Why it is off by default

WAHA is an **unofficial** WhatsApp client. Using it is against WhatsApp's terms
of service, and a linked account can be restricted or banned. At personal-invite
volume to people who already have your number the practical risk is low, but it
is real, so:

- the operator must switch the channel on (`KITH_WHATSAPP_ENABLED`), and
- each host must accept an in-app warning naming the risk before any session is
  created. This gate is a product requirement, not a nicety.

### Where the credentials live

In WAHA's own volume, never in our database. kith stores only the session name,
the last status it saw, the linked number (encrypted), and WhatsApp's restriction
state — enough to render a page and explain a pause. Deleting an account unlinks
first, so a pairing can't outlive the account it belongs to. The volume sits
outside `./data` on purpose: the off-box backup covers `./data`, and these are
live WhatsApp credentials in the clear. Losing them costs a QR re-scan.

### Restrictions are instructions

WhatsApp shadow-restricts accounts that message too many unknown contacts — the
"reachout timelock" behind error 463, which leaves the session looking `WORKING`
while sends fail. WAHA surfaces it (and the separate per-cycle new-chat quota) on
the session, so:

- every batch is pre-flighted once, and a timelock or exhausted quota **stops**
  it, leaving recipients `queued` rather than burning attempt after attempt —
  each further attempt makes the account look worse;
- **never restart or re-pair** during a timelock. It follows the WhatsApp
  account, not the session, so re-pairing only adds churn;
- numbers are checked against WhatsApp before a first send, so a wrong digit
  costs one recipient instead of the host's ability to send;
- **sends are spaced out by a random 5-20s** each
  (`KITH_WAHA_SEND_GAP_MIN_SECONDS` / `..._MAX_SECONDS`). Random, not fixed: an
  exactly-even cadence is as machine-like as no gap at all, only slower, and the
  aim is to look like a person working down a list. This is why a WhatsApp batch
  runs **after** the response as a background task — a dozen invitations takes
  minutes, longer than an HTTP request should live and longer than the tunnel will
  hold one open. Recipients stay `queued` until the batch reaches them, so the
  dashboard shows real progress on a refresh, and a single-process guard stops a
  second Send from double-messaging people mid-batch. Reminders are paced the same
  way, since a sweep can find a dozen due at once.
- **The queue is durable without being a queue.** A message still to go is already
  a durable row — a `Recipient` with status `queued` — so the only thing missing
  was noticing that a card is owed a batch nobody is running. `Event.wa_batch_started_at`
  is that record: set when a send is handed off, cleared when a batch completes a
  full pass, and left set when one is stopped by a restriction. The maintenance
  sweep resumes any card that has been owed a batch for more than two minutes and
  less than a day, so a redeploy mid-list finishes itself, and invitations held by
  a timelock go out when it lifts. It only ever resumes a send that was actually
  started, which is what keeps a draft from being sent by a background job.

### Receipts (opt-in)

Set `KITH_WAHA_WEBHOOK_SECRET` and WAHA will POST `message.ack` and
`session.status` back to `http://kith:8000/wa/webhook` — the compose-network
address, since a public URL would leave the box and return through the tunnel for
nothing. Each POST carries `X-Webhook-Hmac`, a hex HMAC-SHA512 of the exact body
keyed with that secret (verified against a live container by pointing a real
session's webhook at a listener). It is the only endpoint in the app a machine
talks to, so it is the only one authenticated by a secret rather than a cookie;
without the secret no webhook is configured and the endpoint 404s.

Two things it buys: delivery/read receipts per recipient, and knowing a session
died *between* page visits, which is what makes the dashboard's "your WhatsApp
connection has dropped" banner timely rather than incidental.

### Engine choice

Pinned to the **GOWS** build (browserless Go engine, ~850 MB vs ~1.15 GB for the
Chromium one). This is not cosmetic: payload shapes differ between engines, **and
the session store is per-engine** (`/app/.sessions/<engine>/<name>`), so
switching engines re-pairs every user. WEBJS is the fallback if GOWS misbehaves;
re-test the send path if you switch.

### Operational notes worth remembering

Every WAHA call is bounded by an explicit timeout. Against a session that isn't
paired WAHA does not reliably fail fast — a wedged session can make `sendText`,
`contacts/check-exists` and `sessions/{s}/timelock` hang indefinitely (a clean
state mismatch returns a tidy 422 instead). Without that bound, one wedged
session could tie up a request handler or the reminder sweep. Restriction state
is read from the cheap `GET /api/sessions/{s}` (`me.reachoutTimelock`,
`me.messageCapping`); the dedicated `timelock`/`capping` endpoints force a
blocking fetch from WhatsApp and are only used as an explicit refresh.

Pairing has more than one path: 2026.8.1 added `PASSKEY_REQUIRED` and
`PASSKEY_CONFIRMATION_REQUIRED` next to `SCAN_QR_CODE`, and an unpaired session
drifts to `FAILED` on its own, so the linking UI handles all seven states.

QR pairing also has a hole worth naming: the code has to be scanned *by* the
phone, so it is useless *on* the phone — and a host who opens the linking page on
their phone is the common case, not the edge one. So the page offers WhatsApp's
own "link with phone number instead" path as an equal option:
`POST /api/{session}/auth/request-code` with the host's number and no `method`
returns an 8-character code they type into WhatsApp. That request is only valid
while the session is in `SCAN_QR_CODE`, so the route checks the state first
rather than surfacing WAHA's developer-facing 422.

---

## 7. Tracking Design

**No tracking pixel.** All signals come from explicit, first-party HTTP requests
the recipient's browser makes to *our* server — nothing hidden in the email body.

**WhatsApp receipts are not opens.** With receipts enabled (§6a), WAHA pushes
WhatsApp's own delivery and read acks back to us. Those are the channel's facts
about the message — the same ticks the host can already see on their phone — so
they live in their own columns (`wa_delivered_at`, `wa_read_at`, `wa_ack`) and
appear as their own line, never folded into Opened and never able to cancel a
reminder. Opened stays "a person loaded the invitation page". A read receipt is
also worth nothing as an absence, since recipients can switch them off.

**Automated fetches don't count as opens.** When a chat app is handed a URL it
fetches the page itself to build a preview card, so *sending* an invitation
produces a request for it. On the first real WhatsApp send this recorded an open
**0.4 s before the message finished sending** — the signal was reporting Meta's
crawler, not a guest. `core.tracking.is_automated_fetch` now excludes known
preview crawlers and declared prefetches (the page still renders — a preview is
useful to the recipient — we simply don't record it). The list over-matches on
purpose: an undercount is already accepted by design, an invented open is not.
This matters beyond the dashboard, because Opened is one of the signals that
cancels a reminder.

Signals are **channel-independent**: they come from the invitation page, so a
WhatsApp guest (§6a) is tracked exactly like an email one.

| Signal | Mechanism | Reliability |
|--------|-----------|-------------|
| **Sent** | Gmail API returns a message id (or WAHA a WhatsApp message id) | High |
| **Opened** | Recipient opens their invitation page at `/i/{token}`, **excluding automated fetches** (preview crawlers, declared prefetches, requests with no User-Agent, and anything within `OPEN_GRACE_SECONDS` of the send) | High *for those who click through* |
| **Accepted / Declined** | RSVP buttons → `POST /i/{token}/rsvp` → confirm step | High (explicit user action) |

**Honest caveat (stated plainly in the UI):** because the card image is **inlined
in the email**, a recipient can read the whole invitation without ever clicking
through — so "Opened" *undercounts* actual views. We deliberately accept this:
it's the price of having zero hidden beacons. "Opened" means "we know for certain
they visited the page," never an inferred or pre-fetched open. (We dropped the
1×1 pixel entirely — Gmail proxies/caches images via `googleusercontent.com`,
which made pixel "opens" both unreliable *and* a hidden tracker we'd rather not
ship.)

- **Tokens** are opaque, single-purpose, high-entropy (`secrets.token_urlsafe`),
  and map to exactly one `(event, recipient)` pair. They reveal nothing about the
  recipient. RSVP actions require a confirm step (so a security scanner
  pre-fetching the link doesn't silently RSVP).
- **No third-party trackers**, no analytics SDKs, no cookies for recipients. All
  tracking is first-party and self-hosted.

### Changing an RSVP (the link is the dashboard)

The recipient's tokenized link (`/i/{token}`) is **durable, not one-shot** — it's
their permanent view of the invitation. Plans change, so we make changing an
answer feel expected, not like an error path:

- After responding, the page shows their **current** answer as the stamp
  ("Coming!" / "Can't make it") plus a quiet **"Change response"** text link. It
  reverts to the choice buttons; picking again re-stamps. No friction, no
  "are you sure," no account.
- Returning to the link later (e.g. from the same email) always lands on their
  current status with the same change affordance — the link is effectively a tiny
  personal dashboard for that one invite.
- **Editable until the event** (sane default; configurable). Once `event_date`
  passes, the page goes read-only ("This event has passed") so late flips don't
  mislead the host.
- **Data:** `Recipient.status` holds the *latest* answer and `rsvp_at` its time;
  only the latest state is kept, with no per-signal audit log (see G4) — a flip
  updates `status` and `rsvp_at` in place.
- The sender's dashboard reflects the change on next page load; the running
  reminder logic already treats accept/decline as "engaged → stop," and a flip
  back to non-responded does **not** restart reminders (avoids nagging).

---

## 8. Automated Reminders

**Sane default: ON.** When an event has a date, Kith Post automatically nudges
recipients who were sent an invite but **haven't clicked through yet**. A reminder
reuses the recipient's existing token (so tracking stays continuous), is sent from
the user's Gmail like the original, and reads as a natural follow-up ("Re: …") —
not a fresh blast.

> **Threading caveat.** We set the Gmail `threadId` plus RFC `In-Reply-To`/
> `References` headers, which groups the reminder with the original in the *sender's*
> own Gmail. But Gmail **overwrites the outgoing `Message-ID`** (it doesn't honor a
> custom one from a free/Workspace account), and the `gmail.send` scope can't read
> the real ID back — so on the *recipient's* side the nudge may show as a separate
> conversation. Grouping there isn't guaranteed without a broader (read) scope we
> deliberately don't request.

### Default schedule (configurable; per-event override)
Computed relative to `event_date`:

1. **Halfway** — the midpoint between when the invite was sent and the event date.
2. **1 week out** — `event_date − 7d`.
3. **3 days out** — `event_date − 3d`.

### Who gets reminded, and when it stops
- **Target (default `not-clicked`):** recipients still in `sent` with no
  `landing_view` — i.e. they haven't clicked. *(Configurable to `no-rsvp` = nudge
  anyone who hasn't Accepted/Declined, even if they viewed the page.)*
- **Stops immediately** once the recipient meets the responded condition; any
  pending reminders for them are canceled. The whole point is to stop nagging the
  moment they engage.
- **Decline is the built-in opt-out:** every reminder carries the same
  Decline link, so a recipient can stop the nudges with one click and no account.
- Never sends after the event date; hard cap `max_per_recipient` (default 3).

### Edge cases (decided, sane defaults)
- **No event date → no reminders.** Reminders are date-relative; dateless events
  show the feature as unavailable (a manual "send a nudge now" action still works).
- **Event too close / slot already past:** each computed time is kept only if it's
  in the future *and* after the send time; past slots are skipped (logged
  `skipped:past`). A 2-days-out invite simply gets fewer or no reminders.
- **Collapsing slots:** times closer together than `min_gap_hours` (default 24) are
  merged, so two reminders never land the same day.
- **Time of day:** reminders fire at `send_hour_local` (default 09:00 in the
  sender's timezone), not at the exact offset instant — no 3 a.m. emails.
- **Downtime-safe:** each reminder is a persisted row with a `scheduled_for`
  timestamp; the worker fires any `pending` row whose time has passed. If the
  container is off (laptop closed, server reboot), overdue reminders go out on the
  next start — delayed, never dropped or double-sent (state flips atomically).

### Mechanics
Reuses the existing async send-worker and Gmail quota throttle (§6) — **no new
infrastructure.** At send time Kith Post computes each recipient's reminder slots and
writes `Reminder` rows; a periodic sweep (every few minutes) enqueues those that
are due *and* still match the target, then they flow through the same throttled
`messages.send` path and count against the same daily cap. In `dry-run` /
`self-only` modes (§14) reminders are exercised exactly like first sends, so the
whole schedule is testable locally (with an optional clock-override to fast-forward
in tests).

### Config (`config.toml`; per-event overridable)
```toml
[reminders]
enabled           = true            # sane default: on
target            = "no-rsvp"       # or "not-clicked"
offsets           = ["halfway", "7d", "3d"]
send_hour_local   = 9               # ~9am sender-local, not the exact instant
min_gap_hours     = 24              # merge reminders closer than this
max_per_recipient = 3
```

---

## 9. Data Model (initial sketch)

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
  asset_id→Asset         purge_after    status     created_at
  blocks (json)          # which blocks recipients see — any / all / none:
                         #   {date,time,location,message,rsvp,headcount} (bool each)
                         #   all-off (except image) = a plain holiday card
  reminder_cfg (json)?   # per-event override of [reminders]; null = inherit global
                         # (reminders only apply when blocks.rsvp is on)

Asset                    # the uploaded image
  id  user_id→User       sha256    mime    full_path (local, purgeable)
  inline_path (resized)  width height bytes   purged_at?   created_at

Recipient                # one row per (event, person)
  id  event_id→Event     contact_id→Contact?   name 🔒   email 🔒
  token (unique, opaque) status[queued|sent|opened|accepted|declined|bounced]
  party_size?            note? 🔒    sent_at?  first_open_at?  rsvp_at?
  # party_size = total people coming, from the headcount stepper (if enabled).
  # status = LATEST answer (mutable); rsvp_at updates on each change.
  msg_id_hdr?  thread_id?   # RFC822 Message-ID + Gmail thread of the first send,
                            # so reminders thread as replies (In-Reply-To/References)

Reminder                 # one scheduled nudge for a non-responder (§8)
  id  recipient_id→Recipient   slot[halfway|7d|3d|manual]   scheduled_for (UTC)
  state[pending|sent|skipped|canceled]   skip_reason?   sent_at?   gmail_msg_id?
```

---

## 10. Tech Stack & Rationale

| Concern | Choice | Why |
|---------|--------|-----|
| Language | **Python 3.12** | Your wheelhouse; great Google client libs; fast to ship. |
| Web framework | **FastAPI** + Uvicorn | Async (good for the send-queue + I/O to Gmail), typed, clean DI, easy testing. |
| Templating / UI | **Jinja2 + minimal hand-written vanilla JS** | Server-rendered = simple, SEO-irrelevant, no SPA build complexity and no JS framework. Pages reflect state on load; the few interactive bits (envelope animation, group filter, steppers) are small vanilla scripts. Visual direction is **"Kitchen Table" — light & warm** (see `DESIGN-LANGUAGE.md`); this product deliberately overrides the usual dark-theme default to feel friendly and approachable for a non-technical audience. |
| CSS | **Hand-written, token-based CSS** (no framework) | The design is bespoke (paper grain, rubber stamp, envelope, washi tape) — Tailwind wouldn't help and would add a Node build step + utility-class bloat + pull toward the "AI-slop" default look. A small `:root` token system + component classes is cleaner. *(Tailwind was considered and dropped for these reasons.)* |
| Persistence | **SQLite** (WAL mode) via SQLAlchemy | Relational tracking data with queries/joins; single-file, zero-ops, trivially backed up. *(Deviates from the usual "JSON for state" preference — flat JSON can't express the recipient/event/tracking relations or query them; SQLite is the right tool here. Config still TOML.)* |
| Config | **TOML** (`config.toml`) + env for secrets | House style; secrets via env so they never hit git. |
| Email build | `email.message.EmailMessage` (stdlib) | Full control over MIME/CID/multipart; no heavy deps. |
| Google | `google-auth`, `google-auth-oauthlib`, `google-api-python-client` | Official, well-maintained. |
| Image | **Pillow** | Resize to the inline "sane resolution," strip EXIF (privacy + size). |
| Crypto at rest | **`cryptography` (Fernet)** keyed from an env secret | Encrypt PII + refresh tokens. Simple, audited. |
| Background work | **In-process asyncio task** + a `Queued` table (no Celery/Redis) | One container, low volume; a DB-backed queue drained by an async worker is plenty. |
| Tests | **pytest** | House style. Pure-logic core is unit-tested; Gmail/OAuth mocked. |
| Env / deps | **`uv`** managing a standard **`.venv`** | Fast installs + lockfile; still real venv isolation. *(pip considered; uv chosen for speed.)* |
| Lint / format | **Ruff** | One fast tool replacing black + isort + flake8. |
| Container | **Docker** + `docker-compose` | One service + one volume. |
| Ingress / TLS | **Cloudflare Tunnel** | Exposes the container over HTTPS on a custom domain with Cloudflare-managed TLS — no open router ports, works behind CGNAT. This *is* our "free open SSL." (Tailscale Funnel is a viable alternative; see `docs/deploy.md`.) |

---

## 11. Image Handling

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

## 12. Privacy & Data Handling

We chose **persistent** storage (Decision #3) for usability, which is in tension
with the "store little to no data" goal. We resolve it deliberately:

- **Scope of persistence:** only the *user's own* account, their saved contacts,
  their events, and the per-recipient tracking needed to power the dashboard.
- **Encrypt PII at rest:** recipient names/emails/phone numbers, contact entries,
  location, notes, the user's own email and linked WhatsApp number, and the OAuth
  refresh token are Fernet-encrypted with
  a key from env (never in git, never in the DB). DB-at-rest leak ≠ PII leak.
- **Minimize third-party data:** recipients get no account and no cookie; IPs are
  never stored at all — they serve only as an in-memory rate-limiting key; no
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

## 13. Security

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
- **Exposure:** the Cloudflare Tunnel terminates TLS at the edge and forwards to
  the app over the container network — no inbound ports are opened on the host;
  the admin/dashboard requires auth; only `/i/{token}` and its sub-paths
  (recipient-facing, token-gated) and, where receipts are enabled,
  `POST /wa/webhook` (HMAC-authenticated, no cookie) are effectively public.
- **Rate limiting** on tracking + RSVP endpoints to blunt token-guessing/abuse.

---

## 14. Running It — Local-First, Then Home Server

**Design rule: it's the *same image* everywhere.** The only differences between
your laptop and the home server are environment variables and how ingress is
provided. There are no laptop-only code paths, and nothing external (no Postgres,
Redis, S3) — just the app, a SQLite file, and a data folder — so "works on my
laptop" genuinely predicts "works on the server."

### 13.1 Local dev loop (laptop, before any public exposure)

```
laptop
├── (fast inner loop)   uvicorn kith.web.app:app --reload   ← no Docker, hot reload
└── (parity check)      docker compose up                   ← the real image, ./data volume
        env: BASE_URL=http://localhost:8000
             KITH_SEND_MODE=dry-run            ← writes .eml to data/outbox/, no Gmail call
             FERNET_KEY / SESSION_SECRET = dev values from .env
```

- **`KITH_SEND_MODE`** (config + env) has three modes so you can test the full
  pipeline safely:
  - `dry-run` *(default for local)* — composes the real MIME and writes each
    message to `data/outbox/<token>.eml`. Open it in any mail client to verify the
    inlined image, links, and personalization. **No Gmail call, no quota burned,
    nobody emailed.**
  - `self-only` — actually sends, but only to the logged-in user's own address
    (great for a true end-to-end test against your own inbox).
  - `live` — normal sending. Used on the server (and only deliberately locally).
- **OAuth locally:** Google permits `http://localhost` redirect URIs **without
  HTTPS**, so SSO works on the laptop. Register *both* redirect URIs on the one
  OAuth client up front:
  `http://localhost:8000/auth/callback` **and**
  `https://<your-domain>/auth/callback`.
  `BASE_URL` selects which one the app uses — no code change between environments.
- **End-to-end recipient test locally (optional):** recipients can't reach
  `localhost`. Two easy options without touching the home server:
  1. Run `tailscale serve` / `tailscale funnel` **on the laptop** (it's a tailnet
     node too) to get a temporary public HTTPS URL; set `BASE_URL` to it.
  2. Stay in `self-only` and click your own links from the same machine.
- **Data lives in `./data`** (git-ignored): SQLite db + uploaded/derived images +
  the dry-run `outbox/`. Delete the folder to reset; copy it to inspect state.

### 13.2 Home-server deployment (unchanged image)

```
home server
└── docker compose
    ├── kith        (FastAPI app + async send-worker, one image — same as laptop)
    │     volume: ./data  → sqlite db + uploaded/derived images
    │     env:    OAuth creds, FERNET_KEY, SESSION_SECRET,
    │             BASE_URL=https://example.com,
    │             KITH_SEND_MODE=live
    └── cloudflared — Cloudflare Tunnel, forwarding 443 → kith:8000
```

- **Promotion = change env, not code.** Flip `BASE_URL` to the custom domain and
  `KITH_SEND_MODE` to `live`; everything else is identical to what you tested.
- **TLS / domain:** a Cloudflare Tunnel publishes `https://example.com` with
  Cloudflare-managed TLS — free auto-HTTPS, no open router ports, works behind
  CGNAT. Recipient links and OAuth callback use `BASE_URL`. See
  [`docs/deploy.md`](./docs/deploy.md) for the full walkthrough.
  - *Consideration:* Cloudflare terminates TLS, so it can see decrypted traffic.
    For a path where no third party sees plaintext, deploy.md §7 sketches a
    VPS + Caddy + Tailscale alternative (~$5/mo, self-managed Let's Encrypt).
- **Backups:** the SQLite file + `data/` volume; a nightly copy is sufficient for
  a hobby service.
- **Config:** `config.toml` for non-secret tunables (send mode, purge windows,
  daily send caps, inline image dimensions); env for secrets and per-environment
  values (`BASE_URL`, keys). A `config.example.toml` + `.env.example` are checked
  in; the real ones are git-ignored.

---

## 15. Legal / Trust

Static pages, linked in the footer and shown at signup:

- **Privacy Policy** — what we store, encryption, retention/purge, export/delete,
  Google scope usage (`gmail.send` only, no mailbox reading), no selling/sharing.
- **Terms of Service** — free, personal/non-commercial use only, no spam, sender
  is responsible for having consent to email their recipients.
- **Fitness-for-use disclaimer** — provided **"AS IS", without warranty of any
  kind**; no guarantee of deliverability or uptime; not liable for missed events.
  (Aligns with the MIT-style spirit; the code itself will be MIT-licensed.)

---

## 16. Proposed Project Layout

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
│   │   ├── reminders.py     pure slot computation (offsets→times, skip/merge) ← unit-tested
│   │   └── crypto.py        Fernet helpers for PII
│   ├── services/            ← side-effecting glue
│   │   ├── google_auth.py   OAuth flow + token refresh
│   │   ├── gmail.py         messages.send wrapper + quota/backoff
│   │   ├── sendqueue.py     async worker draining Queued rows
│   │   └── scheduler.py     periodic sweep: fire due Reminder rows via sendqueue
│   ├── web/                 ← FastAPI app: routes, deps, templates
│   │   ├── app.py
│   │   ├── routes_auth.py / routes_app.py / routes_track.py
│   │   ├── templates/       (Jinja2, "Kitchen Table" light/warm)
│   │   └── static/
│   ├── db/                  models.py (SQLAlchemy), migrations
│   └── config.py
└── tests/                   pytest (core fully covered; services mocked)
```

---

## 17. Roadmap / Milestones

Each gate is a working, committed, tested increment.

- **G0 — Scaffold.** Repo, pyproject, config, Docker + `docker compose`, FastAPI
  "hello", light/warm base template (per `DESIGN-LANGUAGE.md`), pytest harness, and the **local dev loop**
  (uvicorn `--reload`, `KITH_SEND_MODE=dry-run`, `.env.example`). **✅ Done** — runnable
  landing page, `make` dev loop, 6 tests green, ruff clean.
- **G1 — Google SSO.** OAuth login/logout, encrypted refresh-token storage,
  test-user onboarding, account export/delete stubs. **✅ Done** — google-auth-oauthlib
  flow + dev-login fallback, `User` model w/ Fernet-encrypted email/refresh-token,
  sessions, `/account` export + delete, `docs/google-oauth-setup.md`. 15 tests green.
- **G2 — Compose a card.** Image upload → sanitize → derive inline + full-res;
  event create/edit; recipient list entry; live email preview. **✅ Done** —
  Pillow pipeline (EXIF-strip/resize), Asset/Event/Recipient models w/ optional
  `blocks`, compose form (full-invite default), dashboard, server-rendered preview
  card. 52 tests + a live image-upload E2E (0 errors). *(Live as-you-type preview
  deferred; preview renders on the event page.)*
- **G3 — Send (the hard part).** MIME build w/ CID + tokens, Gmail API send,
  async throttled queue, quota handling, Sent status. **✅ Done** — pure
  `core/mailbuild` (multipart/alternative + inlined card via `cid:`, no pixel),
  `services/gmail` (refresh-token → `messages.send`), `services/send` with three
  modes (`dry-run` writes `.eml` to `data/outbox`, `self-only`, `live`), real Send
  button on the event page, public recipient pages at `/i/{token}` (invite, image,
  `.ics`). 78 tests + a live E2E (dry-run → outbox `.eml`, recipient landing,
  token image, all-day **and** TZ→UTC timed `.ics`, bad-token 404; 0 errors).
  *(Throttled async queue deferred — direct per-recipient send is fine for a
  friends-and-family circle; revisit if volume needs it.)*
- **G-AB — Address book.** Reusable per-user contacts (encrypted email/name +
  blind-index dedup), a `/contacts` page (add/import/edit/delete/CSV export),
  import-into-compose, and an opt-in "save new people?" prompt after create.
  **✅ Done** — 4 phases, 103 tests, live-smoked; contacts in account export +
  delete cascade. *(Inserted before G4 at the user's request.)*
- **G4 — Track & RSVP.** Invitation landing page (logs the "Opened"/`landing_view`
  signal), accept/decline w/ confirm, dashboard. Auto-purge sweep. *(No pixel.)*
  **✅ Done** — `/i/{token}` marks Opened on first visit; `POST /i/{token}/rsvp`
  persists coming/declined + party size (server-clamped to `headcount_max`),
  progressive-enhancement form (works no-JS, PRG reload renders the stamp),
  replies lock after the event date; the event page shows summary tallies +
  a per-recipient status list. Latest-state only (no audit log). 112 tests.
  *(Auto-purge sweep deferred to G5/G6.)*
- **G5 — Automated reminders.** Reminder scheduler (§8): slot computation w/ edge
  cases, downtime-safe `pending` rows, target/cancel logic, reply-threaded sends
  reusing G3's queue, per-event config. Dashboard shows reminder status.
  **✅ Done** — pure `core/reminders.py` (halfway/7d/3d → 9am-local snap → past/
  after-event skip → min-gap merge → cap), `Reminder` table, in-process
  `services/scheduler.py` sweep (testable `sweep_tick` + lifespan loop), reply
  threading via Gmail `threadId`, on-by-default for dated+RSVP events, target
  `no-rsvp` (stop on reply), engage-cancel on RSVP, event-page card + per-event
  toggle. Also **edit reconciliation** (edits no longer wipe RSVPs) and a
  **details-changed → re-send & re-collect** flow (date/time/location change prompts
  an opt-in re-send that clears prior RSVPs and reschedules). 148 tests.

> Test counts in the gates above are historical — what the suite stood at when
> that gate landed. `make test` is the current number (473 at the time of
> writing); don't read a gate's figure as today's.
- **G6 — Polish, deploy & legal.** *(Partial.)* **✅ Done:** contacts address book,
  export/delete, Cloudflare-Tunnel deploy (`live` mode), off-box backups,
  auto-purge of heavy full-res images past a retention window, and the
  Privacy/ToS/disclaimer pages (`/privacy`, `/terms`). **Remaining:** a subtle
  "buy me a coffee" donation link (signed-in only, after the first event; links
  out, no payment data stored).
- **G8 — WhatsApp delivery channel.** A second channel alongside Gmail (§6a):
  invitations and reminders sent from each host's own WhatsApp account via a
  self-hosted WAHA container, one session per user. **✅ Done** — pinned GOWS
  image on the compose network only, `services/waha` client (every call bounded)
  + `services/wa_session` for the kith-side link, risk-gated linking flow at
  `/account/whatsapp` with both QR and typed-code pairing, a second recipient box
  in compose plus a per-contact channel choice in the address book, the card sent
  as a picture captioned with the invitation link, per-channel send and reminders
  with timelock/quota back-off, sends paced a random 5–20s apart from a background
  batch, opt-in delivery/read receipts over an HMAC-signed webhook, and
  unlink-on-account-delete. Off by default (`KITH_WHATSAPP_ENABLED` +
  `KITH_WAHA_API_KEY`). "Opened" is unchanged and still means a person loaded the
  invitation page — receipts are shown separately and never counted as one.
- **G7 — Richer RSVP & contacts.** **✅ Done** — optional reply note + allergies
  toggle, adults/kids headcount split, address-book group tags (filter + add a
  whole group at once), and non-destructive edit reconciliation with a
  details-changed re-send/re-collect flow.

---

## 18. Open Questions & Risks

1. **Refresh-token longevity in Testing mode** (§5) — may force periodic
   re-consent. Validate early in G1; fall back to "In production (unverified)" if
   it bites.
2. **Free-Gmail 500/day cap** — fine for family-scale lists; the UI must set
   expectations for big sends. Workspace users get 2,000.
3. **Link trust** — recipient links now live on a **custom domain via the
   Cloudflare Tunnel** with valid TLS, resolving the earlier `*.ts.net`
   unfamiliarity concern.
4. **"Opened" undercounts** — since there's no pixel and the card is inlined,
   recipients who don't click through aren't counted as opened. Accepted by
   design; the UI states it. RSVP and Sent remain the load-bearing signals.
5. **Spam heuristics** — identical-ish bodies *could* nudge spam scoring even via
   Gmail. Mitigations: per-recipient personalization, plaintext alternative, and
   modest inline-image weight. (Dropping the pixel also removes one classic
   spam-filter trigger.)
6. **Abuse** (whitelisted users only, but still) — rate limits + the
   "non-commercial, you must have consent" ToS.
7. **Reminders vs. quota & annoyance** (§8) — reminders share the daily Gmail cap,
   so a big list mid-send could push reminders to the next day's quota; the
   scheduler must interleave fairly. Annoyance is bounded by the hard cap, the
   stop-on-engage rule, and Decline-as-opt-out. Timezone correctness for
   `send_hour_local` needs care (store the sender's tz at send time).

---

*End of v0.1. Name locked: **Kith Post**. Next step on approval: build G0 (scaffold +
local dev loop) and commit.*
