"""The page the phone opens.

One self-contained HTML document, served by :mod:`motionless.sources.phone`.
It has to be self-contained: the phone is usually in a car with no usable
network beyond the cable to the laptop, so there is nothing to fetch from.

Two browser rules shape everything here:

* ``DeviceMotionEvent`` only fires in a **secure context**. ``localhost`` counts
  as one, which is why the USB path (``adb reverse``) needs no certificate;
  a LAN address does not, which is why the Wi-Fi path needs HTTPS.
* iOS 13+ additionally requires ``DeviceMotionEvent.requestPermission()`` to be
  called from a real user gesture, hence the Start button rather than an
  autostart.
"""

from __future__ import annotations

#: Samples are batched rather than sent one per request. At the ~60 Hz phones
#: report, a request per sample would be 60 requests a second for three
#: numbers each; batching at this interval is ~12 requests a second, and the
#: resulting latency sits well under ``motion.smoothing_tau`` (0.12 s), so it
#: costs nothing visible.
BATCH_MS = 80

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>motionless sensor</title>
<style>
  :root { --bg:#12141a; --fg:#eef1f7; --dim:#8c93a3; --ok:#4ade80; --bad:#f87171; --warn:#fbbf24; }
  * { box-sizing: border-box; }
  body {
    margin:0; padding:24px 20px calc(24px + env(safe-area-inset-bottom));
    background:var(--bg); color:var(--fg);
    font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height:100vh; display:flex; flex-direction:column; gap:20px;
    -webkit-text-size-adjust:100%;
  }
  h1 { margin:0; font-size:20px; letter-spacing:.02em; }
  h1 span { color:var(--dim); font-weight:400; }
  #status { display:flex; align-items:center; gap:10px; font-size:15px; color:var(--dim); }
  #dot { width:10px; height:10px; border-radius:50%; background:var(--dim); flex:none; }
  #dot.live { background:var(--ok); animation:pulse 1.6s ease-in-out infinite; }
  #dot.bad { background:var(--bad); }
  #dot.warn { background:var(--warn); }
  @keyframes pulse { 50% { opacity:.35; } }
  @media (prefers-reduced-motion: reduce) { #dot.live { animation:none; } }
  button {
    font:600 18px/1 inherit; color:#0b0d12; background:var(--fg);
    border:0; border-radius:14px; padding:20px; width:100%;
    -webkit-appearance:none; touch-action:manipulation;
  }
  button:disabled { opacity:.4; }
  button.secondary { background:transparent; color:var(--fg); border:1px solid #333a49; }
  .card { background:#191c24; border:1px solid #242938; border-radius:14px; padding:16px; }
  .axes { display:grid; grid-template-columns:repeat(3, 1fr); gap:12px; text-align:center; }
  .axes b { display:block; font:400 12px/1 inherit; color:var(--dim); letter-spacing:.14em; }
  .axes span {
    display:block; margin-top:8px; font:600 22px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    font-variant-numeric:tabular-nums;
  }
  .bar { height:4px; margin-top:10px; background:#242938; border-radius:2px; overflow:hidden; }
  .bar i { display:block; height:100%; width:0; background:var(--fg); transition:width .1s linear; }
  #note { margin:0; color:var(--dim); font-size:14px; }
  #note.bad { color:var(--bad); }
  #rate { color:var(--dim); font-size:13px; text-align:center; font-variant-numeric:tabular-nums; }
  footer { margin-top:auto; color:var(--dim); font-size:13px; }
  code { color:var(--fg); font-family:ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
</head>
<body>
  <h1>motionless <span>sensor</span></h1>
  <div id="status"><i id="dot"></i><span id="statusText">Ready to start</span></div>

  <button id="start">Start sending motion</button>
  <button id="stop" class="secondary" hidden>Stop</button>

  <div class="card">
    <div class="axes">
      <div><b>X</b><span id="vx">&ndash;</span><div class="bar"><i id="bx"></i></div></div>
      <div><b>Y</b><span id="vy">&ndash;</span><div class="bar"><i id="by"></i></div></div>
      <div><b>Z</b><span id="vz">&ndash;</span><div class="bar"><i id="bz"></i></div></div>
    </div>
    <div id="rate" style="margin-top:14px">waiting for the first reading</div>
  </div>

  <p id="note">Put the phone somewhere fixed — a cradle or a cup holder. It measures
  the car, not your hand.</p>

  <footer>Keep this page open and the screen awake. Sensor readings stop when the
  screen locks.</footer>

<script>
(function () {
  "use strict";
  var TOKEN = "__TOKEN__";
  var POST_URL = "motion" + (TOKEN ? "?t=" + encodeURIComponent(TOKEN) : "");
  var BATCH_MS = __BATCH_MS__;
  // Buffer ceiling: about two seconds of samples. If the laptop stops
  // answering we drop the oldest rather than growing without bound, because
  // stale motion is worse than a gap.
  var MAX_BUFFER = 160;

  // iOS reports accelerationIncludingGravity with the opposite sign to every
  // other platform (a phone lying face up reads z = -9.8 rather than +9.8).
  // We flag it rather than fixing it here, so the correction lives in the
  // daemon where it can be tested and overridden.
  var IOS = /iP(hone|ad|od)/.test(navigator.platform || "") ||
            ((navigator.userAgent || "").indexOf("Mac") >= 0 && "ontouchend" in document);

  var el = function (id) { return document.getElementById(id); };
  var startBtn = el("start"), stopBtn = el("stop");
  var dot = el("dot"), statusText = el("statusText"), note = el("note"), rate = el("rate");
  var out = { x: el("vx"), y: el("vy"), z: el("vz") };
  var bar = { x: el("bx"), y: el("by"), z: el("bz") };

  var buffer = [], inflight = false, timer = null, running = false;
  var sent = 0, received = 0, windowStart = 0, lastPaint = 0;

  function setStatus(text, kind) {
    statusText.textContent = text;
    dot.className = kind || "";
  }
  function setNote(text, bad) {
    note.textContent = text;
    note.className = bad ? "bad" : "";
  }

  function onMotion(event) {
    var a = event.accelerationIncludingGravity;
    if (!a || a.x === null || a.x === undefined) { return; }
    received++;
    buffer.push([event.timeStamp, a.x, a.y, a.z]);
    if (buffer.length > MAX_BUFFER) { buffer.splice(0, buffer.length - MAX_BUFFER); }
    paint(a);
  }

  function paint(a) {
    var now = Date.now();
    if (now - lastPaint < 100) { return; }   // 10 Hz is plenty for a readout
    lastPaint = now;
    ["x", "y", "z"].forEach(function (axis) {
      var value = a[axis] || 0;
      out[axis].textContent = value.toFixed(1);
      bar[axis].style.width = Math.min(100, Math.abs(value) / 12 * 100) + "%";
    });
    if (windowStart) {
      var hz = received / ((now - windowStart) / 1000);
      rate.textContent = hz.toFixed(0) + " Hz from the phone \\u00b7 " + sent + " samples sent";
    }
  }

  function flush() {
    if (inflight || !buffer.length || !running) { return; }
    var batch = buffer;
    buffer = [];
    inflight = true;
    fetch(POST_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ samples: batch, invert: IOS, units: "m/s^2" }),
      cache: "no-store"
    }).then(function (response) {
      inflight = false;
      if (!response.ok) {
        setStatus("Laptop refused the readings (HTTP " + response.status + ")", "bad");
        if (response.status === 403) {
          setNote("Pairing token rejected. Re-run motionless pair on the laptop.", true);
        }
        return;
      }
      sent += batch.length;
      setStatus("Streaming to the laptop", "live");
    }).catch(function () {
      inflight = false;
      setStatus("Lost the laptop \\u2014 retrying", "warn");
      setNote("Check the cable, or that motionless is still running on the laptop.", true);
    });
  }

  function start() {
    var motionEvent = window.DeviceMotionEvent;
    if (motionEvent && typeof motionEvent.requestPermission === "function") {
      setStatus("Waiting for motion permission", "warn");
      motionEvent.requestPermission().then(function (result) {
        if (result === "granted") { begin(); }
        else {
          setStatus("Motion access denied", "bad");
          setNote("Allow it in Settings \\u203a Safari \\u203a Motion & Orientation Access, " +
                  "then reload.", true);
        }
      }).catch(function () {
        setStatus("Could not ask for motion access", "bad");
        setNote("Reload the page and tap Start again \\u2014 iOS only asks on a real tap.", true);
      });
      return;
    }
    begin();
  }

  function begin() {
    running = true;
    windowStart = Date.now();
    received = 0;
    window.addEventListener("devicemotion", onMotion);
    timer = setInterval(flush, BATCH_MS);
    startBtn.hidden = true;
    stopBtn.hidden = false;
    setStatus("Waiting for the first reading", "warn");
    setNote("Put the phone somewhere fixed \\u2014 a cradle or a cup holder. " +
            "It measures the car, not your hand.");
    keepAwake();
    setTimeout(function () {
      if (!received) {
        setStatus("No readings from the sensor", "bad");
        setNote("This browser reported no accelerometer. Try Chrome or Safari; " +
                "some privacy browsers block motion sensors outright.", true);
      }
    }, 2500);
  }

  function stop() {
    running = false;
    window.removeEventListener("devicemotion", onMotion);
    if (timer) { clearInterval(timer); timer = null; }
    buffer = [];
    releaseWake();
    startBtn.hidden = false;
    stopBtn.hidden = true;
    setStatus("Stopped", "");
  }

  // The accelerometer stops when the screen locks, so hold a wake lock where
  // the browser offers one. Where it does not (iOS before 16.4), the footer
  // asks the user to do it by hand.
  var wakeLock = null;
  function keepAwake() {
    if (!navigator.wakeLock) { return; }
    navigator.wakeLock.request("screen").then(function (lock) { wakeLock = lock; })
      .catch(function () { /* denied; the footer already warns about this */ });
  }
  function releaseWake() {
    if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && running) { keepAwake(); }
  });

  startBtn.addEventListener("click", start);
  stopBtn.addEventListener("click", stop);

  if (!window.isSecureContext) {
    startBtn.disabled = true;
    setStatus("Insecure connection \\u2014 sensors blocked", "bad");
    setNote("Browsers only expose motion sensors over https:// or on localhost. " +
            "Run `motionless pair` on the laptop and use the address it prints.", true);
  } else if (!window.DeviceMotionEvent) {
    startBtn.disabled = true;
    setStatus("This browser has no motion sensor API", "bad");
    setNote("Try Chrome on Android or Safari on iOS.", true);
  }
})();
</script>
</body>
</html>
"""


def render_page(token: str = "") -> str:
    """The sender page, with the pairing token (if any) baked in."""
    if '"' in token or "\\" in token or "<" in token:
        raise ValueError("pairing token must be URL-safe text")
    return _PAGE.replace("__TOKEN__", token).replace("__BATCH_MS__", str(BATCH_MS))
