// StealthReader 阅读页（PC / 手机）
// 数据全部走同源的 /v1/*：登录一次存 HttpOnly Cookie，之后 fetch 自动带上。
// 位置用"第几个字"（pos）+ 总字数（total），跟桌面端的进度是同一套坐标。

import './vendor/foliate-js/view.js'

const $ = (sel) => document.querySelector(sel)
const ts = () => Date.now() / 1000
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const fmt = (n) => (Number(n) || 0).toLocaleString('zh-CN')

const DEVICE = ('web-' + String(navigator.platform || 'browser').toLowerCase().replace(/[^a-z0-9]+/g, ''))
  .slice(0, 24)

const SERIF = `'Songti SC','SimSun','Noto Serif CJK SC','Source Han Serif SC',serif`
const SANS = `'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Helvetica Neue',sans-serif`

const THEMES = {
  light: { text_color: 'rgba(27, 27, 27, 255)', bg_color: 'rgba(251, 248, 242, 255)' },
  sepia: { text_color: 'rgba(75, 63, 47, 255)', bg_color: 'rgba(244, 236, 216, 255)' },
  dark: { text_color: 'rgba(201, 201, 201, 255)', bg_color: 'rgba(20, 22, 26, 255)' },
}

const S = {
  books: [], values: {}, clock: {}, book: null, view: null,
  total: 0, pos: 0, timer: 0, dirty: false, applying: false, entered: false,
  flow: 'paginated', cols: 1,          // 版式：翻页/滚动、单栏/双栏（存在本机，不进云端设置）
  progressEtag: '', progressBooks: {}, // 关页面那一下只发一个 PUT，得先记着上次的 rev 和其他书
  syncEveryMs: 15 * 60 * 1000,         // 开着页面时每 15 分钟同步一次；关页面再同步一次
}

/* ── 网络 ─────────────────────────────────────────────── */

async function req(path, opt = {}) {
  const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...opt })
  if (res.status === 401) {
    show('login')
    throw new Error('unauthorized')
  }
  return res
}

async function getJSON(path) {
  const res = await req(path)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`${path} ${res.status}`)
  return { body: await res.json(), etag: res.headers.get('etag') || '' }
}

function toast(msg, ms = 2600) {
  const el = $('#toast')
  el.textContent = msg
  el.hidden = false
  clearTimeout(toast._t)
  toast._t = setTimeout(() => { el.hidden = true }, ms)
}

/* ── 登录 ─────────────────────────────────────────────── */

async function boot() {
  const ping = await fetch('/v1/ping', { credentials: 'same-origin' })
  if (S.entered) return                    // 用户手快，已经自己登录进去了
  if (ping.status === 401) return show('login')
  if (!ping.ok) { toast('同步服务连不上：' + ping.status); return show('login') }
  await enterApp()
}

async function enterApp() {
  await loadSettings()
  await loadShelf()
  S.entered = true
  show('shelf-pane')
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const err = $('#login-err')
  err.textContent = ''
  const token = $('#token').value.trim()
  if (!token) return
  const res = await fetch('/v1/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ token }),
  })
  if (!res.ok) { err.textContent = 'token 不对，或者服务端没配'; return }
  $('#token').value = ''
  try { await enterApp() } catch (e2) { err.textContent = String(e2.message || e2) }
})

$('#logout').addEventListener('click', async () => {
  await fetch('/v1/logout', { method: 'POST', credentials: 'same-origin' })
  show('login')
})

function show(which) {
  for (const id of ['login', 'shelf-pane', 'reader-pane']) $('#' + id).hidden = id !== which
  document.body.dataset.pane = which
}

/* ── 书架 ─────────────────────────────────────────────── */

async function loadShelf() {
  const got = await getJSON('/v1/shelf')
  S.books = (got && got.body && got.body.books) || []
  const list = $('#books')
  list.textContent = ''
  $('#shelf-meta').textContent = `${S.books.length} 本`
  $('#shelf-note').textContent = S.books.length ? '' : '云端还没有书：桌面端「设置 → 云端同步」里传一本上来'
  for (const b of S.books) {
    const li = document.createElement('li')
    li.className = 'book'
    const pct = b.total > 0 ? Math.min(100, Math.round((b.pos / b.total) * 100)) : 0
    li.innerHTML = `
      <div class="b-title"></div>
      <div class="b-meta"><span class="b-pct"></span><span class="b-size"></span></div>
      <div class="b-bar"><i></i></div>`
    li.querySelector('.b-title').textContent = b.title || b.name || b.uid
    li.querySelector('.b-pct').textContent = pct ? `读 ${pct}%` : '未读'
    li.querySelector('.b-size').textContent = b.size ? `${(b.size / 1048576).toFixed(1)} MB` : ''
    li.querySelector('.b-bar i').style.width = pct + '%'
    li.addEventListener('click', () => openBook(b))
    list.appendChild(li)
  }
}

/* ── 打开一本书（只拉这一本） ──────────────────────────── */

async function openBook(book) {
  show('reader-pane')
  $('#pos').textContent = '打开中…'
  const view = $('#view')
  S.book = book
  S.view = view
  S.pos = book.pos || 0
  S.total = book.total || 0
  S.dirty = false

  // 桌面端抽全文时算的章节表：拿到就能按"第几个字"精确定位，也能把 total 校准成桌面端那套
  try {
    const idx = await getJSON(`/v1/lite/${book.uid}/index.json`)
    if (idx && idx.body && Number(idx.body.total) > 0) {
      S.total = Number(idx.body.total)
      S.index = idx.body
    }
  } catch (e) { /* 没有就算了，用进度里的 total */ }

  view.addEventListener('relocate', onRelocate)
  view.addEventListener('error', (e) => toast('书打不开：' + (e.detail?.message || e.detail || '')))

  try {
    await view.open(`/v1/blob/${book.sha}`)
  } catch (e) {
    $('#pos').textContent = '—'
    toast('这本书打不开：' + (e.message || e))
    return
  }
  applyStyles()
  applyLayout()

  // 记一下进度文档的 rev 和别的书：关页面时只发一个 PUT，没工夫再 GET
  try {
    const got = await getJSON('/v1/state/progress')
    if (got) {
      S.progressEtag = got.etag || ''
      S.progressBooks = (got.body && got.body.books && typeof got.body.books === 'object')
        ? { ...got.body.books } : {}
    }
  } catch (e) { /* 拿不到就退回"先 GET 再 PUT"的老路 */ }

  if (S.pos > 0 && S.total > 0) {
    const frac = clamp(S.pos / S.total, 0, 1)
    try { await view.goToFraction(frac) } catch (e) { /* 从头开始读也不会丢进度 */ }
  }
  renderTOC()
  $('#pos').textContent = S.total ? `0%  ·  0 / ${fmt(S.total)} 字` : '—'
}

/* ── 位置：字符偏移 ↔ 进度 ─────────────────────────────── */

function onRelocate(e) {
  const loc = (e && e.detail) || {}
  const frac = Number(loc.fraction) || 0
  S.pos = Math.round(frac * (S.total || 0))
  S.dirty = true
  $('#pos').textContent = S.total
    ? `${Math.round(frac * 100)}%  ·  ${fmt(S.pos)} / ${fmt(S.total)} 字`
    : `${Math.round(frac * 100)}%`
  // 不做实时同步：这里只记"有改动"，等 15 分钟的定时器或者关页面时再推
}

function progressBody() {
  return { v: 1, schema: 1, updated_at: ts(), device: DEVICE, books: {} }
}

function rowFor(pos, total, old = {}) {
  const row = {
    title: S.book.title || S.book.name || '',
    pos: Math.max(0, Math.round(pos)),
    total: Math.max(Math.round(total) || 0, Math.round(Number(old.total) || 0)),
    at: ts(),
    dev: DEVICE,
    hidden: !!old.hidden,
  }
  if (old.stale) row.stale = true
  return row
}

// 只改"我正在读的这一行"，其他行原样带回去；If-Match 冲突就重来（最多 3 次）。
// 桌面端的合并规则是 at 最新的一行赢，所以这样写和它合并出来的结果等价。
// opt.keepalive：关页面/切后台那一下用。只发一个 PUT（GET 多半来不及），rev 用打开书时记的。
async function pushProgress(force = false, opt = {}) {
  if (!S.book || !(force || S.dirty)) return false
  const pos = S.pos
  const total = S.total

  if (opt.keepalive && S.progressEtag) {
    S.dirty = false
    const books = { ...(S.progressBooks || {}) }
    books[S.book.uid] = rowFor(pos, total, books[S.book.uid] || {})
    const body = { v: 1, schema: 1, updated_at: ts(), device: DEVICE, books }
    const put = await req('/v1/state/progress', {
      method: 'PUT',
      headers: { 'content-type': 'application/json', 'if-match': S.progressEtag },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => null)
    if (put && put.ok) {
      S.progressBooks = books
      S.progressEtag = put.headers.get('etag') || S.progressEtag
      return true
    }
    S.dirty = true            // 没成：本地书架里还留着 pos，下次打开再推
    return false
  }

  S.dirty = false
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const got = await getJSON('/v1/state/progress').catch(() => null)
    if (!got) {                        // 远端还没有这个对象：新建
      const body = progressBody()
      body.books[S.book.uid] = rowFor(pos, total)
      const put = await req('/v1/state/progress', {
        method: 'PUT',
        headers: { 'content-type': 'application/json', 'x-create': '1' },
        body: JSON.stringify(body),
      })
      if (put.ok) {
        S.progressBooks = { ...body.books }
        S.progressEtag = put.headers.get('etag') || S.progressEtag
        return true
      }
      continue
    }
    const remote = got.body || {}
    S.progressEtag = got.etag || ''
    const books = (remote.books && typeof remote.books === 'object') ? remote.books : {}
    books[S.book.uid] = rowFor(pos, total, books[S.book.uid] || {})
    const body = {
      v: remote.v || 1,
      schema: Math.max(Number(remote.schema) || 1, 1),
      updated_at: ts(),
      device: DEVICE,
      books,
    }
    const put = await req('/v1/state/progress', {
      method: 'PUT',
      headers: { 'content-type': 'application/json', 'if-match': got.etag },
      body: JSON.stringify(body),
    })
    if (put.ok) {
      S.progressBooks = { ...books }
      S.progressEtag = put.headers.get('etag') || S.progressEtag
      return true
    }
    if (put.status !== 412 && put.status !== 428) return false
  }
  return false
}

/* ── 排版设置（和桌面端同一批键） ───────────────────────── */

async function loadSettings() {
  const got = await getJSON('/v1/state/settings').catch(() => null)
  const remote = (got && got.body) || {}
  S.values = { ...(remote.values || {}) }
  S.clock = { ...(remote.clock || {}) }
  paintValues()
}

async function saveSettings(patch) {
  Object.assign(S.values, patch)
  paintValues()
  applyStyles()
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const got = await getJSON('/v1/state/settings').catch(() => null)
    const remote = (got && got.body) || {}
    const values = { ...(remote.values || {}), ...patch }
    const clock = { ...(remote.clock || {}) }
    for (const k of Object.keys(patch)) clock[k] = ts()
    const body = { v: remote.v || 1, updated_at: ts(), device: DEVICE, values, clock }
    const headers = { 'content-type': 'application/json' }
    if (got) headers['if-match'] = got.etag
    else headers['x-create'] = '1'
    const put = await req('/v1/state/settings', { method: 'PUT', headers, body: JSON.stringify(body) })
    if (put.ok) { S.clock = clock; return true }
    if (put.status !== 412 && put.status !== 428) break
  }
  $('#tools-note').textContent = '设置没存上（下次改再试）'
  return false
}

/* ── 样式 ─────────────────────────────────────────────── */

function num(v, fallback) {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

/** 桌面端存的是 Qt 的 rgba(r, g, b, 0-255)，CSS 要 0-1 的 alpha。 */
function cssColor(v, fallback) {
  const m = /^rgba?\(([^)]+)\)$/i.exec(String(v || '').trim())
  if (!m) return fallback
  const parts = m[1].split(',').map((x) => x.trim())
  if (parts.length < 3) return fallback
  const [r, g, b] = parts.slice(0, 3).map((x) => clamp(Math.round(num(x, 0)), 0, 255))
  let a = parts.length > 3 ? num(parts[3], 1) : 1
  if (a > 1) a = a / 255
  return `rgba(${r}, ${g}, ${b}, ${clamp(a, 0, 1)})`
}

function current() {
  const v = S.values
  const size = clamp(num(v.font_size, 19), 10, 48)
  // 桌面端的 100% 是"Qt 比例行距"，落在 CSS 里大约相当于 1.35 倍字号；这里按同一比例换
  const lh = clamp(1.35 * num(v.line_spacing, 100) / 100, 1.1, 2.8)
  const ps = clamp(num(v.paragraph_spacing, 0), 0, 40)
  const mg = clamp(num(v.page_margin_chars, 1), 0, 8)
  const ind = clamp(num(v.first_line_indent, 2), 0, 6)
  const family = String(v.font_family || '')
  const serif = /simsun|song|serif|宋/i.test(family) && !/yahei|hei|sans|黑/i.test(family)
  return {
    size, lh, ps, mg, ind,
    serif,
    ink: cssColor(v.text_color, '#1b1b1b'),
    paper: cssColor(v.bg_color, '#fbf8f2'),
  }
}

function bookCSS(c) {
  return `
    html, body { background: ${c.paper} !important; color: ${c.ink} !important; }
    body {
      font-family: ${c.serif ? SERIF : SANS} !important;
      font-size: ${c.size}px !important;
      line-height: ${c.lh} !important;
      margin: 0 !important; padding: 0 !important;
      -webkit-text-size-adjust: 100%;
    }
    p, li, blockquote, dd, div {
      font-size: inherit; line-height: ${c.lh};
      margin: 0 0 ${c.ps}px 0 !important;
      text-align: justify; text-justify: inter-ideograph;
    }
    p { text-indent: ${c.ind}em; }
    h1, h2, h3, h4 { line-height: 1.4; margin: 0.6em 0 0.4em; }
    img, svg { max-width: 100% !important; height: auto !important; }
    a { color: inherit; }
    ::selection { background: rgba(120, 160, 255, 0.35); }
  `
}

function applyStyles() {
  const c = current()
  document.documentElement.style.setProperty('--paper', c.paper)
  document.documentElement.style.setProperty('--ink', c.ink)
  document.documentElement.style.setProperty('--pad', `${Math.round(c.mg * c.size)}px`)
  const wrap = $('#view-wrap')
  if (wrap) wrap.style.padding = `${Math.round(c.mg * c.size * 0.6)}px ${Math.round(c.mg * c.size)}px`
  if (S.view && S.view.renderer && typeof S.view.renderer.setStyles === 'function') {
    S.view.renderer.setStyles(bookCSS(c))
  }
}

function paintValues() {
  const c = current()
  $('#v-font').textContent = String(Math.round(c.size))
  $('#v-lh').textContent = c.lh.toFixed(2)
  $('#v-ps').textContent = String(Math.round(c.ps))
  $('#v-mg').textContent = c.mg.toFixed(1)
  $('#v-ind').textContent = c.ind.toFixed(1)
  $('#tools-note').textContent = `字号/行距/段距/边距/缩进/字体/配色都会同步给桌面端（${DEVICE}）`
}

/* ── 版式：左右翻页 / 上下滚动、单栏 / 双栏 ─────────────── */

const LAYOUT_KEY = 'sr.layout.v1'

function loadLayout() {
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null') } catch (e) { saved = null }
  if (saved && typeof saved === 'object') {
    S.flow = saved.flow === 'scrolled' ? 'scrolled' : 'paginated'
    S.cols = Number(saved.cols) === 2 ? 2 : 1
  }
}

function saveLayout() {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ flow: S.flow, cols: S.cols }))
  } catch (e) { /* 隐私模式下存不了，就这次会话内有效 */ }
}

/** 滚动模式滚 dy 像素：走 foliate 自己的 scrollToAnchor
    （它的滚动盒子挂在 closed shadow root 里，外部拿不到；start/viewSize 是公开的 getter） */
function scrollStep(dy) {
  const r = S.view && S.view.renderer
  if (!r || typeof r.scrollToAnchor !== 'function') return false
  const size = Number(r.viewSize) || 0
  if (!size) return false
  r.scrollToAnchor(clamp((Number(r.start) + dy) / size, 0, 1))
  return true
}

function applyLayout() {
  const r = S.view && S.view.renderer
  if (r) {
    r.setAttribute('flow', S.flow)
    r.setAttribute('max-column-count', String(S.cols))
  }
  const wrap = $('#view-wrap')
  if (wrap) wrap.dataset.flow = S.flow
  for (const b of document.querySelectorAll('#tools [data-act]')) {
    const a = b.dataset.act
    if (a === 'flow-page') b.classList.toggle('on', S.flow === 'paginated')
    else if (a === 'flow-scroll') b.classList.toggle('on', S.flow === 'scrolled')
    else if (a === 'cols-1') b.classList.toggle('on', S.cols === 1)
    else if (a === 'cols-2') b.classList.toggle('on', S.cols === 2)
  }
}

function setLayout(flow, cols) {
  if (flow) S.flow = flow
  if (cols) S.cols = cols
  saveLayout()
  applyLayout()
}

/* ── 设置面板 ─────────────────────────────────────────── */

const STEP = {
  'font-': () => ({ font_size: clamp(Math.round(num(S.values.font_size, 19)) - 1, 10, 48) }),
  'font+': () => ({ font_size: clamp(Math.round(num(S.values.font_size, 19)) + 1, 10, 48) }),
  'lh-': () => ({ line_spacing: clamp(Math.round(num(S.values.line_spacing, 100)) - 10, 90, 300) }),
  'lh+': () => ({ line_spacing: clamp(Math.round(num(S.values.line_spacing, 100)) + 10, 90, 300) }),
  'ps-': () => ({ paragraph_spacing: clamp(Math.round(num(S.values.paragraph_spacing, 0)) - 2, 0, 40) }),
  'ps+': () => ({ paragraph_spacing: clamp(Math.round(num(S.values.paragraph_spacing, 0)) + 2, 0, 40) }),
  'mg-': () => ({ page_margin_chars: clamp(Math.round(num(S.values.page_margin_chars, 1) * 10 - 2) / 10, 0, 8) }),
  'mg+': () => ({ page_margin_chars: clamp(Math.round(num(S.values.page_margin_chars, 1) * 10 + 2) / 10, 0, 8) }),
  'ind-': () => ({ first_line_indent: clamp(Math.round(num(S.values.first_line_indent, 2) * 10 - 2) / 10, 0, 6) }),
  'ind+': () => ({ first_line_indent: clamp(Math.round(num(S.values.first_line_indent, 2) * 10 + 2) / 10, 0, 6) }),
  'font-serif': () => ({ font_family: 'SimSun' }),
  'font-sans': () => ({ font_family: 'Microsoft YaHei' }),
  'theme-light': () => THEMES.light,
  'theme-sepia': () => THEMES.sepia,
  'theme-dark': () => THEMES.dark,
}

$('#tools').addEventListener('click', (e) => {
  const act = e.target && e.target.dataset && e.target.dataset.act
  if (!act) return
  // 版式是本机偏好，不进云端设置（桌面端的阅读窗有自己的一套）
  if (act === 'flow-page') return setLayout('paginated')
  if (act === 'flow-scroll') return setLayout('scrolled')
  if (act === 'cols-1') return setLayout(null, 1)
  if (act === 'cols-2') return setLayout(null, 2)
  if (!STEP[act]) return
  const patch = STEP[act]()
  saveSettings(patch)
})

$('#open-tools').addEventListener('click', () => { $('#tools').hidden = !$('#tools').hidden })
$('#back').addEventListener('click', async () => {
  await pushProgress(true)
  $('#tools').hidden = true
  $('#toc-panel').hidden = true
  await loadShelf()
  show('shelf-pane')
})

/* ── 目录 ─────────────────────────────────────────────── */

function renderTOC() {
  const list = $('#toc-list')
  list.textContent = ''
  const toc = (S.view && S.view.book && S.view.book.toc) || []
  const flat = []
  const walk = (items, depth) => {
    for (const it of items || []) {
      flat.push({ href: it.href, label: it.label || '(未命名)', depth })
      walk(it.subitems, depth + 1)
    }
  }
  walk(toc, 0)
  for (const it of flat) {
    const li = document.createElement('li')
    li.className = 'toc-item'
    li.style.paddingLeft = 12 + it.depth * 16 + 'px'
    li.textContent = it.label
    li.addEventListener('click', async () => {
      $('#toc-panel').hidden = true
      try { await S.view.goTo(it.href) } catch (e) { toast('跳不过去：' + it.label) }
    })
    list.appendChild(li)
  }
  if (!flat.length) {
    const li = document.createElement('li')
    li.className = 'toc-item'
    li.textContent = '这本书没有目录'
    list.appendChild(li)
  }
}

$('#toc').addEventListener('click', () => { $('#toc-panel').hidden = !$('#toc-panel').hidden })
$('#toc-close').addEventListener('click', () => { $('#toc-panel').hidden = true })

/* ── 翻页、滚动与快捷键 ───────────────────────────────── */

function turn(dir) {
  if (!S.view) return
  if (dir > 0) S.view.next()
  else S.view.prev()
}

/** 滚轮：翻页模式下一页一动；滚动模式往下滚 */
function onWheel(e) {
  if (S.flow === 'scrolled') {
    const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerHeight : 1
    if (!scrollStep(e.deltaY * px)) return
    e.preventDefault()
    return
  }
  if (!e.deltaY && !e.deltaX) return
  e.preventDefault()
  const now = Date.now()
  if (now - (onWheel.last || 0) < 240) return   // 触控板滑一下会连发几十个事件
  onWheel.last = now
  const d = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX
  if (Math.abs(d) < 4) return
  turn(d > 0 ? 1 : -1)
}

/** 点左三分之一往回、点右三分之一往前（滚动模式下点哪儿都不翻页） */
function onTapTurn(e) {
  if (document.body.dataset.pane !== 'reader-pane') return
  if (S.flow === 'scrolled') return
  if (e.target && e.target.closest && e.target.closest('a[href]')) return
  const w = window.innerWidth
  const x = e.clientX
  if (x < w * 0.3) return turn(-1)
  if (x > w * 0.7) return turn(1)
}

// 书的内容在 iframe 里，滚轮/点击到不了外层：挂到每一篇加载出来的 doc 上
const hookedDocs = new WeakSet()
function hookDoc(doc) {
  if (!doc || hookedDocs.has(doc)) return
  hookedDocs.add(doc)
  doc.addEventListener('wheel', onWheel, { passive: false })
  doc.addEventListener('click', onTapTurn)
}
$('#view').addEventListener('load', (e) => hookDoc(e.detail && e.detail.doc))

document.addEventListener('keydown', (e) => {
  if (document.body.dataset.pane !== 'reader-pane') return
  if (e.target && /input|textarea/i.test(e.target.tagName)) return
  const k = e.key
  const screen = () => window.innerHeight * 0.9
  const scrollOnly = (dy) => (S.flow === 'scrolled' ? scrollStep(dy) : false)
  if (k === 'ArrowRight' || k === 'j') { e.preventDefault(); turn(1) }
  else if (k === 'ArrowLeft' || k === 'k') { e.preventDefault(); turn(-1) }
  else if (k === 'ArrowDown') { e.preventDefault(); if (!scrollOnly(screen() * 0.3)) turn(1) }
  else if (k === 'ArrowUp') { e.preventDefault(); if (!scrollOnly(-screen() * 0.3)) turn(-1) }
  else if (k === 'PageDown' || k === ' ') { e.preventDefault(); if (!scrollOnly(screen())) turn(1) }
  else if (k === 'PageUp') { e.preventDefault(); if (!scrollOnly(-screen())) turn(-1) }
  else if (k === 'Escape') { $('#tools').hidden = true; $('#toc-panel').hidden = true }
})

$('#view-wrap').addEventListener('click', onTapTurn)
$('#view-wrap').addEventListener('wheel', onWheel, { passive: false })

/* ── 同步节奏：不做实时 ───────────────────────────────────
   开着页面：每 15 分钟一次，只有翻动过才发。
   关页面 / 切到后台：立刻推一次（只发一个 PUT，带 keepalive，不然页面一关请求就没了）。 */

function flushProgress() {
  if (!S.book || !S.dirty) return
  pushProgress(true, { keepalive: true }).catch(() => { /* 关页面了，失败也没处提示 */ })
}

setInterval(() => {
  if (S.dirty) pushProgress(false).catch(() => {})
}, S.syncEveryMs)

document.addEventListener('visibilitychange', () => { if (document.hidden) flushProgress() })
window.addEventListener('pagehide', flushProgress)
window.addEventListener('beforeunload', flushProgress)

/* ── 起 ─────────────────────────────────────────────── */

loadLayout()

boot().catch((e) => {
  console.error(e)
  toast('启动失败：' + (e.message || e))
  show('login')
})
