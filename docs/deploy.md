# Deploying Kith Post (home server + Cloudflare Tunnel)

The goal: run the container on your home server and expose it at a real domain
over HTTPS, **without opening any router ports**. Cloudflare Tunnel gives free
auto-TLS and works behind CGNAT. (A privacy-maximising VPS+Caddy alternative is
sketched at the end.)

Everything is one image; moving from laptop to server is a `.env` change, not a
code change.

## Pre-flight checklist

Work top to bottom; each item maps to a section below.

- [ ] `kithpo.st` is **Active** on Cloudflare (registrar nameservers switched) — §1
- [ ] Home server has **Docker + Docker Compose** and can `git clone` the repo — §1
- [ ] `.env` created from `.env.example` with real `KITH_SECRET_KEY` + `KITH_FERNET_KEY` (backed up) — §2
- [ ] `KITH_BASE_URL=https://kithpo.st`, `KITH_SEND_MODE=self-only` to start — §2
- [ ] Cloudflare tunnel created; token in `CLOUDFLARE_TUNNEL_TOKEN`; public hostname `kithpo.st → http://kith:8000` — §3
- [ ] Google OAuth: `https://kithpo.st/auth/callback` added as redirect URI; `kithpo.st` an authorized domain — §4
- [ ] `docker compose --profile public up -d --build`; sign in; self-only test send round-trips — §5
- [ ] Flip `KITH_SEND_MODE=live` and re-up — §5

---

## 0. Why the public URL matters

Recipients are on the open internet and are **not** on your tailnet. The emailed
"View invitation" links and the OAuth redirect are both built from
`KITH_BASE_URL`. So the single most important config on the server is:

```
KITH_BASE_URL=https://kithpo.st
```

If that's wrong, guests get dead links.

---

## 1. Prerequisites

- **Server**: Docker + Docker Compose. `git` to pull the repo.
- **Domain**: `kithpo.st` added to a (free) Cloudflare account — change the
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
KITH_BASE_URL=https://kithpo.st
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
   - Subdomain: *(blank)* · Domain: `kithpo.st`
   - Service: **HTTP** → `kith:8000`
     (`kith` is the compose service name; the connector shares its network.)
3. Save. Cloudflare provisions the TLS cert and the `kithpo.st` DNS record
   automatically.

The app is bound to `127.0.0.1:8000` on the host and reached only through this
tunnel — nothing is exposed to your LAN or a public IP.

---

## 4. Google OAuth wiring

In Google Cloud Console → **APIs & Services → Credentials →** your OAuth client:

- **Authorized redirect URIs**: add `https://kithpo.st/auth/callback`
  (keep `http://localhost:8000/auth/callback` for local dev).
- **OAuth consent screen → Audience**: keep yourself (and any trusted circle) as
  **Test users** — the app stays in Testing mode, no verification needed.
- Consent screen → **Authorized domains**: add `kithpo.st`.

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

Then open `https://kithpo.st`, sign in with Google (accept the "unverified app"
warning), and:

1. With `KITH_SEND_MODE=self-only`, create a card and send — the email lands in
   **your** inbox. Click the emailed link; it now opens `https://kithpo.st/i/...`
   (a real public URL), so RSVP works end to end.
2. Happy? Set `KITH_SEND_MODE=live` in `.env` and `docker compose up -d` to
   apply. Now sends go to actual recipients.

---

## 6. Backups & updates

- **Back up `data/`** — it holds the SQLite DB, uploaded images, and (if you
  didn't set `KITH_FERNET_KEY`) the dev key. A periodic copy of `data/` + your
  `KITH_FERNET_KEY` is a full backup.
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
serving `kithpo.st` (auto Let's Encrypt), join it to your **tailnet**, and have
Caddy reverse-proxy to the home container over Tailscale:

```
kithpo.st { reverse_proxy http://<home-machine-tailscale-name>:8000 }
```

Costs ~$5/mo, exposes nothing at home, and no third party sees decrypted traffic.
Set `KITH_BASE_URL=https://kithpo.st` and the same OAuth redirect as above.

---

## 8. Later: leaving Testing mode

To drop the "unverified app" screen, the 100-test-user cap, and the 7-day token
expiry, submit the app for **OAuth verification**. Because `gmail.send` is a
*sensitive* scope, this is brand verification only (no paid security assessment):
you need the verified domain, a public homepage + privacy policy on it (G6), a
short demo video, and a scope justification, then a review of days–weeks.
