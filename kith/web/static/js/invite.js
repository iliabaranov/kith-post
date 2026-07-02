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
    const extra = document.getElementById("rsvpExtra"); // optional note/allergies fields
    const headcount = document.getElementById("headcount"); // may be absent
    const confirmYes = document.getElementById("confirmYes");
    const back = document.getElementById("backToChoices");
    const confirmEl = document.getElementById("confirm");
    const change = document.getElementById("change");
    const sComing = document.getElementById("stampComing");
    const sDeclined = document.getElementById("stampDeclined");
    const addcal = document.getElementById("addcal");
    const $ = (id) => document.getElementById(id);
    const aInc = $("incAdults"), aDec = $("decAdults"), aCount = $("countAdults"), aField = $("adultsField");
    const kInc = $("incKids"), kDec = $("decKids"), kCount = $("countKids"), kField = $("kidsField");
    let adults = parseInt((aField && aField.value) || "1", 10) || 1;
    let kids = parseInt((kField && kField.value) || "0", 10) || 0;
    const maxGuests = (headcount && parseInt(headcount.dataset.max, 10)) || 30;
    const total = () => adults + kids;

    const renderCount = () => {
      if (aCount) aCount.textContent = adults;
      if (kCount) kCount.textContent = kids;
      if (aField) aField.value = adults;
      if (kField) kField.value = kids;
      if (aDec) aDec.disabled = adults <= 1;   // at least one adult
      if (kDec) kDec.disabled = kids <= 0;
      const full = total() >= maxGuests;
      if (aInc) aInc.disabled = full;
      if (kInc) kInc.disabled = full;
    };
    const setPhase = (p) => {
      if (actions) actions.hidden = p !== "choose";
      if (headcount) headcount.hidden = p !== "headcount";
      if (extra) extra.hidden = p === "none";  // keep note/allergies visible while choosing
    };

    // steppers (shared by both modes); the cap applies to adults + kids together
    if (aInc) aInc.addEventListener("click", () => { if (total() < maxGuests) { adults++; renderCount(); } });
    if (aDec) aDec.addEventListener("click", () => { adults = Math.max(1, adults - 1); renderCount(); });
    if (kInc) kInc.addEventListener("click", () => { if (total() < maxGuests) { kids++; renderCount(); } });
    if (kDec) kDec.addEventListener("click", () => { kids = Math.max(0, kids - 1); renderCount(); });
    if (back) back.addEventListener("click", () => setPhase("choose"));
    // "I'll be there" reveals the steppers first (when a headcount is asked)
    if (headcount) {
      yes.addEventListener("click", (e) => {
        e.preventDefault();
        adults = 1; kids = 0; renderCount(); setPhase("headcount");
        if (aInc) aInc.focus();
      });
    }

    if (preview) {
      const comingMsg = (n) =>
        n > 1 ? "Wonderful — all " + n + " of you. See you there! 🎈" : "Wonderful — see you there! 🎈";
      const finalize = (coming) => {
        setPhase("none");
        if (sComing) sComing.classList.toggle("show", coming);
        if (sDeclined) sDeclined.classList.toggle("show", !coming);
        if (confirmEl) confirmEl.textContent = coming ? comingMsg(total()) : "Thanks for letting us know — you'll be missed.";
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
    }
    // live mode needs no extra JS here: the steppers keep the hidden adults/kids
    // fields current, and the form submits normally (PRG reload renders the stamp).
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
