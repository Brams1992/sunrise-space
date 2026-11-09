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
const mapsLink = (lat, lng) => `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  let res;
  try {
    res = await fetch(path, { method: opts.method || 'GET', headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  } catch {
    throw new Error('Pas de connexion internet. Réessayez.');
  }
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

/* ---------- Rendu principal ---------- */
function render() {
  clearTimers();
  state.tab = null;
  if (!state.user) return renderAuth();
  if (state.user.role === 'client') return renderShell('client', [['nearby', '🗺️', 'Autour de moi'], ['request', '🛵', 'Ma demande'], ['account', '👤', 'Compte']]);
  if (state.user.role === 'driver') return renderShell('driver', [['home', '🏍️', 'Accueil'], ['account', '👤', 'Compte']]);
  if (state.user.role === 'admin') return renderShell('admin', [['stats', '📊', 'Aperçu'], ['drivers', '🏍️', 'Chauffeurs'], ['users', '👥', 'Utilisateurs'], ['reports', '⚑', 'Signalements']]);
}

function renderShell(role, tabs) {
  const first = tabs[0][0];
  $('#app').innerHTML = `
    <div class="shell ${role === 'admin' ? 'wide' : ''}">
      <header class="topbar">
        <div class="brand"><img src="/icon-192.png" alt="" width="30" height="30"><span>Sunrise Space</span></div>
        <div class="who">${esc(state.user.name)}<br><span style="opacity:.7">${{ client: 'Client', driver: 'Chauffeur', admin: 'Administrateur' }[role]}</span></div>
      </header>
      <main id="main"></main>
    </div>
    <nav class="bottomnav" aria-label="Navigation"><div class="inner">
      ${tabs.map(([id, ic, label]) => `<button data-tab="${id}"><span class="ic" aria-hidden="true">${ic}</span>${esc(label)}</button>`).join('')}
    </div></nav>`;
  $$('.bottomnav [data-tab]').forEach((b) => (b.onclick = () => openTab(role, b.dataset.tab)));
  openTab(role, first);
}

function openTab(role, tab) {
  clearTimers();
  state.tab = tab;
  state.onPos = null;
  $$('.bottomnav [data-tab]').forEach((b) => { if (b.dataset.tab === tab) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
  window.scrollTo(0, 0);
  const views = {
    client: { nearby: clientNearby, request: clientRequest, account: accountView },
    driver: { home: driverHome, account: accountView },
    admin: { stats: adminStats, drivers: adminDrivers, users: adminUsers, reports: adminReports },
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
        <p>Trouvez un moto-taxi près de vous, en un instant.</p>
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
          <div id="driver-box" class="stack" hidden>
            <label>Plaque d'immatriculation<input name="plate" maxlength="20" placeholder="Ex. 1234 AB 01"></label>
            <label>Numéro de permis de conduire<input name="permis" maxlength="30"></label>
            <label>Modèle de la moto <span class="hint">(facultatif)</span><input name="moto_model" maxlength="40" placeholder="Ex. Jakarta 125"></label>
            <label>Commune / ville<select name="commune"><option value="">Choisir…</option>${communes}</select></label>
            <label>Numéro WhatsApp <span class="hint">(si différent du téléphone)</span><input name="whatsapp" type="tel" inputmode="numeric" maxlength="18"></label>
            <p class="banner warn">Votre profil sera vérifié par l'équipe Sunrise Space (plaque, permis) avant d'apparaître aux clients.</p>
          </div>
          <p class="banner bad" id="auth-error" role="alert" hidden></p>
          <button class="btn btn-block" id="auth-submit" type="submit">Se connecter</button>
        </form>
      </section>
      <section class="feat">
        <div><span class="ic">📍</span><p><b>Motos disponibles autour de vous</b><br><span class="muted">Voyez les chauffeurs les plus proches en direct.</span></p></div>
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
    form.password.autocomplete = reg ? 'new-password' : 'current-password';
    err.hidden = true;
  };
  $$('[data-mode]').forEach((b) => (b.onclick = () => { mode = b.dataset.mode; sync(); }));
  $$('[data-role]').forEach((b) => (b.onclick = () => { role = b.dataset.role; sync(); }));

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
        if (role === 'driver') body.driver = { plate: form.plate.value, permis: form.permis.value, moto_model: form.moto_model.value, commune: form.commune.value, whatsapp: form.whatsapp.value };
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

async function logout() {
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
        ${d ? `<dt>Moto</dt><dd>${esc(d.moto_model || '—')} · ${esc(d.plate)}</dd><dt>Commune</dt><dd>${esc(d.commune)}</dd>
        <dt>Validation</dt><dd>${{ pending: 'En attente', approved: 'Validé ✅', rejected: 'Refusé' }[d.approval]}</dd>` : ''}
      </dl>
      ${installButtonHTML()}
      <button class="btn btn-dark btn-block" id="btn-logout">Se déconnecter</button>
    </section>
    <section class="card flat"><h3>À propos</h3>
      <p class="muted">Sunrise Space met en relation clients et chauffeurs de moto-taxi partout en Côte d'Ivoire. Le prix de la course se discute directement avec le chauffeur et se règle en espèces.</p>
    </section>`;
  $('#btn-logout').onclick = logout;
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
async function clientNearby() {
  $('#main').innerHTML = `
    <section class="card">${posControlsHTML()}</section>
    <div id="map" class="map" aria-label="Carte des motos disponibles"></div>
    <div class="section-head"><h2>Motos disponibles</h2><button class="link" id="btn-refresh">Actualiser</button></div>
    <div id="drivers" class="list"></div>`;
  bindPosControls();
  setupMap('map');
  let first = true;

  async function load() {
    const box = $('#drivers');
    if (!box) return;
    if (!state.pos) { box.innerHTML = empty('📍', 'Indiquez votre position', 'Touchez « Me localiser » ou choisissez votre commune pour voir les motos autour de vous.'); return; }
    try {
      const r = await api(`/api/drivers/nearby?lat=${state.pos.lat}&lng=${state.pos.lng}`);
      paint(r.drivers);
    } catch (e) { if (e.status !== 401) box.innerHTML = empty('⚠️', 'Chargement impossible', e.message); }
  }

  function paint(list) {
    const box = $('#drivers');
    if (state.layers) {
      state.layers.clearLayers();
      list.forEach((d) => {
        L.marker([d.lat, d.lng], { icon: L.divIcon({ className: 'pin', html: '🏍️', iconSize: [34, 34], iconAnchor: [17, 17] }) })
          .bindPopup(`<b>${esc(d.name)}</b><br>${esc(d.plate)} · ${fmtDist(d.distance_km)}`).addTo(state.layers);
      });
      if (first && list.length && state.pos) {
        state.map.fitBounds([[state.pos.lat, state.pos.lng], ...list.map((d) => [d.lat, d.lng])], { padding: [30, 30], maxZoom: 16 });
      }
    }
    first = false;
    if (!list.length) {
      box.innerHTML = empty('🌅', 'Aucune moto disponible pour le moment', "Aucun chauffeur disponible dans 15 km. Faites une demande : les chauffeurs proches seront prévenus.") +
        '<button class="btn btn-block" id="go-request">🛵 Faire une demande de moto</button>';
      $('#go-request').onclick = () => openTab('client', 'request');
      return;
    }
    box.innerHTML = list.map((d) => `
      <article class="card driver">
        <div class="avatar" aria-hidden="true">🏍️</div>
        <div class="grow">
          <h3>${esc(d.name)}</h3>
          <p class="muted">${esc(d.moto_model || 'Moto')} · ${esc(d.plate)}</p>
          <p class="meta"><span class="chip ok">● Disponible</span><b>${fmtDist(d.distance_km)}</b><span class="muted">· ${esc(d.commune)}</span></p>
          <p class="tiny muted">Position ${esc(ago(d.updated_min_ago))}</p>
        </div>
        <div class="actions">
          <a class="btn btn-sm" href="tel:+225${esc(d.phone)}">📞 Appeler</a>
          <a class="btn btn-sm btn-wa" target="_blank" rel="noopener" href="${waLink(d.whatsapp)}">WhatsApp</a>
          <button class="btn btn-sm btn-ghost" data-report="${d.id}" data-name="${esc(d.name)}">⚑ Signaler</button>
        </div>
      </article>`).join('');
  }

  $('#drivers').addEventListener('click', (e) => {
    const b = e.target.closest('[data-report]');
    if (b) openReport(Number(b.dataset.report), b.dataset.name);
  });
  $('#btn-refresh').onclick = load;
  state.onPos = () => { first = true; load(); };
  every(10000, load);
  if (!state.pos) {
    try { setPos({ ...(await getPosition()), source: 'gps' }); if (state.map) state.map.setView([state.pos.lat, state.pos.lng], 14); }
    catch (e) { toast(e.message, 'bad'); load(); }
  } else load();
}

function empty(icon, title, text) {
  return `<div class="empty"><div class="big">${icon}</div><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
}

async function clientRequest() {
  $('#main').innerHTML = '<div id="reqbox"></div><div id="hist" class="stack"></div>';
  let lastKey = null;

  function freshGps() { return state.pos && state.pos.source === 'gps' && Date.now() - (state.pos.at || 0) < 60000; }

  function formHTML() {
    return `<section class="card stack">
      <h2>Demander une moto</h2>
      <p class="muted">Votre position est détectée automatiquement. Indiquez seulement où vous allez.</p>
      <div class="posline" id="posline">${freshGps() ? posLineHTML() : '<span class="chip">📡 Détection de votre position…</span>'}</div>
      <button type="button" class="link" id="btn-redetect" style="justify-self:start">↻ Actualiser ma position</button>
      <form id="reqform" class="form" style="margin-top:0">
        <label>Où allez-vous ?<input name="dest" required maxlength="120" autocomplete="off" placeholder="Ex. Marché de Cocody"></label>
        <p class="banner bad" id="req-err" role="alert" hidden></p>
        <button class="btn btn-block" type="submit">🛵 Demander une moto</button>
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

  function driverBlock(d) {
    return `<div class="card flat">
      <div class="row"><div class="avatar" aria-hidden="true">🏍️</div><div class="grow"><h3>${esc(d.name)}</h3><p class="muted">${esc(d.moto_model || 'Moto')} · ${esc(d.plate)}</p></div></div>
      <div class="row" style="margin-top:10px">
        <a class="btn btn-sm grow" href="tel:+225${esc(d.phone)}">📞 Appeler</a>
        <a class="btn btn-sm btn-wa grow" target="_blank" rel="noopener" href="${waLink(d.whatsapp, 'Bonjour, j\'ai fait une demande de moto sur Sunrise Space.')}">WhatsApp</a>
      </div></div>`;
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
      $('#reqform').onsubmit = submit;
      $('#btn-redetect').onclick = detectPosition;
      if (!freshGps()) detectPosition();
      return;
    }
    const where = `<dl class="kv"><dt>Départ</dt><dd>${esc(r.pickup_text || 'Ma position (GPS)')}</dd><dt>Destination</dt><dd>${esc(r.destination_text || '—')}</dd>${r.note ? `<dt>Précision</dt><dd>${esc(r.note)}</dd>` : ''}</dl>`;
    if (r.status === 'open') {
      box.innerHTML = `<section class="card stack"><h2><span class="pulse"></span>Recherche d'un chauffeur…</h2>
        <p class="muted">Demande envoyée <span id="age">${esc(ago(r.age_min))}</span>. Elle expire au bout de 30 minutes.</p>${where}
        <button class="btn btn-ghost btn-block" id="btn-cancel">Annuler la demande</button></section>`;
    } else if (r.status === 'accepted' && r.driver) {
      box.innerHTML = `<section class="card stack job"><h2>✅ Un chauffeur arrive !</h2>
        <div id="cmap" class="map" aria-label="Carte : votre position et le chauffeur"></div>
        <p id="ceta" class="muted">Recherche du chauffeur sur la carte…</p>${driverBlock(r.driver)}${where}
        <button class="btn btn-ok btn-block" id="btn-done">J'ai terminé ma course</button>
        <div class="row"><button class="btn btn-ghost btn-sm grow" id="btn-cancel">Annuler</button>
        <button class="btn btn-ghost btn-sm grow" data-report="${r.driver.id}" data-name="${esc(r.driver.name)}">⚑ Signaler</button></div></section>`;
    } else {
      const label = { completed: ['🎉', 'Course terminée', 'Merci d\'avoir utilisé Sunrise Space.'], cancelled: ['✖️', 'Demande annulée', ''], expired: ['⌛', 'Demande expirée', "Aucun chauffeur n'a pu la prendre. Réessayez."] }[r.status] || ['', '', ''];
      box.innerHTML = `<section class="card stack"><div class="empty"><div class="big">${label[0]}</div><h3>${label[1]}</h3><p>${esc(label[2])}</p></div>
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
      await api('/api/requests', { method: 'POST', body: { lat: p.lat, lng: p.lng, accuracy: p.accuracy, pickup_text: '', destination_text: dest, note: '' } });
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

  $('#reqbox').addEventListener('click', (e) => { const b = e.target.closest('[data-report]'); if (b) openReport(Number(b.dataset.report), b.dataset.name, null); });
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
const jobMap = { job: null, cm: null, dm: null, line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false };
const reqMap = { dm: null };
const cliMap = { req: null, cm: null, dm: null, line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false };

function destroyMap() {
  if (state.map) { try { state.map.remove(); } catch { /* rien */ } }
  state.map = null; state.layers = null; state.meMarker = null; state.accCircle = null;
  Object.assign(jobMap, { job: null, cm: null, dm: null, line: null, lastFrom: null, lastAt: 0, busy: false, fitted: false });
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

function initJobMap(job) {
  const el = $('#jobmap');
  if (!el) return;
  jobMap.job = job;
  if (!window.L) {
    el.innerHTML = '<div class="map-fallback">Carte indisponible. Utilisez les boutons Plans ou Google Maps.</div>';
    const me = myPos();
    setEta(me ? `À vol d'oiseau : <b>${fmtDist(distKm(me, job))}</b>` : '');
    return;
  }
  const map = newMap('jobmap').setView([job.lat, job.lng], 15);
  jobMap.cm = L.marker([job.lat, job.lng], { icon: pinIcon('🙋'), zIndexOffset: 900 }).bindPopup(`Client : ${esc(job.client_name)}`).addTo(map);
  refreshJobRoute(true);
}

async function refreshJobRoute(force) {
  const job = jobMap.job, map = state.map;
  if (!job || !map || !window.L) return;
  const me = myPos();
  if (!me) { setEta('En attente de votre position GPS…'); return; }
  if (jobMap.dm) jobMap.dm.setLatLng([me.lat, me.lng]);
  else jobMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon('🏍️'), zIndexOffset: 1000 }).addTo(map);
  const moved = jobMap.lastFrom ? distKm(jobMap.lastFrom, me) * 1000 : Infinity;
  if (!force && moved < 80 && Date.now() - jobMap.lastAt < 45000) return;
  if (jobMap.busy) return;
  jobMap.busy = true; jobMap.lastFrom = me; jobMap.lastAt = Date.now();
  const route = await fetchRoute(me, job);
  jobMap.busy = false;
  if (state.map !== map) return;  // la carte a été remplacée entre-temps
  if (jobMap.line) map.removeLayer(jobMap.line);
  const pts = route ? route.coords : [[me.lat, me.lng], [job.lat, job.lng]];
  jobMap.line = L.polyline(pts, { color: '#FF7A1A', weight: 5, opacity: 0.9, dashArray: route ? null : '8 8' }).addTo(map);
  if (!jobMap.fitted) { jobMap.fitted = true; map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 17 }); }
  setEta(route
    ? `🛵 Environ <b>${route.min} min</b> · ${route.km.toFixed(1).replace('.', ',')} km par la route`
    : `À vol d'oiseau : <b>${fmtDist(distKm(me, job))}</b> <span class="tiny">(itinéraire routier indisponible, utilisez Plans ou Google Maps)</span>`);
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
  if (me) { reqMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon('🏍️'), zIndexOffset: 1000 }).addTo(map); pts.push([me.lat, me.lng]); }
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
  else cliMap.dm = L.marker([me.lat, me.lng], { icon: pinIcon('🏍️'), zIndexOffset: 1000 }).addTo(map);
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
    ? `🏍️ Arrivée dans environ <b>${route.min} min</b> · à ${route.km.toFixed(1).replace('.', ',')} km`
    : `Le chauffeur est à <b>${fmtDist(distKm(me, dest))}</b> (à vol d'oiseau)`);
}

function updateMaps() {
  const me = myPos();
  if (reqMap.dm && me) reqMap.dm.setLatLng([me.lat, me.lng]);
  if (jobMap.job) refreshJobRoute(false);
}

async function driverHome() {
  $('#main').innerHTML = '<div id="dhome" class="stack"><div class="empty"><p>Chargement…</p></div></div>';
  let sig = null, last = null;

  async function load(force) {
    try { last = await api('/api/driver/dashboard'); } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); return; }
    state.driver = last.driver;
    const a = last.driver.availability;
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
      const la = $('#lnk-apple'), lg = $('#lnk-google');
      if (la) la.href = appleLink(last.job.lat, last.job.lng);
      if (lg) lg.href = mapsLink(last.job.lat, last.job.lng);
    }
    updateMaps();
  }

  function paint({ driver: d, job, requests }) {
    let banner = '';
    if (d.approval === 'pending') banner = '<div class="banner warn"><b>Profil en cours de vérification.</b> L\'équipe Sunrise Space contrôle votre plaque et votre permis. Vous pourrez passer « Disponible » dès la validation.</div>';
    if (d.approval === 'rejected') banner = `<div class="banner bad"><b>Profil refusé.</b> ${esc(d.approval_note || 'Contactez le support Sunrise Space.')}</div>`;
    const canToggle = d.approval === 'approved' && d.availability !== 'busy';
    const label = { available: 'Disponible', busy: 'Course en cours', unavailable: 'Indisponible' }[d.availability];
    const hint = { available: 'Les clients proches peuvent vous voir et vous appeler.', busy: 'Terminez votre course pour redevenir disponible.', unavailable: 'Vous n\'apparaissez pas sur la carte des clients.' }[d.availability];
    let html = `${banner}<section class="card statuscard ${d.availability}">
      <div class="grow"><p class="tiny muted">Mon statut</p><h2>${label}</h2><p class="muted tiny">${hint}</p><p style="margin-top:6px" id="locbadge">${d.availability === 'unavailable' ? '' : locBadge()}</p></div>
      <button class="switch ${d.availability === 'available' ? 'on' : ''}" id="btn-toggle" role="switch" aria-checked="${d.availability === 'available'}" aria-label="Je suis disponible" ${canToggle ? '' : 'disabled'}><span></span></button></section>`;

    if (job) {
      html += `<section class="card job stack"><h2>🛵 Course en cours</h2>
        <dl class="kv"><dt>Client</dt><dd>${esc(job.client_name)}</dd><dt>Départ</dt><dd>${esc(job.pickup_text || 'Position GPS du client (voir la carte)')}</dd><dt>Destination</dt><dd>${esc(job.destination_text || '—')}</dd>
        ${job.note ? `<dt>Précision</dt><dd>${esc(job.note)}</dd>` : ''}${job.distance_km != null ? `<dt>Distance</dt><dd>${fmtDist(job.distance_km)}</dd>` : ''}</dl>
        <div class="row"><a class="btn btn-sm grow" href="tel:+225${esc(job.client_phone)}">📞 Appeler</a>
          <a class="btn btn-sm btn-wa grow" target="_blank" rel="noopener" href="${waLink(job.client_phone, 'Bonjour, je suis votre chauffeur Sunrise Space, j\'arrive.')}">WhatsApp</a></div>
        <div id="jobmap" class="map" aria-label="Carte : votre position et le client"></div>
        <p id="jobeta" class="muted">Calcul de l'itinéraire…</p>
        <div class="row"><a id="lnk-apple" class="btn btn-dark btn-sm grow" target="_blank" rel="noopener" href="${appleLink(job.lat, job.lng)}">🧭 Plans (Apple)</a>
          <a id="lnk-google" class="btn btn-dark btn-sm grow" target="_blank" rel="noopener" href="${mapsLink(job.lat, job.lng)}">🧭 Google Maps</a></div>
        <button class="btn btn-ok btn-block" data-act="complete" data-id="${job.id}">Course terminée</button>
        <div class="row"><button class="btn btn-ghost btn-sm grow" data-act="release" data-id="${job.id}">Abandonner</button>
        <button class="btn btn-ghost btn-sm grow" data-act="report" data-cid="${job.client_id || ''}" data-name="${esc(job.client_name)}" data-id="${job.id}">⚑ Signaler le client</button></div></section>`;
    } else if (d.approval === 'approved') {
      html += '<div class="section-head"><h2>Demandes près de vous</h2></div>';
      if (d.availability !== 'available') html += empty('😴', 'Vous êtes indisponible', 'Passez « Disponible » pour recevoir les demandes des clients proches.');
      else if (!requests.length) html += empty('🌅', 'Aucune demande pour le moment', 'Restez disponible : les nouvelles demandes apparaissent ici automatiquement.');
      else html += '<div id="reqmap" class="map" aria-label="Carte des demandes"></div><div class="list">' + requests.map((r) => `<article class="card item">
          <h3>${esc(r.client_name)} <span class="chip sun" id="rq-d-${r.id}">${fmtDist(r.distance_km)}</span></h3>
          <dl class="kv"><dt>Départ</dt><dd>${esc(r.pickup_text || 'Position du client')}</dd><dt>Destination</dt><dd>${esc(r.destination_text || '—')}</dd>${r.note ? `<dt>Précision</dt><dd>${esc(r.note)}</dd>` : ''}</dl>
          <p class="tiny muted" id="rq-a-${r.id}">Demandé ${esc(ago(r.age_min))}</p>
          <div class="actions"><button class="btn grow" data-act="accept" data-id="${r.id}">✅ Accepter la course</button></div></article>`).join('') + '</div>';
    }
    destroyMap();
    $('#dhome').innerHTML = html;
    if (job) initJobMap(job);
    else if (d.approval === 'approved' && d.availability === 'available' && requests.length) initReqMap(requests);
  }

  $('#dhome').addEventListener('click', async (e) => {
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
}

/* ======================= ADMIN ======================= */
function filterTabs(items, current, attr) {
  return `<div class="tabs">${items.map(([v, l]) => `<button data-${attr}="${v}" aria-pressed="${v === current}">${esc(l)}</button>`).join('')}</div>`;
}

async function adminStats() {
  $('#main').innerHTML = '<div id="st" class="stack"></div>';
  async function load() {
    try {
      const s = await api('/api/admin/stats');
      const card = (n, l, hot) => `<div class="stat ${hot ? 'hot' : ''}"><b>${n}</b><span>${l}</span></div>`;
      $('#st').innerHTML = `<h2>Aperçu de la plateforme</h2><div class="stats">
        ${card(s.drivers_available_now, 'Chauffeurs disponibles maintenant')}
        ${card(s.drivers_pending, 'Chauffeurs à valider', s.drivers_pending > 0)}
        ${card(s.reports_open, 'Signalements ouverts', s.reports_open > 0)}
        ${card(s.requests_open, 'Demandes en attente')}
        ${card(s.clients, 'Clients inscrits')}
        ${card(s.drivers, 'Chauffeurs inscrits')}
        ${card(s.requests_today, 'Demandes aujourd\'hui')}
        ${card(s.rides_completed, 'Courses terminées')}</div>`;
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  await load(); every(15000, load);
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
        <dl class="kv"><dt>Téléphone</dt><dd>${esc(fmtPhone(d.phone))}</dd><dt>Moto</dt><dd>${esc(d.moto_model || '—')} · ${esc(d.plate)}</dd>
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

async function adminUsers() {
  let role = '', q = '';
  $('#main').innerHTML = `<h2>Utilisateurs</h2>
    <div class="row"><input id="uq" type="search" placeholder="Rechercher un nom ou un numéro…" style="flex:1" aria-label="Rechercher"></div>
    <div id="ft"></div><div id="ul" class="list cols"></div>`;
  async function load() {
    $('#ft').innerHTML = filterTabs([['', 'Tous'], ['client', 'Clients'], ['driver', 'Chauffeurs']], role, 'r');
    $$('#ft [data-r]').forEach((b) => (b.onclick = () => { role = b.dataset.r; load(); }));
    try {
      const { users } = await api(`/api/admin/users?role=${role}&q=${encodeURIComponent(q)}`);
      $('#ul').innerHTML = users.length ? users.map((u) => `<article class="card item">
        <h3>${esc(u.name)} <span class="chip">${u.role === 'driver' ? 'Chauffeur' : 'Client'}</span>${u.is_blocked ? '<span class="chip bad">Suspendu</span>' : ''}</h3>
        <p class="muted">${esc(fmtPhone(u.phone))} · inscrit le ${esc(u.created_at.slice(0, 10))}</p>
        <div class="actions"><button class="btn btn-sm ${u.is_blocked ? 'btn-ok' : 'btn-danger'}" data-block="${u.is_blocked ? 0 : 1}" data-id="${u.id}">${u.is_blocked ? 'Réactiver' : 'Suspendre'}</button></div></article>`).join('')
        : empty('🔍', 'Aucun résultat', '');
    } catch (e) { if (e.status !== 401) toast(e.message, 'bad'); }
  }
  let t; $('#uq').oninput = (e) => { q = e.target.value.trim(); clearTimeout(t); t = setTimeout(load, 300); };
  $('#ul').addEventListener('click', async (e) => {
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
async function boot() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
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
