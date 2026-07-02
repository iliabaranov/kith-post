# Security Policy

Kith Post handles people's names, email addresses, and Google refresh tokens, so
security reports are taken seriously.

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
PII/tokens, or allow sending mail as another user.

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
