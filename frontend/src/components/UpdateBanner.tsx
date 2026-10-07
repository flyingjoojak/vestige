import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Download, RefreshCw, ChevronDown, ChevronUp, X, AlertTriangle } from "lucide-react"
import { pickReleaseNotes } from "@/lib/releaseNotes"

// Electron preload(preload.js)이 주입하는 업데이트 브리지. 브라우저에는 없다 → 배너 미표시.
type UpdateInfo = {
  version: string
  releaseName?: string
  releaseNotes?: string
  releaseDate?: string
  // macOS 미서명 빌드: 자동 다운로드/설치 대신 다운로드 페이지를 여는 '안내형' 업데이트.
  assisted?: boolean
}
type VestigeUpdater = {
  onAvailable: (cb: (info: UpdateInfo) => void) => () => void
  onProgress: (cb: (p: { percent: number }) => void) => () => void
  onDownloaded: (cb: (info: { version: string }) => void) => () => void
  onError: (cb: (e: { message: string }) => void) => () => void
  download: () => void
  install: () => void
  requestPending: () => void
}

declare global {
  interface Window {
    vestigeUpdater?: VestigeUpdater
  }
}

type Phase = "idle" | "available" | "downloading" | "downloaded"

export function UpdateBanner() {
  const { t, i18n } = useTranslation()
  const [phase, setPhase] = useState<Phase>("idle")
  const [info, setInfo] = useState<UpdateInfo | null>(null)
  const [percent, setPercent] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  const [showNotes, setShowNotes] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [assistedOpened, setAssistedOpened] = useState(false)   // macOS 안내형: 다운로드 페이지 연 뒤 확인 표시

  useEffect(() => {
    const up = window.vestigeUpdater
    if (!up) return
    const offs = [
      up.onAvailable((i) => { setInfo(i); setPhase("available"); setError(null); setDismissed(false); setAssistedOpened(false) }),
      up.onProgress((p) => { setPercent(p.percent); setPhase("downloading") }),
      up.onDownloaded((i) => { setInfo((prev) => ({ ...(prev ?? {}), version: i.version })); setPhase("downloaded"); setError(null); setDismissed(false) }),
      // 다운로드/설치 실패 → 멈춤 방지: 에러 표시 + 재시도 가능하게 available 로 되돌림.
      // (idle 이면 아직 배너 전 → 그대로 숨김. info 클로저를 참조하지 않아 stale 값 문제 없음.)
      up.onError((e) => {
        setError(e.message)
        setDismissed(false)
        setPhase((cur) => (cur === "idle" ? "idle" : cur === "downloaded" ? "downloaded" : "available"))
      }),
    ]
    up.requestPending() // 마운트 시점에 이미 온 알림이 있으면 다시 받아옴(레이스 방지)
    return () => offs.forEach((off) => off())
  }, [])

  if (!window.vestigeUpdater || phase === "idle" || dismissed) return null

  const version = info?.version ? `v${info.version}` : t("update.newVersion")
  // 릴리스 노트는 앱 언어에 맞는 섹션만 표시(GitHub 본문에 <!--lang:en-->/<!--lang:ko--> 로 구분).
  const notes = pickReleaseNotes(info?.releaseNotes, i18n.language)
  const startDownload = () => { setError(null); setPercent(0); setPhase("downloading"); window.vestigeUpdater?.download() }

  return (
    <div role="status" className="border-b border-primary/30 bg-primary/10 px-4 py-2 text-[13px] text-foreground">
      <div className="flex flex-wrap items-center gap-2">
        <Download className="size-4 shrink-0 text-primary" />

        {phase === "available" && (
          <>
            <span>{t("update.available", { version })}</span>
            {notes && (
              <button
                onClick={() => setShowNotes((s) => !s)}
                aria-expanded={showNotes}
                aria-controls="update-notes"
                className="inline-flex items-center gap-0.5 text-muted-foreground hover:text-foreground"
              >
                {t("update.notes")} {showNotes ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
              </button>
            )}
            <div className="ml-auto flex items-center gap-1.5">
              {info?.assisted ? (
                // macOS 미서명: 자동 설치 불가 → 다운로드 페이지를 열고, 즉시 닫지 않고 안내를 남겨
                // 포커스 유실과 무피드백을 방지(a11y).
                <button
                  onClick={() => { window.vestigeUpdater?.download(); setAssistedOpened(true) }}
                  disabled={assistedOpened}
                  aria-describedby="assisted-update-help"
                  className="rounded-md bg-primary px-2.5 py-0.5 font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
                >
                  {assistedOpened ? t("update.assistedOpened") : t("update.assistedOpen")}
                </button>
              ) : (
                <button
                  onClick={startDownload}
                  className="rounded-md bg-primary px-2.5 py-0.5 font-medium text-primary-foreground hover:bg-primary/90"
                >
                  {error ? t("common.retry") : t("update.updateNow")}
                </button>
              )}
              <button
                onClick={() => setDismissed(true)}
                className="rounded-md border border-border px-2 py-0.5 text-muted-foreground hover:bg-muted"
              >
                {t("common.later")}
              </button>
            </div>
            {/* macOS 안내: 수동 교체 방법을 hover 툴팁이 아니라 화면·스크린리더로 항상 노출(a11y). */}
            {info?.assisted && (
              <span id="assisted-update-help" className="w-full text-[12px] text-muted-foreground">
                {assistedOpened ? t("update.macHelpOpened") : t("update.macHelp")}
              </span>
            )}
          </>
        )}

        {phase === "downloading" && (
          <>
            <span>{t("update.downloading")}</span>
            <div
              role="progressbar"
              aria-valuenow={percent}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={t("update.downloadProgress")}
              className="ml-2 h-1.5 w-40 overflow-hidden rounded-full bg-muted"
            >
              <div className="h-full bg-primary transition-all" style={{ width: `${percent}%` }} />
            </div>
            <span className="tabular-nums text-muted-foreground">{percent}%</span>
          </>
        )}

        {phase === "downloaded" && (
          <>
            <RefreshCw className="size-4 shrink-0 text-primary" />
            <span>{t("update.ready", { version })}</span>
            <div className="ml-auto flex items-center gap-1.5">
              <button
                onClick={() => window.vestigeUpdater?.install()}
                className="rounded-md bg-primary px-2.5 py-0.5 font-medium text-primary-foreground hover:bg-primary/90"
              >
                {t("update.installNow")}
              </button>
              <button
                onClick={() => setDismissed(true)}
                title={t("update.installOnQuit")}
                className="rounded-md border border-border px-2 py-0.5 text-muted-foreground hover:bg-muted"
              >
                {t("common.later")}
              </button>
            </div>
          </>
        )}

        {/* 닫기: 어떤 단계에서도 항상 노출(다운로드 중 멈춤 방지). */}
        <button
          onClick={() => setDismissed(true)}
          aria-label={t("common.close")}
          className={`${phase === "available" || phase === "downloaded" ? "" : "ml-auto"} rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground`}
        >
          <X className="size-3.5" />
        </button>
      </div>

      {error && (
        <div className="mt-1 flex items-center gap-1.5 text-[12px] text-amber-700 dark:text-amber-400">
          <AlertTriangle className="size-3.5 shrink-0" />
          <span>{t("update.failed", { error })}</span>
        </div>
      )}

      {phase === "available" && showNotes && notes && (
        <pre id="update-notes" className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-border/60 bg-background/60 p-2 text-[12px] leading-relaxed text-muted-foreground">
          {notes}
        </pre>
      )}
    </div>
  )
}
