"use strict";
/* SmileBot front end: chat + voice call. All bot text is inserted with textContent (no HTML injection). */

const $ = (s) => document.querySelector(s);
const log = $("#log"), chipsEl = $("#chips"), input = $("#input");
let token = null;       // HMAC-signed conversation state from the server
let busy = false;
let lastBot = "";
let lastChips = [];

/* ---------------- clinic hours (client-side, Asia/Kolkata) ---------------- */
(function openNow() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  const mins = +parts.hour * 60 + +parts.minute, el = $("#openNow");
  const open = parts.weekday !== "Sun" && mins >= 540 && mins < 1080 && !(mins >= 780 && mins < 840);
  el.textContent = open ? "Open now" : parts.weekday !== "Sun" && mins >= 780 && mins < 840 ? "Closed for lunch until 2 pm" : "Closed now, but Aria can still book you in";
  el.className = open ? "open" : "closed";
})();

fetch("/api/health").then((r) => r.json()).then((h) => {
  $("#engine").textContent = (h.ai ? "AI conversation on" : "Guided mode (no AI key)") + (h.calendar ? ", calendar synced" : "");
}).catch(() => { $("#engine").textContent = "Offline"; });

/* ---------------- rendering ---------------- */
function scrollDown() { log.scrollTop = log.scrollHeight; }

function addMsg(role, text, extra = "") {
  const li = document.createElement("li");
  li.className = `msg msg--${role} msg--new ${extra}`;
  const b = document.createElement("div");
  b.className = "bubble";
  const p = document.createElement("p");
  p.textContent = text;
  b.append(p);
  li.append(b);
  log.append(li);
  scrollDown();
  return li;
}

function addTyping() {
  const li = document.createElement("li");
  li.className = "msg msg--bot typing";
  li.setAttribute("aria-label", "Aria is typing");
  li.innerHTML = '<div class="bubble"><span></span><span></span><span></span></div>';
  log.append(li);
  scrollDown();
  return li;
}

function addCard(card, confirmed) {
  const node = $("#cardTpl").content.firstElementChild.cloneNode(true);
  const art = node.querySelector(".appt");
  for (const el of node.querySelectorAll("[data-k]")) {
    const v = card[el.dataset.k];
    el.textContent = Array.isArray(v) ? v.join(" · ") : v ?? "";
  }
  node.querySelector(".appt__stubLabel").textContent = confirmed ? "Booking ref" : card.urgent ? "Urgent" : "Not booked yet";
  if (!confirmed) node.querySelector(".appt__ref").textContent = card.urgent ? "Priority" : "Draft";
  art.classList.add(confirmed ? "appt--confirmed" : "appt--pending");
  if (card.urgent) art.classList.add("appt--urgent");
  art.setAttribute("aria-label", `${confirmed ? "Confirmed" : "Draft"} appointment: ${card.slot}`);
  log.append(node);
  scrollDown();
}

function renderChips(chips, links) {
  chipsEl.replaceChildren();
  const callChips = $("#callChips");
  callChips.replaceChildren();
  lastChips = chips;
  for (const label of chips) {
    if (label === "Add to my calendar" && links) {
      const g = Object.assign(document.createElement("a"), { className: "chip chip--link", href: links.google, target: "_blank", rel: "noopener", textContent: "Add to Google Calendar" });
      const i = Object.assign(document.createElement("a"), { className: "chip chip--link", href: links.ics, textContent: "Download .ics" });
      chipsEl.append(g, i);
      continue;
    }
    for (const host of [chipsEl, callChips]) {
      const b = Object.assign(document.createElement("button"), { type: "button", className: "chip", textContent: label });
      b.addEventListener("click", () => send(label));
      host.append(b);
    }
  }
}

/* ---------------- talking to the server ---------------- */
async function send(text, { silent = false } = {}) {
  if (busy) return;
  text = (text ?? "").trim();
  busy = true;
  if (text && !silent) addMsg("user", text);
  chipsEl.replaceChildren();
  const typing = addTyping();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: token, message: text, voice: Call.active }),
    });
    const data = await res.json().catch(() => ({}));
    typing.remove();
    if (res.status === 400) { token = null; addMsg("bot", "This conversation expired. Starting a fresh one for you.", "msg--error"); busy = false; return send(""); }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    token = data.state;
    const r = data.reply;
    lastBot = r.text;
    addMsg("bot", r.text, r.urgent && /urgent/i.test(r.text) ? "msg--urgent" : "");
    if (r.card) addCard(r.card, Boolean(r.booking));
    renderChips(r.chips || [], data.links || r.links);
    requestAnimationFrame(scrollDown);
    if (Call.active) Call.say(r.text);
  } catch (err) {
    typing.remove();
    const li = addMsg("bot", `Message not sent: ${err.message}. Check your connection and try again.`, "msg--error");
    const retry = Object.assign(document.createElement("button"), { type: "button", className: "chip", textContent: "Try again" });
    retry.addEventListener("click", () => { li.remove(); retry.remove(); busy = false; send(text, { silent: true }); });
    chipsEl.replaceChildren(retry);
    if (Call.active) Call.say("Sorry, I'm having trouble connecting. Please try again in a moment.");
  } finally {
    busy = false;
  }
}

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = input.value;
  if (!v.trim() || busy) return;
  input.value = "";
  send(v);
});

$("#restartBtn").addEventListener("click", () => {
  token = null;
  log.replaceChildren();
  send("");
  input.focus();
});

/* ---------------- speech helpers ---------------- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const canSpeak = "speechSynthesis" in window;

function speakable(text) {
  return text
    .replace(/\p{Extended_Pictographic}/gu, "")
    .replace(/\+91(\d{5})(\d{5})/g, (_, a, b) => `${a.split("").join(" ")}, ${b.split("").join(" ")}`)
    .replace(/BSD-([A-Z0-9]{5})/g, (_, r) => `B S D, ${r.split("").join(" ")}`)
    .replace(/₹/g, "rupees ")
    .replace(/(\d)–(\d)/g, "$1 to $2");
}

function pickVoice() {
  const voices = speechSynthesis.getVoices();
  return voices.find((v) => /en-IN/i.test(v.lang) && /female|neerja|heera|google/i.test(v.name))
    || voices.find((v) => /en-IN/i.test(v.lang))
    || voices.find((v) => /en-GB/i.test(v.lang) && /female|google/i.test(v.name))
    || voices.find((v) => /^en/i.test(v.lang));
}
if (canSpeak) speechSynthesis.onvoiceschanged = () => {};

/* one-shot dictation for the chat composer */
const micBtn = $("#micBtn");
if (!SR) { micBtn.hidden = true; }
micBtn.addEventListener("click", () => {
  if (micBtn.getAttribute("aria-pressed") === "true") return;
  const rec = new SR();
  rec.lang = "en-IN";
  rec.interimResults = true;
  micBtn.setAttribute("aria-pressed", "true");
  rec.onresult = (e) => {
    const t = Array.from(e.results).map((r) => r[0].transcript).join("");
    input.value = t;
    if (e.results[e.results.length - 1].isFinal) { input.value = ""; send(t); }
  };
  rec.onend = () => micBtn.setAttribute("aria-pressed", "false");
  rec.onerror = () => micBtn.setAttribute("aria-pressed", "false");
  rec.start();
});

/* ---------------- voice call mode ---------------- */
const Call = {
  active: false, muted: false, rec: null, timer: null, started: 0, misses: 0, stream: null, raf: 0,
  el: $("#call"), orb: $("#orb"), state: $("#callState"), caps: $("#captions"),

  async open() {
    if (!SR || !canSpeak) return;
    this.active = true;
    this.el.hidden = false;
    this.caps.replaceChildren();
    this.started = Date.now();
    this.timer = setInterval(() => {
      const s = Math.floor((Date.now() - this.started) / 1000);
      $("#callTimer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    }, 500);
    this.setState("Ringing…");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      this.meter(this.stream);
    } catch {
      this.setState("Microphone blocked. Allow mic access in your browser to talk to Aria.");
      return;
    }
    $("#endBtn").focus();
    if (!token || !lastBot) send("");
    else this.say(`Hi, it's Aria again. ${lastBot}`);
  },

  meter(stream) {
    const ctx = new AudioContext(), an = ctx.createAnalyser();
    ctx.createMediaStreamSource(stream).connect(an);
    an.fftSize = 256;
    const buf = new Uint8Array(an.frequencyBinCount);
    const tick = () => {
      an.getByteFrequencyData(buf);
      const lvl = Math.min(1, buf.reduce((a, b) => a + b, 0) / buf.length / 60);
      this.orb.style.setProperty("--level", this.listening && !this.muted ? lvl.toFixed(2) : 0);
      this.raf = requestAnimationFrame(tick);
    };
    this.ctx = ctx;
    tick();
  },

  setState(t, mode) {
    this.state.textContent = t;
    this.orb.classList.toggle("orb--speaking", mode === "speaking");
    this.orb.classList.toggle("orb--listening", mode === "listening");
  },

  caption(text, who, interim = false) {
    let p = this.caps.querySelector(".cap--interim");
    if (!p) { p = document.createElement("p"); this.caps.append(p); }
    p.className = `cap--${who}${interim ? " cap--interim" : ""}`;
    p.textContent = text;
    this.caps.scrollTop = this.caps.scrollHeight;
  },

  say(text) {
    if (!this.active) return;
    this.stopListening();
    speechSynthesis.cancel();
    this.caption(text, "aria");
    const u = new SpeechSynthesisUtterance(speakable(text));
    const v = pickVoice();
    if (v) u.voice = v;
    u.lang = v?.lang || "en-IN";
    u.rate = 1.03;
    u.onstart = () => this.setState("Aria is speaking", "speaking");
    u.onend = () => { if (this.active) this.listen(); };
    u.onerror = () => { if (this.active) this.listen(); };
    speechSynthesis.speak(u);
  },

  listen() {
    if (!this.active || this.muted || busy) return;
    this.stopListening();
    const rec = new SR();
    rec.lang = "en-IN";
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    let final = "";
    rec.onstart = () => { this.listening = true; this.setState("Listening…", "listening"); };
    rec.onresult = (e) => {
      const res = e.results[e.results.length - 1];
      if (res.isFinal) final += res[0].transcript;
      else this.caption(res[0].transcript, "you", true);
    };
    rec.onerror = (e) => { if (e.error === "not-allowed") this.setState("Microphone blocked."); };
    rec.onend = () => {
      this.listening = false;
      if (!this.active || this.muted) return;
      if (final.trim()) {
        this.misses = 0;
        this.caption(final.trim(), "you");
        this.caps.append(document.createElement("span"));  // close the interim line
        this.setState("Aria is thinking…");
        send(final.trim());
      } else if (++this.misses >= 2) {
        this.misses = 0;
        this.say("Sorry, I didn't catch that. Are you still there?");
      } else {
        this.listen();
      }
    };
    this.rec = rec;
    try { rec.start(); } catch { /* already started */ }
  },

  stopListening() {
    if (this.rec) { this.rec.onend = null; try { this.rec.abort(); } catch {} this.rec = null; }
    this.listening = false;
  },

  interrupt() {
    if (!this.active || !speechSynthesis.speaking) return;
    speechSynthesis.cancel();
    this.listen();
  },

  toggleMute() {
    this.muted = !this.muted;
    $("#muteBtn").setAttribute("aria-pressed", String(this.muted));
    if (this.muted) { this.stopListening(); this.setState("You're muted"); } else this.listen();
  },

  close() {
    this.active = false;
    this.stopListening();
    speechSynthesis.cancel();
    clearInterval(this.timer);
    cancelAnimationFrame(this.raf);
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.el.hidden = true;
    $("#callBtn").focus();
  },
};

const callBtn = $("#callBtn");
if (!SR || !canSpeak) {
  callBtn.disabled = true;
  $("#voiceHint").textContent = "Voice calls need Chrome, Edge or Safari. You can still chat here.";
}
callBtn.addEventListener("click", () => Call.open());
$("#endBtn").addEventListener("click", () => Call.close());
$("#muteBtn").addEventListener("click", () => Call.toggleMute());
$("#orb").addEventListener("click", () => Call.interrupt());
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && Call.active) Call.close(); });

/* ---------------- go ---------------- */
send("");
