# The SMS channel (optional)

A third way to send an invitation, alongside email and WhatsApp: a plain text
message carrying the same per-recipient `/i/{token}` link. Off by default.

Two things are worth knowing before you read any further.

**A text carries no picture.** Email sends the card as an image and WhatsApp
sends it as a photo; SMS sends words and a link, and the card lives on the page
the link opens. MMS is a different product at a different price and this doesn't
use it.

**Two ways to turn this on: from your account, or from `.env`.** Like
WhatsApp, a host can link their own texting at `/account/sms` — their own
phone, or their own Twilio account — and once that's complete their invitations
go out from their own number. Unlike WhatsApp, there's also a site-wide
default: the operator can configure one provider in `.env` for hosts who
haven't set up their own, which is how this channel worked before the account
page existed and still works today for anyone who doesn't want to bother. A
host's own setup, once complete, always wins over the site's. §1 below is the
host's side of that; §2 is the operator's.

Tracking is unchanged. Opened, RSVP, headcount and allergies all come from the
invitation page, which doesn't care which channel brought someone to it, or
whose number sent it. Nothing is added to the message — no parameter, no
redirect, and deliberately no shortener, since hiding where a link goes is the
single thing that most makes a text look like a scam.

---

## 1. Set it up from your account page

The quickest way in, and the one most hosts will want: sign in, open
**your account → Texting**, and choose how you'd like to send. If the operator
already provides texting site-wide, your cards can offer a text box already —
this page is how you move your own cards onto your own number instead, and
it's entirely optional.

### My own phone

Install the free, open-source
[SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway) app
on a phone with a working SIM, turn on its **Local Server**, and copy three
things off that same screen into the form: the address (just
`http://<the phone's IP>:8080` — no path after it), and the username and
password the app generated. Adding **the phone's own number** is optional, but
it's what lets a STOP reply that arrives without a country code be matched
back to the right sender.

Running the project's self-hosted relay instead of talking to the phone
directly? Tick the box under "Using the project's relay server instead of the
phone" and add the relay's device ID if it fronts more than one phone. §2.2
below has the full shape of the relay, in the operator's terms — it applies to
you too, just pointed at your own phone instead of the site's.

### Twilio

Paste in the **Account SID** and **Auth Token** from the Twilio console's home
page, then either a number you've bought there or a **Messaging Service SID**
if you've registered a campaign (it picks the number itself). US guests still
need **A2P 10DLC** registration first, which is a days-long process, not a
minutes-long one — §2.3 below walks through why and what it costs. A trial
Twilio account can only text numbers you've verified in the console, which is
enough to send yourself the test message below while registration is pending.

### Either way

Add **your own mobile** at the bottom of the form. That's where "Send me a
test text" goes, whatever route you've chosen, and it's also where every text
goes while the site is running in self-only mode. Send yourself one before a
real card depends on it — the page keeps the last success or error and shows
it back to you at the top, so you don't have to guess whether it's working.

**Secrets are stored encrypted and never shown again.** A password or auth
token field reads blank on the page after you save, on purpose — not because
nothing is stored, but because a secret you can read back is a secret that
leaked somewhere. Leaving it blank on a later save keeps what's already
stored; only fill it in when you're actually changing it.

### Deliveries and STOP, on your own phone

The page shows a signing key. Copy it into the app's **Settings → Webhooks →
Signing key**, then press **Register webhooks on the phone** while the phone is
on its own network. That's what tells the app where to POST delivery reports
and any STOP reply. The address it registers is the site's public one, not a
LAN address, and that's deliberate: your phone lives on your own network,
while this app most likely answers behind a tunnel, so the container's LAN
address isn't somewhere your phone can reach at all — only the public one is.

### Deliveries and STOP, on Twilio

In the Twilio console, open your number's **Messaging configuration** and set
"A message comes in" to the URL the page shows you. It's the same URL for
every host on this site — Twilio tells hosts apart by the Account SID it posts
along with everything else, so there's nothing host-specific to type beyond
that one setting. Delivery receipts need no console setup at all: every text
sent through your account registers its own status callback automatically.

Either route, receipts only ever land on your own recipients. Your phone or
your Twilio account can stamp a delivery or a STOP onto your own cards, never
onto another host's, even on the same site.

If your account has no Texting page at all, the operator has turned per-host
setup off (`KITH_SMS_HOST_LINKS_ENABLED=false`) — skip ahead to §2, or ask them
to turn it back on.

---

## 2. Site-wide default (operator, `.env`)

This is how the channel worked before the account page existed, and for a host
who never visits it, it still works exactly the same way: pick one provider in
`.env`, and it applies to every host who hasn't set up their own.

**Precedence, stated once, since it matters everywhere below.** A host's own
row, if it's complete, always wins over this section — their texts go from
their own number regardless of what's set here. A host who has started their
own setup but left something blank does **not** fall back to this section:
their texts are held instead, with the card saying why, because a host
partway through setting up their own number would be more surprised to find
invitations quietly leaving from the operator's than to see them paused. A
host with no row at all — the common case, and the only case before this page
existed — uses exactly what's configured below, unchanged.

```
KITH_SMS_HOST_LINKS_ENABLED=true   # default; set to false to remove the account page
```

With it `false`, `/account/sms` redirects to `/account`, no host can set up
their own, and this section is the only way texting works on the site.

### 2.1 Choosing a provider

Two, and they are not close in character.

#### The Android gateway — recommended for occasional, small lists

[capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway)
is an open-source app that turns a phone you already own into an SMS API. It
sends from your own SIM, so:

- there is **no per-message cost** and nothing to rent;
- there is **no registration** — no brand, no campaign, no waiting;
- the number your guests see is a number that belongs to you.

What it costs instead is a phone kept powered and reachable from the container,
and one real risk: **a consumer number sending a lot of texts in a short window
is what carriers throttle and spam-filter.** This is the same shape of caveat
the WhatsApp channel carries, and the same answer applies — keep lists small,
which is what this app is for. Sends are already paced a random 1–4 seconds
apart to avoid looking like a burst.

#### Twilio — the reliable backup

A real carrier relationship, global reach, and delivery receipts that mean
something. Terms-clean, and nothing to keep charged. It costs money, and
—counter-intuitively— **not really per message**:

| What | Roughly |
| --- | --- |
| Per SMS segment (US) | fractions of a cent |
| A 30-guest event, with reminders | under $2 of variable cost |
| Phone number rental | ~$1.15 / month, whether you send or not |
| A2P 10DLC brand + campaign registration | a one-off brand fee, plus a small monthly campaign fee |

So at this app's volume the recurring fees dominate and the messages are noise.
Confirm all of it against
[Twilio's current pricing](https://www.twilio.com/en-us/sms/pricing/us) rather
than trusting this table — these are the numbers at the time of writing.

Use Twilio when the gateway phone isn't practical: no spare handset, no reliable
power, or a guest list big enough that carrier filtering is a real worry.

### 2.2 Setting up the Android gateway

1. Install **SMS Gateway for Android** on a phone with a working SIM, from the
   project's releases or F-Droid.
2. In the app, turn on **Local Server**. It listens on port `8080` and shows a
   username and password — the app generates them, you don't choose them.
3. Note the phone's LAN IP. Give it a DHCP reservation or a static lease; if the
   address moves, sends start failing with a connection timeout.
4. Keep the phone plugged in, and turn off any battery optimisation for the app.
   A phone that goes to sleep is a channel that goes down.
5. In `.env`:

   ```
   KITH_SMS_ENABLED=true
   KITH_SMS_PROVIDER=gateway
   KITH_SMS_GATEWAY_URL=http://192.168.1.50:8080
   KITH_SMS_GATEWAY_USER=<the username the app shows>
   KITH_SMS_GATEWAY_PASS=<the password the app shows>
   KITH_SMS_WEBHOOK_SECRET=<a long random string>
   ```

   The last line is not optional in practice: it is what lets STOP replies
   reach the app at all (see "Receipts and STOP" below, and §4). Without it the
   channel sends, and never hears back.

6. Restart the app. The SMS box should appear on the compose form, and
   `/account` should say "Text messages: on".

There is **no compose service to add** — the transport is a handset. If instead
you run the project's self-hosted relay (for a phone that isn't directly
reachable from the container), point `KITH_SMS_GATEWAY_URL` at the relay and set
the other path:

```
KITH_SMS_GATEWAY_PATH=/3rdparty/v1/messages
KITH_SMS_GATEWAY_DEVICE_ID=<the device id from the relay>   # only if it fronts several phones
```

The on-device Local Server answers at `/message` (the default); the relay and
cloud server answer the same call at `/3rdparty/v1/messages`. Getting this wrong
is a 404, and the error message will say so and name both — and stop the batch
after one attempt, since every guest would hit the same wall.

**Never put the gateway behind the public tunnel.** Not being reachable from the
internet is the whole of its security model.

#### Receipts and STOP, on the gateway

1. Set a signing key in the app: **Settings → Webhooks → Signing Key**.
2. Put the same value in `.env` as `KITH_SMS_WEBHOOK_SECRET`.
3. Register a webhook in the app pointing at `…/sms/webhook/gateway`, for the
   `sms:received`, `sms:delivered`, `sms:failed` and `sms:cancelled` events. The
   URL has to be one the phone can reach — and that means the site's **public**
   address, not the container's LAN one: the phone is on your own network, the
   app most likely answers behind a tunnel, and a LAN address is only reachable
   from inside that tunnel, not from the phone sitting outside it. (An earlier
   version of this doc said the LAN address was "better." That was wrong for a
   tunnelled deployment, which is the deployment this project is built for —
   see [`docs/deploy.md`](./deploy.md).)
4. Make sure the phone's clock is roughly right. Signed POSTs more than five
   minutes old are refused, which is what stops a captured receipt being replayed.

### 2.3 Setting up Twilio, for occasional use

Written for the case this app is actually for: a handful of sends a year, and a
number you don't want to rent between them.

#### Register once

1. Create a Twilio account and complete **A2P 10DLC brand registration**. This
   is a one-off: the brand persists at the *account* level, so you do not
   re-register it for each event.
2. Register a **low-volume standard campaign** and attach it to a messaging
   service. This is the part with a small recurring fee.

The registration is the slow step — allow days, not minutes. Do it well before
the event you want it for.

#### Provision only when you need it

3. Buy a local number in the month you plan to send, and **release it
   afterwards** to stop the monthly rental.
4. Leave the *campaign* in place between uses unless you have stopped using the
   channel altogether. Re-registering takes time, and the campaign fee is small
   next to the delay of doing it again in a hurry.

#### Configure

```
KITH_SMS_ENABLED=true
KITH_SMS_PROVIDER=twilio
KITH_SMS_TWILIO_ACCOUNT_SID=AC...
KITH_SMS_TWILIO_AUTH_TOKEN=...
KITH_SMS_TWILIO_FROM=+15551234567
KITH_SMS_WEBHOOK_SECRET=<a long random string>
```

or, if you are sending through a messaging service (which is what 10DLC campaign
registration attaches to, so this is the usual case):

```
KITH_SMS_TWILIO_MESSAGING_SERVICE_SID=MG...
```

If both are set, the messaging service wins — it is the more specific
instruction, and it picks the number itself.

#### Receipts and STOP, on Twilio

`KITH_SMS_WEBHOOK_SECRET` (any long random string) is what turns the Twilio
endpoint on. Twilio's own callbacks are verified with your auth token rather
than with this secret, but nothing is recorded — and no STOP is heard — until it
is set. The gateway's endpoint stays a 404 on a Twilio box, and vice versa.

Once it is set, every send registers a `StatusCallback` automatically. Two things
to know:

- **The callback comes from Twilio's servers, so it must be publicly
  reachable.** It uses `KITH_BASE_URL`, unlike the WhatsApp webhook, which is
  compose-internal. If your `KITH_BASE_URL` isn't reachable from the internet,
  receipts won't arrive.
- To receive **STOP** replies, point your number's (or messaging service's)
  inbound-message webhook at the same URL: `…/sms/webhook/twilio`.

---

## 3. Testing safely

The same three modes as every other channel, and they behave the way you'd hope
— whichever layer, §1 or §2, is actually sending:

| `KITH_SEND_MODE` | What happens |
| --- | --- |
| `dry-run` (default) | Each text is written to `data/outbox/<event>/sms/<recipient>.txt`, with the destination number and the **segment count**. Nothing is sent and no provider is called. |
| `self-only` | Sends every text to the sender's own number — a host's `self_number` if their own setup is in use, otherwise `KITH_SMS_SELF_NUMBER`. With that unset the texts are **held** — the guests stay queued and the card says why — rather than written anywhere or sent to anyone. |
| `live` | Sends to the actual recipients. |

Read the outbox before you go live. The segment count is the thing you can't
judge by eye, and it's the one number you can still act on: a long title or note
tips a message into a second segment, which doubles what it costs and, on some
handsets, arrives as two.

The compose page shows the same preview and count before you send, so you can
shorten a title while it's still worth shortening.

---

## 4. Consent, and STOP

**You need consent to text the people you are texting.** This is not different in
kind from what the terms already say about email, but it is enforced more
aggressively by carriers and regulators, and the penalty lands on you rather than
on this software. Text people who have given you their number for this purpose.
That is the whole of the rule, and at this app's scale — people you know, invited
to something you're hosting — it isn't a hard one to keep.

STOP is handled for you **once the webhook is set up** — the signing key and
"Register webhooks on the phone" button on your own account page (§1) if
you've set up your own texting, or `KITH_SMS_WEBHOOK_SECRET` plus the
provider's inbound-message webhook pointed at this site (§2.2 or §2.3) for the
site default. Until then no reply of any kind reaches the app for that layer,
so treat that step as part of turning the channel on, not as an extra. (The
app logs a warning at startup while the site-wide channel is configured and
its secret is not.) Once it is on:

- A reply of **STOP**, **STOPALL**, **UNSUBSCRIBE**, **CANCEL**, **END** or
  **QUIT** opts that number out **permanently, across every card anyone on this
  site ever sends** — not just the one they were invited to, and not just from
  whichever host's number they replied to. The record is kept as a hash of the
  number in a table of its own, so it outlives the card, the address-book entry
  and even the host's account; deleting any of those does not forget it.
- **START** or **UNSTOP** undoes it, so a number that opted out by accident has a
  way back. A bare "yes" does not — that is a reply to an invitation, not a
  re-subscription.
- It is enforced on first sends *and* on reminders, and in `dry-run` too, so the
  outbox shows you the same set of texts a live send would produce.
- The opt-out has to be a message that says only that. Someone writing "stop by
  any time!" is making conversation, and is not unsubscribed.

Anything else a guest texts back is **not** stored, not forwarded, and not shown
in the dashboard. A reply to an invitation belongs in the conversation you're
already having with them.

---

## 5. What the host sees

- **The account page's status line.** `/account/sms` says plainly which of
  three states you're in: your own setup is complete and in use ("Texting is
  set up through your phone/Twilio account…"), the site's default is in use
  because you haven't set up your own, or neither is configured yet — plus,
  once you've set up your own, whether the last test text went through and
  when, or what went wrong.
- **A `· SMS` badge** next to each recipient reached by text.
- **"Delivered by text"** once the carrier confirms it, if you've enabled
  receipts. There is no read receipt for SMS at all, so that is the only delivery
  fact on offer.
- **"The carrier couldn't deliver the text"** when the provider reports a
  failure — the only signal you get that a number is bad.
- **"Replied STOP — won't be texted"** on a guest who opted out. They stay
  "queued" for ever, because neither "sent" nor "failed" would be true.
- **A held card, with a reason, for a host mid-setup.** If your own account
  row is started but incomplete, cards don't fall back to the site's provider
  — they wait, and the card says "set up texting from your account page"
  rather than something more confusing about the operator's settings.
- **"Opened"** still means, and only means, that a person loaded the invitation
  page. A delivery receipt is never counted as one — same rule as WhatsApp.

## 6. When it goes wrong

| Symptom | Likely cause |
| --- | --- |
| No SMS box on the compose form | Neither your own setup nor the site's default is fully configured for you. A named provider with missing credentials counts as not configured, on purpose. |
| Card stuck saying "set up texting from your account page" | Your own row exists but is missing a field — finish it at `/account/sms`, or remove it there to fall back to the site's default. |
| `gateway 404 … check KITH_SMS_GATEWAY_PATH` | On-device path against a relay, or the reverse. See §2.2 (or the relay note in §1 for your own phone). |
| Connection timeout to the gateway | Phone asleep, off its network, or its IP moved. |
| `Twilio 400: 21211 …` | Not a valid number — usually a missing country code. Numbers must be E.164. Costs that one recipient. |
| `Twilio 400: 21610 …` | That number opted out at Twilio's end, but this app has no record of it — the STOP arrived before the webhook was set up. It will fail the same way on every card until the guest texts STOP again with the webhook on. |
| `Twilio 400: 21606 …` / a bare 404 | The From number isn't yours, or the account SID is wrong. Stops the whole batch after one call; the card says "rejected this site's setup" or points at your own account page if it's your own credentials. |
| `Twilio 429 …` / gateway `429` | The provider asked us to slow down. The batch stops; press Send again in a few minutes. |
| Recipients stuck on "queued" after a live send | The card itself says why (bad credentials, a setup problem, no test number, slowed down). Each of those stops the whole batch on purpose and leaves everyone queued; a single bad number costs only that recipient. |
| STOP replies do nothing | For your own setup: the signing key isn't entered in the app, or "Register webhooks on the phone" hasn't been pressed since the last reinstall. For the site default: `KITH_SMS_WEBHOOK_SECRET` is unset, or the provider's *inbound* webhook (not just the status callback) isn't pointed at `…/sms/webhook/<provider>`. The app warns about the site-wide case at startup. |
| Receipts never arrive (Twilio) | `KITH_BASE_URL` isn't publicly reachable, or the webhook secret is unset (yours, on your account page, or the site's, in `.env`). |
| Receipts never arrive (gateway) | Webhook not registered in the app, signing key mismatch, or the phone's clock is more than five minutes out. |
