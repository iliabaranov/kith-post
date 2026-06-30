# Kith Invite — Design Language

> Direction: **"Kitchen Table"** · Theme: **light & warm** · Locked 2026-06-29.
> This is the single source of truth for Kith Invite's look. Every screen derives its
> colors, type, spacing, and motion from here. See `design/invite-preview.html`
> for the reference implementation of the hero screen.

---

## 1. The idea

Kith Invite should feel like **a warm note from a friend left on the kitchen table** —
not a SaaS product. The emotional beat we're designing for is the small, happy
ritual of *getting real mail you actually want*: a handmade card, a personal line,
a stamp pressed onto it. Friendly, unhurried, a little hand-made. Never "techy."

We draw from the **physical world of invitations and the post** — paper stock,
washi tape, rubber stamps, RSVP cards, a handwritten sign-off — because that's
where the warmth and the distinctive details live. We use those materials with
restraint, not as full skeuomorphism.

## 2. Self-critique (why this isn't the generic "AI warm" look)

The obvious trap for a "friendly, warm, paper" brief is the current AI-design
default: **cream `#F4F1EA` background + a high-contrast editorial serif (Playfair)
+ a terracotta accent.** We deliberately broke that pattern on every axis:

- **Background** is a warm *ecru* with a real, subtle **paper grain** (SVG
  turbulence overlay), not a flat cream fill — it reads as stock, not a swatch.
- **Display face** is **Spectral** (a warm, classic book serif) in a deliberate
  4-role system — *not* Inter-everywhere, and *not* the lazy "Inter body + one
  italic-serif accent word" pattern. We rejected Fraunces specifically because its
  `f` has become an AI tell.
- **Accent is honey + plum, not terracotta**, with a single **berry** reserved
  for one celebratory moment. Text ink is a deep **aubergine-brown**, never pure
  black.
- **The memorable element is a rubber-stamp RSVP mark** drawn from Kith Invite's actual
  subject (the post), not a generic gradient/number hero.

If a future change drifts toward flat-cream + Playfair + terracotta, it has
regressed to the default — pull it back.

### 2.1 Anti-slop checklist (informed by community "AI-slop" / "Claude-built app" critiques)

Concrete tells to keep designing *against* — verify every new screen:

- **No Inter** (and no Space Grotesk / Geist / Instrument-Serif-accent combo). We
  use Spectral + Hanken Grotesk + Caveat + Saira Condensed, on purpose.
- **No purple/violet "VibeCode" gradient, no cyan-on-dark, no gradient text/buttons,
  no gradient-stripe surface decoration.** Color is semantic and warm. (The only
  gradient in the mockup is the *placeholder* standing in for the user's photo.)
- **No one-sided colored accent border on cards** — cited as the *single most
  recognizable* AI-UI tell. Our card uses a uniform warm hairline (`--line`).
- **No colored glows / neon box-shadows.** Shadows are neutral, ink-based.
- **No badge/pill eyebrow above an oversized centered sans headline.** Our eyebrow
  is lowercase handwriting (Caveat), rotated; the title is left-aligned Spectral.
- **No icon-top feature-card grids, no `01/02/03` step rails, no stat banners, no
  emoji nav** — none of the SaaS-landing furniture.
- **Vary radius & spacing** (card 16 / button 12 / photo 10 / stamp 4 px) — not one
  uniform 16px radius and 24px pad on everything.
- **Motion is meaningful** (envelope open, stamp press) — no uniform fade-in-on-everything,
  no bouncing/wiggling; buttons ease, never snap.
- **Emoji: at most one, contextual** (e.g. a single 🎈 in a birthday confirmation) —
  never in headings, bullets, or nav.
- **Copy is specific and warm**, never hedging ("may help") or superlative
  ("best-in-class"). Real names, real details.
- **Contrast meets WCAG AA** even for `ink-soft` secondary text (no pale-grey-on-cream).

## 3. Color

Named tokens (use these names in code as CSS variables, e.g. `--paper`):

| Token | Hex | Role |
|-------|-----|------|
| `paper` | `#F4ECDD` | App background (warm ecru, + grain overlay) |
| `card` | `#FCF8EF` | Raised surfaces — the invitation card, panels |
| `ink` | `#3B2A33` | Primary text (deep aubergine-brown, the "near-black") |
| `ink-soft` | `#6E5C63` | Secondary text, captions, metadata |
| `honey` | `#E2972B` | Primary accent & main CTA ("I'll be there") |
| `plum` | `#6B3A57` | Deep secondary accent, headings flourish, links |
| `sage` | `#A9B7A0` | Calm surfaces, dividers, "neutral" status |
| `berry` | `#C44569` | **Reserved** for the one celebratory moment (the "Coming!" stamp). Used nowhere else. |
| `line` | `#E4D8C4` | Hairlines, card edges, input borders |

Rules: one warm family; `ink` (not black) for all text; `berry` is precious —
spend it only on the accepted-RSVP stamp. WCAG AA for all text on `paper`/`card`.

## 4. Typography

Three roles, a deliberate non-default pairing (all on Google Fonts):

- **Display — `Spectral`** (weight 600 for titles), set via the `--display` CSS
  var. A refined, warm classic book serif. ~~Fraunces~~ was **rejected** for being
  a tell-tale of AI-generated design (the distinctive `f` especially). Used with
  restraint for the invite title and H1s.
- **Body / UI — `Hanken Grotesk`**. Friendly humanist sans, very legible at small
  sizes. All paragraphs, labels, buttons, dashboard text. Weights 400/500/600.
- **Hand accent — `Caveat`**. A genuine handwriting face used in **exactly one
  place**: the host's sign-off ("— love, Mara") and the small "you're invited"
  eyebrow. Never for anything functional. (Chanel's rule: this is the one
  accessory; don't add a second.)

Type scale (rem, 1rem = 16px): `0.78 · 0.875 · 1 · 1.125 · 1.33 · 1.78 · 2.37 ·
3.16`. Generous line-height on body (1.6); tighter on display (1.05). Headings in
sentence case, warm and plain.

## 5. Layout

- **The card is an object, not a div.** The invitation sits on the page as a
  tactile card: `card` surface, soft realistic shadow, a *slight* rotation
  (≈ −1.2°), washi-tape tabs at two corners. It feels placed, not rendered.
- **The hero fills the view (no tiny floating card).** On laptop/tablet the
  invitation targets **~75% of viewport height** — `height: min(76vh, 720px)` —
  capped so it never balloons on huge monitors, with width `min(580px, 92vw)` so
  it always fits horizontally without scroll. The **card image flexes** to absorb
  the extra height while text blocks keep their natural size, so nothing is
  stretched or clipped. On phones the card is width-driven with natural height (no
  forced height). Always vertically centered with balanced margins.
- **Generous, asymmetric whitespace.** Roomy margins; content left-aligned and
  comfortable, never edge-to-edge dense.
- **One column on mobile, gentle two-column on wide** (card + details). Mobile is
  the primary target — many recipients open on a phone.
- **Dashboard = a tidy mail tray**, not a data grid: each event is a small card;
  recipients are a friendly list with stamp-style status chips. No zebra tables.

## 6. Signature element — the rubber stamp

The thing people remember. RSVP and key states render as an **ink rubber-stamp
mark** pressed onto the card. To actually *read* as a rubber stamp (not a pill
badge), the execution matters:

- **Double rule** — an outer 2.5px border plus an inner 1px line (`::before`),
  the classic stamp frame.
- **Worn ink** — a fine paper-colored speckle overlay (`::after`, a 3px
  radial-gradient dot grid at ~0.4 opacity) erodes the ink so it looks pressed,
  not printed.
- **Condensed type** — Saira Condensed, heavy, uppercase, tight tracking.
- Rotated ~8°, `mix-blend-mode: multiply` so it sinks into the paper.

States:
- `Opened` / sent → faint `ink-soft` stamp.
- **`Coming!`** → `berry` stamp (the one bright moment).
- `Can't make it` → muted `ink-soft` stamp (rotated the other way).

It doubles as the dashboard status chip language, so the metaphor is consistent
end-to-end (the §7 tracking states *are* stamps).

## 7. Motion (restrained — two coordinated "mail" moments)

Both motions tell the same story — *real mail* — so they read as one idea, not
scattered effects.

- **Envelope open (entrance).** On the recipient's invite page the card **slides up
  and out of a kraft pocket** — it is not a fade. Sequence: the wax seal pops, the
  flap lifts away, the card rises out of the pocket (which is layered *in front* of
  the card's lower half via sibling z-index, so the card is genuinely revealed from
  behind it), then the pocket drops away last. ~1.5s total.
  - **Pure 2D `translateY` + `opacity` — no `rotateX`/3D.** An earlier 3D flap
    flipped *through* the card content and read as a glitchy shard; the flat
    technique is robust and smooth on every device.
  - **The decorative envelope parts are `pointer-events:none` AND removed from the
    DOM on `animationend`.** *(We shipped a bug where the faded-but-present pocket
    sat on top of the card and silently blocked all clicks and text selection —
    never let a decorative overlay retain hit-testing.)*
  - Enhancement only — JS-gated, reduced-motion skips it, no-JS shows the card
    directly.
- **The stamp press (response).** On Accept, the `Coming!` stamp scales from ~1.25
  with a small rotation and a single overshoot, settling in ~260ms — like a stamp
  thunking down.
- Buttons: a 1–2px lift + shadow on hover, 120ms. No scattered scroll-reveals.
- **`prefers-reduced-motion`: honored** — envelope is skipped (the card is simply
  present), the stamp simply appears; no transforms.
- **No-JS:** the envelope is JS-gated — without JS the card renders directly and
  the RSVP still POSTs. Motion never gates function.

## 8. Components

- **Primary button** (`honey`): solid, soft-rounded (radius ~12px, *not* pill),
  `ink` text for contrast, gentle hover lift. Label says the action: "Send
  invites", "I'll be there".
- **Quiet button**: text/outline in `ink-soft` on `card` (e.g. "Can't make it",
  "Maybe later").
- **Status chip = mini stamp** (see §6).
- **"Change response"**: a quiet text link (`ink-soft`, underline on hover) shown
  after a recipient answers; it reverts to the choice buttons so they can re-pick.
  Changing your mind is a normal path, never an error — no confirm dialog, no nag.
- **Headcount stepper.** When (and only when) the host asks for a count, clicking
  *"I'll be there"* gracefully swaps the buttons for a warm follow-up — *"Lovely!
  How many of you are coming?"* — with a `− N +` stepper (min 1, large 40px hit
  targets) and a *"We'll be there"* confirm plus a quiet *"back"*. The count flows
  into the confirmation ("all 4 of you. See you there!"). Decline never asks.
- **Image lightbox.** The card image is clickable (`cursor: zoom-in`, keyboard-
  focusable, `role=button`): it enlarges to fill the viewport over a dim backdrop;
  clicking anywhere, the ✕, or `Esc` shrinks it back and restores focus. Scale +
  fade transition; reduced-motion = instant.
- **Optional blocks.** Every block on the invite — date, time, location, message,
  RSVP buttons, headcount — is **independently optional per event** (see DESIGN.md
  §4). A plain holiday card is just the image (+ optional message) and the quiet
  footer: no RSVP, no stepper, no date. The recipient page renders only what the
  host turned on.
- **Inputs**: `card` fill, `line` border, `honey` focus ring (visible, 2px). Warm,
  rounded ~10px. Labels above, in `ink-soft`.
- **Recipient footer = the growth loop, kept whisper-quiet.** Recipient-facing
  invite/RSVP pages end with a single small link — just *"Sent with Kith Invite"*
  (~0.72rem, `ink-soft`, ~0.7 opacity) → the home/signup page; a `title` tooltip
  carries the "make your own free invite" context. It only brightens (plum +
  underline) on hover/focus. It is the **only** brand element a guest ever sees and
  must never shout — no CTA sub-line, no button chrome.
- **Donation link** (later, G6): a single quiet "☕ Buy me a coffee" — shown
  **only in the signed-in sender app, and only after the user has created their
  first event**. **Never** on any recipient-facing page, in the sent email, or to a
  guest in any form. `ink-soft`, no button chrome, never a modal or nag.

## 9. Copy voice

Plain, warm, active, sentence case. Copy is design material (per the design
process), not decoration.

- Actions name what happens: **"Send invites"** → toast **"Invites sent"**;
  **"I'll be there"** not "Submit RSVP".
- Empty states invite action: *"No invites yet. Upload a card to get started."*
- Errors are calm and specific, in the interface's voice, no apology theater:
  *"That image is a bit big — under 10 MB works best."*
- Reminders read like a nudge from a friend, not a system: *"Just making sure you
  saw Mara's invite 🙂"*

## 10. Quality floor (non-negotiable, never announced)

Responsive to 360px · visible keyboard focus everywhere · `prefers-reduced-motion`
respected · WCAG AA contrast · semantic HTML · works without JS for the core RSVP
(the stamp animation is an enhancement, the Accept/Decline POST is not).
