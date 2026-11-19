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

// 字体：都是系统里就有的。**列表要短、中文名要排在前面**——
// 2026-11-19 实测：往后面塞一堆"可能装了的"名字反而有害，只要有一个只在拉丁文上匹配的
// 家族（HarmonyOS Sans / MiSans / OPPOSans 这类常常只装了拉丁子集）排在中文字体前面，
// 中文就会整段落到系统兜底字形上，看着就是"换了字体没反应"。
// 所以这里只留确定能覆盖中文的几个，顺序按 苹果 / Windows / Android。
// 注意：**别往里加只装了拉丁子集的家族**（HarmonyOS Sans / MiSans / OPPOSans 这类），
// 它们会先把文字认领走，汉字再逐字落到兜底，看着就是"换了字体没反应"，而且坏得没有声音。
// 设备上到底有没有，`faceSupport()` 会真的画一次字来判，不靠这张表猜。
const FACES = {
  serif: `'Songti SC','STSong','SimSun','Noto Serif CJK SC','Noto Serif SC','Source Han Serif SC',serif`,
  sans: `'PingFang SC','Heiti SC','Microsoft YaHei','Hiragino Sans GB','Noto Sans CJK SC','Noto Sans SC',sans-serif`,
  kai: `'Kaiti SC','STKaiti','Kaiti TC','KaiTi','Noto Serif CJK SC',serif`,
  round: `'Yuanti SC','Yuanti TC','PingFang SC','Microsoft YaHei',sans-serif`,
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

/* foliate 的影子根是 closed 的（`el.shadowRoot` 拿到 null），正文 iframe 藏在里面。
   这里在 attachShadow 时记一笔，后面量字体、排障时才有办法摸到正文那个文档。 */
const SHADOWS = []
try {
  const origAttach = Element.prototype.attachShadow
  Element.prototype.attachShadow = function (init) {
    const root = origAttach.call(this, init)
    try { SHADOWS.push(root) } catch (e) { /* 记不下就算了 */ }
    return root
  }
} catch (e) { /* 没有 attachShadow 的浏览器不需要这个 */ }

/** 所有能摸到的正文文档（书是按章节一帧一帧塞进 iframe 的） */
function bookDocs() {
  const docs = []
  const scan = (root) => {
    let frames = []
    try { frames = root.querySelectorAll('iframe') } catch (e) { frames = [] }
    for (const f of frames) {
      try { if (f.contentDocument && f.contentDocument.body) docs.push(f.contentDocument) } catch (e) {}
    }
    let all = []
    try { all = root.querySelectorAll('*') } catch (e) { all = [] }
    for (const el of all) if (el.shadowRoot) scan(el.shadowRoot)
  }
  scan(document)
  for (const s of SHADOWS) scan(s)
  return docs
}

/* 这台设备到底有没有这个字体：把同一串字真画进 canvas，比像素指纹。
   CJK 每个字都是全角，量 advance width 永远是 1em —— 量宽量不出字体差别，只能比墨迹。
   指纹跟「黑」一样 = 换了个名字但落在同一张字形上 = 点了画面上不会有变化。 */
const GLYPH_SAMPLE = '永和九年岁在癸丑暮春之初'
let FACE_OK = null

function glyphInk(family) {
  try {
    const cv = document.createElement('canvas')
    cv.width = 760
    cv.height = 90
    const g = cv.getContext('2d', { willReadFrequently: true })
    if (!g) return null
    g.fillStyle = '#fff'
    g.fillRect(0, 0, cv.width, cv.height)
    g.fillStyle = '#000'
    g.font = '56px ' + family
    g.textBaseline = 'top'
    g.fillText(GLYPH_SAMPLE, 2, 6)
    const d = g.getImageData(0, 0, cv.width, cv.height).data
    let h = 2166136261 >>> 0
    for (let i = 0; i < d.length; i += 4) {
      h ^= d[i]
      h = Math.imul(h, 16777619) >>> 0
    }
    return h.toString(16)
  } catch (e) {
    return null                    // 隐私模式/无 canvas：当"不知道"，别乱标
  }
}

/** { serif: true/false, ... }：每个字体在这台设备上是不是真的换了一张字形 */
function faceSupport() {
  if (FACE_OK) return FACE_OK
  FACE_OK = {}
  const base = glyphInk(FACES.sans)
  for (const key of Object.keys(FACES)) {
    if (key === 'sans') { FACE_OK[key] = true; continue }
    const ink = glyphInk(FACES[key])
    FACE_OK[key] = (ink === null || base === null) ? true : ink !== base
  }
  return FACE_OK
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
  index: null, anchors: null, sf: null, // 桌面端偏移表 / 每节的起始字偏移 / 每节在全书的占比
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

/* 只在 https 下登录：登录下发的 Cookie 带 Secure，浏览器在 http 页面上会直接丢掉它，
   表现就是"输了 token 又弹回登录框"。手机上很多浏览器不会自动升级到 https。 */
if (location.protocol === 'http:'
    && !/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) {
  location.replace('https://' + location.host + location.pathname + location.search)
}

/* token 又长又是等宽乱码，手机上看不见就没法查错：给个「显示/隐藏」 */
$('#peek').addEventListener('click', () => {
  const inp = $('#token')
  const show = inp.type === 'password'
  inp.type = show ? 'text' : 'password'
  $('#peek').textContent = show ? '隐藏' : '显示'
})

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const err = $('#login-err')
  const btn = e.target.querySelector('button[type=submit]')
  err.textContent = ''
  const token = $('#token').value.trim()
  if (!token) return
  btn.disabled = true
  btn.textContent = '登录中…'
  try {
    let res
    try {
      res = await fetch('/v1/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ token }),
      })
    } catch (e2) {
      // fetch 抛异常 = 根本没连上（断网/DNS/被拦），以前这里一声不响，看着像"点了没反应"
      err.textContent = '连不上同步服务（' + (navigator.onLine === false ? '手机没网' : '网络或 DNS 不通') + '）'
      return
    }
    if (res.status === 401) {
      err.textContent = 'token 不对（复制时多带了空格或换行也算不对）'
      return
    }
    if (!res.ok) { err.textContent = '服务端出错（HTTP ' + res.status + '），等会儿再试'; return }
    $('#token').value = ''
    try {
      await enterApp()
    } catch (e2) {
      const msg = String((e2 && e2.message) || e2)
      err.textContent = msg === 'unauthorized'
        ? '登录成功了但浏览器没存下 Cookie：用 https 打开，或关掉无痕/隐私模式再试'
        : msg
    }
  } finally {
    btn.disabled = false
    btn.textContent = '进去'
  }
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
  $('#shelf-note').textContent = S.books.length ? ''
    : '云端还没有书：点右上角「上传书籍」，或者从桌面端「设置 → 云端同步」传一本上来'
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

/** 换书必须把 <foliate-view> 整个换成新的。
 *  foliate 的 view.open() 只会再 append 一个 renderer 到自己的 shadow root 里，旧的
 *  （连同它那本书的 iframe）仍然留着，于是"回书架再打开第 2 本，屏幕上还是第 1 本"——
 *  两本叠在一起，看得见的是先打开的那本。换元素 = 旧的连 shadow 树一起丢掉，内存也还回去。 */
function freshView() {
  const old = $('#view')
  const el = document.createElement('foliate-view')
  el.id = 'view'
  if (old && old.parentNode) old.parentNode.replaceChild(el, old)
  else $('#view-wrap').appendChild(el)
  bindView(el)
  S.view = el
  return el
}

async function openBook(book) {
  show('reader-pane')
  $('#pos').textContent = '打开中…'
  const view = freshView()
  S.book = book
  S.pos = book.pos || 0
  S.total = book.total || 0
  S.index = null                       // 换书了，上一本的章节表不能留着
  S.anchors = null                     // 每节的起始字偏移（拿到偏移表再说）
  S.sf = null                          // 每节在全书的占比（open 之后才有）
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
  // 打开后立刻把字体/字号交给 renderer：foliate 刚 open 完拿到的是空壳文档，
  // 这一步重排才会把正文铺出来（不然第一页一片空白，翻一下才有字）。
  applyStyles()
  applyLayout()
  barsShow(3200)                      // 刚打开时让人看见底栏，之后自己收起来
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

  // 书架那一行可能是几个小时前拉的（手机上的 PWA 一直挂在后台，不会重载），而别的设备
  // 刚读过。上面这份进度文档是刚拿到的，它比书架那行新就按它定位——否则"PC 上读到第 10 章，
  // 手机打开还停在第 2 章，然后一按返回就把 PC 的进度盖回第 2 章"。
  try {
    const row = S.progressBooks[S.book.uid]
    const atRow = Number(row && row.at) || 0
    const atShelf = Number(book.at) || 0
    const rowPos = Number(row && row.pos)
    if (row && Number.isFinite(rowPos) && rowPos >= 0
        && atRow > atShelf && Math.abs(rowPos - S.pos) > 1) {
      S.pos = Math.round(rowPos)
      if (Number(row.total) > 0 && !S.total) S.total = Number(row.total)
      book.pos = S.pos
      book.at = atRow
    }
  } catch (e) { /* 老书架那个位置也能用，别因为这段打不开书 */ }

  // 偏移表 schema>=2 带 files（每个文件的起始字符偏移）：有了就能按"第几个字"精确落位，
  // 没有就只能按字节比例猜（同一本书能差 1%，十几页）。S.anchors=null 就是退回老路。
  S.anchors = buildCharAnchors(view.book, S.index)
  S.sf = sectionFractions()

  if (S.pos > 0 && S.total > 0) {
    await goToPos(S.pos)
  } else {
    const shown = () => {
      const r = S.view && S.view.renderer
      if (!r || typeof r.getContents !== 'function') return false
      return r.getContents().some(c => c.doc && c.doc.body &&
        (c.doc.body.textContent || '').trim().length > 0)
    }
    if (!shown()) {
      await new Promise((r) => setTimeout(r, 80))
      if (!shown()) {
        try { await view.next(); await view.prev() } catch (e) { /* 还是不行就先放着 */ }
      }
    }
  }
  renderTOC()
  if (!S.total) {
    $('#pos').textContent = '—'
    ensureTotal(book).catch(() => {})   // 网页自己传上来的书：没人给它算总字数
  }
}

/* 桌面端的坐标是"抽出来的全文里第几个字"。书是按文件切节的，偏移表 schema>=2 里
   带 files：每个文件的起始字符偏移。有了它，[第几个字] ↔ [第几节 + 节内几分之几]
   是精确换算；只有 total 的话只能按"字节比例"猜，实测差 1%（十几页）。

   返回 {starts: [每节的起始字符偏移], total}，或者 null（索引太老/对不上 → 走老路）。 */
function buildCharAnchors(book, index) {
  const secs = (book && book.sections) || []
  const files = (index && Number(index.schema) >= 2 && Array.isArray(index.files)) ? index.files : null
  const total = Number(index && index.total) || 0
  if (!secs.length || !files || files.length < 2 || !total) return null
  const norm = (p) => String(p || '').replace(/^\.?\//, '')
  const byPath = new Map()
  for (const f of files) {
    const key = norm(f && f.path)
    const st = Number(f && f.start)
    if (key && Number.isFinite(st) && st >= 0) byPath.set(key, st)
  }
  if (!byPath.size) return null
  const starts = new Array(secs.length).fill(null)
  let hit = 0
  secs.forEach((s, i) => {
    const st = byPath.get(norm(s && s.id))
    if (st !== undefined) { starts[i] = st; hit += 1 }
  })
  // 一个路径都对不上（别处传上来的书、路径写法不同）：数量一致时按顺序对齐
  if (!hit && files.length === secs.length) {
    files.forEach((f, i) => { starts[i] = Number(f.start) || 0 })
    hit = secs.length
  }
  if (hit < 2 || hit < Math.ceil(secs.length * 0.6)) return null
  // 没对上的（封面、纯图片页）沿用前一个锚点，再保证单调不减
  let last = 0
  for (let i = 0; i < starts.length; i++) {
    if (starts[i] === null) starts[i] = last
    else last = starts[i]
    if (i && starts[i] < starts[i - 1]) starts[i] = starts[i - 1]
  }
  return { starts, total: Math.max(total, starts[starts.length - 1] + 1) }
}

/* 每一节在全书里的位置（foliate 按字节算的累计比例）——换算节内比例要用它 */
function sectionFractions() {
  const v = S.view
  try {
    const f = v && typeof v.getSectionFractions === 'function' ? v.getSectionFractions() : null
    return Array.isArray(f) && f.length > 1 ? f.map((x) => Number(x) || 0) : null
  } catch (e) { return null }
}

/* relocate 之后：这一步该记成"第几个字" */
function posOfLoc(loc) {
  const frac = Number(loc && loc.fraction) || 0
  const total = Number(S.total) || 0
  const A = S.anchors
  const sf = S.sf
  const idx = Number(loc && loc.section && loc.section.current)
  if (!A || !sf || !total || !Number.isFinite(idx) || idx < 0 || idx >= A.starts.length) {
    return Math.round(frac * total)
  }
  const lo = sf[idx] || 0
  const hi = idx + 1 < sf.length ? sf[idx + 1] : 1
  const within = hi > lo ? clamp((frac - lo) / (hi - lo), 0, 1) : 0
  const s0 = A.starts[idx]
  const s1 = idx + 1 < A.starts.length ? A.starts[idx + 1] : A.total
  return Math.round(clamp(s0 + within * (s1 - s0), 0, total))
}

/* 跳到"第几个字"：有锚点表就按节+节内比例走，没有就退回按比例 */
async function goToPos(pos) {
  if (S.anchors && S.total > 0) {
    const v = S.view
    const A = S.anchors
    const p = clamp(Number(pos) || 0, 0, A.total)
    let idx = A.starts.length - 1
    for (let i = 0; i < A.starts.length; i++) {
      const end = i + 1 < A.starts.length ? A.starts[i + 1] : A.total
      if (p < end) { idx = i; break }
    }
    const s0 = A.starts[idx]
    const s1 = idx + 1 < A.starts.length ? A.starts[idx + 1] : A.total
    const within = s1 > s0 ? clamp((p - s0) / (s1 - s0), 0, 0.99) : 0
    try { await v.goTo(idx) } catch (e) { return }
    await new Promise((r) => setTimeout(r, 250))     // goTo 发起即返回，等它真换完
    if (within > 0.01) {
      try { await v.renderer.scrollToAnchor(within) } catch (e) { /* 差一点就落节首 */ }
    }
    return
  }
  return goToFrac(S.total > 0 ? clamp((Number(pos) || 0) / S.total, 0, 1) : 0)
}

/* 按全书百分比定位（没有锚点表时的老路）。foliate 自带的 goToFraction 依赖"章节字数表"，
   有些书里它还没准备好，调了等于没动（打开就回到开头）。这里自己按章节字数
   累加算出目标章节，用 goTo(章节下标) 跳——这条路桌面和手机上都实测有效。 */
async function goToFrac(frac) {
  const v = S.view
  if (!v) return
  const f = clamp(frac, 0, 1)
  const secs = (v.book && v.book.sections) || []
  if (!secs.length) {
    try { await v.goToFraction(f) } catch (e) { /* 没辙就先放着 */ }
    return
  }
  const sizes = secs.map(s => Number(s.size) || 0)
  const tot = sizes.reduce((a, b) => a + b, 0)
  let idx = 0
  let acc = 0
  if (tot > 0) {
    const target = f * tot
    idx = secs.length - 1
    for (let i = 0; i < secs.length; i++) {
      if (acc + sizes[i] > target) { idx = i; break }
      acc += sizes[i]
    }
  } else {
    idx = Math.min(secs.length - 1, Math.floor(f * secs.length))
  }
  try { await v.goTo(idx) } catch (e) { return }
  // goTo 是"发起即返回"，等它真的换完页再谈章内偏移
  await new Promise((r) => setTimeout(r, 250))
  const size = sizes[idx] || 0
  if (size > 0 && tot > 0) {
    const within = clamp((f * tot - acc) / size, 0, 0.99)
    if (within > 0.02) {
      try { await v.renderer.scrollToAnchor(within) } catch (e) { /* 章内偏一点不成就落章首 */ }
    }
  }
}

/* ── 位置：字符偏移 ↔ 进度 ─────────────────────────────── */

function onRelocate(e) {
  const loc = (e && e.detail) || {}
  const frac = Number(loc.fraction) || 0
  S.pos = posOfLoc(loc)                // 有锚点表就换算成"第几个字"，和桌面端同一个数
  S.dirty = true
  statMove(S.pos)                     // 统计只在本机攒着，跟"推不推"没关系
  const pct = S.total ? Math.round((100 * S.pos) / S.total) : Math.round(frac * 100)
  $('#pos').textContent = S.total
    ? `${pct}%  ·  ${fmt(S.pos)} / ${fmt(S.total)} 字`
    : `${pct}%`
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
    /* 光给 body 加 !important 没用：书自带的样式表常给 p / div.calibre 自己写死字体，
       子元素自己的声明永远赢过"继承来的"值，所以手机上换字体看着没反应。
       这里把所有后代都压成同一个字体，代码块留着等宽。 */
    body, body * { font-family: ${c.face} !important; }
    code, pre, kbd, samp, tt, code * {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace !important;
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
  document.documentElement.style.setProperty('--pad-v', `${Math.round(c.mg * c.size * 0.6)}px`)
  if (S.view && S.view.renderer && typeof S.view.renderer.setStyles === 'function') {
    S.view.renderer.setStyles(bookCSS(c))
  }
  for (const doc of bookDocs()) forceFamily(doc)
}

const MONO_TAGS = { CODE: 1, PRE: 1, KBD: 1, SAMP: 1, TT: 1 }

/** 把字体真的按到正文每个元素上——用 **inline !important**。

 *  为什么不能只靠注入的 `body, body * { font-family: … !important }`：
 *  书自带的样式表常写成 `p.calibre { font-family: "方正…" !important }`，选择器比 `body *` 更具体，
 *  两边都带 !important 时它赢——表现就是**字号能变（字号没人跟我们抢）、字体一点不变**。
 *  层叠里只有 inline 的 !important 比 author 的 !important 更高，所以这里逐元素写一遍。
 *  只在换字体 / 新章节加载时跑，代价是一次遍历。 */
function forceFamily(doc) {
  if (!doc || !doc.documentElement) return
  const family = (FACES[(S.values || {}).face] || FACES.sans)
  doc.documentElement.style.setProperty('font-family', family, 'important')
  const walk = (el) => {
    if (!el || MONO_TAGS[el.tagName]) return
    try { el.style.setProperty('font-family', family, 'important') } catch (e) { return }
    const kids = el.children
    for (let i = 0; i < kids.length; i++) walk(kids[i])
  }
  if (doc.body) walk(doc.body)
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
  // 「点了没反应」要当场说清楚：这台设备没装的字体，按钮画成虚线、面板里点明
  const ok = faceSupport()
  const missing = []
  for (const b of document.querySelectorAll('#tools [data-act^="face-"]')) {
    const act = b.dataset.act || ''
    const good = ok[act.slice(5)] !== false
    b.classList.toggle('off', !good)
    b.title = good ? '' : '这台设备没装这个字体，点了不会有变化'
    if (!good) missing.push(b.textContent)
  }
  const note = $('#font-note')
  if (note) {
    if (missing.length) {
      note.hidden = false
      note.textContent = '这台设备没装：' + missing.join('、') + '（点了画面上不会有变化）'
    } else {
      note.hidden = true
      note.textContent = ''
    }
    // 排障开关：地址后面加 ?fontdiag=1，这一行改成显示**正文那个文档里排版量出来的**
    // 四档行高。canvas 和排版在 iOS 上可能不是同一套回退，所以这里不走 canvas。
    if (/[?&]fontdiag/.test(location.search)) {
      note.hidden = false
      note.textContent = fontDiag()
    }
  }
}

/** 排障用：在**正文那个文档里**量，不看外层页面。
 *  报三样：(1) 正文 <p> 现在赢的 font-family 到底是什么（如果书的样式赢了我们，这里看到的是书的字体）
 *  (2) 四档字体在这台设备上画出来的像素指纹首 8 位（一样 = 同一张字形）
 *  (3) docs / setStyles，确认样式通道通不通。 */
function fontDiag() {
  const docs = bookDocs()
  const d = docs[0]
  if (!d) return 'fontdiag: 没找到正文文档（docs=0）'
  const ink = (family, text) => {
    const cv = d.createElement('canvas')
    cv.width = 760
    cv.height = 90
    const g = cv.getContext('2d', { willReadFrequently: true })
    if (!g) return 'n/a'
    g.fillStyle = '#fff'
    g.fillRect(0, 0, cv.width, cv.height)
    g.fillStyle = '#000'
    g.font = '56px ' + family
    g.textBaseline = 'top'
    g.fillText(text || '永和九年岁在癸丑暮春之初', 2, 6)
    let px
    try { px = g.getImageData(0, 0, cv.width, cv.height).data } catch (e) { return 'blocked' }
    let h = 2166136261 >>> 0
    for (let i = 0; i < px.length; i += 4) { h ^= px[i]; h = Math.imul(h, 16777619) >>> 0 }
    return h.toString(16).slice(0, 8)
  }
  const marks = [['宋', 'serif'], ['黑', 'sans'], ['楷', 'kai'], ['圆', 'round']]
    .map(([label, k]) => label + '=' + ink(FACES[k]))
  // 拉丁字宽：中文全角量不出字体，拉丁的 advance 每套字体不同，是第二条独立线索。
  // 必须写 inline !important —— 我们自己的 bookCSS 有 `body * { font-family: … !important }`，
  // 不重要的 inline 会被它压掉，四个量出来会一模一样。
  const probe = d.createElement('span')
  probe.textContent = 'Hamburgefonstiv'
  probe.style.cssText = 'position:absolute;left:-9999px;top:0;white-space:nowrap;font-size:100px'
  d.body.appendChild(probe)
  const widths = ['serif', 'sans', 'kai', 'round'].map((k) => {
    probe.style.setProperty('font-family', FACES[k], 'important')
    return Math.round(probe.getBoundingClientRect().width)
  })
  const p = d.querySelector('p') || d.body
  const cs = d.defaultView.getComputedStyle(p)
  d.body.removeChild(probe)
  return 'diag ' + marks.join(' ') + ' | 拉丁宽 ' + widths.join('/')
    + ' | 正文 ' + Math.round(parseFloat(cs.fontSize)) + 'px ' + cs.fontFamily.slice(0, 44)
    + ' | docs=' + docs.length
    + ' | setStyles=' + (S.view && S.view.renderer && typeof S.view.renderer.setStyles === 'function')
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
    （它的滚动盒子挂在 closed shadow root 里，外部拿不到；start/viewSize 是公开的 getter）
    返回 false 表示"这个方向已经到头了"，交给调用方去翻章。 */
function scrollStep(dy) {
  const r = S.view && S.view.renderer
  if (!r || typeof r.scrollToAnchor !== 'function') return false
  const size = Number(r.viewSize) || 0
  if (!size) return false
  const start = Number(r.start) || 0
  const page = Number(r.size) || size
  if (dy > 0 && start + page >= size - 4) return false   // 已经到底
  if (dy < 0 && start <= 0) return false                 // 已经到顶
  r.scrollToAnchor(clamp((start + dy) / size, 0, 1))
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
  if (act.startsWith('face-')) {
    const key = act.slice(5)
    const label = (e.target.textContent || '').trim()
    if (faceSupport()[key] === false) {
      // 还是记下这个选择（换了设备可能就有了），但当场告诉人"这台改不动"，不要假装成功
      toast(`这台设备没装「${label}」，正文不会变`)
    }
    return saveSettings({ face: key })
  }
  if (act.startsWith('paper-')) return saveSettings({ paper: act.slice(6) })
  if (!STEP[act]) return
  saveSettings(STEP[act]())
})

$('#open-tools').addEventListener('click', () => { $('#tools').hidden = !$('#tools').hidden;
  if (!$('#tools').hidden) paintValues() })

/** 回书架：先把界面切过去，再去推网络。
 *  以前是"await 推完才切"，网一抖（401/超时/412）就什么都不发生，看着像按钮坏了。 */
function goShelf() {
  $('#tools').hidden = true
  $('#toc-panel').hidden = true
  document.body.classList.remove('bare')
  try { statCommit() } catch (e) { /* 统计收不了场也不该拦着人走 */ }
  show('shelf-pane')
  loadShelf().catch(() => {})
  pushProgress(true).catch(() => {})
  flushStats(false).catch(() => {})
}

$('#back').addEventListener('click', goShelf)

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

/** 滚轮：翻页模式下一页一动；滚动模式往下滚，滚到头就接下一章 */
function onWheel(e) {
  if (S.flow === 'scrolled') {
    const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerHeight : 1
    const dy = e.deltaY * px
    if (!dy) return
    e.preventDefault()
    if (scrollStep(dy)) return
    // 到底/到顶了：再滚就翻章（触控板一次滑动会连发几十个，给个冷却）
    const now = Date.now()
    if (now - (onWheel.edge || 0) < 700) return
    onWheel.edge = now
    turn(dy > 0 ? 1 : -1)
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

/** 点左三分之一往回、点右三分之一往前（滚动模式下点哪儿都不翻页）。
 *  最底下那一条留给"叫出底栏"，不拿来翻页——否则想点「书架」先翻过去一页。
 *  手机上的点击现在由 paginator 在正文里先接住（它才拿得到 iframe 里的触摸），
 *  处理完会抛 tap/swiped 上来；这条 click 路只剩"鼠标点在正文外的页边距"和桌面端。 */
let ignoreClickUntil = 0

function onTapTurn(e) {
  if (document.body.dataset.pane !== 'reader-pane') return
  if (performance.now() < ignoreClickUntil) return
  if (e.clientY > window.innerHeight - 84) {
    if (document.body.classList.contains('bare')) barsShow()
    else barsHide()
    return
  }
  if (e.target && e.target.closest && e.target.closest('a[href]')) return
  const w = window.innerWidth
  const x = e.clientX
  // 左右三分之一翻页（滚动模式不翻页），其余地方——包括四周的页边距——点了就收/叫底栏
  if (S.flow !== 'scrolled') {
    if (x < w * 0.3) return turn(-1)
    if (x > w * 0.7) return turn(1)
  }
  barsToggle()
}

/* ── 沉浸：底栏平时收起来 ───────────────────────────────
   鼠标挪到底部、按任意键、点屏幕最下面一条，底栏浮出来，2.6 秒没动静再收回去。
   栏是浮层、不占正文高度，所以叫出来和收回去都不会让正文重排（重排会跳页）。 */

let barsTimer = 0
function barsShow(ms = 2600) {
  document.body.classList.remove('bare')
  clearTimeout(barsTimer)
  barsTimer = setTimeout(() => {
    if (document.body.dataset.pane === 'reader-pane') document.body.classList.add('bare')
  }, ms)
}
function barsHide() {
  clearTimeout(barsTimer)
  document.body.classList.add('bare')
}
function barsToggle() {
  if (document.body.classList.contains('bare')) barsShow()
  else barsHide()
}
// 鼠标挪到底部就把底栏叫出来——只在"真有鼠标"的设备上这么干。
// iOS 每次点完还会补发一串 mousemove/mousedown/click（兼容事件），手指点在屏幕底部时
// 那条 mousemove 就被当成"鼠标移到底部"，把刚落下去的底栏又弹回来：于是"再点一下收起"
// 在底部那一块永远收不起来。触屏上这条整个不参与，底栏交给 tap / click 那两条路。
const canHover = window.matchMedia ? window.matchMedia('(hover: hover)').matches : true
let lastTouchAt = 0
document.addEventListener('touchstart', () => { lastTouchAt = performance.now() },
  { passive: true, capture: true })
document.addEventListener('mousemove', (e) => {
  if (!canHover) return
  if (performance.now() - lastTouchAt < 800) return   // 手指刚点过：这是兼容事件，不是鼠标
  if (document.body.dataset.pane !== 'reader-pane') return
  if (e.clientY > window.innerHeight - 110) barsShow()
})

// 底栏升起来之后，点它的空白处就收回去——它压在屏幕上，不接这一下的话
// "再点一下收起"在底部那一块永远点不动（按钮自己有自己的事，不拦）。
document.querySelector('#hud').addEventListener('click', (e) => {
  if (e.target && e.target.closest && e.target.closest('button')) return
  barsHide()
})

// 书的内容在 iframe 里，滚轮/点击到不了外层：挂到每一篇加载出来的 doc 上
const hookedDocs = new WeakSet()
function hookDoc(doc) {
  if (!doc || hookedDocs.has(doc)) return
  hookedDocs.add(doc)
  doc.addEventListener('wheel', onWheel, { passive: false })
  doc.addEventListener('click', onTapTurn)
  forceFamily(doc)          // 新章节（新 iframe）也要按上当前字体
}

/** 这一下点的是不是"最下面那一条"（留给叫底栏，不拿来翻页）。
 *  优先用 paginator 折好的 yf（页内比例），它不怕不同环境给的 clientY 偏移；
 *  yf 明显不像比例（没有 / 被偏移带歪）时退回原始 clientY。 */
function bottomStrip(d) {
  const yf = Number(d && d.yf)
  if (Number.isFinite(yf) && yf > 0 && yf < 1.4) return yf > 0.88
  return Number(d && d.clientY) > window.innerHeight - 84
}

/** 外层的几个监听挂到（可能刚换过的）foliate-view 上。
 *  换书会整个换元素（见 freshView），所以不能只在启动时挂一次，否则第 2 本就没人接事件了。 */
function bindView(el) {
  el.addEventListener('load', (e) => hookDoc(e.detail && e.detail.doc))

  // 正文里那层（paginator）把"点了一下 / 横着划了一下"抛上来：它已经处理完翻页了，
  // 这里只管底栏，并且挡住紧跟其后的 click，免得一次点击翻两页。
  el.addEventListener('tap', (e) => {
    ignoreClickUntil = performance.now() + 500
    if (document.body.dataset.pane !== 'reader-pane') return
    const d = e.detail || {}
    // 底栏正开着：再点一下正文就收起来——**不管点在中间还是最下面那一条**。
    // 以前这里对"最下面一条"一律 barsShow()，可底栏一升起来，那一条正好被它自己占住，
    // 于是"点一下叫出来、再点一下收回去"只能work一次。
    if (!document.body.classList.contains('bare')) return barsHide()
    if (d.zone === 'middle' || bottomStrip(d)) barsShow()
  })
  el.addEventListener('swiped', () => {
    ignoreClickUntil = performance.now() + 500
  })
}

/* ── 全屏：能真全屏的就全屏，iPhone 上说清楚该怎么办 ───────────
   iOS 的 Safari 不给网页调 requestFullscreen（只有 iPad 和安卓/桌面浏览器给），
   连"添加到主屏幕"之后那点 standalone 模式，顶上的状态栏（时间/电量）也去不掉——
   系统级的，网页没有任何办法。所以 iPhone 上能做到的最沉浸是：从主屏幕图标打开，
   没有地址栏，正文铺到状态栏底下；安卓/桌面则给真全屏（连状态栏/任务栏一起没有）。 */
function isStandalone() {
  return !!(window.navigator.standalone
    || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches))
}

const isIOS = (() => {
  const p = String(navigator.platform || '')
  return /iP(hone|ad|od)/.test(p) || /iPhone|iPad|iPod/.test(String(navigator.userAgent || ''))
    || (navigator.maxTouchPoints > 1 && /Mac/.test(p))     // iPadOS 会把自己报成 Mac
})()

function toggleFull() {
  const el = document.documentElement
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document)
    return
  }
  const req = el.requestFullscreen || el.webkitRequestFullscreen
  if (req) {
    Promise.resolve(req.call(el, { navigationUI: 'hide' }))
      .catch(() => toast('这台设备不给网页全屏', 3200))
    return
  }
  if (isStandalone()) {
    return toast(isIOS
      ? '已经是「从主屏幕打开」的全屏了。iPhone 顶上的状态栏（时间/电量）系统不让网页去掉，'
        + '正文已经铺到它底下——这是 iPhone 上能做到的最沉浸'
      : '已经是「从主屏幕打开」的全屏了', 6000)
  }
  toast('iPhone 的 Safari 不给网页全屏：点底部「分享」→「添加到主屏幕」，'
    + '以后从桌面图标打开就没有地址栏了（顶上的状态栏系统不让去；安卓上连它一起没有）', 7000)
}

$('#full').addEventListener('click', toggleFull)

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
  else if (k === 'Escape') {
    const open = !$('#tools').hidden || !$('#toc-panel').hidden
    if (open) { $('#tools').hidden = true; $('#toc-panel').hidden = true }
    else barsToggle()                 // 没开面板时：叫出/收起底栏
    return
  }
  if (document.body.classList.contains('bare')) barsShow(2000)
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
      ...remote,                     // 云端还有别的键（version/schema…）原样带回去，别一推就掉
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

/* ── 网页自己传上来的书：把总字数补上 ─────────────────────
   桌面端传书时顺手算好了章节表和总字数（/v1/lite/<uid>/index.json）；
   从网页传的书没人算过，第一次打开时自己数一遍：按 spine 逐篇取 textContent 的长度，
   加起来就是 total，同时把每一章的字偏移记下来（和桌面端那份 index 同一个形状）。 */

async function ensureTotal(book) {
  const sections = (S.view && S.view.book && S.view.book.sections) || []
  if (!sections.length) return
  // 几千篇的书（连载合集）逐篇解析太慢：只按压缩前的字节数粗估，够用了
  const many = sections.length > 400
  let total = 0
  const out = []
  for (let i = 0; i < sections.length; i += 1) {
    const s = sections[i]
    let n = 0
    if (many) {
      n = Math.round((Number(s.size) || 0) / 3)
    } else {
      try {
        const doc = await s.createDocument()
        n = String((doc && doc.body && doc.body.textContent) || '').replace(/\s+/g, '').length
      } catch (e) {
        n = Math.round((Number(s.size) || 0) / 3)      // 解不开就按"一个汉字三字节"粗估
      }
    }
    out.push({ title: '', start: total, level: 1 })
    total += n
  }
  if (!(total > 0)) return
  S.total = total
  S.dirty = true

  const payload = { schema: 1, v: 1, uid: book.uid, total, sections: out, generated_at: ts() }
  await req(`/v1/lite/${book.uid}/index.json`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  }).catch(() => null)
  pushProgress(true).catch(() => {})                // 顺手把 total/书名写进进度文档
  if (document.body.dataset.pane === 'reader-pane' && S.book && S.book.uid === book.uid) {
    $('#pos').textContent = `${Math.round(clamp(S.pos / total, 0, 1) * 100)}%  ·  `
      + `${fmt(S.pos)} / ${fmt(total)} 字`
  }
}

/* ── 阅读统计页：能看到数就行，不做花活 ────────────────────
   数据就是云端账本（/v1/state/stats）：days 是每日小计，books 是每本书的累计。
   网页只往里加会话，日账/书账是桌面端合并时重算的，所以这里显示的是"所有设备合起来"。 */

function statRow(name, sec, chars, max) {
  const row = document.createElement('div')
  row.className = 's-row'
  const a = document.createElement('span')
  a.className = 's-name'
  a.textContent = name
  const bar = document.createElement('span')
  bar.className = 's-bar'
  const fill = document.createElement('i')
  fill.style.width = (max > 0 ? Math.max(2, Math.round((sec / max) * 100)) : 0) + '%'
  bar.appendChild(fill)
  const val = document.createElement('span')
  val.className = 's-val'
  val.textContent = `${Math.max(1, Math.round(sec / 60))} 分 · ${fmt(chars)} 字`
  row.append(a, bar, val)
  return row
}

async function openStats() {
  $('#stats-body').textContent = ''
  $('#stats-note').textContent = '读取中…'
  $('#shelf-pane').hidden = true
  $('#stats-pane').hidden = false
  document.body.dataset.pane = 'stats-pane'
  const got = await getJSON('/v1/state/stats').catch(() => null)
  const doc = (got && got.body) || {}
  const days = (doc.days && typeof doc.days === 'object') ? doc.days : {}
  const books = (doc.books && typeof doc.books === 'object') ? doc.books : {}
  const body = $('#stats-body')
  body.textContent = ''

  // 今天 / 累计
  const today = days[dayKey()] || {}
  const totalSec = Object.values(days).reduce((a, d) => a + (Number(d && d.sec) || 0), 0)
  const totalChars = Object.values(days).reduce((a, d) => a + (Number(d && d.chars) || 0), 0)
  const big = document.createElement('div')
  big.className = 's-big'
  const b1 = document.createElement('b')
  b1.textContent = `${Math.max(0, Math.round((Number(today.sec) || 0) / 60))} 分钟`
  const s1 = document.createElement('span')
  s1.textContent = `今天 · ${fmt(today.chars)} 字`
  big.append(b1, s1)
  body.appendChild(big)

  const sum = document.createElement('div')
  sum.className = 's-empty'
  const dayN = Object.keys(days).length
  sum.textContent = dayN
    ? `一共读了 ${dayN} 天 · ${Math.round(totalSec / 60)} 分钟 · ${fmt(totalChars)} 字`
    : '账本还是空的：读满一分钟就会记上'
  body.appendChild(sum)

  // 最近 7 天
  const keys = Object.keys(days).sort().slice(-7)
  if (keys.length) {
    const h = document.createElement('div')
    h.className = 's-sec'
    h.textContent = '最近几天'
    body.appendChild(h)
    const maxSec = keys.reduce((a, k) => Math.max(a, Number((days[k] || {}).sec) || 0), 0)
    for (const k of keys.slice().reverse()) {
      const d = days[k] || {}
      body.appendChild(statRow(k.slice(5), Number(d.sec) || 0, Number(d.chars) || 0, maxSec))
    }
  }

  // 每本书（按时间排，只列前 12 本）
  const rows = Object.entries(books)
    .map(([uid, b]) => ({ uid, ...(b || {}) }))
    .filter((b) => Number(b.sec) > 0)
    .sort((a, b) => (Number(b.sec) || 0) - (Number(a.sec) || 0))
  if (rows.length) {
    const h = document.createElement('div')
    h.className = 's-sec'
    h.textContent = '每本书'
    body.appendChild(h)
    const maxSec = Number(rows[0].sec) || 0
    for (const r of rows.slice(0, 12)) {
      const row = document.createElement('div')
      row.className = 's-book'
      const t = document.createElement('span')
      t.className = 's-title'
      const titles = S.books.find((x) => x.uid === r.uid)
      t.textContent = r.title || (titles && titles.title) || r.uid
      const v = document.createElement('span')
      v.className = 's-val'
      v.textContent = `${Math.round((Number(r.sec) || 0) / 60)} 分 · ${fmt(r.chars)} 字`
      const bar = document.createElement('span')
      bar.className = 's-bar'
      const fill = document.createElement('i')
      fill.style.width = (maxSec > 0 ? Math.max(2, Math.round((Number(r.sec) / maxSec) * 100)) : 0) + '%'
      bar.appendChild(fill)
      row.append(t, v, bar)
      body.appendChild(row)
    }
  }
  $('#stats-note').textContent = doc.updated_at
    ? '更新于 ' + new Date(Number(doc.updated_at) * 1000).toLocaleString('zh-CN')
    : ''
}

$('#stats-btn').addEventListener('click', () => { openStats().catch(() => {}) })
$('#stats-close').addEventListener('click', () => {
  $('#stats-pane').hidden = true
  show('shelf-pane')
  loadShelf().catch(() => {})
})

/* ── 上传书籍：内容寻址，服务器上有就跳过 ────────────────
   和桌面端走同一套接口：POST /v1/blob/check 问缺哪些 → PUT /v1/blob/<sha256> 传字节。
   文件名放 X-File-Name（百分号编码，Worker 会解），书架上就显示原名。
   传完刷新书架；第一次打开时 ensureTotal 再补总字数。 */

const BLOB_CT = {
  '.epub': 'application/epub+zip', '.mobi': 'application/x-mobipocket-ebook',
  '.azw3': 'application/vnd.amazon.ebook', '.fb2': 'application/x-fictionbook+xml',
  '.cbz': 'application/vnd.comicbook+zip',
}

async function sha256Hex(buf) {
  const dig = await crypto.subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(dig)).map((b) => b.toString(16).padStart(2, '0')).join('')
}

async function uploadOne(file) {
  const buf = await file.arrayBuffer()
  const sha = await sha256Hex(buf)
  const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase()
  const ct = BLOB_CT[ext] || 'application/octet-stream'
  const check = await req('/v1/blob/check', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ sha256: [sha] }),
  })
  const info = check.ok ? await check.json().catch(() => ({})) : {}
  if (Array.isArray(info.missing) && info.missing.includes(sha)) {
    const put = await req(`/v1/blob/${sha}`, {
      method: 'PUT',
      headers: { 'content-type': ct, 'x-file-name': encodeURIComponent(file.name) },
      body: buf,
    })
    if (!put.ok) throw new Error(`上传失败（${put.status}）`)
  }
  return sha
}

$('#upload-btn').addEventListener('click', () => $('#file').click())
$('#file').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length) return
  let ok = 0
  const failed = []
  for (let i = 0; i < files.length; i += 1) {
    try {
      toast(`正在上传 ${i + 1}/${files.length}：${files[i].name}`, 20000)
      await uploadOne(files[i])
      ok += 1
    } catch (err) {
      failed.push(`${files[i].name}（${err.message || err}）`)
    }
    toast(`正在上传 ${i + 1}/${files.length}：${files[i].name}`, 20000)
  }
  await loadShelf().catch(() => {})
  if (failed.length) toast(`传了 ${ok} 本，失败：${failed.join('；')}`, 6000)
  else toast(`传上去了 ${ok} 本`, 4000)
})

/* ── 起 ─────────────────────────────────────────────── */

loadLayout()
loadPending()      // 上次没推上去的会话，先把队列捞回来（进书架后补推）

boot().catch((e) => {
  console.error(e)
  toast('启动失败：' + (e.message || e))
  show('login')
})
