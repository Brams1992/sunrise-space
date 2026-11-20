'use strict';
/* Sunrise Space — interface (client / chauffeur / admin). JavaScript pur, sans dépendance. */

// Centres approximatifs : permet de choisir sa commune si le GPS est refusé.
const COMMUNE_COORDS = {
  'Abobo': [5.4167, -4.0167], 'Adjamé': [5.3667, -4.0333], 'Anyama': [5.4947, -4.0517],
  'Attécoubé': [5.3333, -4.0333], 'Bingerville': [5.3556, -3.8856], 'Cocody': [5.35, -3.9833],
  'Koumassi': [5.2972, -3.9528], 'Marcory': [5.3, -3.9833], 'Plateau': [5.32, -4.0167],
  'Port-Bouët': [5.25, -3.9333], 'Songon': [5.3333, -4.2667], 'Treichville': [5.29, -4.0],
  'Yopougon': [5.3333, -4.0833], 'Abengourou': [6.7297, -3.4964], 'Abidjan (autre)': [5.36, -4.0083],
  'Agboville': [5.9333, -4.2167], 'Bondoukou': [8.0402, -2.8], 'Bouaflé': [6.9906, -5.7447],
  'Bouaké': [7.6906, -5.0303], 'Daloa': [6.8774, -6.4502], 'Dimbokro': [6.65, -4.7167],
  'Divo': [5.8372, -5.3572], 'Ferkessédougou': [9.5928, -5.1944], 'Gagnoa': [6.1319, -5.9506],
  'Grand-Bassam': [5.2, -3.7333], 'Guiglo': [6.5436, -7.4936], 'Korhogo': [9.458, -5.6294],
  'Man': [7.4125, -7.5538], 'Odienné': [9.5051, -7.5643], 'San-Pédro': [4.7485, -6.6363],
  'Séguéla': [7.9611, -6.6731], 'Soubré': [5.7856, -6.5944], 'Yamoussoukro': [6.8276, -5.2893],
};

const state = {
  token: localStorage.getItem('ss_token') || null,
  user: null, driver: null, meta: null,
  pos: null,            // {lat, lng, source: 'gps'|'map'|'commune'}
  tab: null, timers: [], map: null, layers: null, meMarker: null,
  locTimer: null, locOk: null, installEvt: null, onPos: null,
};

/* ---------- Utilitaires ---------- */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (kind === 'bad' ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ''; }, 3600);
}

const fmtPhone = (p) => String(p || '').replace(/(\d{2})(?=\d)/g, '$1 ').trim();
const fmtDist = (km) => (km < 1 ? `${Math.round(km * 1000 / 10) * 10} m` : `${km.toFixed(1).replace('.', ',')} km`);
const ago = (m) => (m == null ? '' : m < 1 ? "à l'instant" : m < 60 ? `il y a ${m} min` : `il y a ${Math.floor(m / 60)} h`);
const waLink = (num, text) => `https://wa.me/225${encodeURIComponent(num)}?text=${encodeURIComponent(text || "Bonjour, je vous contacte depuis Sunrise Space. Êtes-vous disponible pour une course ?")}`;
const VEHICLES = {
  moto: { icon: '🏍️', label: 'Moto-taxi', short: 'Moto' },
  voiture: { icon: '🚕', label: 'Taxi (voiture)', short: 'Voiture' },
  tricycle: { icon: '🛺', label: 'Tricycle', short: 'Tricycle' },
};
const veh = (t) => VEHICLES[t] || VEHICLES.moto;
const mapsLink = (lat, lng) => `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  let res;
  try {
    res = await fetch(path, { method: opts.method || 'GET', headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  } catch {
    netBar(true);
    throw new Error('Pas de connexion internet. Réessayez.');
  }
  netBar(false);
  let data = {};
  try { data = await res.json(); } catch { /* corps vide */ }
  if (!res.ok) {
    if (res.status === 401 && state.token) sessionLost();
    const e = new Error(data.error || 'Une erreur est survenue.');
    e.status = res.status;
    throw e;
  }
  return data;
}

function sessionLost() {
  if (!state.user) return;
  localStorage.removeItem('ss_token');
  state.token = null; state.user = null; state.driver = null;
  stopLocSharing();
  render();
  toast('Session expirée. Reconnectez-vous.', 'bad');
}

function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  stopClientTracking();
  destroyMap();
}
const every = (ms, fn) => state.timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms));

function modal(html) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="overlay"><div class="sheet" role="dialog" aria-modal="true">${html}</div></div>`;
  const close = () => { root.innerHTML = ''; };
  $('.overlay', root).addEventListener('click', (e) => { if (e.target.classList.contains('overlay')) close(); });
  return { el: $('.sheet', root), close };
}

function askNote(title, placeholder, confirmLabel = 'Confirmer') {
  return new Promise((resolve) => {
    const m = modal(`<h2>${esc(title)}</h2>
      <textarea id="note" maxlength="200" placeholder="${esc(placeholder || 'Note (facultatif)')}"></textarea>
      <div class="row"><button class="btn btn-ghost grow" id="n-cancel">Annuler</button><button class="btn grow" id="n-ok">${esc(confirmLabel)}</button></div>`);
    $('#n-cancel', m.el).onclick = () => { m.close(); resolve(null); };
    $('#n-ok', m.el).onclick = () => { const v = $('#note', m.el).value.trim(); m.close(); resolve(v); };
  });
}

/* ---------- Géolocalisation ---------- */
const GEO_HELP = "Localisation refusée. Sur iPhone : Réglages › Confidentialité et sécurité › Service de localisation › Safari (ou Chrome) › « Lors de l'utilisation » et activez « Position précise ».";

/* Attend jusqu'à `timeout` ms et garde la position la plus précise reçue (s'arrête dès ±25 m). */
function getPosition({ timeout = 15000, good = 25 } = {}) {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("La localisation n'est pas disponible sur cet appareil."));
    let best = null, done = false, id = null, timer = null;
    const out = (p) => ({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: Math.round(p.coords.accuracy || 0) });
    const finish = (fn, v) => {
      if (done) return;
      done = true; clearTimeout(timer);
      try { navigator.geolocation.clearWatch(id); } catch { /* rien */ }
      fn(v);
    };
    id = navigator.geolocation.watchPosition(
      (p) => {
        if (!best || p.coords.accuracy < best.coords.accuracy) best = p;
        if (p.coords.accuracy <= good) finish(resolve, out(best));
      },
      (e) => { if (e.code === 1) finish(reject, new Error(GEO_HELP)); },
      { enableHighAccuracy: true, maximumAge: 0, timeout },
    );
    timer = setTimeout(() => (best
      ? finish(resolve, out(best))
      : finish(reject, new Error("Position introuvable. Sortez à l'air libre et vérifiez que le GPS est activé."))), timeout);
  });
}

function meIcon() { return L.divIcon({ className: 'pin-me', iconSize: [20, 20] }); }

function drawMe(p) {
  if (!state.map) return;
  const ll = [p.lat, p.lng];
  if (state.meMarker) state.meMarker.setLatLng(ll);
  else state.meMarker = L.marker(ll, { icon: meIcon(), keyboard: false, zIndexOffset: 1000 }).addTo(state.map);
  if (state.accCircle) { state.map.removeLayer(state.accCircle); state.accCircle = null; }
  if (p.source === 'gps' && p.accuracy) {
    state.accCircle = L.circle(ll, { radius: p.accuracy, color: '#1E6CFF', weight: 1, fillOpacity: 0.12, interactive: false }).addTo(state.map);
  }
}

function setPos(p) {
  state.pos = { at: Date.now(), ...p };
  const line = $('#posline');
  if (line) line.innerHTML = posLineHTML();
  drawMe(p);
  if (state.onPos) state.onPos();
}

function posLineHTML() {
  if (!state.pos) return '<span class="chip warn">Position non définie</span> <span class="muted">Touchez « Me localiser » ou choisissez votre commune.</span>';
  const p = state.pos;
  if (p.source === 'commune') return '<span class="chip warn">📍 Position approximative (commune)</span><p class="tiny muted">Touchez « Me localiser » pour une position précise.</p>';
  if (p.source === 'map') return '<span class="chip ok">📍 Position choisie sur la carte</span>';
  const a = p.accuracy;
  if (a == null) return '<span class="chip ok">📍 Position GPS</span>';
  const label = a <= 50 ? 'Position précise' : a <= 150 ? 'Précision moyenne' : 'Position approximative';
  const cls = a <= 50 ? 'ok' : a <= 150 ? 'warn' : 'bad';
  const tip = a > 100 ? '<p class="tiny muted">Placez-vous à l\'extérieur, loin des murs, puis réessayez. Vérifiez « Position précise » dans Réglages › Confidentialité › Service de localisation.</p>' : '';
  return `<span class="chip ${cls}">📍 ${label} (±${a} m)</span>${tip}`;
}

function posControlsHTML() {
  const opts = (state.meta.communes || []).filter((c) => COMMUNE_COORDS[c])
    .map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  return `<div class="posbar">
    <div class="posline" id="posline">${posLineHTML()}</div>
    <div class="row">
      <button type="button" class="btn btn-ghost btn-sm" id="btn-loc">📍 Me localiser</button>
      <select id="sel-commune" aria-label="Choisir ma commune"><option value="">Ma commune…</option>${opts}</select>
    </div></div>`;
}

function bindPosControls() {
  $('#btn-loc').onclick = async (ev) => {
    const b = ev.currentTarget, label = b.textContent;
    b.disabled = true; b.textContent = '📡 Recherche…';
    try {
      const p = await getPosition();
      setPos({ ...p, source: 'gps' });
      if (state.map) state.map.setView([p.lat, p.lng], p.accuracy <= 50 ? 17 : 15);
      toast(`Position trouvée (±${p.accuracy} m)`);
    } catch (e) { toast(e.message, 'bad'); }
    finally { b.disabled = false; b.textContent = label; }
  };
  $('#sel-commune').onchange = (ev) => {
    const c = COMMUNE_COORDS[ev.target.value];
    if (c) { setPos({ lat: c[0], lng: c[1], source: 'commune' }); if (state.map) state.map.setView(c, 14); }
  };
}

/* ---------- Carte (Leaflet, optionnelle) ---------- */
function setupMap(elId) {
  const el = $('#' + elId);
  if (!window.L) {
    el.innerHTML = '<div class="map-fallback">La carte est indisponible (connexion faible ?). Utilisez la liste ci‑dessous.</div>';
    return null;
  }
  const c = state.pos || { ...state.meta.default_center };
  const map = L.map(elId, { zoomControl: true }).setView([c.lat, c.lng], state.pos ? 14 : 11);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
  state.layers = L.layerGroup().addTo(map);
  state.map = map;
  if (state.pos) drawMe(state.pos);
  map.on('click', (e) => setPos({ lat: e.latlng.lat, lng: e.latlng.lng, source: 'map' }));
  setTimeout(() => map.invalidateSize(), 150);
  state.map = map;
  return map;
}

/* ---------- Installation (PWA) ---------- */
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); state.installEvt = e; $$('[data-install]').forEach((b) => (b.hidden = false)); });
function installButtonHTML() {
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  if (standalone) return '';
  if (ios) return '<p class="hint">💡 Pour installer : touchez Partager puis « Sur l\'écran d\'accueil ».</p>';
  return `<button class="btn btn-ghost btn-block" data-install ${state.installEvt ? '' : 'hidden'}>📲 Installer l'application</button>`;
}
document.addEventListener('click', async (e) => {
  if (e.target.closest('[data-install]') && state.installEvt) {
    state.installEvt.prompt();
    await state.installEvt.userChoice.catch(() => {});
    state.installEvt = null;
    $$('[data-install]').forEach((b) => (b.hidden = true));
  }
});


/* ---------- Sirène des demandes (application ouverte) ---------- */
const alarm = { ctx: null, timer: null };
function alarmEnabled() { try { return localStorage.getItem('ss_alarm') !== '0'; } catch { return true; } }
function unlockAudio() {   // iPhone : le son n'est permis qu'après un premier toucher
  try {
    if (!alarm.ctx) { const C = window.AudioContext || window.webkitAudioContext; if (C) alarm.ctx = new C(); }
    if (alarm.ctx && alarm.ctx.state === 'suspended') alarm.ctx.resume();
  } catch { /* rien */ }
}
['touchstart', 'pointerdown', 'click'].forEach((ev) => document.addEventListener(ev, unlockAudio, { passive: true }));

function stopAlarm() {
  clearInterval(alarm.timer); alarm.timer = null;
  try { if (navigator.vibrate) navigator.vibrate(0); } catch { /* rien */ }
  const b = $('#alarmbar'); if (b) b.remove();
}

function startAlarm(force) {
  if ((!force && !alarmEnabled()) || alarm.timer) return;
  unlockAudio();
  const end = Date.now() + 10000;                       // 10 secondes, ou jusqu'au premier toucher
  const beep = () => {
    if (Date.now() >= end) return stopAlarm();
    const ctx = alarm.ctx;
    if (!ctx || ctx.state !== 'running') return;
    const t = ctx.currentTime;
    [880, 1320].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain(), at = t + i * 0.25;
      o.type = 'square'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, at); g.gain.exponentialRampToValueAtTime(0.7, at + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, at + 0.23);
      o.connect(g); g.connect(ctx.destination); o.start(at); o.stop(at + 0.25);
    });
  };
  const bar = document.createElement('div');
  bar.id = 'alarmbar'; bar.className = 'alarmbar'; bar.textContent = '🙋 Nouvelle demande ! Touchez pour couper le son';
  document.body.appendChild(bar);
  beep(); alarm.timer = setInterval(beep, 600);
  try { if (navigator.vibrate) navigator.vibrate(Array.from({ length: 9 }, () => [800, 300]).flat()); } catch { /* rien */ }
  document.addEventListener('pointerdown', stopAlarm, { once: true });
}

/* ---------- Notes ---------- */
function starsText(avg, count) { return avg == null ? 'Pas encore de note' : `⭐ ${String(avg).replace('.', ',')} (${count} avis)`; }
function ratingHTML(r) {
  if (r.rating) return `<p class="muted center">Votre note : ${'⭐'.repeat(r.rating)}</p>`;
  return `<div class="stack center"><b>Comment s'est passée la course ?</b><div class="stars">${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="star" data-stars="${n}" data-rid="${r.id}" aria-label="${n} étoile${n > 1 ? 's' : ''}">⭐</button>`).join('')}</div></div>`;
}

/* ---------- Appels Sunrise : voix par internet, numéros jamais affichés ---------- */
const callState = { id: null, pc: null, stream: null, poll: null, after: 0, ringTimer: null, tick: null, since: 0, muted: false, queue: [], outIce: [], busy: false, ended: false };
const callSupported = () => !!(window.RTCPeerConnection && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

function callUI(status, buttons, sub) {
  let ui = $('#callui');
  if (!ui) { ui = document.createElement('div'); ui.id = 'callui'; ui.className = 'callui'; document.body.appendChild(ui); }
  ui.innerHTML = `<div class="callbox"><img src="/icon-192.png" alt="" width="84" height="84" class="callicon">
    <h1>Sunrise</h1><p class="callstat" id="callstat">${esc(status)}</p><p class="tiny callsub">${esc(sub || 'Appel gratuit par internet')}</p>
    <div class="callbtns">${buttons}</div></div><audio id="callaudio" autoplay playsinline></audio>`;
  const a = $('#callaudio'); if (a && callState.stream0) a.srcObject = callState.stream0;
}
const callBtn = (id, cls, icon, label) => `<button class="callbtn ${cls}" id="${id}"><span>${icon}</span>${esc(label)}</button>`;

function ringTone(on) {
  clearInterval(callState.ringTimer); callState.ringTimer = null;
  try { if (navigator.vibrate) navigator.vibrate(0); } catch { /* rien */ }
  if (!on) return;
  unlockAudio();
  const play = () => {
    const ctx = alarm.ctx;
    if (ctx && ctx.state === 'running') {
      const t = ctx.currentTime;
      [[523, 0], [659, 0.18], [784, 0.36]].forEach(([f, d]) => {
        const o = ctx.createOscillator(), g = ctx.createGain(), at = t + d;
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, at); g.gain.exponentialRampToValueAtTime(0.5, at + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, at + 0.3);
        o.connect(g); g.connect(ctx.destination); o.start(at); o.stop(at + 0.32);
      });
    }
    try { if (navigator.vibrate) navigator.vibrate([500, 250, 500]); } catch { /* rien */ }
  };
  play(); callState.ringTimer = setInterval(play, 2200);
}

function callCleanup(message) {
  ringTone(false);
  clearInterval(callState.poll); clearInterval(callState.tick);
  callState.poll = callState.tick = null;
  if (callState.pc) { try { callState.pc.close(); } catch { /* rien */ } }
  if (callState.stream) callState.stream.getTracks().forEach((t) => t.stop());
  Object.assign(callState, { id: null, pc: null, stream: null, stream0: null, after: 0, queue: [], outIce: [], muted: false, busy: false, since: 0 });
  const ui = $('#callui'); if (ui) ui.remove();
  if (message) toast(message);
}

async function callPrepare() {
  if (!callSupported()) throw new Error("L'appel n'est pas disponible sur cet appareil. Mettez à jour votre navigateur.");
  callState.ended = false;
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { throw new Error('Autorisez le micro pour passer un appel (Réglages du téléphone > Sunrise Space / Safari > Micro).'); }
  let servers = [{ urls: 'stun:stun.l.google.com:19302' }];
  try { servers = (await api('/api/calls/ice')).iceServers || servers; } catch { /* STUN par défaut */ }
  const pc = new RTCPeerConnection({ iceServers: servers });
  stream.getTracks().forEach((t) => pc.addTrack(t, stream));
  callState.stream = stream; callState.pc = pc;
  pc.ontrack = (e) => { callState.stream0 = e.streams[0]; const a = $('#callaudio'); if (a) { a.srcObject = e.streams[0]; a.play().catch(() => {}); } };
  pc.onicecandidate = (e) => {
    if (!e.candidate) return;
    const payload = JSON.stringify(e.candidate.toJSON ? e.candidate.toJSON() : e.candidate);
    if (callState.id) api(`/api/calls/${callState.id}/signal`, { method: 'POST', body: { kind: 'ice', payload } }).catch(() => {});
    else callState.outIce.push(payload);
  };
  pc.onconnectionstatechange = () => {
    if (callState.pc !== pc) return;
    if (pc.connectionState === 'connected') callConnected();
    if (pc.connectionState === 'failed') hangUp('Connexion impossible. Vérifiez votre réseau et réessayez.');
  };
  return pc;
}

function callConnected() {
  if (callState.since) return;
  ringTone(false);
  callState.since = Date.now();
  callUI('En communication · 00:00', callBtn('call-mute', 'mute', '🎙️', 'Couper le micro') + callBtn('call-end', 'end', '📞', 'Raccrocher'));
  bindCallButtons();
  callState.tick = setInterval(() => {
    const sec = Math.floor((Date.now() - callState.since) / 1000);
    const el = $('#callstat'); if (el) el.textContent = `En communication · ${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
  }, 1000);
}

function bindCallButtons() {
  const end = $('#call-end'); if (end) end.onclick = () => hangUp();
  const mute = $('#call-mute');
  if (mute) mute.onclick = () => {
    callState.muted = !callState.muted;
    if (callState.stream) callState.stream.getAudioTracks().forEach((t) => { t.enabled = !callState.muted; });
    mute.innerHTML = `<span>${callState.muted ? '🔇' : '🎙️'}</span>${callState.muted ? 'Micro coupé' : 'Couper le micro'}`;
  };
}

async function hangUp(message) {
  const id = callState.id;
  callState.ended = true;
  callCleanup(message);
  if (id) { try { await api(`/api/calls/${id}/end`, { method: 'POST' }); } catch { /* rien */ } }
}

async function applySignals(list) {
  const pc = callState.pc;
  for (const sg of list) {
    callState.after = Math.max(callState.after, sg.id);
    try {
      if (sg.kind === 'answer') {
        await pc.setRemoteDescription(JSON.parse(sg.payload));
        for (const c of callState.queue.splice(0)) await pc.addIceCandidate(c).catch(() => {});
      } else if (sg.kind === 'ice') {
        const c = JSON.parse(sg.payload);
        if (pc.remoteDescription) await pc.addIceCandidate(c).catch(() => {}); else callState.queue.push(c);
      }
    } catch { /* signal ignoré */ }
  }
}

function pollCall(caller) {
  clearInterval(callState.poll);
  const id = callState.id;
  callState.poll = setInterval(async () => {
    if (callState.id !== id || callState.busy) return;
    callState.busy = true;
    try {
      const r = await api(`/api/calls/${id}?after=${callState.after}`);
      if (callState.id !== id) return;
      await applySignals(r.signals.filter((x) => x.kind !== 'offer'));
      if (r.status === 'active' && !callState.since && callState.pc && callState.pc.connectionState === 'connected') callConnected();
      if (r.status !== 'ringing' && r.status !== 'active') {
        callState.ended = true;
        callCleanup({ declined: 'Appel refusé', missed: caller ? 'Pas de réponse' : 'Appel manqué', ended: 'Appel terminé' }[r.status] || 'Appel terminé');
      }
    } catch (e) { if (e.status === 404) callCleanup(); } finally { callState.busy = false; }
  }, 1500);
}

async function startCall(requestId) {
  if (callState.id || callState.pc) return;
  try {
    callUI('Appel en cours…', callBtn('call-end', 'end', '📞', 'Annuler'), 'Connexion…');
    bindCallButtons();
    const pc = await callPrepare();
    const offer = await pc.createOffer({ offerToReceiveAudio: true });
    await pc.setLocalDescription(offer);
    const r = await api('/api/calls', { method: 'POST', body: { request_id: requestId, offer: JSON.stringify(pc.localDescription) } });
    if (callState.ended || callState.pc !== pc) { api(`/api/calls/${r.id}/end`, { method: 'POST' }).catch(() => {}); return; }
    callState.id = r.id;
    for (const payload of callState.outIce.splice(0)) api(`/api/calls/${r.id}/signal`, { method: 'POST', body: { kind: 'ice', payload } }).catch(() => {});
    callUI('Sonnerie…', callBtn('call-end', 'end', '📞', 'Raccrocher'), 'En attente de réponse');
    bindCallButtons();
    pollCall(true);
  } catch (e) { callCleanup(); toast(e.message, 'bad'); }
}

async function answerCall(call) {
  try {
    ringTone(false);
    callUI('Connexion…', callBtn('call-end', 'end', '📞', 'Raccrocher'));
    bindCallButtons();
    const pc = await callPrepare();
    callState.id = call.id;
    await pc.setRemoteDescription(JSON.parse(call.offer));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    await api(`/api/calls/${call.id}/accept`, { method: 'POST', body: { answer: JSON.stringify(pc.localDescription) } });
    for (const payload of callState.outIce.splice(0)) api(`/api/calls/${call.id}/signal`, { method: 'POST', body: { kind: 'ice', payload } }).catch(() => {});
    callUI('Connexion…', callBtn('call-end', 'end', '📞', 'Raccrocher'));
    bindCallButtons();
    callState.after = 0;
    pollCall(false);
  } catch (e) {
    const id = call.id; callCleanup(); toast(e.status === 409 ? 'Cet appel est terminé.' : e.message, 'bad');
    api(`/api/calls/${id}/end`, { method: 'POST' }).catch(() => {});
  }
}

function showIncoming(call) {
  callState.id = call.id; callState.incoming = true;
  callUI('Appel entrant', callBtn('call-no', 'end', '✖', 'Refuser') + callBtn('call-yes', 'yes', '📞', 'Répondre'), 'Sunrise Space');
  ringTone(true);
  $('#call-yes').onclick = () => { callState.id = null; answerCall(call); };
  $('#call-no').onclick = async () => { const id = call.id; callCleanup(); try { await api(`/api/calls/${id}/end`, { method: 'POST' }); } catch { /* rien */ } };
  // l'appelant a raccroché ou n'a pas eu de réponse : l'écran disparaît
  clearInterval(callState.poll);
  callState.poll = setInterval(async () => {
    if (callState.id !== call.id || callState.pc) return;
    try { const r = await api(`/api/calls/${call.id}?after=999999999`); if (r.status !== 'ringing') callCleanup('Appel manqué'); } catch { /* rien */ }
  }, 2000);
}

async function checkIncomingCall() {
  if (!state.user || !['client', 'driver'].includes(state.user.role) || callState.id || callState.pc || document.hidden) return;
  try {
    const { call } = await api('/api/calls/incoming');
    if (call && call.offer && !callState.id && !callState.pc) showIncoming(call);
  } catch { /* rien */ }
}

function startCallWatcher() {
  clearInterval(state.callTimer);
  state.callTimer = setInterval(checkIncomingCall, 3000);
  checkIncomingCall();
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkIncomingCall(); });
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-call]');
  if (b) { unlockAudio(); startCall(Number(b.dataset.call)); }
});

/* ---------- Réseau faible ---------- */
function netBar(show) {
  let b = $('#netbar');
  if (!b) { b = document.createElement('div'); b.id = 'netbar'; b.className = 'netbar'; b.textContent = '📶 Connexion perdue… reconnexion automatique'; document.body.appendChild(b); }
  b.hidden = !show;
}
window.addEventListener('offline', () => netBar(true));
window.addEventListener('online', () => netBar(false));

/* ---------- Notifications push : prévenu même application fermée ---------- */
const PUSH_OK = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
const IS_STANDALONE = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
const IS_IOS = /iphone|ipad|ipod/i.test(navigator.userAgent);

function urlB64ToBytes(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/* Abonne cet appareil (ou le re-lie au compte connecté). La permission doit déjà être accordée. */
async function syncPush() {
  if (!PUSH_OK || !state.user || state.user.role === 'admin' || Notification.permission !== 'granted') return false;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const { key } = await api('/api/push/key');
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToBytes(key) });
    }
    await api('/api/push/subscribe', { method: 'POST', body: sub.toJSON() });
    return true;
  } catch { return false; }
}

async function enablePush() {
  if (!PUSH_OK) return false;
  const perm = await Notification.requestPermission();   // doit venir d'un toucher de l'utilisateur
  if (perm !== 'granted') return false;
  return syncPush();
}

async function forgetPush() {
  try {
    if (!PUSH_OK) return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } });
  } catch { /* rien */ }
}

function pushWhy() {
  return state.user && state.user.role === 'driver'
    ? 'Recevez les demandes de course près de vous, même application fermée.'
    : 'Soyez prévenu dès qu\'un chauffeur accepte votre demande, même application fermée.';
}

function updatePushBar() {
  const bar = $('#pushbar');
  if (!bar) return;
  const role = state.user && state.user.role;
  bar.hidden = true;
  if (role !== 'client' && role !== 'driver') return;
  if (PUSH_OK) {
    if (Notification.permission !== 'default') return;
    bar.innerHTML = `<div><strong>🔔 Notifications</strong><br>${pushWhy()}</div><button class="btn btn-sm" id="push-on">Activer</button>`;
    bar.hidden = false;
    $('#push-on').onclick = async () => {
      const ok = await enablePush();
      toast(ok ? 'Notifications activées ✅' : 'Notifications non activées', ok ? undefined : 'bad');
      updatePushBar();
      if (state.tab === 'account') accountView();
    };
  } else if (IS_IOS && !IS_STANDALONE()) {
    let hidden = false;
    try { hidden = Date.now() - Number(localStorage.getItem('ss_push_hint') || 0) < 7 * 864e5; } catch { /* rien */ }
    if (hidden) return;
    bar.innerHTML = `<div><strong>📲 Notifications sur iPhone</strong><br>Touchez <b>Partager</b> puis <b>Sur l'écran d'accueil</b>, ouvrez l'icône Sunrise Space, et activez les notifications.</div><button class="btn btn-sm btn-ghost" id="push-x" aria-label="Fermer">✕</button>`;
    bar.hidden = false;
    $('#push-x').onclick = () => { try { localStorage.setItem('ss_push_hint', String(Date.now())); } catch { /* rien */ } bar.hidden = true; };
  }
}

function pushAccountHTML() {
  if (!state.user || state.user.role === 'admin') return '';
  if (!PUSH_OK) return IS_IOS ? `<p class="muted tiny">🔔 Notifications : ajoutez d'abord l'application à l'écran d'accueil (Partager → Sur l'écran d'accueil), puis rouvrez-la depuis l'icône.</p>` : '';
  const p = Notification.permission;
  if (p === 'granted') return '<p class="muted tiny">🔔 Notifications activées ✅</p>';
  if (p === 'denied') return `<p class="muted tiny">🔔 Notifications bloquées. Autorisez-les dans les Réglages de l'iPhone (Notifications → Sunrise Space).</p>`;
  return `<button class="btn btn-ghost btn-block" id="btn-push">🔔 Activer les notifications</button>`;
}

/* ---------- Rendu principal ---------- */
function render() {
  clearTimers();
  clearInterval(state.unreadTimer);
  state.lastUnread = null; state.unread = 0;
  state.tab = null;
  if (!state.user) return renderAuth();
  if (state.user.must_change_password) return renderForcedChange();
  if (state.user.role === 'client') return renderShell('client', [['request', '🛵', 'Ma demande'], ['inbox', '✉️', 'Messages'], ['account', '👤', 'Compte']]);
  if (state.user.role === 'driver') return renderShell('driver', [['home', '🏍️', 'Accueil'], ['inbox', '✉️', 'Messages'], ['account', '👤', 'Compte']]);
  if (state.user.role === 'admin' && state.user.scope === 'validator') return renderShell('validator', [['drivers', '🏍️', 'Chauffeurs'], ['users', '👥', 'Inscrits'], ['account', '👤', 'Compte']]);
  if (state.user.role === 'admin') return renderShell('admin', [['stats', '📊', 'Aperçu'], ['drivers', '🏍️', 'Chauffeurs'], ['users', '👥', 'Utilisateurs'], ['reports', '⚑', 'Signalements'], ['inbox', '✉️', 'Messages']]);
}

function renderShell(role, tabs) {
  const first = tabs[0][0];
  $('#app').innerHTML = `
    <div class="shell ${role === 'admin' || role === 'validator' ? 'wide' : ''}">
      <header class="topbar">
        <div class="brand"><img src="/icon-192.png" alt="" width="30" height="30"><span>Sunrise Space</span></div>
        ${role === 'admin' ? '<button class="logoutbtn" id="btn-top-logout" aria-label="Se déconnecter">Déconnexion</button>' : ''}
        <div class="who">${esc(state.user.name)}<br><span style="opacity:.7">${{ client: 'Client', driver: 'Chauffeur', admin: 'Administrateur', validator: 'Validateur' }[role]}</span></div>
      </header>
      <button class="annbar" id="annbar" hidden></button>
      <div class="pushbar" id="pushbar" hidden></div>
      <main id="main"></main>
    </div>
    <nav class="bottomnav" aria-label="Navigation"><div class="inner">
      ${tabs.map(([id, ic, label]) => `<button data-tab="${id}"><span class="ic" aria-hidden="true">${ic}${id === 'inbox' ? '<i class="dot" id="inbox-dot" hidden></i>' : ''}</span>${esc(label)}</button>`).join('')}
    </div></nav>`;
  $$('.bottomnav [data-tab]').forEach((b) => (b.onclick = () => openTab(role, b.dataset.tab)));
  const tl = $('#btn-top-logout');
  if (tl) tl.onclick = () => { if (confirm('Se déconnecter du compte administrateur ?')) logout(); };
  openTab(role, first);
  updatePushBar();
  clearInterval(state.unreadTimer);
  if (role === 'validator') return;   // pas de messagerie ni de notifications pour un validateur
  syncPush();
  refreshUnread();
  if (role === 'client' || role === 'driver') startCallWatcher();
  state.unreadTimer = setInterval(() => { if (!document.hidden) refreshUnread(); }, 25000);
}

function updateBanner() {
  const b = $('#annbar');
  if (!b) return;
  const n = state.unread || 0;
  const admin = state.user && state.user.role === 'admin';
  if (!n || state.tab === 'inbox') { b.hidden = true; return; }
  b.hidden = false;
  b.textContent = admin ? `✉️ ${n} nouveau${n > 1 ? 'x' : ''} message${n > 1 ? 's' : ''} — toucher pour lire` : `📣 ${n > 1 ? n + ' nouveautés' : 'Nouveau message'} de Sunrise Space — toucher pour lire`;
  b.onclick = () => openTab(state.user.role, 'inbox');
}

async function refreshUnread() {
  if (!state.user) return;
  try {
    const { unread } = await api('/api/inbox/unread');
    state.unread = unread; updateBanner();
    const d = $('#inbox-dot');
    if (d) { d.hidden = !unread; d.textContent = unread > 9 ? '9+' : String(unread || ''); }
    if (state.lastUnread != null && unread > state.lastUnread && state.tab !== 'inbox') toast(state.user.role === 'admin' ? 'Nouveau message reçu' : 'Nouveau message de Sunrise Space');
    state.lastUnread = unread;
  } catch { /* on réessaiera */ }
}

function openTab(role, tab) {
  clearTimers();
  state.tab = tab;
  state.onPos = null;
  $$('.bottomnav [data-tab]').forEach((b) => { if (b.dataset.tab === tab) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
  window.scrollTo(0, 0);
  updateBanner();
  const views = {
    client: { request: clientRequest, inbox: userInbox, account: accountView },
    driver: { home: driverHome, inbox: userInbox, account: accountView },
    admin: { stats: adminStats, drivers: adminDrivers, users: adminUsers, reports: adminReports, inbox: adminInbox },
    validator: { drivers: adminDrivers, users: validatorUsers, account: validatorAccount },
  };
  views[role][tab]();
}

/* ---------- Authentification ---------- */
function renderAuth() {
  let mode = 'login', role = 'client';
  const communes = (state.meta.communes || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  $('#app').innerHTML = `
    <div class="shell">
      <section class="hero">
        <img src="/icon-192.png" alt="Logo Sunrise Space">
        <h1>Sunrise Space</h1>
        <p>Trouvez une moto, un taxi ou un tricycle près de vous, en un instant.</p>
      </section>
      <section class="card auth-card">
        <div class="seg" role="group" aria-label="Mode">
          <button type="button" data-mode="login" aria-pressed="true">Connexion</button>
          <button type="button" data-mode="register" aria-pressed="false">Créer un compte</button>
        </div>
        <form class="form" id="authform" novalidate>
          <div id="role-box" hidden>
            <div class="seg" role="group" aria-label="Je suis">
              <button type="button" data-role="client" aria-pressed="true">🙋 Je suis client</button>
              <button type="button" data-role="driver" aria-pressed="false">🏍️ Je suis chauffeur</button>
            </div>
          </div>
          <label id="f-name" hidden>Nom complet<input name="name" autocomplete="name" maxlength="60" placeholder="Ex. Awa Koné"></label>
          <label>Numéro de téléphone<input name="phone" type="tel" inputmode="numeric" autocomplete="tel" placeholder="07 07 07 07 07" maxlength="18" required></label>
          <label>Mot de passe<input name="password" type="password" autocomplete="current-password" minlength="6" placeholder="Au moins 6 caractères" required></label>
          <button type="button" class="link" id="forgot">Mot de passe oublié ?</button>
          <div id="driver-box" class="stack" hidden>
            <label>Votre véhicule<select name="vehicle_type">${Object.entries(VEHICLES).map(([k, v]) => `<option value="${k}">${v.icon} ${esc(v.label)}</option>`).join('')}</select></label>
            <label>Plaque d'immatriculation<input name="plate" maxlength="20" placeholder="Ex. 1234 AB 01"></label>
            <label>Numéro de permis de conduire<input name="permis" maxlength="30"></label>
            <label>Modèle du véhicule <span class="hint">(facultatif)</span><input name="moto_model" maxlength="40" placeholder="Ex. Jakarta 125, Toyota Corolla…"></label>
            <label>Commune / ville<select name="commune"><option value="">Choisir…</option>${communes}</select></label>
            <label>Numéro WhatsApp <span class="hint">(si différent du téléphone)</span><input name="whatsapp" type="tel" inputmode="numeric" maxlength="18"></label>
            <p class="banner warn">Votre profil sera vérifié par l'équipe Sunrise Space (plaque, permis) avant d'apparaître aux clients.</p>
          </div>
          <p class="banner bad" id="auth-error" role="alert" hidden></p>
          <button class="btn btn-block" id="auth-submit" type="submit">Se connecter</button>
        </form>
      </section>
      <section class="feat">
        <div><span class="ic">📍</span><p><b>Un chauffeur près de vous</b><br><span class="muted">Moto, taxi ou tricycle : envoyez votre demande, le premier chauffeur disponible l'accepte.</span></p></div>
        <div><span class="ic">📞</span><p><b>Appel ou WhatsApp direct</b><br><span class="muted">Vous vous arrangez avec le chauffeur, paiement en espèces.</span></p></div>
        <div><span class="ic">🛡️</span><p><b>Chauffeurs vérifiés</b><br><span class="muted">Chaque profil est contrôlé, et vous pouvez signaler un abus.</span></p></div>
        ${installButtonHTML()}
      </section>
    </div>`;

  const form = $('#authform'), err = $('#auth-error');
  const sync = () => {
    const reg = mode === 'register';
    $$('[data-mode]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    $$('[data-role]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.role === role)));
    $('#role-box').hidden = !reg;
    $('#f-name').hidden = !reg;
    $('#driver-box').hidden = !(reg && role === 'driver');
    $('#auth-submit').textContent = reg ? 'Créer mon compte' : 'Se connecter';
    $('#forgot').hidden = reg;
    form.password.autocomplete = reg ? 'new-password' : 'current-password';
    err.hidden = true;
  };
  $$('[data-mode]').forEach((b) => (b.onclick = () => { mode = b.dataset.mode; sync(); }));
  $$('[data-role]').forEach((b) => (b.onclick = () => { role = b.dataset.role; sync(); }));
  $('#forgot').onclick = () => openForgot(form.phone.value);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const btn = $('#auth-submit');
    btn.disabled = true;
    try {
      let data;
      if (mode === 'login') {
        data = await api('/api/auth/login', { method: 'POST', body: { phone: form.phone.value, password: form.password.value } });
      } else {
        const body = { role, name: form.name.value, phone: form.phone.value, password: form.password.value };
        if (role === 'driver') body.driver = { vehicle_type: form.vehicle_type.value, plate: form.plate.value, permis: form.permis.value, moto_model: form.moto_model.value, commune: form.commune.value, whatsapp: form.whatsapp.value };
        data = await api('/api/auth/register', { method: 'POST', body });
      }
      state.token = data.token;
      localStorage.setItem('ss_token', data.token);
      state.user = data.user; state.driver = data.driver;
      render();
      if (mode === 'register') toast(role === 'driver' ? 'Compte créé. Validation en cours.' : 'Bienvenue sur Sunrise Space !');
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
    } finally { btn.disabled = false; }
  };
}

function openForgot(prefill) {
  const m = modal(`<h2>Mot de passe oublié</h2>
    <p class="muted">Entrez le numéro de votre compte. L'équipe Sunrise Space vérifie votre identité puis vous contacte (appel ou WhatsApp) pour vous remettre un mot de passe provisoire.</p>
    <form id="fgf" class="form" novalidate>
      <label>Numéro de téléphone<input name="phone" type="tel" inputmode="numeric" autocomplete="tel" maxlength="18" placeholder="07 07 07 07 07" value="${esc(prefill || '')}" required></label>
      <p class="banner bad" id="fg-err" role="alert" hidden></p>
      <div class="row"><button type="button" class="btn btn-ghost grow" id="fg-cancel">Annuler</button><button class="btn grow" type="submit" id="fg-ok">Envoyer la demande</button></div>
    </form>`);
  $('#fg-cancel', m.el).onclick = () => m.close();
  $('#fgf', m.el).onsubmit = async (ev) => {
    ev.preventDefault();
    const err = $('#fg-err', m.el), btn = $('#fg-ok', m.el);
    err.hidden = true; btn.disabled = true;
    try {
      await api('/api/auth/forgot', { method: 'POST', body: { phone: ev.target.phone.value } });
      m.el.innerHTML = `<h2>Demande envoyée ✅</h2>
        <p>Si ce numéro correspond à un compte, l'équipe Sunrise Space a été prévenue. Elle vous contactera avec un <b>mot de passe provisoire</b> que vous changerez dès votre connexion.</p>
        <p class="muted">Gardez votre téléphone à portée. Pas besoin de refaire une demande.</p>
        <button class="btn btn-block" id="fg-done">OK</button>`;
      $('#fg-done', m.el).onclick = () => m.close();
    } catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
  };
}

function passwordFormHTML(currentLabel) {
  return `<label>${currentLabel}<input name="cur" type="password" autocomplete="current-password" required></label>
    <label>Nouveau mot de passe<input name="new" type="password" autocomplete="new-password" minlength="6" placeholder="Au moins 6 caractères" required></label>
    <label>Répétez le nouveau mot de passe<input name="new2" type="password" autocomplete="new-password" required></label>
    <p class="banner bad" id="pw-err" role="alert" hidden></p>`;
}

function bindPasswordForm(form, btn, done) {
  const err = $('#pw-err', form);
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    if (form.new.value !== form.new2.value) { err.textContent = 'Les deux mots de passe ne sont pas identiques.'; err.hidden = false; return; }
    btn.disabled = true;
    try {
      const r = await api('/api/auth/change-password', { method: 'POST', body: { current: form.cur.value, new: form.new.value } });
      done(r.user);
    } catch (e) { err.textContent = e.message; err.hidden = false; btn.disabled = false; }
  };
}

function openChangePassword() {
  const m = modal(`<h2>Changer mon mot de passe</h2>
    <form id="pwf" class="form" novalidate>${passwordFormHTML('Mot de passe actuel')}
      <p class="muted tiny">Vos autres appareils seront déconnectés.</p>
      <div class="row"><button type="button" class="btn btn-ghost grow" id="pw-cancel">Annuler</button><button class="btn grow" type="submit" id="pw-ok">Enregistrer</button></div>
    </form>`);
  $('#pw-cancel', m.el).onclick = () => m.close();
  bindPasswordForm($('#pwf', m.el), $('#pw-ok', m.el), (user) => { m.close(); state.user = user; toast('Mot de passe modifié.'); });
}

/* Après une réinitialisation par l'équipe : on doit choisir son propre mot de passe avant de continuer. */
function renderForcedChange() {
  $('#app').innerHTML = `
    <div class="shell">
      <section class="hero">
        <img src="/icon-192.png" alt="Logo Sunrise Space">
        <h1>Choisissez votre mot de passe</h1>
        <p>Vous êtes connecté avec un mot de passe provisoire. Choisissez maintenant le vôtre.</p>
      </section>
      <section class="card auth-card">
        <form class="form" id="pwf" novalidate>${passwordFormHTML('Mot de passe provisoire (reçu de l\'équipe)')}
          <button class="btn btn-block" type="submit" id="pw-ok">Enregistrer et continuer</button>
          <button class="link" type="button" id="pw-out">Me déconnecter</button>
        </form>
      </section>
    </div>`;
  bindPasswordForm($('#pwf'), $('#pw-ok'), (user) => { state.user = user; render(); toast('Mot de passe modifié. Bienvenue !'); });
  $('#pw-out').onclick = logout;
}

async function logout() {
  clearInterval(state.unreadTimer); clearInterval(state.callTimer);
  if (callState.id || callState.pc) await hangUp();
  await forgetPush();   // cet appareil ne doit plus recevoir les notifications de ce compte
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* on se déconnecte quand même */ }
  localStorage.removeItem('ss_token');
  stopLocSharing();
  state.token = null; state.user = null; state.driver = null; state.pos = null;
  render();
}

function accountView() {
  const u = state.user;
  const d = state.driver;
  $('#main').innerHTML = `
    <section class="card stack">
      <h2>Mon compte</h2>
      <dl class="kv"><dt>Nom</dt><dd>${esc(u.name)}</dd><dt>Téléphone</dt><dd>${esc(fmtPhone(u.phone))}</dd>
        <dt>Profil</dt><dd>${{ client: 'Client', driver: 'Chauffeur', admin: 'Administrateur' }[u.role]}</dd>
        ${d ? `<dt>Véhicule</dt><dd>${veh(d.vehicle_type).icon} ${esc(veh(d.vehicle_type).label)}${d.moto_model ? ' · ' + esc(d.moto_model) : ''} · ${esc(d.plate)}</dd><dt>Commune</dt><dd>${esc(d.commune)}</dd>
        <dt>Courses</dt><dd>${d.rides_done || 0} terminée${(d.rides_done || 0) > 1 ? 's' : ''}</dd><dt>Ma note</dt><dd>${starsText(d.rating_avg, d.rating_count)}</dd>
        <dt>Validation</dt><dd>${{ pending: 'En attente', approved: 'Validé ✅', rejected: 'Refusé' }[d.approval]}</dd>` : ''}
      </dl>
      ${installButtonHTML()}
      ${pushAccountHTML()}
      ${state.user && state.user.role === 'driver' ? `<div class="row"><button class="btn btn-ghost grow" id="btn-alarm">${alarmEnabled() ? '🔊 Sirène des demandes : activée' : '🔇 Sirène des demandes : coupée'}</button><button class="btn btn-ghost btn-sm" id="btn-alarm-test">Tester</button></div>` : ''}
      <button class="btn btn-ghost btn-block" id="btn-pw">🔒 Changer mon mot de passe</button>
      <button class="btn btn-dark btn-block" id="btn-logout">Se déconnecter</button>
    </section>
    <section class="card flat"><h3>À propos</h3>
      <p class="muted">Sunrise Space met en relation clients et chauffeurs (moto-taxi, taxi, tricycle) partout en Côte d'Ivoire. Le prix de la course se discute directement avec le chauffeur et se règle en espèces.</p>
    </section>`;
  $('#btn-pw').onclick = openChangePassword;
  $('#btn-logout').onclick = logout;
  const ba = $('#btn-alarm');
  if (ba) {
    ba.onclick = () => { try { localStorage.setItem('ss_alarm', alarmEnabled() ? '0' : '1'); } catch { /* rien */ } accountView(); };
    $('#btn-alarm-test').onclick = () => { unlockAudio(); startAlarm(true); };
  }
  const bp = $('#btn-push');
  if (bp) bp.onclick = async () => { const ok = await enablePush(); toast(ok ? 'Notifications activées ✅' : 'Notifications non activées', ok ? undefined : 'bad'); updatePushBar(); accountView(); };
}

/* ---------- Signalement ---------- */
function openReport(targetId, targetName, requestId) {
  const reasons = Object.entries(state.meta.report_reasons || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
  const m = modal(`<h2>Signaler ${esc(targetName)}</h2>
    <p class="muted">Votre signalement est envoyé à l'équipe Sunrise Space qui l'examinera.</p>
    <label>Motif<select id="r-reason"><option value="">Choisir…</option>${reasons}</select></label>
    <label>Détails <span class="hint">(facultatif)</span><textarea id="r-details" maxlength="500"></textarea></label>
    <p class="banner bad" id="r-err" hidden></p>
    <div class="row"><button class="btn btn-ghost grow" id="r-cancel">Annuler</button><button class="btn btn-danger grow" id="r-send">Envoyer</button></div>`);
  $('#r-cancel', m.el).onclick = m.close;
  $('#r-send', m.el).onclick = async () => {
    const reason = $('#r-reason', m.el).value;
    if (!reason) { const e = $('#r-err', m.el); e.textContent = 'Choisissez un motif.'; e.hidden = false; return; }
    try {
      await api('/api/reports', { method: 'POST', body: { target_user_id: targetId, reason, details: $('#r-details', m.el).value, request_id: requestId || null } });
      m.close(); toast('Signalement envoyé. Merci.');
    } catch (e) { const el = $('#r-err', m.el); el.textContent = e.message; el.hidden = false; }
  };
}

/* ======================= CLIENT ======================= */
function empty(icon, title, text) {
  return `<div class="empty"><div class="big">${icon}</div><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
}

async function clientRequest() {
  $('#main').innerHTML = '<div id="reqbox"></div><div id="hist" class="stack"></div>';
  let lastKey = null, destPick = null;   // destPick : point d'arrivée choisi (recherche ou carte)

  function destNote() {
    const n = $('#dnote');
    if (!n) return;
    const km = destPick && state.pos ? distKm(state.pos, destPick) : null;
    n.textContent = destPick
      ? `✅ Destination placée sur la carte${km != null ? ` · environ ${km.toFixed(1).replace('.', ',')} km` : ''}. Le chauffeur verra la distance et l'itinéraire.`
      : 'Astuce : choisissez un lieu proposé ou placez-le sur la carte, le chauffeur verra la distance et l\'itinéraire.';
  }

  function bindDestination() {
    const inp = $('#dest'), box = $('#dsug');
    if (!inp) return;
    let timer;
    inp.addEventListener('input', () => {
      destPick = null; destNote();
      clearTimeout(timer);
      const q = inp.value.trim();
      if (q.length < 3) { box.hidden = true; return; }
      timer = setTimeout(async () => {
        try {
          const p = state.pos;
          const r = await api(`/api/geocode?q=${encodeURIComponent(q)}${p ? `&lat=${p.lat}&lng=${p.lng}` : ''}`);
          if (inp.value.trim() !== q) return;
          box._res = r.results || [];
          box.innerHTML = box._res.map((x, i) => `<button type="button" class="sug" data-i="${i}">📍 ${esc(x.label)}</button>`).join('');
          box.hidden = !box._res.length;
        } catch { /* la saisie libre reste possible */ }
      }, 600);
    });
    box.addEventListener('click', (e) => {
      const b = e.target.closest('[data-i]');
      if (!b) return;
      const x = box._res[Number(b.dataset.i)];
      inp.value = x.label.slice(0, 120); destPick = { lat: x.lat, lng: x.lng }; box.hidden = true; destNote();
    });
    $('#btn-pick').onclick = pickOnMap;
    destNote();
  }

  function pickOnMap() {
    if (!window.L) { toast('La carte est indisponible pour le moment. Tapez le nom du lieu.', 'bad'); return; }
    const start = destPick || state.pos || { lat: 5.36, lng: -4.0083 };
    const m = modal(`<h2>Choisir la destination</h2><p class="muted tiny">Touchez la carte à l'endroit où vous allez, puis confirmez.</p>
      <div id="pickmap" class="map" style="height:52vh"></div>
      <div class="row"><button class="btn btn-ghost grow" id="pk-cancel">Annuler</button><button class="btn grow" id="pk-ok" disabled>Confirmer ce lieu</button></div>`);
    const map = L.map('pickmap').setView([start.lat, start.lng], destPick ? 16 : 14);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
    setTimeout(() => map.invalidateSize(), 200);
    let mk = null, pick = destPick;
    const put = (ll) => { pick = { lat: ll.lat, lng: ll.lng }; if (mk) mk.setLatLng(ll); else mk = L.marker(ll, { icon: pinIcon('🏁') }).addTo(map); $('#pk-ok', m.el).disabled = false; };
    if (destPick) put(destPick);
    if (state.pos) L.marker([state.pos.lat, state.pos.lng], { icon: pinIcon('🙋'), interactive: false }).addTo(map);
    map.on('click', (e) => put(e.latlng));
    const done = () => { try { map.remove(); } catch { /* rien */ } m.close(); };
    $('#pk-cancel', m.el).onclick = done;
    $('#pk-ok', m.el).onclick = () => {
      destPick = pick; done();
      const inp = $('#dest');
      if (inp && inp.value.trim().length < 2) inp.value = 'Lieu choisi sur la carte';
      const box = $('#dsug'); if (box) box.hidden = true;
      destNote();
    };
  }

  function freshGps() { return state.pos && state.pos.source === 'gps' && Date.now() - (state.pos.at || 0) < 60000; }

  function formHTML() {
    return `<section class="card stack">
      <h2>Demander un chauffeur</h2>
      <p class="muted">Votre position est détectée automatiquement. Indiquez seulement où vous allez.</p>
      <div class="posline" id="posline">${freshGps() ? posLineHTML() : '<span class="chip">📡 Détection de votre position…</span>'}</div>
      <button type="button" class="link" id="btn-redetect" style="justify-self:start">↻ Actualiser ma position</button>
      <form id="reqform" class="form" style="margin-top:0">
        <label>Où allez-vous ?<input name="dest" id="dest" required maxlength="120" autocomplete="off" placeholder="Ex. Marché de Cocody"></label>
        <div class="sugs" id="dsug" hidden></div>
        <button type="button" class="btn btn-ghost btn-sm" id="btn-pick" style="justify-self:start">📍 Choisir la destination sur la carte</button>
        <p class="tiny muted" id="dnote"></p>
        <label>Quel véhicule ?<select name="vtype" id="vtype"><option value="any">Peu importe (le plus proche)</option>${Object.entries(VEHICLES).map(([k, v]) => `<option value="${k}"${state.wantType === k ? ' selected' : ''}>${v.icon} ${esc(v.label)}</option>`).join('')}</select></label>
        <p class="tiny muted" id="vhint"></p>
        <p class="banner bad" id="req-err" role="alert" hidden></p>
        <button class="btn btn-block" type="submit">🛵 Demander un chauffeur</button>
      </form></section>`;
  }

  async function detectPosition() {
    const line = $('#posline');
    if (!line) return;
    line.innerHTML = '<span class="chip">📡 Détection de votre position…</span>';
    try { setPos({ ...(await getPosition()), source: 'gps' }); }
    catch (e) {
      const l = $('#posline');
      if (l) l.innerHTML = `<span class="chip bad">⚠ Position introuvable</span><p class="tiny muted">${esc(e.message)}</p>`;
    }
  }

  function driverBlock(d, rid) {
    return `<div class="card flat">
      <div class="row"><div class="avatar" aria-hidden="true">🏍️</div><div class="grow"><h3>${esc(d.name)}</h3><p class="muted">${esc(d.moto_model || veh(d.vehicle_type).label)} · ${esc(d.plate)}</p>${d.rating_avg != null ? `<p class="tiny muted">${starsText(d.rating_avg, d.rating_count)}</p>` : ''}</div></div>
      <div class="row" style="margin-top:10px"><button class="btn btn-sm btn-block" data-call="${rid}">📞 Appeler le chauffeur (Sunrise)</button></div>
      <p class="tiny muted">🔒 Les numéros de téléphone restent privés : l'appel passe par Sunrise.</p></div>`;
  }

  function paint(r) {
    const box = $('#reqbox');
    const key = r ? `${r.id}:${r.status}` : 'none';
    if (key === lastKey) {
      const a = $('#age'); if (a && r) a.textContent = ago(r.age_min);
      if (r && r.status === 'accepted' && r.driver) updateClientMap(r, false);  // la moto avance : on met la carte à jour
      return;
    }
    lastKey = key;
    destroyMap();
    if (!r) {
      box.innerHTML = formHTML();
      destPick = null;
      $('#reqform').onsubmit = submit;
      $('#btn-redetect').onclick = detectPosition;
      bindDestination();
      if (!freshGps()) detectPosition();
      return;
    }
    const wantedLabel = r.vehicle_type && r.vehicle_type !== 'any' ? `${veh(r.vehicle_type).icon} ${esc(veh(r.vehicle_type).label)}` : 'Peu importe';
    const where = `<dl class="kv"><dt>Véhicule</dt><dd>${wantedLabel}</dd><dt>Départ</dt><dd>${esc(r.pickup_text || 'Ma position (GPS)')}</dd><dt>Destination</dt><dd>${esc(r.destination_text || '—')}</dd>${r.note ? `<dt>Précision</dt><dd>${esc(r.note)}</dd>` : ''}</dl>`;
    if (r.status === 'open') {
      box.innerHTML = `<section class="card stack"><h2><span class="pulse"></span>Recherche d'un chauffeur…</h2>
        <p class="muted">Demande envoyée <span id="age">${esc(ago(r.age_min))}</span>. Elle expire au bout de 30 minutes.</p>${where}
        <button class="btn btn-ghost btn-block" id="btn-cancel">Annuler la demande</button></section>`;
    } else if (r.status === 'accepted' && r.driver) {
      box.innerHTML = `<section class="card stack job"><h2>✅ Un chauffeur arrive !</h2>
        <div id="cmap" class="map" aria-label="Carte : votre position et le chauffeur"></div>
        <p id="ceta" class="muted">Recherche du chauffeur sur la carte…</p>${driverBlock(r.driver, r.id)}${where}
        <button class="btn btn-ok btn-block" id="btn-done">J'ai terminé ma course</button>
        <div class="row"><button class="btn btn-ghost btn-sm grow" id="btn-cancel">Annuler</button>
        <button class="btn btn-ghost btn-sm grow" data-report="${r.driver.id}" data-name="${esc(r.driver.name)}">⚑ Signaler</button></div></section>`;
    } else {
      const label = { completed: ['🎉', 'Course terminée', 'Merci d\'avoir utilisé Sunrise Space.'], cancelled: ['✖️', 'Demande annulée', ''], expired: ['⌛', 'Demande expirée', "Aucun chauffeur n'a pu la prendre. Réessayez."] }[r.status] || ['', '', ''];
      box.innerHTML = `<section class="card stack"><div class="empty"><div class="big">${label[0]}</div><h3>${label[1]}</h3><p>${esc(label[2])}</p></div>
        ${r.status === 'completed' && r.driver ? ratingHTML(r) : ''}
        ${r.status === 'completed' && r.driver ? `<button class="btn btn-ghost btn-sm" data-report="${r.driver.id}" data-name="${esc(r.driver.name)}">⚑ Signaler ce chauffeur</button>` : ''}
        <button class="btn btn-block" id="btn-new">Nouvelle demande</button></section>`;
      const nb = $('#btn-new'); if (nb) nb.onclick = () => { lastKey = null; paint(null); };
    }
    if (r.status === 'accepted' && r.driver) initClientMap(r);
    const c = $('#btn-cancel'); if (c) c.onclick = () => act(`/api/requests/${r.id}/cancel`, 'Demande annulée');
    const d = $('#btn-done'); if (d) d.onclick = () => act(`/api/requests/${r.id}/complete`, 'Course terminée');
  }

  async function act(url, msg) {
    try { await api(url, { method: 'POST' }); toast(msg); lastKey = null; load(); loadHist(); }
    catch (e) { toast(e.message, 'bad'); }
  }

  async function submit(ev) {
    ev.preventDefault();
    const f = ev.target, err = $('#req-err'), btn = f.querySelector('button[type=submit]');
    err.hidden = true;
    const dest = f.dest.value.trim();
    if (dest.length < 2) { err.textContent = 'Indiquez votre destination.'; err.hidden = false; return; }
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = '📡 Détection de votre position…';
    try {
      let p = freshGps() ? state.pos : null;
      if (!p) { p = { ...(await getPosition()), source: 'gps' }; setPos(p); }
      if (p.accuracy > 150 && !confirm(`Votre position est approximative (±${p.accuracy} m). Le chauffeur risque de ne pas vous trouver. Envoyer quand même ?`)) {
        throw new Error("Demande non envoyée. Sortez à l'air libre, touchez « Actualiser ma position », puis réessayez.");
      }
      await api('/api/requests', { method: 'POST', body: { lat: p.lat, lng: p.lng, accuracy: p.accuracy, pickup_text: '', destination_text: dest, note: '', vehicle_type: f.vtype.value, ...(destPick ? { dest_lat: destPick.lat, dest_lng: destPick.lng } : {}) } });
      state.wantType = f.vtype.value === 'any' ? '' : f.vtype.value;
      toast('Demande envoyée !'); lastKey = null; load();
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  async function load() {
    try {
      const r = (await api('/api/requests/current')).request;
      paint(r);
      if (r && (r.status === 'open' || r.status === 'accepted')) startClientTracking(r.id); else stopClientTracking();
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }

  const STATUS = { open: ['En recherche', 'sun'], accepted: ['En cours', 'ok'], completed: ['Terminée', 'ok'], cancelled: ['Annulée', ''], expired: ['Expirée', ''] };
  async function loadHist() {
    try {
      const { requests } = await api('/api/requests/history');
      $('#hist').innerHTML = requests.length ? `<h2>Mes dernières demandes</h2>` + requests.slice(0, 6).map((r) => `
        <div class="card flat"><div class="row"><b class="grow">${esc(r.destination_text || 'Course')}</b><span class="chip ${STATUS[r.status][1]}">${STATUS[r.status][0]}</span></div>
        ${r.driver ? `<p class="tiny muted">Chauffeur : ${esc(r.driver.name)} · ${esc(r.driver.plate)}</p>` : ''}</div>`).join('') : '';
    } catch { /* l'historique est secondaire */ }
  }

  $('#reqbox').addEventListener('click', async (e) => {
    const st = e.target.closest('[data-stars]');
    if (st) {
      try { await api(`/api/requests/${st.dataset.rid}/rate`, { method: 'POST', body: { stars: Number(st.dataset.stars) } }); toast('Merci pour votre note ⭐'); lastKey = null; load(); }
      catch (err) { toast(err.message, 'bad'); }
      return;
    }
    const b = e.target.closest('[data-report]'); if (b) openReport(Number(b.dataset.report), b.dataset.name, null);
  });
  await load(); loadHist();
  every(5000, load);
  every(30000, loadHist);
}

/* ======================= CHAUFFEUR ======================= */
let wakeLock = null;
async function keepAwake() {
  try {
    if (!('wakeLock' in navigator) || wakeLock) return;
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch { wakeLock = null; }
}
function releaseWake() { try { if (wakeLock) wakeLock.release(); } catch { /* rien */ } wakeLock = null; }
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.locTimer) keepAwake(); });

function stopLocSharing() {
  if (state.locTimer) { try { navigator.geolocation.clearWatch(state.locTimer.id); } catch { /* rien */ } }
  state.locTimer = null; state.locOk = null; state.locAcc = null; state.locLast = 0; state.locWeak = false; state.locDenied = false; state.lastFix = null;
  releaseWake();
}

function refreshBadge() { const b = $('#locbadge'); if (b) b.innerHTML = locBadge(); }

async function onDriverFix(p) {
  const acc = Math.round(p.coords.accuracy || 0);
  state.locAcc = acc; state.locDenied = false;
  if (acc > 150) { state.locWeak = true; refreshBadge(); return; }   // trop imprécis : on ne l'envoie pas
  state.locWeak = false;
  state.lastFix = { lat: p.coords.latitude, lng: p.coords.longitude };
  updateMaps();
  const t = Date.now();
  if (t - (state.locLast || 0) < 8000) { refreshBadge(); return; }  // un envoi toutes les ~8 s
  state.locLast = t;
  try {
    await api('/api/driver/location', { method: 'POST', body: { lat: p.coords.latitude, lng: p.coords.longitude, accuracy: acc } });
    state.locOk = true;
  } catch { state.locOk = false; }
  refreshBadge();
}

function onDriverErr(e) { state.locOk = false; state.locDenied = e.code === 1; refreshBadge(); }

function startLocSharing() {
  stopLocSharing();
  if (!navigator.geolocation) { state.locOk = false; return; }
  const id = navigator.geolocation.watchPosition(onDriverFix, onDriverErr, { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 });
  state.locTimer = { id };
  keepAwake();
}

function locBadge() {
  if (state.locDenied) return '<span class="chip bad">⚠ Localisation refusée</span>';
  if (state.locWeak) return `<span class="chip warn">📡 Signal GPS faible (±${state.locAcc} m) : position non envoyée</span>`;
  if (state.locOk === false) return '<span class="chip bad">⚠ Position non partagée</span>';
  if (state.locOk) return `<span class="chip ok">📡 Position partagée${state.locAcc != null ? ` · ±${state.locAcc} m` : ''}</span>`;
  return '<span class="chip">📡 Localisation…</span>';
}

/* ---------- Cartes du chauffeur : client + itinéraire ---------- */
const OSRM_URL = 'https://router.project-osrm.org/route/v1/driving';
const jobMap = { job: null, cm: null, dm: null, dst: null, trip: null, tripLine: null, phase: 'pickup', line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false };
const reqMap = { dm: null };
const cliMap = { req: null, cm: null, dm: null, line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false };

function destroyMap() {
  if (state.map) { try { state.map.remove(); } catch { /* rien */ } }
  state.map = null; state.layers = null; state.meMarker = null; state.accCircle = null;
  Object.assign(jobMap, { job: null, cm: null, dm: null, dst: null, trip: null, tripLine: null, phase: 'pickup', line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false });
  reqMap.dm = null;
  Object.assign(cliMap, { req: null, cm: null, dm: null, line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false });
}

function distKm(a, b) {
  const r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLng = (b.lng - a.lng) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLng / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

const pinIcon = (emoji) => L.divIcon({ className: 'pin', html: emoji, iconSize: [34, 34], iconAnchor: [17, 17] });
const appleLink = (lat, lng) => `https://maps.apple.com/?daddr=${lat},${lng}&dirflg=d`;
const myPos = () => state.lastFix || (state.driver && state.driver.lat != null ? { lat: state.driver.lat, lng: state.driver.lng } : null);

async function fetchRoute(a, b) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 8000);
  try {
    const res = await fetch(`${OSRM_URL}/${a.lng},${a.lat};${b.lng},${b.lat}?overview=full&geometries=geojson`, { signal: ctl.signal });
    if (!res.ok) return null;
    const data = await res.json();
    const rt = data.routes && data.routes[0];
    if (!rt) return null;
    return { coords: rt.geometry.coordinates.map(([lng, lat]) => [lat, lng]), km: Math.round(rt.distance / 100) / 10, min: Math.max(1, Math.round(rt.duration / 60)) };
  } catch { return null; } finally { clearTimeout(timer); }
}

function setEta(html) { const el = $('#jobeta'); if (el) el.innerHTML = html; }

function newMap(elId) {
  const map = L.map(elId, { zoomControl: true });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
  state.map = map;
  state.layers = L.layerGroup().addTo(map);
  setTimeout(() => map.invalidateSize(), 150);
  return map;
}

const hasDest = (job) => job && job.dest_lat != null && job.dest_lng != null;
const kmFr = (km) => km.toFixed(1).replace('.', ',');

/* Cible actuelle de la navigation : le client (étape 1) ou la destination (étape 2). */
function jobTarget() {
  const job = jobMap.job;
  if (hasDest(job) && jobMap.phase === 'dest') return { lat: job.dest_lat, lng: job.dest_lng, dest: true };
  return { lat: job.lat, lng: job.lng, dest: false };
}

function updateNavLinks() {
  const job = jobMap.job;
  if (!job) return;
  const t = jobTarget();
  const la = $('#lnk-apple'), lg = $('#lnk-google'), nt = $('#navtarget');
  if (la) la.href = appleLink(t.lat, t.lng);
  if (lg) lg.href = mapsLink(t.lat, t.lng);
  if (nt) nt.textContent = t.dest ? '🏁 Navigation vers : la destination' : '🙋 Navigation vers : le client';
  $$('#phaseseg [data-ph]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.ph === (t.dest ? 'dest' : 'pickup'))));
}

function setPhase(ph) {
  const job = jobMap.job;
  if (!job) return;
  jobMap.phase = hasDest(job) && ph === 'dest' ? 'dest' : 'pickup';
  try { localStorage.setItem('ss_phase_' + job.id, jobMap.phase); } catch { /* rien */ }
  jobMap.fitted = false; jobMap.lastFrom = null;
  updateNavLinks();
  refreshJobRoute(true);
}

function showTrip() {
  const el = $('#jobtrip'), job = jobMap.job;
  if (!el || !hasDest(job)) return;
  const tr = jobMap.trip;
  el.innerHTML = tr
    ? `🏁 Trajet du client : <b>${kmFr(tr.km)} km</b> · environ <b>${tr.min} min</b> par la route`
    : job.trip_km != null ? `🏁 Trajet du client : environ <b>${kmFr(job.trip_km)} km</b> <span class="tiny">(à vol d'oiseau)</span>` : '';
}

function initJobMap(job) {
  const el = $('#jobmap');
  if (!el) return;
  jobMap.job = job;
  let ph = 'pickup';
  try { ph = localStorage.getItem('ss_phase_' + job.id) || 'pickup'; } catch { /* rien */ }
  jobMap.phase = hasDest(job) && ph === 'dest' ? 'dest' : 'pickup';
  updateNavLinks();
  showTrip();
  if (!window.L) {
    el.innerHTML = '<div class="map-fallback">Carte indisponible. Utilisez les boutons Plans ou Google Maps.</div>';
    refreshJobRoute(true);
    return;
  }
  const map = newMap('jobmap').setView([job.lat, job.lng], 15);
  jobMap.cm = L.marker([job.lat, job.lng], { icon: pinIcon('🙋'), zIndexOffset: 900 }).bindPopup(`Client : ${esc(job.client_name)}`).addTo(map);
  if (hasDest(job)) {
    jobMap.dst = L.marker([job.dest_lat, job.dest_lng], { icon: pinIcon('🏁'), zIndexOffset: 800 }).bindPopup(`Destination : ${esc(job.destination_text || '')}`).addTo(map);
    fetchRoute({ lat: job.lat, lng: job.lng }, { lat: job.dest_lat, lng: job.dest_lng }).then((r) => {   // trajet du client (une seule fois)
      if (state.map !== map || !r) return;
      jobMap.trip = r; showTrip();
      jobMap.tripLine = L.polyline(r.coords, { color: '#1E6CFF', weight: 4, opacity: 0.55, dashArray: '2 8' }).addTo(map);
    });
  }
  refreshJobRoute(true);
}

async function refreshJobRoute(force) {
  const job = jobMap.job, map = state.map;
  if (!job) return;
  const me = myPos();
  if (!window.L || !map) {   // sans carte : distance à vol d'oiseau vers la cible
    if (me) { const t = jobTarget(); setEta(`${t.dest ? '🏁 Vers la destination' : '🛵 Vers le client'} : <b>${fmtDist(distKm(me, t))}</b> à vol d'oiseau`); }
    return;
  }
  if (!me) { setEta('En attente de votre position GPS…'); return; }
  const tgt = jobTarget();
  if (jobMap.dm) jobMap.dm.setLatLng([me.lat, me.lng]);
  else jobMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon(veh(state.driver && state.driver.vehicle_type).icon), zIndexOffset: 1000 }).addTo(map);
  if (distKm(me, tgt) * 1000 < 30) {  // déjà sur place : pas de ligne à tracer
    if (jobMap.line) { map.removeLayer(jobMap.line); jobMap.line = null; }
    setEta(tgt.dest ? '🏁 Vous êtes <b>arrivé à la destination</b>' : '📍 Vous êtes <b>au point de rendez-vous</b>');
    return;
  }
  const moved = jobMap.lastFrom ? distKm(jobMap.lastFrom, me) * 1000 : Infinity;
  if (!force && moved < 80 && Date.now() - jobMap.lastAt < 45000) return;
  if (jobMap.busy) return;
  jobMap.busy = true; jobMap.lastFrom = me; jobMap.lastAt = Date.now();
  const route = await fetchRoute(me, tgt);
  jobMap.busy = false;
  if (state.map !== map) return;  // la carte a été remplacée entre-temps
  if (jobTarget().dest !== tgt.dest) return refreshJobRoute(true);   // le chauffeur a changé d'étape pendant le calcul
  if (jobMap.line) map.removeLayer(jobMap.line);
  const pts = route ? route.coords : [[me.lat, me.lng], [tgt.lat, tgt.lng]];
  jobMap.line = L.polyline(pts, { color: tgt.dest ? '#1E9E5A' : '#FF7A1A', weight: 5, opacity: 0.9, dashArray: route ? null : '8 8' }).addTo(map);
  if (!jobMap.fitted) { jobMap.fitted = true; map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 17 }); }
  const what = tgt.dest ? '🏁 Vers la destination' : '🛵 Vers le client';
  setEta(route
    ? `${what} : environ <b>${route.min} min</b> · ${kmFr(route.km)} km par la route`
    : `${what} : <b>${fmtDist(distKm(me, tgt))}</b> à vol d'oiseau <span class="tiny">(itinéraire routier indisponible, utilisez Plans ou Google Maps)</span>`);
}

function initReqMap(requests) {
  const el = $('#reqmap');
  if (!el) return;
  if (!window.L) { el.innerHTML = '<div class="map-fallback">Carte indisponible. Utilisez la liste ci-dessous.</div>'; return; }
  const map = newMap('reqmap');
  const pts = [];
  requests.forEach((r) => {
    L.marker([r.lat, r.lng], { icon: pinIcon('🙋') }).bindPopup(`<b>${esc(r.client_name)}</b><br>${fmtDist(r.distance_km)}`).addTo(state.layers);
    pts.push([r.lat, r.lng]);
  });
  const me = myPos();
  if (me) { reqMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon(veh(state.driver && state.driver.vehicle_type).icon), zIndexOffset: 1000 }).addTo(map); pts.push([me.lat, me.lng]); }
  map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 16 });
}

function stopClientTracking() {
  if (state.cliWatch) { try { navigator.geolocation.clearWatch(state.cliWatch.id); } catch { /* rien */ } }
  state.cliWatch = null;
}

/* Tant que la demande est ouverte ou acceptée, la position du client est envoyée automatiquement. */
function startClientTracking(rid) {
  if (state.cliWatch && state.cliWatch.rid === rid) return;
  stopClientTracking();
  if (!navigator.geolocation) return;
  const w = { rid, last: null, at: 0, id: null };
  w.id = navigator.geolocation.watchPosition(async (p) => {
    if (p.coords.accuracy > 150) return;
    const cur = { lat: p.coords.latitude, lng: p.coords.longitude };
    const now = Date.now();
    if (now - w.at < 8000) return;
    if (w.last && distKm(w.last, cur) * 1000 < 25) return;
    w.at = now; w.last = cur;
    try { await api(`/api/requests/${rid}/location`, { method: 'POST', body: cur }); } catch { /* on réessaiera au prochain point */ }
  }, () => { /* refus : la position initiale reste valable */ }, { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 });
  state.cliWatch = w;
}

function setCEta(html) { const el = $('#ceta'); if (el) el.innerHTML = html; }

/* Côté client : voir la moto arriver (position du chauffeur, trajet et temps estimé). */
function initClientMap(r) {
  const el = $('#cmap');
  if (!el) return;
  if (!window.L) {
    el.innerHTML = '<div class="map-fallback">Carte indisponible. Appelez le chauffeur pour vous coordonner.</div>';
    updateClientMap(r, true);
    return;
  }
  const map = newMap('cmap').setView([r.lat, r.lng], 15);
  cliMap.cm = L.marker([r.lat, r.lng], { icon: pinIcon('🙋'), zIndexOffset: 900 }).bindPopup('Vous êtes ici').addTo(map);
  updateClientMap(r, true);
}

async function updateClientMap(r, force) {
  cliMap.req = r;
  const d = r.driver;
  if (!d || d.lat == null) { setCEta('En attente de la position du chauffeur…'); return; }
  const me = { lat: d.lat, lng: d.lng }, dest = { lat: r.lat, lng: r.lng };
  const map = state.map;
  if (cliMap.cm) cliMap.cm.setLatLng([dest.lat, dest.lng]);  // le client a pu bouger
  if (!window.L || !map) { setCEta(`Le chauffeur est à <b>${fmtDist(distKm(me, dest))}</b> (à vol d'oiseau)`); return; }
  if (cliMap.dm) cliMap.dm.setLatLng([me.lat, me.lng]);
  else cliMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon(veh(d.vehicle_type).icon), zIndexOffset: 1000 }).addTo(map);
  if (distKm(me, dest) * 1000 < 30) {  // le chauffeur est là : pas de ligne à tracer
    if (cliMap.line) { map.removeLayer(cliMap.line); cliMap.line = null; }
    setCEta(`${veh(d.vehicle_type).icon} Le chauffeur est <b>tout près</b> de vous`);
    return;
  }
  const moved = cliMap.lastFrom ? distKm(cliMap.lastFrom, me) * 1000 : Infinity;
  if (!force && moved < 80 && Date.now() - cliMap.lastAt < 45000) return;
  if (cliMap.busy) return;
  cliMap.busy = true; cliMap.lastFrom = me; cliMap.lastAt = Date.now();
  const route = await fetchRoute(me, dest);
  cliMap.busy = false;
  if (state.map !== map) return;
  if (cliMap.line) map.removeLayer(cliMap.line);
  const pts = route ? route.coords : [[me.lat, me.lng], [dest.lat, dest.lng]];
  cliMap.line = L.polyline(pts, { color: '#FF7A1A', weight: 5, opacity: 0.9, dashArray: route ? null : '8 8' }).addTo(map);
  if (!cliMap.fitted) { cliMap.fitted = true; map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 17 }); }
  setCEta(route
    ? `${veh(d.vehicle_type).icon} Arrivée dans environ <b>${route.min} min</b> · à ${route.km.toFixed(1).replace('.', ',')} km`
    : `Le chauffeur est à <b>${fmtDist(distKm(me, dest))}</b> (à vol d'oiseau)`);
}

function updateMaps() {
  const me = myPos();
  if (reqMap.dm && me) reqMap.dm.setLatLng([me.lat, me.lng]);
  if (jobMap.job) refreshJobRoute(false);
}

async function driverHome() {
  $('#main').innerHTML = '<div id="dhome" class="stack"><div class="empty"><p>Chargement…</p></div></div>';
  let sig = null, last = null, seen = null;

  async function load(force) {
    try { last = await api('/api/driver/dashboard'); } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); return; }
    state.driver = last.driver;
    const a = last.driver.availability;
    const ids = last.requests.map((r) => r.id);
    if (seen && a === 'available' && !last.job && ids.some((id) => !seen.has(id))) startAlarm();   // nouvelle demande : sirène
    if (a !== 'available' || !ids.length) stopAlarm();
    seen = new Set(ids);
    if ((a === 'available' || a === 'busy') && !state.locTimer) startLocSharing();
    if (a === 'unavailable' && state.locTimer) stopLocSharing();
    const s = JSON.stringify([a, last.driver.approval, last.job && last.job.id, last.requests.map((r) => r.id)]);
    if (force || s !== sig) { sig = s; paint(last); }
    else {  // même contenu : on met à jour distance et ancienneté sans redessiner (la carte reste en place)
      last.requests.forEach((r) => {
        const c = $('#rq-d-' + r.id); if (c) c.textContent = fmtDist(r.distance_km);
        const t = $('#rq-a-' + r.id); if (t) t.textContent = 'Demandé ' + ago(r.age_min);
      });
    }
    if (last.job && jobMap.job && jobMap.job.id === last.job.id) {  // le client a pu bouger
      jobMap.job.lat = last.job.lat; jobMap.job.lng = last.job.lng;
      if (jobMap.cm) jobMap.cm.setLatLng([last.job.lat, last.job.lng]);
      updateNavLinks();
    }
    updateMaps();
  }

  function paint({ driver: d, job, requests }) {
    let banner = '';
    if (d.approval === 'pending') banner = '<div class="banner warn"><b>Profil en cours de vérification.</b> L\'équipe Sunrise Space contrôle votre plaque et votre permis. Vous pourrez passer « Disponible » dès la validation.</div>';
    if (d.approval === 'rejected') banner = `<div class="banner bad"><b>Profil refusé.</b> ${esc(d.approval_note || 'Contactez le support Sunrise Space.')}</div>`;
    const canToggle = d.approval === 'approved' && d.availability !== 'busy';
    const label = { available: 'Disponible', busy: 'Course en cours', unavailable: 'Indisponible' }[d.availability];
    const hint = { available: 'Vous recevez les demandes des clients proches.', busy: 'Terminez votre course pour redevenir disponible.', unavailable: 'Vous ne recevez aucune demande.' }[d.availability];
    let html = `${banner}<section class="card statuscard ${d.availability}">
      <div class="grow"><p class="tiny muted">Mon statut</p><h2>${label}</h2><p class="muted tiny">${hint}</p><p style="margin-top:6px" id="locbadge">${d.availability === 'unavailable' ? '' : locBadge()}</p></div>
      <button class="switch ${d.availability === 'available' ? 'on' : ''}" id="btn-toggle" role="switch" aria-checked="${d.availability === 'available'}" aria-label="Je suis disponible" ${canToggle ? '' : 'disabled'}><span></span></button></section>`;

    if (job) {
      html += `<section class="card job stack"><h2>🛵 Course en cours</h2>
        <dl class="kv"><dt>Client</dt><dd>${esc(job.client_name)}</dd><dt>Départ</dt><dd>${esc(job.pickup_text || 'Position GPS du client (voir la carte)')}</dd><dt>Destination</dt><dd>${esc(job.destination_text || '—')}</dd>
        ${job.note ? `<dt>Précision</dt><dd>${esc(job.note)}</dd>` : ''}${job.distance_km != null ? `<dt>Distance</dt><dd>${fmtDist(job.distance_km)}</dd>` : ''}</dl>
        <button class="btn btn-sm btn-block" data-call="${job.id}">📞 Appeler le client (Sunrise)</button>
        <p class="tiny muted">🔒 Le numéro du client reste privé : l'appel passe par Sunrise.</p>
        ${hasDest(job) ? `<div class="seg" id="phaseseg" role="group" aria-label="Étape de la course"><button type="button" data-ph="pickup" aria-pressed="true">① Rejoindre le client</button><button type="button" data-ph="dest" aria-pressed="false">② Aller à la destination</button></div>` : ''}
        <div id="jobmap" class="map" aria-label="Carte : votre position, le client et la destination"></div>
        <p id="jobeta" class="muted">Calcul de l'itinéraire…</p>
        <p id="jobtrip" class="muted"></p>
        <p id="navtarget" class="tiny muted"></p>
        <div class="row"><a id="lnk-apple" class="btn btn-dark btn-sm grow" target="_blank" rel="noopener" href="${appleLink(job.lat, job.lng)}">🧭 Plans (Apple)</a>
          <a id="lnk-google" class="btn btn-dark btn-sm grow" target="_blank" rel="noopener" href="${mapsLink(job.lat, job.lng)}">🧭 Google Maps</a></div>
        ${!hasDest(job) && job.destination_text ? `<div class="row"><a class="btn btn-ghost btn-sm grow" target="_blank" rel="noopener" href="https://maps.apple.com/?daddr=${encodeURIComponent(job.destination_text + ', Côte d\'Ivoire')}&dirflg=d">🏁 Destination : Plans</a>
          <a class="btn btn-ghost btn-sm grow" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(job.destination_text + ', Côte d\'Ivoire')}">🏁 Destination : Google</a></div>` : ''}
        <button class="btn btn-ok btn-block" data-act="complete" data-id="${job.id}">Course terminée</button>
        <div class="row"><button class="btn btn-ghost btn-sm grow" data-act="release" data-id="${job.id}">Abandonner</button>
        <button class="btn btn-ghost btn-sm grow" data-act="report" data-cid="${job.client_id || ''}" data-name="${esc(job.client_name)}" data-id="${job.id}">⚑ Signaler le client</button></div></section>`;
    } else if (d.approval === 'approved') {
      html += '<div class="section-head"><h2>Demandes près de vous</h2></div>';
      if (d.availability !== 'available') html += empty('😴', 'Vous êtes indisponible', 'Passez « Disponible » pour recevoir les demandes des clients proches.');
      else if (!requests.length) html += empty('🌅', 'Aucune demande pour le moment', 'Restez disponible : les nouvelles demandes apparaissent ici automatiquement.');
      else html += '<p class="muted tiny">🔒 Le numéro du client n\'est jamais affiché : après acceptation, vous l\'appelez via Sunrise.</p><div id="reqmap" class="map" aria-label="Carte des demandes"></div><div class="list">' + requests.map((r) => `<article class="card item">
          <h3>${esc(r.client_name)} <span class="chip sun" id="rq-d-${r.id}">${fmtDist(r.distance_km)}</span>${r.vehicle_type && r.vehicle_type !== 'any' ? `<span class="chip">Demande : ${veh(r.vehicle_type).icon} ${esc(veh(r.vehicle_type).short)}</span>` : ''}</h3>
          <dl class="kv"><dt>Départ</dt><dd>${esc(r.pickup_text || 'Position du client')}</dd><dt>Destination</dt><dd>${esc(r.destination_text || '—')}</dd>${r.trip_km != null ? `<dt>Trajet</dt><dd>🏁 environ ${kmFr(r.trip_km)} km <span class="tiny muted">(à vol d'oiseau)</span></dd>` : ''}${r.note ? `<dt>Précision</dt><dd>${esc(r.note)}</dd>` : ''}</dl>
          <p class="tiny muted" id="rq-a-${r.id}">Demandé ${esc(ago(r.age_min))}</p>
          <div class="actions"><button class="btn grow" data-act="accept" data-id="${r.id}">✅ Accepter la course</button></div></article>`).join('') + '</div>';
    }
    destroyMap();
    $('#dhome').innerHTML = html;
    if (job) initJobMap(job);
    else if (d.approval === 'approved' && d.availability === 'available' && requests.length) initReqMap(requests);
  }

  $('#dhome').addEventListener('click', async (e) => {
    const ph = e.target.closest('#phaseseg [data-ph]');
    if (ph) { setPhase(ph.dataset.ph); return; }
    const t = e.target.closest('#btn-toggle, [data-act]');
    if (!t) return;
    try {
      if (t.id === 'btn-toggle') {
        const want = state.driver.availability === 'available' ? 'unavailable' : 'available';
        if (want === 'available') {
          const p = await getPosition();
          if (p.accuracy > 200) throw new Error(`Signal GPS trop faible (±${p.accuracy} m). Placez-vous à l'extérieur et réessayez.`);
          await api('/api/driver/location', { method: 'POST', body: p });
        }
        const r = await api('/api/driver/status', { method: 'PUT', body: { availability: want } });
        state.driver = r.driver;
        if (want === 'available') startLocSharing(); else stopLocSharing();
        toast(want === 'available' ? 'Vous êtes disponible' : 'Vous êtes indisponible');
        return load(true);
      }
      const { act, id, cid, name } = t.dataset;
      if (act === 'accept') { await api(`/api/requests/${id}/accept`, { method: 'POST' }); toast('Course acceptée'); return load(true); }
      if (act === 'complete') { await api(`/api/requests/${id}/complete`, { method: 'POST' }); toast('Course terminée. Bravo !'); return load(true); }
      if (act === 'release') {
        if (!confirm('Abandonner cette course ? La demande sera rendue aux autres chauffeurs.')) return;
        await api(`/api/requests/${id}/release`, { method: 'POST' }); toast('Course abandonnée'); return load(true);
      }
      if (act === 'report') { const target = Number(cid) || (last.job && last.job.client_id); if (target) openReport(target, name, Number(id)); else toast('Signalement indisponible.', 'bad'); }
    } catch (err) { toast(err.message, 'bad'); load(true); }
  });

  await load(true);
  every(6000, () => load(false));
  state.onPush = () => load(false);   // une notification vient d'arriver : on actualise tout de suite
}

/* ======================= ADMIN ======================= */
function filterTabs(items, current, attr) {
  return `<div class="tabs">${items.map(([v, l]) => `<button data-${attr}="${v}" aria-pressed="${v === current}">${esc(l)}</button>`).join('')}</div>`;
}

async function saveBackupFile(prefix) {
  const res = await fetch('/api/admin/backup', { headers: { Authorization: 'Bearer ' + state.token } });
  if (!res.ok) throw new Error('Sauvegarde impossible pour le moment.');
  const blob = await res.blob();
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${prefix}-${stamp}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

async function downloadBackup(btn) {
  btn.disabled = true;
  try {
    await saveBackupFile('sunrise-sauvegarde');
    toast('Sauvegarde téléchargée. Gardez ce fichier en lieu sûr.');
  } catch (e) { toast(e.message, 'bad'); }
  finally { btn.disabled = false; }
}

const RESTORE_LABELS = { users: 'Comptes', drivers: 'Profils chauffeurs', ride_requests: 'Courses', reports: 'Signalements', announcements: 'Annonces', messages: 'Messages' };

async function pickRestoreFile(input) {
  const file = input.files && input.files[0];
  input.value = '';
  if (!file) return;
  if (file.size > 25 * 1024 * 1024) return toast('Fichier trop volumineux.', 'bad');
  let backup;
  try { backup = JSON.parse(await file.text()); }
  catch { return toast("Ce fichier n'est pas une sauvegarde lisible (il doit se terminer par .json).", 'bad'); }
  let info;
  try { info = await api('/api/admin/restore', { method: 'POST', body: { backup } }); }
  catch (e) { return toast(e.message, 'bad'); }
  confirmRestore(backup, info);
}

function confirmRestore(backup, info) {
  const rows = Object.keys(RESTORE_LABELS).map((t) =>
    `<tr><td>${RESTORE_LABELS[t]}</td><td>${info.backup[t] ?? 0}</td><td>${info.current[t] ?? 0}</td></tr>`).join('');
  const when = info.created_at ? esc(String(info.created_at).replace('T', ' ').slice(0, 16)) + ' (UTC)' : 'date inconnue';
  const phones = (info.admin_phones || []).map((p) => esc(fmtPhone(p))).join(', ');
  const m = modal(`<h2>Restaurer cette sauvegarde ?</h2>
    <p class="muted">Sauvegarde du <b>${when}</b>.</p>
    <table class="rtable"><thead><tr><th></th><th>Dans le fichier</th><th>Actuellement</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="warnbox"><b>Tout le contenu actuel sera remplacé</b> par celui du fichier. Ce qui a été fait après la date de la sauvegarde sera perdu, et tout le monde sera déconnecté.
      Pour vous reconnecter, utilisez le compte administrateur de la sauvegarde (téléphone : <b>${phones}</b>) avec le mot de passe qu'il avait à cette date.</div>
    <button class="btn btn-ghost btn-block" id="rs-safe">1. Télécharger d'abord une copie de sécurité de l'état actuel</button>
    <label class="muted" for="rs-word">2. Tapez RESTAURER en majuscules pour confirmer</label>
    <input id="rs-word" autocomplete="off" autocapitalize="characters" placeholder="RESTAURER" disabled>
    <div class="row"><button class="btn btn-ghost grow" id="rs-cancel">Annuler</button><button class="btn btn-danger grow" id="rs-go" disabled>Restaurer</button></div>`);
  let saved = false;
  const refresh = () => { $('#rs-go', m.el).disabled = !(saved && $('#rs-word', m.el).value === 'RESTAURER'); };
  $('#rs-cancel', m.el).onclick = () => m.close();
  $('#rs-word', m.el).oninput = refresh;
  $('#rs-safe', m.el).onclick = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await saveBackupFile('sunrise-avant-restauration');
      saved = true;
      btn.textContent = '✓ Copie de sécurité téléchargée (vérifiez qu’elle est bien dans vos fichiers)';
      $('#rs-word', m.el).disabled = false;
      refresh();
    } catch (err) { toast(err.message, 'bad'); btn.disabled = false; }
  };
  $('#rs-go', m.el).onclick = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = 'Restauration…';
    try {
      await api('/api/admin/restore', { method: 'POST', body: { backup, dry_run: false, confirmation: $('#rs-word', m.el).value } });
    } catch (err) {
      if (err.status === 401) return;   // session déjà perdue : l'application affiche la connexion
      toast(err.message, 'bad'); btn.disabled = false; btn.textContent = 'Restaurer';
      return;
    }
    m.close();
    clearInterval(state.unreadTimer);
    localStorage.removeItem('ss_token');
    stopLocSharing();
    state.token = null; state.user = null; state.driver = null; state.pos = null;
    render();
    toast('Restauration terminée. Reconnectez-vous.');
  };
}

async function adminStats() {
  $('#main').innerHTML = `<div id="st" class="stack"></div><div id="online"></div><div id="reqs"></div>
    <section class="card stack"><h3>💾 Sauvegarde</h3>
      <p class="muted">Téléchargez une copie de toutes les données (comptes, courses, messages…). Ce fichier contient des informations personnelles : gardez-le privé.</p>
      <button class="btn btn-ghost btn-block" id="bk">Télécharger une sauvegarde</button>
      <p class="muted">Pour revenir à une ancienne sauvegarde, choisissez le fichier .json téléchargé ici. Un aperçu s'affiche d'abord, rien n'est modifié avant votre confirmation.</p>
      <button class="btn btn-ghost btn-block" id="rs">Restaurer depuis un fichier</button>
      <input type="file" id="rsfile" accept=".json,application/json" hidden>
      <div id="sc"></div></section>`;
  (async () => {
    try {
      const { copies } = await api('/api/admin/safety-copies');
      const list = copies.filter((c) => c.kind === 'restore');
      $('#sc').innerHTML = list.length ? `<h4>Copies automatiques avant restauration</h4><p class="muted tiny">Le serveur garde l'état précédent à chaque restauration. Téléchargez-le puis « Restaurer depuis un fichier » pour annuler.</p>
        ${list.map((c) => `<div class="row"><span class="grow tiny">${esc(c.label)}</span><button class="btn btn-sm btn-ghost" data-sc="${c.id}">Télécharger</button></div>`).join('')}` : '';
    } catch { /* secondaire */ }
  })();
  $('#sc').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-sc]');
    if (!b) return;
    try {
      const res = await fetch(`/api/admin/safety-copies/${b.dataset.sc}/download`, { headers: { Authorization: 'Bearer ' + state.token } });
      if (!res.ok) throw new Error('Téléchargement impossible.');
      const a = document.createElement('a'); a.href = URL.createObjectURL(await res.blob()); a.download = `sunrise-avant-restauration-${b.dataset.sc}.json`;
      document.body.appendChild(a); a.click(); a.remove();
    } catch (err) { toast(err.message, 'bad'); }
  });
  $('#bk').onclick = (e) => downloadBackup(e.currentTarget);
  $('#rs').onclick = () => $('#rsfile').click();
  $('#rsfile').onchange = (e) => pickRestoreFile(e.currentTarget);
  async function loadOnline() {
    try {
      const o = await api('/api/admin/online');
      const row = (u, extra) => `<li><span class="dotlive"></span><b>${esc(u.name)}</b> <span class="muted">${esc(fmtPhone(u.phone))}${extra || ''} · ${ago(u.seen_min)}</span></li>`;
      const dst = { available: ' · 🟢 disponible', busy: ' · 🔴 en course', unavailable: ' · ⚪ indisponible' };
      $('#online').innerHTML = `<section class="card stack"><h3>🟢 En ligne maintenant</h3>
        <p class="muted tiny">Application ouverte à l'écran dans les 3 dernières minutes.</p>
        <h4>Chauffeurs (${o.drivers.length})</h4>${o.drivers.length ? `<ul class="onlist">${o.drivers.map((u) => row(u, ` · ${veh(u.vehicle_type).icon}${dst[u.availability] || ''}`)).join('')}</ul>` : '<p class="muted">Aucun chauffeur en ligne.</p>'}
        <h4>Clients (${o.clients.length})</h4>${o.clients.length ? `<ul class="onlist">${o.clients.map((u) => row(u)).join('')}</ul>` : '<p class="muted">Aucun client en ligne.</p>'}</section>`;
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  let rq = 'cancelled';
  async function loadReqs() {
    try {
      const { requests, counts } = await api('/api/admin/requests?status=' + rq);
      const lab = { cancelled: 'Annulée par le client', expired: 'Expirée (personne n\'a accepté)', completed: 'Terminée', open: 'En attente' };
      $('#reqs').innerHTML = `<section class="card stack"><h3>📋 Demandes de course</h3>
        <div id="rqf">${filterTabs([['cancelled', `Annulées (${counts.cancelled})`], ['expired', `Expirées (${counts.expired})`], ['completed', `Terminées (${counts.completed})`], ['open', `En attente (${counts.open})`]], rq, 'q')}</div>
        ${requests.length ? requests.map((r) => `<article class="card flat"><p><b>${esc(r.client_name)}</b> · ${esc(fmtPhone(r.client_phone))}</p>
          <p class="muted">${esc(r.pickup_text || 'Position GPS')} → ${esc(r.destination_text || '—')}${r.vehicle_type && r.vehicle_type !== 'any' ? ' · ' + veh(r.vehicle_type).icon : ''}</p>
          <p class="muted tiny">${lab[r.status] || r.status} · demandée ${fmtWhen(r.created_at)}${r.closed_at ? ' · fermée ' + fmtWhen(r.closed_at) : ''}${r.driver_name ? ' · chauffeur : ' + esc(r.driver_name) : ''}</p></article>`).join('') : '<p class="muted">Rien dans cette catégorie.</p>'}</section>`;
      $$('#rqf [data-q]').forEach((b) => (b.onclick = () => { rq = b.dataset.q; loadReqs(); }));
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  loadOnline(); loadReqs(); every(20000, loadOnline);
  async function load() {
    try {
      const s = await api('/api/admin/stats');
      const card = (n, l, hot) => `<div class="stat ${hot ? 'hot' : ''}"><b>${n}</b><span>${l}</span></div>`;
      $('#st').innerHTML = `<h2>Aperçu de la plateforme</h2><div class="stats">
        ${card(s.drivers_available_now, 'Chauffeurs disponibles maintenant')}
        ${card(s.drivers_pending, 'Chauffeurs à valider', s.drivers_pending > 0)}
        ${card(s.reports_open, 'Signalements ouverts', s.reports_open > 0)}
        ${card(s.messages_unread, 'Messages non lus', s.messages_unread > 0)}
        ${card(s.password_resets_open, 'Mots de passe oubliés', s.password_resets_open > 0)}
        ${card(s.requests_open, 'Demandes en attente')}
        ${card(s.clients, 'Clients inscrits')}
        ${card(s.drivers, 'Chauffeurs inscrits')}
        ${card(s.requests_today, 'Demandes aujourd\'hui')}
        ${card(s.rides_completed, 'Courses terminées')}
        ${card(s.rides_week, 'Courses sur 7 jours')}
        ${card(s.avg_rating == null ? '—' : String(s.avg_rating).replace('.', ',') + ' ⭐', 'Note moyenne')}</div>`;
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  await load(); every(15000, load);
}

/* ---------- Validateur : accès réduit ---------- */
async function validatorUsers() {
  let role = '', q = '';
  $('#main').innerHTML = `<h2>Inscrits</h2>
    <div class="row"><input id="uq" type="search" placeholder="Rechercher un nom ou un numéro…" style="flex:1" aria-label="Rechercher"></div>
    <div id="ft"></div><div id="ul" class="list cols"></div>`;
  async function load() {
    $('#ft').innerHTML = filterTabs([['', 'Tous'], ['client', 'Clients'], ['driver', 'Chauffeurs']], role, 'r');
    $$('#ft [data-r]').forEach((b) => (b.onclick = () => { role = b.dataset.r; load(); }));
    try {
      const { users } = await api(`/api/admin/users?role=${role}&q=${encodeURIComponent(q)}`);
      $('#ul').innerHTML = users.length ? users.map((u) => `<article class="card item">
        <h3>${esc(u.name)} <span class="chip">${u.role === 'driver' ? 'Chauffeur' : 'Client'}</span></h3>
        <dl class="kv"><dt>Téléphone</dt><dd>${esc(fmtPhone(u.phone))}</dd><dt>Inscrit</dt><dd>${fmtWhen(u.created_at)}</dd></dl></article>`).join('')
        : empty('👥', 'Aucun résultat', 'Aucun inscrit ne correspond.');
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  let timer; $('#uq').oninput = (e) => { q = e.target.value.trim(); clearTimeout(timer); timer = setTimeout(load, 300); };
  await load();
}

function validatorAccount() {
  $('#main').innerHTML = `<section class="card stack"><h2>Mon compte</h2>
    <dl class="kv"><dt>Nom</dt><dd>${esc(state.user.name)}</dd><dt>Téléphone</dt><dd>${esc(fmtPhone(state.user.phone))}</dd><dt>Accès</dt><dd>Validation des chauffeurs et liste des inscrits</dd></dl>
    <button class="btn btn-ghost btn-block" id="btn-pw">Changer mon mot de passe</button>
    <button class="btn btn-ghost btn-block" id="btn-logout">Se déconnecter</button></section>`;
  $('#btn-pw').onclick = openChangePassword;
  $('#btn-logout').onclick = logout;
}

/* Côté administrateur : ouvrir, réinitialiser ou retirer un accès validateur */
function validatorForm(m0, title, withName, onSubmit) {
  const m = modal(`<h2>${esc(title)}</h2>
    ${withName ? `<label for="vn">Nom</label><input id="vn" autocomplete="off" placeholder="Ex. Valérie Koffi">
    <label for="vph">Téléphone (10 chiffres)</label><input id="vph" inputmode="tel" autocomplete="off" placeholder="07 07 07 07 07">` : ''}
    <label for="vp">Mot de passe provisoire (6 caractères minimum)</label><input id="vp" autocomplete="off" placeholder="Ex. Soleil26">
    <p class="muted tiny">La personne devra choisir son propre mot de passe à sa première connexion.</p>
    <div class="row"><button class="btn btn-ghost grow" id="v-cancel">Annuler</button><button class="btn grow" id="v-ok">Valider</button></div>`);
  $('#v-cancel', m.el).onclick = () => m.close();
  $('#v-ok', m.el).onclick = async (e) => {
    e.currentTarget.disabled = true;
    try { await onSubmit({ name: withName ? $('#vn', m.el).value : undefined, phone: withName ? $('#vph', m.el).value : undefined, password: $('#vp', m.el).value }); m.close(); }
    catch (err) { toast(err.message, 'bad'); e.currentTarget.disabled = false; }
  };
}

async function loadValidators() {
  const box = $('#vals');
  if (!box) return;
  try {
    const { validators } = await api('/api/admin/validators');
    box.innerHTML = `<section class="card stack"><h3>🧑‍💼 Validateurs</h3>
      <p class="muted">Un validateur peut seulement <b>valider ou refuser les chauffeurs</b> et <b>voir la liste des clients et chauffeurs inscrits</b>. Il ne voit ni les messages, ni les signalements, ni les sauvegardes, et ne peut rien supprimer.</p>
      ${validators.map((v) => `<article class="card flat"><p><b>${esc(v.name)}</b> · ${esc(fmtPhone(v.phone))}</p><p class="muted tiny">ajouté ${fmtWhen(v.created_at)}</p>
        <div class="actions"><button class="btn btn-sm btn-ghost" data-vpw="${v.id}">Nouveau mot de passe</button><button class="btn btn-sm btn-danger" data-vdel="${v.id}" data-name="${esc(v.name)}">Retirer l'accès</button></div></article>`).join('')}
      <button class="btn btn-block" id="v-add">＋ Ajouter un validateur</button></section>`;
    $('#v-add').onclick = () => validatorForm(null, 'Nouveau validateur', true, async (d) => { await api('/api/admin/validators', { method: 'POST', body: d }); toast('Validateur ajouté ✅'); loadValidators(); });
    $$('[data-vpw]', box).forEach((b) => (b.onclick = () => validatorForm(null, 'Nouveau mot de passe', false, async (d) => { await api(`/api/admin/validators/${b.dataset.vpw}/password`, { method: 'POST', body: { password: d.password } }); toast('Mot de passe changé. Il sera déconnecté.'); })));
    $$('[data-vdel]', box).forEach((b) => (b.onclick = async () => {
      if (!confirm(`Retirer l'accès de ${b.dataset.name} ? Son compte sera supprimé.`)) return;
      try { await api(`/api/admin/validators/${b.dataset.vdel}`, { method: 'DELETE' }); toast('Accès retiré'); loadValidators(); } catch (err) { toast(err.message, 'bad'); }
    }));
  } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
}

async function adminDrivers() {
  let filter = 'pending';
  $('#main').innerHTML = '<h2>Chauffeurs</h2><div id="ft"></div><div id="dl" class="list cols"></div>';
  async function load() {
    $('#ft').innerHTML = filterTabs([['pending', 'À valider'], ['approved', 'Validés'], ['rejected', 'Refusés'], ['all', 'Tous']], filter, 'f');
    $$('#ft [data-f]').forEach((b) => (b.onclick = () => { filter = b.dataset.f; load(); }));
    try {
      const { drivers } = await api('/api/admin/drivers?approval=' + filter);
      const ap = { pending: ['À valider', 'warn'], approved: ['Validé', 'ok'], rejected: ['Refusé', 'bad'] };
      $('#dl').innerHTML = drivers.length ? drivers.map((d) => `<article class="card item">
        <h3>${esc(d.name)} <span class="chip ${ap[d.approval][1]}">${ap[d.approval][0]}</span>${d.is_blocked ? '<span class="chip bad">Suspendu</span>' : ''}</h3>
        <dl class="kv"><dt>Téléphone</dt><dd>${esc(fmtPhone(d.phone))}</dd><dt>Véhicule</dt><dd>${veh(d.vehicle_type).icon} ${esc(veh(d.vehicle_type).label)}${d.moto_model ? ' · ' + esc(d.moto_model) : ''} · ${esc(d.plate)}</dd>
        <dt>Permis</dt><dd>${esc(d.permis)}</dd><dt>Commune</dt><dd>${esc(d.commune)}</dd></dl>
        <div class="actions">
          ${d.approval !== 'approved' ? `<button class="btn btn-ok btn-sm" data-ap="approved" data-id="${d.id}">✅ Valider</button>` : ''}
          ${d.approval !== 'rejected' ? `<button class="btn btn-danger btn-sm" data-ap="rejected" data-id="${d.id}">Refuser</button>` : ''}
          <a class="btn btn-ghost btn-sm" href="tel:+225${esc(d.phone)}">📞 Appeler</a></div></article>`).join('')
        : empty('✅', 'Rien ici', 'Aucun chauffeur dans cette catégorie.');
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  $('#dl').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-ap]');
    if (!b) return;
    const decision = b.dataset.ap;
    const note = decision === 'rejected' ? await askNote('Motif du refus', 'Ex. Plaque illisible, permis expiré…', 'Refuser') : '';
    if (note === null) return;
    try { await api(`/api/admin/drivers/${b.dataset.id}/approval`, { method: 'POST', body: { decision, note } }); toast(decision === 'approved' ? 'Chauffeur validé' : 'Chauffeur refusé'); load(); }
    catch (err) { toast(err.message, 'bad'); }
  });
  await load();
}

function showTempPassword(r) {
  const msg = `Bonjour ${r.name}, voici votre mot de passe provisoire Sunrise Space : ${r.temp_password}\nConnectez-vous avec votre numéro, puis choisissez votre propre mot de passe.`;
  const m = modal(`<h2>Mot de passe provisoire</h2>
    <p>Pour <b>${esc(r.name)}</b> · ${esc(fmtPhone(r.phone))}</p>
    <div class="tempcode" id="tp">${esc(r.temp_password)}</div>
    <p class="muted">Il ne sera <b>plus affiché</b> après cette fenêtre. La personne devra en choisir un nouveau dès sa connexion.</p>
    <a class="btn btn-wa btn-block" target="_blank" rel="noopener" href="${waLink(r.phone, msg)}">Envoyer par WhatsApp</a>
    <button class="btn btn-ghost btn-block" id="tp-copy">Copier le message</button>
    <button class="btn btn-block" id="tp-ok">Terminé</button>`);
  $('#tp-copy', m.el).onclick = async () => { try { await navigator.clipboard.writeText(msg); toast('Message copié'); } catch { toast('Copie impossible : notez le code.', 'bad'); } };
  $('#tp-ok', m.el).onclick = () => m.close();
}

async function adminUsers() {
  let role = '', q = '';
  $('#main').innerHTML = `<div id="pwr"></div><h2>Utilisateurs</h2>
    <div class="row"><input id="uq" type="search" placeholder="Rechercher un nom ou un numéro…" style="flex:1" aria-label="Rechercher"></div>
    <div id="ft"></div><div id="ul" class="list cols"></div><div id="bin"></div><div id="vals"></div>`;
  loadValidators();
  async function loadBin() {
    try {
      const { copies } = await api('/api/admin/safety-copies');
      const gone = copies.filter((c) => c.kind === 'user_delete');
      $('#bin').innerHTML = gone.length ? `<section class="card stack"><h3>♻️ Corbeille (30 jours)</h3>
        <p class="muted">Comptes supprimés par erreur ? Vous pouvez les remettre, avec leur mot de passe, leurs messages et leurs courses.</p>
        ${gone.map((c) => `<article class="card flat"><p><b>${esc(c.label)}</b></p><p class="muted">supprimé ${fmtWhen(c.created_at)}</p>
          <button class="btn btn-sm" data-undel="${c.id}">Restaurer ce compte</button></article>`).join('')}</section>` : '';
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  $('#bin').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-undel]');
    if (!b) return;
    b.disabled = true;
    try { await api(`/api/admin/safety-copies/${b.dataset.undel}/restore-user`, { method: 'POST', body: {} }); toast('Compte restauré ✅'); load(); }
    catch (err) { toast(err.message, 'bad'); b.disabled = false; }
  });
  async function loadResets() {
    try {
      const { requests } = await api('/api/admin/password-resets');
      $('#pwr').innerHTML = requests.length ? `<section class="card stack"><h3>🔑 Mots de passe oubliés (${requests.length})</h3>
        <p class="muted">Avant de donner un mot de passe provisoire, vérifiez que c'est bien la personne : rappelez-la sur le numéro du compte.</p>
        ${requests.map((r) => `<article class="card flat"><p><b>${esc(r.name)}</b> <span class="chip">${r.role === 'driver' ? 'Chauffeur' : 'Client'}</span>${r.is_blocked ? '<span class="chip bad">Suspendu</span>' : ''}</p>
          <p class="muted">${esc(fmtPhone(r.phone))} · demandé ${fmtWhen(r.created_at)}</p>
          <div class="actions"><button class="btn btn-sm" data-pw="reset" data-id="${r.id}">Donner un mot de passe provisoire</button><button class="btn btn-sm btn-ghost" data-pw="dismiss" data-id="${r.id}">Ignorer</button></div></article>`).join('')}</section>` : '';
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  $('#pwr').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-pw]');
    if (!b) return;
    const reset = b.dataset.pw === 'reset';
    if (reset && !confirm("Avez-vous vérifié l'identité de cette personne ? Son ancien mot de passe ne marchera plus et elle sera déconnectée.")) return;
    b.disabled = true;
    try {
      const r = await api(`/api/admin/password-resets/${b.dataset.id}`, { method: 'POST', body: { action: b.dataset.pw } });
      if (reset) showTempPassword(r);
      else toast('Demande ignorée');
      loadResets();
    } catch (err) { toast(err.message, 'bad'); b.disabled = false; loadResets(); }
  });
  async function load() {
    loadResets(); loadBin();
    $('#ft').innerHTML = filterTabs([['', 'Tous'], ['client', 'Clients'], ['driver', 'Chauffeurs']], role, 'r');
    $$('#ft [data-r]').forEach((b) => (b.onclick = () => { role = b.dataset.r; load(); }));
    try {
      const { users } = await api(`/api/admin/users?role=${role}&q=${encodeURIComponent(q)}`);
      $('#ul').innerHTML = users.length ? users.map((u) => `<article class="card item">
        <h3>${esc(u.name)} <span class="chip">${u.role === 'driver' ? 'Chauffeur' : 'Client'}</span>${u.is_blocked ? '<span class="chip bad">Suspendu</span>' : ''}</h3>
        <p class="muted">${esc(fmtPhone(u.phone))} · inscrit le ${esc(u.created_at.slice(0, 10))}</p>
        <div class="actions"><button class="btn btn-sm btn-ghost" data-write="${u.id}" data-name="${esc(u.name)}">✉️ Écrire</button><button class="btn btn-sm ${u.is_blocked ? 'btn-ok' : 'btn-danger'}" data-block="${u.is_blocked ? 0 : 1}" data-id="${u.id}">${u.is_blocked ? 'Réactiver' : 'Suspendre'}</button><button class="btn btn-sm btn-danger" data-del="${u.id}" data-name="${esc(u.name)}">🗑️ Supprimer</button></div></article>`).join('')
        : empty('🔍', 'Aucun résultat', '');
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  let t; $('#uq').oninput = (e) => { q = e.target.value.trim(); clearTimeout(t); t = setTimeout(load, 300); };
  $('#ul').addEventListener('click', async (e) => {
    const w = e.target.closest('[data-write]');
    if (w) { state.openThread = { id: +w.dataset.write, name: w.dataset.name }; return openTab('admin', 'inbox'); }
    const d = e.target.closest('[data-del]');
    if (d) {
      const typed = prompt(`SUPPRESSION DÉFINITIVE de « ${d.dataset.name} ».\nSon compte, ses messages et ses demandes seront effacés. Impossible d'annuler.\n\nPour confirmer, tapez SUPPRIMER :`);
      if (typed === null) return;
      try { await api(`/api/admin/users/${d.dataset.del}`, { method: 'DELETE', body: { confirm: typed.trim() } }); toast('Compte supprimé définitivement'); load(); }
      catch (err) { toast(err.message, 'bad'); }
      return;
    }
    const b = e.target.closest('[data-block]');
    if (!b) return;
    if (b.dataset.block === '1' && !confirm('Suspendre ce compte ? La personne sera déconnectée.')) return;
    try { await api(`/api/admin/users/${b.dataset.id}/block`, { method: 'POST', body: { blocked: b.dataset.block === '1' } }); toast('Compte mis à jour'); load(); }
    catch (err) { toast(err.message, 'bad'); }
  });
  await load();
}

async function adminReports() {
  let status = 'open';
  $('#main').innerHTML = '<h2>Signalements</h2><div id="ft"></div><div id="rl" class="list cols"></div>';
  async function load() {
    $('#ft').innerHTML = filterTabs([['open', 'Ouverts'], ['resolved', 'Traités'], ['dismissed', 'Rejetés'], ['all', 'Tous']], status, 's');
    $$('#ft [data-s]').forEach((b) => (b.onclick = () => { status = b.dataset.s; load(); }));
    try {
      const { reports } = await api('/api/admin/reports?status=' + status);
      const st = { open: ['Ouvert', 'warn'], resolved: ['Traité', 'ok'], dismissed: ['Rejeté', ''] };
      $('#rl').innerHTML = reports.length ? reports.map((r) => `<article class="card item">
        <h3>${esc(r.reason_label)} <span class="chip ${st[r.status][1]}">${st[r.status][0]}</span></h3>
        <dl class="kv"><dt>Signalé</dt><dd>${esc(r.target_name)} (${r.target_role === 'driver' ? 'chauffeur' : 'client'}) · ${esc(fmtPhone(r.target_phone))}${r.target_blocked ? ' — suspendu' : ''}</dd>
        <dt>Par</dt><dd>${esc(r.reporter_name)} (${r.reporter_role === 'driver' ? 'chauffeur' : 'client'}) · ${esc(fmtPhone(r.reporter_phone))}</dd>
        <dt>Date</dt><dd>${esc(r.created_at)} UTC</dd>${r.details ? `<dt>Détails</dt><dd>${esc(r.details)}</dd>` : ''}${r.admin_note ? `<dt>Note admin</dt><dd>${esc(r.admin_note)}</dd>` : ''}</dl>
        ${r.status === 'open' ? `<div class="actions">
          <button class="btn btn-danger btn-sm" data-rs="resolved" data-block="1" data-id="${r.id}">Traiter + suspendre</button>
          <button class="btn btn-ok btn-sm" data-rs="resolved" data-id="${r.id}">Traité</button>
          <button class="btn btn-ghost btn-sm" data-rs="dismissed" data-id="${r.id}">Rejeter</button></div>` : ''}</article>`).join('')
        : empty('✅', 'Aucun signalement', 'Tout est calme.');
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  $('#rl').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-rs]');
    if (!b) return;
    const note = await askNote('Note interne', 'Décision / explication (facultatif)');
    if (note === null) return;
    try {
      await api(`/api/admin/reports/${b.dataset.id}`, { method: 'POST', body: { status: b.dataset.rs, note, block_target: b.dataset.block === '1' } });
      toast('Signalement mis à jour'); load();
    } catch (err) { toast(err.message, 'bad'); }
  });
  await load();
}

/* ---------- Démarrage ---------- */

/* ======================= MESSAGERIE ======================= */
const fmtWhen = (s) => {
  if (!s) return '';
  const d = new Date(String(s).replace(' ', 'T') + 'Z');
  return d.toLocaleString('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
};
const nl = (t) => esc(t).replace(/\n/g, '<br>');
const bubbles = (msgs, mine) => msgs.length
  ? msgs.map((m) => `<div class="bubble ${m.sender === mine ? 'me' : 'them'}${m.unread ? ' new' : ''}">${nl(m.body)}<time>${fmtWhen(m.created_at)}</time></div>`).join('')
  : '<p class="muted center">Aucun message pour le moment.</p>';
const stickBottom = (el) => { if (el) el.scrollTop = el.scrollHeight; };

async function userInbox() {
  $('#main').innerHTML = `<h2>Messages</h2>
    <section class="card stack"><h3>📣 Annonces de Sunrise Space</h3><div id="ann" class="list"></div></section>
    <section class="card stack"><h3>💬 Écrire à l'équipe Sunrise Space</h3>
      <p class="muted">Une question, une idée, un problème ? Écrivez-nous ici, nous vous répondons dans cette boîte.</p>
      <div id="thread" class="thread" aria-live="polite"></div>
      <form id="mf" class="msgform"><textarea id="mt" maxlength="1000" rows="2" placeholder="Votre message…" aria-label="Votre message"></textarea>
        <button class="btn btn-primary" id="ms">Envoyer</button></form></section>`;
  let sig = '';
  async function load() {
    try {
      const r = await api('/api/inbox');
      const key = JSON.stringify([r.announcements.map((a) => [a.id, a.unread]), r.messages.map((m) => [m.id, m.unread])]);
      if (key !== sig) {
        sig = key;
        $('#ann').innerHTML = r.announcements.length ? r.announcements.map((a) => `<article class="ann${a.unread ? ' new' : ''}">
          <h4>${a.unread ? '<span class="chip warn">Nouveau</span> ' : ''}${esc(a.title)}</h4><p>${nl(a.body)}</p><time>${fmtWhen(a.created_at)}</time></article>`).join('')
          : '<p class="muted">Aucune annonce pour le moment.</p>';
        const th = $('#thread'); th.innerHTML = bubbles(r.messages, 'user'); stickBottom(th);
      }
      if (r.announcements.some((a) => a.unread) || r.messages.some((m) => m.unread)) {
        await api('/api/inbox/read', { method: 'POST' });
        refreshUnread();
      }
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  $('#mf').onsubmit = async (ev) => {
    ev.preventDefault();
    const t = $('#mt').value.trim();
    if (!t) return;
    $('#ms').disabled = true;
    try { await api('/api/inbox/messages', { method: 'POST', body: { body: t } }); $('#mt').value = ''; sig = ''; await load(); }
    catch (e) { toast(e.message, 'bad'); }
    finally { $('#ms').disabled = false; }
  };
  await load(); every(15000, load);
}

async function adminInbox() {
  let view = 'conv', thread = state.openThread || null;
  state.openThread = null;
  $('#main').innerHTML = '<h2>Messages</h2><div id="ft"></div><div id="box" class="stack"></div>';
  const tabs = () => { $('#ft').innerHTML = thread ? '' : filterTabs([['conv', 'Boîte de réception'], ['ann', 'Annonces'], ['wel', 'Bienvenue']], view, 'v'); $$('#ft [data-v]').forEach((b) => (b.onclick = () => { view = b.dataset.v; draw(); })); };

  async function drawConversations() {
    try {
      const { conversations } = await api('/api/admin/conversations');
      $('#box').innerHTML = conversations.length ? `<div class="list cols">${conversations.map((c) => `<button class="card item conv${c.unread ? ' new' : ''}" data-uid="${c.id}" data-name="${esc(c.name)}">
        <h3>${esc(c.name)} <span class="chip">${c.role === 'driver' ? 'Chauffeur' : 'Client'}</span>${c.unread ? `<span class="chip warn">${c.unread} non lu${c.unread > 1 ? 's' : ''}</span>` : ''}</h3>
        <p class="muted">${c.last_sender === 'admin' ? 'Vous : ' : ''}${esc(c.last_body.slice(0, 90))}</p><time>${fmtWhen(c.last_at)}</time></button>`).join('')}</div>`
        : empty('✉️', 'Boîte vide', "Les messages des utilisateurs arriveront ici. Pour écrire à quelqu'un, ouvrez l'onglet Utilisateurs.");
      $$('#box [data-uid]').forEach((b) => (b.onclick = () => { thread = { id: +b.dataset.uid, name: b.dataset.name }; draw(); }));
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }

  async function drawThread() {
    $('#box').innerHTML = `<button class="btn btn-ghost btn-sm" id="back">← Retour</button>
      <section class="card stack"><h3>💬 ${esc(thread.name)}</h3><div id="thread" class="thread tall"></div>
      <form id="mf" class="msgform"><textarea id="mt" maxlength="1000" rows="2" placeholder="Votre réponse…" aria-label="Votre réponse"></textarea><button class="btn btn-primary" id="ms">Envoyer</button></form></section>`;
    $('#back').onclick = () => { thread = null; draw(); };
    let sig = '';
    async function load() {
      try {
        const r = await api(`/api/admin/conversations/${thread.id}`);
        const key = r.messages.map((m) => m.id).join(',');
        if (key !== sig) { sig = key; const th = $('#thread'); th.innerHTML = bubbles(r.messages, 'admin'); stickBottom(th); }
        refreshUnread();
      } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
    }
    $('#mf').onsubmit = async (ev) => {
      ev.preventDefault();
      const t = $('#mt').value.trim();
      if (!t) return;
      $('#ms').disabled = true;
      try { await api(`/api/admin/conversations/${thread.id}`, { method: 'POST', body: { body: t } }); $('#mt').value = ''; sig = ''; await load(); }
      catch (e) { toast(e.message, 'bad'); }
      finally { $('#ms').disabled = false; }
    };
    await load(); every(10000, load);
  }

  async function drawWelcome() {
    let w = { client: '', driver: '' };
    try { w = await api('/api/admin/welcome'); } catch (e) { toast(e.message, 'bad'); }
    $('#box').innerHTML = `<section class="card stack"><h3>👋 Message de bienvenue automatique</h3>
      <p class="muted">Envoyé dans la messagerie de chaque <b>nouvel</b> inscrit, dès la création de son compte. Laissez vide pour ne rien envoyer. Écrivez <b>{prenom}</b> pour insérer son prénom.</p>
      <form id="wf" class="stack">
        <label>Aux nouveaux clients<textarea id="wc" maxlength="800" rows="5" placeholder="Ex. Bienvenue {prenom} sur Sunrise Space ! Envoyez votre demande de course : un chauffeur proche vous répondra.">${esc(w.client)}</textarea></label>
        <label>Aux nouveaux chauffeurs<textarea id="wd" maxlength="800" rows="5" placeholder="Ex. Bienvenue {prenom} ! Votre profil sera vérifié par l'équipe avant de recevoir des demandes.">${esc(w.driver)}</textarea></label>
        <button class="btn btn-primary btn-block" id="wsave">Enregistrer</button></form></section>`;
    $('#wf').onsubmit = async (e) => {
      e.preventDefault();
      try { await api('/api/admin/welcome', { method: 'PUT', body: { client: $('#wc').value, driver: $('#wd').value } }); toast('Message de bienvenue enregistré ✅'); }
      catch (err) { toast(err.message, 'bad'); }
    };
  }

  async function drawAnnouncements() {
    $('#box').innerHTML = `<section class="card stack"><h3>📣 Nouvelle annonce</h3>
      <p class="muted">Elle apparaîtra dans la boîte de réception des personnes choisies, avec une pastille « non lu ».</p>
      <form id="af" class="stack"><label>Destinataires<select id="aud"><option value="all">Tout le monde</option><option value="client">Clients</option><option value="driver">Chauffeurs</option></select></label>
      <label>Titre<input id="at" maxlength="80" placeholder="Ex. La phase test commence"></label>
      <label>Message<textarea id="ab" maxlength="1500" rows="4" placeholder="Votre information…"></textarea></label>
      <button class="btn btn-primary btn-block" id="as">Envoyer l'annonce</button></form></section>
      <h3>Annonces envoyées</h3><div id="al" class="list"></div>`;
    async function list() {
      try {
        const { announcements } = await api('/api/admin/announcements');
        $('#al').innerHTML = announcements.length ? announcements.map((a) => `<article class="card item"><h3>${esc(a.title)} <span class="chip">${esc(a.audience_label)}</span></h3>
          <p>${nl(a.body)}</p><p class="muted">${fmtWhen(a.created_at)} · lue par ${a.reads} / ${a.audience_size}</p>
          <div class="actions"><button class="btn btn-danger btn-sm" data-del="${a.id}">Supprimer</button></div></article>`).join('')
          : '<p class="muted">Aucune annonce envoyée.</p>';
      } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
    }
    $('#af').onsubmit = async (ev) => {
      ev.preventDefault();
      $('#as').disabled = true;
      try {
        await api('/api/admin/announcements', { method: 'POST', body: { audience: $('#aud').value, title: $('#at').value, body: $('#ab').value } });
        $('#at').value = ''; $('#ab').value = ''; toast('Annonce envoyée'); list();
      } catch (e) { toast(e.message, 'bad'); }
      finally { $('#as').disabled = false; }
    };
    $('#al').addEventListener('click', async (e) => {
      const b = e.target.closest('[data-del]');
      if (!b || !confirm('Supprimer cette annonce ? Elle disparaîtra des boîtes de réception.')) return;
      try { await api(`/api/admin/announcements/${b.dataset.del}`, { method: 'DELETE' }); toast('Annonce supprimée'); list(); }
      catch (err) { toast(err.message, 'bad'); }
    });
    await list();
  }

  function draw() {
    clearTimers(); tabs();
    if (thread) return drawThread();
    if (view === 'wel') return drawWelcome();
    if (view === 'ann') return drawAnnouncements();
    drawConversations(); every(15000, drawConversations);
  }
  draw();
}

async function boot() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
    navigator.serviceWorker.addEventListener('message', (e) => { if (e.data && e.data.type === 'push') { if (state.onPush) state.onPush(); checkIncomingCall(); } });
  }
  try { state.meta = await api('/api/meta'); }
  catch { state.meta = { communes: [], report_reasons: {}, default_center: { lat: 5.36, lng: -4.0083 } }; }
  if (state.token) {
    try { const r = await api('/api/me'); state.user = r.user; state.driver = r.driver; }
    catch (e) { if (e.status === 401 || e.status === 403) { localStorage.removeItem('ss_token'); state.token = null; } }
  }
  render();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
