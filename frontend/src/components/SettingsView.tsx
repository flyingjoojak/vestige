import { useEffect, useRef, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  AlertTriangle, Check, Copy, Database, Loader2, Monitor, Moon,
  Plug, RefreshCw, SlidersHorizontal, Sparkles, Sun, X,
} from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { SegmentedRadioGroup } from "@/components/ui/SegmentedRadioGroup"
import { SchemaReportSection } from "@/components/SchemaReportSection"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  getConfig, getEmbedModels, getEnrichStatus, getIndexStatus, getMcp, getSkipSdkStats, getStats, getSyncStatus,
  archiveSync, getSyncthingStatus, getSystem, mcpRegister, mcpUnregister, putConfig, reindex, runEnrich, runIndex,
  syncthingPair, syncthingStart, syncthingStop, syncthingUnpair, toggleSource, verifyEnrich,
  type Config, type EmbedModel, type EnrichStatus, type IndexStatus, type McpTarget, type SyncStatus,
  type SyncthingStatus, type SyncthingSync, type SystemInfo,
} from "@/lib/api"
import type { Stats } from "@/lib/types"
import { errText } from "@/lib/errors"
import { type ThemeMode, getThemeMode, setThemeMode } from "@/lib/theme"
import { getLang, setLang, type Lang } from "@/lib/lang"

const BACKENDS = [
  { v: "claude", labelKey: "settings.backendClaude", key: null, keyEx: "", keyExKey: null, modelEnv: "VESTIGE_ENRICH_MODEL", models: ["sonnet", "opus", "haiku"] },
  { v: "anthropic", labelKey: "settings.backendAnthropic", key: "ANTHROPIC_API_KEY", keyEx: "sk-ant-api03-...", keyExKey: "settings.keyExAnthropic", modelEnv: "VESTIGE_ENRICH_API_MODEL", models: ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5"] },
  { v: "openai", labelKey: "settings.backendOpenai", key: "OPENAI_API_KEY", keyEx: "sk-... or sk-proj-...", keyExKey: "settings.keyExOpenai", modelEnv: "VESTIGE_OPENAI_MODEL", models: ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"] },
  { v: "gemini", labelKey: "settings.backendGemini", key: "GEMINI_API_KEY", keyEx: "AIza...", keyExKey: "settings.keyExGemini", modelEnv: "VESTIGE_GEMINI_MODEL", models: ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"] },
  { v: "ollama", labelKey: "settings.backendOllama", key: null, keyEx: "", keyExKey: null, modelEnv: "VESTIGE_OLLAMA_MODEL", models: ["llama3.1", "llama3.2", "qwen2.5", "mistral", "gemma2"] },
  { v: "off", labelKey: "settings.backendOff", key: null, keyEx: "", keyExKey: null, modelEnv: null, models: [] },
] as const
const CUSTOM = "__custom__"

type TabKey = "general" | "enrich" | "index" | "sync" | "mcp"
type IndexMode = "off" | "interval" | "realtime" | "scheduled"
const INDEX_MODES = ["off", "interval", "realtime", "scheduled"] as const

// 긴 HuggingFace 경로 대신 짧고 읽기 쉬운 이름으로.
function shortModelName(model: string): string {
  const base = model.split("/").pop() ?? model
  if (base.includes("e5-large")) return base.includes("int8") ? "e5-large int8" : "e5-large"
  if (base.toLowerCase().includes("minilm")) return "MiniLM L12 v2"
  return base
}
const TABS: { key: TabKey; labelKey: string; icon: React.ReactNode }[] = [
  { key: "general", labelKey: "settings.tabGeneral", icon: <SlidersHorizontal className="size-4" /> },
  { key: "enrich", labelKey: "settings.tabEnrich", icon: <Sparkles className="size-4" /> },
  { key: "index", labelKey: "settings.tabIndex", icon: <Database className="size-4" /> },
  { key: "sync", labelKey: "settings.tabSync", icon: <RefreshCw className="size-4" /> },
  { key: "mcp", labelKey: "settings.tabMcp", icon: <Plug className="size-4" /> },
]


function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-6">
      <h3 className="mb-1.5 text-sm font-medium text-muted-foreground">{title}</h3>
      <div className="rounded-xl border bg-card px-4 shadow-sm">{children}</div>
    </section>
  )
}
function Row({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b py-3.5 last:border-0">
      <span className="text-sm">{label}</span>
      <div className="flex items-center gap-2 text-sm">{children}</div>
    </div>
  )
}

// 상태 알약(초록=정상/주황=주의/회색=해당없음·확인중). 비개발자도 한눈에 상태만 보게.
function StatusChip({ tone, children }: { tone: "ok" | "warn" | "muted"; children: React.ReactNode }) {
  const cls = tone === "ok" ? "bg-primary/10 text-primary"
    // warn: 라이트 모드 대비 확보(amber-700 ≈ 4.5:1+), 다크는 amber-400 유지.
    : tone === "warn" ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
    : "bg-muted text-muted-foreground"
  return <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`}>{children}</span>
}

// 로그 폴더 지정: 평소엔 한 줄(이름 + 상태 + 「변경」)만. 「변경」을 눌러야 입력칸이 펼쳐진다
// → 화면 공간을 거의 안 쓰면서, 대부분(자동 감지된) 사용자는 상태만 확인하면 된다.
function FolderRow({ label, chip, path, onPathChange, onSave, saved, err, placeholder, help }: {
  label: string; chip: React.ReactNode; path: string; onPathChange: (v: string) => void
  onSave: () => void; saved: boolean; err?: string; placeholder: string; help: React.ReactNode
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const panelId = `folder-edit-${label.replace(/\s+/g, "-").toLowerCase()}`
  return (
    <div className="border-b py-3 last:border-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2 text-sm"><span className="font-medium">{label}</span>{chip}</span>
        <span className="flex items-center gap-2">
          {saved && <span className="inline-flex items-center gap-1 text-[12px] text-primary"><Check className="size-3.5" />{t("common.saved")}</span>}
          <Button variant="ghost" size="sm" onClick={() => setEditing((v) => !v)}
            aria-expanded={editing} aria-controls={panelId}>{editing ? t("common.close") : t("settings.changePath")}</Button>
        </span>
      </div>
      {editing && (
        <div id={panelId} className="mt-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Input value={path} onChange={(e) => onPathChange(e.target.value)} aria-label={t("settings.folderPathAria", { label })}
              className="h-8 min-w-0 flex-1 font-mono text-[12px]" placeholder={placeholder} />
            <Button size="sm" variant="outline" onClick={onSave}>{t("common.save")}</Button>
          </div>
          <p className="mt-1.5 text-[11px] text-muted-foreground">{help}</p>
          {err && <p role="alert" className="mt-1 text-[11px] text-destructive">{err}</p>}
        </div>
      )}
    </div>
  )
}

// MCP 연동: vestige-mcp를 각 클라이언트 설정에 등록/해제(파일은 .bak 백업 후 수정).
function McpSection() {
  const { t } = useTranslation()
  const [targets, setTargets] = useState<McpTarget[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [snip, setSnip] = useState<string | null>(null)
  const [err, setErr] = useState(false)   // 로드 실패 — 빈 화면 대신 재시도 노출

  // 성공 시 목록 갱신, 실패 시 err(단, 이미 목록이 있으면 백그라운드 실패는 무시하고 유지).
  const load = () => getMcp().then((r) => { setTargets(r.targets); setErr(false) }).catch(() => setErr(true))
  useEffect(() => { load() }, [])

  async function toggle(tgt: McpTarget) {
    const willRegister = !tgt.registered
    setBusy(tgt.id); setNote(null)
    try {
      const r = willRegister ? await mcpRegister(tgt.id) : await mcpUnregister(tgt.id)
      if (!r.ok) { setNote(`${tgt.label}: ${errText(t, r, "mcp.failed")}`); setSnip(tgt.id) }
      else {
        // 낙관적 반영: 즉시 등록/해제 상태로 바꿔 표시(느린 `claude mcp list` 재조회를 안 기다림).
        setTargets((ts) => ts?.map((x) => (x.id === tgt.id ? { ...x, registered: willRegister } : x)) ?? ts)
        setNote(willRegister ? t("mcp.registeredRestart", { label: tgt.label }) : t("mcp.unregistered", { label: tgt.label }))
      }
    } finally {
      setBusy(null)   // 스피너 즉시 종료(등록 subprocess만 끝나면 됨)
    }
    load()            // 실제 상태 재확인은 백그라운드(await 안 함 → 스피너에 안 걸림)
  }

  if (targets === null) {
    return err ? (
      <div className="flex flex-col items-center gap-2 py-6 text-center text-sm">
        <span className="inline-flex items-center gap-1.5 text-destructive"><AlertTriangle className="size-4" />{t("mcp.loadFailed")}</span>
        <span className="text-[12px] text-muted-foreground">{t("mcp.loadFailedHint")}</span>
        <button onClick={load} className="rounded-md border bg-card px-3 py-1.5 text-[13px] shadow-sm hover:text-foreground">{t("common.retry")}</button>
      </div>
    ) : (
      <Row label={t("common.loading")}><Loader2 className="size-4 animate-spin text-muted-foreground" /></Row>
    )
  }
  const active = targets.find((tgt) => tgt.id === snip)
  return (
    <>
      {targets.map((tgt) => (
        <Row key={tgt.id} label={
          <span className="flex items-center gap-2">
            {tgt.label}
            {tgt.registered
              ? <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{t("mcp.registered")}</span>
              : !tgt.installed && <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{t("mcp.notInstalled")}</span>}
          </span>
        }>
          <Button variant="ghost" size="sm" onClick={() => setSnip(snip === tgt.id ? null : tgt.id)}>{t("mcp.command")}</Button>
          <Button variant={tgt.registered ? "outline" : "default"} size="sm" busy={busy === tgt.id} onClick={() => toggle(tgt)}>
            {busy === tgt.id ? <Loader2 className="size-4 animate-spin" /> : tgt.registered ? t("mcp.unregister") : t("mcp.register")}
          </Button>
        </Row>
      ))}
      {active && (
        <div className="border-b py-3 last:border-0">
          <div className="mb-1 text-[11px] text-muted-foreground">{t("mcp.manualSnippet", { path: active.path })}</div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-md bg-muted p-2 text-[11px]">{active.snippet}</pre>
        </div>
      )}
      {note && <div className="py-3 text-xs text-muted-foreground">{note}</div>}
    </>
  )
}

// 동기화 충돌 자동 정리: 기기 간 동기화 중 생긴 충돌 파일을 자동 정리(긴 쪽 채택, 진짜 분기만 새 세션 보존).
// 자동 동기화 상태 + 수동 병합(진단). 충돌 정리·기기 간 병합은 '기기 연결'이 켜지면 자동 실행되므로
// 별도 토글은 없다. 여기서는 자동 실행 상태(해소 누계)를 보여주고, 필요 시 수동 병합만 제공한다.
function AutoSyncSection() {
  const { t } = useTranslation()
  const [sync, setSync] = useState<SyncStatus | null>(null)
  const [archiving, setArchiving] = useState(false)
  const [archiveMsg, setArchiveMsg] = useState<string | null>(null)

  useEffect(() => {
    const load = () => getSyncStatus().then(setSync).catch(() => setSync(null))
    load()
    const id = setInterval(load, 5000)   // 자동 정리 상태·해소 누계 주기 갱신
    return () => clearInterval(id)
  }, [])

  async function mergeNow() {
    setArchiving(true); setArchiveMsg(null)
    try {
      const r = await archiveSync()
      setArchiveMsg(r.imported > 0
        ? t("sync.mergeImported", { count: r.imported.toLocaleString() })
        : t("sync.mergeUpToDate"))
    } catch (e) { setArchiveMsg(errText(t, e, "sync.mergeFailed")) }
    finally { setArchiving(false) }
  }

  return (
    <>
      <Row label={
        <span className="flex flex-col">
          <span className="text-[13px]">{t("sync.mergeNow")}</span>
          <span className="text-[11px] text-muted-foreground">{t("sync.autoHelp")}</span>
        </span>
      }>
        <Button variant="outline" size="sm" busy={archiving} onClick={mergeNow}>
          {archiving && <Loader2 className="mr-1 size-4 animate-spin" />}{t("sync.mergeNow")}
        </Button>
      </Row>
      {(archiveMsg || sync?.running) && (
        <div className="py-1 text-[11px] text-muted-foreground">
          {archiveMsg ?? t("sync.resolvedStat", { count: sync!.resolved_total })}
          {sync?.last_error && <span className="text-destructive"> · {t("sync.errorInline", { error: sync.last_error })}</span>}
        </div>
      )}
    </>
  )
}

// 기기 연결(앱 내장 Syncthing) — 외부 프로그램 설치 없이 앱 안에서 기기 페어링.
function SyncthingSection() {
  const { t } = useTranslation()
  const [st, setSt] = useState<SyncthingStatus | null>(null)
  const [stErr, setStErr] = useState(false)   // 상태 조회 실패 — 무한 '확인 중' 대신 에러+재시도 표시
  const [busy, setBusy] = useState(false)
  const [peer, setPeer] = useState("")
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [unpairing, setUnpairing] = useState<{ id: string; name: string } | null>(null)   // 해제 확인 대기 기기
  const [unpairBusy, setUnpairBusy] = useState(false)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const progRef = useRef<{ key: string; ts: number }>({ key: "", ts: Date.now() })   // 동기 진척이 마지막으로 변한 시각(멈춤 감지용)

  const load = () => getSyncthingStatus().then((d) => { setSt(d); setStErr(false) }).catch(() => setStErr(true))
  useEffect(() => {
    load()
    const id = setInterval(load, 3000)
    return () => { clearInterval(id); if (copyTimer.current) clearTimeout(copyTimer.current) }
  }, [])

  const starting = !!st?.starting
  async function start() { setBusy(true); try { await syncthingStart() } catch { /* noop */ } finally { setBusy(false); load() } }
  async function stop() { setBusy(true); try { await syncthingStop() } catch { /* noop */ } finally { setBusy(false); load() } }
  async function copyMyId() {
    if (!st?.my_id || !navigator.clipboard?.writeText) return
    try {
      await navigator.clipboard.writeText(st.my_id)
      setCopied(true)
      if (copyTimer.current) clearTimeout(copyTimer.current)
      copyTimer.current = setTimeout(() => setCopied(false), 1500)
    } catch { /* noop */ }
  }
  async function pair() {
    const id = peer.trim()
    if (!id) return
    setBusy(true); setNote(null)
    try {
      const r = await syncthingPair(id)
      if (r.ok) { setNote({ ok: true, text: t("sync.pairSent") }); setPeer("") }
      else setNote({ ok: false, text: errText(t, r, "sync.pairFailed") })
      load()
    } catch (e) { setNote({ ok: false, text: errText(t, e, "sync.pairFailed") }) }
    finally { setBusy(false) }
  }
  async function doUnpair() {
    if (!unpairing) return
    setUnpairBusy(true); setNote(null)
    try {
      const r = await syncthingUnpair(unpairing.id)
      if (r.ok) setNote({ ok: true, text: t("sync.unpairDone") })
      else setNote({ ok: false, text: errText(t, r, "sync.unpairFailed") })
      load()
    } catch (e) { setNote({ ok: false, text: errText(t, e, "sync.unpairFailed") }) }
    finally { setUnpairBusy(false); setUnpairing(null) }
  }

  // 전송이 오래 진척 없으면 '멈춤'으로 안내(하드 취소 아님 — Syncthing 백그라운드 재시도는 유지).
  const _sync = st?.sync
  const syncPending = !!_sync && (_sync.state === "syncing" || _sync.need_items > 0 || _sync.need_bytes > 0
    || (_sync.remote_complete != null && _sync.remote_complete < 100))
  const syncKey = _sync ? `${_sync.completion}|${_sync.remote_complete ?? ""}|${_sync.need_items}|${_sync.peers_connected ?? 0}` : ""
  useEffect(() => {
    if (syncKey !== progRef.current.key) progRef.current = { key: syncKey, ts: Date.now() }
  }, [syncKey])
  const stalled = syncPending && Date.now() - progRef.current.ts > 60_000   // 60초 이상 무진척 = 멈춤

  const loading = st === null && !stErr   // 첫 응답 전(로딩) vs 조회 실패(stErr)를 구분 — 무한 스피너 방지
  const statusFailed = st === null && stErr
  const running = !!st?.running
  return (
    <>
      <Row label={
        <span className="flex items-center gap-2">
          {t("sync.deviceSync")}
          {loading
            ? <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{t("sync.checking")}</span>
            : statusFailed
              ? <span className="rounded-full bg-destructive/10 px-2 py-0.5 text-[10px] font-medium text-destructive">{t("sync.statusFailed")}</span>
              : running
                ? <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{t("sync.running")}</span>
                : <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{t("sync.stopped")}</span>}
        </span>
      }>
        {loading
          ? <Loader2 className="size-4 animate-spin text-muted-foreground" />
          : statusFailed
            ? <Button variant="outline" size="sm" onClick={load}>{t("common.retry")}</Button>
            : running
              ? <Button variant="outline" size="sm" busy={busy} onClick={stop}>{t("sync.stop")}</Button>
              : <Button size="sm" busy={busy || starting} onClick={start}>
                  {busy || starting ? <Loader2 className="size-4 animate-spin" /> : t("sync.start")}
                </Button>}
      </Row>
      {statusFailed && (
        <div className="py-2 text-[11px] text-destructive">{t("sync.statusFailedHint")}</div>
      )}

      {!loading && !running && (
        <div className="py-2 text-[11px] text-muted-foreground">
          {starting ? t("sync.engineStarting")
            : st?.last_error ? <span className="text-destructive">{t("sync.errorInline", { error: st.last_error })}</span>
              : t("sync.engineIdleHint")}
        </div>
      )}

      {running && (
        <>
          <SyncStateLine sync={st?.sync} stalled={stalled} />
          <div className="py-2">
            <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t("sync.myCodeLabel")}</div>
            <div className="flex items-center gap-1.5">
              <code className="min-w-0 flex-1 truncate rounded bg-muted px-1.5 py-1 font-mono text-[11px]" title={st?.my_id ?? ""}>{st?.my_id ?? "…"}</code>
              <button type="button" onClick={copyMyId} aria-label={t("sync.copyMyCodeAria")}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] hover:bg-muted">
                {copied ? <Check className="size-3.5 text-primary" /> : <Copy className="size-3.5" />}{copied ? t("sync.copied") : t("sync.copy")}
              </button>
            </div>
          </div>
          <div className="py-2">
            <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t("sync.pastePeerLabel")}</div>
            <div className="flex items-center gap-1.5">
              <Input value={peer} onChange={(e) => setPeer(e.target.value)} placeholder="XXXXXXX-XXXXXXX-…" className="h-8 min-w-0 flex-1 font-mono text-[12px]" />
              <Button size="sm" disabled={!peer.trim()} busy={busy} onClick={pair}>{t("sync.connect")}</Button>
            </div>
            {note && <div className={`mt-1 text-[11px] ${note.ok ? "text-primary" : "text-destructive"}`}>{note.text}</div>}
          </div>
          {st?.devices && st.devices.length > 0 && (
            <div className="py-2">
              <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t("sync.connectedDevices")}</div>
              {st.devices.map((d) => (
                <div key={d.id} className="flex items-center gap-2 py-0.5 text-[11px]">
                  <span className={`size-2 shrink-0 rounded-full ${d.connected ? "bg-primary" : "bg-muted-foreground/40"}`} />
                  <span className="truncate font-mono">{d.name || d.id.slice(0, 7)}</span>
                  <span className="text-muted-foreground">{d.connected ? t("sync.connected") : t("sync.waiting")}</span>
                  <button type="button" onClick={() => setUnpairing({ id: d.id, name: d.name || d.id.slice(0, 7) })}
                    className="ml-auto shrink-0 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-destructive">
                    {t("sync.unpair")}
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="py-2 text-[11px] leading-relaxed text-muted-foreground">
            <Trans i18nKey="sync.pairHelp" components={{ code: <code className="rounded bg-muted px-1" />, b: <b />, br: <br /> }} />
          </div>
        </>
      )}

      <AlertDialog open={!!unpairing} onOpenChange={(o) => !o && setUnpairing(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("sync.unpairConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              <Trans i18nKey="sync.unpairConfirmDesc" values={{ name: unpairing?.name ?? "" }} components={{ b: <b /> }} />
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={unpairBusy}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction disabled={unpairBusy} onClick={doUnpair}>{t("sync.unpair")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

// 항목별 실패 목록(색인/정제) — 조용히 스턱되는 항목을 사용자가 보게.
function Errs({ errors }: { errors?: string[] }) {
  const { t } = useTranslation()
  if (!errors || errors.length === 0) return null
  return (
    <div className="mt-2 rounded-md border border-destructive/30 bg-destructive/5 p-2 text-[11px] text-destructive">
      <div className="mb-1 flex items-center gap-1.5 font-medium"><AlertTriangle className="size-3.5" />{t("settings.errsSome", { count: errors.length })}</div>
      <ul className="list-disc space-y-0.5 pl-4">
        {errors.map((e, i) => <li key={i} className="truncate" title={e}>{e.replace(/^ERROR\s*/, "")}</li>)}
      </ul>
    </div>
  )
}

// 진행률 유틸 + 진행바(청크·파일 공용). ETA는 현재 모델 cps(청크/초)로 추정.
function pct(done: number, total: number) { return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0 }
function BarProgress({ done, total, unit, cps }: { done: number; total: number; unit: string; cps?: number }) {
  const { t } = useTranslation()
  const p = pct(done, total)
  let eta = ""
  if (cps && cps > 0 && total > 0 && done < total) {
    const sec = (total - done) / cps
    eta = sec < 60 ? t("settings.etaSec", { n: Math.ceil(sec) }) : t("settings.etaMin", { n: Math.ceil(sec / 60) })
  }
  return (
    <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={p} aria-label={unit}>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className="h-full origin-left rounded-full bg-primary transition-transform duration-300"
          style={{ transform: `scaleX(${p / 100})` }} />
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground tabular-nums">
        {done.toLocaleString()}/{total.toLocaleString()} {unit} ({p}%){eta ? ` · ${eta}` : ""}
      </div>
    </div>
  )
}

// 공유 폴더 동기 상태 한 줄. 수신(내가 받음)과 전송(상대가 받음)을 합쳐 '진짜 최신'을 판정.
function SyncStateLine({ sync, stalled = false }: { sync?: SyncthingSync | null; stalled?: boolean }) {
  const { t } = useTranslation()
  let dot = "bg-muted-foreground/40"
  let text: React.ReactNode = t("sync.folderPreparing")
  if (sync) {
    const receiving = sync.state === "syncing" || sync.need_items > 0 || sync.need_bytes > 0
    const sending = sync.remote_complete != null && sync.remote_complete < 100
    if (sync.state === "error") {
      dot = "bg-destructive"; text = <span className="text-destructive">{t("sync.stateError")}</span>
    } else if (sync.state === "scanning") {
      dot = "bg-amber-500"; text = t("sync.stateScanning")
    } else if (stalled) {
      // 전송 중인데 오래 진척 없음(상대 미연결 등) → '멈춤' 정직 안내. Syncthing 재시도는 유지.
      dot = "bg-destructive"; text = <span className="text-destructive">{t("sync.stalled")}</span>
    } else if (receiving) {
      dot = "bg-amber-500"
      text = <>{t("sync.receiving")} <b className="text-foreground tabular-nums">{sync.completion}%</b>{sync.need_items > 0 ? t("sync.remainingItems", { n: sync.need_items.toLocaleString() }) : ""}</>
    } else if (sending) {
      dot = "bg-amber-500"
      text = <>{t("sync.sending")} <b className="text-foreground tabular-nums">{sync.remote_complete}%</b></>
    } else if ((sync.peers_connected ?? 0) === 0) {
      // 이 기기는 최신이지만 상대가 연결 안 돼 있어 '양쪽 최신'은 확인 불가.
      dot = "bg-muted-foreground/40"; text = <>{t("sync.thisLatest")} · <span className="text-muted-foreground">{t("sync.peerOffline")}</span></>
    } else {
      dot = "bg-emerald-500"; text = <span className="text-emerald-600 dark:text-emerald-400">{t("sync.bothLatest")}</span>
    }
  }
  return (
    <div className="flex items-center gap-2 py-2 text-[11px] text-muted-foreground">
      <span className={`size-2 shrink-0 rounded-full ${dot}`} />
      <span className="font-medium text-foreground/80">{t("sync.stateLabel")}</span>
      <span>{text}</span>
    </div>
  )
}

// 색인 상태 행(프레젠테이션). 자동(프리즈) 또는 수동 색인이 돌 때 노출.
function AutoIndexRow({ ix }: { ix: IndexStatus | null }) {
  const { t } = useTranslation()
  if (!ix || (!ix.enabled && !ix.running)) return null
  return (
    <Row label={
      <span className="flex items-center gap-2">{t("settings.indexingLabel")}
        {ix.running
          ? <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{t("settings.inProgress")}</span>
          : <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{t("settings.waiting")}</span>}
      </span>
    }>
      <span className="text-xs text-muted-foreground tabular-nums">
        {ix.last_error ? t("settings.errorInline", { error: ix.last_error })
          : ix.external ? t("settings.indexingBackground")
          : ix.running && ix.total_chunks > 0 ? t("settings.selfHealChunks", { done: ix.done_chunks, total: ix.total_chunks, pct: pct(ix.done_chunks, ix.total_chunks) })
          : ix.running && ix.total_files > 0 ? t("settings.indexingFiles", { done: ix.done_files, total: ix.total_files })
          : ix.phase}
      </span>
    </Row>
  )
}

export function SettingsView() {
  const { t } = useTranslation()
  const [mode, setMode] = useState<ThemeMode>(getThemeMode())
  const [lang, setLangState] = useState<Lang>(getLang())
  const [stats, setStats] = useState<Stats | null>(null)
  const [cfg, setCfg] = useState<Config | null>(null)
  const [srcBusy, setSrcBusy] = useState<string | null>(null)   // 색인 소스 토글 진행 중인 소스명
  const [srcErr, setSrcErr] = useState<string | null>(null)
  const [backend, setBackend] = useState("claude")
  const [model, setModel] = useState("")
  const [customModel, setCustomModel] = useState(false)
  const [apiKey, setApiKey] = useState("")
  const [enrichTime, setEnrichTime] = useState("04:00")
  const [interval, setIntervalMin] = useState(10)
  const [indexMode, setIndexMode] = useState<IndexMode>("interval")
  const [indexTime, setIndexTime] = useState("03:00")       // scheduled 색인 시각
  const [ollamaUrl, setOllamaUrl] = useState("http://localhost:11434/v1")
  const [claudeBin, setClaudeBin] = useState("")   // claude CLI 경로 override(빈값=자동 탐색)
  const [skipSdk, setSkipSdk] = useState(true)     // 자동화(SDK/claude -p) 세션 제외 - 기본 켜짐
  const [skipSdkBusy, setSkipSdkBusy] = useState(false)
  const [skipSdkStats, setSkipSdkStats] = useState<{ sessions: number; turns: number } | null>(null)  // 제외 대상 규모(표기용)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [verify, setVerify] = useState<{ ok: boolean; msg: string } | null>(null)
  const [blockMsg, setBlockMsg] = useState("")
  const [projectsDir, setProjectsDir] = useState("")
  const [projSaved, setProjSaved] = useState(false)
  const [projErr, setProjErr] = useState("")
  const [codexDir, setCodexDir] = useState("")
  const [codexSaved, setCodexSaved] = useState(false)
  const [codexErr, setCodexErr] = useState("")
  const [tab, setTab] = useState<TabKey>("general")
  const [intervalSaved, setIntervalSaved] = useState(false)
  const [indexErr, setIndexErr] = useState("")
  const [rawMaxMb, setRawMaxMb] = useState("")   // 원본 로그 보존소 상한(MB, 빈값=무제한, #163)

  const [embed, setEmbed] = useState<EmbedModel[]>([])
  const [recommendedModel, setRecommendedModel] = useState("")
  const [reindexing, setReindexing] = useState(false)
  const [reindexMsg, setReindexMsg] = useState("")
  const [reindexErr, setReindexErr] = useState("")
  const [reindexProg, setReindexProg] = useState({ doneFiles: 0, totalFiles: 0, doneChunks: 0, totalChunks: 0 })
  const [fastReindex, setFastReindex] = useState(false)   // 병렬(고RAM 기기) 빠른 재색인
  const [parallelN, setParallelN] = useState(2)           // 병렬 프로세스 수(사용자 지정)
  const [sys, setSys] = useState<SystemInfo | null>(null) // 기기 메모리(병렬 권장치 계산용)
  const [confirmModel, setConfirmModel] = useState<EmbedModel | null>(null)
  const [ixStatus, setIxStatus] = useState<IndexStatus | null>(null)   // 증분 색인 상태(자동/수동)
  const [enrichSt, setEnrichSt] = useState<EnrichStatus | null>(null)   // 정제 상태
  const [enrichErr, setEnrichErr] = useState("")
  const poll = useRef<number | null>(null)
  const indexModeTimer = useRef<number | null>(null)   // 색인 모드 세그먼트 디바운스 커밋 타이머

  // 색인·정제 상태 폴링(진행 표시 + 버튼 비활성).
  useEffect(() => {
    let alive = true
    const load = () => {
      getIndexStatus().then((r) => alive && setIxStatus(r)).catch(() => {})
      getEnrichStatus().then((r) => alive && setEnrichSt(r)).catch(() => {})
      // 저장소 현황·JSONL 개수를 실시간 갱신(새로고침 없이). cfg는 setCfg만 → 폼 입력값은 안 건드림.
      getStats().then((r) => alive && setStats(r)).catch(() => {})
      getConfig().then((c) => alive && setCfg(c)).catch(() => {})
    }
    load()
    const id = window.setInterval(load, 3000)
    return () => { alive = false; window.clearInterval(id) }
  }, [])

  async function doRunIndex() {
    setIxStatus((s) => s ? { ...s, running: true, phase: t("settings.starting") } : s)
    try {
      const r = await runIndex()
      if (r && r.ok === false) {
        // busy = 에러가 아니라 "다른 색인 진행 중"(잠시 후 자가 교정). 실패 문구 대신 대기 안내.
        const phase = r.busy ? t("settings.indexingBackground") : errText(t, r, "settings.indexRunFailed")
        setIxStatus((s) => s ? { ...s, running: false, phase } : s)
      }
    } catch (e) {
      setIxStatus((s) => s ? { ...s, running: false, phase: errText(t, e, "settings.indexRunFailed") } : s)
    }
  }
  async function doEnrich() {
    setEnrichErr("")
    try {
      const r = await runEnrich(false)
      if (!r.ok) setEnrichErr(errText(t, r, "settings.enrichRunFailed"))
      else setEnrichSt((s) => ({ ...(s ?? { done_sessions: 0, total_sessions: 0, enriched: 0, last_error: null }), running: true, phase: t("settings.starting") }))
    } catch (e) { setEnrichErr(errText(t, e, "settings.enrichRunFailed")) }
  }

  // 색인 소스 on/off. 낙관적 반영(깜빡임 방지) → 서버 확정, 실패 시 되돌림 + 에러 표시.
  async function toggleSourceRow(name: string, targetEnabled: boolean) {
    if (srcBusy) return   // 진행 중이면 중복 클릭 무시
    setSrcBusy(name); setSrcErr(null)
    setCfg((c) => (c ? { ...c, sources: c.sources?.map((x) => (x.name === name ? { ...x, disabled: !targetEnabled } : x)) } : c))
    const label = name === "codex" ? "Codex" : name === "claude-code" ? "Claude Code" : name
    try {
      const r = await toggleSource(name, targetEnabled)
      if (!r.ok) throw new Error("toggle failed")
      const fresh = await getConfig(); setCfg(fresh)   // 서버 진실로 확정
    } catch {
      setSrcErr(t("settings.sourceToggleFailed", { label }))
      getConfig().then(setCfg).catch(() => {})          // 서버 상태로 되돌림
    } finally {
      setSrcBusy(null)
    }
  }

  // 자동화(SDK/claude -p) 세션 제외 토글. 다음 색인부터 반영(이미 색인된 데이터엔 소급 안 됨 - 데이터 폴더 초기화 후 재색인해야 제거).
  async function toggleSkipSdk(next: boolean) {
    if (skipSdkBusy) return
    setSkipSdkBusy(true); setSkipSdk(next)
    try {
      await putConfig({ VESTIGE_SKIP_SDK_SESSIONS: next ? "1" : "0" })   // 끌 땐 명시적 0(기본이 켜짐이라 빈값=켜짐)
      getConfig().then(setCfg).catch(() => {})
    } catch {
      setSkipSdk(!next)   // 실패 시 되돌림
    } finally {
      setSkipSdkBusy(false)
    }
  }

  useEffect(() => {
    getStats().then(setStats).catch(() => {})
    getSkipSdkStats().then((s) => setSkipSdkStats({ sessions: s.sessions, turns: s.turns })).catch(() => {})
    getConfig().then((c) => {
      setCfg(c); setBackend(c.enrich_backend); setEnrichTime(c.enrich_time)
      setIntervalMin(c.index_interval); setOllamaUrl(c.ollama_url); setProjectsDir(c.projects_dir); setCodexDir(c.codex_dir)
      setClaudeBin(c.claude_bin ?? "")
      setSkipSdk(!!c.skip_sdk)
      setIndexMode((c.index_mode as IndexMode) || "interval"); setIndexTime(c.index_time || "03:00")
      setRawMaxMb(c.raw_archive_max_mb || "")
      const cur = c.models[c.enrich_backend] ?? ""
      const opts = BACKENDS.find((b) => b.v === c.enrich_backend)?.models ?? []
      setModel(cur); setCustomModel(!!cur && !(opts as readonly string[]).includes(cur))
    }).catch(() => {})
    loadEmbed()
    getSystem().then(setSys).catch(() => {})
    return () => {
      if (poll.current) window.clearInterval(poll.current)
      if (indexModeTimer.current) window.clearTimeout(indexModeTimer.current)
    }
  }, [])

  function loadEmbed() {
    getEmbedModels().then((r) => {
      setEmbed(r.models); setRecommendedModel(r.recommended); setReindexing(r.reindex.running); setReindexMsg(r.reindex.msg)
      setReindexProg({ doneFiles: r.reindex.done_files, totalFiles: r.reindex.total_files, doneChunks: r.reindex.done_chunks, totalChunks: r.reindex.total_chunks })
      if (r.reindex.running && !poll.current) startPoll()
    }).catch(() => {})
  }
  function startPoll() {
    poll.current = window.setInterval(async () => {
      const r = await getEmbedModels()
      setEmbed(r.models); setReindexMsg(r.reindex.msg)
      setReindexProg({ doneFiles: r.reindex.done_files, totalFiles: r.reindex.total_files, doneChunks: r.reindex.done_chunks, totalChunks: r.reindex.total_chunks })
      if (!r.reindex.running) {
        setReindexing(false)
        if (poll.current) { window.clearInterval(poll.current); poll.current = null }
      }
    }, 2000)
  }

  const be = BACKENDS.find((b) => b.v === backend)!

  function onBackendChange(v: string) {
    setBackend(v)
    const cur = cfg?.models[v] ?? ""
    const opts = BACKENDS.find((b) => b.v === v)?.models ?? []
    setModel(cur); setCustomModel(!!cur && !(opts as readonly string[]).includes(cur))
  }

  async function runTest() {
    setTesting(true); setVerify(null); setBlockMsg("")
    try {
      const r = await verifyEnrich({
        backend, model, api_key: apiKey || undefined,
        ollama_url: backend === "ollama" ? ollamaUrl : undefined,
      })
      setVerify({ ok: r.ok, msg: r.message }); return r.ok
    } catch (e) {
      setVerify({ ok: false, msg: String(e) }); return false
    } finally { setTesting(false) }
  }

  async function commitSave() {
    const u: Record<string, string> = {
      VESTIGE_ENRICH_BACKEND: backend,
      VESTIGE_ENRICH_TIME: enrichTime,
    }
    if (be.modelEnv && model) u[be.modelEnv] = model
    if (be.key && apiKey) u[be.key] = apiKey
    if (backend === "ollama" && ollamaUrl) u.VESTIGE_OLLAMA_URL = ollamaUrl
    if (backend === "claude") u.VESTIGE_CLAUDE_BIN = claudeBin.trim()   // 빈값=자동 탐색으로 되돌림
    try {
      const r = await putConfig(u)
      if (!r.ok) { setBlockMsg(errText(t, r, "settings.saveFailed")); return }
    } catch (e) { setBlockMsg(errText(t, e, "settings.saveFailed")); return }
    setApiKey(""); setVerify(null); setBlockMsg(""); setSaved(true); setTimeout(() => setSaved(false), 1800)
    getConfig().then(setCfg).catch(() => {})
  }

  async function save() {
    setBlockMsg("")
    // 키가 필요한 백엔드인데 입력도 없고 저장된 것도 없으면 → 저장 차단.
    if (be.key && !apiKey && !cfg?.keys[be.key]) {
      setBlockMsg(t("settings.needApiKey"))
      return
    }
    // 검증 통과해야 저장. 실패 시 경고만 두고 '그래도 저장'으로 강제 가능.
    if (await runTest()) await commitSave()
  }

  async function saveProjects() {
    setProjErr("")
    try {
      const r = await putConfig({ CLAUDE_PROJECTS_DIR: projectsDir })
      if (!r.ok) { setProjErr(errText(t, r, "settings.saveFailed")); return }
    } catch (e) { setProjErr(errText(t, e, "settings.saveFailed")); return }
    setProjSaved(true); setTimeout(() => setProjSaved(false), 1800)
    getConfig().then(setCfg).catch(() => {})
  }

  async function saveCodex() {
    setCodexErr("")
    try {
      const r = await putConfig({ CODEX_SESSIONS_DIR: codexDir })
      if (!r.ok) { setCodexErr(errText(t, r, "settings.saveFailed")); return }
    } catch (e) { setCodexErr(errText(t, e, "settings.saveFailed")); return }
    setCodexSaved(true); setTimeout(() => setCodexSaved(false), 1800)
    getConfig().then(setCfg).catch(() => {})
  }

  // 저장 실패 시 서버 진실로 되돌림(낙관적으로 바꾼 indexMode가 미저장 상태로 남지 않게).
  function resyncIndex() {
    getConfig().then((c) => { setCfg(c); setIndexMode((c.index_mode as IndexMode) || "interval") }).catch(() => {})
  }
  function clearIndexTimer() {
    if (indexModeTimer.current) { window.clearTimeout(indexModeTimer.current); indexModeTimer.current = null }
  }
  async function commitIndex(updates: Record<string, string>) {
    setIndexErr("")
    try {
      const r = await putConfig(updates)
      if (!r.ok) { setIndexErr(errText(t, r, "settings.saveFailed")); resyncIndex(); return }
    } catch (e) { setIndexErr(errText(t, e, "settings.saveFailed")); resyncIndex(); return }
    setIntervalSaved(true); setTimeout(() => setIntervalSaved(false), 1800)
    getConfig().then(setCfg).catch(() => {})
  }
  // interval/time "저장" 버튼용: 세 값 함께 저장. 대기 중인 디바운스 커밋은 취소(중복 방지).
  async function saveIndex(mode: IndexMode = indexMode) {
    clearIndexTimer()
    setIndexMode(mode)
    await commitIndex({ VESTIGE_INDEX_MODE: mode, VESTIGE_INDEX_INTERVAL: String(interval), VESTIGE_INDEX_TIME: indexTime })
  }
  // 색인 모드 세그먼트: 표시(선택)는 즉시, 백엔드 커밋은 디바운스 —
  // radiogroup 화살표 이동이 키 입력마다 putConfig를 쏘지 않게(WCAG 3.2.2). 모드만 저장해
  // interval/time을 stale 값으로 덮어쓰지 않게 한다(각 필드는 자기 저장 버튼이 담당).
  function onIndexModeChange(m: IndexMode) {
    setIndexMode(m); setIndexErr("")
    clearIndexTimer()
    indexModeTimer.current = window.setTimeout(() => { indexModeTimer.current = null; commitIndex({ VESTIGE_INDEX_MODE: m }) }, 400)
  }

  async function doReindex() {
    if (!confirmModel) return
    const m = confirmModel.model
    const fast = fastReindex
    setConfirmModel(null); setReindexErr(""); setReindexing(true); setReindexMsg(t("settings.starting"))
    try {
      const r = await reindex(m, fast ? { fast, parallel: parallelN } : {})
      if (!r.ok) { setReindexing(false); setReindexErr(errText(t, r, "settings.reindexFailed")); return }
      startPoll()
    } catch (e) {
      setReindexing(false); setReindexErr(errText(t, e, "settings.reindexFailed"))
    }
  }

  const themes: { v: ThemeMode; icon: React.ReactNode; labelKey: string }[] = [
    { v: "light", icon: <Sun className="size-4" />, labelKey: "settings.themeLight" },
    { v: "dark", icon: <Moon className="size-4" />, labelKey: "settings.themeDark" },
    { v: "system", icon: <Monitor className="size-4" />, labelKey: "settings.themeSystem" },
  ]
  const langs: Lang[] = ["ko", "en"]

  // 대기 집계 표시용 문구.
  const p = ixStatus?.pending
  const pendingParts = p && p.files > 0
    ? [p.new_sessions ? t("settings.pendingNew", { n: p.new_sessions }) : "", p.updated_sessions ? t("settings.pendingUpdated", { n: p.updated_sessions }) : ""]
        .filter(Boolean).join(" · ")
    : ""
  const idxPendingText = p && p.files > 0 ? t("settings.pendingWaiting", { items: pendingParts }) : t("settings.allIndexed")
  const enrichPending = enrichSt?.pending_turns ?? 0
  const curCps = embed.find((e) => e.current)?.cps   // 현재 모델 처리량(청크/초) — ETA 추정용

  return (
    <div className="mx-auto max-w-3xl px-6 py-5">
      <h2 className="mb-4 text-lg font-semibold">{t("settings.title")}</h2>

      <div className="flex flex-col gap-5 md:flex-row md:items-start">
        {/* 큰 메뉴 네비게이션 — 넓은 화면=좌측 세로, 좁은 화면=상단 가로 스크롤 */}
        <nav aria-label={t("settings.menuAria")}
          className="flex shrink-0 gap-1 overflow-x-auto pb-1 md:w-52 md:flex-col md:overflow-visible md:pb-0">
          {TABS.map((item) => (
            <button key={item.key} type="button" onClick={() => setTab(item.key)}
              aria-current={tab === item.key ? "page" : undefined}
              className={`inline-flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-left text-sm leading-tight transition-colors ${
                tab === item.key ? "bg-primary/10 font-semibold text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              <span className="shrink-0">{item.icon}</span>{t(item.labelKey)}
            </button>
          ))}
        </nav>

        <div className="min-w-0 flex-1">
          {/* ── 일반: 테마 + 로그 폴더 + 저장소 현황 ── */}
          {tab === "general" && (
            <>
              <Section title={t("settings.appearance")}>
                <Row label={t("settings.theme")}>
                  <SegmentedRadioGroup
                    label={t("settings.theme")}
                    value={mode}
                    onChange={(v) => { setMode(v); setThemeMode(v) }}
                    options={themes.map((th) => ({ value: th.v, label: <>{th.icon}{t(th.labelKey)}</> }))}
                  />
                </Row>
                <Row label={t("settings.language")}>
                  <SegmentedRadioGroup
                    label={t("settings.language")}
                    value={lang}
                    onChange={(v) => { setLang(v); setLangState(v) }}
                    options={langs.map((l) => ({ value: l, label: l === "ko" ? t("settings.langKo") : t("settings.langEn") }))}
                  />
                </Row>
              </Section>

              <Section title={t("settings.storageStatus")}>
                <Row label={t("settings.sessions")}>{stats?.sessions ?? "—"}{t("settings.countSuffix")}</Row>
                <Row label={t("settings.turns")}>{stats?.turns ?? "—"}{t("settings.countSuffix")}</Row>
                <Row label={t("settings.vectors")}>{stats?.vectors ?? "—"}{t("settings.countSuffix")}</Row>
                <Row label={t("settings.enrichedDone")}>{stats?.enriched ?? "—"}{t("settings.countSuffix")}</Row>
                <AutoIndexRow ix={ixStatus} />
              </Section>

              <Section title={t("settings.indexSources")}>
                <div className="space-y-1.5 py-2">
                  {(cfg?.sources ?? []).map((s) => {
                    const enabled = !s.disabled
                    const on = s.exists && enabled            // 실제 색인 중(색상·배지 일관)
                    const status = !s.exists ? t("settings.folderMissing") : s.disabled ? t("common.off") : t("settings.indexingCount", { count: s.count })
                    const label = s.name === "codex" ? "Codex CLI" : s.name === "claude-code" ? "Claude Code" : s.name
                    const busy = srcBusy === s.name
                    return (
                      <div key={s.name} className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium">{label}</span>
                        <span id={`src-status-${s.name}`}
                          className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${on ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{status}</span>
                        <code className="cm-inline min-w-0 flex-1 truncate text-[11px] text-muted-foreground">{s.root ?? "—"}</code>
                        <button type="button" role="switch" aria-checked={enabled}
                          disabled={!s.exists} aria-disabled={busy || undefined} aria-busy={busy || undefined}
                          aria-label={t("settings.sourceToggleAria", { label, action: enabled ? t("sync.turnOff") : t("sync.turnOn") })}
                          aria-describedby={`src-status-${s.name}`}
                          onClick={() => { if (busy) return; toggleSourceRow(s.name, !enabled) }}
                          className={`ml-auto inline-flex h-6 shrink-0 items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 aria-disabled:opacity-50 aria-disabled:pointer-events-none ${enabled ? "border-primary/40 bg-primary/10 text-primary hover:bg-primary/15" : "border-border text-muted-foreground hover:bg-muted"}`}>
                          {busy && <Loader2 className="size-3 animate-spin" />}{enabled ? t("common.on") : t("common.off")}
                        </button>
                      </div>
                    )
                  })}
                  {srcErr && <div className="text-[11px] text-destructive">{srcErr}</div>}
                  {(cfg?.sources ?? []).length > 0 && (cfg?.sources ?? []).every((x) => !x.exists || x.disabled) && (
                    <div className="flex items-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                      <AlertTriangle className="size-3.5 shrink-0" />{t("settings.allSourcesOff")}
                    </div>
                  )}
                  <p className="text-xs text-muted-foreground">
                    <Trans i18nKey="settings.sourcesNote" components={{ b: <b /> }} />
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-2 border-t pt-2.5 text-sm">
                    <span className="font-medium">{t("settings.skipSdk")}</span>
                    <button type="button" role="switch" aria-checked={skipSdk} aria-disabled={skipSdkBusy || undefined} aria-busy={skipSdkBusy || undefined}
                      aria-label={t("settings.skipSdk")} aria-describedby="skip-sdk-help"
                      onClick={() => { if (skipSdkBusy) return; toggleSkipSdk(!skipSdk) }}
                      className={`ml-auto inline-flex h-6 shrink-0 items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 aria-disabled:opacity-50 aria-disabled:pointer-events-none ${skipSdk ? "border-primary/40 bg-primary/10 text-primary hover:bg-primary/15" : "border-border text-muted-foreground hover:bg-muted"}`}>
                      {skipSdkBusy && <Loader2 className="size-3 animate-spin" />}{skipSdk ? t("common.on") : t("common.off")}
                    </button>
                  </div>
                  <p id="skip-sdk-help" className="text-[11px] text-muted-foreground">{t("settings.skipSdkHelp")}</p>
                  {skipSdkStats && skipSdkStats.turns > 0 && (
                    <p className="text-[11px] text-muted-foreground tabular-nums">
                      {t(skipSdk ? "settings.skipSdkCountOn" : "settings.skipSdkCountOff", { sessions: skipSdkStats.sessions.toLocaleString(), turns: skipSdkStats.turns.toLocaleString() })}
                    </p>
                  )}
                  {/* 원본 로그 보존소(#163): 정리로 사라져도 복구 가능하게 별도 보관 중인 원본의 용량·상한 */}
                  <div className="mt-1 flex flex-wrap items-center gap-2 border-t pt-2.5 text-sm">
                    <span className="font-medium">{t("settings.rawArchive")}</span>
                    <span className="text-[11px] tabular-nums text-muted-foreground">
                      {((cfg?.raw_archive_bytes ?? 0) / 1024 / 1024).toFixed(1)} MB
                    </span>
                    <Input type="number" min={0} placeholder={t("settings.rawArchiveUnlimited")}
                      value={rawMaxMb} onChange={(e) => setRawMaxMb(e.target.value)}
                      aria-label={t("settings.rawArchiveMaxMb")}
                      onWheel={(e) => (e.target as HTMLInputElement).blur()}
                      className="ml-auto h-8 w-24 tabular-nums" />
                    <span className="text-[11px] text-muted-foreground">MB</span>
                    <Button size="sm" variant="outline" onClick={() => commitIndex({ VESTIGE_RAW_ARCHIVE_MAX_MB: rawMaxMb.trim() })}>{t("common.save")}</Button>
                    {intervalSaved && <span className="inline-flex items-center gap-1 text-sm text-primary"><Check className="size-4" />{t("common.saved")}</span>}
                  </div>
                  <p className="text-[11px] text-muted-foreground">{t("settings.rawArchiveHelp")}</p>
                </div>
              </Section>

              <Section title={t("settings.logFolder")}>
                <FolderRow
                  label="Claude Code"
                  chip={!cfg
                    ? <StatusChip tone="muted"><Loader2 className="size-3 animate-spin" />{t("settings.checking")}</StatusChip>
                    : cfg.projects_exists
                      ? <StatusChip tone="ok"><Check className="size-3" />{t("settings.conversationsDetected", { count: cfg.jsonl_count })}</StatusChip>
                      : <StatusChip tone="warn"><AlertTriangle className="size-3" />{t("settings.folderMissing")}</StatusChip>}
                  path={projectsDir} onPathChange={setProjectsDir} onSave={saveProjects} saved={projSaved} err={projErr}
                  placeholder="~/.claude/projects"
                  help={t("settings.claudeFolderHelp")}
                />
                <FolderRow
                  label="Codex"
                  chip={!cfg
                    ? <StatusChip tone="muted"><Loader2 className="size-3 animate-spin" />{t("settings.checking")}</StatusChip>
                    : cfg.codex_exists
                      ? <StatusChip tone="ok"><Check className="size-3" />{t("settings.conversationsDetected", { count: (cfg.sources ?? []).find((s) => s.name === "codex")?.count ?? 0 })}</StatusChip>
                      : <StatusChip tone="muted">{t("settings.notUsed")}</StatusChip>}
                  path={codexDir} onPathChange={setCodexDir} onSave={saveCodex} saved={codexSaved} err={codexErr}
                  placeholder="~/.codex/sessions"
                  help={t("settings.codexFolderHelp")}
                />
              </Section>

              <SchemaReportSection />
            </>
          )}

          {/* ── 정제 AI: 백엔드/모델/키/시각 + 저장 + 지금 정제 ── */}
          {tab === "enrich" && (
            <>
              <Section title={t("settings.tabEnrich")}>
                <Row label={t("settings.backend")}>
                  <select value={backend} onChange={(e) => onBackendChange(e.target.value)} aria-label={t("settings.backendAria")}
                    className="rounded-md border bg-background px-2 py-1.5 outline-none">
                    {BACKENDS.map((b) => <option key={b.v} value={b.v}>{t(b.labelKey)}</option>)}
                  </select>
                </Row>
                {be.modelEnv && (
                  <Row label={t("settings.model")}>
                    <select value={customModel ? CUSTOM : model} aria-label={t("settings.modelAria")}
                      onChange={(e) => { if (e.target.value === CUSTOM) { setCustomModel(true) } else { setCustomModel(false); setModel(e.target.value) } }}
                      className="rounded-md border bg-background px-2 py-1.5 outline-none">
                      {be.models.map((m) => <option key={m} value={m}>{m}</option>)}
                      <option value={CUSTOM}>{t("settings.modelCustom")}</option>
                    </select>
                    {customModel && (
                      <Input value={model} onChange={(e) => setModel(e.target.value)} className="h-8 w-44" placeholder={t("settings.modelPlaceholder")} />
                    )}
                  </Row>
                )}
                {be.key && (
                  <Row label={`${t("settings.apiKey")} ${cfg?.keys[be.key] ? t("settings.apiKeySet") : ""}`}>
                    <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} aria-label={t("settings.apiKey")}
                      className="h-8 w-56" placeholder={cfg?.keys[be.key] ? t("settings.apiKeyChangePlaceholder") : (be.keyExKey ? t(be.keyExKey) : "")} />
                  </Row>
                )}
                {backend === "claude" && (
                  <>
                    <Row label={t("settings.claudeBin")}>
                      <Input value={claudeBin} onChange={(e) => setClaudeBin(e.target.value)} aria-label={t("settings.claudeBin")}
                        className="h-8 w-72" placeholder={t("settings.claudeBinPlaceholder")} />
                    </Row>
                    <div className="border-b py-2 text-xs last:border-0">
                      {cfg?.claude_found
                        ? <span className="inline-flex items-center gap-1 text-primary"><Check className="size-3" />{t("settings.claudeBinFound", { path: cfg.claude_resolved })}</span>
                        : <span className="inline-flex items-center gap-1 text-destructive"><AlertTriangle className="size-3" />{t("settings.claudeBinNotFound")}</span>}
                      <div className="mt-1 text-muted-foreground">{t("settings.claudeBinHint")}</div>
                    </div>
                  </>
                )}
                {backend === "ollama" && (
                  <>
                    <Row label={t("settings.ollamaUrl")}>
                      <Input value={ollamaUrl} onChange={(e) => setOllamaUrl(e.target.value)} aria-label={t("settings.ollamaUrl")}
                        className="h-8 w-56" placeholder="http://localhost:11434/v1" />
                    </Row>
                    <div className="border-b py-3 text-xs text-muted-foreground last:border-0">
                      <Trans i18nKey="settings.ollamaHint" values={{ model: model || "llama3.1" }} components={{ code: <code className="cm-inline" /> }} />
                    </div>
                  </>
                )}
                <Row label={t("settings.enrichTime")}>
                  <Input type="time" value={enrichTime} onChange={(e) => setEnrichTime(e.target.value)} className="h-8 w-32 tabular-nums" aria-label={t("settings.enrichTime")} />
                </Row>
              </Section>

              <div className="mb-2 flex flex-wrap items-center gap-3">
                {backend !== "off" && (
                  <Button variant="outline" onClick={runTest} busy={testing}>
                    {testing ? <><Loader2 className="size-4 animate-spin" />{t("settings.testing")}</> : t("settings.testConnection")}
                  </Button>
                )}
                <Button onClick={save} busy={testing}>{t("common.save")}</Button>
                {saved && <span className="inline-flex items-center gap-1 text-sm text-primary"><Check className="size-4" />{t("settings.savedScheduled")}</span>}
              </div>
              {blockMsg && (
                <div className="mb-4 flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                  <AlertTriangle className="size-4 shrink-0" />{blockMsg}
                </div>
              )}
              {verify && (
                <div className={`mb-4 rounded-lg border px-3 py-2 text-sm ${verify.ok ? "border-primary/40 bg-primary/5 text-primary" : "border-destructive/40 bg-destructive/5 text-destructive"}`}>
                  <div className="flex items-center gap-2">
                    {verify.ok ? <Check className="size-4 shrink-0" /> : <X className="size-4 shrink-0" />}
                    {verify.ok ? t("settings.connOk") : t("settings.connFail", { msg: verify.msg })}
                  </div>
                  {!verify.ok && (
                    <div className="mt-2 flex items-center gap-2">
                      <Button size="sm" variant="outline" onClick={commitSave}>{t("settings.saveAnyway")}</Button>
                      <span className="text-xs text-muted-foreground">{t("settings.saveAnywayHint")}</span>
                    </div>
                  )}
                </div>
              )}
              {/* 수동 정제: 아직 요약·태그 없는 턴을 지금 정제(대기 수·진행바 표시) */}
              <div className="mb-3 border-t py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" disabled={backend === "off"} busy={!!enrichSt?.running} onClick={doEnrich}>
                    {enrichSt?.running && <Loader2 className="mr-1 size-4 animate-spin" />}{t("settings.enrichNow")}
                  </Button>
                  <span role="status" aria-live="polite" className={`text-[11px] ${enrichErr && !enrichSt?.running ? "text-destructive" : "text-muted-foreground"}`}>
                    {enrichSt?.running
                      ? (enrichSt.total_sessions > 0 ? t("settings.enrichingSessions", { done: enrichSt.done_sessions, total: enrichSt.total_sessions }) : t("settings.enrichingPhase", { phase: enrichSt.phase }))
                      : enrichErr
                        ? enrichErr
                        : <b className="text-foreground">{enrichPending > 0 ? t("settings.enrichPending", { n: enrichPending }) : t("settings.enrichUpToDate")}</b>}
                  </span>
                </div>
                {enrichSt?.running && enrichSt.total_sessions > 0 && (
                  <div className="mt-2">
                    <BarProgress done={enrichSt.done_sessions} total={enrichSt.total_sessions} unit={t("settings.unitSessions")} />
                  </div>
                )}
                <Errs errors={enrichSt?.errors} />
              </div>
            </>
          )}

          {/* ── 색인·임베딩: 증분 간격 + 증분 색인(대기 수) + 임베딩 모델/재색인 ── */}
          {tab === "index" && (
            <>
              <Section title={t("settings.incrementalIndex")}>
                <Row label={t("settings.indexMode")}>
                  <SegmentedRadioGroup
                    label={t("settings.indexMode")}
                    value={indexMode}
                    onChange={onIndexModeChange}
                    options={INDEX_MODES.map((m) => ({ value: m, label: t(`settings.indexMode_${m}`) }))}
                  />
                </Row>
                {indexErr && <Row label=""><span aria-live="polite" className="text-sm text-destructive">{indexErr}</span></Row>}
                {indexMode === "interval" && (
                  <Row label={t("settings.indexInterval")}>
                    <Input type="number" min={1} value={interval} onChange={(e) => setIntervalMin(+e.target.value)} aria-label={t("settings.indexInterval")}
                      onWheel={(e) => (e.target as HTMLInputElement).blur()} className="h-8 w-24 tabular-nums" />
                    <Button size="sm" variant="outline" onClick={() => saveIndex()}>{t("common.save")}</Button>
                    {intervalSaved && <span className="inline-flex items-center gap-1 text-sm text-primary"><Check className="size-4" />{t("common.saved")}</span>}
                  </Row>
                )}
                {indexMode === "scheduled" && (
                  <Row label={t("settings.indexTime")}>
                    <Input type="time" value={indexTime} onChange={(e) => setIndexTime(e.target.value)} aria-label={t("settings.indexTime")}
                      className="h-8 w-32 tabular-nums" />
                    <Button size="sm" variant="outline" onClick={() => saveIndex()}>{t("common.save")}</Button>
                    {intervalSaved && <span className="inline-flex items-center gap-1 text-sm text-primary"><Check className="size-4" />{t("common.saved")}</span>}
                  </Row>
                )}
                <div className="border-b py-2 text-xs text-muted-foreground last:border-0">{t(`settings.indexModeHint_${indexMode}`)}</div>
                {/* 지금 색인 + 대기(새 대화) 수 + 자가복구 진행 */}
                <div className="border-b py-3 last:border-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Button variant="outline" size="sm" busy={!!ixStatus?.running || reindexing} onClick={doRunIndex}>
                      {ixStatus?.running && <Loader2 className="mr-1 size-4 animate-spin" />}{t("settings.indexNow")}
                    </Button>
                    <span role="status" aria-live="polite" className="text-[11px] text-muted-foreground">
                      {ixStatus?.running
                        ? (ixStatus.total_chunks > 0
                            ? t("settings.selfHealing")
                            : ixStatus.total_files > 0 ? t("settings.indexingFilesEta", { done: ixStatus.done_files, total: ixStatus.total_files }) : t("settings.indexingShort"))
                        : <b className="text-foreground">{idxPendingText}</b>}
                    </span>
                  </div>
                  {ixStatus?.running && ixStatus.total_chunks > 0 && (
                    <div className="mt-2">
                      <BarProgress done={ixStatus.done_chunks} total={ixStatus.total_chunks} unit={t("settings.unitChunks")} cps={curCps} />
                    </div>
                  )}
                  <Errs errors={ixStatus?.errors} />
                </div>
              </Section>

              <Section title={t("settings.embedModel")}>
                <div className="border-b py-2.5 text-[11px] text-muted-foreground">
                  {t("settings.embedHint")}
                </div>
                {reindexing && (
                  <div className="border-b py-3.5">
                    <div role="status" aria-live="polite" className="mb-1.5 flex items-center gap-2 text-sm text-primary">
                      <Loader2 className="size-4 animate-spin" />{t("settings.reindexing", { msg: reindexMsg })}
                    </div>
                    {reindexProg.totalChunks > 0
                      ? <BarProgress done={reindexProg.doneChunks} total={reindexProg.totalChunks} unit={t("settings.unitChunks")} cps={curCps} />
                      : reindexProg.totalFiles > 0
                        ? <BarProgress done={reindexProg.doneFiles} total={reindexProg.totalFiles} unit={t("settings.unitFiles")} />
                        : <div className="text-[11px] text-muted-foreground">{t("settings.reindexPreparing")}</div>}
                  </div>
                )}
                {reindexErr && (
                  <div role="alert" className="border-b py-3 text-sm text-destructive">{reindexErr}</div>
                )}
                {embed.map((m) => (
                  <Row key={m.model} label={
                    <span className="flex flex-col">
                      <span className="flex items-center gap-1.5 font-mono text-[13px]">
                        {shortModelName(m.model)}
                        {m.model === recommendedModel && <span className="rounded-full bg-primary/10 px-1.5 py-0.5 font-sans text-[10px] font-medium text-primary">{t("settings.recommended")}</span>}
                        {m.ram_gb <= 1.5 && <span className="rounded-full bg-muted px-1.5 py-0.5 font-sans text-[10px] font-medium text-muted-foreground">{t("settings.lowSpecRec")}</span>}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {t("settings.diskLabel", { gb: m.size_gb })} · <b className="text-foreground/80">{t("settings.ramApprox", { gb: m.ram_gb })}</b>
                        {m.est_reindex_min != null && <> · {t("settings.estReindexLabel")} <b className="text-foreground/80 tabular-nums">{t("settings.estReindexMin", { min: m.est_reindex_min })}</b></>}
                      </span>
                    </span>
                  }>
                    {m.current
                      ? <span className="inline-flex items-center gap-2">
                          <span className="inline-flex items-center gap-1 text-xs text-primary"><Check className="size-3.5" />{t("settings.inUse")}</span>
                          <Button variant="outline" size="sm" busy={reindexing || !!ixStatus?.running} onClick={() => setConfirmModel(m)}>{t("settings.fullReindex")}</Button>
                        </span>
                      : <Button variant="outline" size="sm" busy={reindexing || !!ixStatus?.running} onClick={() => setConfirmModel(m)}>{t("settings.change")}</Button>}
                  </Row>
                ))}
              </Section>
            </>
          )}

          {/* ── 동기화: 내장 엔진 + 멀티기기 세션 동기화 ── */}
          {tab === "sync" && (
            <>
              <Section title={t("sync.deviceConnectSection")}>
                <SyncthingSection />
              </Section>
              <Section title={t("sync.autoSection")}>
                <AutoSyncSection />
              </Section>
            </>
          )}

          {/* ── MCP 연동 ── */}
          {tab === "mcp" && (
            <Section title={t("mcp.sectionTitle")}>
              <McpSection />
            </Section>
          )}
        </div>
      </div>

      <AlertDialog open={!!confirmModel} onOpenChange={(o) => !o && setConfirmModel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmModel?.current ? t("settings.reindexTitleCurrent") : t("settings.reindexTitleChange")}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirmModel?.current
                ? <Trans i18nKey="settings.reindexDescCurrent" values={{ model: shortModelName(confirmModel?.model ?? "") }} components={{ b: <b /> }} />
                : <Trans i18nKey="settings.reindexDescChange" values={{ model: shortModelName(confirmModel?.model ?? "") }} components={{ b: <b /> }} />}
              <br /><br />
              <Trans i18nKey="settings.reindexTail"
                values={{
                  min: confirmModel?.est_reindex_min, ram: confirmModel?.ram_gb,
                  download: confirmModel?.current ? "" : t("settings.reindexDownload", { size: confirmModel?.size_gb }),
                }}
                components={{ b: <b /> }} />
            </AlertDialogDescription>
          </AlertDialogHeader>
          {(() => {
            const ramGb = confirmModel?.ram_gb ?? 0
            const availGb = sys?.ram_avail_mb != null ? sys.ram_avail_mb / 1024 : null
            const totalGb = sys?.ram_total_mb != null ? sys.ram_total_mb / 1024 : null
            // 권장 최대 = 가용 RAM / 모델 RAM (최소 1). 백엔드도 같은 기준으로 하드 상한.
            const recMax = availGb != null ? Math.max(1, Math.floor(availGb / Math.max(ramGb, 0.1))) : 8
            const willUse = Math.round(ramGb * parallelN)
            const over = availGb != null && willUse > availGb
            return (
              <div className="rounded-lg border bg-muted/30 p-3 text-[12px] leading-relaxed">
                <label className="flex items-start gap-2">
                  <input type="checkbox" checked={fastReindex} onChange={(e) => setFastReindex(e.target.checked)}
                    className="mt-0.5 size-4 shrink-0 accent-primary" />
                  <span className="text-muted-foreground">
                    <Trans i18nKey="settings.fastReindexDesc" components={{ b: <b className="text-foreground" /> }} />
                  </span>
                </label>
                {fastReindex && (
                  <div className="mt-2.5 space-y-1.5 border-t pt-2.5">
                    <div className="flex items-center gap-2">
                      <span className="text-foreground">{t("settings.parallelProcs")}</span>
                      <input type="number" min={1} max={16} value={parallelN}
                        onChange={(e) => setParallelN(Math.max(1, Math.min(16, +e.target.value || 1)))}
                        onWheel={(e) => (e.target as HTMLInputElement).blur()}
                        className="h-7 w-16 rounded-md border bg-background px-2 tabular-nums outline-none" />
                      <span className="text-muted-foreground">{t("settings.recMaxLabel")} <b className="text-foreground tabular-nums">{recMax}</b></span>
                    </div>
                    <div className="tabular-nums text-muted-foreground">
                      {t("settings.deviceRam")} {totalGb != null ? <>{t("settings.ramTotal")} <b className="text-foreground">{totalGb.toFixed(1)}GB</b></> : t("settings.ramNoInfo")}
                      {availGb != null && <> · {t("settings.ramAvail")} <b className="text-foreground">{availGb.toFixed(1)}GB</b></>}
                      {" · "}{t("settings.ramWillUse")} <b className={over ? "text-destructive" : "text-foreground"}>{willUse}GB</b>
                    </div>
                    {over && (
                      <div className="flex items-center gap-1.5 text-destructive">
                        <AlertTriangle className="size-3.5 shrink-0" />{t("settings.ramOver", { recMax })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })()}
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={doReindex}>{t("settings.reindexStart")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
