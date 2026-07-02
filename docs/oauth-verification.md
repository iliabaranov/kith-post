# Leaving Testing mode: Google OAuth verification

Kith Post runs in Google **Testing** mode (invite-only, ≤100 test users, no
verification). That's fine for a trusted circle — the only costs are the
"unverified app" screen and ~7-day refresh-token expiry.

To remove those, submit for **verification**.

## The good news: sensitive, not restricted (no paid CASA)

Kith Post requests exactly one non-basic scope: **`gmail.send`**. Per Google's
[Gmail API scopes reference](https://developers.google.com/workspace/gmail/api/auth/scopes),
`gmail.send` is a **sensitive** scope, *not* a **restricted** one (the restricted
Gmail scopes are `mail.google.com`, `gmail.readonly`, `gmail.compose`,
`gmail.insert`, `gmail.modify`, `gmail.metadata`, `gmail.settings.*`).

That means this is **sensitive-scope / brand verification** — a **free** Google
review, **no third-party CASA security assessment** (CASA applies only to
restricted scopes and costs ~$540–$1,000/yr as of 2026). Review typically takes
a few days to ~2 weeks.

The other three scopes we request — `openid`, `.../userinfo.email`,
`.../userinfo.profile` — are **basic** scopes used only for sign-in and don't
themselves trigger verification.

## Pre-flight status (already done ✓)

Verified live on `kithpo.st`:

- ✅ **Public homepage** at `/` — describes the app, links Privacy + Terms in the footer.
- ✅ **Privacy policy** at `/privacy` — names the developer (Ilia Baranov), explains
  `gmail.send` ("cannot read, search, modify, or delete"), lists retention +
  one-click export/delete, gives a contact, and contains the required **Limited
  Use** sentence: *"Kith Post's use and transfer of information received from
  Google APIs adheres to the Google API Services User Data Policy, including the
  Limited Use requirements."*
- ✅ **Terms** at `/terms`.
- ✅ **HTTPS** on a real domain (Cloudflare).

## Checklist (what's left — all in the Google Cloud Console)

- [ ] **Verify the domain** `kithpo.st` in
  [Google Search Console](https://search.google.com/search-console) as Owner
  (DNS TXT record via Cloudflare, or the Cloudflare integration).
- [ ] **OAuth consent screen → Branding**: App name = **Kith Post**, **User
  support email** set, **Developer contact email** set, **App home page** =
  `https://kithpo.st`, **Privacy policy** = `https://kithpo.st/privacy`, **Terms**
  = `https://kithpo.st/terms`, **Authorized domain** = `kithpo.st`. An app
  logo is optional but helps (a square PNG; `design/favicon.svg` rasterized works).
- [ ] **Publishing status → "In production"** (this is also what removes the
  7-day refresh-token expiry, independent of verification).
- [ ] **Record the demo video** (script below) and upload as **unlisted YouTube**.
- [ ] **Paste the scope justification** (below) into the verification form.
- [ ] **Submit** in the OAuth Verification Center and reply promptly to any
  reviewer follow-ups (they email the developer-contact address).

## Scope justification (ready to paste)

> Kith Post lets a signed-in user send personal event invitations and holiday
> cards **from their own Gmail account**, and follow up with reminders. It uses
> `gmail.send` solely to send these user-composed messages on the user's behalf.
> `gmail.send` is the narrowest scope that supports this: it is send-only and
> grants no read, search, modify, or delete access to the mailbox. Broader Gmail
> scopes (`gmail.compose`, `gmail.modify`, or full `mail.google.com`) are
> unnecessary and would over-request access we neither need nor want.
> `openid`/`email`/`profile` are used only to authenticate the user and identify
> their account; we never read mailbox contents. Data handling, retention, and
> the Limited Use commitment are described at https://kithpo.st/privacy.

---

## Demo video — turnkey recording script

Google's reviewers need the video to prove three things: it's **your** OAuth
client, the **consent screen** shows the requested scopes, and each scope is
**actually used** as described. Follow this literally.

### Hard requirements (do not skip)

- **Language:** narrate in **English** (or add English captions).
- **Show the OAuth client ID:** during the Google consent step, the browser
  **address bar must be visible** — the `client_id=...` in the URL is how Google
  ties the video to your project. Do **not** crop or blur it.
- **Show the app name** on the consent screen (it must read *Kith Post*).
- Record at **≥720p**, whole browser window visible, no fast cuts.
- The "unverified app" / "Google hasn't verified this app" screen **will** appear
  (you're pre-verification) — that's expected; just click **Advanced → Continue**
  on camera. Reviewers know.
- Use a real Google account you control that is currently a **test user** on the
  project (add it under Audience → Test users first).

### Setup before hitting record

1. In a clean browser profile, be **signed out** of Kith Post.
2. Have a card image ready to upload and a recipient address you own (your own
   inbox is perfect — you'll show the sent mail arriving).
3. Open your **Gmail Sent** folder in a second tab (you'll switch to it to prove
   the send).

### Shot list with narration (~3 minutes)

| # | On screen | Say (narration) |
|---|-----------|-----------------|
| 1 | `https://kithpo.st` homepage | "This is Kith Post, a self-hosted app that sends personal invitations and holiday cards from the user's own Gmail. I'll show how it uses the Google scopes it requests." |
| 2 | Click **Sign in with Google** | "The user signs in with Google." |
| 3 | Google account chooser → pick account | "I'm choosing my Google account." |
| 4 | **Unverified-app screen** → Advanced → Continue | "This is the pre-verification warning; I'll continue." |
| 5 | **Consent screen — keep the address bar visible** | "Here is the consent screen for the app 'Kith Post'. Notice the URL contains our OAuth client ID. The app requests sign-in (openid, email, profile) and one sensitive scope, 'Send email on your behalf' — gmail.send." |
| 6 | Click **Continue / Allow** → land on dashboard | "I grant access and land in the app, signed in — that's the openid/email/profile scopes: only to identify my account." |
| 7 | Click **Create a card**; set a title, upload the image, type a message, set a date, tick RSVP | "I compose a card: a title, an image, a short message, a date, and RSVP options." |
| 8 | Add a recipient (your own email), click **Send** | "I add a recipient and send. This is the only use of gmail.send — sending the message I just composed, from my own Gmail." |
| 9 | Switch to the **Gmail Sent** tab; open the sent message | "Here it is in my Gmail Sent folder — sent from my account. The app has no read access; it cannot open, search, or delete anything in my mailbox." |
| 10 | Open the invitation link → click an RSVP button | "The recipient opens the invitation and RSVPs — no account or tracking pixel for them." |
| 11 | Go to **/account** → point at **Export** and **Delete** | "Finally, the account page lets a user export all their data or permanently delete it, matching our privacy policy." |
| 12 | Show `kithpo.st/privacy` briefly | "Our privacy policy documents this and includes the Google API Limited Use commitment." |

### After recording

- Upload to YouTube as **Unlisted**, title it e.g. *"Kith Post — Google OAuth
  scope usage demo"*, paste the link into the verification form.
- Keep the video's behavior **in sync with the privacy policy** — reviewers
  cross-check. If you change scopes later, re-verify.

## Notes / common rejection reasons to avoid

- Privacy-policy URL on the consent screen **must exactly match** the live
  `https://kithpo.st/privacy` and be on the **verified** domain.
- App name in the video, on the consent screen, and in the console must all be
  identical.
- Don't request scopes you don't use — we only declare `gmail.send` + the three
  basic sign-in scopes.
- "In production" publishing status is separate from verification but is what
  actually stops the 7-day refresh-token expiry; set it either way.
