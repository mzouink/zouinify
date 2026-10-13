/* zouinify panel v1 (read-only). One page, hash routes, no framework, no build step, no request to any
   host but this one: the page fetches /api/snapshot every refresh_s seconds and renders everything from
   it. Every control is disabled with the tooltip "available after receipts (T-0162)" or hidden; nothing
   on this page writes anywhere (the server answers 405 to any POST anyway). */
(() => {
  'use strict';
  const DISABLED = 'available after receipts (T-0162)';
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const state = { snap: null, error: null, loading: true, fetchedAt: 0, drawer: null };

  const ICONS = {
    dashboard: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/></svg>',
    tickets: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M3 10h18"/></svg>',
    sessions: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M7 9l3 2.5L7 14M12 14h5"/></svg>',
    live: '<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>',
    inbox: '<svg viewBox="0 0 24 24"><path d="M4 4h16v12H4zM4 12h5l2 3h2l2-3h5"/><path d="M4 16v4h16v-4"/></svg>',
    chat: '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.5A8 8 0 1 1 21 12z"/></svg>',
    settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    menu: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>',
  };
  const NAV = [['dashboard', 'Dashboard'], ['tickets', 'Tickets'], ['sessions', 'Sessions'], ['live', 'Live checks'], ['inbox', 'Inbox'], ['chat', 'Desk chat'], ['settings', 'Settings']];
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
  const btn = (label, cls = '') => `<button type="button" class="btn ${cls}" disabled aria-disabled="true" title="${DISABLED}">${esc(label)}</button>`;
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
  async function load() {
    try {
      const r = await fetch('/api/snapshot', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      state.snap = await r.json();
      state.error = null;
    } catch (e) {
      state.error = String(e && e.message ? e.message : e);
    }
    state.loading = false;
    state.fetchedAt = Date.now();
    render();
    if (state.snap && state.snap.loading && state.snap.loading.length) { clearTimeout(load.warm); load.warm = setTimeout(load, 2000); }
  }

  // ---- shell --------------------------------------------------------------------------------------------
  function renderNav() {
    const { page } = route();
    const s = state.snap;
    const counts = { inbox: s ? s.kpis.inbox : '', sessions: s ? s.kpis.waiting || '' : '', tickets: s ? s.kpis.tickets : '' };
    $('#nav').innerHTML = NAV.map(([key, label]) =>
      `<a href="#/${key}" class="${page === key ? 'active' : ''}" aria-current="${page === key ? 'page' : 'false'}">${ICONS[key]}<span class="lbl">${label}</span>${counts[key] !== '' && counts[key] != null ? `<span class="count">${esc(counts[key])}</span>` : ''}</a>`).join('');
    $('#sidebar-foot').innerHTML = s ? `read-only v1 · ${esc(s.panel.bind)} · refresh ${esc(s.panel.refresh_s)} s<br>zouinify ${esc(s.version)}` : '';
  }
  function renderHeader() {
    const s = state.snap;
    const el = $('#header');
    if (!s) { el.innerHTML = `<button type="button" class="btn ghost menu" data-menu aria-label="menu">${ICONS.menu}</button><span class="proj">zouinify</span>${state.error ? `<span class="error">${esc(state.error)} <button type="button" class="btn ghost" data-refresh>retry</button></span>` : '<span class="skeleton"></span>'}`; return; }
    const f = s.header.freeze, c = s.header.creds, d = s.header.desk;
    el.innerHTML = [
      `<button type="button" class="btn ghost menu" data-menu aria-label="menu">${ICONS.menu}</button>`,
      `<span class="proj">${esc(s.project.short)}</span><span class="secondary">${esc(s.project.name)}</span>`,
      `<span class="sep">·</span><span class="mono">trunk ${esc(s.project.trunk)}${s.project.trunk_sha ? ' @' + esc(s.project.trunk_sha) : ''}</span>`,
      `<span class="sep">·</span>${f.on ? badge('FREEZE on', 'freeze-on', 'allowed: ' + (f.ids || []).join(' ')) : badge('FREEZE off', 'freeze-off')}`,
      `<span class="sep">·</span><span title="${esc(c.source)}">creds ${esc(c.left)}</span>`,
      `<span class="sep">·</span><span class="presence ${esc(d.state)}" title="${esc(d.presence)}">desk ${esc(STATE_LABEL[d.state] || d.state)}</span>`,
      `<span class="sep">·</span><span class="muted" title="generated ${esc(s.generated)}">data ${fmtDur(Date.now() - state.fetchedAt) || '0m'} ago</span>`,
      s.loading && s.loading.length ? `<span class="sep">·</span>${badge('loading ' + s.loading.join(', '), 'stale', 'sources still warming up')}` : '',
      `<span class="sep">·</span>${badge(s.panel.bind === 'loopback' ? 'loopback' : s.panel.bind, 'lock', 'the panel listens on 127.0.0.1 only')}`,
      `<button type="button" class="btn ghost" data-refresh title="refresh now" aria-label="refresh now">↻</button>`,
    ].join('');
  }

  // ---- components -----------------------------------------------------------------------------------
  function p0Banner(s) {
    if (!s.p0.length) return '';
    return `<div class="p0banner" role="alert">${s.p0.map((t) => `<span><span class="id">!! P0 ${esc(t.id)}</span> ${esc(t.title)} <span class="muted">· ${esc(t.status)}${t.owner ? ' · ' + esc(t.owner) : ''}</span></span>`).join('')}<span class="muted">a P0 is the one thing that may interrupt before the next sitting</span></div>`;
  }
  function kpis(s) {
    const k = s.kpis, sit = s.sitting;
    return `<div class="kpis">
      <a class="panel card kpi" href="#/inbox"><div class="label">For you</div><div class="big">${k.inbox == null ? '?' : esc(k.inbox)}</div><div class="sub">${k.p0 ? `<span class="p0">P0 ${esc(k.p0)}</span> · ` : ''}${sit ? `next sitting ${esc(sit.at)} in ${esc(countdown(sit))}` : 'no sitting scheduled'}</div></a>
      <a class="panel card kpi" href="#/sessions"><div class="label">Sessions</div><div class="big ${k.waiting ? 'amber' : ''}">${esc(k.waiting)}</div><div class="sub">waiting for you · ${esc(k.working)} working · ${esc(k.idle)} idle · ${esc(k.gone)} gone</div></a>
      <a class="panel card kpi" href="#/tickets?state=ready-to-deploy"><div class="label">Deploy &amp; verify</div><div class="big">${esc(k.to_deploy)}</div><div class="sub">to deploy · ${esc(k.deployed)} deployed awaiting a live check</div></a>
    </div>`;
  }
  function countdown(sit) {
    if (!sit) return '';
    if (sit.at_iso) { const ms = new Date(sit.at_iso).getTime() - Date.now(); return ms > 0 ? fmtDur(ms) : 'now'; }
    return sit.in || '';
  }
  function tcard(t, lane) {
    const extras = [];
    if (lane === 'in-dev' || lane === 'review') { if (t.owner) extras.push(esc(t.owner)); if (t.claimed) extras.push('claimed ' + age(t.claimed) + ' ago'); if (t.status === 'ready-to-merge') extras.push(badge('ready-to-merge', '')); }
    if (lane === 'to deploy') { if (t.plan) extras.push(`<span class="mono">${esc(String(t.plan).split('/').pop())}</span>`); if (t.plan_void) extras.push(badge('VOID', 'void', 'a newer plan exists')); if (t.status === 'merged') extras.push('merged, no plan yet'); }
    if (lane === 'deployed') { if (t.closed_by) extras.push(`<span class="mono">${esc(String(t.closed_by).replace(/[\[\]]/g, '').slice(0, 40))}</span>`); extras.push('awaiting live check'); if (t.plan_void) extras.push(badge('VOID', 'void')); }
    if (lane === 'ready') { if (t.deploy && t.deploy !== 'none') extras.push('deploy: ' + esc(t.deploy)); }
    const freeze = state.snap.header.freeze;
    const launchable = lane === 'ready' && !(freeze.on && !(freeze.ids || []).includes(t.id));
    return `<div class="card tcard clickable" tabindex="0" role="button" data-open-ticket="${esc(t.id)}" aria-label="open ${esc(t.id)}">
      <div class="top"><span class="id">${esc(t.id)}</span>${sev(t.severity)}${t.human ? '<span class="bell" title="a pending ask for the human">🔔</span>' : ''}</div>
      <div class="title">${esc(t.title)}</div>
      ${extras.length ? `<div class="extra">${extras.join('<span class="sep">·</span>')}</div>` : ''}
      ${launchable ? `<div class="actions">${btn('launch worker')}</div>` : ''}
    </div>`;
  }
  function board(s, compact = true) {
    const b = s.board;
    const chips = Object.entries(b.chips).map(([k, n]) => `<a class="chip ${k === 'blocked' && n ? 'blocked' : ''}" href="#/tickets?state=${esc(k)}">${esc(k)} <b>${esc(n)}</b></a>`).join('');
    const other = Object.entries(b.other || {}).map(([k, n]) => `<a class="chip" href="#/tickets?state=${esc(k)}" title="a state outside the five lanes">${esc(k)} <b>${esc(n)}</b></a>`).join('');
    const lanes = LANES.map(([name, cls]) => { const cards = b.lanes[name] || []; return `<section class="lane ${cls}" aria-label="${esc(name)}"><header>${esc(name)}<span class="n">${cards.length}</span></header><div class="cards">${cards.length ? cards.map((t) => tcard(t, name)).join('') : '<div class="empty">none</div>'}</div></section>`; }).join('');
    return `<div class="boardwrap"><div class="boardhead"><h2>Tickets</h2>${chips}${other}<span class="muted">${stale('board')}</span><span class="spacer"></span>${btn('Send to desk')}</div>
      <div class="board" role="list">${lanes}</div>
      ${b.newest_plan ? `<div class="note">newest plan: <span class="mono">${esc(b.newest_plan)}</span>${b.void_plans.length ? ' · VOID: ' + esc(b.void_plans.join(', ')) : ''}</div>` : ''}
      ${b.warnings && b.warnings.length ? `<div class="error">${b.warnings.map(esc).join(' · ')}</div>` : ''}</div>`;
  }
  function inboxItem(it, i) {
    const isQuestion = /^question:|^reply /i.test(it.command || '');
    const typeIn = it.tier === 2;
    let act;
    if (typeIn) { const toType = /^(APPLY|DEPLOY|PUSH)$/.test(it.verb) ? `${it.tickets} ok` : (it.expect || it.command); act = `<span class="type">${esc(it.hint)}</span> ${copyBtn(toType, 'copy what to type')} ${badge('tier 2: never clickable', 'tier2')}`; }
    else if (it.verb === 'DECIDE') act = it.options.length ? `<span class="options">${it.options.map((o) => `<label><input type="radio" name="d-${esc(it.id)}" disabled> ${esc(o)}</label>`).join('')}</span>${btn('Send answer')}` : `<input class="input" disabled placeholder="your answer (${DISABLED})"> ${btn('Send answer')}`;
    else act = `<span class="type">${esc(it.hint)}</span>${btn('OK')}`;
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
  function waitingPanel(s) {
    const rows = s.waiting;
    return `<section class="panel waitpanel"><header><h2>Panes waiting for you</h2>${stale('sessions')}</header>${rows.length ? rows.map((r) => `<div class="wait-row"><div class="name"><a href="#/sessions/${encodeURIComponent(r.name)}">${esc(r.name)}</a>${badge(r.role, '')}${r.ticket ? `<span class="mono muted">${esc(r.ticket)}</span>` : ''}</div><div class="what">${esc(r.waiting_for || 'waiting')}</div><div class="since">${r.tmux ? 'window ' + esc(r.tmux) + ' · ' : ''}answer in that window (permission prompts are never clickable)</div></div>`).join('') : '<div class="empty">no session is waiting for you</div>'}</section>`;
  }
  function sittingPanel(s) {
    const sit = s.sitting;
    return `<section class="panel sitting"><header><h2>Next sitting</h2></header><div class="body">${sit ? `<div class="big">${esc(sit.day)} ${esc(sit.at)} <span class="muted">${esc(sit.tz)}</span></div><div>in <span class="in">${esc(countdown(sit))}</span> · inbox freezes at ${esc(sit.freeze_at)}</div><div class="note">a P0 is the only thing that may interrupt before</div>` : '<div class="muted">Not scheduled (no sittings in .ops/config.yaml)</div>'}</div></section>`;
  }
  function sessionsTable(s, full = false) {
    const rows = s.sessions.rows;
    if ((s.loading || []).includes('sessions')) return `<section class="panel sesspanel"><header><h2>Sessions</h2>${stale('sessions')}</header><div class="body"><div class="skeleton"></div></div></section>`;
    const tr = (r) => `<tr class="row-${esc(r.state)} ${r.role === 'desk' ? 'row-desk' : ''}">
      <td>${esc(r.role)}</td><td><a href="#/sessions/${encodeURIComponent(r.name)}">${esc(r.name)}</a></td><td class="mono">${esc(r.tmux || '—')}</td><td>${modelBadge(r)}</td><td>${stateBadge(r.state)}</td>
      <td class="mono">${r.ticket ? `<a href="#/tickets/${esc(r.ticket)}">${esc(r.ticket)}</a>` : ''}</td><td class="last truncate" title="${esc(r.last_line)}">${esc(r.waiting_for || r.last_line || '')}</td>
      ${full ? `<td class="nowrap">${r.state === 'gone' ? btn('launch') : r.ticket ? btn('kill') : ''}</td>` : ''}</tr>`;
    return `<section class="panel sesspanel sessions-table"><header><h2>Sessions</h2><span class="muted">${rows.length} of this project · desk first</span>${stale('sessions')}${full ? '' : '<a href="#/sessions" class="muted">all</a>'}</header>
      ${s.sessions.error ? `<div class="error">${esc(s.sessions.error)}</div>` : ''}
      <div class="body"><table class="tbl"><thead><tr><th>role</th><th>name</th><th>tmux</th><th>model</th><th>state</th><th>ticket</th><th>last line</th>${full ? '<th></th>' : ''}</tr></thead><tbody>${rows.map(tr).join('')}</tbody></table></div></section>`;
  }
  function chatPanel(s) {
    const d = s.header.desk;
    return `<section class="panel chat"><header><h2>Desk chat</h2><span class="presence ${esc(d.state)}">${esc(d.name || 'desk')} · ${esc(STATE_LABEL[d.state] || d.state)}</span></header><div class="body">
      <div class="msgs"><div class="msg"><div class="who">desk · last visible line</div>${d.last_line ? `<span class="mono">${esc(d.last_line)}</span>` : '<span class="muted">nothing visible</span>'}</div></div>
      <div class="composer"><input class="input" disabled placeholder="ask the desk… (${DISABLED})" aria-label="message to the desk">${btn('send')}</div>
      <div class="note">The desk treats this as a question or a claim, never as an approval. Approvals are typed in the desk's tmux window.</div></div></section>`;
  }
  function logsPanel(s) {
    const entries = Object.entries(s.logs || {});
    if (!entries.length) return '';
    return `<section class="panel logtail"><header><h2>Log tails</h2>${stale('logs')}</header><div class="body">${entries.map(([label, l]) => `<h3 class="secondary">${esc(label)} <span class="mono muted">${esc(l.path || l.glob)}${l.mtime ? ' · ' + esc(l.mtime.slice(0, 16)) : ''}</span></h3><pre>${l.tail.length ? esc(l.tail.join('\n')) : '(no file matches yet)'}</pre>`).join('')}</div></section>`;
  }

  // ---- pages --------------------------------------------------------------------------------------------
  function pageDashboard(s) {
    return `<div class="dash">${p0Banner(s)}${kpis(s)}${board(s)}<div class="row2">${inboxPanel(s)}${waitingPanel(s)}${sittingPanel(s)}</div><div class="row3">${sessionsTable(s)}${chatPanel(s)}</div>${logsPanel(s)}</div>`;
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
  function pageChat(s) { return `<h1>Desk chat</h1>${chatPanel(s)}<div class="note">v2 (T-0162/T-0163): a question becomes a receipt the desk answers through its Stop hook; presence listening / working / away. v1 shows the desk's last visible line only.</div>`; }
  function pageSettings(s) {
    const c = s.config;
    const sess = Object.entries(c.sessions || {}).map(([role, x]) => `<tr><td>${esc(role)}</td><td>${esc(x.name)}</td><td class="mono">${esc(x.tmux)}</td><td>${esc(x.model)}</td><td>${esc(x.permission_mode || '(unset)')}</td><td class="mono">${esc(x.brief || x.prompt || '')}</td></tr>`).join('');
    const repos = (c.repos || []).map((r) => `<tr><td>${esc(r.name)}</td><td class="mono">${esc(r.path)}</td><td>${esc(r.trunk)}</td><td>${esc(r.prod || '')}</td></tr>`).join('');
    return `<h1>Settings <span class="muted">read-only</span></h1>
      <section class="panel settings"><header><h2>Sessions (.ops/config.yaml)</h2></header><div class="body"><table class="tbl"><thead><tr><th>role</th><th>name</th><th>tmux</th><th>model</th><th>permission mode</th><th>brief / prompt</th></tr></thead><tbody>${sess}</tbody></table></div></section>
      <section class="panel settings"><header><h2>Repositories, sittings, panel</h2></header><div class="body">
        <table class="tbl"><thead><tr><th>repo</th><th>path</th><th>trunk</th><th>prod</th></tr></thead><tbody>${repos}</tbody></table>
        <dl class="kv"><dt>sittings</dt><dd>${esc((c.sittings.times || []).join(', ') || 'none')} (${esc(c.sittings.tz)}); freeze ${esc(c.sittings.freeze_before_min)} min before</dd>
        <dt>panel</dt><dd>port ${esc(c.panel.port)} · bind ${esc(c.panel.bind)} · state ${esc(c.panel.state_dir)} · refresh ${esc(c.panel.refresh_s)} s · autostart ${esc(c.panel.autostart)}</dd>
        <dt>logs</dt><dd>${(c.panel.logs || []).length ? c.panel.logs.map((l) => `${esc(l.label)}: <span class="mono">${esc(l.glob)}</span>`).join(' · ') : 'none configured'}</dd>
        <dt>plans</dt><dd>${c.panel.plans_glob ? `<span class="mono">${esc(c.panel.plans_glob)}</span>` : 'no plan rule'}</dd>
        <dt>human-only</dt><dd>${(c.human_only || []).map(esc).join('<br>') || 'nothing listed'}</dd>
        <dt>budget</dt><dd>${c.budget && c.budget.paid_usd_per_day != null ? esc(c.budget.paid_usd_per_day) + ' USD/day · ' + esc(c.budget.approval_text || '') : 'none'}</dd></dl></div></section>
      <section class="panel settings"><header><h2>Hook (T-0163)</h2></header><div class="body"><p>hook installed in <span class="mono">${esc(s.hook.path)}</span>: <b>${s.hook.installed ? 'yes' : 'no'}</b>. ${esc(s.hook.note)}</p>${s.hook.installed ? '' : `<pre>${esc(s.hook.snippet)}</pre>${copyBtn(s.hook.snippet, 'copy snippet')}`}</div></section>
      <section class="panel settings"><header><h2>Token and versions</h2></header><div class="body"><p>v1 has no write path and no token. From T-0162 on: rotate with <code>zouinify panel token rotate</code> in your terminal; the token is printed once there and never shown here.</p><dl class="kv"><dt>zouinify</dt><dd>${esc(s.version)}</dd><dt>panel started</dt><dd>${esc(s.panel.started_at)}</dd><dt>board source</dt><dd>${esc(s.board.source)}</dd></dl></div></section>`;
  }

  function renderPage() {
    const el = $('#page');
    const { page, id, q } = route();
    if (!state.snap) { el.innerHTML = state.loading ? '<div class="panel"><div class="body"><div class="skeleton"></div></div></div>' : `<div class="panel"><div class="body error">could not load /api/snapshot: ${esc(state.error)} <button type="button" class="btn" data-refresh>Retry</button></div></div>`; return; }
    const s = state.snap;
    const pages = { dashboard: pageDashboard, tickets: pageTickets, sessions: pageSessions, live: pageLive, inbox: pageInbox, chat: pageChat, settings: pageSettings };
    el.innerHTML = (pages[page] || pageDashboard)(s, q);
    if (page === 'tickets' && id) openTicket(id);
    else if (page === 'sessions' && id) openSession(id);
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
         ${d.waiting_for ? `<div class="wait-row"><div class="what">${esc(d.waiting_for)}</div><div class="since">answer in window ${esc(d.tmux || '')} — permission prompts are never clickable</div></div>` : ''}
         <div><h2>Visible screen <span class="muted">redacted</span></h2><pre class="screen">${d.screen ? esc(d.screen) : '(no tmux window to capture)'}</pre></div>${reg}
         <div>${d.state === 'gone' ? btn('launch (via the desk)') : d.ticket ? btn('kill (via the desk, done tickets only)') : ''}</div>`);
    } catch (e) {
      drawerShell(esc(name), `<div class="error">${esc(e.message || e)}</div>`);
    }
  }

  // ---- events -------------------------------------------------------------------------------------------
  document.addEventListener('click', (ev) => {
    const copy = ev.target.closest('[data-copy]');
    if (copy) { const text = copy.getAttribute('data-copy'); (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast('copied'), () => toast('copy failed: select the text')); return; }
    const t = ev.target.closest('[data-open-ticket]');
    if (t) { const { page } = route(); location.hash = `#/${page === 'dashboard' ? 'tickets' : page}/${encodeURIComponent(t.getAttribute('data-open-ticket'))}`; if (page === 'dashboard') return; openTicket(t.getAttribute('data-open-ticket')); return; }
    if (ev.target.closest('[data-close-drawer]')) { closeDrawer(true); return; }
    if (ev.target.closest('[data-refresh]')) { load(); toast('refreshing'); return; }
    if (ev.target.closest('[data-menu]')) { $('#sidebar').classList.toggle('open'); return; }
    if (!ev.target.closest('#sidebar') && $('#sidebar').classList.contains('open') && !ev.target.closest('[data-menu]')) $('#sidebar').classList.remove('open');
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrawer(true);
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches('[data-open-ticket]')) { ev.preventDefault(); ev.target.click(); }
  });
  document.addEventListener('input', (ev) => {
    if (ev.target.matches('[data-search]')) { const { q } = route(); q.set('q', ev.target.value); if (!ev.target.value) q.delete('q'); history.replaceState(null, '', '#/tickets' + (q.toString() ? '?' + q.toString() : '')); const el = $('#page'); const pos = ev.target.selectionStart; renderPage(); const inp = $('[data-search]', el); if (inp) { inp.focus(); inp.setSelectionRange(pos, pos); } }
  });
  window.addEventListener('hashchange', () => { $('#sidebar').classList.remove('open'); render(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });

  // ---- boot -----------------------------------------------------------------------------------------------
  render();
  load();
  let timer = setInterval(load, 30000);
  const retime = () => { if (state.snap && state.snap.panel && state.snap.panel.refresh_s) { clearInterval(timer); timer = setInterval(load, Math.max(5, state.snap.panel.refresh_s) * 1000); } };
  setTimeout(retime, 3000);
  setInterval(() => { if (state.snap) { renderHeader(); } }, 30000);
})();
