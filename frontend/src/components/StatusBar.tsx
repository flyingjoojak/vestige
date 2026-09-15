import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import { Loader2 } from "lucide-react"
import { getIndexStatus, getStats, getSyncthingStatus, type IndexStatus, type SyncthingStatus } from "@/lib/api"
import type { Stats } from "@/lib/types"

// 하단 상태바(옵시디언식): 설정에 안 들어가도 저장소 현황·색인·동기화를 한눈에.
// 색인은 필수 기능이라 오른쪽에 색상으로 표시하고, 동기화(옵션)는 켜졌을 때만 그 옆에 붙인다.
function pct(done: number, total: number): number {
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
}

function indexLabel(
  ix: IndexStatus | null, pending: number, t: TFunction,
): { text: string; dot: string; tone: string; spin: boolean } {
  if (ix?.running) {
    // 진행 %는 in-process 색인에서만 계산(external=OS 스케줄러 프로세스는 진행 데이터가 경계를 못 넘음).
    // 청크 진행(자가복구/전체 재색인)이 가장 의미 있고, 없으면 파일 단위, 둘 다 없으면 텍스트만.
    const p = ix.external ? null
      : ix.total_chunks > 0 ? pct(ix.done_chunks, ix.total_chunks)
      : ix.total_files > 0 ? pct(ix.done_files, ix.total_files)
      : null
    const text = p != null ? t("statusbar.indexingPct", { pct: p }) : t("statusbar.indexing")
    return { text, dot: "bg-sky-500", tone: "text-sky-600 dark:text-sky-400", spin: true }
  }
  if (pending > 0)
    return { text: t("statusbar.pendingNew", { n: pending }), dot: "bg-amber-500", tone: "text-amber-600 dark:text-amber-500", spin: false }
  return { text: t("statusbar.upToDate"), dot: "bg-emerald-500", tone: "text-emerald-600 dark:text-emerald-400", spin: false }
}

function syncLabel(st: SyncthingStatus | null, t: TFunction, stalled: boolean): { text: string; dot: string; tone: string } {
  const g = "text-muted-foreground"
  if (!st || !st.running) return { text: t("statusbar.syncOff"), dot: "bg-muted-foreground/40", tone: g }
  const s = st.sync
  if (!s) return { text: t("statusbar.syncWaiting"), dot: "bg-muted-foreground/40", tone: g }
  if (s.state === "error") return { text: t("statusbar.syncError"), dot: "bg-destructive", tone: "text-destructive" }
  if (s.state === "scanning") return { text: t("statusbar.syncScanning"), dot: "bg-amber-500", tone: "text-amber-600 dark:text-amber-500" }
  if (stalled) return { text: t("statusbar.syncStalled"), dot: "bg-destructive", tone: "text-destructive" }   // 오래 무진척(상대 미연결 등)
  if (s.state === "syncing" || s.need_items > 0 || s.need_bytes > 0) return { text: t("statusbar.syncReceiving", { pct: s.completion }), dot: "bg-amber-500", tone: "text-amber-600 dark:text-amber-500" }
  if (s.remote_complete != null && s.remote_complete < 100) return { text: t("statusbar.syncSending", { pct: s.remote_complete }), dot: "bg-amber-500", tone: "text-amber-600 dark:text-amber-500" }
  if ((s.peers_connected ?? 0) === 0) return { text: t("statusbar.syncLatestPeerOff"), dot: "bg-muted-foreground/40", tone: g }
  return { text: t("statusbar.syncBothLatest"), dot: "bg-emerald-500", tone: "text-emerald-600 dark:text-emerald-400" }   // 완전 동기화 = 초록
}

export function StatusBar() {
  const { t } = useTranslation()
  const [stats, setStats] = useState<Stats | null>(null)
  const [ix, setIx] = useState<IndexStatus | null>(null)
  const [st, setSt] = useState<SyncthingStatus | null>(null)
  const progRef = useRef<{ key: string; ts: number }>({ key: "", ts: Date.now() })   // 동기 진척이 마지막으로 변한 시각(멈춤 감지)

  useEffect(() => {
    let alive = true
    const dbg = (e: unknown) => console.debug("[statusbar]", e)   // 무음 대신 진단 로그
    const load = () => {
      getStats().then((r) => alive && setStats(r)).catch(dbg)
      getIndexStatus().then((r) => alive && setIx(r)).catch(dbg)
      getSyncthingStatus().then((r) => alive && setSt(r)).catch(dbg)
    }
    load()
    const id = window.setInterval(load, 1000)   // 전부 로컬(SQLite·벡터 파일·localhost REST)이라 1초 폴링도 부담 없음
    return () => { alive = false; window.clearInterval(id) }
  }, [])

  const pending = ix?.pending?.files ?? 0
  const idx = indexLabel(ix, pending, t)
  // 전송이 60초 이상 진척 없으면 '멈춤'으로 표시(하드 취소 아님 — Syncthing 재시도는 유지).
  const s = st?.sync
  const syncKey = s ? `${s.completion}|${s.remote_complete ?? ""}|${s.need_items}|${s.peers_connected ?? 0}` : ""
  useEffect(() => {
    if (syncKey !== progRef.current.key) progRef.current = { key: syncKey, ts: Date.now() }
  }, [syncKey])
  const syncPending = !!s && (s.state === "syncing" || s.need_items > 0 || s.need_bytes > 0
    || (s.remote_complete != null && s.remote_complete < 100))
  const stalled = syncPending && Date.now() - progRef.current.ts > 60_000
  const sync = syncLabel(st, t, stalled)
  const n = (v?: number) => (v ?? 0).toLocaleString()

  return (
    <footer aria-label={t("statusbar.ariaLabel")} className="fixed inset-x-0 bottom-0 z-20 flex h-7 items-center gap-x-3 overflow-hidden border-t bg-sidebar px-3 text-[11px] text-muted-foreground tabular-nums">
      {/* 왼쪽: 저장소 현황 — 공간 부족 시 이쪽만 truncate(오른쪽 색인/동기화 상태는 온전히 유지) */}
      <span className="flex min-w-0 shrink items-center gap-x-3 truncate">
        <span>{t("statusbar.sessions", { n: n(stats?.sessions) })}</span>
        <span>{t("statusbar.turns", { n: n(stats?.turns) })}</span>
        <span title={t("statusbar.vectorsTip")}>{t("statusbar.vectors", { n: n(stats?.vectors) })}</span>
        <span>{t("statusbar.enriched", { n: n(stats?.enriched) })}</span>
      </span>
      {/* 오른쪽: 색인 상태(필수·색상) + 동기화 상태(꺼짐이면 회색으로 표시) — 항상 한 줄에 온전히 */}
      <span className="ml-auto flex shrink-0 items-center gap-x-3 whitespace-nowrap">
        <span title={t("statusbar.indexTip")} aria-live="polite" aria-atomic="true" className={`inline-flex items-center gap-1.5 font-medium ${idx.tone}`}>
          {idx.spin
            ? <Loader2 className="size-3 shrink-0 animate-spin" />
            : <span className={`size-2 rounded-full ${idx.dot}`} />}
          {idx.text}
        </span>
        <span className="opacity-30">·</span>
        <span title={t("statusbar.syncTip")} aria-live="polite" aria-atomic="true" className={`inline-flex items-center gap-1.5 ${sync.tone}`}>
          <span className={`size-2 rounded-full ${sync.dot}`} />{sync.text}
        </span>
      </span>
    </footer>
  )
}
