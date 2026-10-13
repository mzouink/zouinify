/* zouinify terminal page (T-0181). No framework, no build, no request to any other host. Everything the server
   returns is rendered with textContent; the only writes are the unlock, the open, the keys you type and the close. */
(() => {
  'use strict';
  const $ = (sel) => document.querySelector(sel);
  const params = new URLSearchParams(location.search);
  const wanted = params.get('target') || '';
  const prefill = params.get('text') || '';
  let tid = null;
  let pollTimer = null;
  let toastTimer = null;

  function toast(text) {
    const t = $('#toast');
    t.textContent = text;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), 1800);
  }
  function show(id) {
    for (const s of ['unlock', 'pick', 'term', 'closed']) $('#' + s).hidden = s !== id;
    $('#lock').hidden = id === 'unlock';
    if (id !== 'term' && pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }
  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ cache: 'no-store', credentials: 'same-origin' }, opts || {}));
    let d = {};
    try { d = await r.json(); } catch (e) { d = {}; }
    return { status: r.status, d };
  }
  function post(path, body) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }

  async function start() {
    const me = await api('/term/me');
    if (me.status !== 200) { show('unlock'); $('#passphrase').focus(); return; }
    $('#who').textContent = `${me.d.project} · unlock ${me.d.unlock8}`;
    if (wanted) open(wanted); else pick();
  }

  $('#unlock-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const pp = $('#passphrase').value;
    $('#passphrase').value = '';
    if (!pp) return;
    const r = await post('/term/unlock', { passphrase: pp });
    if (r.status === 200) { $('#unlock-msg').textContent = ''; start(); return; }
    $('#unlock-msg').textContent = r.status === 429 ? (r.d.error || 'too many attempts: wait') : r.status === 503 ? (r.d.error || 'no passphrase is set') : 'wrong passphrase';
    $('#passphrase').focus();
  });

  async function pick() {
    show('pick');
    const r = await api('/term/targets');
    if (r.status !== 200) { show('unlock'); return; }
    const ul = $('#targets');
    ul.textContent = '';
    for (const t of r.d.targets || []) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn target' + (t.live ? '' : ' dead');
      b.disabled = !t.live;
      b.textContent = t.name + (t.ticket ? ' · ' + t.ticket : '') + (t.live ? '' : ' · ' + t.reason);
      b.addEventListener('click', () => open(t.name));
      li.appendChild(b);
      ul.appendChild(li);
    }
    $('#pick-msg').textContent = (r.d.targets || []).length ? '' : 'no worker session is running in this project';
  }
  $('#refresh').addEventListener('click', pick);

  async function open(name) {
    const r = await post('/term/open', { target: name });
    if (r.status !== 200) {
      if (r.status === 401) { show('unlock'); return; }
      $('#pick-msg').textContent = (r.d.error || 'cannot open') + (r.d.reason ? ': ' + r.d.reason : '');
      show('pick');
      pick();
      return;
    }
    tid = r.d.tid;
    $('#target').textContent = r.d.target;
    $('#tmux').textContent = r.d.tmux || '';
    $('#tid8').textContent = r.d.tid8;
    $('#closes').textContent = r.d.closes_in_s;
    $('#screen').textContent = '';
    $('#text').value = prefill;
    show('term');
    $('#text').focus();
    poll();
  }

  async function poll() {
    if (!tid) return;
    const r = await api(`/term/${tid}/screen`);
    if (!tid) return;
    if (r.status === 410) { closed(r.d.error || 'closed'); return; }
    if (r.status === 401) { tid = null; show('unlock'); return; }
    if (r.status === 200) {
      $('#screen').textContent = r.d.text || '';
      $('#closes').textContent = r.d.closes_in_s;
    }
    pollTimer = setTimeout(poll, 300);
  }

  async function send(ops) {
    if (!tid) return;
    const r = await post(`/term/${tid}/keys`, { ops });
    if (r.status === 410) { closed(r.d.error || 'closed'); return; }
    if (r.status === 401) { tid = null; show('unlock'); return; }
    if (r.status !== 200) { $('#term-msg').textContent = r.d.error || ('refused (' + r.status + ')'); return; }
    $('#term-msg').textContent = '';
    $('#closes').textContent = r.d.closes_in_s;
    toast('sent');
  }

  function closed(why) {
    tid = null;
    $('#closed-msg').textContent = why;
    show('closed');
  }

  document.querySelector('.keybar').addEventListener('click', (ev) => {
    const b = ev.target.closest('button.key');
    if (!b) return;
    if (b.dataset.key) send([{ k: b.dataset.key }]);
    else if (b.dataset.text) send([{ t: b.dataset.text }]);
    $('#text').focus();
  });
  $('#type-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const v = $('#text').value;
    $('#text').value = '';
    send(v ? [{ t: v }, { k: 'Enter' }] : [{ k: 'Enter' }]);
  });
  $('#text').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.shiftKey) {
      ev.preventDefault();
      const v = $('#text').value;
      $('#text').value = '';
      if (v) send([{ t: v }]);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      send([{ k: 'Escape' }]);
    }
  });
  $('#close').addEventListener('click', async () => {
    if (tid) { await post(`/term/${tid}/close`, {}); }
    closed('closed by you');
  });
  $('#again').addEventListener('click', pick);
  $('#lock').addEventListener('click', async () => {
    await post('/term/lock', {});
    tid = null;
    show('unlock');
  });

  start();
})();
