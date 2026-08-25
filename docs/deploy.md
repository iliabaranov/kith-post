# Deploying Kith Post (home server + Cloudflare Tunnel)

The goal: run the container on your home server and expose it at a real domain
over HTTPS, **without opening any router ports**. Cloudflare Tunnel gives free
auto-TLS and works behind CGNAT. (A privacy-maximising VPS+Caddy alternative is
sketched at the end.)

Everything is one image; moving from laptop to server is a `.env` change, not a
code change.

## Pre-flight checklist

Work top to bottom; each item maps to a section below.

- [ ] `example.com` is **Active** on Cloudflare (registrar nameservers switched) — §1
- [ ] Home server has **Docker + Docker Compose** and can `git clone` the repo — §1
- [ ] `.env` created from `.env.example` with real `KITH_SECRET_KEY` + `KITH_FERNET_KEY` (backed up) — §2
- [ ] `KITH_BASE_URL=https://example.com`, `KITH_SEND_MODE=self-only` to start — §2
- [ ] Cloudflare tunnel created; token in `CLOUDFLARE_TUNNEL_TOKEN`; public hostname `example.com → http://kith:8000` — §3
- [ ] Google OAuth: `https://example.com/auth/callback` added as redirect URI; `example.com` an authorized domain — §4
- [ ] `docker compose --profile public up -d --build`; sign in; self-only test send round-trips — §5
- [ ] Flip `KITH_SEND_MODE=live` and re-up — §5

---

## 0. Why the public URL matters

Recipients are on the open internet and are **not** on your tailnet. The emailed
"View invitation" links and the OAuth redirect are both built from
`KITH_BASE_URL`. So the single most important config on the server is:

```
KITH_BASE_URL=https://example.com
```

If that's wrong, guests get dead links.

---

## 1. Prerequisites

- **Server**: Docker + Docker Compose. `git` to pull the repo.
- **Domain**: `example.com` added to a (free) Cloudflare account — change the
  registrar's nameservers to the ones Cloudflare assigns, wait for it to go
  "Active." DNS now lives at Cloudflare.
- **Google OAuth client**: the one from G1 (`docs/google-oauth-setup.md`).

---

## 2. Secrets

On the server, in the repo root, `cp .env.example .env` and fill it in.

```bash
# session signing key
python3 -c "import secrets; print('KITH_SECRET_KEY=' + secrets.token_urlsafe(48))"
# encryption key — BACK THIS UP somewhere safe
python3 -c "from cryptography.fernet import Fernet; print('KITH_FERNET_KEY=' + Fernet.generate_key().decode())"
```

- **Fresh start (recommended):** generate a *new* `KITH_FERNET_KEY`, start with an
  empty `data/`, and just sign in again on the server. Simplest.
- **Migrate existing data:** copy your laptop's `data/` to the server *and* set
  `KITH_FERNET_KEY` to the **same** key you used locally, or the stored
  emails/refresh tokens won't decrypt.

Set the rest in `.env`:

```
KITH_BASE_URL=https://example.com
KITH_SEND_MODE=self-only          # flip to `live` once you've tested
KITH_GOOGLE_CLIENT_ID=...         # from Google Cloud Console
KITH_GOOGLE_CLIENT_SECRET=...
CLOUDFLARE_TUNNEL_TOKEN=...        # filled in step 3
```

`.env` is git-ignored — it never leaves the server.

---

## 3. Cloudflare Tunnel

In the Cloudflare dashboard → **Zero Trust → Networks → Tunnels**:

1. **Create a tunnel** (name it `kith`). Choose the **Docker** connector — Cloudflare
   shows a `docker run ... --token eyJ...` command. Copy just the **token**
   (the long `eyJ...` string) into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.
   (Our compose runs the connector for you; you only need the token.)
2. Add a **Public Hostname** to the tunnel:
   - Subdomain: *(blank)* · Domain: `example.com`
   - Service: **HTTP** → `kith:8000`
     (`kith` is the compose service name; the connector shares its network.)
3. Save. Cloudflare provisions the TLS cert and the `example.com` DNS record
   automatically.

The app is bound to `127.0.0.1:8000` on the host and reached only through this
tunnel — nothing is exposed to your LAN or a public IP.

---

## 4. Google OAuth wiring

In Google Cloud Console → **APIs & Services → Credentials →** your OAuth client:

- **Authorized redirect URIs**: add `https://example.com/auth/callback`
  (keep `http://localhost:8000/auth/callback` for local dev).
- **OAuth consent screen → Audience**: keep yourself (and any trusted circle) as
  **Test users** — the app stays in Testing mode, no verification needed.
- Consent screen → **Authorized domains**: add `example.com`.

The app requests only `gmail.send` (a *sensitive*, not *restricted*, scope), so no
security assessment is involved. Testing-mode refresh tokens expire ~7 days, so
you may need to sign in again periodically until you verify the app.

---

## 5. Run it

```bash
git clone <your-repo> kith && cd kith
cp .env.example .env && $EDITOR .env      # step 2 + 3
docker compose --profile public up -d --build
docker compose logs -f                    # watch kith + cloudflared come up
```

> **Data dir ownership.** The container runs as uid **10001** (`app`), and
> `./data` is a bind mount, so the host directory must be writable by that uid or
> SQLite crashes on startup with `attempt to write a readonly database`. A fresh
> empty `data/` that the container creates is fine; if you *migrate* an existing
> `data/` (or restore a backup) from another user, run once on the host:
> `sudo chown -R 10001:10001 data`.

Then open `https://example.com`, sign in with Google (accept the "unverified app"
warning), and:

1. With `KITH_SEND_MODE=self-only`, create a card and send — the email lands in
   **your** inbox. Click the emailed link; it now opens `https://example.com/i/...`
   (a real public URL), so RSVP works end to end.
2. Happy? Set `KITH_SEND_MODE=live` in `.env` and `docker compose up -d` to
   apply. Now sends go to actual recipients.

---

## 5a. Optional: the WhatsApp channel (WAHA)

Invitations and reminders can also go out over WhatsApp, sent from each host's own
account by a self-hosted [WAHA](https://github.com/devlikeapro/waha) container
(Apache-2.0). Skip this section entirely if you only want email.

> **Read this before enabling it.** WAHA is an **unofficial** WhatsApp client.
> Using it is against WhatsApp's terms of service and a linked account can be
> restricted or banned. At personal-invite volume to people who already have your
> number the practical risk is low, but it is real. Every host is warned in-app
> before they link, and the channel stays off until you turn it on.

```bash
# in .env
KITH_WHATSAPP_ENABLED=true
KITH_WAHA_API_KEY=<a long random string>   # the app AND the container read this

docker compose --profile public --profile whatsapp up -d
```

Then each host links their own account at **`/account/whatsapp`**: accept the
warning, press Link, and pair one of two ways —

- **scan the QR** with WhatsApp → Settings → Linked devices (needs a second
  screen: a QR has to be scanned *by* the phone, so it can't be *on* it); or
- **type a code**, for a host reading the page on the phone being linked. They
  enter that phone's number and get an 8-character code for WhatsApp → Settings →
  Linked devices → *Link with phone number instead*. The number is used for that
  one request and never stored. WhatsApp only issues a code while the session is
  waiting to pair, so an attempt left too long has to be restarted first.

**What the compose file already does for you, and why:**

- **The image is pinned** (`devlikeapro/waha:gows-2026.8.1`), never `:latest` —
  same reasoning as the cloudflared pin. Dependabot will open the bump PR.
- **No published ports.** The app reaches it at `waha:3000` over the compose
  network; nothing is exposed to the LAN or the tunnel. Note that *every* WAHA
  route sits behind the API key, including `/health`.
- **The dashboard and Swagger are off**, but set `WAHA_DASHBOARD_USERNAME` /
  `WAHA_DASHBOARD_PASSWORD` in `.env` anyway: with them unset WAHA generates its
  own and **prints them to its log**, and it does that even with the UI disabled.
  To
  poke at a misbehaving pairing, bring it up temporarily on localhost:
  ```bash
  WAHA_DASHBOARD_PASSWORD=<something long> \
  docker compose -f docker-compose.yml -f docker-compose.dashboard.yml \
    --profile whatsapp up -d waha
  ```
- **Sessions live in a named volume** (`waha-sessions:/app/.sessions`),
  deliberately *outside* `./data`. The off-box backup below covers `./data`, and
  these are live WhatsApp credentials in the clear — so they are **not** backed
  up. Losing them costs each host a QR re-scan, nothing more.

**Engine choice is not cosmetic.** This is the GOWS build (browserless Go engine,
~850 MB vs ~1.15 GB for the Chromium one). WEBJS is the fallback if GOWS
misbehaves — but payload shapes differ between engines **and the session store is
per-engine** (`/app/.sessions/<engine>/<name>`), so switching engines makes every
host re-pair, and the send path needs re-testing.

**Pacing.** WhatsApp invitations go out a random 5-20 seconds apart, so a batch
runs in the background after the page responds rather than during the request —
a dozen guests takes a few minutes. The event page says so and fills in as the
batch works through the list. If a deploy interrupts a batch, the maintenance
sweep picks it up within a few minutes and finishes the list on its own — the
pending work is durable because a waiting recipient is a row, not something held
in memory. Pressing Send again also works.

**Delivery + read receipts (optional).** Add a secret and WAHA will report back:

```bash
# in .env
KITH_WAHA_WEBHOOK_SECRET=<a long random string>
```

Each recipient then shows "Delivered on WhatsApp" / "Read on WhatsApp", and a
session that dies is noticed straight away rather than at the next page load.
WAHA POSTs to `http://kith:8000/wa/webhook` over the compose network, signing each
body with that secret; without it no webhook is configured and the endpoint
refuses everything. Receipts are **not** treated as "Opened" — that still means a
person loaded the invitation page.

**Monitoring.** `/healthz` stays deliberately dependency-free — the uptime cron
pings it every five minutes and a WhatsApp outage must not read as the site being
down. Add a second check on **`/healthz?deep=1`** if you want to be told about the
channel: it answers `503` when WAHA is unreachable. The container's own healthcheck
covers WAHA from the inside.

**If sending stops working:**

- *"WhatsApp has paused new conversations"* — a reachout timelock (the error-463
  restriction). Recipients stay queued; wait for the date shown. **Do not restart
  or re-pair the session:** the restriction follows the WhatsApp account, not the
  session, and re-pairing only adds churn that looks worse.
- *"used up WhatsApp's allowance"* — the per-cycle new-chat quota. Same deal: the
  invitations wait for the next cycle.
- The host's dashboard says so on its own — a dropped link raises a banner there,
  the same way an expired Google token does — so you don't have to notice it from
  a failed send.
- *Session shows `FAILED`* — WhatsApp ended the linked device. Re-link; anything
  already sent keeps working, since the invitation lives on the web page rather
  than in the message.

---

## 6. Backups & updates

- **Back up `data/`** — it holds the SQLite DB, uploaded images, and (if you
  didn't set `KITH_FERNET_KEY`) the dev key. A periodic copy of `data/` + your
  `KITH_FERNET_KEY` is a full backup.
- **WhatsApp pairings are not backed up, on purpose** (§5a). They live in the
  `waha-sessions` volume rather than `data/`, because shipping live WhatsApp
  credentials off-box is a worse trade than asking each host to re-scan a QR.
- **Update:**
  ```bash
  git pull
  docker compose --profile public up -d --build
  ```
  Schema changes are additive and applied automatically on startup
  (`ensure_schema`), so pulls are safe.

---

## 7. Alternative: VPS + Caddy + Tailscale (TLS you control)

If you'd rather Cloudflare *not* terminate TLS: run a small VPS with **Caddy**
serving `example.com` (auto Let's Encrypt), join it to your **tailnet**, and have
Caddy reverse-proxy to the home container over Tailscale:

```
example.com { reverse_proxy http://<home-machine-tailscale-name>:8000 }
```

Costs ~$5/mo, exposes nothing at home, and no third party sees decrypted traffic.
Set `KITH_BASE_URL=https://example.com` and the same OAuth redirect as above.

---

## 8. Later: leaving Testing mode

To drop the "unverified app" screen, the 100-test-user cap, and the 7-day token
expiry, submit the app for **OAuth verification**. Because `gmail.send` is a
*sensitive* scope, this is brand verification only (no paid security assessment):
you need the verified domain, a public homepage + privacy policy on it (G6), a
short demo video, and a scope justification, then a review of days–weeks.
