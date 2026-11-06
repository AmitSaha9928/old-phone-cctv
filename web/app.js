'use strict';
/* OldPhone CCTV viewer: multi-camera live (WebCodecs), listen/talk, motion alerts, recordings timeline. */

const $ = (id) => document.getElementById(id);
const S = {
  state: null,          // last server state
  cams: new Map(),      // slot -> per-camera live view
  focus: null,          // slot shown big, or null for the grid
  listening: false,
  talking: false,
  armedBusy: false,
};
const canDecode = 'VideoDecoder' in window && window.isSecureContext;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtGB = (g) => (g == null ? '?' : g < 1 ? Math.round(g * 1000) + ' MB' : g.toFixed(1) + ' GB');
const fmtDur = (s) => (s > 86400 ? Math.floor(s / 86400) + 'd ' : '') + Math.floor((s % 86400) / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
const camList = () => (S.state ? S.state.cameras : []);
const camBySlot = (slot) => camList().find((c) => c.slot === slot);
const camById = (id) => camList().find((c) => c.id === id);

// ------------------------------------------------------------------ tabs + toast
document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.id === 'tab-' + name));
  if (name === 'recordings') loadRecordings();
  if (name === 'settings') refreshAdb();
  try { localStorage.setItem('tab', name); } catch (e) {}
}
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3000);
}

// ------------------------------------------------------------------ server link
// One long HTTP response carries [u32 length][message] frames; commands and talk audio go up as POSTs.
// (Plain HTTP passes antivirus web filters that break WebSockets.)
let viewerId = null;
const dec = new TextDecoder();
async function connect() {
  try {
    const r = await fetch('/stream', { cache: 'no-store' });
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
    const rd = r.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      const nb = new Uint8Array(buf.length + value.length);
      nb.set(buf); nb.set(value, buf.length); buf = nb;
      let off = 0;
      while (buf.length - off >= 4) {
        const n = ((buf[off] << 24) | (buf[off + 1] << 16) | (buf[off + 2] << 8) | buf[off + 3]) >>> 0;
        if (buf.length - off - 4 < n) break;
        handleMsg(buf.subarray(off + 4, off + 4 + n));
        off += 4 + n;
      }
      buf = buf.slice(off);
    }
  } catch (e) { console.warn('stream', e); }
  viewerId = null;
  for (const v of S.cams.values()) setTileMsg(v, 'Lost the connection to the server. Reconnecting…');
  setTimeout(connect, 2000);
}
function handleMsg(d) {
  if (d[0] === 0) {
    const j = JSON.parse(dec.decode(d.subarray(1)));
    if (j.t === 'viewer') { viewerId = j.id; sendWatch(); if (S.listening && S.focus != null) send({ t: 'listen', cam: S.focus, on: true }); }
    else if (j.t === 'state') onState(j);
    else if (j.t === 'motion') onMotion(j);
    return;
  }
  const v = S.cams.get(d[1]);
  if (!v) return;
  if (d[0] === 1) onConfig(v, d.subarray(2));
  else if (d[0] === 2) onFrame(v, d);
  else if (d[0] === 3) onAudio(v, d.subarray(2));
}
function post(body, type, extra = '') {
  if (!viewerId) return;
  fetch('/api/viewer?id=' + viewerId + extra, { method: 'POST', headers: { 'Content-Type': type }, body }).catch(() => {});
}
function send(obj) { post(JSON.stringify(obj), 'application/json'); }
function sendWatch() { send({ t: 'watch', cams: S.focus == null ? null : [S.focus] }); }

// ------------------------------------------------------------------ live tiles (one decoder per camera)
function tileFor(cam) {
  let v = S.cams.get(cam.slot);
  if (v) return v;
  const el = document.createElement('div');
  el.className = 'tile';
  el.innerHTML = `<canvas></canvas>
    <div class="overlay tl"><span class="live-dot"></span><span class="tname"></span></div>
    <div class="overlay tr tstats"></div>
    <div class="motion-badge" hidden>Motion</div>
    <div class="offline"><div class="spinner"></div><p class="tmsg">Connecting…</p></div>
    <button class="fs" title="Full screen" aria-label="Full screen"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></button>`;
  v = { slot: cam.slot, el, canvas: el.querySelector('canvas'), decoder: null, codec: null, waitKey: true,
        rotate: 0, lastFrameAt: 0, frames: 0 };
  v.ctx = v.canvas.getContext('2d');
  el.addEventListener('click', (e) => {
    if (e.target.closest('.fs')) return;
    if (S.focus == null && camList().length > 1) setFocus(cam.slot);
  });
  el.querySelector('.fs').addEventListener('click', (e) => {
    e.stopPropagation();
    if (document.fullscreenElement) return document.exitFullscreen();
    if (el.requestFullscreen) el.requestFullscreen().catch(() => el.classList.toggle('theater'));
    else el.classList.toggle('theater');
  });
  S.cams.set(cam.slot, v);
  $('grid').appendChild(el);
  return v;
}
function setTileMsg(v, msg) {
  v.el.querySelector('.tmsg').textContent = msg;
  v.el.querySelector('.offline').hidden = false;
}
function setFocus(slot) {
  S.focus = slot;
  try { if (slot == null) localStorage.removeItem('focus'); else localStorage.setItem('focus', camBySlot(slot)?.id || ''); } catch (e) {}
  if (S.listening) { send({ t: 'listen', cam: slot, on: slot != null }); }
  sendWatch();
  layout();
}
$('btnAll').addEventListener('click', () => setFocus(null));

function layout() {
  const cams = camList();
  const single = cams.length === 1 ? cams[0].slot : S.focus;
  const grid = $('grid');
  grid.classList.toggle('single', single != null);
  grid.classList.toggle('multi', single == null && cams.length > 1);
  for (const v of S.cams.values()) v.el.hidden = single != null && v.slot !== single;
  $('focusBar').hidden = !(S.focus != null && cams.length > 1);
  $('focusControls').hidden = single == null;
  $('emptyLive').hidden = cams.length > 0;
  renderFocusControls();
}
const focused = () => { const cams = camList(); return cams.length === 1 ? cams[0] : camBySlot(S.focus); };

function codecFromConfig(cfg) {
  for (let i = 0; i + 4 < cfg.length; i++) {
    if (cfg[i] === 0 && cfg[i + 1] === 0 && cfg[i + 2] === 1 && (cfg[i + 3] & 0x1f) === 7) {
      const h = (x) => x.toString(16).padStart(2, '0');
      return 'avc1.' + h(cfg[i + 4]) + h(cfg[i + 5]) + h(cfg[i + 6]);
    }
  }
  return null;
}
function onConfig(v, cfg) {
  const c = codecFromConfig(cfg);
  if (!c || !canDecode) return;
  if (c !== v.codec || !v.decoder || v.decoder.state === 'closed') { v.codec = c; makeDecoder(v); }
}
function makeDecoder(v) {
  try { if (v.decoder && v.decoder.state !== 'closed') v.decoder.close(); } catch (e) {}
  v.decoder = new VideoDecoder({
    output: (f) => drawFrame(v, f),
    error: (e) => { console.warn('decoder', e); v.waitKey = true; setTimeout(() => makeDecoder(v), 300); send({ t: 'key', cam: v.slot }); },
  });
  v.decoder.configure({ codec: v.codec, optimizeForLatency: true });
  v.waitKey = true;
}
function onFrame(v, d) {
  if (!v.decoder || v.decoder.state !== 'configured') return;
  const key = (d[2] & 1) === 1;
  const pts = Number(new DataView(d.buffer, d.byteOffset).getBigInt64(3));
  if (v.waitKey && !key) return;
  if (v.decoder.decodeQueueSize > 8 && !key) { v.waitKey = true; send({ t: 'key', cam: v.slot }); return; }
  v.waitKey = false;
  try { v.decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: d.subarray(11) })); }
  catch (e) { v.waitKey = true; send({ t: 'key', cam: v.slot }); }
}
function drawFrame(v, frame) {
  const r = v.rotate, w = frame.displayWidth, h = frame.displayHeight;
  const cw = r % 180 ? h : w, ch = r % 180 ? w : h;
  if (v.canvas.width !== cw || v.canvas.height !== ch) {
    v.canvas.width = cw; v.canvas.height = ch;
    v.el.classList.toggle('portrait', ch > cw);
  }
  v.ctx.save();
  v.ctx.translate(cw / 2, ch / 2);
  v.ctx.rotate((r * Math.PI) / 180);
  v.ctx.drawImage(frame, -w / 2, -h / 2, w, h);
  v.ctx.restore();
  frame.close();
  v.lastFrameAt = performance.now();
  v.frames++;
  v.el.querySelector('.offline').hidden = true;
}
setInterval(() => {
  const now = new Date().toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  for (const v of S.cams.values()) {
    const cam = camBySlot(v.slot);
    if (!cam) continue;
    v.el.querySelector('.tname').textContent = (camList().length > 1 ? cam.name + ' · ' : '') + now;
    if (cam.connected && v.lastFrameAt && performance.now() - v.lastFrameAt > 6000 && !v.el.hidden) {
      setTileMsg(v, 'Waiting for video…');
      send({ t: 'key', cam: v.slot });
    }
  }
}, 1000);

// ------------------------------------------------------------------ audio out (listen)
let actx = null, nextT = 0;
function audioCtx() {
  if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state === 'suspended') actx.resume();
  return actx;
}
function onAudio(v, pcm) {
  if (!S.listening || S.talking || !actx) return;
  const n = pcm.length >> 1;
  const buf = actx.createBuffer(1, n, 16000);
  const ch = buf.getChannelData(0);
  const dv = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
  const src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(actx.destination);
  const now = actx.currentTime;
  if (nextT < now + 0.05 || nextT > now + 0.8) nextT = now + 0.15;
  src.start(nextT);
  nextT += buf.duration;
}
$('btnListen').addEventListener('click', () => {
  const cam = focused();
  if (!cam) return;
  S.listening = !S.listening;
  if (S.listening) audioCtx();
  $('btnListen').setAttribute('aria-pressed', S.listening);
  send({ t: 'listen', cam: cam.slot, on: S.listening });
});

// ------------------------------------------------------------------ talk (your mic -> phone speaker)
let mic = null, micNode = null, micSrc = null, talkSlot = null;
async function startTalk() {
  const cam = focused();
  if (S.talking || !cam) return;
  if (!window.isSecureContext || !navigator.mediaDevices) {
    toast('Talking needs a secure page: use localhost on the PC or the Tailscale https:// address');
    return;
  }
  S.talking = true; talkSlot = cam.slot;
  $('btnTalk').classList.add('on');
  $('talkLabel').textContent = 'Talking…';
  try {
    const ctx = audioCtx();
    if (!startTalk.loaded) { await ctx.audioWorklet.addModule('talk-worklet.js'); startTalk.loaded = true; }
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    if (!S.talking) return stopTalk();
    micSrc = ctx.createMediaStreamSource(mic);
    micNode = new AudioWorkletNode(ctx, 'talk-capture');
    let batch = [];
    micNode.port.onmessage = (e) => {
      if (!S.talking) return;
      batch.push(new Uint8Array(e.data));
      if (batch.length < 3) return; // 120 ms per request
      const out = new Uint8Array(batch.reduce((n, b) => n + b.length, 0));
      let o = 0;
      for (const b of batch) { out.set(b, o); o += b.length; }
      batch = [];
      post(out, 'application/octet-stream', '&cam=' + talkSlot);
    };
    const mute = ctx.createGain(); mute.gain.value = 0;
    micSrc.connect(micNode).connect(mute).connect(ctx.destination);
    send({ t: 'talk', cam: talkSlot, on: true });
  } catch (e) {
    toast('Microphone blocked: ' + e.message);
    stopTalk();
  }
}
function stopTalk() {
  if (!S.talking && !mic) return;
  S.talking = false; latched = false;
  $('btnTalk').classList.remove('on');
  $('talkLabel').textContent = 'Hold to talk';
  if (mic) mic.getTracks().forEach((t) => t.stop());
  try { micSrc && micSrc.disconnect(); micNode && micNode.disconnect(); } catch (e) {}
  mic = micSrc = null;
  send({ t: 'talk', cam: talkSlot, on: false });
}
let pressAt = 0, latched = false;
const talkBtn = $('btnTalk');
talkBtn.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  talkBtn.setPointerCapture(e.pointerId);
  if (latched) { stopTalk(); return; }
  pressAt = Date.now();
  startTalk();
});
talkBtn.addEventListener('pointerup', () => {
  if (!S.talking) return;
  if (Date.now() - pressAt < 300) { latched = true; $('talkLabel').textContent = 'Tap to stop'; }
  else stopTalk();
});
talkBtn.addEventListener('pointercancel', () => { if (!latched) stopTalk(); });
talkBtn.addEventListener('contextmenu', (e) => e.preventDefault());

// ------------------------------------------------------------------ snapshot, rotate, torch, lens
$('btnSnap').addEventListener('click', () => {
  const cam = focused(); const v = cam && S.cams.get(cam.slot);
  if (!v || !v.canvas.width) return toast('No picture yet');
  v.canvas.toBlob((b) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = `${cam.name}-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }, 'image/png');
});
$('btnRotate').addEventListener('click', () => {
  const cam = focused(); if (!cam) return;
  const v = S.cams.get(cam.slot);
  const next = (cam.rotate + 90) % 360;
  v.rotate = next; v.canvas.width = 0;
  saveSettings({ cam: cam.id, rotate: next });
});
$('btnTorch').addEventListener('click', () => {
  const cam = focused(); if (!cam) return;
  const on = $('btnTorch').getAttribute('aria-pressed') !== 'true';
  $('btnTorch').setAttribute('aria-pressed', on);
  saveSettings({ cam: cam.id, torch: on });
});
const lensLabel = (c) => (c.facing === 'front' ? 'Front' : c.facing === 'back' ? 'Rear' : 'Camera ' + c.id);
function activeLens(hello) {
  const lenses = hello.cameras || [], want = hello.cfg && String(hello.cfg.camera);
  return lenses.find((c) => c.id === want) || lenses.find((c) => c.facing === want) || lenses[0];
}
function renderFocusControls() {
  const cam = focused();
  const box = $('camSwitch');
  if (!cam || !cam.hello) { box.innerHTML = ''; return; }
  const lenses = cam.hello.cameras || [];
  const cur = activeLens(cam.hello);
  const icon = (c) => c.facing === 'front'
    ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M4 8h3l2-2.5h6L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>';
  box.innerHTML = lenses.length > 1 ? lenses.map((c) => `<button role="radio" aria-checked="${cur && c.id === cur.id}"
    class="${cur && c.id === cur.id ? 'on' : ''}" data-id="${c.id}">${icon(c)}${lensLabel(c)}${c.mp ? ` <small>${Math.round(c.mp)} MP</small>` : ''}</button>`).join('') : '';
  box.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    if (cur && b.dataset.id === cur.id) return;
    setTileMsg(S.cams.get(cam.slot), 'Switching camera…');
    saveSettings({ cam: cam.id, camera: b.dataset.id });
  }));
  $('btnTorch').setAttribute('aria-pressed', !!(cam.hello.cfg && cam.hello.cfg.torch));
  $('btnTalk').disabled = !cam.connected;
}

// ------------------------------------------------------------------ state from server
function onState(s) {
  S.state = s;
  $('appName').textContent = s.name || 'OldPhone CCTV';
  document.title = s.name || 'OldPhone CCTV';
  const cams = s.cameras;
  // restore a remembered focus once cameras are known
  if (S.focus == null && !onState.restored && cams.length > 1) {
    onState.restored = true;
    try { const id = localStorage.getItem('focus'); const c = id && cams.find((x) => x.id === id); if (c) { S.focus = c.slot; sendWatch(); } } catch (e) {}
  }
  for (const cam of cams) {
    const v = tileFor(cam);
    v.rotate = Number(cam.rotate) || 0;
    const st = cam.status || {};
    v.el.querySelector('.tstats').textContent = cam.connected && st.fps != null
      ? `${(cam.hello?.video || '').split(' ')[0]} · ${st.fps.toFixed(0)} fps${st.battery != null ? ' · ' + (st.charging ? '⚡' : '') + st.battery + '%' : ''}` : '';
    v.el.querySelector('.motion-badge').hidden = !cam.motion;
    v.el.classList.toggle('moving', !!cam.motion);
    if (!cam.connected) setTileMsg(v, cam.error || 'Waiting for this camera…');
    else if (st.cam === 'error') setTileMsg(v, 'Camera problem: ' + (st.error || 'unknown') + ' — retrying');
  }
  for (const [slot, v] of S.cams) if (!cams.find((c) => c.slot === slot)) { v.el.remove(); S.cams.delete(slot); }
  if (S.focus != null && !camBySlot(S.focus)) setFocus(null);

  const online = cams.filter((c) => c.connected).length;
  const live = $('pillLive');
  live.className = 'pill ' + (online ? 'live' : 'bad');
  live.querySelector('b').textContent = !cams.length ? 'No cameras' : online === cams.length ? (cams.length > 1 ? `${online} live` : 'Live') : `${online}/${cams.length} live`;
  $('pillRec').hidden = !cams.some((c) => c.rec.active);
  const armed = $('btnArmed');
  armed.classList.toggle('off', !s.armed);
  armed.querySelector('b').textContent = s.armed ? 'Armed' : 'Disarmed';

  const notes = [];
  if (!canDecode) notes.push(window.isSecureContext ? 'This browser can’t decode the live video. Use Chrome, Edge or Safari 16.4+.'
    : 'Live video and talking need a secure page. On the PC open http://localhost:' + (location.port || 80) + ', elsewhere use the Tailscale https:// address.');
  const fc = focused();
  if (fc && fc.talking && !S.talking) notes.push('Someone else is talking through this camera right now.');
  $('liveNote').hidden = !notes.length;
  $('liveNote').textContent = notes.join(' ');
  layout();
  fillSettings();
  if ($('tab-recordings').classList.contains('on')) renderRecCams();
}

$('btnArmed').addEventListener('click', () => {
  if (!S.state) return;
  saveSettings({ armed: !S.state.armed }, S.state.armed ? 'Disarmed: no alerts' : 'Armed: alerts on');
});

// ------------------------------------------------------------------ motion notifications
function onMotion(j) {
  const msg = `Motion: ${j.name} at ${new Date(j.time * 1000).toLocaleTimeString()}`;
  toast(msg);
  if (document.hidden && 'Notification' in window && Notification.permission === 'granted' && S.state?.armed) {
    try { new Notification(msg, { body: 'Tap to open OldPhone CCTV', icon: j.snap || 'icon.svg', tag: 'motion-' + j.cam }); } catch (e) {}
  }
  if ($('tab-recordings').classList.contains('on')) setTimeout(loadRecordings, 2500);
}
$('btnBrowserNotify').addEventListener('click', async () => {
  if (!('Notification' in window)) return toast('This browser has no notifications');
  const p = await Notification.requestPermission();
  toast(p === 'granted' ? 'This browser will notify you while the viewer is open' : 'Notifications are blocked for this site');
});

// ------------------------------------------------------------------ settings
let setCam = null;   // camera id being edited
async function saveSettings(obj, okMsg = 'Saved') {
  try {
    const r = await fetch('/api/settings', { method: 'POST', body: JSON.stringify(obj) });
    const j = await r.json();
    if (!j.ok) toast(j.error || 'Could not save'); else toast(okMsg);
    return j.ok;
  } catch (e) { toast('Could not save: ' + e.message); return false; }
}
function fillSettings() {
  const s = S.state; if (!s) return;
  const cams = s.cameras;
  if (!setCam || !camById(setCam)) setCam = cams[0]?.id || null;
  $('setCams').innerHTML = cams.map((c) => `<button class="${c.id === setCam ? 'on' : ''}" data-id="${esc(c.id)}">
    <i class="dot ${c.connected ? 'ok' : ''}"></i>${esc(c.name)}</button>`).join('') || '<span class="dim">No cameras yet</span>';
  $('setCams').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { setCam = b.dataset.id; fillSettings(); }));
  $('camSettings').hidden = !setCam;
  const busy = document.activeElement && document.activeElement.closest('#tab-settings input[type=text], #tab-settings select');
  const cam = camById(setCam);
  if (cam && !busy) {
    const cfg = cam.hello?.cfg;
    $('setName').value = cam.name;
    const lenses = cam.hello?.cameras || [];
    $('setCamera').innerHTML = lenses.map((l) => `<option value="${l.id}">${lensLabel(l)}${l.mp ? ' · ' + Math.round(l.mp) + ' MP' : ''}</option>`).join('');
    if (cam.hello) $('setCamera').value = activeLens(cam.hello)?.id || '';
    if (cfg) {
      $('setQuality').value = cfg.width + 'x' + cfg.height;
      $('setFps').value = String(cfg.fps);
      $('setBitrate').value = String(cfg.bitrate);
      $('setAudio').checked = !!cfg.audio;
      $('setVolume').value = cfg.volume;
      $('setMotion').checked = cfg.motion !== false;
      $('setSens').value = cfg.sensitivity ?? 50;
    }
    $('setRotate').value = String(cam.rotate || 0);
    $('setRecord').checked = !!cam.rec.enabled;
    $('setCamAlerts').checked = cam.alerts !== false;
    for (const id of ['setCamera', 'setQuality', 'setFps', 'setBitrate', 'setAudio', 'setVolume', 'setMotion', 'setSens']) $(id).disabled = !cam.connected;
    const mh = $('motionHint');
    mh.hidden = !(cam.connected && cam.hello && cfg?.motion !== false && cam.hello.motionAvailable === false);
    mh.textContent = 'This phone’s camera can’t run motion detection alongside video, so motion alerts are off for it.';
    $('btnRemove').hidden = cam.connected;
  }
  if (!busy) {
    $('setArmed').checked = !!s.armed;
    $('setNtfy').value = s.alerts.ntfy_url || '';
    $('setTgChat').value = s.alerts.telegram_chat || '';
    $('setTgToken').placeholder = s.alerts.telegram_set ? '(saved, type to replace)' : '123456:ABC… from @BotFather';
    $('setCooldown').value = String(s.alerts.cooldown_s || 60);
    $('setViewerUrl').value = s.alerts.viewer_url || (location.protocol === 'https:' ? location.origin : '');
    $('setRetention').value = String(s.rec.retention_hours);
    $('setDir').value = s.rec.dir || '';
  }
  const total = (s.rec.used_gb || 0) + (s.rec.free_gb || 0);
  $('storageBar').style.width = total ? Math.min(100, (100 * s.rec.used_gb) / total) + '%' : '0';
  $('storageText').textContent = `Recordings use ${fmtGB(s.rec.used_gb)} · ${fmtGB(s.rec.free_gb)} free · clips every ${s.rec.segment_minutes} min`;
  const errs = cams.filter((c) => c.rec.error).map((c) => `${c.name}: ${c.rec.error}`);
  if (!s.ffmpeg) errs.unshift('FFmpeg not found: live view works, recording is off. Run setup.ps1.');
  $('recError').hidden = !errs.length;
  $('recError').textContent = errs.join(' · ');
  const fc = cam || cams[0];
  const st = fc?.status || {}, h = fc?.hello || {};
  const rows = [['Version', s.version], ['Cameras', cams.length], ['Viewers', s.viewers],
    ['Phone', h.model || '—'], ['Android', h.android || '—'], ['Address', fc?.addr || '—'],
    ['Wi-Fi signal', st.rssi != null ? st.rssi + ' dBm' : '—'], ['Phone uptime', st.uptime != null ? fmtDur(st.uptime) : '—']];
  $('aboutInfo').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
}
const camSet = (obj) => setCam && saveSettings({ cam: setCam, ...obj });
$('btnName').addEventListener('click', () => camSet({ name: $('setName').value.trim() }));
$('setCamera').addEventListener('change', (e) => camSet({ camera: e.target.value }));
$('setQuality').addEventListener('change', (e) => { const [w, h] = e.target.value.split('x').map(Number); camSet({ width: w, height: h }); });
$('setFps').addEventListener('change', (e) => camSet({ fps: Number(e.target.value) }));
$('setBitrate').addEventListener('change', (e) => camSet({ bitrate: Number(e.target.value) }));
$('setRotate').addEventListener('change', (e) => camSet({ rotate: Number(e.target.value) }));
$('setRecord').addEventListener('change', (e) => camSet({ record: e.target.checked }));
$('setAudio').addEventListener('change', (e) => camSet({ audio: e.target.checked }));
$('setVolume').addEventListener('change', (e) => camSet({ volume: Number(e.target.value) }));
$('setMotion').addEventListener('change', (e) => camSet({ motion: e.target.checked }));
$('setSens').addEventListener('change', (e) => camSet({ sensitivity: Number(e.target.value) }));
$('setCamAlerts').addEventListener('change', (e) => camSet({ alerts: e.target.checked }));
$('btnRemove').addEventListener('click', async () => {
  const cam = camById(setCam);
  if (!cam || !confirm(`Remove "${cam.name}" from the list? Its recordings stay on disk.`)) return;
  const j = await (await fetch('/api/camera/remove', { method: 'POST', body: JSON.stringify({ id: cam.id }) })).json();
  toast(j.ok ? 'Removed' : j.error);
});
$('setArmed').addEventListener('change', (e) => saveSettings({ armed: e.target.checked }));
$('setRetention').addEventListener('change', (e) => saveSettings({ retention_hours: Number(e.target.value) }));
$('btnDir').addEventListener('click', () => {
  const d = $('setDir').value.trim();
  if (!d) return toast('Type a folder, e.g. D:\\CCTV');
  saveSettings({ recordings_dir: d });
});
$('btnNtfyNew').addEventListener('click', () => {
  const abc = 'abcdefghijkmnpqrstuvwxyz23456789';
  const rnd = Array.from(crypto.getRandomValues(new Uint8Array(14)), (b) => abc[b % abc.length]).join('');
  $('setNtfy').value = 'https://ntfy.sh/oldphonecctv-' + rnd;
  toast('New private topic. Press Save, then subscribe to it in the ntfy app.');
});
$('btnAlertsSave').addEventListener('click', () => {
  const a = { ntfy_url: $('setNtfy').value.trim(), telegram_chat: $('setTgChat').value.trim(),
    cooldown_s: Number($('setCooldown').value), viewer_url: $('setViewerUrl').value.trim() };
  if ($('setTgToken').value.trim()) a.telegram_token = $('setTgToken').value.trim();
  saveSettings({ alerts: a }, 'Alert settings saved');
});
$('btnAlertsTest').addEventListener('click', async () => {
  toast('Sending…');
  const j = await (await fetch('/api/alerts/test', { method: 'POST', body: '{}' })).json();
  toast(j.ok ? 'Test alert sent. Check your phone' : (j.error || (j.results || []).join(' · ')));
});

// phones on USB
async function refreshAdb() {
  try {
    const j = await (await fetch('/api/phone', { method: 'POST', body: JSON.stringify({ action: 'devices' }) })).json();
    const sel = $('adbDevices'), prev = sel.value;
    sel.innerHTML = (j.devices || []).map((d) => `<option value="${esc(d.serial)}">${esc(d.model)} (${esc(d.serial)})</option>`).join('')
      || '<option value="">— none found —</option>';
    if (prev) sel.value = prev;
  } catch (e) {}
}
$('btnAdbRefresh').addEventListener('click', refreshAdb);
async function phoneLog(title, url, body) {
  const log = $('modeLog');
  log.hidden = false;
  log.textContent = title + '\n';
  document.querySelectorAll('.mode, #btnInstall').forEach((b) => (b.disabled = true));
  try {
    const j = await (await fetch(url, { method: 'POST', body: JSON.stringify(body) })).json();
    log.textContent += (j.output || j.error || '') + '\n' + (j.ok ? 'Done.' : 'Failed.');
    return j.ok;
  } catch (e) { log.textContent += e.message; return false; }
  finally { document.querySelectorAll('.mode, #btnInstall').forEach((b) => (b.disabled = false)); }
}
document.querySelectorAll('.mode').forEach((b) => b.addEventListener('click', async () => {
  const m = b.dataset.mode, serial = $('adbDevices').value;
  if (!serial) return toast('Plug a phone in by USB first (then Refresh)');
  const msg = m === 'surveillance'
    ? 'Switch this phone to Surveillance mode? Other apps are disabled (not deleted) until you switch back to Normal.'
    : 'Switch this phone back to Normal mode? Apps and settings are restored and its camera stops.';
  if (confirm(msg) && await phoneLog('Switching to ' + m + ' mode…', '/api/mode', { mode: m, serial })) toast(m === 'surveillance' ? 'Surveillance mode on' : 'Normal mode restored');
}));
$('btnPreview').addEventListener('click', () => {
  const serial = $('adbDevices').value;
  if (!serial) return toast('Plug a phone in by USB first (then Refresh)');
  phoneLog('Checking what Surveillance mode would change…', '/api/mode', { mode: 'surveillance', dry_run: true, serial });
});
$('btnInstall').addEventListener('click', () => phoneLog('Installing the camera app over USB…', '/api/phone', { action: 'install', serial: $('adbDevices').value || null }));

// ------------------------------------------------------------------ recordings: one continuous timeline per camera/day
let recCam = null, clips = [], events = [], dayKey = null, dayStart = 0;
const view = { start: 0, span: 86400 };
const rec = { t: null, clip: null, playing: false, sel: null, pendingSeek: null };
const pv = $('player');
const track = $('track');
const dayOf = (t) => { const d = new Date(t * 1000); return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); };
const midnight = (t) => { const d = new Date(t * 1000); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
const clock = (t, sec = true) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: sec ? '2-digit' : undefined, hour12: false });
const clipEnd = (c) => (c.recording ? Date.now() / 1000 : c.end);

function renderRecCams() {
  const cams = camList();
  if (!recCam || !camById(recCam)) recCam = cams[0]?.id || null;
  $('recCams').hidden = cams.length < 2;
  $('recCams').innerHTML = cams.map((c) => `<button class="${c.id === recCam ? 'on' : ''}" data-id="${esc(c.id)}">${esc(c.name)}</button>`).join('');
  $('recCams').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    recCam = b.dataset.id; rec.clip = null; rec.t = null; pv.removeAttribute('src'); $('pvEmpty').hidden = false; dayKey = null;
    loadRecordings();
  }));
}

async function loadRecordings() {
  renderRecCams();
  if (!recCam) { clips = []; events = []; } else {
    try { clips = (await (await fetch('/api/recordings?cam=' + encodeURIComponent(recCam))).json()).clips; } catch (e) { clips = []; }
  }
  const days = [...new Set(clips.map((c) => dayOf(c.start)))].reverse();
  if (rec.pendingSeek) { const d = dayOf(rec.pendingSeek); if (days.includes(d)) setDay(d); }
  if (!days.includes(dayKey)) setDay(days[0] || null);
  const today = dayOf(Date.now() / 1000), yest = dayOf(Date.now() / 1000 - 86400);
  $('days').innerHTML = days.map((d) => {
    const c = clips.find((x) => dayOf(x.start) === d);
    const label = d === today ? 'Today' : d === yest ? 'Yesterday' : new Date(c.start * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
    return `<button data-day="${d}" class="${d === dayKey ? 'on' : ''}">${label}</button>`;
  }).join('');
  $('days').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { setDay(b.dataset.day); loadRecordings(); }));
  $('noClips').hidden = clips.length > 0;
  try { events = recCam ? (await (await fetch(`/api/events?cam=${encodeURIComponent(recCam)}&start=${dayStart}&end=${dayStart + 86400}`)).json()).events : []; }
  catch (e) { events = []; }
  renderTimeline();
  renderEvents();
  if (rec.pendingSeek) { const t = rec.pendingSeek; rec.pendingSeek = null; seekTo(t, true); }
}

function setDay(key) {
  dayKey = key;
  const c = clips.find((x) => dayOf(x.start) === key);
  dayStart = c ? midnight(c.start) : midnight(Date.now() / 1000);
  view.start = dayStart; view.span = 86400;
  document.querySelectorAll('#zoom button').forEach((b) => b.classList.toggle('on', b.dataset.span === '86400'));
  rec.sel = null;
  syncExportInputs();
}

const pct = (t) => ((t - view.start) / view.span) * 100;
function renderTimeline() {
  track.querySelectorAll('.seg, .ev').forEach((s) => s.remove());
  const vEnd = view.start + view.span;
  for (const c of clips) {
    const e = clipEnd(c);
    if (e < view.start || c.start > vEnd) continue;
    const seg = document.createElement('div');
    seg.className = 'seg' + (c.recording ? ' live' : '');
    const a = Math.max(c.start, view.start), b = Math.min(e, vEnd);
    seg.style.left = pct(a) + '%';
    seg.style.width = Math.max(0.15, pct(b) - pct(a)) + '%';
    track.prepend(seg);
  }
  for (const ev of events) {
    if (ev.start < view.start || ev.start > vEnd) continue;
    const m = document.createElement('div');
    m.className = 'ev';
    m.style.left = pct(ev.start) + '%';
    m.style.width = Math.max(0.3, pct(Math.min(ev.end || ev.start + 10, vEnd)) - pct(ev.start)) + '%';
    track.appendChild(m);
  }
  const step = view.span >= 86400 ? 3 * 3600 : view.span >= 21600 ? 3600 : view.span >= 3600 ? 600 : 60;
  let html = '';
  for (let t = Math.ceil(view.start / step) * step; t <= vEnd; t += step) {
    html += `<span style="left:${pct(t)}%">${t - dayStart >= 86400 ? '24:00' : clock(t, false)}</span>`;
  }
  $('ticks').innerHTML = html;
  renderCursor();
  renderSel();
}
function renderEvents() {
  $('eventsCard').hidden = !events.length;
  $('eventCount').textContent = events.length ? `· ${events.length} today` : '';
  $('events').innerHTML = events.slice().reverse().map((ev) => `<button class="event" data-t="${ev.start}">
      ${ev.snap ? `<img loading="lazy" src="/snap/${encodeURIComponent(recCam)}/${encodeURIComponent(ev.snap)}" alt="">` : '<span class="nosnap"></span>'}
      <b>${clock(ev.start)}</b><span>${ev.end ? Math.max(1, Math.round(ev.end - ev.start)) + ' s' : 'now'}</span></button>`).join('');
  $('events').querySelectorAll('.event').forEach((b) => b.addEventListener('click', () => seekTo(Number(b.dataset.t) - 5, true)));
}
function renderCursor() {
  const cur = $('tlCursor');
  if (rec.t == null || rec.t < view.start || rec.t > view.start + view.span) { cur.hidden = true; return; }
  cur.hidden = false;
  cur.style.left = pct(rec.t) + '%';
  $('cursorLabel').textContent = clock(rec.t);
  $('pvTime').textContent = new Date(rec.t * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' }) + '  ' + clock(rec.t);
}
function renderSel() {
  const s = $('tlSel');
  if (!rec.sel) { s.hidden = true; return; }
  const a = Math.min(rec.sel.a, rec.sel.b), b = Math.max(rec.sel.a, rec.sel.b);
  s.hidden = false;
  s.style.left = pct(a) + '%';
  s.style.width = Math.max(0.2, pct(b) - pct(a)) + '%';
}
// The clip being recorded right now is playable too: the server hands out a fresh copy of it.
function clipAt(t) {
  return clips.find((c) => t >= c.start && t < clipEnd(c)) || clips.find((c) => c.start >= t);
}
let loadToken = 0;
function seekTo(t, play) {
  const c = clipAt(t);
  if (!c) { toast('No footage at that point yet'); return; }
  t = Math.max(t, c.start);
  rec.t = t;
  $('pvEmpty').hidden = true;
  const loadedLiveTooShort = c.recording && rec.clip && rec.clip.name === c.name && t - c.start > pv.duration - 0.5;
  if (rec.clip && rec.clip.name === c.name && pv.readyState >= 1 && !loadedLiveTooShort) {
    pv.currentTime = Math.min(t - c.start, Math.max(0, pv.duration - 0.2));
    if (play) pv.play().catch(() => {});
  } else {
    rec.clip = c;
    const token = ++loadToken;
    pv.src = `/rec/${encodeURIComponent(recCam)}/${encodeURIComponent(c.name)}` + (c.recording ? '?v=' + Date.now() : '');
    pv.onloadedmetadata = () => {
      if (token !== loadToken) return;
      pv.currentTime = Math.min(t - c.start, Math.max(0, pv.duration - 0.2));
      pv.playbackRate = Number($('speed').value);
      if (play) pv.play().catch(() => {});
    };
  }
  followView();
  renderCursor();
}
function followView() {
  if (rec.t == null || view.span >= 86400) return;
  if (rec.t < view.start || rec.t > view.start + view.span) {
    view.start = Math.max(dayStart, Math.min(dayStart + 86400 - view.span, rec.t - view.span * 0.1));
    renderTimeline();
  }
}
pv.addEventListener('timeupdate', () => {
  if (!rec.clip || dragging) return;
  rec.t = rec.clip.start + pv.currentTime;
  followView();
  renderCursor();
});
pv.addEventListener('ended', async () => {
  if (!rec.clip) return;
  if (rec.clip.recording) {
    // Caught up with the live clip: fetch what has been recorded since and carry on.
    const at = rec.t;
    await loadRecordings();
    if (Date.now() / 1000 - at > 4) seekTo(at, true); else setPlayIcon(false);
    return;
  }
  const next = clips.find((c) => c.start >= rec.clip.end - 3 && c.name > rec.clip.name);
  if (next) seekTo(next.start, true); else setPlayIcon(false);
});
pv.addEventListener('play', () => setPlayIcon(true));
pv.addEventListener('pause', () => setPlayIcon(false));
function setPlayIcon(on) {
  rec.playing = on;
  $('playIcon').innerHTML = on ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>' : '<path d="M8 5v14l11-7z"/>';
}
let dragging = false, dragMode = null, wasPlaying = false, pendingT = null;
const tFromX = (x) => { const r = track.getBoundingClientRect(); return view.start + Math.max(0, Math.min(1, (x - r.left) / r.width)) * view.span; };
track.addEventListener('pointerdown', (e) => {
  track.setPointerCapture(e.pointerId);
  const t = tFromX(e.clientX);
  dragging = true;
  if (e.shiftKey) { dragMode = 'select'; rec.sel = { a: t, b: t }; renderSel(); return; }
  dragMode = 'scrub';
  wasPlaying = rec.playing;
  pv.pause();
  seekTo(t, false);
});
track.addEventListener('pointermove', (e) => {
  const t = tFromX(e.clientX);
  const h = $('tlHover');
  h.hidden = false; h.style.left = pct(t) + '%';
  if (!dragging) return;
  if (dragMode === 'select') { rec.sel.b = t; renderSel(); syncExportInputs(); return; }
  if (pendingT == null) requestAnimationFrame(() => { if (pendingT != null) seekTo(pendingT, false); pendingT = null; });
  pendingT = t;
});
track.addEventListener('pointerleave', () => { $('tlHover').hidden = true; });
const endDrag = () => {
  if (!dragging) return;
  dragging = false;
  if (dragMode === 'scrub' && wasPlaying) pv.play().catch(() => {});
  if (dragMode === 'select') syncExportInputs();
};
track.addEventListener('pointerup', endDrag);
track.addEventListener('pointercancel', endDrag);
track.addEventListener('wheel', (e) => {
  if (view.span >= 86400) return;
  e.preventDefault();
  view.start = Math.max(dayStart, Math.min(dayStart + 86400 - view.span, view.start + (e.deltaY + e.deltaX) / track.clientWidth * view.span));
  renderTimeline();
}, { passive: false });
$('btnPlay').addEventListener('click', () => {
  if (!rec.clip) { const first = clips.find((c) => !c.recording && dayOf(c.start) === dayKey); if (first) seekTo(first.start, true); return; }
  if (pv.paused) pv.play().catch(() => {}); else pv.pause();
});
$('btnBack').addEventListener('click', () => rec.t != null && seekTo(rec.t - 10, rec.playing));
$('btnFwd').addEventListener('click', () => rec.t != null && seekTo(rec.t + 10, rec.playing));
$('speed').addEventListener('change', (e) => { pv.playbackRate = Number(e.target.value); });
document.querySelectorAll('#zoom button').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('#zoom button').forEach((x) => x.classList.toggle('on', x === b));
  view.span = Number(b.dataset.span);
  const center = rec.t != null ? rec.t : Date.now() / 1000;
  view.start = view.span >= 86400 ? dayStart : Math.max(dayStart, Math.min(dayStart + 86400 - view.span, center - view.span / 2));
  renderTimeline();
}));
document.addEventListener('keydown', (e) => {
  if (!$('tab-recordings').classList.contains('on') || e.target.closest('input,select')) return;
  if (e.code === 'Space') { e.preventDefault(); $('btnPlay').click(); }
  if (e.code === 'ArrowLeft') seekTo((rec.t || 0) - (e.shiftKey ? 60 : 5), rec.playing);
  if (e.code === 'ArrowRight') seekTo((rec.t || 0) + (e.shiftKey ? 60 : 5), rec.playing);
});

// export any range as one video
const toInput = (t) => clock(t);
const fromInput = (v) => { const [h, m, s] = v.split(':').map(Number); return dayStart + h * 3600 + m * 60 + (s || 0); };
function syncExportInputs() {
  if (rec.sel) {
    $('exFrom').value = toInput(Math.min(rec.sel.a, rec.sel.b));
    $('exTo').value = toInput(Math.max(rec.sel.a, rec.sel.b));
  }
  updateExportLen();
}
function updateExportLen() {
  const f = $('exFrom').value, t = $('exTo').value;
  if (!f || !t) { $('exLen').textContent = ''; return; }
  const d = fromInput(t) - fromInput(f);
  $('exLen').textContent = d > 0 ? `${Math.floor(d / 3600) ? Math.floor(d / 3600) + 'h ' : ''}${Math.floor((d % 3600) / 60)}m ${Math.floor(d % 60)}s` : 'End must be after start';
}
function selFromInputs() {
  if (!$('exFrom').value || !$('exTo').value) return;
  rec.sel = { a: fromInput($('exFrom').value), b: fromInput($('exTo').value) };
  renderSel(); updateExportLen();
}
$('exFrom').addEventListener('change', selFromInputs);
$('exTo').addEventListener('change', selFromInputs);
$('exFromNow').addEventListener('click', () => { if (rec.t == null) return toast('Scrub to a moment first'); $('exFrom').value = toInput(rec.t); selFromInputs(); });
$('exToNow').addEventListener('click', () => { if (rec.t == null) return toast('Scrub to a moment first'); $('exTo').value = toInput(rec.t); selFromInputs(); });
$('exGo').addEventListener('click', async () => {
  if (!$('exFrom').value || !$('exTo').value) return toast('Set From and To first');
  const a = fromInput($('exFrom').value), b = fromInput($('exTo').value);
  if (b <= a) return toast('End must be after start');
  const btn = $('exGo');
  btn.disabled = true; btn.textContent = 'Preparing…';
  try {
    const r = await fetch(`/api/export?cam=${encodeURIComponent(recCam)}&start=${a}&end=${b}`);
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const name = (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = name ? name[1] : 'oldphonecctv.mp4';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    toast('Saved ' + link.download);
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = 'Download video';
});
setInterval(() => { if ($('tab-recordings').classList.contains('on')) loadRecordings(); }, 30000);

// ------------------------------------------------------------------ deep link from an alert: #rec=<camera>@<unix time>
function handleHash() {
  const m = location.hash.match(/^#rec=([^@]+)@(\d+)/);
  if (!m) return;
  recCam = decodeURIComponent(m[1]);
  rec.pendingSeek = Number(m[2]);
  history.replaceState(null, '', location.pathname);
  showTab('recordings');
}
window.addEventListener('hashchange', handleHash);

// ------------------------------------------------------------------ go
try { const t = localStorage.getItem('tab'); if (t) showTab(t); } catch (e) {}
handleHash();
connect();
