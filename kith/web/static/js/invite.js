// Kith Post invitation page: envelope intro, image lightbox, RSVP -> headcount ->
// stamp, change response. Every block is optional, so guard for missing elements.
// Live mode persists via a real form POST (works with no JS; the reload renders
// the stamp) — JS only adds the headcount stepper. Preview mode is a client-only
// demo and saves nothing.
(function () {
  document.documentElement.className = "js";
  const params = new URLSearchParams(location.search);
  const frozen = params.get("stage") === "envelope";
  if (frozen) document.documentElement.classList.add("frozen");

  // The envelope intro plays only the first time a real invite is opened on this
  // device. Later loads — notably the PRG reload after an RSVP POST, and the
  // ?edit=1 "change response" view — skip straight to the card. Scoped to /i/…
  // so preview (which never reloads) keeps animating each time.
  if (!frozen && location.pathname.indexOf("/i/") === 0) {
    const seenKey = "kith:opened:" + location.pathname;
    let seen = false;
    try { seen = localStorage.getItem(seenKey) === "1"; } catch (e) {}
    if (seen) document.documentElement.classList.add("seen");
    else { try { localStorage.setItem(seenKey, "1"); } catch (e) {} }
  }

  const envelope = document.querySelector(".envelope");
  if (envelope && !frozen) {
    envelope.addEventListener("animationend", () => envelope.remove());
  }

  // ---- RSVP (only present when the host enabled it) ----
  const yes = document.getElementById("yes");
  if (yes) {
    const form = document.getElementById("rsvpForm");
    const preview = !!(form && form.dataset.preview === "1");
    const no = document.getElementById("no");
    const actions = document.getElementById("actions");
    const headcount = document.getElementById("headcount"); // may be absent
    const inc = document.getElementById("inc");
    const dec = document.getElementById("dec");
    const countEl = document.getElementById("count");
    const confirmYes = document.getElementById("confirmYes");
    const back = document.getElementById("backToChoices");
    const partySize = document.getElementById("partySize");
    const confirmEl = document.getElementById("confirm");
    const change = document.getElementById("change");
    const sComing = document.getElementById("stampComing");
    const sDeclined = document.getElementById("stampDeclined");
    const addcal = document.getElementById("addcal");
    let count = parseInt((partySize && partySize.value) || "1", 10) || 1;
    const maxGuests = (headcount && parseInt(headcount.dataset.max, 10)) || 30;

    const renderCount = () => {
      if (countEl) countEl.textContent = count;
      if (partySize) partySize.value = count;
      if (dec) dec.disabled = count <= 1;
      if (inc) inc.disabled = count >= maxGuests;
    };
    const setPhase = (p) => {
      if (actions) actions.hidden = p !== "choose";
      if (headcount) headcount.hidden = p !== "headcount";
    };

    // stepper (shared by both modes)
    if (inc) inc.addEventListener("click", () => { count = Math.min(count + 1, maxGuests); renderCount(); });
    if (dec) dec.addEventListener("click", () => { count = Math.max(count - 1, 1); renderCount(); });
    if (back) back.addEventListener("click", () => setPhase("choose"));
    // "I'll be there" reveals the stepper first (when a headcount is asked)
    if (headcount) {
      yes.addEventListener("click", (e) => {
        e.preventDefault();
        count = 1; renderCount(); setPhase("headcount");
        if (inc) inc.focus();
      });
    }

    if (preview) {
      const comingMsg = (n) =>
        n > 1 ? "Wonderful — all " + n + " of you. See you there! 🎈" : "Wonderful — see you there! 🎈";
      const finalize = (coming) => {
        setPhase("none");
        if (sComing) sComing.classList.toggle("show", coming);
        if (sDeclined) sDeclined.classList.toggle("show", !coming);
        if (confirmEl) confirmEl.textContent = coming ? comingMsg(count) : "Thanks for letting us know — you'll be missed.";
        if (change) change.hidden = false;
        if (addcal) addcal.hidden = !coming;
      };
      const reopen = () => {
        setPhase("choose");
        if (sComing) sComing.classList.remove("show");
        if (sDeclined) sDeclined.classList.remove("show");
        if (confirmEl) confirmEl.textContent = "No problem — just pick again.";
        if (change) change.hidden = true;
        if (addcal) addcal.hidden = true;
        yes.focus();
      };
      if (form) form.addEventListener("submit", (e) => e.preventDefault());
      if (!headcount) yes.addEventListener("click", () => finalize(true));
      no.addEventListener("click", () => finalize(false));
      if (confirmYes) confirmYes.addEventListener("click", () => finalize(true));
      if (change) change.addEventListener("click", reopen);
    } else if (confirmYes) {
      // live: keep party size current, then let the form submit (PRG reload)
      confirmYes.addEventListener("click", () => { if (partySize) partySize.value = count; });
    }
  }

  // ---- image lightbox ----
  const photo = document.getElementById("photo");
  const lightbox = document.getElementById("lightbox");
  if (photo && lightbox) {
    const closeBtn = document.getElementById("lightboxClose");
    const open = () => { lightbox.classList.add("open"); if (closeBtn) closeBtn.focus(); };
    const close = () => { lightbox.classList.remove("open"); photo.focus(); };
    photo.addEventListener("click", open);
    photo.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    lightbox.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && lightbox.classList.contains("open")) close();
    });
  }
})();
