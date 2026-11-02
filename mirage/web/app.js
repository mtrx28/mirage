// The page's logic. It has three jobs: capture the microphone (through the AudioWorklet), send the audio to the
// local server over one WebSocket, and show what the server reports. It talks only to the server that served it.
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  start: $("start"), hangup: $("hangup"), status: $("status"), problem: $("problem"), transcript: $("transcript"),
  held: $("held"), limit: $("limit"), callCredits: $("callCredits"), ledger: $("ledger"), pool: $("pool"),
  poolPct: $("poolPct"), creditWarn: $("creditWarn"),
  micState: $("micState"), noticeList: $("noticeList"), stageNow: $("stageNow"), stageScenario: $("stageScenario"),
  stageTimeline: $("stageTimeline"), intelCards: $("intelCards"), latencyBody: $("latencyBody"),
  checkAudio: $("checkAudio"), audioCheck: $("audioCheck"),
};

const END_REASONS = {
  uncle_ended: "Uncle ended the call.",
  gate_ended: "The safety gate ended the call (nothing safe to say).",
  tts_failed: "The call was ended because Uncle's voice failed several times in a row.",
  internal_error: "The call was ended because of an internal error (see the notice and the call's log).",
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
let callPlayer = null, callTurn = 0, heldSeconds = 0;

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

function addTranscript(text, t, who) {
  const li = document.createElement("li");
  if (who === "uncle") li.className = "uncle";
  const time = document.createElement("time");
  time.textContent = mmss(t);
  li.appendChild(time);
  li.appendChild(document.createTextNode((who === "uncle" ? "Uncle: " : "") + text));   // text node: nothing from the server is parsed as HTML
  els.transcript.appendChild(li);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

// ---------------------------------------------------------------- the dashboard: notices, stage, intel, latency
function addNotice(text) {
  setStatus(text);
  const li = document.createElement("li");
  li.appendChild(document.createTextNode(text));
  els.noticeList.appendChild(li);
}

const KIND_LABEL = { phone: "Phone number", account: "Account number", case: "Case number", ambiguous: "Number (not labelled)",
                     vpa: "UPI ID", ifsc: "IFSC code", letters: "Letters", amount: "Amount" };

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderIntel(findings) {
  els.intelCards.textContent = "";
  if (!findings.length) { els.intelCards.appendChild(el("span", "placeholder", "Nothing found yet.")); return; }
  for (const f of findings) {
    const card = el("div", "card");
    card.appendChild(el("div", "kind", KIND_LABEL[f.kind] || f.kind));
    card.appendChild(el("div", "value", f.value));                               // as heard: never replaced by a guess
    const badges = el("div");
    badges.appendChild(el("span", "badge", "confidence: " + f.confidence));
    badges.appendChild(el("span", f.valid ? "badge" : "badge bad", f.valid ? "valid format" : "not valid: " + f.reason));
    if (f.needs_confirmation) badges.appendChild(el("span", "badge bad", "needs confirmation"));
    card.appendChild(badges);
    card.appendChild(el("div", "quote", "“" + f.quote + "” at " + mmss(f.t)));
    els.intelCards.appendChild(card);
  }
}

function renderStage(msg) {
  els.stageNow.textContent = msg.stage;
  els.stageScenario.textContent = "scenario: " + msg.scenario + (msg.signals.length ? "; signals: " + msg.signals.join(", ") : "");
  els.stageTimeline.textContent = "";
  for (const s of msg.stage_timeline) els.stageTimeline.appendChild(el("li", "", s.from + " → " + s.to + " at " + mmss(s.t)));
}

const latencyCells = {};                                                      // turn number -> its table cells

function addTurnRow(msg) {
  const tr = el("tr");
  const cells = {
    n: el("td", "", String(msg.turn)), heard: el("td", "", msg.heard.length > 48 ? msg.heard.slice(0, 48) + "…" : msg.heard),
    wait: el("td", "", msg.wait.wait_s.toFixed(2)), brain: el("td", "", (msg.timing.brain_ms + msg.timing.gate_ms).toFixed(0)),
    tts: el("td", "", "-"), heardToAudible: el("td", "", "-"),
    notes: el("td", "", [msg.tactic, msg.gate_action ? "gate: " + msg.gate_action : "", msg.wait.digit_mode ? "digit mode" : "",
                         msg.wait.hold_extra_s > 0.05 ? "held " + msg.wait.hold_extra_s.toFixed(1) + " s" : ""].filter(Boolean).join("; ")),
  };
  for (const c of Object.values(cells)) tr.appendChild(c);
  els.latencyBody.appendChild(tr);
  latencyCells[msg.turn] = cells;
}

function updateTurnLatency(msg) {
  const cells = latencyCells[msg.turn];
  if (!cells) return;
  cells.tts.textContent = msg.cached ? "cached" : (msg.tts_first_byte_ms === null ? "-" : String(msg.tts_first_byte_ms));
  cells.heardToAudible.textContent = msg.arrival_to_audible_s === null ? "-" : msg.arrival_to_audible_s.toFixed(2);
}

function showMicState(muted) {
  els.micState.style.display = "block";
  els.micState.className = muted ? "muted" : "";
  els.micState.textContent = muted ? "Uncle is speaking. Stay quiet: anything you say now is lost."
                                   : "Your turn: the microphone is live.";
}

function resetDashboard() {
  els.transcript.textContent = "";
  els.noticeList.textContent = "";
  els.latencyBody.textContent = "";
  els.stageNow.textContent = "-";
  els.stageScenario.textContent = "";
  els.stageTimeline.textContent = "";
  els.micState.style.display = "none";
  renderIntel([]);
  for (const k of Object.keys(latencyCells)) delete latencyCells[k];
}

function sendToServer(message) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
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
  els.checkAudio.disabled = false;
  els.micState.style.display = "none";
  callPlayer = null;
  speakEls.button.disabled = speaking;                        // the voice test is available again once the call is over
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
      heldSeconds = msg.held_s;
      els.held.textContent = mmss(msg.held_s);
      showCredits(msg.call_credits, msg.ledger_spent, msg.pool, msg.pool_fraction, msg.warning);
      break;
    case "notice":
      addNotice(msg.message);
      break;
    case "turn":
      callTurn = msg.turn;
      renderStage(msg);
      renderIntel(msg.intel);
      addTurnRow(msg);
      break;
    case "latency":
      updateTurnLatency(msg);
      break;
    case "uncle_speech_start": {
      addTranscript(msg.text, heldSeconds, "uncle");
      if (ctx && ctx.state !== "running") {                    // sound would be blocked: say so plainly, and tell the server
        const warning = "The browser is not letting this page play sound (audio state: " + ctx.state + "). Click the page and press Start again.";
        addNotice(warning);
        sendToServer({ type: "page_warning", code: "audio_" + ctx.state, message: warning });
        ctx.resume();
      }
      const turn = msg.turn;
      callPlayer = makePlayer(ctx, msg.sample_rate, (info) => {
        sendToServer({ type: "playback_done", turn, underruns: info.underruns, audio_state: ctx ? ctx.state : "closed" });
      }, () => {
        sendToServer({ type: "playing", turn, output_latency_ms: ctx ? Math.round(((ctx.outputLatency || ctx.baseLatency || 0)) * 1000) : null });
      });
      break;
    }
    case "uncle_speech_end":
      if (callPlayer) callPlayer.end();
      break;
    case "mic":
      showMicState(msg.muted);
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
  resetDashboard();
  callPlayer = null;
  els.start.disabled = true;
  els.checkAudio.disabled = true;
  speakEls.button.disabled = true;                            // no voice test during a call
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
  ws.onmessage = (event) => {
    if (typeof event.data === "string") onServerMessage(JSON.parse(event.data));
    else if (callPlayer) callPlayer.push(event.data);                // Uncle's voice
  };
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

// ---------------------------------------------------------------- the voice test (stop 2)
// One fixed line, chosen by the server. The page only asks "speak" and plays the audio that comes back.
const speakEls = { button: $("speak"), status: $("speakStatus"), line: $("speakLine"), result: $("speakResult"),
                   problem: $("speakProblem") };
let speakWs = null, speakCtx = null, speakPlayer = null, speaking = false;

// Plays raw 16-bit mono audio as it arrives: each piece is scheduled right after the previous one.
function makePlayer(ctx, rate, onDone, onStart) {
  const lead = 0.12;                         // a small head start so the first piece is not cut off
  let next = 0, carry = null, open = 0, ended = false, underruns = 0, finished = false;
  function check() {
    if (ended && open === 0 && !finished) { finished = true; onDone({ underruns }); }
  }
  function push(buffer) {
    let bytes = new Uint8Array(buffer);
    if (carry !== null) {                    // a piece can end mid-sample: keep the odd byte for the next piece
      const merged = new Uint8Array(carry.length + bytes.length);
      merged.set(carry); merged.set(bytes, carry.length);
      bytes = merged; carry = null;
    }
    if (bytes.length % 2) { carry = bytes.slice(bytes.length - 1); bytes = bytes.slice(0, bytes.length - 1); }
    if (!bytes.length) return;
    const count = bytes.length / 2, view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const audio = ctx.createBuffer(1, count, rate), samples = audio.getChannelData(0);
    for (let i = 0; i < count; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    const source = ctx.createBufferSource();
    source.buffer = audio;
    source.connect(ctx.destination);
    const now = ctx.currentTime;
    const first = next === 0;
    if (first) next = now + lead;
    else if (next < now) { underruns++; next = now + 0.03; }       // the audio arrived late: there will be a gap
    source.start(next);
    if (first && onStart) setTimeout(onStart, Math.max(0, (next - now) * 1000));   // "playing" is reported when the first sound starts
    next += audio.duration;
    open++;
    source.onended = () => { open--; check(); };
  }
  return { push, end() { ended = true; check(); } };
}

function speakMessage(msg) {
  if (msg.type === "speech_start") {
    speakEls.line.textContent = msg.text;
    speakEls.status.textContent = "Speaking...";
    speakPlayer = makePlayer(speakCtx, msg.sample_rate, (info) => {
      speaking = false;
      speakEls.button.disabled = false;
      speakEls.status.textContent = "Finished." + (info.underruns ? " (There were " + info.underruns + " gap(s): audio arrived late.)" : "");
    });
  } else if (msg.type === "speech_end") {
    speakEls.result.textContent = (msg.cached ? "From memory (cached): free, instant. " : "Asked the speech service: " +
      (msg.first_byte_s === null ? "" : "first audio after " + msg.first_byte_s + " s, ") + msg.attempts + " attempt(s). ") +
      msg.chars + " characters, about " + msg.credits.toFixed(3) + " credits (estimate). Audio length " + msg.audio_s + " s.";
    showCredits(0, msg.ledger_spent, parseFloat(els.pool.textContent) || 5000, msg.ledger_spent / (parseFloat(els.pool.textContent) || 5000), false);
    if (speakPlayer) speakPlayer.end();
  } else if (msg.type === "speech_error") {
    speakEls.problem.textContent = msg.message;
    speakEls.status.textContent = "Not spoken.";
    speaking = false;
    speakEls.button.disabled = false;
  }
}

function openSpeakSocket() {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket("ws://" + location.host + "/ws/speak");
    socket.binaryType = "arraybuffer";
    socket.onopen = () => resolve(socket);
    socket.onerror = () => reject(new Error("Could not talk to the local server. Is it still running?"));
    socket.onmessage = (event) => {
      if (typeof event.data === "string") speakMessage(JSON.parse(event.data));
      else if (speakPlayer) speakPlayer.push(event.data);
    };
    socket.onclose = () => { speakWs = null; };
  });
}

async function speakTestLine() {
  if (speaking) return;
  speakEls.problem.textContent = "";
  speakEls.result.textContent = "";
  speaking = true;
  speakEls.button.disabled = true;
  speakEls.status.textContent = "Asking...";
  try {
    if (!speakCtx) speakCtx = new AudioContext();            // created inside the click so the browser allows sound
    await speakCtx.resume();
    if (!speakWs || speakWs.readyState !== WebSocket.OPEN) speakWs = await openSpeakSocket();
    speakWs.send(JSON.stringify({ type: "speak" }));
  } catch (err) {
    speakEls.problem.textContent = err.message || "The test line could not be started.";
    speakEls.status.textContent = "Not spoken.";
    speaking = false;
    speakEls.button.disabled = false;
  }
}
speakEls.button.addEventListener("click", speakTestLine);

els.start.addEventListener("click", startCall);
els.hangup.addEventListener("click", hangUp);
window.addEventListener("beforeunload", () => { if (ws) ws.close(); });
loadStatus();

// ---------------------------------------------------------------- "Check audio": free, before you speak to Uncle
// Plays a short quiet tone while the microphone is open, and counts how many microphone chunks kept arriving during it.
// This is the same thing a call does when Uncle speaks (capture and playback at once), without any server or any cost.
async function checkAudio() {
  if (ctx || speaking) return;
  const out = els.audioCheck;
  const lines = [];
  const show = () => { out.textContent = ""; for (const l of lines) out.appendChild(el("div", l.bad ? "warn" : "", l.text)); };
  const say = (text, bad) => { lines.push({ text, bad: !!bad }); show(); };
  els.checkAudio.disabled = true;
  els.start.disabled = true;
  say("Checking... stay quiet for a few seconds, and keep your headphones on.");
  let c = null, s = null, n = null;
  try {
    c = new AudioContext();
    await c.resume();
    s = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    await c.audioWorklet.addModule("/static/capture-worklet.js");
    const phases = { quiet: { chunks: 0, sumsq: 0, samples: 0 }, tone: { chunks: 0, sumsq: 0, samples: 0 } };
    let phase = "quiet";
    n = new AudioWorkletNode(c, "capture-processor", { processorOptions: { chunkMs: 20 } });
    n.port.onmessage = (event) => {
      const p = phases[phase], data = new Int16Array(event.data);
      p.chunks++;
      for (let i = 0; i < data.length; i++) { const v = data[i] / 32768; p.sumsq += v * v; }
      p.samples += data.length;
    };
    const mute = c.createGain();
    mute.gain.value = 0;
    c.createMediaStreamSource(s).connect(n);
    n.connect(mute);
    mute.connect(c.destination);
    await new Promise((r) => setTimeout(r, 1000));                    // 1 s of listening only
    phase = "tone";
    const osc = c.createOscillator(), gain = c.createGain();
    osc.frequency.value = 440;
    gain.gain.value = 0.08;
    osc.connect(gain);
    gain.connect(c.destination);
    osc.start();
    await new Promise((r) => setTimeout(r, 1500));                    // 1.5 s of tone while still listening
    osc.stop();
    const level = (p) => (p.samples ? 20 * Math.log10(Math.sqrt(p.sumsq / p.samples) + 1e-9) : -120);
    const expectedQuiet = 50, expectedTone = 75;                       // 20 ms chunks: 50 per second
    say("Audio output state: " + c.state + (c.state === "running" ? " (ok)" : " (not ok: the browser is blocking sound)"), c.state !== "running");
    say("Sample rate " + c.sampleRate + " Hz; output latency about " + Math.round((c.outputLatency || c.baseLatency || 0) * 1000) + " ms.");
    say("Microphone chunks while listening only: " + phases.quiet.chunks + " (expected about " + expectedQuiet + ").", phases.quiet.chunks < expectedQuiet * 0.6);
    const during = phases.tone.chunks >= expectedTone * 0.6;
    say("Microphone chunks while the tone played: " + phases.tone.chunks + " (expected about " + expectedTone + "). " +
        (during ? "Capturing and playing at the same time works." : "FAILED: the microphone stopped arriving while sound played. Uncle's calls will not work properly in this browser."), !during);
    say("Microphone level in the quiet second: " + level(phases.quiet).toFixed(0) + " dBFS" +
        (level(phases.quiet) < -75 ? " (very low or silent: check the microphone)" : " (ok)") + ". With the tone: " + level(phases.tone).toFixed(0) + " dBFS.", level(phases.quiet) < -75);
    say("Did you hear a short soft tone in your headphones? If not, sound output is not working even though the page thinks it is.");
  } catch (err) {
    say(micProblem(err), true);
  } finally {
    if (n) { try { n.port.onmessage = null; n.disconnect(); } catch (e) {} }
    if (s) s.getTracks().forEach((t) => t.stop());
    if (c) c.close().catch(() => {});
    els.checkAudio.disabled = false;
    els.start.disabled = false;
  }
}
els.checkAudio.addEventListener("click", checkAudio);
