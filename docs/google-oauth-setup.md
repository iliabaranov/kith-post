# Google OAuth setup (G1)

Kith Post signs people in with Google and sends mail from their own Gmail. Until
you do this, `/auth/login` falls back to a local **dev sign-in** so the app is
testable without Google.

## 1. Create the OAuth client

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → create a
   project (e.g. "Kith Post").
2. **APIs & Services → Enable APIs** → enable the **Gmail API**.
3. **OAuth consent screen**:
   - User type: **External**.
   - Publishing status: leave in **Testing**.
   - **Scopes**: add `.../auth/userinfo.email`, `.../auth/userinfo.profile`,
     `openid`, and `https://www.googleapis.com/auth/gmail.send`.
   - **Test users**: add the Google accounts allowed to use it (≤ 100). *Only these
     accounts can sign in while in Testing — this is the whitelist that keeps us
     out of Google's verification process.*
4. **Credentials → Create credentials → OAuth client ID → Web application**.
   - **Authorized redirect URIs** — add **both** so the same app works locally and
     on the server:
     - `http://localhost:8000/auth/callback`
     - `https://<machine>.<tailnet>.ts.net/auth/callback`

## 2. Configure Kith Post

Put the client id/secret and a Fernet key in `.env`:

```bash
KITH_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
KITH_GOOGLE_CLIENT_SECRET=xxxx
KITH_FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
KITH_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
# locally: KITH_BASE_URL=http://localhost:8000   (selects which redirect URI is used)
```

Once `KITH_GOOGLE_CLIENT_ID` + `KITH_GOOGLE_CLIENT_SECRET` are set, the dev
sign-in disappears and `/auth/login` redirects to the real Google consent screen.

## 3. What test users will see

Because the app stays **unverified** in Testing, a whitelisted user hits a
"Google hasn't verified this app" screen → **Advanced → Go to Kith Post
(unsafe)** → grant. That's expected for a small trusted circle. Document it for
your people so they aren't alarmed.

## Notes

- We request `gmail.send` only — Kith Post can send on the user's behalf but can
  **never read** their mailbox.
- The refresh token is stored **encrypted at rest** (Fernet). Losing
  `KITH_FERNET_KEY` means stored tokens/PII can't be decrypted — back it up.
- Testing-mode refresh tokens can expire after ~7 days in some setups; the app
  will re-prompt consent if a token goes invalid (handled from G3).
