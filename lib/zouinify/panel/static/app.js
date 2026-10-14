/* zouinify panel. One page, hash routes, no framework, no build step, no request to any host but this one: the
   page fetches /api/snapshot every refresh_s seconds and renders everything from it. The ONE write is
   POST /api/receipts (T-0162): a tier-1 click (OK after a command the human ran, an answer among offered options, a
   question or a report to the desk, launch worker, kill) becomes a receipt the desk fetches and verifies before
   acting; the page shows the receipt id. Tier 2 (an approval, a production apply/deploy/push, a CLAUDE.md or
   settings decision, a permission prompt) has no button: the text to type in the desk's window, with a copy
   button. The POST carries the HttpOnly cookie the server set at unlock; page script never holds the token. */
(() => {
  'use strict';
  const NO_TOKEN = 'no browser token yet: run `zouinify panel token rotate` in your own terminal and open the address it prints in this browser';
  const BANNER_429 = 'receipts rate-limited: something other than you is knocking (refused requests in the last minute)';
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const state = { snap: null, error: null, loading: true, fetchedAt: 0, drawer: null, denied: null, sent: {}, banner: null, newForm: false, freshInFlight: false };
  // a fresh token in the address (#t=...): drop it from the address first (never in history or a screenshot; the
  // server never sees the fragment), then hand it once to /api/unlock as a Bearer header: the server sets the
  // HttpOnly SameSite=Strict cookie every later request carries. Page script never writes the cookie itself.
  (function () {
    const m = location.hash.match(/[#&]t=([A-Za-z0-9_-]{32,256})/);
    if (m) {
      history.replaceState(null, '', location.pathname + location.search + '#/dashboard');
      fetch('/api/unlock', { headers: { Authorization: 'Bearer ' + m[1] }, cache: 'no-store' }).catch(() => {});
    }
  })();

  const ICONS = {
    dashboard: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>',
    tickets: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M3 10h18"/></svg>',
    sessions: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 9l3 2.5L7 14M12 14h5"/></svg>',
    waiting: '<svg viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6z"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
    live: '<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>',
    inbox: '<svg viewBox="0 0 24 24"><path d="M4 4h16v12H4zM4 12h5l2 3h2l2-3h5"/><path d="M4 16v4h16v-4"/></svg>',
    chat: '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.5A8 8 0 1 1 21 12z"/></svg>',
    settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    menu: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
  };
  const NAV = [['dashboard', 'Dashboard'], ['waiting', 'Waiting for me'], ['tickets', 'Tickets'], ['sessions', 'Sessions'], ['live', 'Live checks'], ['inbox', 'Inbox'], ['chat', 'Desk chat'], ['settings', 'Settings']];
  const LANES = [['ready', 'ready'], ['in-dev', 'in-dev'], ['review', 'review'], ['to deploy', 'to-deploy'], ['deployed', 'deployed']];
  const STATE_LABEL = { waiting: 'waiting for you', working: 'working', idle: 'idle', gone: 'gone', shell: 'shell' };

  // ---- helpers ----------------------------------------------------------------------------------------
  function route() {
    const h = location.hash.replace(/^#\/?/, '');
    const [path, qs] = h.split('?');
    const parts = path.split('/').filter(Boolean);
    return { page: parts[0] || 'dashboard', id: parts[1] ? decodeURIComponent(parts[1]) : null, q: new URLSearchParams(qs || '') };
  }
  function parseSince(s) {
    if (!s) return null;
    const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0));
  }
  function fmtDur(ms) {
    if (ms == null || isNaN(ms)) return '';
    const m = Math.max(0, Math.round(ms / 60000));
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 48) return h + 'h' + (m % 60 ? String(m % 60).padStart(2, '0') : '');
    return Math.floor(h / 24) + 'd';
  }
  function age(since) { const d = parseSince(since); return d ? fmtDur(Date.now() - d.getTime()) : ''; }
  function sinceLabel(since) { const d = parseSince(since); if (!d) return esc(since || ''); return `since ${esc(String(since).slice(5, 16))} (${age(since)})`; }
  const badge = (text, cls = '', title = '') => `<span class="badge ${esc(cls)}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
  const sev = (s) => s ? badge(s, 'sev ' + s) : '';
  const off = (label, reason) => `<button type="button" class="btn" disabled aria-disabled="true" title="${esc(reason)}">${esc(label)}</button>`;
  // a receipt button: data-rc carries the body; `from` names where the text comes from (radio | input | form)
  function canWrite() { const r = state.snap && state.snap.panel && state.snap.panel.receipts; return !!(r && r.write_path && r.token_configured); }
  function rc(label, data, cls = '') {
    if (!canWrite()) return off(label, NO_TOKEN);
    return `<button type="button" class="btn ${cls}" data-rc="${esc(JSON.stringify(data))}">${esc(label)}</button>`;
  }
  function recentFor(pred) {
    const r = state.snap && state.snap.panel && state.snap.panel.receipts;
    return ((r && r.recent) || []).find(pred) || null;
  }
  function panelStarted() { const r = state.snap && state.snap.panel && state.snap.panel.receipts; return Date.parse((r && r.started_at) || (state.snap && state.snap.panel && state.snap.panel.started_at) || '') || 0; }
  function sentBadge(key, pred) {
    const mine = state.sent[key];
    const known = pred ? recentFor(pred) : null;
    if (!known && mine && mine.at && Date.parse(mine.at) < panelStarted()) {
      // the panel restarted after this click: receipts live in memory only, so it is gone (audit.jsonl is never read back)
      return `<span class="badge stale" title="receipts pending at a restart are dropped">the panel restarted at ${esc(new Date(panelStarted()).toTimeString().slice(0, 5))}: click again if still needed</span>`;
    }
    const rec = known || (mine ? { id: mine.id, delivered_at: '', acked_at: '', reply: '' } : null);
    if (!rec) return '';
    if (rec.reply) {
      // the desk's verdict (T-0201): green when its first word says it happened, amber when it says it did not
      return `<span class="badge sent ${verdictClass(rec.reply)}" title="the desk's reply after verifying; a reply is never a ticket move">receipt ${esc(rec.id)} · desk ${esc(String(rec.replied_at).slice(11, 16))}: ${esc(rec.reply)}</span>`;
    }
    const st = rec.acked_at ? `acked by the desk ${esc(String(rec.acked_at).slice(11, 16))}, verifying` : rec.delivered_at ? `fetched ${esc(String(rec.delivered_at).slice(11, 16))}` : 'waiting for the desk\u2019s poll';
    return `<span class="badge sent" title="a receipt is a claim the desk verifies before acting">receipt ${esc(rec.id)} · ${st}</span>`;
  }
  const VERDICT_OK = /^(launched|done|ok|moved|filed|killed|answered|resumed|applied|noted|yes|sso ok)\b/i;
  const VERDICT_NO = /^(not|refused|still|no|cannot|can't|failed|wrong|unverified)\b/i;
  function verdictClass(reply) { const t = String(reply || '').trim(); return VERDICT_OK.test(t) ? 'ok' : VERDICT_NO.test(t) ? 'warn' : ''; }
  const copyBtn = (text, label = 'copy') => `<button type="button" class="btn ghost" data-copy="${esc(text)}" aria-label="copy to clipboard">${esc(label)}</button>`;
  function modelBadge(row) {
    if (!row.model || row.model === '?') return badge('?', 'model unknown', 'status bar not on the visible screen');
    if (row.model_mismatch) return badge(row.model + ' ≠ ' + (row.model_expected || 'config'), 'model mismatch', 'not the configured model');
    return badge(row.model, 'model');
  }
  function stateBadge(st) { return badge(STATE_LABEL[st] || st, 'state-' + esc(st)); }
  function stale(name) {
    const s = state.snap && state.snap.sources && state.snap.sources[name];
    if (!s) return '';
    if (s.error) return badge('error', 'stale', s.error);
    if (s.stale) return badge('stale ' + fmtDur(s.age_s * 1000), 'stale', 'older than 3 x its refresh');
    return `<span class="muted nowrap" title="age of this source">${s.age_s == null ? '…' : fmtDur(s.age_s * 1000) || '0m'} ago</span>`;
  }
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 1800); }

  // ---- data -------------------------------------------------------------------------------------------
  // load() reads the cached snapshot (the refresher keeps every source within its ttl); load({ fresh: true }) is the
  // refresh button (T-0197): /api/snapshot?fresh=1 re-reads every source now. One fresh request in flight at a time, and
  // the server serves at most one fresh per 5 s per client (the cached answer otherwise, with its age in `fresh`).
  async function load(opts) {
    const fresh = !!(opts && opts.fresh);
    if (fresh) { if (state.freshInFlight) return; state.freshInFlight = true; }
    try {
      const r = await (fresh ? fetch('/api/snapshot?fresh=1', { cache: 'no-store' }) : fetch('/api/snapshot', { cache: 'no-store' }));
      if (r.status === 401) { state.denied = 'token missing or wrong: open the address printed by `zouinify panel token rotate` in your own terminal'; throw new Error('HTTP 401'); }
      if (r.status === 429) { state.denied = 'rate-limited: too many refused requests from this address (something other than you may be knocking); wait a minute'; throw new Error('HTTP 429'); }
      state.denied = null;
      if (!r.ok) throw new Error('HTTP ' + r.status);
      state.snap = await r.json();
      state.error = null;
      if (fresh) {
        const f = state.snap.fresh || {};
        const age = boardAge();
        toast(f.served === false ? `fresh data was fetched ${f.wait_s} s ago: showing it (board ${age})` : `refreshed: board ${age}`);
      }
    } catch (e) {
      state.error = String(e && e.message ? e.message : e);
      if (state.denied) toast(state.denied);
    } finally {
      if (fresh) state.freshInFlight = false;
    }
    state.loading = false;
    state.fetchedAt = Date.now();
    render();
    if (state.snap && state.snap.loading && state.snap.loading.length) { clearTimeout(load.warm); load.warm = setTimeout(() => load(), 2000); }
  }
  function boardAge() {
    const b = state.snap && state.snap.sources && state.snap.sources.board;
    if (!b || b.age_s == null) return '…';
    return b.age_s < 60 ? Math.round(b.age_s) + ' s' : fmtDur(b.age_s * 1000);
  }

  // ---- shell --------------------------------------------------------------------------------------------
  function renderNav() {
    const { page } = route();
    const s = state.snap;
    const counts = { inbox: s ? s.kpis.inbox : '', sessions: s ? s.kpis.waiting || '' : '', tickets: s ? s.kpis.tickets : '',
      waiting: s ? ((s.kpis.waiting || 0) + (s.kpis.waiting_elsewhere || 0)) || '' : '' };
    $('#nav').innerHTML = NAV.map(([key, label]) =>
      `<a href="#/${key}" class="${page === key ? 'active' : ''}" aria-current="${page === key ? 'page' : 'false'}">${ICONS[key]}<span class="lbl">${label}</span>${counts[key] !== '' && counts[key] != null ? `<span class="count">${esc(counts[key])}</span>` : ''}</a>`).join('');
    $('#sidebar-foot').innerHTML = s ? `read-only v1 · ${esc(s.panel.bind)} · refresh ${esc(s.panel.refresh_s)} s<br>zouinify ${esc(s.version)}` : '';
  }
  function renderHeader() {
    const s = state.snap;
    const el = $('#header');
    if (!s) { el.innerHTML = `<button type="button" class="btn ghost menu" data-menu aria-label="menu">${ICONS.menu}</button><span class="proj">zouinify</span>${state.denied ? `<span class="error">${esc(state.denied)} <button type="button" class="btn ghost" data-reload>reload</button></span>` : state.error ? `<span class="error">${esc(state.error)} <button type="button" class="btn ghost" data-refresh>retry</button></span>` : '<span class="skeleton"></span>'}`; return; }
    const f = s.header.freeze, c = s.header.creds, d = s.header.desk;
    el.innerHTML = [
      `<button type="button" class="btn ghost menu" data-menu aria-label="menu">${ICONS.menu}</button>`,
      `<span class="proj">${esc(s.project.short)}</span><span class="secondary">${esc(s.project.name)}</span>`,
      `<span class="sep">·</span><span class="mono">trunk ${esc(s.project.trunk)}${s.project.trunk_sha ? ' @' + esc(s.project.trunk_sha) : ''}</span>`,
      `<span class="sep">·</span>${f.on ? badge('FREEZE on', 'freeze-on', 'allowed: ' + (f.ids || []).join(' ')) : badge('FREEZE off', 'freeze-off')}`,
      `<span class="sep">·</span><span title="${esc(c.source)}">creds ${esc(c.left)}</span>`,
      ssoControl(s),
      `<span class="sep">·</span><span class="presence ${esc(d.state)} ${d.polling ? 'polling' : 'nopoll'}" title="desk session state · receipts poll presence">desk ${esc(STATE_LABEL[d.state] || d.state)} · ${esc(d.presence || '')}</span>`,
      `<span class="sep">·</span><span class="muted" title="generated ${esc(s.generated)}">data ${fmtDur(Date.now() - state.fetchedAt) || '0m'} ago</span>`,
      `<span class="sep">·</span><span class="muted mono" title="age of the board source on the server (the refresh button re-reads it)">board ${esc(boardAge())}</span>`,
      s.loading && s.loading.length ? `<span class="sep">·</span>${badge('loading ' + s.loading.join(', '), 'stale', 'sources still warming up')}` : '',
      `<span class="sep">·</span>${badge(s.panel.bind === 'loopback' ? 'loopback' : s.panel.bind, 'lock', s.panel.bind === 'loopback' ? 'the panel listens on 127.0.0.1 only' : 'LAN mode: a browser token is required on every request; HTTP is clear text on the lab network')}`,
      state.denied ? `<span class="error">${esc(state.denied)}</span>` : '',
      `<button type="button" class="btn ghost" data-refresh title="re-read every source now (at most once per 5 s)" aria-label="refresh now"${state.freshInFlight ? ' disabled aria-disabled="true"' : ''}>↻</button>`,
    ].join('');
  }

  // the SSO updated button (T-0195): shown when the login is not ok or any ticket waits on it (its newest log line is
  // `waits-on: aws-sso -- resume: <step>`), HIDDEN when creds are ok and nobody waits. The click is a receipt of kind
  // sso: the panel re-probes the credentials at once and the badge says what it saw; the desk verifies and resumes.
  function ssoControl(s) {
    const x = s.sso;
    const sent = state.sent['sso'];
    const recentSso = recentFor((r) => r.kind === 'sso' && Date.now() - Date.parse(r.at) < 600000);
    // hidden when the login is ok and nobody waits; the desk's verdict on a click of the last ten minutes stays visible meanwhile
    if (!x || (!x.show_button && !recentSso)) return '';
    if (!x.show_button) return `<span class="sep">·</span><span class="sso">${sentBadge('sso', (r) => r.kind === 'sso')}</span>`;
    const list = (x.waiting || []).length ? `<span class="sso-waiting" title="open tickets whose newest log line is ${esc(x.marker)}">${esc(x.waiting.length)} waiting: ${x.waiting.map((w) => `<a href="#/tickets/${encodeURIComponent(w.id)}">${esc(w.id)}</a> ${esc(w.owner || '(no owner)')}`).join(', ')}</span>` : '<span class="sso-waiting muted">nobody waits on it</span>';
    const mine = sent ? recentFor((r) => r.kind === 'sso' && r.id === sent.id) : null;
    const stillExpired = sent && sent.panel_saw === 'LOGIN' && !(mine && mine.reply);
    return `<span class="sep">·</span><span class="sso">${rc('SSO updated', { kind: 'sso', key: 'sso' }, 'primary')} ${list}${stillExpired ? '<span class="sso-still">still expired: run aws sso login in your terminal, then click again</span>' : ''}${sentBadge('sso', (r) => r.kind === 'sso')}</span>`;
  }

  // ---- components -----------------------------------------------------------------------------------
  function p0Banner(s) {
    if (!s.p0.length) return '';
    return `<div class="p0banner" role="alert">${s.p0.map((t) => `<span><span class="id">!! P0 ${esc(t.id)}</span> ${esc(t.title)} <span class="muted">· ${esc(t.status)}${t.owner ? ' · ' + esc(t.owner) : ''}</span></span>`).join('')}<span class="muted">a P0 is the one thing that interrupts the human</span></div>`;
  }
  function kpis(s) {
    const k = s.kpis;
    return `<div class="kpis">
      <a class="panel card kpi" href="#/inbox"><div class="label">For you</div><div class="big">${k.inbox == null ? '?' : esc(k.inbox)}</div><div class="sub">${k.p0 ? `<span class="p0">P0 ${esc(k.p0)}</span> · ` : ''}${k.inbox ? 'items waiting for you' : 'nothing waiting for you'}</div></a>
      <a class="panel card kpi" href="#/sessions"><div class="label">Sessions</div><div class="big ${k.waiting ? 'amber' : ''}">${esc(k.waiting)}</div><div class="sub">waiting for you${k.waiting_elsewhere ? ` · ${esc(k.waiting_elsewhere)} elsewhere` : ''} · ${esc(k.working)} working · ${esc(k.idle)} idle · ${esc(k.gone)} gone</div></a>
      <a class="panel card kpi" href="#/tickets?state=ready-to-deploy"><div class="label">Deploy &amp; verify</div><div class="big">${esc(k.to_deploy)}</div><div class="sub">to deploy · ${esc(k.deployed)} deployed awaiting a live check</div></a>
    </div>`;
  }
  function tcard(t, lane) {
    const extras = [];
    if (lane === 'in-dev' || lane === 'review') { if (t.owner) extras.push(esc(t.owner)); if (t.claimed) extras.push('claimed ' + age(t.claimed) + ' ago'); if (t.status === 'ready-to-merge') extras.push(badge('ready-to-merge', '')); }
    if (lane === 'to deploy') { if (t.plan) extras.push(`<span class="mono">${esc(String(t.plan).split('/').pop())}</span>`); if (t.plan_void) extras.push(badge('VOID', 'void', 'a newer plan exists')); if (t.status === 'merged') extras.push('merged, no plan yet'); }
    if (lane === 'deployed') { if (t.closed_by) { const shas = String(t.closed_by).replace(/[\[\]]/g, ''); extras.push(`<span class="mono closed-by truncate" title="${esc(shas)}">${esc(shas)}</span>`); } extras.push('awaiting live check'); if (t.plan_void) extras.push(badge('VOID', 'void')); }
    if (lane === 'ready') { if (t.deploy && t.deploy !== 'none') extras.push('deploy: ' + esc(t.deploy)); }
    const freeze = state.snap.header.freeze;
    const launchable = lane === 'ready' && !(freeze.on && !(freeze.ids || []).includes(t.id));
    return `<div class="card tcard clickable" tabindex="0" role="button" data-open-ticket="${esc(t.id)}" aria-label="open ${esc(t.id)}">
      <div class="top"><span class="id">${esc(t.id)}</span>${sev(t.severity)}${t.human ? '<span class="bell" title="a pending ask for the human">🔔</span>' : ''}</div>
      <div class="title">${esc(t.title)}</div>
      ${extras.length ? `<div class="extra">${extras.join('<span class="sep">·</span>')}</div>` : ''}
      ${launchable ? `<div class="actions">${rc('launch worker', { kind: 'launch', ticket: t.id, key: 'launch:' + t.id })}${sentBadge('launch:' + t.id, (r) => r.kind === 'launch' && r.ticket === t.id)}</div>` : ''}
    </div>`;
  }
  function board(s, compact = true) {
    const b = s.board;
    const chips = Object.entries(b.chips).map(([k, n]) => `<a class="chip ${k === 'blocked' && n ? 'blocked' : ''}" href="#/tickets?state=${esc(k)}">${esc(k)} <b>${esc(n)}</b></a>`).join('');
    const other = Object.entries(b.other || {}).map(([k, n]) => `<a class="chip" href="#/tickets?state=${esc(k)}" title="a state outside the five lanes">${esc(k)} <b>${esc(n)}</b></a>`).join('');
    const lanes = LANES.map(([name, cls]) => { const cards = b.lanes[name] || []; return `<section class="lane ${cls}" aria-label="${esc(name)}"><header>${esc(name)}<span class="n">${cards.length}</span></header><div class="cards">${cards.length ? cards.map((t) => tcard(t, name)).join('') : '<div class="empty">none</div>'}</div></section>`; }).join('');
    const form = state.newForm ? `<form class="sendform panel" data-newform aria-label="send to desk">
        <select class="input" name="type" aria-label="type"><option>bug</option><option>feature</option><option>chore</option><option>process</option></select>
        <select class="input" name="severity" aria-label="severity"><option>P2</option><option>P0</option><option>P1</option><option>P3</option></select>
        <input class="input" name="title" maxlength="120" placeholder="title (one line)" aria-label="title" required>
        <textarea class="input" name="body" maxlength="1500" placeholder="what you saw, in two lines; the desk investigates and files it" aria-label="details"></textarea>
        <div class="row">${rc('send to desk', { kind: 'new', from: 'form', key: 'new' })} <button type="button" class="btn ghost" data-newform-close>cancel</button> <span class="note">a report to the desk: it checks the code, files the ticket and marks it ready; nothing moves on the click itself</span></div>
      </form>` : '';
    return `<div class="boardwrap"><div class="boardhead"><h2>Tickets</h2>${chips}${other}<span class="muted">${stale('board')}</span><span class="spacer"></span>${canWrite() ? `<button type="button" class="btn" data-newform-open>Send to desk</button>` : off('Send to desk', NO_TOKEN)}${sentBadge('new', (r) => r.kind === 'new')}</div>
      ${form}
      <div class="board" role="list">${lanes}</div>
      ${b.newest_plan ? `<div class="note">newest plan: <span class="mono">${esc(b.newest_plan)}</span>${b.void_plans.length ? ' · VOID: ' + esc(b.void_plans.join(', ')) : ''}</div>` : ''}
      ${b.warnings && b.warnings.length ? `<div class="error">${b.warnings.map(esc).join(' · ')}</div>` : ''}</div>`;
  }
  function inboxItem(it, i) {
    const isQuestion = /^question:|^reply /i.test(it.command || '');
    const typeIn = it.tier === 2;
    let act;
    const key = 'item:' + it.id;
    const badgeSent = sentBadge(key, (r) => r.item_id === it.id);
    if (typeIn) { const toType = /^(APPLY|DEPLOY|PUSH)$/.test(it.verb) ? `${it.tickets} ok` : (it.expect || it.command); act = `<span class="type">${esc(it.hint)}</span> ${copyBtn(toType, 'copy what to type')} ${badge('tier 2: never clickable', 'tier2')}`; }
    else if (it.void) act = `<span class="type">void: a newer plan exists, nothing to acknowledge</span>`;
    else if (it.verb === 'DECIDE') act = (it.options.length ? `<span class="options">${it.options.map((o) => `<label><input type="radio" name="d-${esc(it.id)}" value="${esc(o)}"> ${esc(o)}</label>`).join('')}</span>${rc('Send answer', { item_id: it.id, from: 'radio', key })}` : `<input class="input" data-rc-input="${esc(key)}" maxlength="500" placeholder="your answer (one line)" aria-label="your answer"> ${rc('Send answer', { item_id: it.id, from: 'input', key })}`) + badgeSent;
    else act = `<span class="type">${esc(it.hint)}</span>${rc('OK', { item_id: it.id, key })}${badgeSent}`;
    return `<div class="inbox-item ${it.void ? 'void' : ''}" id="inbox-${esc(it.id)}">
      <div class="head"><span class="num">${i + 1}</span>${badge(it.verb, 'verb ' + it.verb)}<span class="id">${esc(it.tickets)}</span>${sev(it.priority)}<span class="title">${esc(it.title)}</span><span class="age">${sinceLabel(it.since)}</span></div>
      <div class="cmd ${isQuestion ? 'question' : ''}"><pre>${esc(it.command)}</pre>${isQuestion ? '' : copyBtn(it.command)}</div>
      <div class="meta"><span><b>expect:</b> ${esc(it.expect)}</span><span><b>if you do nothing:</b> ${esc(it.if_nothing)}</span>${it.note ? `<span><b>note:</b> ${esc(it.note)}</span>` : ''}${it.void ? badge('do NOT run: a newer plan exists', 'void') : ''}</div>
      <div class="act">${act}</div>
    </div>`;
  }
  function inboxPanel(s, full = false) {
    const items = s.inbox.items;
    const loading = (s.loading || []).includes('inbox');
    const body = s.inbox.error ? `<div class="error">${esc(s.inbox.error)}</div>` : loading ? '<div class="body"><div class="skeleton"></div></div>' : items.length ? items.map(inboxItem).join('') : '<div class="empty">nothing is waiting for you</div>';
    return `<section class="panel inboxpanel"><header><h2>Inbox</h2><span class="muted">scripts/ticket.sh human --json · ${items.length} item${items.length === 1 ? '' : 's'} · in the command's order</span>${stale('inbox')}${full ? '' : '<a href="#/inbox" class="muted">all</a>'}</header>${body}</section>`;
  }
  function typeLink(r) {
    // T-0181: the terminal page (a separate process) types into WORKER panes only; the panel only links to it
    const term = state.snap && state.snap.terminal;
    if (!(r.in_project !== false && r.role === 'worker' && term && term.running && term.url)) return '';
    return ` <a class="btn ghost" href="${esc(term.url)}?target=${encodeURIComponent(r.name)}" target="_blank" rel="noopener">type</a>`;
  }
  function waitRow(r) {
    const mine = r.in_project;
    const link = mine ? `#/sessions/${encodeURIComponent(r.name)}` : `#/waiting/${encodeURIComponent(r.name)}`;
    return `<div class="wait-row"><div class="name"><a href="${link}">${esc(r.name)}</a>${badge(r.project, mine ? 'mine' : 'other', mine ? 'this project' : r.registered ? 'another zouinify project' : 'directory name of that session')}${r.tmux ? `<span class="mono muted">${esc(r.tmux)}</span>` : '<span class="muted">no tmux window found</span>'}</div>
      <div class="what">${esc(r.waiting_for || 'waiting')}</div>
      <div class="since">${sinceLabel(r.since)} · ${r.tmux ? `answer from your terminal: attach to tmux window <span class="mono">${esc(r.tmux)}</span> ${copyBtn(r.tmux, 'copy window name')}` : 'answer in that session\'s own window'}${typeLink(r)}</div></div>`;
  }
  function pageWaiting(s) {
    const w = s.waiting_all || { rows: [], elsewhere: 0 };
    if ((s.loading || []).includes('sessions') || (s.loading || []).includes('foreign_waiting')) return `<h1>Waiting for me</h1><section class="panel"><div class="body"><div class="skeleton"></div></div></section>`;
    const mine = w.rows.filter((r) => r.in_project), other = w.rows.filter((r) => !r.in_project);
    const groups = [];
    other.forEach((r) => { let g = groups.find((x) => x.label === r.project); if (!g) { g = { label: r.project, rows: [] }; groups.push(g); } g.rows.push(r); });
    return `<h1>Waiting for me <span class="muted">${w.rows.length} pane${w.rows.length === 1 ? '' : 's'} · ${mine.length} of this project · ${other.length} elsewhere</span></h1>
      ${w.error ? `<div class="error">${esc(w.error)}</div>` : ''}
      <section class="panel waitpanel"><header><h2>${esc(s.project.name)}</h2>${stale('sessions')}</header>${mine.length ? mine.map(waitRow).join('') : '<div class="empty">no session of this project is waiting for you</div>'}</section>
      ${groups.map((g) => `<section class="panel waitpanel"><header><h2>${esc(g.label)}</h2><span class="muted">${g.rows[0].registered ? 'another zouinify project' : 'not a zouinify project'}</span>${stale('foreign_waiting')}</header>${g.rows.map(waitRow).join('')}</section>`).join('')}
      ${other.length ? '' : '<div class="note">no session elsewhere is waiting for you</div>'}
      <div class="note">This project's rows are the dashboard's (a prompt or a dialog on the screen counts); elsewhere, every session <span class="mono">claude agents</span> reports as waiting. Answers are typed in that tmux window from your terminal (attach to the window named on the row). Rows of other projects show the project name only, never a path. Typing from this page is a separate decision (Z-11).</div>`;
  }
  function waitingPanel(s) {
    const rows = s.waiting;
    const elsewhere = s.kpis.waiting_elsewhere || 0;
    return `<section class="panel waitpanel"><header><h2>Panes waiting for you</h2>${stale('sessions')}<a href="#/waiting" class="muted">${elsewhere ? `+ ${esc(elsewhere)} elsewhere` : 'all'}</a></header>${rows.length ? rows.map((r) => `<div class="wait-row"><div class="name"><a href="#/sessions/${encodeURIComponent(r.name)}">${esc(r.name)}</a>${badge(r.role, '')}${r.ticket ? `<span class="mono muted">${esc(r.ticket)}</span>` : ''}</div><div class="what">${esc(r.waiting_for || 'waiting')}</div><div class="since">${r.tmux ? 'window ' + esc(r.tmux) + ' · ' : ''}answer in that window (permission prompts are never clickable)${typeLink(r)}</div></div>`).join('') : '<div class="empty">no session is waiting for you</div>'}</section>`;
  }
  function sessionsTable(s, full = false) {
    const rows = s.sessions.rows;
    if ((s.loading || []).includes('sessions')) return `<section class="panel sesspanel"><header><h2>Sessions</h2>${stale('sessions')}</header><div class="body"><div class="skeleton"></div></div></section>`;
    const tr = (r) => `<tr class="row-${esc(r.state)} ${r.role === 'desk' ? 'row-desk' : ''}">
      <td>${esc(r.role)}</td><td><a href="#/sessions/${encodeURIComponent(r.name)}">${esc(r.name)}</a></td><td class="mono">${esc(r.tmux || '—')}</td><td>${modelBadge(r)}</td><td>${stateBadge(r.state)}</td>
      <td class="mono">${r.ticket ? `<a href="#/tickets/${esc(r.ticket)}">${esc(r.ticket)}</a>` : ''}</td><td class="last truncate" title="${esc(r.last_reply || r.last_line)}">${esc(r.waiting_for || r.last_reply || r.last_line || '')}${r.last_event ? `<span class="muted"> · ${esc(r.last_event)}</span>` : ''}</td>
      ${full ? `<td class="nowrap actions">${sessionActions(s, r)}</td>` : ''}</tr>`;
    return `<section class="panel sesspanel sessions-table ${full ? 'has-actions' : ''}"><header><h2>Sessions</h2><span class="muted">${rows.length} of this project · desk first</span>${stale('sessions')}${full ? '' : '<a href="#/sessions" class="muted">all</a>'}</header>
      ${s.sessions.error ? `<div class="error">${esc(s.sessions.error)}</div>` : ''}
      <div class="body scroll-x"><table class="tbl"><thead><tr><th>role</th><th>name</th><th>tmux</th><th>model</th><th>state</th><th>ticket</th><th>last line</th>${full ? '<th class="actions">actions</th>' : ''}</tr></thead><tbody>${rows.map(tr).join('')}</tbody></table></div></section>`;
  }
  function ticketStatus(s, id) { const t = (s.tickets || []).find((x) => x.id === id); return t ? t.status : ''; }
  function sessionActions(s, r) {
    // launch: a gone worker whose ticket is ready again (the desk runs the launcher); kill: a worker whose ticket is done/dropped
    if (r.state === 'gone' && r.ticket && ticketStatus(s, r.ticket) === 'ready') return rc('launch (via the desk)', { kind: 'launch', ticket: r.ticket, key: 'launch:' + r.ticket }) + sentBadge('launch:' + r.ticket, (x) => x.kind === 'launch' && x.ticket === r.ticket);
    if (r.role === 'worker' && r.ticket && /^(done|dropped)$/.test(ticketStatus(s, r.ticket)) && r.state !== 'gone') return rc('kill (via the desk)', { kind: 'kill', text: r.name, key: 'kill:' + r.name }, 'danger') + sentBadge('kill:' + r.name, (x) => x.kind === 'kill' && x.text === r.name);
    return '';
  }
  function chatPanel(s) {
    const d = s.header.desk;
    const rec = ((s.panel.receipts && s.panel.receipts.recent) || []).filter((r) => r.kind === 'question').slice(0, 8).reverse();
    const msgs = rec.map((r) => `<div class="msg you"><div class="who">you · receipt ${esc(r.id)} · ${esc(String(r.at).slice(11, 16))}${r.reply ? '' : r.acked_at ? ' · acked by the desk, answering' : r.delivered_at ? ' · fetched by the desk' : ' · waiting for the desk\u2019s poll'}</div>${esc(r.text)}</div>`
      + (r.reply ? `<div class="msg desk ${verdictClass(r.reply)}"><div class="who">desk (${esc(r.id)}) · ${esc(String(r.replied_at).slice(11, 16))}</div>${esc(r.reply)}</div>` : '')).join('');
    return `<section class="panel chat"><header><h2>Desk chat</h2><span class="presence ${esc(d.state)} ${d.polling ? 'polling' : 'nopoll'}">${esc(d.name || 'desk')} · ${esc(STATE_LABEL[d.state] || d.state)} · ${esc(d.presence || '')}</span></header><div class="body">
      <div class="msgs">${msgs}<div class="msg"><div class="who">desk · ${d.last_reply ? 'last reply' : 'last visible line'}</div>${d.last_reply || d.last_line ? `<span class="${d.last_reply ? '' : 'mono'}">${esc(d.last_reply || d.last_line)}</span>` : '<span class="muted">nothing visible</span>'}</div></div>
      <div class="composer">${canWrite() ? `<input class="input" data-rc-input="chat" maxlength="1500" placeholder="ask the desk… (Enter sends)" aria-label="message to the desk">` : `<input class="input" disabled placeholder="ask the desk…" title="${esc(NO_TOKEN)}" aria-label="message to the desk">`}${rc('send', { kind: 'question', from: 'input', key: 'chat' })}</div>
      <div class="note">The desk treats this as a question or a claim, never as an approval. Approvals are typed in the desk's tmux window. Its answer comes back here as its reply to the receipt (\`zouinify reply\`); the last line below it is its last visible reply to anyone.</div></div></section>`;
  }
  function logsPanel(s) {
    const entries = Object.entries(s.logs || {});
    if (!entries.length) return '';
    return `<section class="panel logtail"><header><h2>Log tails</h2>${stale('logs')}</header><div class="body">${entries.map(([label, l]) => `<h3 class="secondary">${esc(label)} <span class="mono muted">${esc(l.path || l.glob)}${l.mtime ? ' · ' + esc(l.mtime.slice(0, 16)) : ''}</span></h3><pre>${l.tail.length ? esc(l.tail.join('\n')) : '(no file matches yet)'}</pre>`).join('')}</div></section>`;
  }

  // ---- pages --------------------------------------------------------------------------------------------
  function pageDashboard(s) {
    return `<div class="dash">${p0Banner(s)}${kpis(s)}${board(s)}<div class="row2">${inboxPanel(s)}${waitingPanel(s)}</div><div class="row3">${sessionsTable(s)}${chatPanel(s)}</div>${logsPanel(s)}</div>`;
  }
  function pageTickets(s, q) {
    const states = Object.keys(s.board.counts).sort();
    const want = q.get('state') || '';
    const text = (q.get('q') || '').toLowerCase();
    const SEVN = { P0: 0, P1: 1, P2: 2, P3: 3 };
    let list = s.tickets.filter((t) => (!want || t.status === want) && (!text || (t.id + ' ' + t.title + ' ' + t.owner).toLowerCase().includes(text)));
    list = list.slice().sort((a, b) => (SEVN[a.severity] ?? 9) - (SEVN[b.severity] ?? 9) || (a.created < b.created ? -1 : 1));
    return `<h1>Tickets <span class="muted">${list.length} of ${s.tickets.length}</span></h1>
      <div class="filters"><a class="chip ${!want ? 'active' : ''}" href="#/tickets">all <b>${s.tickets.length}</b></a>${states.map((st) => `<a class="chip ${want === st ? 'active' : ''}" href="#/tickets?state=${esc(st)}">${esc(st)} <b>${esc(s.board.counts[st])}</b></a>`).join('')}
      <input class="input" type="search" data-search placeholder="search id, title, owner" value="${esc(q.get('q') || '')}" aria-label="search tickets"></div>
      <section class="panel tlist">${list.length ? list.map((t) => `<div class="trow" role="button" tabindex="0" data-open-ticket="${esc(t.id)}"><span class="id">${esc(t.id)}</span><span>${sev(t.severity)}</span><span class="truncate">${esc(t.title)}</span><span class="c4 muted">${esc(t.status)}</span><span class="c5 muted truncate">${esc(t.owner || '')}</span></div>`).join('') : '<div class="empty">no ticket matches</div>'}</section>`;
  }
  function pageSessions(s) { return `<h1>Sessions</h1>${sessionsTable(s, true)}<div class="note">states: waiting for you (amber), working, idle, gone (registered or configured but not running), shell (the pane runs something other than claude). The model badge turns red when the status bar names a model other than the configured one; "?" means no status bar is on the visible screen.</div>`; }
  function pageLive(s) {
    const rows = s.live_checks;
    const awaiting = rows.filter((r) => !r.has_evidence).length;
    return `<h1>Live checks <span class="muted">${rows.length} deployed · ${awaiting} awaiting a live check</span></h1>
      <section class="panel"><div class="body"><table class="tbl"><thead><tr><th>ticket</th><th></th><th>title</th><th>deploy</th><th>latest evidence</th></tr></thead><tbody>
      ${rows.length ? rows.map((r) => `<tr><td class="mono"><a href="#/tickets/${esc(r.id)}">${esc(r.id)}</a></td><td>${sev(r.severity)}</td><td>${esc(r.title)}</td><td class="muted">${esc(r.deploy)}</td><td class="evidence ${r.has_evidence ? '' : 'none'}">${r.has_evidence ? esc(r.evidence) + `<div class="muted">${esc(r.evidence_at)} ${esc(r.evidence_by)}</div>` : 'no live-check line yet' + (r.last ? `<div class="muted">last: ${esc(r.last)}</div>` : '')}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">nothing is deployed</td></tr>'}
      </tbody></table></div></section><div class="note">Evidence is what the debug sessions log on the ticket (a live-check line, a reload marker, a job id, a read-back). There is no test runner and no pass rate.</div>`;
  }
  function pageInbox(s) { return `<h1>Inbox <span class="muted">${s.inbox.items.length} items</span></h1>${inboxPanel(s, true)}`; }
  function pageChat(s) { return `<h1>Desk chat</h1>${chatPanel(s)}<div class="note">A message is a receipt of kind question: the desk fetches it with its poll and answers in its own window; the reply shows here through its Stop hook. Presence: polling (a poll is open) or not polling since HH:MM.</div>`; }
  function pageSettings(s) {
    const c = s.config;
    const sess = Object.entries(c.sessions || {}).map(([role, x]) => `<tr><td>${esc(role)}</td><td>${esc(x.name)}</td><td class="mono">${esc(x.tmux)}</td><td>${esc(x.model)}</td><td>${esc(x.permission_mode || '(unset)')}</td><td class="mono">${esc(x.brief || x.prompt || '')}</td></tr>`).join('');
    const repos = (c.repos || []).map((r) => `<tr><td>${esc(r.name)}</td><td class="mono">${esc(r.path)}</td><td>${esc(r.trunk)}</td><td>${esc(r.prod || '')}</td></tr>`).join('');
    return `<h1>Settings <span class="muted">read-only</span></h1>
      <section class="panel settings"><header><h2>Sessions (.ops/config.yaml)</h2></header><div class="body"><table class="tbl"><thead><tr><th>role</th><th>name</th><th>tmux</th><th>model</th><th>permission mode</th><th>brief / prompt</th></tr></thead><tbody>${sess}</tbody></table></div></section>
      <section class="panel settings"><header><h2>Repositories and panel</h2></header><div class="body">
        <table class="tbl"><thead><tr><th>repo</th><th>path</th><th>trunk</th><th>prod</th></tr></thead><tbody>${repos}</tbody></table>
        <dl class="kv">
        <dt>panel</dt><dd>port ${esc(c.panel.port)} · bind ${esc(c.panel.bind)} · state ${esc(c.panel.state_dir)} · refresh ${esc(c.panel.refresh_s)} s · autostart ${esc(c.panel.autostart)}</dd>
        <dt>logs</dt><dd>${(c.panel.logs || []).length ? c.panel.logs.map((l) => `${esc(l.label)}: <span class="mono">${esc(l.glob)}</span>`).join(' · ') : 'none configured'}</dd>
        <dt>plans</dt><dd>${c.panel.plans_glob ? `<span class="mono">${esc(c.panel.plans_glob)}</span>` : 'no plan rule'}</dd>
        <dt>human-only</dt><dd>${(c.human_only || []).map(esc).join('<br>') || 'nothing listed'}</dd>
        <dt>budget</dt><dd>${c.budget && c.budget.paid_usd_per_day != null ? esc(c.budget.paid_usd_per_day) + ' USD/day · ' + esc(c.budget.approval_text || '') : 'none'}</dd></dl></div></section>
      <section class="panel settings"><header><h2>Hook (T-0163)</h2></header><div class="body"><p>hook installed in <span class="mono">${esc(s.hook.path)}</span>: <b>${s.hook.installed ? 'yes' : 'no'}</b>. ${esc(s.hook.note)}</p>${s.hook.installed ? '' : `<pre>${esc(s.hook.snippet)}</pre>${copyBtn(s.hook.snippet, 'copy snippet')}`}</div></section>
      <section class="panel settings"><header><h2>Receipts, token and versions</h2></header><div class="body"><p>The one write of this page is <code>POST /api/receipts</code>: a click is a receipt the desk fetches with its poll and verifies before acting. It needs the browser token on every request, loopback included: rotate with <code>zouinify panel token rotate</code> in your own terminal and open the address it prints in this browser (the token is printed once there and never shown here). Token set: <b>${s.panel.receipts && s.panel.receipts.token_configured ? 'yes' : 'no'}</b> · desk ${esc((s.panel.receipts && s.panel.receipts.presence) || '')} · ${esc((s.panel.receipts && s.panel.receipts.accepted_1m) || 0)} receipt(s) in the last minute, ${esc((s.panel.receipts && s.panel.receipts.refused_1m) || 0)} refused write(s) · budget ${esc((s.panel.receipts && s.panel.receipts.rate && s.panel.receipts.rate.per_minute) || '')}/min, ${esc((s.panel.receipts && s.panel.receipts.rate && s.panel.receipts.rate.per_hour) || '')}/h.</p>
        ${(s.panel.receipts && s.panel.receipts.recent && s.panel.receipts.recent.length) ? `<table class="tbl"><thead><tr><th>receipt</th><th>at</th><th>kind</th><th>ticket</th><th>text</th><th>desk</th><th>reply</th></tr></thead><tbody>${s.panel.receipts.recent.map((r) => `<tr><td class="mono">${esc(r.id)}</td><td class="mono">${esc(String(r.at).slice(5, 16))}</td><td>${esc(r.kind)}</td><td class="mono">${esc(r.ticket)}</td><td class="truncate" title="${esc(r.text)}">${esc(String(r.text).slice(0, 80))}</td><td class="muted">${r.acked_at ? 'acked ' + esc(String(r.acked_at).slice(11, 16)) : r.delivered_at ? 'fetched ' + esc(String(r.delivered_at).slice(11, 16)) + (r.redelivered ? ', re-sent ' + esc(r.redelivered) + '×' : '') : 'pending'}</td><td class="reply ${verdictClass(r.reply)}" title="${esc(r.reply)}">${r.reply ? esc(String(r.replied_at).slice(11, 16)) + ' ' + esc(r.reply) : '<span class="muted">—</span>'}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">no receipt yet</p>'}
        <dl class="kv"><dt>zouinify</dt><dd>${esc(s.version)}</dd><dt>panel started</dt><dd>${esc(s.panel.started_at)}</dd><dt>board source</dt><dd>${esc(s.board.source)}</dd></dl></div></section>`;
  }

  function renderPage() {
    const el = $('#page');
    const { page, id, q } = route();
    if (!state.snap) { el.innerHTML = state.loading ? '<div class="panel"><div class="body"><div class="skeleton"></div></div></div>' : `<div class="panel"><div class="body error">could not load /api/snapshot: ${esc(state.error)} <button type="button" class="btn" data-refresh>Retry</button></div></div>`; return; }
    const s = state.snap;
    const pages = { dashboard: pageDashboard, waiting: pageWaiting, tickets: pageTickets, sessions: pageSessions, live: pageLive, inbox: pageInbox, chat: pageChat, settings: pageSettings };
    const rcMeta = s.panel.receipts || {};
    const bannerText = state.banner || (rcMeta.refused_1m >= 5 ? BANNER_429 : '');
    const banner = bannerText ? `<div class="banner red" role="alert">${esc(bannerText)}${state.banner ? ' <button type="button" class="btn ghost" data-banner-close aria-label="dismiss">✕</button>' : ''}</div>` : '';
    el.innerHTML = banner + (pages[page] || pageDashboard)(s, q);
    if (page === 'tickets' && id) openTicket(id);
    else if (page === 'sessions' && id) openSession(id);
    else if (page === 'waiting' && id) openWaiting(id);
    else closeDrawer(false);
    document.title = `zouinify · ${s.project.short}${s.kpis.waiting ? ` · ${s.kpis.waiting} waiting` : ''}${s.kpis.p0 ? ` · P0 ${s.kpis.p0}` : ''}`;
  }
  function render() { renderNav(); renderHeader(); renderPage(); }

  // ---- drawer -------------------------------------------------------------------------------------------
  function drawerShell(title, body) {
    const d = $('#drawer');
    d.innerHTML = `<header><h2 id="drawer-title">${title}</h2><button type="button" class="btn ghost" data-close-drawer aria-label="close">✕</button></header><div class="body">${body}</div>`;
    d.hidden = false;
    $('[data-close-drawer]', d).focus();
  }
  function closeDrawer(nav = true) {
    const d = $('#drawer');
    if (d.hidden) return;
    d.hidden = true;
    state.drawer = null;
    if (nav) { const { page } = route(); location.hash = '#/' + (page === 'dashboard' ? 'dashboard' : page); }
  }
  async function openTicket(id) {
    state.drawer = 'ticket:' + id;
    drawerShell(`<span class="mono">${esc(id)}</span>`, '<div class="skeleton"></div>');
    try {
      const r = await fetch('/api/tickets/' + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const t = await r.json();
      if (state.drawer !== 'ticket:' + id) return;
      const fm = t.front_matter;
      const kv = Object.entries(fm).filter(([k, v]) => v !== '' && v != null && k !== 'id').map(([k, v]) => `<dt>${esc(k)}</dt><dd class="${/^(branch|worktree|base|closed_by|plan|repos)$/.test(k) ? 'mono' : ''}">${esc(v)}</dd>`).join('');
      const log = t.log.map((l) => `<div class="l ${l.evidence ? 'ev' : ''}"><span class="muted">${esc(l.at)} ${esc(l.by)}</span> ${esc(l.text)}</div>`).join('');
      const handoff = t.handoff ? `<div class="handoff"><h2>Deploy handoff</h2>${t.handoff.split('\n').map((line) => /^\s*(cd |scripts\/|terraform|aws |git |docker )/.test(line) ? `<pre>${esc(line)}</pre>${copyBtn(line.trim(), 'copy line')}` : `<pre>${esc(line)}</pre>`).join('')}</div>` : '';
      const owner = fm.owner ? `<a href="#/sessions/${encodeURIComponent(fm.owner)}">${esc(fm.owner)}</a>` : '';
      const inboxItem = (state.snap.inbox.items || []).find((i) => String(i.tickets || '').includes(id));
      drawerShell(`<span class="mono">${esc(id)}</span> ${sev(fm.severity)} <span class="secondary">${esc(fm.status)}</span>`,
        `<div>${esc(fm.title)}</div><dl class="kv">${kv}</dl>${owner ? `<div class="note">owner session: ${owner}</div>` : ''}${inboxItem ? `<div class="note"><a href="#/inbox">in the inbox: ${esc(inboxItem.verb)} ${esc(inboxItem.title)}</a></div>` : ''}
         <div><h2>Log <span class="muted">last ${t.log.length} of ${t.log_total}, newest first</span></h2><div class="loglines">${log || '<span class="muted">no log lines</span>'}</div></div>${handoff}
         <div class="note">Nothing here is editable: a state change needs evidence and is made by the owning session through the ticket lifecycle script. <span class="mono">${esc(t.file)}</span></div>`);
    } catch (e) {
      drawerShell(`<span class="mono">${esc(id)}</span>`, `<div class="error">${esc(e.message || e)}</div>`);
    }
  }
  async function openSession(name) {
    state.drawer = 'session:' + name;
    drawerShell(esc(name), '<div class="skeleton"></div>');
    try {
      const r = await fetch('/api/sessions/' + encodeURIComponent(name) + '?fresh=1', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      if (state.drawer !== 'session:' + name) return;
      const reg = d.registry ? `<dl class="kv"><dt>launched</dt><dd>${esc(d.registry.launched_at || '')} by ${esc(d.registry.by || '')}</dd><dt>role</dt><dd>${esc(d.registry.role || '')}</dd><dt>ticket</dt><dd>${esc(d.registry.ticket || '')}</dd><dt>permission mode</dt><dd>${esc(d.registry.permission_mode || '(unset)')}</dd></dl>` : '<div class="note">not launched by zouinify (no registry entry)</div>';
      drawerShell(`${esc(name)} ${stateBadge(d.state)} ${modelBadge(d)}`,
        `<dl class="kv"><dt>role</dt><dd>${esc(d.role)}</dd><dt>tmux</dt><dd class="mono">${esc(d.tmux || '—')}</dd><dt>ticket</dt><dd>${d.ticket ? `<a href="#/tickets/${esc(d.ticket)}">${esc(d.ticket)}</a>` : ''}</dd><dt>status</dt><dd>${esc(d.status)}</dd>${d.cwd ? `<dt>cwd</dt><dd class="mono">${esc(d.cwd)}</dd>` : ''}</dl>
         ${d.waiting_for ? `<div class="wait-row"><div class="what">${esc(d.waiting_for)}</div><div class="since">answer in window ${esc(d.tmux || '')} — permission prompts are never clickable${d.state === 'waiting' ? typeLink(d) : ''}</div></div>` : ''}
         <div><h2>Visible screen <span class="muted">redacted</span></h2><pre class="screen">${d.screen ? esc(d.screen) : '(no tmux window to capture)'}</pre></div>${reg}
         <div>${sessionActions(state.snap, d)}</div>`);
    } catch (e) {
      drawerShell(esc(name), `<div class="error">${esc(e.message || e)}</div>`);
    }
  }

  async function openWaiting(name) {
    state.drawer = 'waiting:' + name;
    drawerShell(esc(name), '<div class="skeleton"></div>');
    try {
      const r = await fetch('/api/waiting/' + encodeURIComponent(name) + '?fresh=1', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status === 404 ? 'that session is not waiting any more' : 'HTTP ' + r.status);
      const d = await r.json();
      if (state.drawer !== 'waiting:' + name) return;
      drawerShell(`${esc(name)} ${stateBadge('waiting')} ${badge(d.project, d.in_project ? 'mine' : 'other')}`,
        `<dl class="kv"><dt>project</dt><dd>${esc(d.project)}${d.in_project ? ' (this project)' : d.registered ? ' (another zouinify project)' : ' (directory name)'}</dd><dt>tmux</dt><dd class="mono">${esc(d.tmux || '—')}</dd><dt>waiting</dt><dd>${sinceLabel(d.since)}</dd></dl>
         ${d.waiting_for ? `<div class="wait-row"><div class="what">${esc(d.waiting_for)}</div><div class="since">answer from your terminal in tmux window ${esc(d.tmux || '(none found)')}${d.tmux ? ` ${copyBtn(d.tmux, 'copy window name')}` : ''}</div></div>` : ''}
         ${d.withheld ? `<div class="note">screen withheld: ${esc(d.withheld)}. Answer in that tmux window.</div>` : `<div><h2>${d.in_project ? 'Visible screen' : 'Dialog'} <span class="muted">${d.in_project ? 'redacted' : 'another project: the dialog only, redacted, paths and addresses removed'}</span></h2><pre class="screen">${d.screen ? esc(d.screen) : '(no tmux window to capture)'}</pre></div>`}`);
    } catch (e) {
      drawerShell(esc(name), `<div class="error">${esc(e.message || e)}</div>`);
    }
  }

  // ---- receipts (the one write) ---------------------------------------------------------------------------
  function textFor(data, button) {
    if (data.from === 'radio') { const item = button.closest('.inbox-item'); const r = item && item.querySelector('input[type=radio]:checked'); return r ? r.value : ''; }
    if (data.from === 'input') { const inp = document.querySelector(`[data-rc-input="${CSS.escape(data.key)}"]`); return inp ? inp.value.trim() : ''; }
    if (data.from === 'form') { const f = button.closest('form'); if (!f) return ''; const fd = new FormData(f); const title = String(fd.get('title') || '').trim(); if (!title) return ''; return `${fd.get('type')} ${fd.get('severity')} ${title}\n${String(fd.get('body') || '').trim()}`.trim(); }
    return data.text || '';
  }
  async function sendReceipt(data, button) {
    const body = {};
    if (data.item_id) body.item_id = data.item_id; else body.kind = data.kind;
    if (data.ticket) body.ticket = data.ticket;
    const text = textFor(data, button);
    if (text) body.text = text;
    if (data.from && !text) { toast(data.from === 'form' ? 'a title is needed' : data.from === 'radio' ? 'pick an answer first' : 'type an answer first'); return; }
    button.disabled = true;
    try {
      const r = await fetch('/api/receipts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' });
      let d = {};
      try { d = await r.json(); } catch (e) { d = {}; }
      if (r.status === 201 || r.status === 200) {
        state.sent[data.key || JSON.stringify(body)] = { id: d.id, at: d.at, repeat: !!d.repeat, panel_saw: d.panel_saw || '' };
        state.banner = null;
        if (data.kind === 'sso' && d.panel_saw === 'LOGIN') { toast(`receipt ${d.id}: the panel still sees the login expired; run aws sso login in your terminal, then click again`); render(); load(); return; }
        if (data.from === 'input') { const inp = document.querySelector(`[data-rc-input="${CSS.escape(data.key)}"]`); if (inp) inp.value = ''; }
        if (data.from === 'form') state.newForm = false;
        toast(`sent, receipt ${d.id}${d.repeat ? ' (the same one: the desk has not fetched it yet)' : ''}; desk ${d.presence || 'verifying'}`);
        render();
        load();
        return;
      }
      if (r.status === 429) { state.banner = BANNER_429; render(); toast(d.error || BANNER_429); return; }
      if (r.status === 401) { toast(d.error || 'token missing or wrong: open the address printed by `zouinify panel token rotate` in your own terminal'); return; }
      if (r.status === 404 || r.status === 409) { toast(d.error || 'that item is gone'); load(); return; }
      toast(d.error || ('refused: HTTP ' + r.status));
    } catch (e) {
      toast('could not reach the panel: ' + (e && e.message ? e.message : e));
    } finally {
      button.disabled = false;
    }
  }

  // ---- events -------------------------------------------------------------------------------------------
  document.addEventListener('click', (ev) => {
    const rcb = ev.target.closest('[data-rc]');
    if (rcb) { let data = {}; try { data = JSON.parse(rcb.getAttribute('data-rc')); } catch (e) { return; } sendReceipt(data, rcb); return; }
    if (ev.target.closest('[data-newform-open]')) { state.newForm = true; renderPage(); const t = $('[data-newform] [name=title]'); if (t) t.focus(); return; }
    if (ev.target.closest('[data-newform-close]')) { state.newForm = false; renderPage(); return; }
    if (ev.target.closest('[data-banner-close]')) { state.banner = null; renderPage(); return; }
    const copy = ev.target.closest('[data-copy]');
    if (copy) { const text = copy.getAttribute('data-copy'); (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast('copied'), () => toast('copy failed: select the text')); return; }
    const t = ev.target.closest('[data-open-ticket]');
    if (t) { const { page } = route(); location.hash = `#/${page === 'dashboard' ? 'tickets' : page}/${encodeURIComponent(t.getAttribute('data-open-ticket'))}`; if (page === 'dashboard') return; openTicket(t.getAttribute('data-open-ticket')); return; }
    if (ev.target.closest('[data-close-drawer]')) { closeDrawer(true); return; }
    if (ev.target.closest('[data-refresh]')) { if (state.freshInFlight) return; toast('refreshing…'); load({ fresh: true }); return; }
    if (ev.target.closest('[data-reload]')) { location.reload(); return; }
    if (ev.target.closest('[data-menu]')) { $('#sidebar').classList.toggle('open'); return; }
    if (!ev.target.closest('#sidebar') && $('#sidebar').classList.contains('open') && !ev.target.closest('[data-menu]')) $('#sidebar').classList.remove('open');
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target.matches('[data-rc-input]') && !ev.shiftKey) { ev.preventDefault(); const b = ev.target.parentElement && ev.target.parentElement.querySelector('[data-rc]'); if (b) b.click(); return; }
    if (ev.key === 'Enter' && ev.target.matches('[data-newform] input') ) { ev.preventDefault(); const b = ev.target.closest('form').querySelector('[data-rc]'); if (b) b.click(); return; }
    if (ev.key === 'Escape') closeDrawer(true);
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches('[data-open-ticket]')) { ev.preventDefault(); ev.target.click(); }
  });
  document.addEventListener('input', (ev) => {
    if (ev.target.matches('[data-search]')) { const { q } = route(); q.set('q', ev.target.value); if (!ev.target.value) q.delete('q'); history.replaceState(null, '', '#/tickets' + (q.toString() ? '?' + q.toString() : '')); const el = $('#page'); const pos = ev.target.selectionStart; renderPage(); const inp = $('[data-search]', el); if (inp) { inp.focus(); inp.setSelectionRange(pos, pos); } }
  });
  // T-0199: a new PAGE starts at the top (Dashboard -> Sessions -> Dashboard used to land mid-board); a drawer opening or
  // closing on the same page (#/tickets -> #/tickets/T-x) or a filter change keeps the list where it was
  function routeChanged(prevHash, nextHash) {
    const page = (h) => String(h || '').replace(/^#\/?/, '').split('?')[0].split('/')[0] || 'dashboard';
    return page(prevHash) !== page(nextHash);
  }
  let lastHash = location.hash;
  window.addEventListener('hashchange', () => {
    $('#sidebar').classList.remove('open');
    const prev = lastHash;
    lastHash = location.hash;
    render();
    if (routeChanged(prev, location.hash)) window.scrollTo(0, 0);
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });   // the cached path: a tab switch is not a click

  // ---- boot -----------------------------------------------------------------------------------------------
  render();
  load();
  // live events (T-0163): a hook event re-reads the snapshot at once; the poll below stays as the fallback
  (function listen() {
    if (!window.EventSource) return;
    let es;
    try { es = new EventSource('/api/events'); } catch (e) { return; }
    let pending = null;
    es.addEventListener('hook', () => { clearTimeout(pending); pending = setTimeout(load, 300); });
    es.onerror = () => { es.close(); setTimeout(listen, 15000); };
  })();
  let timer = setInterval(() => load(), 30000);
  const retime = () => { if (state.snap && state.snap.panel && state.snap.panel.refresh_s) { clearInterval(timer); timer = setInterval(() => load(), Math.max(5, state.snap.panel.refresh_s) * 1000); } };
  setTimeout(retime, 3000);
  setInterval(() => { if (state.snap) { renderHeader(); } }, 30000);
})();
