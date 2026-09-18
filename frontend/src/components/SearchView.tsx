import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { X, Blend, Brain, ChevronsDownUp, ChevronsUpDown, Type, SearchX, AlertTriangle } from "lucide-react"
import { Magnifier } from "@/components/ui/Magnifier"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { ChatThread } from "./ChatThread"
import { SourceFilter } from "./SourceFilter"
import { AddToFolder } from "./AddToFolder"
import { SegmentedRadioGroup } from "@/components/ui/SegmentedRadioGroup"
import { getSources, hideTurn, listSessions, search, unhideTurn, type SearchMode, type SourceOption } from "@/lib/api"
import { fmtTime } from "@/lib/format"
import { sourceLabel } from "@/lib/source"
import { errText } from "@/lib/errors"
import { clearRecentQueries, loadRecentQueries, pushRecentQuery } from "@/lib/recentQueries"
import type { Hit, SessionRow } from "@/lib/types"

// 날짜 input 클릭 시 네이티브 달력을 강제로 연다(웹뷰에서 안 뜨는 문제 대응).
function openPicker(e: React.MouseEvent<HTMLInputElement> | React.FocusEvent<HTMLInputElement>) {
  const el = e.currentTarget as HTMLInputElement & { showPicker?: () => void }
  try { el.showPicker?.() } catch { /* 미지원 브라우저 */ }
}

export function SearchView() {
  const { t } = useTranslation()
  const EXAMPLES = [t("search.ex1"), t("search.ex2"), t("search.ex3"), t("search.ex4"), t("search.ex5")]
  const MODES: { value: SearchMode; label: React.ReactNode }[] = [
    { value: "hybrid", label: <><Blend className="size-3.5" />{t("search.modeHybrid")}</> },
    { value: "semantic", label: <><Brain className="size-3.5" />{t("search.modeSemantic")}</> },
    { value: "keyword", label: <><Type className="size-3.5" />{t("search.modeKeyword")}</> },
  ]
  const [q, setQ] = useState("")
  const [mode, setMode] = useState<SearchMode>("hybrid")
  const [k, setK] = useState(15)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [hits, setHits] = useState<Hit[]>([])
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">("idle")
  const [searchErr, setSearchErr] = useState("")
  // 검색 전 화면(idle)에 띄울 것들 — 빈 검색창만 두면 '뭘 검색해야 하나'로 막힌다.
  const [recent, setRecent] = useState<string[]>(() => loadRecentQueries())
  const [recentSessions, setRecentSessions] = useState<SessionRow[] | null>(null)
  // 선택한 결과 → 오른쪽 채팅 스레드로 표시(열고닫기 없이 클릭 전환).
  const [sel, setSel] = useState<{ session: string; turn: string } | null>(null)
  // 검색 소스 필터: 데이터 있는 출처만 목록에 뜬다(1종뿐이면 필터 자체를 숨김).
  const [srcOpts, setSrcOpts] = useState<SourceOption[]>([])
  const [srcSel, setSrcSel] = useState<Set<string>>(new Set())
  const [hideErr, setHideErr] = useState("")   // 접기(#128) 실패 알림(간단히 잠깐 표시)
  // 이번 결과 목록에서 접은 턴들. 새 검색을 하면 비운다(서버가 이미 접힌 턴을 빼고 주므로).
  const [folded, setFolded] = useState<Set<string>>(new Set())
  const reqId = useRef(0)   // 최신 요청만 반영(빠른 연속 검색 시 오래된 응답이 덮어쓰기 방지)
  const hideErrTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (hideErrTimer.current) clearTimeout(hideErrTimer.current) }, [])

  // 검색 결과에서 접기/펼치기(#128). 지금 목록에서 바로 치우지 않고 '접힘' 한 줄로 남긴다 —
  // 방금 접은 걸 그 자리에서 되돌릴 수 있게. 실제로 빠지는 건 다음 검색부터(서버가 접힌 턴을 제외).
  async function fold(id: string, folded: boolean, e: React.MouseEvent) {
    e.stopPropagation()
    const mark = (on: boolean) => setFolded((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
    mark(folded)
    if (folded) setSel((s) => (s?.turn === id ? null : s))   // 접은 턴을 오른쪽에 계속 띄워두지 않음
    try {
      await (folded ? hideTurn(id) : unhideTurn(id))
    } catch (err) {
      mark(!folded)                                          // 서버가 거부하면 표시도 되돌린다
      setHideErr(errText(t, err, "search.foldFailed"))
      if (hideErrTimer.current) clearTimeout(hideErrTimer.current)
      hideErrTimer.current = setTimeout(() => setHideErr(""), 4000)
    }
  }

  useEffect(() => {
    let alive = true
    getSources().then((r) => {
      if (!alive) return
      setSrcOpts(r.sources)
      setSrcSel(new Set(r.sources.map((s) => s.source)))   // 기본=전체 선택
    }).catch(() => { /* 소스 목록 실패 시 필터만 숨김(검색은 전체로 동작) */ })
    // 검색 전 화면용 최근 대화. 실패하면 그 블록만 안 뜨고 검색은 그대로 된다(장식용 데이터).
    listSessions()
      .then((r) => { if (alive) setRecentSessions(r.sessions.filter((s) => !s.subagent).slice(0, 6)) })
      .catch(() => { if (alive) setRecentSessions([]) })
    return () => { alive = false }
  }, [])

  const hasMultipleSources = srcOpts.length > 1

  // 선택이 전체(또는 비었으면)면 undefined(=모든 출처), 부분집합일 때만 목록 전달.
  function sourcesParam(selSet: Set<string>): string[] | undefined {
    if (!hasMultipleSources) return undefined
    if (selSet.size === 0 || selSet.size >= srcOpts.length) return undefined
    return [...selSet]
  }

  async function run(query = q, m: SearchMode = mode, selSet: Set<string> = srcSel) {
    const term = query.trim()
    if (!term) { reqId.current++; setState("idle"); setHits([]); setSel(null); setFolded(new Set()); return }   // 진행 중 요청 무효화
    const myId = ++reqId.current
    setState("loading")
    try {
      const r = await search({ q: term, k, mode: m, since: since || undefined, until: until || undefined, sources: sourcesParam(selSet) })
      if (myId !== reqId.current) return   // 더 새 요청이 진행 중 → 이 응답 폐기
      if (r.code || r.error) {   // 200이지만 실패를 알림(예: no_embed_model) → '결과 없음'과 구분
        setHits([]); setSearchErr(errText(t, r, "search.errorTitle")); setState("error"); setSel(null); return
      }
      const list = r.hits || []
      setHits(list); setSearchErr(""); setState("done"); setFolded(new Set())
      setRecent(pushRecentQuery(term))   // 성공한 검색만 기록(오타·실패는 안 남김)
      setSel(list.length ? { session: list[0].session_full, turn: list[0].id } : null)   // 첫 결과 자동 선택
    } catch (e) {
      if (myId !== reqId.current) return
      // 실패는 '결과 없음'과 구분. 원인을 버리면 임베더 실패·500·네트워크 끊김이 다 똑같아 보인다.
      setHits([]); setSearchErr(errText(t, e, "search.errorTitle")); setState("error"); setSel(null)
    }
  }
  function pick(e: string) { setQ(e); run(e) }

  return (
    <div className="grid h-full grid-cols-[minmax(330px,390px)_1fr] overflow-hidden">
      {/* 왼쪽: 검색 + 결과 리스트 */}
      <div className="flex min-h-0 flex-col border-r">
        <div className="shrink-0 border-b p-4">
          <div className="relative">
            <Magnifier className="pointer-events-none absolute left-3 top-1/2 size-[17px] -translate-y-1/2 text-muted-foreground" />
            <Input
              autoFocus value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              aria-label={t("search.placeholder")}
              placeholder={t("search.placeholder")} className="h-11 rounded-lg pl-10 text-[15px] shadow-sm"
            />
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <SegmentedRadioGroup label={t("search.modeLabel")} value={mode}
              onChange={(v) => { setMode(v); run(q, v) }} options={MODES} />
            <label className="inline-flex items-center gap-1">{t("search.show")}
              <select value={k} onChange={(e) => { setK(+e.target.value); run() }}
                className="rounded-md border bg-card px-1.5 py-1 outline-none shadow-sm">
                {[8, 15, 30].map((n) => <option key={n}>{n}</option>)}
              </select>
            </label>
            {hasMultipleSources && (
              <SourceFilter available={srcOpts} selected={srcSel}
                onChange={(next) => { setSrcSel(next); run(q, mode, next) }} />
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <label className="inline-flex items-center gap-1">{t("search.since")}
              <input type="date" value={since} onClick={openPicker} onFocus={openPicker} onChange={(e) => { setSince(e.target.value); run() }}
                className="cursor-pointer rounded-md border bg-card px-1.5 py-1 tabular-nums outline-none shadow-sm [color-scheme:light_dark]" /></label>
            <label className="inline-flex items-center gap-1">{t("search.until")}
              <input type="date" value={until} onClick={openPicker} onFocus={openPicker} onChange={(e) => { setUntil(e.target.value); run() }}
                className="cursor-pointer rounded-md border bg-card px-1.5 py-1 tabular-nums outline-none shadow-sm [color-scheme:light_dark]" /></label>
            {(since || until) && (
              <button onClick={() => { setSince(""); setUntil(""); run() }}
                className="inline-flex items-center gap-1 rounded-md border bg-card px-2 py-1 hover:text-foreground">
                <X className="size-3" />{t("search.reset")}
              </button>)}
          </div>
          {state === "done" && (
            <div className="mt-2 text-xs text-muted-foreground tabular-nums">{t("search.resultsPrefix")}<b className="text-foreground">{hits.length}</b>{t("search.resultsSuffix")}</div>
          )}
          {hideErr && <div className="mt-1 text-[10.5px] text-destructive">{hideErr}</div>}
          {/* 스크린리더용 상태 안내(비시각 사용자에 검색 진행/결과 알림) */}
          <div className="sr-only" role="status" aria-live="polite">
            {state === "loading" ? t("search.srSearching") : state === "done" ? t("search.srResults", { n: hits.length }) : state === "error" ? t("search.srFailed") : ""}
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
          {state === "loading" && [0, 1, 2, 3].map((i) => (
            <div key={i} className="rounded-lg border bg-card p-3 shadow-sm">
              {["w-2/5", "w-11/12", "w-2/3"].map((wd, j) => (
                <div key={j} className={`my-2 h-3 rounded bg-muted ${wd} animate-pulse`} />
              ))}
            </div>
          ))}

          {state === "done" && hits.length === 0 && (
            <div className="py-12 text-center text-sm text-muted-foreground">
              <SearchX className="mx-auto mb-2.5 size-8 text-muted-foreground/60" />
              {/* 색인된 소스가 하나도 없으면 '검색 실패'가 아니라 '아직 색인 전'임을 알린다(첫 실행 오해 방지). */}
              {srcOpts.length === 0 ? t("search.emptyNotIndexed") : t("search.emptyResults")}
            </div>
          )}

          {state === "error" && (
            <div className="py-12 text-center text-sm">
              <AlertTriangle className="mx-auto mb-2.5 size-7 text-destructive/70" />
              <div className="text-destructive">{searchErr || t("search.errorTitle")}</div>
              {!searchErr && <div className="mt-1 text-muted-foreground">{t("search.errorHint")}</div>}
              <button onClick={() => run()} className="mt-3 rounded-lg border bg-card px-3 py-1.5 text-[13px] shadow-sm hover:text-foreground">{t("common.retry")}</button>
            </div>
          )}

          {state === "idle" && (
            <div className="space-y-5 py-4">
              {/* 최근 검색어: 같은 걸 다시 찾는 일이 잦다. 없으면(첫 실행) 이 블록을 아예 안 띄운다. */}
              {recent.length > 0 && (
                <section>
                  <div className="mb-2 flex items-center justify-between px-1">
                    <h3 className="text-[12px] font-medium text-muted-foreground">{t("search.recentQueries")}</h3>
                    <button type="button"
                      onClick={() => { clearRecentQueries(); setRecent([]) }}
                      className="rounded px-1.5 py-0.5 text-[11.5px] text-muted-foreground transition-colors hover:text-foreground">
                      {t("search.clearRecent")}
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5 px-1">
                    {recent.map((r) => (
                      <button key={r} type="button" onClick={() => pick(r)}
                        className="max-w-full truncate rounded-full border bg-card px-2.5 py-1 text-[12px] shadow-sm transition-colors hover:border-primary/50 hover:text-foreground">
                        {r}
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {/* 최근 대화: 검색어가 없어도 바로 들어갈 곳을 준다(빈 화면으로 시작하지 않게). */}
              {recentSessions != null && recentSessions.length > 0 && (
                <section>
                  <h3 className="mb-2 px-1 text-[12px] font-medium text-muted-foreground">{t("search.recentSessions")}</h3>
                  <div className="space-y-1">
                    {recentSessions.map((s) => (
                      <button key={s.session} type="button"
                        onClick={() => setSel({ session: s.session, turn: "" })}
                        className={`flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors hover:border-primary/50 hover:bg-muted/50 ${
                          sel?.session === s.session ? "border-primary/60 bg-primary/5" : "bg-card"}`}>
                        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
                          {s.headline || t("browse.untitled")}
                        </span>
                        {/* 표기는 세션 탭과 같은 키를 쓴다 — 같은 값이 화면마다 다르게 보이지 않게
                            ('대화 N개' = 턴 N개. 이 앱에서 세션 ⊃ 대화(턴)) */}
                        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                          {fmtTime(s.ended)} · {t("chat.turnCount", { count: s.count })}
                        </span>
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {/* 둘 다 없을 때(첫 실행·색인 전)만 예시를 보여준다 — 그때는 이게 유일한 단서다. */}
              {recent.length === 0 && recentSessions != null && recentSessions.length === 0 && (
                <div className="py-8 text-center text-muted-foreground">
                  <Magnifier className="mx-auto mb-3 size-8 text-muted-foreground/60" />
                  <div className="mb-4 text-sm">{t("search.idleHint")}</div>
                  <div className="flex flex-wrap justify-center gap-2 px-2">
                    {EXAMPLES.map((e) => (
                      <button key={e} onClick={() => pick(e)}
                        className="rounded-full border bg-card px-3 py-1.5 text-[12.5px] shadow-sm transition-colors hover:border-primary/50 hover:text-foreground">
                        {e}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {state === "done" && hits.map((h) => {
            const active = sel?.turn === h.id
            // 접은 결과는 목록에서 치우지 않고 한 줄로 남긴다 — 그 자리에서 되돌릴 수 있게.
            // 실제로 검색에서 빠지는 건 다음 검색부터(서버가 접힌 턴을 빼고 준다).
            if (folded.has(h.id)) {
              return (
                <div key={h.id} className="flex items-center gap-2 rounded-lg border border-dashed bg-muted/30 px-3 py-1.5 text-[11px] text-muted-foreground">
                  <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-medium">{t("browse.folded")}</span>
                  <span className="min-w-0 flex-1 truncate" title={h.summary || h.question}>{h.summary || h.question || t("search.untitled")}</span>
                  <button type="button" onClick={(e) => fold(h.id, false, e)}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground">
                    <ChevronsUpDown className="size-3" />{t("chat.unfold")}
                  </button>
                </div>
              )
            }
            return (
              <div key={h.id} role="button" tabIndex={0}
                onClick={() => setSel({ session: h.session_full, turn: h.id })}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSel({ session: h.session_full, turn: h.id }) } }}
                className={`group w-full cursor-pointer rounded-lg border p-3 text-left transition-colors ${active ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
                <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-muted-foreground tabular-nums">
                  {hasMultipleSources && h.source && (
                    <span className="rounded bg-muted px-1 py-0.5 text-[9.5px] font-medium text-foreground/70">{sourceLabel(h.source)}</span>
                  )}
                  {h.sources.map((s) => (
                    <Badge key={s} variant={s === "keyword" ? "secondary" : "default"} className="h-4 px-1.5 text-[9.5px]">{t(s === "keyword" ? "search.srcKeyword" : "search.srcSemantic")}</Badge>
                  ))}
                  <span>{h.cosine != null ? `cos ${h.cosine.toFixed(2)}` : t("search.keywordMatch")}</span>
                  <span className="opacity-40">·</span>
                  <span>{fmtTime(h.timestamp)}</span>
                  <span className="opacity-40">·</span>
                  <span>{t("search.session", { n: h.session })}</span>
                  <span className="ml-auto flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                    <AddToFolder target={{ turnId: h.id }} />
                    <button type="button" onClick={(e) => fold(h.id, true, e)}
                      title={t("search.foldTurn")} aria-label={t("search.foldTurn")}
                      className="inline-flex items-center rounded p-0.5 transition-colors hover:bg-muted hover:text-foreground">
                      <ChevronsDownUp className="size-3.5" />
                    </button>
                  </span>
                </div>
                <div className="line-clamp-2 text-[13.5px] font-medium leading-snug text-balance">
                  {h.summary || h.question || t("search.untitled")}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* 오른쪽: 선택한 결과의 세션을 채팅 스레드로 */}
      <div className="min-h-0 overflow-hidden">
        {sel
          // 검색 결과로 들어오면 그 턴으로, '최근 세션'으로 들어오면(지목한 턴 없음) 마지막 대화로.
          ? <ChatThread key={`${sel.session}:${sel.turn}`} session={sel.session}
              focusTurn={sel.turn} focusLast={!sel.turn} />
          : <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground">
              {t("search.detailPlaceholder")}
            </div>}
      </div>
    </div>
  )
}
