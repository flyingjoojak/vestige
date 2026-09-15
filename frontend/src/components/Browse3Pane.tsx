import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertTriangle, ArchiveRestore, ArrowLeft, Blend, Bot, Brain, Check, ChevronRight, Copy, FileDown, Loader2, MessagesSquare, RotateCcw, TerminalSquare, Type } from "lucide-react"
import { Magnifier } from "@/components/ui/Magnifier"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { SegmentedRadioGroup } from "@/components/ui/SegmentedRadioGroup"
import { ChatThread } from "./ChatThread"
import { getGraph3D, getSession, listSessions, resumeSession, restoreSession, search, type SearchMode } from "@/lib/api"
import { errText } from "@/lib/errors"
import { fmtTime } from "@/lib/format"
import type { Hit, SessionDetail } from "@/lib/types"

const PALETTE = [
  "#6ea8fe", "#f4845f", "#5cc8a8", "#c78be0", "#e6b34a", "#7ed957", "#ef6f9b",
  "#5bc0de", "#b58a63", "#9aa0ff", "#4fb477", "#e05c5c", "#a3c644", "#c96bb3", "#59b0a3", "#d98a5b",
]
const colorOf = (c: number) => PALETTE[((c % PALETTE.length) + PALETTE.length) % PALETTE.length]
const MODES: { v: SearchMode; key: string; Icon: typeof Blend }[] = [
  { v: "hybrid", key: "browse.modeHybrid", Icon: Blend },
  { v: "semantic", key: "browse.modeSemantic", Icon: Brain },
  { v: "keyword", key: "browse.modeKeyword", Icon: Type },
]
function openPicker(e: React.MouseEvent<HTMLInputElement> | React.FocusEvent<HTMLInputElement>) {
  const el = e.currentTarget as HTMLInputElement & { showPicker?: () => void }
  try { el.showPicker?.() } catch { /* 미지원 */ }
}

type Group = { id: string; label: string; sub: string; count: number; color?: string; subagent?: boolean }
type Conv = { t: string; s: string; h: string }

// 세션/군집 공통 3분할 브라우저. 초기=목록(가운데), 선택 후=[검색+대화목록 | 채팅 | 목록].
// initialSel: 지도에서 진입할 때 특정 그룹을 바로 선택된 상태로 연다(탭으로 들어온 것과 동일 화면).
// initialTurn: 지도에서 대화를 클릭해 들어오면 그 대화를 가운데 채팅에 바로 연다.
export function Browse3Pane({ kind, initialSel = null, initialTurn = null }: {
  kind: "sessions" | "clusters"; initialSel?: string | null; initialTurn?: { turn: string; session: string } | null
}) {
  const { t } = useTranslation()
  const [groups, setGroups] = useState<Group[] | null>(null)
  const [groupsErr, setGroupsErr] = useState(false)   // 목록 로드 실패 — '빈 목록'과 구분
  const [pointsByCluster, setPointsByCluster] = useState<Map<number, Conv[]>>(new Map())
  const [sel, setSel] = useState<string | null>(initialSel)
  const [selTurn, setSelTurn] = useState<{ session: string; turn: string } | null>(initialTurn)
  const [convs, setConvs] = useState<Conv[] | null>(null)   // 선택 그룹의 전체 대화
  const [q, setQ] = useState("")
  const [mode, setMode] = useState<SearchMode>("hybrid")
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchErr, setSearchErr] = useState("")
  const [copied, setCopied] = useState(false)
  const [opening, setOpening] = useState(false)
  const [restoring, setRestoring] = useState(false)   // 원본 복구 진행 중(#163 P1)
  const [resumeMsg, setResumeMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [detail, setDetail] = useState<SessionDetail | null>(null)   // 선택 세션 상세(출처·재개커맨드·원문존재)
  const [detailErr, setDetailErr] = useState(false)                  // 세션 상세 로드 실패

  // 타이머 정리(unmount 후 setState 방지). copied 되돌림 / resumeMsg 자동 해제용.
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const msgTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const searchReq = useRef(0)   // 최신 검색만 반영(빠른 연속 입력 시 오래된 응답 덮어쓰기 방지) — SearchView 와 동일 패턴
  useEffect(() => () => {
    if (copyTimer.current) clearTimeout(copyTimer.current)
    if (msgTimer.current) clearTimeout(msgTimer.current)
  }, [])
  function flashMsg(m: { ok: boolean; text: string }) {
    setResumeMsg(m)
    if (msgTimer.current) clearTimeout(msgTimer.current)
    msgTimer.current = setTimeout(() => setResumeMsg(null), 4000)
  }

  // 세션 재개 커맨드(출처별: claude --resume / codex resume). 상세 로드 전엔 판단 보류.
  const ready = detail != null                              // 상세(출처·원문존재) 로드 완료
  const resumeCmd = detail?.resume_cmd || (sel ? `claude --resume ${sel}` : "")
  const fileExists = detail?.source_file_exists !== false   // 로드 후에만 의미(로드 전엔 버튼 비활성)
  const sourceLabel = detail?.source === "codex" ? "Codex" : detail?.source === "claude-code" ? "Claude Code" : ""
  const isSubagent = detail?.subagent === true              // 배경(서브에이전트) 대화 — 직접 재개 불가
  const parentSid = detail?.parent || null                  // 파생된 부모 세션(역링크)
  const canRestore = detail?.can_restore === true            // 원문은 없지만 보존된 원본으로 복구 가능(#163 P1)
  async function copyResume() {
    if (!resumeCmd) return
    // clipboard API는 비보안 컨텍스트(평문 http 비-localhost 등)에서 없을 수 있음 → 가드 + 폴백 안내.
    const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined
    if (!clip?.writeText) {
      flashMsg({ ok: false, text: t("browse.clipboardUnavailable") })
      return
    }
    try {
      await clip.writeText(resumeCmd)
      setCopied(true)
      if (copyTimer.current) clearTimeout(copyTimer.current)
      copyTimer.current = setTimeout(() => setCopied(false), 1500)
    } catch {
      flashMsg({ ok: false, text: t("browse.copyFailed") })
    }
  }
  // 이 PC에서 새 터미널로 바로 재개(로컬 백엔드가 프로세스 실행).
  // 활성 가드: 세션이 최근 수정됐으면(다른 기기 진행 가능) 확인받고 강제 재개.
  async function openResume() {
    if (!sel || opening) return
    setOpening(true); setResumeMsg(null)
    try {
      let r = await resumeSession(sel)
      if (!r.ok && r.active) {   // 최근 수정됨 → 확인 후 강제 재개
        const go = window.confirm(`${errText(t, r, "browse.resumeActiveWarning")}\n\n${t("browse.resumeConfirm")}`)
        if (!go) return
        r = await resumeSession(sel, true)
      }
      // 성공 여부는 r.ok 로만 판단(missing·실행실패 등은 경고로). '거짓 성공' 방지.
      if (r.ok) flashMsg({ ok: true, text: t("browse.resumeStarted") })
      else flashMsg({ ok: false, text: errText(t, r, "browse.resumeCannotOpen") })
    } catch (e) {
      flashMsg({ ok: false, text: errText(t, e, "browse.execFailed") })
    } finally {
      setOpening(false)
    }
  }

  // 정리로 사라진 원문을 보존된 원본으로 복구(#163 P1). 성공하면 상세를 다시 불러와
  // source_file_exists/can_restore 를 갱신 — 바로 위 "열기" 버튼이 활성화됨.
  async function doRestore() {
    if (!sel || restoring) return
    setRestoring(true); setResumeMsg(null)
    try {
      const r = await restoreSession(sel)
      if (r.ok) {
        flashMsg({ ok: true, text: t("browse.restoreDone") })
        const d = await getSession(sel)
        setDetail(d)
      } else {
        flashMsg({ ok: false, text: errText(t, r, "browse.restoreFailed") })
      }
    } catch (e) {
      flashMsg({ ok: false, text: errText(t, e, "browse.execFailed") })
    } finally {
      setRestoring(false)
    }
  }

  // 지도에서 진입(그룹/대화)하면 선택 갱신. 객체 대신 원시값을 dep으로 써 매 렌더 재실행 방지.
  const jumpTurn = initialTurn?.turn ?? null
  const jumpSess = initialTurn?.session ?? null
  useEffect(() => {
    if (initialSel != null) setSel(initialSel)
    setSelTurn(jumpTurn && jumpSess ? { turn: jumpTurn, session: jumpSess } : null)
  }, [initialSel, jumpTurn, jumpSess])

  // 그룹 목록 로드(실패=에러 상태로 구분, 재시도 가능)
  const loadGroups = useCallback(() => {
    setGroupsErr(false); setGroups(null)
    if (kind === "sessions") {
      listSessions().then((r) => setGroups((r.sessions || []).map((s) => ({
        id: s.session, label: s.headline || t("browse.untitled"), count: s.count,
        sub: t("browse.sessionSub", { count: s.count, start: fmtTime(s.started), end: fmtTime(s.ended) }),
        subagent: s.subagent,   // 배경 에이전트 세션이면 목록에서 아이콘으로 구분
      })))).catch(() => setGroupsErr(true))
    } else {
      getGraph3D().then((g) => {
        const m = new Map<number, Conv[]>(); const seen = new Map<number, Set<string>>()
        for (const p of g.points) {
          if (!m.has(p.c)) { m.set(p.c, []); seen.set(p.c, new Set()) }
          const sset = seen.get(p.c)!; if (sset.has(p.t)) continue
          sset.add(p.t); m.get(p.c)!.push({ t: p.t, s: p.s, h: p.h })
        }
        setPointsByCluster(m)
        setGroups((g.clusters || []).map((c) => ({
          id: String(c.id), label: c.label, count: c.n, sub: t("browse.clusterSub", { count: c.n }), color: colorOf(c.id),
        })))
      }).catch(() => setGroupsErr(true))
    }
  }, [kind, t])
  useEffect(() => { loadGroups() }, [loadGroups])

  // 선택 그룹의 대화 목록
  useEffect(() => {
    setConvs(null); setHits(null); setQ(""); setDetail(null); setDetailErr(false)
    if (sel == null) return
    if (kind === "sessions") {
      let cancelled = false   // 다른 세션으로 빠르게 전환 시, 늦게 온 응답이 덮어쓰지 않게
      getSession(sel).then((d) => {
        if (cancelled) return
        setConvs(d.turns.map((turn) => ({ t: turn.id, s: sel, h: turn.summary || turn.question || "" })))
        setDetail(d)
      }).catch(() => { if (!cancelled) { setConvs([]); setDetailErr(true) } })
      return () => { cancelled = true }
    }
    setConvs(pointsByCluster.get(Number(sel)) ?? [])
  }, [sel, kind, pointsByCluster])

  const convSet = useMemo(() => new Set((convs ?? []).map((c) => c.t)), [convs])

  async function runSearch(v = q, m = mode) {
    setQ(v)
    const term = v.trim()
    if (!term) { searchReq.current++; setHits(null); setSearchErr(""); return }   // 진행 중 요청 무효화
    const myId = ++searchReq.current
    setSearching(true); setSearchErr("")
    try {
      if (kind === "sessions") {
        const r = await search({ q: term, k: 20, session: sel!, mode: m, since: since || undefined, until: until || undefined })
        if (myId !== searchReq.current) return   // 더 새 요청이 진행 중 → 이 응답 폐기
        if (r.code || r.error) { setHits([]); setSearchErr(errText(t, r, "browse.noResults")); return }
        const list = r.hits || []; setHits(list)
        if (list.length) setSelTurn({ session: sel!, turn: list[0].id })
      } else {
        const r = await search({ q: term, k: 100, mode: m, since: since || undefined, until: until || undefined })
        if (myId !== searchReq.current) return   // 더 새 요청이 진행 중 → 이 응답 폐기
        if (r.code || r.error) { setHits([]); setSearchErr(errText(t, r, "browse.noResults")); return }
        const list = (r.hits || []).filter((h) => convSet.has(h.id)); setHits(list)
        if (list.length) setSelTurn({ session: list[0].session_full, turn: list[0].id })
      }
    } catch { if (myId === searchReq.current) setHits([]) } finally { if (myId === searchReq.current) setSearching(false) }
  }

  function pickGroup(id: string) { setSel(id); setSelTurn(null) }
  const selGroup = groups?.find((g) => g.id === sel)
  const title = kind === "sessions" ? t("browse.sessionsTitle") : t("browse.clustersTitle")

  // 그룹 아이콘: 세션=말풍선 / 군집=색점.
  const groupIcon = (g: Group) => g.color
    ? <span className="size-3 shrink-0 rounded-full" style={{ background: g.color }} />
    : g.subagent
      ? <Bot className="size-4 shrink-0 text-muted-foreground" aria-label={t("browse.subagentBadge")} />
      : <MessagesSquare className="size-4 shrink-0 text-muted-foreground" />

  // 그룹 목록 — 초기(가운데)는 큼직한 카드(hover 떠오름), 오른쪽 패널은 compact.
  const groupList = (compact: boolean) => (
    <div className={compact ? "min-h-0 flex-1 space-y-1 overflow-y-auto p-3" : "mx-auto w-full max-w-2xl space-y-2.5 p-4"}>
      {groupsErr && (
        <div className="flex flex-col items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-center text-sm">
          <span className="inline-flex items-center gap-1.5 text-destructive"><AlertTriangle className="size-4" />{t("browse.loadFailed")}</span>
          <span className="text-muted-foreground">{t("browse.appMayBeStarting")}</span>
          <button onClick={loadGroups} className="rounded-md border bg-card px-3 py-1.5 text-[13px] shadow-sm hover:text-foreground">{t("common.retry")}</button>
        </div>
      )}
      {!groups && !groupsErr && <div className="grid h-40 place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>}
      {groups && groups.length === 0 && !groupsErr && (
        <div className="grid h-40 place-items-center px-6 text-center text-sm text-muted-foreground">{t("browse.emptyGroups")}</div>
      )}
      {groups?.map((g) => compact ? (
        <button key={g.id} onClick={() => pickGroup(g.id)}
          className={`cm-cv-row flex w-full items-center gap-2.5 rounded-lg border p-3 text-left transition-colors ${sel === g.id ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
          {groupIcon(g)}
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{g.label}</span>
            <span className="block truncate text-[11px] text-muted-foreground tabular-nums">{g.sub}</span>
          </span>
        </button>
      ) : (
        <button key={g.id} onClick={() => pickGroup(g.id)}
          className="cm-cv-row group flex w-full items-center gap-3 rounded-xl border bg-card p-4 text-left shadow-sm transition-all hover:-translate-y-px hover:shadow-md">
          {groupIcon(g)}
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{g.label}</div>
            <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground tabular-nums">{g.sub}</div>
          </div>
          <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </button>
      ))}
    </div>
  )

  if (sel == null) {
    return (
      <div className="flex h-full flex-col">
        <div className="shrink-0 px-6 pt-5">
          <h2 className="text-lg font-semibold">{title}</h2>
          <p className="text-xs text-muted-foreground">{groups ? t("browse.groupCount", { count: groups.length }) : ""}</p>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{groupList(false)}</div>
      </div>
    )
  }

  return (
    <div className="grid h-full grid-cols-[minmax(300px,360px)_1fr_minmax(230px,290px)] overflow-hidden">
      {/* 왼쪽: 검색 + 선택 그룹의 대화 목록 */}
      <div className="flex min-h-0 flex-col border-r">
        <div className="shrink-0 border-b p-4">
          <div className="mb-2 flex items-center gap-2">
            <button onClick={() => setSel(null)} className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:opacity-75">
              <ArrowLeft className="size-4" />{title}
            </button>
            <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
              {selGroup?.color && <span className="mr-1.5 inline-block size-2.5 rounded-full align-middle" style={{ background: selGroup.color }} />}
              {selGroup?.label} · {convs?.length ?? 0}
            </span>
          </div>
          {/* 배경(서브에이전트) 대화: 직접 열 수 없음 → 안내 + 부모 세션 역링크 */}
          {kind === "sessions" && sel && isSubagent && (
            <div className="mb-2 rounded-lg border bg-muted/40 p-2">
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                <Bot className="size-3.5" />{t("browse.subagentBadge")}
              </div>
              <div className="text-[10.5px] text-muted-foreground">{t("browse.subagentNote")}</div>
              {parentSid && (
                <button type="button" onClick={() => setSel(parentSid)}
                  className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-1.5 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/15">
                  <ArrowLeft className="size-3.5" />{t("browse.openParent")}
                </button>
              )}
            </div>
          )}
          {/* 세션 재개: 이 PC에서 새 터미널로 바로 열기 + 커맨드 복사 폴백 */}
          {kind === "sessions" && sel && !isSubagent && (
            <div className="mb-2 rounded-lg border bg-muted/40 p-2">
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                <RotateCcw className="size-3.5" />{t("browse.resumeSession")}
                {sourceLabel && (
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-foreground/70">{sourceLabel}</span>
                )}
              </div>
              <div className="flex items-center gap-1.5">
                <code className="min-w-0 flex-1 truncate rounded bg-background px-1.5 py-1 font-mono text-[11px]" title={resumeCmd}>{resumeCmd}</code>
                <button type="button" onClick={openResume} disabled={opening || !ready || !fileExists}
                  title={ready && !fileExists ? t("browse.cannotOpenNoLog") : undefined}
                  aria-label={ready && !fileExists ? t("browse.openAriaDisabled") : t("browse.openAriaEnabled")}
                  aria-describedby={ready && !fileExists && !canRestore ? "resume-missing-note" : undefined}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-1.5 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-60">
                  {opening ? <Loader2 className="size-3.5 animate-spin" /> : <TerminalSquare className="size-3.5" />}{t("browse.open")}
                </button>
                <button type="button" onClick={copyResume} disabled={!ready} aria-label={t("browse.copyCommandAria")}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted disabled:opacity-60">
                  {copied ? <Check className="size-3.5 text-primary" /> : <Copy className="size-3.5" />}
                  {copied ? t("browse.copied") : t("browse.copy")}
                </button>
              </div>
              {resumeMsg && (
                <div className={`mt-1 text-[10.5px] ${resumeMsg.ok ? "text-muted-foreground" : "text-destructive"}`}>{resumeMsg.text}</div>
              )}
              {detailErr && !resumeMsg && (
                <div className="mt-1 text-[10.5px] text-destructive">{t("browse.detailLoadFailed")}</div>
              )}
              {ready && !fileExists && canRestore && (
                <div className="mt-1 flex items-center gap-1.5">
                  <button type="button" onClick={doRestore} disabled={restoring}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-1.5 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-60">
                    {restoring ? <Loader2 className="size-3.5 animate-spin" /> : <ArchiveRestore className="size-3.5" />}{t("browse.restore")}
                  </button>
                  <span className="text-[10.5px] text-muted-foreground">{t("browse.restoreHint")}</span>
                </div>
              )}
              {ready && !fileExists && !canRestore && !resumeMsg && (
                <div id="resume-missing-note" className="mt-1 text-[10.5px] text-destructive">{t("browse.noLogNote")}</div>
              )}
              {/* 원문·미러 둘 다 없는 레거시 세션: 재개는 불가능해도 아카이브(turns)로 열람용 markdown은 내보낼 수 있음(#190) */}
              {ready && !fileExists && !canRestore && sel && (
                <a href={`/api/session/export?id=${encodeURIComponent(sel)}`} download={`${sel}.md`}
                  className="mt-1 inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
                  <FileDown className="size-3.5" />{t("browse.exportMarkdown")}
                </a>
              )}
            </div>
          )}
          <div className="relative">
            <Magnifier className="pointer-events-none absolute left-3 top-1/2 size-[17px] -translate-y-1/2 text-muted-foreground" />
            <Input value={q} onChange={(e) => runSearch(e.target.value)} aria-label={t("browse.searchPlaceholder", { title })} placeholder={t("browse.searchPlaceholder", { title })} className="h-9 rounded-lg pl-9 text-sm" />
            {searching && <Loader2 className="absolute right-3 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" />}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <SegmentedRadioGroup label={t("search.modeLabel")} value={mode}
              onChange={(v) => { setMode(v); runSearch(q, v) }}
              options={MODES.map((m) => ({ value: m.v, label: <><m.Icon className="size-3.5" />{t(m.key)}</> }))} />
            <label className="inline-flex items-center gap-1">{t("browse.since")}
              <input type="date" value={since} onClick={openPicker} onFocus={openPicker} onChange={(e) => { setSince(e.target.value); runSearch() }}
                className="cursor-pointer rounded-md border bg-card px-1.5 py-1 tabular-nums outline-none shadow-sm [color-scheme:light_dark]" /></label>
            <label className="inline-flex items-center gap-1">{t("browse.until")}
              <input type="date" value={until} onClick={openPicker} onFocus={openPicker} onChange={(e) => { setUntil(e.target.value); runSearch() }}
                className="cursor-pointer rounded-md border bg-card px-1.5 py-1 tabular-nums outline-none shadow-sm [color-scheme:light_dark]" /></label>
          </div>
        </div>
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
          {!convs && <div className="grid h-full place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>}
          {hits !== null ? (
            <>
              {!searching && hits.length === 0 && <div className={`py-6 text-center ${searchErr ? "text-destructive" : "text-muted-foreground"}`}>{searchErr || t("browse.noResults")}</div>}
              {hits.map((h) => (
                <button key={h.id} onClick={() => setSelTurn({ session: h.session_full, turn: h.id })}
                  className={`cm-cv-row w-full rounded-lg border p-2.5 text-left transition-colors ${selTurn?.turn === h.id ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
                  <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-muted-foreground tabular-nums">
                    {h.sources.map((s) => <Badge key={s} variant={s === "keyword" ? "secondary" : "default"} className="h-4 px-1.5 text-[9.5px]">{t(s === "keyword" ? "search.srcKeyword" : "search.srcSemantic")}</Badge>)}
                    <span>{h.cosine != null ? `cos ${h.cosine.toFixed(2)}` : t("browse.keywordMatch")}</span><span className="opacity-40">·</span><span>{fmtTime(h.timestamp)}</span>
                  </div>
                  <div className="line-clamp-2 text-[13px] font-medium leading-snug">{h.summary || h.question || t("browse.untitled")}</div>
                </button>
              ))}
            </>
          ) : (
            convs?.map((it) => (
              <button key={it.t} onClick={() => setSelTurn({ session: it.s, turn: it.t })}
                className={`cm-cv-row w-full rounded-lg border p-2.5 text-left transition-colors ${selTurn?.turn === it.t ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
                <div className="line-clamp-2 text-[13px] font-medium leading-snug">{it.h || t("browse.untitled")}</div>
                <div className="mt-0.5 text-[10.5px] text-muted-foreground tabular-nums">{t("browse.sessionId", { id: it.s.slice(0, 8) })}</div>
              </button>
            ))
          )}
        </div>
      </div>

      {/* 가운데: 채팅 */}
      <div className="min-h-0 overflow-hidden">
        {selTurn
          ? <ChatThread key={`${selTurn.session}:${selTurn.turn}`} session={selTurn.session} focusTurn={selTurn.turn} />
          : <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground">{t("browse.selectPrompt")}</div>}
      </div>

      {/* 오른쪽: 그룹 목록(전환용) */}
      <div className="flex min-h-0 flex-col border-l">
        <div className="shrink-0 border-b px-3 py-2.5 text-xs font-medium text-muted-foreground">{t("browse.groupListTitle", { title })}</div>
        {groupList(true)}
      </div>
    </div>
  )
}
