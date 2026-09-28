// Vestige 데스크탑 셸 (Electron)
// 파이썬 백엔드(FastAPI+fastembed) 사이드카를 spawn·감독 → 준비되면 창에 로드.
// 백엔드가 React 프론트를 / 에서 서빙하므로 창은 http://127.0.0.1:<port>/ 만 로드.
// 셸이 담당: 단일 인스턴스 · 동적 포트 · 크래시 자동 재시작 · 트레이 · 로그 캡처 · 자동 업데이트.

const { app, BrowserWindow, Tray, Menu, shell, nativeImage, nativeTheme, ipcMain, session } = require("electron")
const { spawn } = require("child_process")
const net = require("net")
const http = require("http")
const path = require("path")
const fs = require("fs")

let backend = null
let win = null
let tray = null
let port = 0
let quitting = false
let restarts = 0
let logStream = null

// 단일 인스턴스: 두 번째 실행은 기존 창을 띄우고 종료(포트 충돌·중복 백엔드 방지).
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on("second-instance", showWindow)
  app.whenReady().then(start)
}

// 상단 애플리케이션 메뉴(File/Edit/View…) 완전 제거 → 흰 메뉴 줄 사라짐.
Menu.setApplicationMenu(null)

// 렌더러가 preload로 보낸 테마값에 맞춰 네이티브 창(타이틀바 등) 색을 동기화.
// 창 backgroundColor 도 테마에 맞춘다 → 라이브 리사이즈 중 Chromium이 새 프레임 전에 잠깐
// 드러내는 여백/찢긴 영역이 테마색이라 '색 튐'이 사라진다(라이트=흰색, 다크=앱 배경).
ipcMain.on("cm-theme", (_e, mode) => {
  const m = mode === "light" || mode === "dark" ? mode : "system"
  nativeTheme.themeSource = m
  if (win && !win.isDestroyed()) {
    try { win.setBackgroundColor(m === "light" ? "#ffffff" : "#0c0d10") } catch (_) { /* 창 정리 중 — 무시 */ }
  }
})

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on("error", reject)
    srv.listen(0, "127.0.0.1", () => {
      const { port: p } = srv.address()
      srv.close(() => resolve(p))
    })
  })
}

// 개발: 저장소의 파이썬 진입점. 배포: 동봉된 PyInstaller exe.
function backendCommand(p) {
  if (!app.isPackaged) {
    const repo = path.resolve(__dirname, "..")
    return { cmd: "python", args: [path.join(repo, "packaging", "backend_entry.py"), String(p)], cwd: repo }
  }
  const exe = process.platform === "win32" ? "vestige-backend.exe" : "vestige-backend"
  const dir = path.join(process.resourcesPath, "backend")
  return { cmd: path.join(dir, exe), args: [String(p)], cwd: dir }
}

function logPath() {
  return path.join(app.getPath("userData"), "backend.log")
}

const RELEASES_URL = "https://github.com/flyingjoojak/vestige/releases/latest"

// 설치가 깨져 사이드카가 통째로 없는 경우(자동 업데이트가 폴더를 비우고 중단되면 실제로 생긴다).
// 창을 띄울 수 없으니 네이티브 대화상자로 알리고 설치본 받는 곳을 열어준다.
function showBrokenInstall(detail) {
  const { dialog } = require("electron")
  const r = dialog.showMessageBoxSync({
    type: "error",
    title: "Vestige",
    message: "설치가 손상되었습니다",
    detail: `${detail}

설치본을 다시 내려받아 설치해 주세요.
로그: ${logPath()}`,
    buttons: ["설치본 내려받기", "닫기"],
    defaultId: 0, cancelId: 1,
  })
  if (r === 0) { try { shell.openExternal(RELEASES_URL) } catch (_) { /* 브라우저 없음 */ } }
  quitting = true
  app.quit()
}

function spawnBackend() {
  const { cmd, args, cwd } = backendCommand(port)
  // 동봉 사이드카가 아예 없으면 spawn 이 ENOENT 로 터진다. 그대로 두면 Electron 이
  // "A JavaScript error occurred in the main process" 를 띄우고 끝나 사용자가 할 수 있는 게 없다.
  if (app.isPackaged && !fs.existsSync(cmd)) {
    showBrokenInstall(`백엔드 파일이 없습니다:
${cmd}`)
    return
  }
  // VESTIGE_MANAGED=1 → 백엔드가 뮤텍스·브라우저 자동열기·app.log를 끔(셸이 담당).
  const env = { ...process.env, VESTIGE_MANAGED: "1", VESTIGE_PORT: String(port) }
  backend = spawn(cmd, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true })
  try {
    logStream = logStream || fs.createWriteStream(logPath(), { flags: "a" })
    backend.stdout.pipe(logStream)
    backend.stderr.pipe(logStream)
  } catch (_) { /* 로그 실패해도 진행 */ }
  // spawn 실패(ENOENT/EACCES)는 exit 가 아니라 error 로 온다. 핸들러가 없으면 Node 가
  // uncaught exception 으로 올려 앱이 그 자리에서 죽는다 — 실제로 그렇게 죽었다(#229).
  backend.on("error", (err) => {
    backend = null
    if (quitting) return
    if (app.isPackaged && err && err.code === "ENOENT") {
      showBrokenInstall(`백엔드를 실행할 수 없습니다:
${cmd}`)
      return
    }
    try { fs.appendFileSync(logPath(), `
[spawn error] ${err && err.message}
`) } catch (_) { /* 무시 */ }
  })
  backend.on("exit", (code) => {
    backend = null
    if (quitting) return
    // 크래시 → 자동 재시작(무한 루프 방지: 최대 5회, 5초 뒤 카운터 리셋).
    if (restarts < 5) {
      restarts++
      setTimeout(() => { restarts = Math.max(0, restarts - 1) }, 30000)
      setTimeout(bootAndLoad, 1000)
    } else if (win) {
      win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
        `<body style="font-family:sans-serif;padding:40px;background:#0c0d10;color:#e9eaee">
         <h2>백엔드가 반복적으로 종료됐어요</h2>
         <p>로그를 확인해 주세요: ${logPath()}</p></body>`))
    }
  })
}

function waitReady(timeoutMs = 600000) {   // 첫 실행은 임베딩 모델(~2GB) 다운로드로 오래 걸림
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/api/stats", timeout: 2000 }, (res) => {
        res.resume()
        if (res.statusCode === 200) resolve()
        else retry()
      })
      req.on("error", retry)
      req.on("timeout", () => req.destroy())
    }
    const retry = () => {
      if (quitting) return
      if (Date.now() > deadline) reject(new Error("백엔드 시작 시간 초과"))
      else setTimeout(tryOnce, 500)
    }
    tryOnce()
  })
}

async function bootAndLoad() {
  if (!backend) spawnBackend()
  try {
    await waitReady()
    if (win && !win.isDestroyed()) {
      await win.loadURL(`http://127.0.0.1:${port}/`)
      // 뒤로가기(마우스 4번·Alt+←)로 시작 화면(loading.html)에 되돌아가 '엔진 불러오는 중'에
      // 갇히는 것 방지: 앱 URL 로드 후 내비게이션 히스토리를 비워 '뒤로 갈 대상'을 없앤다.
      try { win.webContents.navigationHistory.clear() }
      catch (_) { try { win.webContents.clearHistory() } catch (_2) { /* 구버전/미지원 — 무시 */ } }
    }
  } catch (e) {
    if (win && !win.isDestroyed()) {
      win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
        `<body style="font-family:sans-serif;padding:40px;background:#0c0d10;color:#e9eaee">
         <h2>백엔드를 시작하지 못했어요</h2><p>${e.message}</p>
         <p style="opacity:.6">로그: ${logPath()}</p></body>`))
    }
  }
}

function createWindow() {
  // SPA가 테마를 보고할 때까지의 첫 프레임 흰색 방지 → 기본 다크로 시작(theme.ts 기본과 일치).
  nativeTheme.themeSource = "dark"
  win = new BrowserWindow({
    width: 1280, height: 800, minWidth: 960, minHeight: 600,
    backgroundColor: "#0c0d10", title: "Vestige", show: false,
    autoHideMenuBar: true, icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      contextIsolation: true, nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  })
  win.setMenuBarVisibility(false)
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" } })
  // SPA라 페이지 이동이 없다 → 마우스 뒤로/앞으로 버튼(및 Alt+←/→)으로 인한 히스토리 이동을 차단.
  // 이게 없으면 뒤로가기가 시작 화면(loading.html)으로 돌아가 무한 '엔진 불러오는 중'에 갇힌다.
  win.on("app-command", (e, cmd) => {
    if (cmd === "browser-backward" || cmd === "browser-forward") e.preventDefault()
  })
  win.webContents.on("will-navigate", (e, url) => {
    // 앱 URL(그리고 그 하위) 로만 이동 허용. loading.html(file://)·외부로의 이동은 차단.
    if (!url.startsWith(`http://127.0.0.1:${port}/`)) e.preventDefault()
  })
  win.loadFile(path.join(__dirname, "loading.html"))
  win.show()
  // 창 닫기 = 트레이로 숨김(백그라운드 색인·동기 계속). 완전 종료는 트레이 메뉴.
  win.on("close", (e) => {
    if (quitting) return
    e.preventDefault()
    win.hide()
  })
}

function showWindow() {
  if (!win || win.isDestroyed()) { createWindow(); bootAndLoad() }
  else { win.show(); win.focus() }
}

function createTray() {
  try {
    const img = nativeImage.createFromPath(path.join(__dirname, "icon.png")).resize({ width: 16, height: 16 })
    tray = new Tray(img)
    tray.setToolTip("Vestige")
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: "열기", click: showWindow },
      { label: "백엔드 재시작", click: () => { killBackend(); restarts = 0; bootAndLoad() } },
      { type: "separator" },
      { label: "완전 종료", click: () => { quitting = true; killBackend(); app.quit() } },
    ]))
    tray.on("click", showWindow)
  } catch (_) { /* 트레이 실패해도 앱은 동작 */ }
}

function killBackend() {
  if (backend && !backend.killed) { try { backend.kill() } catch (_) {} backend = null }
}

// electron-updater releaseNotes 는 문자열 또는 [{version, note}] 배열 → 하나의 문자열로 정규화.
function normalizeNotes(notes) {
  if (!notes) return ""
  if (typeof notes === "string") return notes
  if (Array.isArray(notes)) {
    return notes.map((n) => (n && typeof n === "object" ? (n.note || "") : String(n))).join("\n\n")
  }
  return String(notes)
}

// 업데이트 상태 캐시 → 렌더러가 늦게 뜨거나(백엔드 크래시 재시작 등) 재요청하면 현재 단계를 재전송(레이스 방지).
let updInfo = null       // { version, releaseName, releaseNotes, releaseDate }
let updPhase = "idle"    // "available" | "downloading" | "downloaded"
let updPercent = 0
let downloading = false  // 다운로드 재진입 방지(중복 downloadUpdate 차단)

function sendToWin(channel, payload) {
  if (win && !win.isDestroyed() && win.webContents) {
    try { win.webContents.send(channel, payload) } catch (_) { /* 창 정리 중 — 무시 */ }
  }
}

// 현재 단계를 렌더러에 (재)전송. 렌더러가 마운트 직후 requestPending() 으로 이걸 부른다.
function replayUpdate() {
  if (updPhase === "available") sendToWin("cm-update-available", updInfo)
  else if (updPhase === "downloading") sendToWin("cm-update-progress", { percent: updPercent })
  else if (updPhase === "downloaded") sendToWin("cm-update-downloaded", { version: updInfo && updInfo.version })
}

function onAvailable(info) { updInfo = info; updPhase = "available"; downloading = false; replayUpdate() }
function onProgress(pct) { updPhase = "downloading"; downloading = true; updPercent = pct; sendToWin("cm-update-progress", { percent: pct }) }
function onDownloaded(version) { updPhase = "downloaded"; downloading = false; sendToWin("cm-update-downloaded", { version }) }
function onUpdError(message) {
  downloading = false
  if (updPhase === "downloading") updPhase = "available"   // 실패 시 재시도 가능 상태로 되돌림
  sendToWin("cm-update-error", { message: String(message) })
}

// 자동 업데이트: 자동 다운로드는 끄고(사용자 선택), 새 버전과 릴리스 노트를 렌더러에 알려 배너로 표시.
function setupAutoUpdate() {
  ipcMain.on("cm-update-request", replayUpdate)

  // 개발 미리보기: VESTIGE_FAKE_UPDATE=1 이면 가짜 흐름으로 배너 UI를 확인.
  if (!app.isPackaged) {
    if (!process.env.VESTIGE_FAKE_UPDATE) return
    onAvailable({
      version: process.env.VESTIGE_FAKE_UPDATE_VERSION || "9.9.9",
      releaseName: "미리보기 릴리스",
      releaseNotes: "새로운 기능\n- 업데이트 알림 배너 추가\n\nNew\n- Update notification banner",
      releaseDate: new Date().toISOString(),
    })
    ipcMain.on("cm-update-download", () => {
      if (downloading || updPhase === "downloaded") return
      let p = 0
      const timer = setInterval(() => {
        p += 25
        if (p >= 100) { clearInterval(timer); onProgress(100); onDownloaded(updInfo && updInfo.version) }
        else onProgress(p)
      }, 250)
    })
    ipcMain.on("cm-update-install", () => { /* 개발: 실제 설치 없음 */ })
    return
  }

  // macOS: 미서명 앱은 Squirrel.Mac 자동 적용이 불가(코드서명 필수). → 자동 교체 대신 '안내형 업데이트':
  // GitHub 릴리스에서 최신 버전을 직접 확인해 배너로 알리고, 사용자가 누르면 다운로드 페이지를 연다.
  // (서명 인증서가 준비되면 이 분기를 제거하고 아래 electron-updater 경로로 통일하면 된다.)
  if (process.platform === "darwin") {
    const REPO = "flyingjoojak/vestige"
    let dlUrl = `https://github.com/${REPO}/releases/latest`
    const httpsMod = require("https")
    // 리다이렉트는 최대 3회까지만 추적(주석과 구현 일치 + 리다이렉트 루프 방어).
    const getJson = (url, depth = 0) => new Promise((resolve, reject) => {
      httpsMod.get(url, { headers: { "User-Agent": "Vestige", Accept: "application/vnd.github+json" } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume()
          if (depth >= 3) { reject(new Error("리다이렉트 횟수 초과")); return }
          getJson(res.headers.location, depth + 1).then(resolve, reject); return
        }
        if (res.statusCode !== 200) { res.resume(); reject(new Error("HTTP " + res.statusCode)); return }
        let body = ""; res.on("data", (d) => (body += d))
        res.on("end", () => { try { resolve(JSON.parse(body)) } catch (e) { reject(e) } })
      }).on("error", reject)
    })
    // 단순 유의버전 비교(x.y.z, 프리릴리스 접미사는 무시) — 알림 목적엔 충분.
    const isNewer = (r, l) => {
      const nums = (v) => String(v).replace(/^v/, "").split("-")[0].split(".").map((x) => parseInt(x, 10) || 0)
      const a = nums(r), b = nums(l)
      for (let i = 0; i < 3; i++) { if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0) }
      return false
    }
    // shell.openExternal 은 스킴에 따라 로컬파일·커스텀 프로토콜 실행으로도 이어질 수 있는 민감 싱크.
    // 릴리스 파이프라인/계정 침해로 응답 URL이 변조돼도, 신뢰 호스트(https GitHub)만 열도록 화이트리스트.
    const openIfTrusted = (url) => {
      const ok = /^https:\/\/(github\.com|objects\.githubusercontent\.com|[a-z0-9-]+\.githubusercontent\.com)\//i.test(url)
      if (!ok) { console.error("update: 신뢰할 수 없는 다운로드 URL 차단:", url); return }
      shell.openExternal(url).catch((e) => console.error("update: openExternal 실패:", e))
    }
    ipcMain.on("cm-update-download", () => openIfTrusted(dlUrl))   // '다운로드 페이지 열기'
    ipcMain.on("cm-update-install", () => openIfTrusted(dlUrl))
    getJson(`https://api.github.com/repos/${REPO}/releases/latest`).then((rel) => {
      const remote = String(rel.tag_name || "").replace(/^v/, "")
      if (!remote || !isNewer(remote, app.getVersion())) return
      const dmg = (rel.assets || []).find((a) => /\.dmg$/i.test(a.name || ""))
      dlUrl = (dmg && dmg.browser_download_url) || rel.html_url || dlUrl
      onAvailable({
        version: remote,
        releaseName: rel.name || "",
        releaseNotes: rel.body || "",
        releaseDate: rel.published_at || "",
        assisted: true,   // 렌더러: 자동 다운로드 대신 '다운로드 페이지 열기' 버튼으로 표시
      })
    }).catch((e) => { console.error("update: macOS 업데이트 확인 실패:", e && e.message ? e.message : e) })
    return
  }

  let au
  try { au = require("electron-updater").autoUpdater } catch (_) { return } // 피드/모듈 없음 — 조용히 미동작
  au.autoDownload = false           // 사용자가 [지금 업데이트] 누를 때만 받음(업데이트는 선택)
  au.autoInstallOnAppQuit = true    // 받아뒀으면 종료 시 설치

  au.on("update-available", (info) => onAvailable({
    version: info.version,
    releaseName: info.releaseName || "",
    releaseNotes: normalizeNotes(info.releaseNotes),
    releaseDate: info.releaseDate || "",
  }))
  au.on("download-progress", (p) => onProgress(Math.round(p.percent || 0)))
  au.on("update-downloaded", (info) => onDownloaded(info.version))
  au.on("error", (err) => onUpdError((err && err.message) || err))

  ipcMain.on("cm-update-download", () => {
    if (downloading || updPhase === "downloaded") return   // 재진입 방지
    downloading = true
    updPhase = "downloading"; updPercent = 0
    Promise.resolve().then(() => au.downloadUpdate()).catch((e) => onUpdError((e && e.message) || e))
  })
  ipcMain.on("cm-update-install", () => {
    quitting = true
    try { au.quitAndInstall() } catch (e) { quitting = false; onUpdError((e && e.message) || e) }
  })

  au.checkForUpdates().catch(() => { /* 오프라인·피드 없음 — 조용히 */ })
}

// 업데이트 시 캐시 자동 청소: 앱 버전이 바뀌면(=업데이트됨) HTTP/코드 캐시를 1회 비운다.
// 백엔드 번들의 청크 해시가 바뀌므로, 캐시된 옛 index.html이 사라진 청크를 import하는
// "Failed to fetch dynamically imported module"를 원천 차단(사용자가 수동으로 캐시 지울 필요 없음).
async function clearCacheOnUpgrade() {
  try {
    const verFile = path.join(app.getPath("userData"), ".app-version")
    const cur = app.getVersion()
    let prev = null
    try { prev = fs.readFileSync(verFile, "utf-8").trim() } catch (_) { /* 최초 실행 */ }
    if (prev !== cur) {
      await session.defaultSession.clearCache()
      try { await session.defaultSession.clearCodeCaches({}) } catch (_) { /* 구버전 미지원 */ }
      try { fs.writeFileSync(verFile, cur) } catch (_) { /* 쓰기 실패해도 다음 실행에 재시도 */ }
    }
  } catch (_) { /* 캐시 청소 실패해도 앱 기동은 계속 */ }
}

async function start() {
  port = await findFreePort()
  await clearCacheOnUpgrade()
  createWindow()
  createTray()
  bootAndLoad()
  setupAutoUpdate()
  app.on("activate", showWindow)
}

app.on("before-quit", () => { quitting = true; killBackend() })
app.on("window-all-closed", () => { /* 트레이 상주 — 자동 종료 안 함 */ })
process.on("exit", killBackend)
