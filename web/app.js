'use strict';
/* OldPhone CCTV viewer: live H.264 via WebCodecs, PCM audio via Web Audio, push-to-talk, recordings, settings. */

const $ = (id) => document.getElementById(id);
const state = { s: null, rotate: 0, listening: false, talking: false, lastFrameAt: 0, frames: 0 };

// ------------------------------------------------------------------ tabs
document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.id === 'tab-' + name));
  if (name === 'recordings') loadRecordings();
  try { localStorage.setItem('tab', name); } catch (e) {}
}
try { const t = localStorage.getItem('tab'); if (t) showTab(t); } catch (e) {}

function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 2600);
}

// ------------------------------------------------------------------ server link
// One long HTTP response carries [u32 length][message] frames down; commands and talk audio go up
// as small POSTs. (Plain HTTP passes antivirus web filters that break WebSockets.)
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
  setOffline('Lost the connection to the OldPhone CCTV server. Reconnecting…');
  setTimeout(connect, 2000);
}
function handleMsg(d) {
  if (d[0] === 0) {
    const j = JSON.parse(dec.decode(d.subarray(1)));
    if (j.t === 'viewer') { viewerId = j.id; if (state.listening) send({ t: 'listen', on: true }); }
    else if (j.t === 'state') onState(j);
    return;
  }
  if (d[0] === 1) onConfig(d.subarray(1));
  else if (d[0] === 2) onFrame(d);
  else if (d[0] === 3) onAudio(d.subarray(1));
}
function post(body, type) {
  if (!viewerId) return;
  fetch('/api/viewer?id=' + viewerId, { method: 'POST', headers: { 'Content-Type': type }, body }).catch(() => {});
}
function send(obj) { post(JSON.stringify(obj), 'application/json'); }

// ------------------------------------------------------------------ video (WebCodecs)
const canvas = $('video');
const ctx2d = canvas.getContext('2d');
let decoder = null, codec = null, waitKey = true;
const canDecode = 'VideoDecoder' in window && window.isSecureContext;

function codecFromConfig(cfg) {
  for (let i = 0; i + 4 < cfg.length; i++) {
    if (cfg[i] === 0 && cfg[i + 1] === 0 && cfg[i + 2] === 1 && (cfg[i + 3] & 0x1f) === 7) {
      const h = (x) => x.toString(16).padStart(2, '0');
      return 'avc1.' + h(cfg[i + 4]) + h(cfg[i + 5]) + h(cfg[i + 6]);
    }
  }
  return null;
}

function onConfig(cfg) {
  const c = codecFromConfig(cfg);
  if (!c || !canDecode) return;
  if (c !== codec || !decoder || decoder.state === 'closed') {
    codec = c;
    makeDecoder();
  }
}

function makeDecoder() {
  try { if (decoder && decoder.state !== 'closed') decoder.close(); } catch (e) {}
  decoder = new VideoDecoder({
    output: drawFrame,
    error: (e) => { console.warn('decoder', e); waitKey = true; setTimeout(makeDecoder, 300); send({ t: 'key' }); },
  });
  decoder.configure({ codec, optimizeForLatency: true });
  waitKey = true;
}

function onFrame(d) {
  if (!decoder || decoder.state !== 'configured') return;
  const key = (d[1] & 1) === 1;
  const view = new DataView(d.buffer, d.byteOffset);
  const pts = Number(view.getBigInt64(2));
  if (waitKey && !key) return;
  if (decoder.decodeQueueSize > 8 && !key) { waitKey = true; send({ t: 'key' }); return; }
  waitKey = false;
  try {
    decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: d.subarray(10) }));
  } catch (e) { waitKey = true; send({ t: 'key' }); }
}

function drawFrame(frame) {
  const r = state.rotate, w = frame.displayWidth, h = frame.displayHeight;
  const cw = r % 180 ? h : w, ch = r % 180 ? w : h;
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw; canvas.height = ch;
    $('stage').classList.toggle('portrait', ch > cw);
  }
  ctx2d.save();
  ctx2d.translate(cw / 2, ch / 2);
  ctx2d.rotate((r * Math.PI) / 180);
  ctx2d.drawImage(frame, -w / 2, -h / 2, w, h);
  ctx2d.restore();
  frame.close();
  state.lastFrameAt = performance.now();
  state.frames++;
  $('offline').hidden = true;
}

function setOffline(msg) {
  $('offlineMsg').textContent = msg;
  $('offline').hidden = false;
}

// clock + frozen-picture watchdog
setInterval(() => {
  $('clock').textContent = new Date().toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const s = state.s;
  if (s && s.phone.connected && state.lastFrameAt && performance.now() - state.lastFrameAt > 6000) {
    setOffline('Waiting for video from the phone…');
    send({ t: 'key' });
  }
}, 1000);

// ------------------------------------------------------------------ audio out (listen)
let actx = null, nextT = 0;
function audioCtx() {
  if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state === 'suspended') actx.resume();
  return actx;
}

function onAudio(pcm) {
  if (!state.listening || state.talking || !actx) return;
  const n = pcm.length >> 1;
  const buf = actx.createBuffer(1, n, 16000);
  const ch = buf.getChannelData(0);
  const dv = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
  const src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(actx.destination);
  const now = actx.currentTime;
  if (nextT < now + 0.05 || nextT > now + 0.8) nextT = now + 0.15; // (re)build a small jitter buffer
  src.start(nextT);
  nextT += buf.duration;
}

$('btnListen').addEventListener('click', () => {
  state.listening = !state.listening;
  if (state.listening) audioCtx();
  $('btnListen').setAttribute('aria-pressed', state.listening);
  send({ t: 'listen', on: state.listening });
});

// ------------------------------------------------------------------ talk (mic -> phone speaker)
let mic = null, micNode = null, micSrc = null;
async function startTalk() {
  if (state.talking) return;
  if (!window.isSecureContext || !navigator.mediaDevices) {
    toast('Talking needs a secure page: use localhost on the PC or the Tailscale https:// address');
    return;
  }
  state.talking = true;
  $('btnTalk').classList.add('on');
  $('talkLabel').textContent = 'Talking…';
  try {
    const ctx = audioCtx();
    if (!micNode) {
      await ctx.audioWorklet.addModule('talk-worklet.js');
    }
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    if (!state.talking) return stopTalk();
    micSrc = ctx.createMediaStreamSource(mic);
    micNode = new AudioWorkletNode(ctx, 'talk-capture');
    let batch = [];
    micNode.port.onmessage = (e) => {
      if (!state.talking) return;
      batch.push(new Uint8Array(e.data));
      if (batch.length < 3) return; // send 120 ms at a time
      const out = new Uint8Array(batch.reduce((n, b) => n + b.length, 0));
      let o = 0;
      for (const b of batch) { out.set(b, o); o += b.length; }
      batch = [];
      post(out, 'application/octet-stream');
    };
    const mute = ctx.createGain(); mute.gain.value = 0;
    micSrc.connect(micNode).connect(mute).connect(ctx.destination);
    send({ t: 'talk', on: true });
  } catch (e) {
    toast('Microphone blocked: ' + e.message);
    stopTalk();
  }
}
function stopTalk() {
  if (!state.talking && !mic) return;
  state.talking = false;
  latched = false;
  $('btnTalk').classList.remove('on');
  $('talkLabel').textContent = 'Hold to talk';
  if (mic) mic.getTracks().forEach((t) => t.stop());
  try { micSrc && micSrc.disconnect(); micNode && micNode.disconnect(); } catch (e) {}
  mic = micSrc = null;
  send({ t: 'talk', on: false });
}
// press-and-hold talks while held; a quick tap latches it on until the next tap
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
  if (!state.talking) return;
  if (Date.now() - pressAt < 300) { latched = true; $('talkLabel').textContent = 'Tap to stop'; }
  else stopTalk();
});
talkBtn.addEventListener('pointercancel', () => { if (!latched) stopTalk(); });
talkBtn.addEventListener('contextmenu', (e) => e.preventDefault());

// ------------------------------------------------------------------ snapshot, torch, fullscreen
$('btnSnap').addEventListener('click', () => {
  if (!canvas.width) return toast('No picture yet');
  canvas.toBlob((b) => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = 'oldphonecctv-' + new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-') + '.png';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }, 'image/png');
});
$('btnRotate').addEventListener('click', () => {
  const next = (state.rotate + 90) % 360;
  state.rotate = next;          // live view turns immediately
  canvas.width = 0;             // force the canvas to re-measure for the new orientation
  saveSettings({ rotate: next }); // server applies it to new recordings too
});
$('btnTorch').addEventListener('click', () => {
  const on = $('btnTorch').getAttribute('aria-pressed') !== 'true';
  saveSettings({ torch: on });
  $('btnTorch').setAttribute('aria-pressed', on);
});
$('btnFull').addEventListener('click', () => {
  const st = $('stage');
  if (document.fullscreenElement) return document.exitFullscreen();
  if (st.requestFullscreen) st.requestFullscreen().catch(() => st.classList.toggle('theater'));
  else st.classList.toggle('theater');
});

// ------------------------------------------------------------------ state from server
function onState(s) {
  state.s = s;
  state.rotate = Number(s.rotate) || 0;
  $('camName').textContent = s.name || 'OldPhone CCTV';
  document.title = s.name || 'OldPhone CCTV';
  const p = s.phone, st = p.status || {}, hello = p.hello || {};

  const live = $('pillLive');
  if (p.connected && st.cam === 'streaming') { live.className = 'pill live'; live.lastChild.textContent = 'Live'; }
  else if (p.connected) { live.className = 'pill bad'; live.lastChild.textContent = st.cam === 'error' ? 'Camera problem' : 'Starting'; }
  else { live.className = 'pill bad'; live.lastChild.textContent = 'Offline'; }

  $('pillRec').hidden = !(s.rec.active);
  if (st.battery != null) {
    const pb = $('pillBattery');
    pb.hidden = false;
    pb.textContent = (st.charging ? '⚡ ' : '') + st.battery + '%';
    pb.className = 'pill' + (st.battery < 20 && !st.charging ? ' warn' : '');
    const pt = $('pillTemp');
    pt.hidden = false;
    pt.textContent = st.temp.toFixed(0) + '°C';
    pt.className = 'pill' + (st.temp >= 45 ? ' warn' : '');
  }

  if (!p.connected) setOffline(p.error || 'Waiting for the phone… Make sure the OldPhone CCTV app is running.');
  else if (st.cam === 'error') setOffline('The phone is connected, but its camera failed: ' + (st.error || 'unknown error') + '. Retrying automatically.');
  $('stats').textContent = p.connected && st.fps != null ? `${(hello.video || '').split(' ')[0]} · ${st.fps.toFixed(0)} fps · ${(st.kbps / 1000).toFixed(1)} Mbps` : '';

  const notes = [];
  if (!canDecode) notes.push(window.isSecureContext
    ? 'This browser can’t decode the live video. Use Chrome, Edge or Safari 16.4+.'
    : 'Live video and talking need a secure page. On this PC open http://localhost:' + (location.port || 80) + ', on other devices use the Tailscale https:// address.');
  if (s.talking && !state.talking) notes.push('Someone else is talking through the phone right now.');
  $('liveNote').hidden = !notes.length;
  $('liveNote').textContent = notes.join(' ');
  $('btnTalk').disabled = !p.connected;
  $('btnTorch').setAttribute('aria-pressed', !!(hello.cfg && hello.cfg.torch));

  renderCameras(hello);
  fillSettings(s);
}

// ------------------------------------------------------------------ camera choice
const camLabel = (c) => (c.facing === 'front' ? 'Front' : c.facing === 'back' ? 'Rear' : 'Camera ' + c.id);
function activeCamera(hello) {
  const cams = hello.cameras || [], want = hello.cfg && String(hello.cfg.camera);
  return cams.find((c) => c.id === want) || cams.find((c) => c.facing === want) || cams[0];
}
let switching = null;
function renderCameras(hello) {
  const box = $('camSwitch'), sel = $('setCamera');
  const cams = (hello && hello.cameras) || [];
  if (!cams.length) { box.innerHTML = ''; return; }
  const cur = activeCamera(hello);
  if (switching && cur && cur.id === switching) switching = null;
  const icon = (c) => c.facing === 'front'
    ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M4 8h3l2-2.5h6L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>';
  box.innerHTML = cams.map((c) => `<button role="radio" aria-checked="${cur && c.id === cur.id}" class="${cur && c.id === cur.id ? 'on' : ''}"
      data-id="${c.id}" ${switching ? 'disabled' : ''}>${icon(c)}${camLabel(c)}${c.mp ? ` <small>${Math.round(c.mp)} MP</small>` : ''}</button>`).join('');
  box.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => pickCamera(b.dataset.id)));
  if (!(document.activeElement === sel)) {
    sel.innerHTML = cams.map((c) => `<option value="${c.id}">${camLabel(c)}${c.mp ? ' · ' + Math.round(c.mp) + ' MP' : ''}</option>`).join('');
    if (cur) sel.value = cur.id;
  }
}
function pickCamera(id) {
  const cur = state.s && state.s.phone.hello && activeCamera(state.s.phone.hello);
  if (cur && cur.id === id) return;
  switching = id;
  setOffline('Switching camera…');
  saveSettings({ camera: id });
  renderCameras(state.s.phone.hello);
  setTimeout(() => { switching = null; }, 10000);
}

// ------------------------------------------------------------------ settings
let settingsFilled = false;
function fillSettings(s) {
  const cfg = (s.phone.hello && s.phone.hello.cfg) || null;
  const focused = document.activeElement && document.activeElement.closest('#tab-settings');
  if (!focused) {
    if (cfg) {
      $('setQuality').value = cfg.width + 'x' + cfg.height;
      $('setFps').value = String(cfg.fps);
      $('setBitrate').value = String(cfg.bitrate);
      $('setAudio').checked = !!cfg.audio;
      $('setVolume').value = cfg.volume;
    }
    $('setRotate').value = String(s.rotate || 0);
    $('setRecord').checked = !!s.rec.enabled;
    $('setRetention').value = String(s.rec.retention_hours);
    if (s.rec.dir) $('setDir').value = s.rec.dir;
  }
  const total = (s.rec.used_gb || 0) + (s.rec.free_gb || 0);
  $('storageBar').style.width = total ? Math.min(100, (100 * s.rec.used_gb) / total) + '%' : '0';
  $('storageText').textContent = `Recordings use ${fmtGB(s.rec.used_gb)} · ${fmtGB(s.rec.free_gb)} free on this disk · clips every ${s.rec.segment_minutes} min`;
  $('recError').hidden = !s.rec.error;
  $('recError').textContent = s.rec.error ? 'Recorder: ' + s.rec.error : '';

  const p = s.phone, st = p.status || {}, h = p.hello || {};
  const rows = [
    ['Phone', h.model || '—'],
    ['Android', h.android || '—'],
    ['Address', p.addr || '—'],
    ['Video', h.video || '—'],
    ['Wi-Fi signal', st.rssi != null ? st.rssi + ' dBm' : '—'],
    ['Phone uptime', st.uptime != null ? fmtDur(st.uptime) : '—'],
    ['Viewers', s.viewers],
  ];
  $('phoneInfo').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`).join('');
  settingsFilled = true;
}
const fmtGB = (g) => (g == null ? '?' : g < 1 ? Math.round(g * 1000) + ' MB' : g.toFixed(1) + ' GB');
const fmtDur = (s) => (s > 86400 ? Math.floor(s / 86400) + 'd ' : '') + Math.floor((s % 86400) / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function saveSettings(obj) {
  try {
    const r = await fetch('/api/settings', { method: 'POST', body: JSON.stringify(obj) });
    const j = await r.json();
    if (!j.ok) toast(j.error || 'Could not save');
    else toast('Saved');
  } catch (e) { toast('Could not save: ' + e.message); }
}
$('setCamera').addEventListener('change', (e) => pickCamera(e.target.value));
$('setQuality').addEventListener('change', (e) => { const [w, h] = e.target.value.split('x').map(Number); saveSettings({ width: w, height: h }); });
$('setFps').addEventListener('change', (e) => saveSettings({ fps: Number(e.target.value) }));
$('setBitrate').addEventListener('change', (e) => saveSettings({ bitrate: Number(e.target.value) }));
$('setRotate').addEventListener('change', (e) => saveSettings({ rotate: Number(e.target.value) }));
$('setAudio').addEventListener('change', (e) => saveSettings({ audio: e.target.checked }));
$('setVolume').addEventListener('change', (e) => saveSettings({ volume: Number(e.target.value) }));
$('setRecord').addEventListener('change', (e) => saveSettings({ record: e.target.checked }));
$('setRetention').addEventListener('change', (e) => saveSettings({ retention_hours: Number(e.target.value) }));
$('btnDir').addEventListener('click', () => {
  const d = $('setDir').value.trim();
  if (!d) return toast('Type a folder, e.g. D:\\CCTV');
  saveSettings({ recordings_dir: d });
});
$('setDir').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btnDir').click(); });

// phone modes
async function runMode(mode, dryRun) {
  const log = $('modeLog');
  document.querySelectorAll('.mode').forEach((b) => (b.disabled = true));
  log.hidden = false;
  log.textContent = (dryRun ? 'Checking what Surveillance mode would change…' : 'Switching to ' + mode + ' mode…') + '\n';
  try {
    const r = await fetch('/api/mode', { method: 'POST', body: JSON.stringify({ mode, dry_run: dryRun }) });
    const j = await r.json();
    log.textContent += (j.output || j.error || '') + '\n' + (j.ok ? 'Done.' : 'Failed.');
    if (!dryRun && j.ok) toast(mode === 'surveillance' ? 'Surveillance mode on' : 'Normal mode restored');
  } catch (e) { log.textContent += e.message; }
  document.querySelectorAll('.mode').forEach((b) => (b.disabled = false));
}
document.querySelectorAll('.mode').forEach((b) => b.addEventListener('click', () => {
  const m = b.dataset.mode;
  const msg = m === 'surveillance'
    ? 'Switch the phone to Surveillance mode? Other apps are disabled (not deleted) until you switch back to Normal.'
    : 'Switch the phone back to Normal mode? All apps and settings are restored and the camera stops so you can use the phone.';
  if (confirm(msg)) runMode(m, false);
}));
$('btnPreview').addEventListener('click', () => runMode('surveillance', true));
$('btnReinstall').addEventListener('click', async () => {
  const log = $('modeLog');
  log.hidden = false;
  log.textContent = 'Installing the camera app on the phone over ADB…\n';
  const r = await fetch('/api/phone', { method: 'POST', body: JSON.stringify({ action: 'install' }) });
  const j = await r.json();
  log.textContent += j.output + '\n' + (j.ok ? 'Done.' : 'Failed.');
});

// ------------------------------------------------------------------ recordings: one continuous timeline per day
// The 5-minute files are an internal detail: the player treats the day as one stretch of time,
// hops between files as you scrub or play, and Export cuts any range into one video.
let clips = [], dayKey = null, dayStart = 0;
const view = { start: 0, span: 86400 };
const rec = { t: null, clip: null, playing: false, sel: null };
const pv = $('player');
const track = $('track');
const dayOf = (t) => { const d = new Date(t * 1000); return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); };
const midnight = (t) => { const d = new Date(t * 1000); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
const clock = (t, sec = true) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: sec ? '2-digit' : undefined, hour12: false });
const clipEnd = (c) => (c.recording ? Date.now() / 1000 : c.end);

async function loadRecordings() {
  try { clips = (await (await fetch('/api/recordings')).json()).clips; } catch (e) { clips = []; }
  const days = [...new Set(clips.map((c) => dayOf(c.start)))].reverse();
  if (!days.includes(dayKey)) setDay(days[0] || null);
  const today = dayOf(Date.now() / 1000), yest = dayOf(Date.now() / 1000 - 86400);
  $('days').innerHTML = days.map((d) => {
    const c = clips.find((x) => dayOf(x.start) === d);
    const label = d === today ? 'Today' : d === yest ? 'Yesterday' : new Date(c.start * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
    return `<button data-day="${d}" class="${d === dayKey ? 'on' : ''}">${label}</button>`;
  }).join('');
  $('days').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { setDay(b.dataset.day); loadRecordings(); }));
  $('noClips').hidden = clips.length > 0;
  renderTimeline();
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
  track.querySelectorAll('.seg').forEach((s) => s.remove());
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
  // ticks
  const step = view.span >= 86400 ? 3 * 3600 : view.span >= 21600 ? 3600 : view.span >= 3600 ? 600 : 60;
  let html = '';
  for (let t = Math.ceil(view.start / step) * step; t <= vEnd; t += step) {
    html += `<span style="left:${pct(t)}%">${t - dayStart >= 86400 ? '24:00' : clock(t, false)}</span>`;
  }
  $('ticks').innerHTML = html;
  renderCursor();
  renderSel();
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

// ---- seeking across files
function clipAt(t) {
  return clips.find((c) => !c.recording && t >= c.start && t < c.end)
    || clips.find((c) => !c.recording && c.start >= t); // in a gap: jump to the next footage
}

let loadToken = 0;
function seekTo(t, play) {
  const c = clipAt(t);
  if (!c) { toast('No footage after this point yet'); return; }
  t = Math.max(t, c.start);
  rec.t = t;
  $('pvEmpty').hidden = true;
  if (rec.clip && rec.clip.name === c.name && pv.readyState >= 1) {
    pv.currentTime = Math.min(t - c.start, Math.max(0, pv.duration - 0.2));
    if (play) pv.play().catch(() => {});
  } else {
    rec.clip = c;
    const token = ++loadToken;
    pv.src = '/rec/' + encodeURIComponent(c.name);
    pv.playbackRate = Number($('speed').value);
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
pv.addEventListener('ended', () => {
  if (!rec.clip) return;
  const next = clips.find((c) => !c.recording && c.start >= rec.clip.end - 3 && c.name > rec.clip.name);
  if (next) seekTo(next.start, true);
  else setPlayIcon(false);
});
pv.addEventListener('play', () => setPlayIcon(true));
pv.addEventListener('pause', () => setPlayIcon(false));
function setPlayIcon(on) {
  rec.playing = on;
  $('playIcon').innerHTML = on ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>' : '<path d="M8 5v14l11-7z"/>';
}

// ---- scrubbing
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
track.addEventListener('wheel', (e) => { // pan when zoomed in
  if (view.span >= 86400) return;
  e.preventDefault();
  view.start = Math.max(dayStart, Math.min(dayStart + 86400 - view.span, view.start + (e.deltaY + e.deltaX) / track.clientWidth * view.span));
  renderTimeline();
}, { passive: false });

// ---- transport
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

// ---- export any time range as one video
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
    const r = await fetch(`/api/export?start=${a}&end=${b}`);
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

// ------------------------------------------------------------------ go
if (!canDecode) setOffline(window.isSecureContext ? 'This browser can’t show the live video.' : 'Open this page via localhost or the Tailscale https:// address to see live video.');
connect();
