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

// 字体：都是系统里就有的，按 苹果 / Windows / Android 的顺序挑到一个算一个
const FACES = {
  serif: `'Songti SC','SimSun','Noto Serif CJK SC','Source Han Serif SC',serif`,
  sans: `'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Helvetica Neue',sans-serif`,
  kai: `'Kaiti SC','KaiTi','STKaiti','Noto Serif CJK SC',serif`,
  round: `'Yuanti SC','YouYuan','Hiragino Maru Gothic ProN','Microsoft YaHei',sans-serif`,
}

// 配色：纸色 + 墨色成对给，别让正文和底色分家
const PAPERS = {
  white: { bg: '#fbf8f2', ink: '#1b1b1b' },
  sepia: { bg: '#f4ecd8', ink: '#4b3f2f' },
  green: { bg: '#e6efd8', ink: '#22301d' },
  grey: { bg: '#eef0f2', ink: '#23262b' },
  blue: { bg: '#1b2430', ink: '#c9d4e2' },
  night: { bg: '#14161a', ink: '#c9c9c9' },
  black: { bg: '#000000', ink: '#a6a6a6' },
}

const SET_KEY = 'sr.settings.v1'
const DEFAULT_SET = {
  font_size: 19, line_spacing: 100, paragraph_spacing: 0,
  page_margin_chars: 1, first_line_indent: 2,
  face: 'sans', paper: 'white',
}

const S = {
  books: [], values: {}, book: null, view: null,
  total: 0, pos: 0, timer: 0, dirty: false, applying: false, entered: false,
  flow: 'paginated', cols: 1,          // 版式：翻页/滚动、单栏/双栏（存在本机）
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
  loadSettings()
  await loadShelf()
  S.entered = true
  show('shelf-pane')
  // 上次没推上去的统计（断网 / 关页面被掐）：进来先补推一次
  if (STAT.pending.length) flushStats(false).catch(() => {})
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
  await loadStatsLine()
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
  statOpen(book, S.pos)               // 从这一本开始记阅读统计
  claimTab()                          // 这一页在读了，别让别的标签也记

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
  statMove(S.pos)                     // 统计只在本机攒着，跟"推不推"没关系
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

/* ── 排版设置：只在本机（localStorage），不进云端 ──────────
   桌面端那套设置是它自己的（阅读窗有独立的分栏/字体逻辑），网页改了去覆盖它只会互相打架。
   这里存的东西跟书无关，换机器就是默认值——手机和电脑各调各的反而顺手。 */

function loadSettings() {
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(SET_KEY) || 'null') } catch (e) { saved = null }
  const set = (saved && typeof saved === 'object') ? saved : {}
  S.values = { ...DEFAULT_SET }
  for (const k of Object.keys(DEFAULT_SET)) {
    if (set[k] !== undefined && set[k] !== null) S.values[k] = set[k]
  }
  if (!FACES[S.values.face]) S.values.face = DEFAULT_SET.face
  if (!PAPERS[S.values.paper]) S.values.paper = DEFAULT_SET.paper
  paintValues()
}

function saveSettings(patch) {
  Object.assign(S.values, patch)
  try { localStorage.setItem(SET_KEY, JSON.stringify(S.values)) } catch (e) { /* 隐私模式：这次会话内有效 */ }
  paintValues()
  applyStyles()
}

/* ── 样式 ─────────────────────────────────────────────── */

function num(v, fallback) {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

/** 桌面端存的是 Qt 的 rgba(r, g, b, 0-255)；老版本设置里可能是这个形状，认出来就转一下。 */
function cssColor(v, fallback) {
  const s = String(v || '').trim()
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(s)) return s
  const m = /^rgba?\(([^)]+)\)$/i.exec(s)
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
  const paper = PAPERS[v.paper] || PAPERS.white
  return {
    size, lh, ps, mg, ind,
    face: FACES[v.face] || FACES.sans,
    ink: cssColor(v.text_color, paper.ink),
    paper: cssColor(v.bg_color, paper.bg),
  }
}

function bookCSS(c) {
  return `
    html, body { background: ${c.paper} !important; color: ${c.ink} !important; }
    body {
      font-family: ${c.face} !important;
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
  const put = (sel, text) => { const el = $(sel); if (el) el.textContent = text }
  put('#v-font', String(Math.round(c.size)))
  put('#v-lh', c.lh.toFixed(2))
  put('#v-ps', String(Math.round(c.ps)))
  put('#v-mg', c.mg.toFixed(1))
  put('#v-ind', c.ind.toFixed(1))
  for (const b of document.querySelectorAll('#tools [data-act]')) {
    const a = b.dataset.act || ''
    if (a.startsWith('face-')) b.classList.toggle('on', a.slice(5) === S.values.face)
    else if (a.startsWith('paper-')) b.classList.toggle('on', a.slice(6) === S.values.paper)
  }
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
}

$('#tools').addEventListener('click', (e) => {
  const act = e.target && e.target.dataset && e.target.dataset.act
  if (!act) return
  // 版式是本机偏好（桌面端的阅读窗有自己的一套）
  if (act === 'flow-page') return setLayout('paginated')
  if (act === 'flow-scroll') return setLayout('scrolled')
  if (act === 'cols-1') return setLayout(null, 1)
  if (act === 'cols-2') return setLayout(null, 2)
  if (act.startsWith('face-')) return saveSettings({ face: act.slice(5) })
  if (act.startsWith('paper-')) return saveSettings({ paper: act.slice(6) })
  if (!STEP[act]) return
  saveSettings(STEP[act]())
})

$('#open-tools').addEventListener('click', () => { $('#tools').hidden = !$('#tools').hidden })
$('#back').addEventListener('click', async () => {
  await pushProgress(true)
  await flushStats(false).catch(() => {})
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

/* ── 阅读统计：本机算，跟着同步那一轮推上去 ─────────────────
   口径跟桌面端 reading_stats 对齐，别让两边的账对不上：
   · 5 秒一拍记时长，3 分钟没有任何翻动就先不计（可能人走了）；
   · 一次翻动往前挪超过"一页半"就当成跳转（点目录/点脚注），那一拍不算字；
     一页多少字不在网页这边硬算，用自己最近的翻页量取中位数当"一页"；
   · 一场不到 1 分钟不记；一场平均速度不超过 1200 字/分（20 字/秒）。
   推上去的是"会话"，身份是 (书 uid, 会话开始时刻)：桌面端 merge_stats 按这个身份
   取并集再重算日账/书账，所以两边读的字数会加在一起，而不是互相覆盖。 */

const TICK_SEC = 5
const IDLE_SEC = 180
const MIN_SESSION_SEC = 60
const MAX_CHARS_PER_SEC = 20
const JUMP_PAGE_FACTOR = 1.6
const MIN_JUMP_CHARS = 400

const STAT = {
  uid: '', title: '', start: 0, end: 0, sec: 0, chars: 0,
  lastPos: 0, lastMove: 0,
  pages: [], page: 800,        // 一页大概多少字：自己翻出来的中位数，先按 800 起
  pending: [],                 // 还没推上去的会话
}

// 待推队列落在本机：推不上去（断网 / 412 / 关页面被掐）也不会丢，下次打开接着推。
// 会话身份是 (书, 开始时刻)，重复推同一个身份不会记两遍 —— 所以"重试"是安全的。
const PEND_KEY = 'sr.pendingstats.v1'
const OWNER_KEY = 'sr.statowner.v1'      // 同一浏览器开两个标签时，只让一个记账
const TAB_ID = Math.random().toString(36).slice(2, 10)

function loadPending() {
  let raw = null
  try { raw = JSON.parse(localStorage.getItem(PEND_KEY) || 'null') } catch (e) { raw = null }
  if (Array.isArray(raw)) {
    STAT.pending = raw.filter((s) => s && s.k && Number(s.start) > 0).slice(-60)
  }
}

function savePending() {
  try {
    if (STAT.pending.length) localStorage.setItem(PEND_KEY, JSON.stringify(STAT.pending.slice(-60)))
    else localStorage.removeItem(PEND_KEY)
  } catch (e) { /* 隐私模式：这次会话内先攒着 */ }
}

function claimTab() {
  try { localStorage.setItem(OWNER_KEY, JSON.stringify({ id: TAB_ID, at: ts() })) } catch (e) { /* 忽略 */ }
}

/** 该不该由这个标签记账：别人刚活动过就不记，免得同一个浏览器开两个标签算两遍 */
function isOwner() {
  try {
    const o = JSON.parse(localStorage.getItem(OWNER_KEY) || 'null')
    if (!o || !o.id || o.id === TAB_ID) return true
    return (ts() - Number(o.at || 0)) > 90
  } catch (e) { return true }
}

function statOpen(book, pos) {
  statCommit()                 // 上一本先收场
  STAT.uid = String(book.uid || '')
  STAT.title = book.title || book.name || STAT.uid
  STAT.start = ts()
  STAT.end = STAT.start
  STAT.sec = 0
  STAT.chars = 0
  STAT.lastPos = Math.max(0, Math.round(pos || 0))
  STAT.lastMove = STAT.start
}

/** 挪了一次位置：判断这算不算"读过去了一页" */
function statMove(pos) {
  if (!STAT.uid || !STAT.start) return
  const now = ts()
  const at = Math.max(0, Math.round(pos || 0))
  const delta = at - STAT.lastPos
  STAT.lastMove = now
  STAT.end = Math.max(STAT.end, now)
  if (delta <= 0) { STAT.lastPos = at; return }         // 往回翻，不算新读
  const limit = Math.max(MIN_JUMP_CHARS, Math.round(STAT.page * JUMP_PAGE_FACTOR))
  if (delta > limit) { STAT.lastPos = at; return }      // 跳转：眼睛没扫过，不算
  if (delta <= 6000 && delta >= 80) {                   // 顺手校准"一页多少字"
    STAT.pages.push(delta)
    if (STAT.pages.length > 24) STAT.pages.shift()
    const sorted = STAT.pages.slice().sort((a, b) => a - b)
    STAT.page = sorted[Math.floor(sorted.length / 2)] || STAT.page
  }
  STAT.chars += delta
  STAT.lastPos = at
}

setInterval(() => {
  if (!STAT.uid || !STAT.start) return
  if (document.visibilityState !== 'visible') return
  if (!isOwner()) return                                // 另一个标签正在记，别重复记
  const now = ts()
  if (now - STAT.lastMove > IDLE_SEC) return            // 三分钟没动，先不计时
  STAT.sec += TICK_SEC
  STAT.end = now
}, TICK_SEC * 1000)

/** 收场：把这一场（够 1 分钟才算）放进待推队列 */
function statCommit() {
  if (!STAT.uid || !STAT.start) { STAT.uid = ''; return }
  const sec = Math.round(STAT.sec)
  let chars = Math.round(STAT.chars)
  const cap = Math.round(sec * MAX_CHARS_PER_SEC)
  if (chars > cap) chars = cap
  if (sec >= MIN_SESSION_SEC) {
    STAT.pending.push({
      k: STAT.uid,
      title: STAT.title,
      start: Math.round(STAT.start * 1000) / 1000,
      end: Math.round(Math.max(STAT.end, STAT.start) * 1000) / 1000,
      sec,
      chars,
      gaps: [],
    })
    STAT.pending = STAT.pending.slice(-40)
  }
  STAT.uid = ''
  STAT.title = ''
  STAT.start = 0
  STAT.end = 0
  STAT.sec = 0
  STAT.chars = 0
  STAT.lastPos = 0
  STAT.lastMove = 0
  savePending()                 // 一收场就落盘：这时候关掉标签页也不会丢
}

function dayKey(d) {
  const t = d || new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`
}

const sessKey = (s) => `${s.k}|${Math.round(Number(s.start) * 1000) / 1000}`

/** 把待推的会话并进云端账本：先 GET、按身份去重、再整份 PUT（带 if-match）。
    推成功才从队列里删；推不上就留在本机（已落盘），下一轮或下次打开接着推——
    同样的会话身份重复推不会记两遍（合并端也按身份取并集）。 */
async function pushStats(keepalive) {
  if (!STAT.pending.length) return false
  if (pushStats.busy) return false
  pushStats.busy = true
  const mine = STAT.pending.slice()
  try {
    const got = await getJSON('/v1/state/stats').catch(() => null)
    const remote = (got && got.body) || {}
    const sessions = Array.isArray(remote.sessions) ? remote.sessions.slice() : []
    const seen = new Set(sessions.map(sessKey))
    for (const s of mine) {
      const id = sessKey(s)
      if (seen.has(id)) continue
      sessions.push(s)
      seen.add(id)
    }
    const body = {
      v: Math.max(Number(remote.v) || 1, 1),
      updated_at: ts(),
      device: DEVICE,
      sessions,
      days: (remote.days && typeof remote.days === 'object') ? remote.days : {},
      books: (remote.books && typeof remote.books === 'object') ? remote.books : {},
    }
    const headers = { 'content-type': 'application/json' }
    if (got && got.etag) headers['if-match'] = got.etag
    else headers['x-create'] = '1'
    const opt = { method: 'PUT', headers, body: JSON.stringify(body) }
    if (keepalive) opt.keepalive = true
    const put = await req('/v1/state/stats', opt).catch(() => null)
    if (!put || !put.ok) return false
    const done = new Set(mine.map(sessKey))
    STAT.pending = STAT.pending.filter((s) => !done.has(sessKey(s)))
    savePending()
    return true
  } finally {
    pushStats.busy = false
  }
}

/** 收场 + 推上去；restart 时（15 分钟那一轮）如果还开着书就接着记下一场 */
async function flushStats(keepalive, opt = {}) {
  const book = S.book
  statCommit()
  const ok = await pushStats(keepalive).catch(() => false)
  if (opt.restart && book && document.visibilityState === 'visible'
      && document.body.dataset.pane === 'reader-pane') {
    statOpen(book, S.pos)
  }
  return ok
}

async function loadStatsLine() {
  const el = $('#shelf-stats')
  if (!el) return
  const got = await getJSON('/v1/state/stats').catch(() => null)
  const days = (got && got.body && got.body.days) || {}
  const cell = days[dayKey()] || {}
  const sec = Number(cell.sec) || 0
  if (sec <= 0) { el.textContent = ''; return }
  const min = Math.max(1, Math.round(sec / 60))
  el.textContent = `今天 ${min} 分钟 · ${fmt(cell.chars)} 字`
}

/* ── 同步节奏：不做实时 ───────────────────────────────────
   开着页面：每 15 分钟一次（进度有变动才发、统计有会话才发）。
   关页面 / 切到后台：立刻推一次（带 keepalive，不然页面一关请求就没了）。 */

function flushProgress() {
  if (S.book && S.dirty) pushProgress(true, { keepalive: true }).catch(() => { /* 关页面了，失败也没处提示 */ })
  flushStats(true).catch(() => {})
}

setInterval(() => {
  if (S.dirty) pushProgress(false).catch(() => {})
  if (STAT.pending.length || (STAT.uid && STAT.sec >= MIN_SESSION_SEC)) {
    flushStats(false, { restart: true }).catch(() => {})
  }
}, S.syncEveryMs)

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return flushProgress()          // 收场 + 推上去（队列已落盘，推不上也不丢）
  // 切回来接着记：刚才那一推已经把这一场收场了，不重开就再也不计数
  claimTab()
  if (!STAT.uid && S.book && document.body.dataset.pane === 'reader-pane') statOpen(S.book, S.pos)
  if (STAT.pending.length) flushStats(false).catch(() => {})
})

// 从"往返缓存"里回来（手机上切应用再回来常见）：定时器停过，重新开一场，别把中途那段算进去
window.addEventListener('pageshow', (e) => {
  if (!e.persisted) return
  claimTab()
  if (!STAT.uid && S.book && document.body.dataset.pane === 'reader-pane') statOpen(S.book, S.pos)
  if (STAT.pending.length) flushStats(false).catch(() => {})
})
window.addEventListener('pagehide', flushProgress)
window.addEventListener('beforeunload', flushProgress)

/* ── 起 ─────────────────────────────────────────────── */

loadLayout()
loadPending()      // 上次没推上去的会话，先把队列捞回来（进书架后补推）

boot().catch((e) => {
  console.error(e)
  toast('启动失败：' + (e.message || e))
  show('login')
})
