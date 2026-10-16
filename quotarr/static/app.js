'use strict';

const view = document.getElementById('view');
const qInput = document.getElementById('q');
const filterBar = document.getElementById('show-filter');
const PAGE = 48;

let cleanup = null;     // teardown for the current page (timers, key handlers)
let requestId = 0;      // drops responses from superseded searches
let showsCache = null;

// ------------------------------------------------------------------ helpers
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) node.append(kid instanceof Node ? kid : String(kid));
  }
  return node;
}

async function getJSON(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let detail;
    try { detail = (await resp.json()).detail; } catch { /* not JSON */ }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

const postJSON = (url, body) => getJSON(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

function b64(text) {
  let bin = '';
  for (const byte of new TextEncoder().encode(text)) bin += String.fromCharCode(byte);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fmtTime(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? `${h}:` : '') + `${mm}:${String(sec).padStart(2, '0')}`;
}

function fmtBytes(n) {
  if (!n) return '0 MB';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  return `${Math.max(0.1, n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} MB`;
}

function absolute(path) { return new URL(path, location.origin).href; }

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs HTTPS; fall back for plain-HTTP LAN installs.
    const ta = el('textarea', { class: 'offscreen' }, text);
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  if (button) {
    const label = button.textContent;
    button.textContent = 'Copied!';
    setTimeout(() => { button.textContent = label; }, 1400);
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function message(title, body, actions) {
  return el('div', { class: 'empty' }, el('h2', {}, title), body ? el('p', {}, body) : null, actions || null);
}

function loading() { return el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Loading…'); }

function readSelected() {
  const fromUrl = new URLSearchParams(location.search).get('shows');
  return new Set((fromUrl || '').split(',').filter(Boolean));
}

async function loadShows() {
  if (!showsCache) showsCache = await getJSON('/api/shows').catch(() => []);
  return showsCache;
}

// ------------------------------------------------------------------ routing
function navigate(url, { replace = false } = {}) {
  history[replace ? 'replaceState' : 'pushState'](null, '', url);
  route();
}

document.addEventListener('click', (e) => {
  const a = e.target.closest('a[data-link]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(a.getAttribute('href'));
});

window.addEventListener('popstate', route);

function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const path = location.pathname;
  const cap = path.match(/^\/caption\/([^/]+)\/([^/]+)\/(\d+)\/?$/);
  if (cap) return renderCaption(cap[1], cap[2], Number(cap[3]));
  if (path === '/admin') return renderAdmin();
  return renderHome();
}

// ------------------------------------------------------------------ search box
function searchUrl(q, selected) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (selected.size) params.set('shows', [...selected].join(','));
  const qs = params.toString();
  return qs ? `/?${qs}` : '/';
}

const runTypedSearch = debounce(() => {
  const onHome = location.pathname === '/';
  const already = new URLSearchParams(location.search).get('q');
  navigate(searchUrl(qInput.value.trim(), readSelected()), { replace: onHome && !!already });
}, 260);

qInput.addEventListener('input', runTypedSearch);
document.getElementById('search-form').addEventListener('submit', (e) => {
  e.preventDefault();
  navigate(searchUrl(qInput.value.trim(), readSelected()));
});

document.getElementById('random-btn').addEventListener('click', async () => {
  const selected = readSelected();
  const qs = selected.size ? `&shows=${[...selected].join(',')}` : '';
  try {
    const { results } = await getJSON(`/api/random?n=1${qs}`);
    if (results.length) navigate(`/caption/${results[0].show}/${results[0].code}/${results[0].ms}`);
  } catch { /* nothing indexed yet */ }
});

document.addEventListener('keydown', (e) => {
  if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    e.preventDefault();
    qInput.focus();
    qInput.select();
  }
});

// ------------------------------------------------------------------ home / results
function card(r) {
  return el('a', { href: `/caption/${r.show}/${r.code}/${r.ms}`, 'data-link': true, class: 'card' },
    el('div', { class: 'frame' },
      el('img', {
        src: r.thumb, loading: 'lazy', decoding: 'async', alt: r.text,
        onerror: (e) => e.target.parentElement.classList.add('broken'),
      })),
    el('div', { class: 'card-meta' },
      el('span', { class: 'tag' }, `${r.show_title} · ${r.code}`, el('span', { class: 'time' }, fmtTime(r.ms))),
      el('p', { class: 'line' }, r.text)));
}

async function renderFilter() {
  const shows = await loadShows();
  const selected = readSelected();
  filterBar.hidden = shows.length < 2;
  if (filterBar.hidden) return;
  const q = new URLSearchParams(location.search).get('q') || '';
  filterBar.replaceChildren(
    el('a', { href: searchUrl(q, new Set()), 'data-link': true, class: `chip ${selected.size ? '' : 'on'}` }, 'All shows'),
    ...shows.map((s) => {
      const next = new Set(selected);
      if (next.has(s.slug)) next.delete(s.slug); else next.add(s.slug);
      return el('a', {
        href: searchUrl(q, next), 'data-link': true, class: `chip ${selected.has(s.slug) ? 'on' : ''}`,
        title: `${s.episodes} episodes · ${s.lines.toLocaleString()} lines`,
      }, el('img', { src: s.poster, alt: '', loading: 'lazy', onerror: (e) => e.target.remove() }), s.title);
    }));
}

async function renderHome() {
  document.body.dataset.page = 'home';
  const params = new URLSearchParams(location.search);
  const q = (params.get('q') || '').trim();
  if (document.activeElement !== qInput) qInput.value = q;
  document.title = q ? `“${q}” · Quotarr` : 'Quotarr';
  renderFilter();
  const shows = params.get('shows') || '';
  const id = ++requestId;

  if (!q) {
    const [library, random] = await Promise.all([
      loadShows(), getJSON(`/api/random?n=24${shows ? `&shows=${shows}` : ''}`).catch(() => ({ results: [] })),
    ]);
    if (id !== requestId) return;
    if (!library.length) {
      view.replaceChildren(message('Nothing indexed yet',
        'Go to Admin, switch on the shows you want, and they will appear here once their subtitles are indexed.',
        el('a', { href: '/admin', 'data-link': true, class: 'btn primary' }, 'Open Admin')));
      return;
    }
    view.replaceChildren(
      el('div', { class: 'section-head' }, el('h2', {}, 'Random screencaps'),
        el('button', { class: 'btn ghost', type: 'button', onclick: () => route() }, 'Shuffle')),
      el('div', { class: 'grid' }, random.results.map(card)));
    return;
  }

  const grid = el('div', { class: 'grid' });
  const head = el('div', { class: 'section-head' }, el('h2', {}, 'Searching…'));
  const more = el('div', { class: 'more' });
  view.replaceChildren(head, grid, more);

  let offset = 0;
  async function page() {
    const url = `/api/search?q=${encodeURIComponent(q)}&limit=${PAGE}&offset=${offset}${shows ? `&shows=${shows}` : ''}`;
    let data;
    try { data = await getJSON(url); } catch (err) {
      if (id === requestId) view.replaceChildren(message('Search failed', err.message));
      return;
    }
    if (id !== requestId) return;
    if (!data.total) {
      view.replaceChildren(message(`No matches for “${q}”`, 'Try fewer words or check the spelling.'));
      return;
    }
    head.replaceChildren(el('h2', {}, `${data.total}${data.total >= 300 ? '+' : ''} matches`));
    grid.append(...data.results.map(card));
    offset += data.results.length;
    more.replaceChildren(...(offset < data.total
      ? [el('button', { class: 'btn', type: 'button', onclick: page }, 'Load more')]
      : []));
  }
  await page();
}

// ------------------------------------------------------------------ caption page
async function renderCaption(slug, code, ms) {
  document.body.dataset.page = 'caption';
  filterBar.hidden = true;
  view.replaceChildren(loading());
  const id = ++requestId;
  let d;
  try {
    d = await getJSON(`/api/caption/${slug}/${code}/${ms}`);
  } catch (err) {
    view.replaceChildren(message('Could not load that frame', err.message));
    return;
  }
  if (id !== requestId) return;
  code = d.code;
  ms = d.ms;
  const cur = d.lines[d.current];
  const frameUrl = `/img/${slug}/${code}/${ms}.jpg`;
  const memeUrl = (text) => text.trim() ? `/meme/${slug}/${code}/${ms}.jpg?b64lines=${b64(text.trim())}` : frameUrl;
  document.title = `${d.show_title} ${code} · Quotarr`;

  // --- stage: big frame + nearby frames
  const preview = el('img', { class: 'preview', alt: cur ? cur.text : '', src: memeUrl(cur ? cur.text : '') });
  const stageFrame = el('div', { class: 'stage-frame loading-img' }, preview, el('span', { class: 'spinner' }));
  preview.addEventListener('load', () => stageFrame.classList.remove('loading-img', 'broken'));
  preview.addEventListener('error', () => { stageFrame.classList.remove('loading-img'); stageFrame.classList.add('broken'); });

  const nearby = el('div', { class: 'nearby' }, d.nearby.map((t) =>
    el('a', { href: `/caption/${slug}/${code}/${t}`, 'data-link': true, class: `nearby-frame ${t === ms ? 'on' : ''}`,
      title: fmtTime(t) },
    el('img', { src: `/thumb/${slug}/${code}/${t}.jpg`, loading: 'lazy', alt: '',
      onerror: (e) => e.target.parentElement.classList.add('broken') }))));

  // --- caption editor
  const caption = el('textarea', { id: 'caption', rows: 3, maxlength: 400, spellcheck: 'true' }, cur ? cur.text : '');
  const updatePreview = debounce(() => {
    stageFrame.classList.add('loading-img');
    preview.src = memeUrl(caption.value);
  }, 380);
  caption.addEventListener('input', updatePreview);

  const copyBtn = el('button', { class: 'btn primary', type: 'button',
    onclick: (e) => copyText(absolute(memeUrl(caption.value)), e.currentTarget) }, 'Copy image link');
  const downloadBtn = el('button', { class: 'btn', type: 'button',
    onclick: () => { window.location.href = memeUrl(caption.value) + (caption.value.trim() ? '&' : '?') + 'download=1'; } },
  'Download');
  const openBtn = el('button', { class: 'btn ghost', type: 'button',
    onclick: () => window.open(memeUrl(caption.value), '_blank', 'noopener') }, 'Open image');
  const pageLinkBtn = el('button', { class: 'btn ghost', type: 'button',
    onclick: (e) => copyText(location.href, e.currentTarget) }, 'Copy page link');

  // --- dialogue list + GIF range
  const checks = [];
  const rangeInfo = el('span', { class: 'hint' });
  const gifBtn = el('button', { class: 'btn primary', type: 'button' }, 'Make GIF');
  const mp4Btn = el('button', { class: 'btn', type: 'button' }, 'Make MP4');
  const mode = el('select', { 'aria-label': 'Clip text' },
    el('option', { value: 'subs' }, 'Burn in subtitles'),
    el('option', { value: 'custom' }, 'Use my caption'),
    el('option', { value: 'none' }, 'No text'));
  const clipOut = el('div', { class: 'clip-out' });

  function range() {
    const on = checks.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
    if (!on.length) return null;
    const lo = Math.min(...on), hi = Math.max(...on);
    checks.forEach((c, i) => { c.checked = i >= lo && i <= hi; });
    return { start: d.lines[lo].start_ms, end: d.lines[hi].end_ms };
  }

  function updateRange() {
    const r = range();
    const tooLong = r && r.end - r.start > d.max_clip_ms;
    gifBtn.disabled = mp4Btn.disabled = !r || tooLong;
    rangeInfo.textContent = !r ? 'Tick at least one line.'
      : `${((r.end - r.start) / 1000).toFixed(1)}s${tooLong ? ` (max ${d.max_clip_ms / 1000}s)` : ''}`;
    rangeInfo.classList.toggle('warn', !!tooLong);
  }

  const dialogue = el('ol', { class: 'dialogue' }, d.lines.map((line, i) => {
    const box = el('input', { type: 'checkbox', 'aria-label': `Include line ${i + 1} in clip`, onchange: updateRange });
    box.checked = i === d.current;
    checks.push(box);
    const mid = Math.floor((line.start_ms + line.end_ms) / 2);
    return el('li', { class: i === d.current ? 'current' : '' },
      box,
      el('a', { href: `/caption/${slug}/${code}/${mid}`, 'data-link': true },
        el('span', { class: 'time' }, fmtTime(line.start_ms)), el('span', { class: 'text' }, line.text)));
  }));

  function makeClip(fmt) {
    const r = range();
    if (!r) return;
    let url = `/clip/${slug}/${code}/${r.start}-${r.end}.${fmt}`;
    if (mode.value === 'custom' && caption.value.trim()) url += `?b64lines=${b64(caption.value.trim())}`;
    else if (mode.value === 'none') url += '?nosubs=1';
    const status = el('p', { class: 'hint' }, el('span', { class: 'spinner small' }), ' Rendering… this can take a few seconds.');
    clipOut.replaceChildren(status);
    const media = fmt === 'gif'
      ? el('img', { alt: 'Generated GIF' })
      : el('video', { controls: true, autoplay: true, loop: true, playsinline: true });
    const sep = url.includes('?') ? '&' : '?';
    const done = () => clipOut.replaceChildren(media, el('div', { class: 'row' },
      el('button', { class: 'btn', type: 'button', onclick: (e) => copyText(absolute(url), e.currentTarget) }, 'Copy link'),
      el('a', { class: 'btn ghost', href: `${url}${sep}download=1` }, 'Download'),
      el('a', { class: 'btn ghost', href: url, target: '_blank', rel: 'noopener' }, 'Open')));
    const fail = () => clipOut.replaceChildren(el('p', { class: 'hint warn' },
      'Rendering failed. Check the container logs (docker logs quotarr).'));
    if (fmt === 'gif') { media.onload = done; media.onerror = fail; } else { media.onloadeddata = done; media.onerror = fail; }
    media.src = url;
  }
  gifBtn.addEventListener('click', () => makeClip('gif'));
  mp4Btn.addEventListener('click', () => makeClip('mp4'));
  updateRange();

  // --- keyboard: left/right step through lines
  const onKey = (e) => {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
    const step = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0;
    const target = d.lines[d.current + step];
    if (step && target) {
      e.preventDefault();
      navigate(`/caption/${slug}/${code}/${Math.floor((target.start_ms + target.end_ms) / 2)}`);
    }
  };
  document.addEventListener('keydown', onKey);
  cleanup = () => document.removeEventListener('keydown', onKey);

  view.replaceChildren(el('section', { class: 'caption-page' },
    el('div', { class: 'stage' }, stageFrame, nearby,
      el('p', { class: 'hint center' }, 'Use ← and → to step through lines.')),
    el('aside', { class: 'panel' },
      el('div', { class: 'ep-info' },
        el('a', { href: `/?shows=${slug}`, 'data-link': true, class: 'show-link' }, d.show_title),
        el('h1', {}, code, d.episode_title ? el('span', {}, ` · ${d.episode_title}`) : null),
        el('span', { class: 'time' }, `${fmtTime(ms)}${d.duration_ms ? ` / ${fmtTime(d.duration_ms)}` : ''}`)),
      el('label', { class: 'field', for: 'caption' }, 'Caption'),
      caption,
      el('div', { class: 'row' }, copyBtn, downloadBtn, openBtn, pageLinkBtn),
      el('h2', {}, 'Make a GIF or clip'),
      el('p', { class: 'hint' }, 'Tick the lines to include, then choose what text goes on it.'),
      dialogue,
      el('div', { class: 'row' }, mode, gifBtn, mp4Btn, rangeInfo),
      clipOut)));
}

// ------------------------------------------------------------------ admin
const STATUS_LABEL = { indexed: 'Indexed', no_subs: 'No subs', error: 'Error', pending: 'Pending' };

async function renderAdmin() {
  document.body.dataset.page = 'admin';
  document.title = 'Admin · Quotarr';
  filterBar.hidden = true;
  view.replaceChildren(loading());

  const statusGrid = el('div', { class: 'status-grid' });
  const filter = el('input', { type: 'search', class: 'filter-input', placeholder: 'Filter shows…', 'aria-label': 'Filter shows' });
  const libraries = el('div', { class: 'libraries' });
  const open = new Set();   // show ids with the episode list expanded
  let shows = [];
  let busy = false;

  async function refreshStatus() {
    let s;
    try { s = await getJSON('/api/admin/status'); } catch (err) {
      statusGrid.replaceChildren(el('div', { class: 'stat bad' }, el('h3', {}, 'Server'), el('p', {}, err.message)));
      return;
    }
    busy = !!s.indexer.current_show || s.indexer.queued.length > 0;
    const plexOk = s.plex.server && !s.plex.error;
    const [n, total] = s.indexer.progress;
    const avgThumb = s.disk.thumb_files > 50 ? s.disk.thumbs / s.disk.thumb_files : 20000;
    statusGrid.replaceChildren(
      el('div', { class: `stat ${plexOk ? 'ok' : 'bad'}` }, el('h3', {}, 'Plex'),
        plexOk ? el('p', {}, `Connected to `, el('strong', {}, s.plex.server.name), ` (v${s.plex.server.version})`)
          : el('p', {}, s.plex.error || (s.plex.configured ? 'Connecting…' : 'Set PLEX_URL and PLEX_TOKEN.')),
        el('button', { class: 'btn small', type: 'button', onclick: async (e) => {
          e.currentTarget.disabled = true;
          await postJSON('/api/admin/sync');
          setTimeout(() => { refreshStatus(); refreshShows(); }, 1500);
        } }, 'Refresh from Plex')),
      el('div', { class: `stat ${busy ? 'busy' : ''}` }, el('h3', {}, 'Indexer'),
        s.indexer.current_show
          ? el('p', {}, el('strong', {}, s.indexer.current_show), s.indexer.current_episode ? ` · ${s.indexer.current_episode}` : '',
            total ? el('span', { class: 'progress' }, el('span', { style: `width:${(100 * n) / total}%` })) : null,
            total ? el('small', {}, `${n} / ${total} episodes`) : null)
          : el('p', {}, 'Idle'),
        s.indexer.queued.length ? el('small', {}, `Queued: ${s.indexer.queued.join(', ')}`) : null,
        s.indexer.last_error ? el('small', { class: 'warn' }, s.indexer.last_error) : null),
      el('div', { class: 'stat' }, el('h3', {}, 'Index'),
        el('p', {}, el('strong', {}, (s.totals.indexed || 0).toLocaleString()), ' episodes · ',
          el('strong', {}, (s.totals.lines || 0).toLocaleString()), ' lines'),
        (s.totals.no_subs || s.totals.errors)
          ? el('small', {}, `${s.totals.no_subs || 0} without usable subtitles, ${s.totals.errors || 0} errors`) : null),
      el('div', { class: 'stat' }, el('h3', {}, 'Storage'),
        el('p', {}, `DB ${fmtBytes(s.disk.database)} · thumbs ${fmtBytes(s.disk.thumbs)} · cache ${fmtBytes(s.disk.cache)}`),
        el('small', {}, s.settings.pregenerate_thumbs
          ? 'Thumbnails are pregenerated during indexing.'
          : `Frames render on demand (cache cap ${fmtBytes(s.settings.cache_max_mb * 1e6)}). Pregenerating every thumbnail would take about ${fmtBytes((s.totals.lines || 0) * avgThumb)}.`)));
  }

  async function refreshShows() {
    try { shows = await getJSON('/api/admin/shows'); } catch (err) {
      libraries.replaceChildren(message('Could not load shows', err.message));
      return;
    }
    drawShows();
  }

  function drawShows() {
    const needle = filter.value.trim().toLowerCase();
    const groups = new Map();
    for (const s of shows) {
      if (needle && !s.title.toLowerCase().includes(needle)) continue;
      const lib = s.library_title || 'TV';
      if (!groups.has(lib)) groups.set(lib, []);
      groups.get(lib).push(s);
    }
    if (!shows.length) {
      libraries.replaceChildren(message('No TV shows yet', 'Once Quotarr can reach Plex, every show in your TV libraries is listed here.'));
      return;
    }
    libraries.replaceChildren(...[...groups].map(([lib, list]) => {
      list.sort((a, b) => (b.enabled - a.enabled) || a.title.localeCompare(b.title));
      return el('section', { class: 'library' }, el('h2', {}, lib, el('small', {}, `${list.length} shows`)),
        el('ul', { class: 'show-list' }, list.map(showRow)));
    }));
  }

  function showRow(s) {
    const pct = s.known ? Math.round((100 * (s.indexed || 0)) / s.known) : 0;
    const toggle = el('input', { type: 'checkbox', class: 'switch', 'aria-label': `Index ${s.title}` });
    toggle.checked = !!s.enabled;
    toggle.addEventListener('change', async () => {
      s.enabled = toggle.checked ? 1 : 0;
      await postJSON(`/api/admin/shows/${s.id}`, { enabled: toggle.checked }).catch((err) => alert(err.message));
      showsCache = null;
      refreshStatus();
      drawShows();
    });
    const details = el('div', { class: 'episodes' });
    const item = el('li', { class: `show ${s.enabled ? 'enabled' : ''}` },
      el('label', { class: 'show-main' }, toggle,
        el('img', { src: s.poster, alt: '', loading: 'lazy', onerror: (e) => e.target.classList.add('noposter') }),
        el('span', { class: 'show-title' }, s.title, s.year ? el('small', {}, ` (${s.year})`) : null,
          el('small', { class: 'sub' }, `${s.leaf_count} episodes in Plex`))),
      s.enabled || s.lines ? el('div', { class: 'show-progress' },
        el('span', { class: 'progress' }, el('span', { style: `width:${pct}%` })),
        el('small', {}, `${s.indexed || 0}/${s.known || s.leaf_count} indexed · ${s.lines.toLocaleString()} lines`),
        s.no_subs ? el('span', { class: 'badge warn' }, `${s.no_subs} no subs`) : null,
        s.errors ? el('span', { class: 'badge bad' }, `${s.errors} errors`) : null) : el('div'),
      el('div', { class: 'show-actions' },
        s.known ? el('button', { class: 'btn small ghost', type: 'button', onclick: () => {
          if (open.has(s.id)) open.delete(s.id); else open.add(s.id);
          drawShows();
        } }, open.has(s.id) ? 'Hide episodes' : 'Episodes') : null,
        s.enabled ? el('button', { class: 'btn small ghost', type: 'button', title: 'Re-read every subtitle',
          onclick: async () => { await postJSON(`/api/admin/shows/${s.id}/reindex`); refreshStatus(); } }, 'Reindex') : null,
        s.lines ? el('button', { class: 'btn small ghost danger', type: 'button', title: 'Delete this show\'s index and thumbnails',
          onclick: async () => {
            if (!confirm(`Delete the index and thumbnails for ${s.title}?`)) return;
            await postJSON(`/api/admin/shows/${s.id}/purge`);
            showsCache = null;
            refreshShows();
          } }, 'Purge') : null),
      details);
    if (open.has(s.id)) loadEpisodes(s, details);
    return item;
  }

  async function loadEpisodes(s, box) {
    box.replaceChildren(loading());
    const eps = await getJSON(`/api/admin/shows/${s.id}/episodes`).catch(() => []);
    box.replaceChildren(el('table', {},
      el('thead', {}, el('tr', {}, el('th', {}, 'Ep'), el('th', {}, 'Title'), el('th', {}, 'Status'), el('th', {}, 'Details'))),
      el('tbody', {}, eps.map((e) => el('tr', {},
        el('td', {}, e.code),
        el('td', {}, e.title || ''),
        el('td', {}, el('span', { class: `badge ${e.status === 'indexed' ? 'ok' : e.status === 'pending' ? '' : e.status === 'error' ? 'bad' : 'warn'}` },
          STATUS_LABEL[e.status] || e.status), e.hdr ? el('span', { class: 'badge' }, 'HDR') : null),
        el('td', { class: 'detail' }, e.status === 'indexed' ? `${e.line_count} lines from ${e.sub_source}` : (e.error || '')))))));
  }

  filter.addEventListener('input', drawShows);
  view.replaceChildren(el('section', { class: 'admin' },
    el('div', { class: 'section-head' }, el('h1', {}, 'Libraries'), filter),
    statusGrid,
    el('p', { class: 'hint' }, 'Switch on the shows you want searchable. Quotarr only reads subtitles and grabs frames when they are needed, so turning a show on costs very little disk.'),
    libraries));
  await Promise.all([refreshStatus(), refreshShows()]);

  let ticks = 0;
  const timer = setInterval(async () => {
    ticks += 1;
    await refreshStatus();
    if (busy || ticks % 5 === 0) refreshShows();
  }, 3000);
  cleanup = () => clearInterval(timer);
}

route();
