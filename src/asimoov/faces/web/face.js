/* ASIMOOV face: procedural eyes on a canvas, no dependency, no build step.
 * Query params: ?token=... ?theme=light ?demo=1 ?hud=1
 * Reads face.state envelopes from ws://<host>/face/ws, sends {type:"tap"},
 * {type:"estop"} and binary frame.browser camera frames. */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var THEME = params.get("theme") === "light" ? "light" : "dark";
  var DEMO = params.get("demo") === "1";
  var TOKEN = params.get("token");
  var scriptUrl = document.currentScript ? document.currentScript.src : "face.js";

  var TRANSITION_MS = 300;
  var BLINK_MS = 150;
  var LIP_TAU_MS = 50;
  var GAZE_TAU_MS = 80;
  var CAMERA_FPS = 5;
  var CAMERA_W = 640;
  var CAMERA_H = 360;
  var FRAME_VERSION = 1;
  var FRAME_TOPIC_BROWSER = "frame.browser";

  var canvas = document.getElementById("face");
  var ctx = canvas.getContext("2d");
  var hudEl = document.getElementById("hud");
  var statusEl = document.getElementById("status");
  var video = document.getElementById("capture");
  document.body.setAttribute("data-theme", THEME);

  var table = null;
  var palette = null;
  var current = null; // interpolated drawing params
  var from = null;
  var to = null;
  var transitionStart = 0;
  var state = {
    emotion: "neutral",
    intensity: 1,
    gaze: { x: 0, y: 0 },
    lip: 0,
    blink: false,
    eyelids: 0,
    talking: false,
  };
  var gaze = { x: 0, y: 0 };
  var lip = 0;
  var blinkStart = -1e9;
  var lastBlinkFlag = false;
  var lastStateAt = 0;
  var metrics = {};
  var fps = 0;
  var socket = null;
  var reconnectDelay = 500;

  // ---------------------------------------------------------------- helpers

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  function smoothstep(t) {
    t = clamp(t, 0, 1);
    return t * t * (3 - 2 * t);
  }

  function hexToRgb(hex) {
    var n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + alpha + ")";
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function paramsFor(emotion, intensity) {
    var e = table.emotions[emotion] || table.emotions.neutral;
    var base = table.emotions.neutral;
    var t = clamp(intensity === undefined ? 1 : intensity, 0, 1);
    var iris = hexToRgb(e.iris[THEME]);
    var neutralIris = hexToRgb(base.iris[THEME]);
    return {
      pupil: lerp(base.pupil, e.pupil, t),
      lid_top: lerp(base.lid_top, e.lid_top, t),
      lid_bot: lerp(base.lid_bot, e.lid_bot, t),
      brow: lerp(base.brow, e.brow, t),
      px: lerp(base.px, e.px, t),
      py: lerp(base.py, e.py, t),
      iris: [
        lerp(neutralIris[0], iris[0], t),
        lerp(neutralIris[1], iris[1], t),
        lerp(neutralIris[2], iris[2], t),
      ],
    };
  }

  function mix(a, b, t) {
    return {
      pupil: lerp(a.pupil, b.pupil, t),
      lid_top: lerp(a.lid_top, b.lid_top, t),
      lid_bot: lerp(a.lid_bot, b.lid_bot, t),
      brow: lerp(a.brow, b.brow, t),
      px: lerp(a.px, b.px, t),
      py: lerp(a.py, b.py, t),
      iris: [
        Math.round(lerp(a.iris[0], b.iris[0], t)),
        Math.round(lerp(a.iris[1], b.iris[1], t)),
        Math.round(lerp(a.iris[2], b.iris[2], t)),
      ],
    };
  }

  // ------------------------------------------------------------ face state

  function applyState(next) {
    var intensity = next.intensity === undefined ? 1 : next.intensity;
    var emotionChanged =
      next.emotion !== state.emotion || Math.abs(intensity - state.intensity) > 0.01;
    state = {
      emotion: next.emotion,
      intensity: intensity,
      gaze: next.gaze || { x: 0, y: 0 },
      lip: next.lip || 0,
      blink: !!next.blink,
      eyelids: next.eyelids || 0,
      talking: !!next.talking,
    };
    lastStateAt = performance.now();
    if (table && emotionChanged) {
      from = current ? current : paramsFor(state.emotion, state.intensity);
      to = paramsFor(state.emotion, state.intensity);
      transitionStart = performance.now();
    }
    if (state.blink && !lastBlinkFlag) {
      blinkStart = performance.now();
    }
    lastBlinkFlag = state.blink;
  }

  // --------------------------------------------------------------- drawing

  function resize() {
    var dpr = window.devicePixelRatio || 1;
    var w = Math.round(canvas.clientWidth * dpr);
    var h = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
  }

  function drawEye(cx, cy, eyeW, eyeH, p, lidTop, lidBot, glow) {
    ctx.save();
    ctx.beginPath();
    ctx.ellipse(cx, cy, eyeW, eyeH, 0, 0, Math.PI * 2);
    ctx.fillStyle = palette.eye;
    ctx.fill();
    ctx.clip();

    var px = cx + (p.px + gaze.x) * eyeW * 0.55;
    var py = cy - (p.py + gaze.y) * eyeH * 0.5;
    var pupilR = eyeW * 0.45 * p.pupil;

    if (pupilR > 0.5) {
      var g = ctx.createRadialGradient(px, py, pupilR * 0.4, px, py, pupilR * 2.1);
      g.addColorStop(0, rgba(p.iris, 0.4 * glow));
      g.addColorStop(1, rgba(p.iris, 0));
      ctx.fillStyle = g;
      ctx.fillRect(cx - eyeW, cy - eyeH, eyeW * 2, eyeH * 2);

      ctx.beginPath();
      ctx.arc(px, py, pupilR, 0, Math.PI * 2);
      ctx.fillStyle = rgba(p.iris, 1);
      ctx.fill();

      ctx.beginPath();
      ctx.arc(px + pupilR * 0.3, py - pupilR * 0.3, pupilR * 0.25, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      ctx.fill();
    }

    ctx.fillStyle = palette.bg;
    if (lidTop > 0.005) {
      ctx.fillRect(cx - eyeW, cy - eyeH, eyeW * 2, eyeH * 2 * lidTop);
    }
    if (lidBot > 0.005) {
      ctx.fillRect(cx - eyeW, cy + eyeH - eyeH * 2 * lidBot, eyeW * 2, eyeH * 2 * lidBot);
    }
    ctx.restore();

    if (palette.stroke_width > 0) {
      // Outline only the part of the eye the lids leave visible, then close it
      // with a lid line, so a half-closed eye reads as an eyelid and not as a
      // full eye crossed by a chord.
      var yTop = cy - eyeH + eyeH * 2 * lidTop;
      var yBot = cy + eyeH - eyeH * 2 * lidBot;
      ctx.save();
      ctx.lineWidth = palette.stroke_width;
      ctx.strokeStyle = palette.stroke;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.rect(cx - eyeW * 2, yTop, eyeW * 4, Math.max(0, yBot - yTop));
      ctx.clip();
      ctx.beginPath();
      ctx.ellipse(cx, cy, eyeW, eyeH, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.lineWidth = palette.stroke_width;
      ctx.strokeStyle = palette.stroke;
      ctx.lineCap = "round";
      if (lidTop > 0.005) {
        drawChord(ctx, cx, cy, eyeW, eyeH, yTop);
      }
      if (lidBot > 0.005) {
        drawChord(ctx, cx, cy, eyeW, eyeH, yBot);
      }
      ctx.restore();
    }
  }

  function drawChord(ctx, cx, cy, eyeW, eyeH, y) {
    var ratio = clamp((y - cy) / eyeH, -1, 1);
    var half = eyeW * Math.sqrt(1 - ratio * ratio);
    ctx.beginPath();
    ctx.moveTo(cx - half, y);
    ctx.lineTo(cx + half, y);
    ctx.stroke();
  }

  function drawBrow(cx, cy, eyeW, eyeH, angleDeg, side, color) {
    var y = cy - eyeH * 1.22;
    ctx.save();
    ctx.translate(cx, y);
    ctx.rotate(((angleDeg * side) * Math.PI) / 180);
    ctx.lineCap = "round";
    ctx.lineWidth = Math.max(3, eyeH * 0.09);
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(-eyeW * 0.8, 0);
    ctx.lineTo(eyeW * 0.8, 0);
    ctx.stroke();
    ctx.restore();
  }

  function drawMouth(cx, cy, w, opening, color) {
    var h = Math.max(w * 0.02, opening * w * 0.38);
    var r = Math.min(h / 2, w / 2);
    ctx.beginPath();
    if (ctx.roundRect) {
      ctx.roundRect(cx - w / 2, cy - h / 2, w, h, r);
    } else {
      ctx.rect(cx - w / 2, cy - h / 2, w, h);
    }
    ctx.fillStyle = color;
    ctx.fill();
  }

  var lastFrameAt = 0;

  function frame(now) {
    requestAnimationFrame(frame);
    if (!table) return;
    var dt = lastFrameAt ? now - lastFrameAt : 16;
    lastFrameAt = now;
    fps = fps ? fps * 0.9 + (1000 / Math.max(dt, 1)) * 0.1 : 1000 / Math.max(dt, 1);

    if (to) {
      current = mix(from, to, smoothstep((now - transitionStart) / TRANSITION_MS));
    } else if (!current) {
      current = paramsFor(state.emotion, state.intensity);
    }

    var kGaze = 1 - Math.exp(-dt / GAZE_TAU_MS);
    gaze.x += (clamp(state.gaze.x, -1, 1) - gaze.x) * kGaze;
    gaze.y += (clamp(state.gaze.y, -1, 1) - gaze.y) * kGaze;
    lip += (clamp(state.lip, 0, 1) - lip) * (1 - Math.exp(-dt / LIP_TAU_MS));

    var blinkT = (now - blinkStart) / BLINK_MS;
    var blinkClose = blinkT >= 0 && blinkT <= 1 ? Math.sin(Math.PI * blinkT) : 0;

    resize();
    var w = canvas.width;
    var h = canvas.height;
    ctx.fillStyle = palette.bg;
    ctx.fillRect(0, 0, w, h);

    var unit = Math.min(w, h);
    var eyeW = unit * 0.125;
    var eyeH = unit * 0.17;
    var spacing = unit * 0.21;
    var cy = h * 0.44;
    var glow = palette.glow * (state.talking ? 1.3 + 0.2 * Math.sin(now / 160) : 1);

    var lidTop = clamp(
      Math.max(current.lid_top + state.eyelids * 0.8, blinkClose * 0.98),
      0,
      1,
    );
    var lidBot = clamp(current.lid_bot, 0, 1);

    for (var i = 0; i < 2; i++) {
      var side = i === 0 ? -1 : 1;
      var cx = w / 2 + side * spacing;
      drawBrow(cx, cy, eyeW, eyeH, current.brow, side, rgba(current.iris, 0.85));
      drawEye(cx, cy, eyeW, eyeH, current, lidTop, lidBot, glow);
    }

    drawMouth(w / 2, cy + unit * 0.3, unit * 0.18, lip, palette.mouth);

    if (hudEl && !hudEl.hidden) {
      drawHud();
    }
  }

  function drawHud() {
    var lines = [
      "fps        " + fps.toFixed(0),
      "socket     " + (socket && socket.readyState === 1 ? "open" : "closed"),
      "state age  " + Math.round(performance.now() - lastStateAt) + " ms",
      "emotion    " + state.emotion + " @ " + state.intensity.toFixed(2),
      "gaze       " + gaze.x.toFixed(2) + ", " + gaze.y.toFixed(2),
      "lip        " + lip.toFixed(2) + (state.talking ? "  talking" : ""),
    ];
    Object.keys(metrics)
      .sort()
      .forEach(function (topic) {
        lines.push(topic + "  " + JSON.stringify(metrics[topic]));
      });
    hudEl.textContent = lines.join("\n");
  }

  // ------------------------------------------------------------- transport

  function wsUrl() {
    var url = new URL("ws", new URL(".", scriptUrl));
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    if (TOKEN) url.search = "?token=" + encodeURIComponent(TOKEN);
    return url.href;
  }

  function setStatus(text) {
    if (!statusEl) return;
    statusEl.textContent = text || "";
    statusEl.hidden = !text;
  }

  function connect() {
    socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";
    socket.onopen = function () {
      reconnectDelay = 500;
      setStatus("");
    };
    socket.onmessage = function (event) {
      if (typeof event.data !== "string") return;
      var envelope;
      try {
        envelope = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (envelope.topic === "face.state") {
        applyState(envelope.data);
      } else if (envelope.topic && envelope.topic.indexOf("metric.") === 0) {
        metrics[envelope.topic] = envelope.data;
      }
    };
    socket.onclose = function () {
      setStatus("reconnecting…");
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };
    socket.onerror = function () {
      socket.close();
    };
  }

  function send(payload) {
    if (socket && socket.readyState === 1) {
      socket.send(JSON.stringify(payload));
    }
  }

  // ---------------------------------------------------------------- camera

  var cameraTimer = null;
  var cameraStream = null;
  var frameSeq = 0;
  var grabCanvas = document.createElement("canvas");
  grabCanvas.width = CAMERA_W;
  grabCanvas.height = CAMERA_H;
  var grabCtx = grabCanvas.getContext("2d");

  /* contracts/frames.py: ver | tlen | seq (u16) | ts_ms (u32), big-endian,
     then the UTF-8 topic, then the JPEG. */
  function frameHeader(topic, tsMs) {
    var header = new Uint8Array(8 + topic.length);
    header[0] = FRAME_VERSION;
    header[1] = topic.length;
    var seq = frameSeq % 65536;
    header[2] = (seq >> 8) & 0xff;
    header[3] = seq & 0xff;
    var ms = Math.floor(tsMs) % 4294967296;
    for (var i = 7; i >= 4; i--) {
      header[i] = ms % 256;
      ms = Math.floor(ms / 256);
    }
    for (var j = 0; j < topic.length; j++) header[8 + j] = topic.charCodeAt(j);
    return header;
  }

  function grabFrame() {
    if (!socket || socket.readyState !== 1 || !video.videoWidth) return;
    grabCtx.drawImage(video, 0, 0, CAMERA_W, CAMERA_H);
    grabCanvas.toBlob(
      function (blob) {
        if (!blob) return;
        blob.arrayBuffer().then(function (buffer) {
          var header = frameHeader(FRAME_TOPIC_BROWSER, Date.now());
          frameSeq += 1;
          var message = new Uint8Array(header.length + buffer.byteLength);
          message.set(header, 0);
          message.set(new Uint8Array(buffer), header.length);
          if (socket && socket.readyState === 1) socket.send(message);
        });
      },
      "image/jpeg",
      0.6,
    );
  }

  function stopCamera() {
    if (cameraTimer) clearInterval(cameraTimer);
    cameraTimer = null;
    if (cameraStream) {
      cameraStream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    cameraStream = null;
  }

  function startCamera(button) {
    navigator.mediaDevices
      .getUserMedia({ video: { width: CAMERA_W, height: CAMERA_H, facingMode: "user" } })
      .then(function (stream) {
        cameraStream = stream;
        video.srcObject = stream;
        return video.play();
      })
      .then(function () {
        cameraTimer = setInterval(grabFrame, 1000 / CAMERA_FPS);
        button.textContent = "Camera on";
        button.setAttribute("aria-pressed", "true");
      })
      .catch(function (err) {
        setStatus("camera: " + err.name);
        button.textContent = "Camera off";
        button.setAttribute("aria-pressed", "false");
      });
  }

  // ------------------------------------------------------------ user input

  var wakeLock = null;

  function requestWakeLock() {
    if (!navigator.wakeLock) return;
    navigator.wakeLock
      .request("screen")
      .then(function (lock) {
        wakeLock = lock;
      })
      .catch(function () {});
  }

  function bindControls() {
    canvas.addEventListener("pointerdown", function () {
      send({ type: "tap", intensity: 1 });
      requestWakeLock();
      if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(function () {});
      }
    });

    var estop = document.getElementById("estop");
    if (estop) {
      estop.addEventListener("click", function (event) {
        event.stopPropagation();
        send({ type: "estop", reason: "face_page" });
      });
    }

    var cameraButton = document.getElementById("camera");
    if (cameraButton) {
      cameraButton.addEventListener("click", function (event) {
        event.stopPropagation();
        if (cameraStream) {
          stopCamera();
          cameraButton.textContent = "Camera off";
          cameraButton.setAttribute("aria-pressed", "false");
        } else {
          startCamera(cameraButton);
        }
      });
    }

    var hudButton = document.getElementById("hud-toggle");
    if (hudButton && hudEl) {
      hudEl.hidden = params.get("hud") !== "1";
      hudButton.setAttribute("aria-pressed", String(!hudEl.hidden));
      hudButton.addEventListener("click", function (event) {
        event.stopPropagation();
        hudEl.hidden = !hudEl.hidden;
        hudButton.setAttribute("aria-pressed", String(!hudEl.hidden));
      });
    }

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && wakeLock === null) requestWakeLock();
    });
  }

  // ------------------------------------------------------------- demo mode

  var DEMO_EMOTIONS = [
    "neutral",
    "happy",
    "curious",
    "excited",
    "love",
    "sad",
    "annoyed",
    "angry",
    "sleeping",
  ];

  function runDemo() {
    var started = performance.now();
    var pointer = { x: 0, y: 0 };
    window.addEventListener("pointermove", function (event) {
      pointer.x = clamp((event.clientX / window.innerWidth - 0.5) * 2, -1, 1);
      pointer.y = clamp(-(event.clientY / window.innerHeight - 0.5) * 2, -1, 1);
    });
    setInterval(function () {
      var elapsed = (performance.now() - started) / 1000;
      var emotion = DEMO_EMOTIONS[Math.floor(elapsed / 4) % DEMO_EMOTIONS.length];
      var talking = elapsed % 4 > 1.2 && elapsed % 4 < 2.8;
      applyState({
        emotion: emotion,
        intensity: 1,
        gaze: pointer,
        lip: talking ? Math.abs(Math.sin(elapsed * 9)) : 0,
        blink: elapsed % 4.5 < 0.06,
        eyelids: 0,
        talking: talking,
      });
    }, 50);
  }

  // ------------------------------------------------------------------ boot

  fetch(new URL("emotions.json", new URL(".", scriptUrl)).href)
    .then(function (response) {
      return response.json();
    })
    .then(function (loaded) {
      table = loaded;
      palette = table.themes[THEME];
      TRANSITION_MS = table.timing.transition_ms;
      BLINK_MS = table.timing.blink_ms;
      LIP_TAU_MS = table.timing.lip_smoothing_ms;
      current = paramsFor(state.emotion, state.intensity);
      bindControls();
      if (DEMO) {
        runDemo();
      } else {
        connect();
      }
    })
    .catch(function (err) {
      setStatus("emotions.json: " + err.message);
    });

  requestAnimationFrame(frame);

  window.asimoovFace = {
    render: applyState,
    theme: THEME,
  };
})();
