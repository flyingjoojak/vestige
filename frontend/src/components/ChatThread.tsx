import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronRight, ChevronsDownUp, ChevronsUpDown, FileText, Loader2 } from "lucide-react"
import { getSession, hideSession as apiHideSession, hideTurn, unhideTurn } from "@/lib/api"
import { AddToFolder } from "./AddToFolder"
import { errText } from "@/lib/errors"
import { fmtTime, mdToHtml } from "@/lib/format"
import type { SessionDetail as Detail, SessionTurn } from "@/lib/types"

// 접힌 턴(#128): 사라지지 않고 제자리에 한 줄로 남는다 → 맥락이 유지되고 바로 펼 수 있다.
// (접힌 동안은 검색·지도에서만 빠진다)
function FoldedTurn({ t, i, highlight, onUnhide }: {
  t: SessionTurn; i: number; highlight: boolean; onUnhide: (id: string) => void
}) {
  const { t: tr } = useTranslation()
  const headline = t.summary || t.question || tr("chat.noQuestion")
  // 접힌 턴도 지목해서 들어올 수 있다('접힘' 화면의 '세션 열기') → 펼친 턴과 똑같이 그 자리로 이동·강조.
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => { if (highlight) ref.current?.scrollIntoView({ behavior: "instant" as ScrollBehavior, block: "start" }) }, [highlight])
  return (
    <div ref={ref} className={`flex scroll-mt-4 items-center gap-2 rounded-lg border border-dashed px-3 py-1.5 text-[11px] text-muted-foreground ${
      highlight ? "border-primary/50 bg-primary/5 ring-1 ring-primary/30" : "bg-muted/30"}`}>
      <span className="shrink-0 tabular-nums">#{i + 1}</span>
      <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-medium">{tr("chat.folded")}</span>
      {highlight && <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{tr("chat.selected")}</span>}
      <span className="min-w-0 flex-1 truncate" title={headline}>{headline}</span>
      <button type="button" onClick={() => onUnhide(t.id)}
        className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground">
        <ChevronsUpDown className="size-3" />{tr("chat.unfold")}
      </button>
    </div>
  )
}

// 한 턴을 채팅 말풍선(질문 우 / 답변 좌)으로. 접고 펴는 것 없이 항상 펼쳐 보여줌.
function Turn({ t, i, highlight, onHide }: { t: SessionTurn; i: number; highlight: boolean; onHide: (id: string) => void }) {
  const { t: tr } = useTranslation()
  const [openBash, setOpenBash] = useState(false)   // bash는 자동노출 X, 눌러서만
  const ref = useRef<HTMLDivElement | null>(null)
  // 선택한 턴이면 그 '상단'으로 즉시 이동(애니메이션 없이 한 번에 — 긴 내용도 번잡하지 않게).
  useEffect(() => { if (highlight) ref.current?.scrollIntoView({ behavior: "instant" as ScrollBehavior, block: "start" }) }, [highlight])
  return (
    <div ref={ref} className={`group scroll-mt-4 rounded-xl border p-3 ${highlight ? "border-primary/50 bg-primary/5 ring-1 ring-primary/30" : "bg-card"}`}>
      <div className="mb-2 flex items-center gap-2 text-[11px] text-muted-foreground tabular-nums">
        <span className="shrink-0">#{i + 1} · {fmtTime(t.timestamp)}</span>
        {highlight && <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{tr("chat.selected")}</span>}
        {t.summary && (
          <span className="min-w-0 flex-1 truncate">
            <FileText className="mr-1 -mt-0.5 inline size-3 text-primary/70" />{t.summary}
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <AddToFolder target={{ turnId: t.id }} />
          <button type="button" onClick={() => onHide(t.id)} title={tr("chat.foldTurn")} aria-label={tr("chat.foldTurn")}
            className="inline-flex shrink-0 items-center rounded p-0.5 transition-colors hover:bg-muted hover:text-foreground">
            <ChevronsDownUp className="size-3.5" />
          </button>
        </span>
      </div>
      <div className="flex flex-col items-end">
        <span className="mb-1 mr-1 text-[10px] font-medium text-muted-foreground">{tr("chat.question")}</span>
        <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-primary/10 px-3.5 py-2 text-sm ring-1 ring-primary/15">
          <div className="cm-md text-foreground" dangerouslySetInnerHTML={{ __html: mdToHtml(t.question) || tr("chat.noQuestion") }} />
        </div>
      </div>
      <div className="mt-2 flex flex-col items-start">
        <span className="mb-1 ml-1 text-[10px] font-medium text-muted-foreground">{tr("chat.answer")}</span>
        <div className="max-w-[80%] overflow-x-auto rounded-2xl rounded-bl-sm bg-muted px-3.5 py-2 text-sm">
          <div className="cm-md text-foreground" dangerouslySetInnerHTML={{ __html: mdToHtml(t.answer) || tr("chat.emptyAnswer") }} />
        </div>
      </div>
      {t.actions.length > 0 && (
        <div className="mt-2">
          <button onClick={() => setOpenBash((v) => !v)} aria-expanded={openBash}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ChevronRight className={`size-3.5 transition-transform ${openBash ? "rotate-90" : ""}`} />{tr("chat.actions", { count: t.actions.length })}
          </button>
          {openBash && <pre className="cm-code cm-md mt-2">{t.actions.join("\n")}</pre>}
        </div>
      )}
    </div>
  )
}

// 세션 전체를 채팅 스레드로 렌더. focusTurn이 있으면 그 턴을 강조+상단 스크롤.
const PAD = 25   // 포커스 턴 위/아래로 이만큼만 먼저 렌더(큰 세션 로딩 지연 방지)

// 포커스 턴 주변 렌더 창. data 를 받는 시점에 함께 확정해야 첫 렌더부터 최종 창으로 그려진다.
function windowFor(d: Detail, focusTurn?: string) {
  const n = d.turns.length
  const fi = focusTurn ? d.turns.findIndex((t) => t.id === focusTurn) : 0
  const c = fi >= 0 ? fi : 0
  return { s: Math.max(0, c - PAD), e: Math.min(n, c + PAD + 1) }
}

export function ChatThread({ session, focusTurn }: { session: string; focusTurn?: string }) {
  const { t } = useTranslation()
  const [data, setData] = useState<Detail | null>(null)
  const [err, setErr] = useState("")
  const [hideErr, setHideErr] = useState("")   // 접기/펼치기(#128) 실패 알림
  const [range, setRange] = useState<{ s: number; e: number }>({ s: 0, e: PAD * 2 })
  useEffect(() => {
    setData(null); setErr(""); setHideErr("")
    getSession(session).then((d) => {
      // 렌더 창을 data 와 '같은 렌더'에 확정한다(두 setState 는 배치됨). 효과에서 뒤늦게 잡으면
      // 첫 렌더가 기본 창(0~50)으로 그려지고, 그 사이 자식이 먼저 scrollIntoView 를 해버려
      // 곧이어 창이 바뀌며(위쪽 턴이 빠지며) 지목한 턴이 엉뚱한 위치로 밀린다(26~49번째에서 발생).
      setRange(windowFor(d, focusTurn))
      setData(d)
    }).catch((e) => setErr(String(e)))
  }, [session, focusTurn])

  // 접기/펼치기(#128): 목록에서 빼지 않고 hidden 플래그만 뒤집는다 → 제자리에서 바로 되돌릴 수 있다.
  function setFolded(ids: Set<string>, folded: boolean) {
    setData((d) => (d ? { ...d, turns: d.turns.map((x) => (ids.has(x.id) ? { ...x, hidden: folded } : x)) } : d))
  }
  async function foldTurn(id: string, folded: boolean) {
    const prev = folded   // 실패 시 되돌릴 값
    setFolded(new Set([id]), folded)   // 낙관적 반영(클릭 즉시 접힘/펼침)
    try {
      await (folded ? hideTurn(id) : unhideTurn(id))
    } catch (e) {
      setFolded(new Set([id]), !prev)  // 서버가 거부하면 화면도 되돌린다
      setHideErr(errText(t, e, "chat.foldFailed"))
    }
  }
  // 세션 전체 접기 — 확인 후 이 세션의 모든 턴을 한 번에(목록에선 '접힘'으로 남는다).
  async function foldWholeSession() {
    if (!window.confirm(t("chat.foldSessionConfirm"))) return
    const ids = new Set((data?.turns ?? []).map((x) => x.id))
    setFolded(ids, true)
    try {
      await apiHideSession(session)
    } catch (e) {
      setFolded(ids, false)
      setHideErr(errText(t, e, "chat.foldFailed"))
    }
  }

  const turns = data?.turns ?? []
  const foldedCount = turns.filter((x) => x.hidden).length
  const allFolded = turns.length > 0 && foldedCount === turns.length
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b px-5 py-3 text-[13px] text-muted-foreground tabular-nums">
        <span className="min-w-0 flex-1 truncate">
          {t("chat.sessionLabel", { id: session.slice(0, 8) })}{data ? ` · ${t("chat.turnCount", { count: data.count })}` : ""}
          {foldedCount > 0 && ` · ${t("chat.foldedCount", { count: foldedCount })}`}
        </span>
        {data && turns.length > 0 && (
          <AddToFolder target={{ sessionId: session }} showLabel
            className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted" />
        )}
        {data && turns.length > 0 && (
          allFolded
            ? <button type="button" onClick={() => turns.forEach((x) => foldTurn(x.id, false))} title={t("chat.unfoldAll")}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
                <ChevronsUpDown className="size-3.5" />{t("chat.unfoldAll")}
              </button>
            : <button type="button" onClick={foldWholeSession} title={t("chat.foldSession")}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
                <ChevronsDownUp className="size-3.5" />{t("chat.foldSession")}
              </button>
        )}
      </div>
      {hideErr && <div className="shrink-0 px-5 pt-2 text-[11px] text-destructive">{hideErr}</div>}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {err && <div className="py-10 text-center text-muted-foreground">{t("chat.error", { err })}</div>}
        {!data && !err && <div className="grid h-full place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>}
        {/* 읽기 좋은 폭으로 묶고 가운데 정렬 — 넓은 패널에서도 채팅답게 */}
        <div className="mx-auto max-w-3xl space-y-3">
          {data && range.s > 0 && (
            <button onClick={() => setRange((r) => ({ ...r, s: Math.max(0, r.s - 50) }))}
              className="mx-auto block rounded-md border bg-card px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground">{t("chat.loadPrev", { count: range.s })}</button>
          )}
          {turns.slice(range.s, range.e).map((turn, j) => (turn.hidden
            ? <FoldedTurn key={turn.id} t={turn} i={range.s + j} highlight={turn.id === focusTurn} onUnhide={(id) => foldTurn(id, false)} />
            : <Turn key={turn.id} t={turn} i={range.s + j} highlight={turn.id === focusTurn} onHide={(id) => foldTurn(id, true)} />))}
          {data && range.e < turns.length && (
            <button onClick={() => setRange((r) => ({ ...r, e: Math.min(turns.length, r.e + 50) }))}
              className="mx-auto block rounded-md border bg-card px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground">{t("chat.loadNext", { count: turns.length - range.e })}</button>
          )}
        </div>
      </div>
    </div>
  )
}
