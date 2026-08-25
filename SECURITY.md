# Security Policy

Kith Post handles people's names, email addresses, phone numbers, and Google
refresh tokens, plus a link to a WhatsApp session where that channel is enabled
(the WhatsApp credentials themselves live in the WAHA container's own volume, never
in this app's database), so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue, the impact, and steps to reproduce.

This keeps the report private until a fix is available. You'll get an
acknowledgement, and we'll coordinate a fix and disclosure timeline with you.

## Scope

In scope: anything that could expose another user's data, bypass the invite-only
access control, leak or forge invitation tokens, break the encryption of stored
PII/tokens, or allow sending mail as another user. Where the WhatsApp channel is
enabled, that also covers forging or replaying a `POST /wa/webhook` (each is signed
with an HMAC-SHA512 over the exact body) and anything that could leak
`KITH_WAHA_API_KEY` — which is why the pairing QR is proxied server-side rather
than fetched by the browser.

Out of scope: findings that require access to the host's server, filesystem, or
`.env` (the deployment is single-tenant and self-hosted — whoever runs it is
trusted); rate-limit tuning; and best-practice suggestions without a concrete
exploit (open those as normal issues).

## Good to know

- PII and Google refresh tokens are stored Fernet-encrypted; lookups use a keyed
  HMAC blind index, so plaintext is never queried.
- Instances are self-hosted. If you run your own, keep your `FERNET_KEY` and
  `SECRET_KEY` secret and backed up — losing the Fernet key makes stored data
  unrecoverable.
