# Leaving Testing mode: Google OAuth verification

Kith Post runs in Google **Testing** mode (invite-only, ≤100 test users, no
verification). That's fine for a trusted circle — the only costs are the
"unverified app" screen and ~7-day refresh-token expiry.

To remove those, submit for **verification**. Kith Post requests only
`gmail.send`, which is a **sensitive** (not *restricted*) scope, so this is
**brand verification** — no paid CASA security assessment. Review typically takes
up to ~10 days.

## Checklist

- [ ] **Public homepage** on the verified domain, describing the app and linking
  to the privacy policy — the landing page at `/` (links Privacy + Terms in the footer).
- [ ] **Privacy policy** at `/privacy` (same domain), also set as the Privacy Policy
  URL on the OAuth consent screen.
- [ ] **Terms** at `/terms` (optional but present).
- [ ] **Verify the domain** in [Google Search Console](https://search.google.com/search-console)
  (Owner/Editor) — DNS TXT or the Cloudflare integration.
- [ ] **OAuth consent screen**: correct app name, **User support email**, and
  **Developer contact** set.
- [ ] **Demo video** (unlisted YouTube) — see script below.
- [ ] **Scope justification** — paste the text below.
- [ ] Submit in the **OAuth Verification Center** (Cloud Console), declaring the
  `gmail.send`, `openid`, `email`, `profile` scopes.

## Scope justification (ready to paste)

> Kith Post lets a signed-in user send personal event invitations and holiday
> cards **from their own Gmail account**, and follow up with reminders. It uses
> `gmail.send` solely to send these user-composed messages on the user's behalf.
> `gmail.send` is the narrowest scope that supports this: it is send-only and
> grants no read, search, modify, or delete access to the mailbox. Broader Gmail
> scopes (e.g. `gmail.compose`, `gmail.modify`, or full access) are unnecessary and
> would over-request access we neither need nor want. `openid`/`email`/`profile`
> are used only to authenticate the user and identify their account. We never read
> mailbox contents. Data handling is described at https://<your-domain>/privacy.

## Demo video script (~2 minutes, English, unlisted YouTube)

1. Show the browser **address bar with the OAuth client ID** visible during the
   consent flow.
2. Start at the public homepage → click **Sign in with Google**.
3. Show the **consent screen** with the correct **app name** and the requested
   scopes, and complete sign-in.
4. In the app: create a card, add a recipient, and **send** — demonstrating what
   `gmail.send` does (an email sent from the user's Gmail). Show the sent message.
5. Briefly show the invitation page + RSVP to round out the functionality.

## Notes

- The privacy policy URL on the consent screen **must match** the live `/privacy`
  URL and be on the same verified domain.
- Keep actual data behavior in sync with the privacy policy (Limited Use).
