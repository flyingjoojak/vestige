import { useCallback, useEffect, useState, lazy, Suspense } from "react"
import { useTranslation } from "react-i18next"
import { MessagesSquare, Layers, Box, FoldVertical, FolderTree, Settings } from "lucide-react"
import { Magnifier } from "@/components/ui/Magnifier"
import { Loader2 } from "lucide-react"
import { SearchView } from "@/components/SearchView"
import { Browse3Pane } from "@/components/Browse3Pane"
import { SettingsView } from "@/components/SettingsView"
import { FoldedView } from "@/components/FoldedView"
import { FolderView } from "@/components/FolderView"
import { Onboarding } from "@/components/Onboarding"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { StatusBar } from "@/components/StatusBar"
import { UpdateBanner } from "@/components/UpdateBanner"
import { AlertTriangle } from "lucide-react"
import { getOnboarding, getSchemaReport, getSystem, getStats, getIndexStatus, listHidden, type SchemaSource } from "@/lib/api"
import { buildIssueUrl, copyText } from "@/lib/report"
import { applyTheme } from "@/lib/theme"
import vestigeMark from "@/assets/vestige-mark.png"

// three.js는 무거우니 3D 탭 열 때만 로드(초기 번들 경량).
const GraphView3D = lazy(() => import("@/components/GraphView3D").then((m) => ({ default: m.GraphView3D })))

type View = "search" | "sessions" | "clusters" | "graph3d" | "folders" | "folded" | "settings"

const NAV: { v: View; icon: React.ReactNode; labelKey: string }[] = [
  { v: "search", icon: <Magnifier className="size-[18px]" />, labelKey: "nav.search" },
  { v: "sessions", icon: <MessagesSquare className="size-[18px]" />, labelKey: "nav.sessions" },
  { v: "clusters", icon: <Layers className="size-[18px]" />, labelKey: "nav.clusters" },
  { v: "graph3d", icon: <Box className="size-[18px]" />, labelKey: "nav.map" },
  { v: "folders", icon: <FolderTree className="size-[18px]" />, labelKey: "nav.folders" },
  { v: "folded", icon: <FoldVertical className="size-[18px]" />, labelKey: "nav.folded" },
  { v: "settings", icon: <Settings className="size-[18px]" />, labelKey: "nav.settings" },
]

export default function App() {
  const { t } = useTranslation()
  const [view, setView] = useState<View>("search")
  // 지도에서 대화(턴)를 클릭하면 그 탭(3분할·왼쪽 검색창)으로 이동해 그 대화를 연다(진입 경로만 다르고 화면 동일).
  const [jump, setJump] = useState<{ kind: "sessions" | "clusters"; id: string; turn: string; session: string } | null>(null)
  // 같은 탭을 다시 클릭하면 그 화면을 처음 상태로 리셋(nonce를 올려 key를 바꿔 remount).
  const [nonce, setNonce] = useState(0)
  const onNav = (v: View) => {
    if (v === view) setNonce((k) => k + 1)   // 활성 탭 재클릭 → 리셋
    else setView(v)
    setJump(null)
  }
  const openTurn = (kind: "sessions" | "clusters", id: string, turn: string, session: string) => {
    setJump({ kind, id, turn, session }); setView(kind); setNonce((k) => k + 1)
  }
  // 좌측 메뉴 '접힘' 배지 개수. 검색·세션 어디서 접든 반영돼야 해서 가볍게 폴링한다(COUNT만).
  const [foldedCount, setFoldedCount] = useState(0)
  const refreshFolded = useCallback(() => {
    listHidden(0).then((r) => setFoldedCount(r.count)).catch(() => { /* 배지일 뿐이라 조용히 무시 */ })
  }, [])
  useEffect(() => {
    refreshFolded()
    const id = window.setInterval(refreshFolded, 5000)
    return () => window.clearInterval(id)
  }, [refreshFolded])
  // 첫 실행이면(프리즈 exe·모델 미선택) 모델 선택 화면을 먼저. null=확인중.
  const [onboard, setOnboard] = useState<boolean | null>(null)
  const [backendDown, setBackendDown] = useState(false)
  const [mismatch, setMismatch] = useState<{ stored: string; current: string } | null>(null)
  const [drift, setDrift] = useState<string[]>([])   // 로그 형식이 바뀌어 못 읽는 소스
  // 첫 실행 배너: 색인이 돌고 아카이브가 아직 거의 빈 상태면 "지금 채우는 중"임을 전면에 알린다
  // (하단 7px 상태바만으론 첫 사용자가 앱이 멈춘 줄 오해). 아카이브가 차면(>200턴) 자동으로 사라진다.
  const [firstRun, setFirstRun] = useState<{ turns: number } | null>(null)
  useEffect(() => { applyTheme() }, [])
  // 온보딩 상태 확인 = 백엔드 헬스체크 겸용. 실패는 '완료'가 아니라 '백엔드 미기동'으로 구분(빈 화면 방지).
  const checkOnboard = useCallback(() => {
    getOnboarding()
      .then((r) => { setOnboard(r.needed); setBackendDown(false) })
      .catch(() => { setBackendDown(true) })
  }, [])
  useEffect(() => { checkOnboard() }, [checkOnboard])
  useEffect(() => {   // 미기동이면 뜰 때까지 자동 재시도(exe 기동 지연 대비)
    if (!backendDown) return
    const id = setInterval(checkOnboard, 2000)
    return () => clearInterval(id)
  }, [backendDown, checkOnboard])
  // 모델↔벡터 불일치 배너: 폴링으로 (1) 콜드스타트 시 재시도해 결국 표시, (2) 재색인으로 해소되면 자동 사라짐.
  useEffect(() => {
    const load = () => getSystem().then((s) => { setMismatch(s.model_mismatch); setDrift(s.drift_sources ?? []) }).catch(() => {})
    load()
    const id = window.setInterval(load, 20000)
    return () => window.clearInterval(id)
  }, [])
  // 첫 실행 색인 진행 폴링: (색인 중 또는 대기) && 아카이브가 아직 거의 빈 상태(<200턴)일 때만 배너.
  useEffect(() => {
    if (onboard !== false) return   // 온보딩 끝난 뒤에만
    const load = async () => {
      try {
        const [ix, stats] = await Promise.all([getIndexStatus(), getStats()])
        const turns = stats?.turns ?? 0
        const busy = !!ix?.running || (ix?.pending?.files ?? 0) > 0
        setFirstRun(busy && turns < 200 ? { turns } : null)
      } catch { /* 백엔드 미기동 등은 다른 배너가 처리 */ }
    }
    load()
    const id = window.setInterval(load, 4000)
    return () => window.clearInterval(id)
  }, [onboard])

  // 드리프트 원클릭 신고: 그 소스의 리댁트 지문(대화 내용 없음)을 클립보드에 담고 프리필된 GitHub 이슈를 연다.
  async function reportDrift() {
    const src = drift[0]
    if (!src) return
    try {
      const report = await getSchemaReport(src as SchemaSource)
      const json = JSON.stringify(report, null, 2)
      const ok = await copyText(json)
      window.open(buildIssueUrl(report, json, ok), "_blank", "noopener,noreferrer")
    } catch { /* 실패해도 '설정 열기' 폴백이 있음 */ }
  }

  if (backendDown) {
    return (
      <div className="grid h-full place-items-center bg-background px-6">
        <div className="flex max-w-sm flex-col items-center gap-3 text-center">
          <AlertTriangle className="size-6 text-amber-500" />
          <div className="text-sm font-medium">{t("app.backendDownTitle")}</div>
          <div className="text-[13px] text-muted-foreground">{t("app.backendDownBody")}</div>
          <button onClick={checkOnboard} className="mt-1 inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground">
            <Loader2 className="size-4 animate-spin" />{t("app.backendDownRetry")}
          </button>
        </div>
      </div>
    )
  }
  if (onboard === null) {
    return <div className="grid h-full place-items-center bg-background text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>
  }
  if (onboard) {
    return <Onboarding onDone={() => setOnboard(false)} />
  }

  return (
    <div className="grid h-full grid-cols-[60px_1fr] overflow-hidden pb-7">
      {/* 옵시디언식 좌측 아이콘 리본 */}
      <nav aria-label={t("nav.mainAria")} className="flex flex-col items-center gap-1 border-r bg-sidebar py-3">
        <img src={vestigeMark} alt="Vestige" width={32} height={32} className="mb-2 size-8" />
        {NAV.map((n) => (
          <button
            key={n.v}
            onClick={() => onNav(n.v)}
            title={t(n.labelKey)}
            aria-label={t(n.labelKey)}
            className={`relative grid size-10 place-items-center rounded-lg transition-colors ${
              view === n.v ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
          >
            {n.icon}
            {n.v === "folded" && foldedCount > 0 && (
              <span aria-hidden className="absolute right-0.5 top-0.5 min-w-[15px] rounded-full bg-muted px-1 text-[9px] font-medium leading-[15px] text-muted-foreground tabular-nums">
                {foldedCount > 99 ? "99+" : foldedCount}
              </span>
            )}
          </button>
        ))}
      </nav>

      {/* 메인 패널 — 뷰 크래시가 앱 전체를 죽이지 않게 격리 */}
      <main className="min-h-0 overflow-y-auto">
        <UpdateBanner />
        {firstRun && (
          <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-2 border-b border-sky-500/40 bg-sky-500/10 px-4 py-2 text-[13px] text-sky-700 dark:text-sky-300">
            <Loader2 className="size-4 shrink-0 animate-spin" />
            <span>{t("app.firstRunIndexing", { n: firstRun.turns.toLocaleString() })}</span>
          </div>
        )}
        {mismatch && (
          <div role="alert" className="flex flex-wrap items-center gap-2 border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-[13px] text-amber-700 dark:text-amber-400">
            <AlertTriangle className="size-4 shrink-0" />
            <span>{t("app.mismatchMsg", { stored: mismatch.stored.split("/").pop(), current: mismatch.current.split("/").pop() })}</span>
            <button onClick={() => { setView("settings"); setJump(null) }}
              className="ml-auto rounded-md border border-amber-500/50 px-2 py-0.5 font-medium hover:bg-amber-500/20">
              {t("app.mismatchReindex")}
            </button>
          </div>
        )}
        {drift.length > 0 && (
          <div role="alert" className="flex flex-wrap items-center gap-2 border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-[13px] text-amber-700 dark:text-amber-400">
            <AlertTriangle className="size-4 shrink-0" />
            <span>
              {t("app.driftMsg", { sources: drift.map((s) => (s === "codex" ? "Codex" : s === "claude-code" ? "Claude Code" : s)).join(", ") })}
            </span>
            <button onClick={reportDrift}
              className="ml-auto rounded-md border border-amber-500/50 bg-amber-500/20 px-2 py-0.5 font-medium hover:bg-amber-500/30">
              {t("app.driftReport")}
            </button>
            <button onClick={() => { setView("settings"); setJump(null) }}
              className="rounded-md border border-amber-500/50 px-2 py-0.5 font-medium hover:bg-amber-500/20">
              {t("app.driftOpenSettings")}
            </button>
          </div>
        )}
        <ErrorBoundary key={`${view}:${nonce}`}>
          {view === "search" && <SearchView />}
          {view === "sessions" && (
            <Browse3Pane kind="sessions" initialSel={jump?.kind === "sessions" ? jump.id : null}
              initialTurn={jump?.kind === "sessions" ? { turn: jump.turn, session: jump.session } : null} />
          )}
          {view === "clusters" && (
            <Browse3Pane kind="clusters" initialSel={jump?.kind === "clusters" ? jump.id : null}
              initialTurn={jump?.kind === "clusters" ? { turn: jump.turn, session: jump.session } : null} />
          )}
          {view === "graph3d" && (
            <Suspense fallback={<div className="grid h-full place-items-center text-muted-foreground">{t("app.mapLoading")}</div>}>
              <GraphView3D onOpenTurn={openTurn} />
            </Suspense>
          )}
          {view === "folders" && (
            /* 폴더 항목은 턴이면 그 턴으로, 세션이면 세션만 열어 목록에서 고르게 한다. */
            <FolderView onOpen={(session, turn) => openTurn("sessions", session, turn ?? "", session)} />
          )}
          {view === "folded" && (
            <FoldedView onChanged={refreshFolded}
              onOpenTurn={(session, turn) => openTurn("sessions", session, turn, session)} />
          )}
          {view === "settings" && <SettingsView />}
        </ErrorBoundary>
      </main>

      {/* 하단 상태바 — 설정에 안 들어가도 저장소 현황·색인·동기화를 한눈에 */}
      <StatusBar />
    </div>
  )
}
