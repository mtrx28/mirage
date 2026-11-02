// The page's logic. It has three jobs: capture the microphone (through the AudioWorklet), send the audio to the
// local server over one WebSocket, and show what the server reports. It talks only to the server that served it.
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  start: $("start"), hangup: $("hangup"), status: $("status"), problem: $("problem"), transcript: $("transcript"),
  held: $("held"), limit: $("limit"), callCredits: $("callCredits"), ledger: $("ledger"), pool: $("pool"),
  poolPct: $("poolPct"), creditWarn: $("creditWarn"),
};

const END_REASONS = {
  hangup: "You hung up.",
  browser_closed: "The page was closed.",
  time_limit: "The call reached its time limit and was ended.",
  credit_limit: "The credit hard stop was reached; the call was ended.",
  call_credit_cap: "This call reached its per-call credit cap and was ended.",
  prisma_closed: "The speech service closed the connection.",
  prisma_unavailable: "The speech service could not be reached.",
  credit_limit_at_start: "The credit hard stop is reached; no call was started.",
  bad_audio_format: "The microphone's audio format is not supported.",
};

let ctx = null, stream = null, ws = null, node = null, streaming = false, ended = false;

function mmss(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function setStatus(text) { els.status.textContent = text; }

function showProblem(text) {
  els.problem.textContent = text;
  els.problem.style.display = text ? "block" : "none";
}

function micProblem(err) {
  if (!window.isSecureContext || !navigator.mediaDevices) {
    return "The browser will not give a page the microphone unless it is opened as http://127.0.0.1:8000 or " +
           "http://localhost:8000 (or over https). Open the page from exactly that address.";
  }
  switch (err && err.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "The microphone is blocked. Click the lock or camera icon in the address bar, allow the microphone for " +
             "this page, then press Start again. (Also check Windows Settings > Privacy > Microphone.)";
    case "NotFoundError":
    case "OverconstrainedError":
      return "No microphone was found. Plug one in or choose one in the browser settings, then press Start again.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone is busy or not readable: another app may be using it. Close it and press Start again.";
    default:
      return "The microphone could not be opened (" + (err && err.name ? err.name : "unknown error") + ").";
  }
}

function addTranscript(text, t) {
  const li = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = mmss(t);
  li.appendChild(time);
  li.appendChild(document.createTextNode(text));            // text node: nothing from the server is parsed as HTML
  els.transcript.appendChild(li);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function showCredits(call, ledger, pool, fraction, warning) {
  els.callCredits.textContent = call.toFixed(2);
  if (ledger !== undefined && pool !== undefined) {          // never show a half-filled "of (0.0%)" line
    els.ledger.textContent = ledger.toFixed(2);
    els.pool.textContent = pool;
    els.poolPct.textContent = (fraction * 100).toFixed(1) + "%";
  }
  els.creditWarn.textContent = warning ? "Warning: over 70% of the credit pool is used." : "";
}

function cleanup() {
  streaming = false;
  if (node) { try { node.port.onmessage = null; node.disconnect(); } catch (e) {} node = null; }
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  if (ctx) { ctx.close().catch(() => {}); ctx = null; }
  els.start.disabled = false;
  els.hangup.disabled = true;
}

function onServerMessage(msg) {
  switch (msg.type) {
    case "ready":
      streaming = true;
      els.limit.textContent = "Ends automatically at " + mmss(msg.max_call_s) + ".";
      setStatus("Connected. Speak now.");
      break;
    case "transcript":
      addTranscript(msg.text, msg.t);
      break;
    case "tick":
      els.held.textContent = mmss(msg.held_s);
      showCredits(msg.call_credits, msg.ledger_spent, msg.pool, msg.pool_fraction, msg.warning);
      break;
    case "notice":
      setStatus(msg.message);
      break;
    case "error":
      showProblem(msg.message);
      setStatus("Call not started.");
      break;
    case "ended": {
      ended = true;
      els.held.textContent = mmss(msg.held_s);
      showCredits(msg.call_credits, msg.ledger_spent, msg.pool, msg.pool_fraction, msg.warning);
      const why = END_REASONS[msg.reason] || ("The call ended (" + msg.reason + ").");
      setStatus(why + " Call length " + mmss(msg.held_s) + ", " + msg.transcripts + " transcript(s), about " +
                msg.call_credits.toFixed(2) + " credits.");
      cleanup();
      break;
    }
  }
}

async function loadStatus() {
  try {
    const s = await (await fetch("/api/status", { cache: "no-store" })).json();
    showCredits(0, s.ledger_spent, s.pool, s.ledger_spent / s.pool, s.warning);
    els.limit.textContent = "A call ends automatically at " + mmss(s.max_call_s) + ".";
    if (s.blocked) showProblem("The credit hard stop is reached, so calls will be refused. Check the dashboard and " +
                               "run: python -m mirage.credits sync <figure>");
  } catch (e) { /* the server is the page's source; if it is gone the Start button will say so */ }
}

async function startCall() {
  showProblem("");
  els.transcript.textContent = "";
  els.start.disabled = true;
  ended = false;
  setStatus("Asking for the microphone...");
  try {
    // Create the audio context inside the click so the browser allows audio to run.
    ctx = new AudioContext();
    await ctx.resume();
    // Echo cancellation, noise suppression and gain control are the browser defaults, stated here on purpose.
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await ctx.audioWorklet.addModule("/static/capture-worklet.js");
  } catch (err) {
    showProblem(micProblem(err));
    setStatus("Not connected.");
    cleanup();
    return;
  }

  setStatus("Connecting to the speech service...");
  ws = new WebSocket("ws://" + location.host + "/ws/call");
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ type: "start", sample_rate: ctx.sampleRate }));
  ws.onmessage = (event) => onServerMessage(JSON.parse(event.data));
  ws.onerror = () => { if (!ended) showProblem("Could not talk to the local server. Is it still running?"); };
  ws.onclose = () => {
    if (!ended && ctx) { setStatus("The connection to the server closed."); cleanup(); }
  };

  const source = ctx.createMediaStreamSource(stream);
  node = new AudioWorkletNode(ctx, "capture-processor", { processorOptions: { chunkMs: 20 } });
  node.port.onmessage = (event) => {
    if (streaming && ws && ws.readyState === WebSocket.OPEN) ws.send(event.data);
  };
  // The worklet outputs silence; routing it through a muted gain keeps the browser pulling audio through it
  // without playing the microphone back to you.
  const mute = ctx.createGain();
  mute.gain.value = 0;
  source.connect(node);
  node.connect(mute);
  mute.connect(ctx.destination);
  els.hangup.disabled = false;
}

function hangUp() {
  els.hangup.disabled = true;
  streaming = false;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "hangup" }));
    setTimeout(() => { if (!ended) { setStatus("You hung up."); cleanup(); } }, 3000);   // if no summary arrives
  } else {
    cleanup();
  }
}

els.start.addEventListener("click", startCall);
els.hangup.addEventListener("click", hangUp);
window.addEventListener("beforeunload", () => { if (ws) ws.close(); });
loadStatus();
