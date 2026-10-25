"use strict";
// The Voice panel (app/ghost.py): Keys Ghost Machine, the hands-free voice agent Boo, as the Studio's voice director.
// The header's microphone button opens Boo's face page beside the studio (full screen on a phone); the page is served
// by the Studio under /ghost/, so a phone that reaches the Studio with its access key reaches Boo too. Hiding the panel
// keeps the conversation going; End closes it. The camera stays off until it is turned on in Boo's own page. Boo's
// Studio agent asks the Studio to play a take: the panel shows it here, from /files/ like every clip of the studio.
const GHOST = { open: false, live: false, state: null, url: "" };
const GHOST_MIC = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';

async function ghostState() { try { GHOST.state = await api("/api/ghost"); } catch (e) { GHOST.state = { configured: false, error: e.message }; } GHOST.url = (GHOST.state && GHOST.state.url) || ""; return GHOST.state; }
function ghostButton() { const b = $("#ghostbtn"); if (!b) return; b.classList.toggle("on", GHOST.open); b.classList.toggle("live", GHOST.live);
  b.setAttribute("aria-pressed", GHOST.open ? "true" : "false"); b.title = GHOST.live ? "Voice director (talking; click to " + (GHOST.open ? "hide" : "show") + ")" : "Voice director"; }

function ghostPanel() { let p = $("#ghostpanel"); if (p) return p;
  p = el("aside", { id: "ghostpanel", "aria-label": "Voice director", role: "complementary" },
    el("div", { class: "gh-head" }, el("span", { class: "gh-dot", id: "ghostdot", "aria-hidden": "true" }), el("b", {}, "Voice director"), el("span", { class: "gh-sub", id: "ghostsub" }, "Boo"),
      el("span", { class: "fill" }),
      el("a", { class: "gh-btn", id: "ghosttab", href: "/ghost/", target: "_blank", rel: "noopener", title: "Open Boo in a tab of its own" }, "Open in a tab"),
      el("button", { class: "gh-btn", type: "button", title: "Hide the panel; Boo keeps listening", onclick: () => ghostShow(false) }, "Hide"),
      el("button", { class: "gh-btn gh-end", type: "button", title: "End the conversation and close the panel", onclick: ghostEnd }, "End")),
    el("div", { class: "gh-body", id: "ghostbody" }));
  document.body.append(p); return p; }

function ghostSetup(body, state) { const box = el("input", { type: "url", placeholder: "http://192.168.1.10:8095", value: GHOST.url || "", "aria-label": "The voice agent's address", autocomplete: "off" }),
    said = el("p", { class: "note m-flat", role: "status" }),
    save = async () => { said.textContent = "Checking…"; try { const s = await api("/api/ghost", { url: box.value.trim() }); GHOST.state = s; GHOST.url = s.url || "";
      if (!s.url) { said.textContent = "Forgotten."; return; } if (!s.reachable) { said.textContent = "Saved, but nothing answers there yet: start the voice agent with a profile that has the Studio agent (docs/voice-director.md)."; return; } ghostDraw(); }
      catch (e) { said.textContent = e.message; } };
  body.replaceChildren(el("div", { class: "gh-setup" },
    el("h2", {}, "Talk to the Studio"),
    el("p", { class: "note m-flat" }, "Boo, the Keys Ghost Machine voice agent, hears you, answers out loud and works the studio for you: what is in the queue and how long it has left, your films, shots and takes, a render of a shot at a size, a change to how a shot looks, a take played back, a job cancelled. It asks out loud before it cancels anything or starts a long render."),
    el("p", { class: "note m-flat" }, state && state.configured && !state.reachable ? "The voice agent at " + state.url + " does not answer. Start it, or give its new address." :
      "It runs on its own computer (a Mac, or a DGX Spark that is not leading the others) with the Studio agent in its profile; give its address here. Set it on the computer running the studio, or through its Tailscale address."),
    el("form", { class: "row", onsubmit: e => { e.preventDefault(); save(); } }, box, el("button", { type: "submit", class: "primary fit" }, "Save")), said,
    el("p", { class: "note m-flat" }, "On a phone: open the studio as usual (Settings → Access key has the QR code), then this button. The microphone needs HTTPS, so on a phone reach the studio through ", el("code", {}, "tailscale serve"), ". The camera is off unless you turn it on in Boo's page."))); }

function ghostFrame(body) { let f = body.querySelector("iframe");
  if (!f) { f = el("iframe", { src: "/ghost/", title: "Boo, the voice director", allow: "microphone; camera; autoplay", referrerpolicy: "no-referrer" }); body.replaceChildren(f); }
  GHOST.live = true; ghostButton(); return f; }

async function ghostDraw() { const p = ghostPanel(), body = $("#ghostbody", p);
  if (!body.querySelector("iframe")) { const s = await ghostState(); if (!s || !s.configured || !s.reachable) { ghostSetup(body, s); $("#ghostdot").className = "gh-dot" + (s && s.configured ? " bad" : ""); return; }
    if (s.sockets === false) { $("#ghostdot").className = "gh-dot bad"; body.replaceChildren(el("div", { class: "gh-setup" }, el("h2", {}, "One package missing"),
      el("p", { class: "note m-flat" }, "This studio's environment has no websockets package, so it cannot carry the conversation's socket. Run the setup again with --upgrade (it installs it), or install websockets 17.2 into the studio's environment, then restart the studio."),
      el("a", { class: "gh-btn", href: s.url, target: "_blank", rel: "noopener" }, "Open the voice agent at its own address meanwhile"))); return; } }
  const s = GHOST.state || {}; $("#ghostsub").textContent = "Boo"; $("#ghostsub").title = "Keys Ghost Machine" + (s.agent && s.agent.profile ? " · profile " + s.agent.profile : "") + (s.url ? " · " + s.url : ""); $("#ghostdot").className = "gh-dot ok";
  ghostFrame(body); }

function ghostShow(open) { GHOST.open = open; const p = ghostPanel(); p.classList.toggle("open", open); document.documentElement.classList.toggle("ghost-open", open);
  if (open) ghostDraw(); ghostButton(); }
function ghostEnd() { const p = $("#ghostpanel"); if (p) { const f = p.querySelector("iframe"); if (f) f.remove(); } GHOST.live = false; ghostShow(false); }

// a take Boo was asked to play: the studio's own player, over everything, from the studio's folder
function ghostPlay(path, title) { if (!/^[\w./ -]+$/.test(path) || path.includes("..")) return; $("#ghostplay") && $("#ghostplay").remove();
  const ext = (path.split(".").pop() || "").toLowerCase(), src = file(path);
  const media = ["wav", "mp3", "flac", "m4a"].includes(ext) ? el("audio", { src, controls: true, autoplay: true }) :
    ["png", "jpg", "jpeg", "webp"].includes(ext) ? el("img", { src, alt: title || path }) : el("video", { src, controls: true, autoplay: true, playsinline: true });
  const close = () => { const n = $("#ghostplay"); if (n) { n.querySelectorAll("video,audio").forEach(m => { m.pause(); m.removeAttribute("src"); m.load(); }); n.remove(); } };
  document.body.append(el("div", { id: "ghostplay", role: "dialog", "aria-modal": "true", "aria-label": title || "Playing a take", onclick: e => { if (e.target.id === "ghostplay") close(); } },
    el("div", { class: "gh-play" }, el("div", { class: "gh-head" }, el("b", {}, title || path), el("span", { class: "fill" }), el("button", { class: "gh-btn", type: "button", onclick: close }, "Close")), media))); }
window.addEventListener("message", e => { if (e.origin !== location.origin || !e.data || e.data.type !== "ghost-studio") return;
  if (e.data.action === "play" && typeof e.data.path === "string") ghostPlay(e.data.path, String(e.data.title || "")); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#ghostplay")) $("#ghostplay").remove(); });

// started once the page has parsed: $, el and api are defined by index.html's own script, after the modules
function ghostStart() { const b = $("#ghostbtn"); if (!b) return; b.innerHTML = GHOST_MIC; b.onclick = () => ghostShow(!GHOST.open); ghostButton();
  if (/[?&]voice=1\b/.test(location.search)) ghostShow(true);  // /?voice=1: a phone's home-screen link straight to Boo
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ghostStart); else ghostStart();
