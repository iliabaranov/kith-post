// Kith Post invitation page: envelope intro, image lightbox, RSVP -> headcount ->
// stamp, change response. Every block is optional, so guard for missing elements.
// (In preview mode there's no persistence; recipient persistence lands in G4.)
(function () {
  document.documentElement.className = "js";
  const params = new URLSearchParams(location.search);
  const frozen = params.get("stage") === "envelope";
  if (frozen) document.documentElement.classList.add("frozen");

  const envelope = document.querySelector(".envelope");
  if (envelope && !frozen) {
    envelope.addEventListener("animationend", () => envelope.remove());
  }

  // ---- RSVP (only present when the host enabled it) ----
  const yes = document.getElementById("yes");
  if (yes) {
    const no = document.getElementById("no");
    const actions = document.getElementById("actions");
    const headcount = document.getElementById("headcount"); // may be absent
    const inc = document.getElementById("inc");
    const dec = document.getElementById("dec");
    const countEl = document.getElementById("count");
    const confirmYes = document.getElementById("confirmYes");
    const back = document.getElementById("backToChoices");
    const confirmEl = document.getElementById("confirm");
    const change = document.getElementById("change");
    const sComing = document.getElementById("stampComing");
    const sDeclined = document.getElementById("stampDeclined");
    const addcal = document.getElementById("addcal");
    let count = 1;
    const maxGuests = (headcount && parseInt(headcount.dataset.max, 10)) || 30;

    const renderCount = () => {
      if (countEl) countEl.textContent = count;
      if (dec) dec.disabled = count <= 1;
      if (inc) inc.disabled = count >= maxGuests;
    };
    const setPhase = (p) => {
      actions.hidden = p !== "choose";
      if (headcount) headcount.hidden = p !== "headcount";
    };
    const comingMsg = (n) =>
      n > 1
        ? "Wonderful — all " + n + " of you. See you there! 🎈"
        : "Wonderful — see you there! 🎈";
    const finalize = (coming) => {
      setPhase("none");
      if (sComing) sComing.classList.toggle("show", coming);
      if (sDeclined) sDeclined.classList.toggle("show", !coming);
      confirmEl.textContent = coming
        ? comingMsg(count)
        : "Thanks for letting us know — you'll be missed.";
      change.hidden = false;
      if (addcal) addcal.hidden = !coming;  // calendar links only after "Coming"
    };
    const reopen = () => {
      setPhase("choose");
      if (sComing) sComing.classList.remove("show");
      if (sDeclined) sDeclined.classList.remove("show");
      confirmEl.textContent = "No problem — just pick again.";
      change.hidden = true;
      if (addcal) addcal.hidden = true;
      yes.focus();
    };

    yes.addEventListener("click", () => {
      if (headcount) {
        count = 1;
        renderCount();
        setPhase("headcount");
        if (inc) inc.focus();
      } else {
        finalize(true);
      }
    });
    no.addEventListener("click", () => finalize(false));
    if (inc) inc.addEventListener("click", () => { count = Math.min(count + 1, maxGuests); renderCount(); });
    if (dec) dec.addEventListener("click", () => { count = Math.max(count - 1, 1); renderCount(); });
    if (confirmYes) confirmYes.addEventListener("click", () => finalize(true));
    if (back) back.addEventListener("click", () => setPhase("choose"));
    change.addEventListener("click", reopen);
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
