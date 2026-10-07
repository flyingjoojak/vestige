import { useCallback, useEffect, useState, useRef } from "react"
import { useTranslation } from "react-i18next"
import { ChevronRight, ChevronsUpDown, ExternalLink, Loader2 } from "lucide-react"
import { listHidden, unhideSession, unhideTurn } from "@/lib/api"
import { errText } from "@/lib/errors"
import { rememberNeighborRow } from "@/lib/focus"
import { fmtTime } from "@/lib/format"
import type { FoldedChats, FoldedGroups, FoldedSession } from "@/lib/types"

// 접힌 것 모아보기(#128). 검색 결과에서 접으면 어느 세션이었는지 잊기 쉬워, 접은 것들을
// 한곳에서 다시 찾아 펼치거나 원본 세션으로 갈 수 있어야 한다(설정이 아니라 전용 화면).
//
// 세션 단위로 묶는다. 예전엔 접힌 대화를 하나씩 늘어놔서 500턴 세션을 접으면 500줄이 됐고,
// 200개를 넘는 부분은 아예 안 보였다.
//   - 접은 세션: 통째로 접은 세션. 세션 목록에서는 빠지므로 여기가 유일한 자리다
//   - 접은 채팅: 일부만 접힌 세션의 대화를 세션별로 묶는다(펼쳐 보기 전엔 한 줄)
export function FoldedView({ onOpenTurn, onChanged }: {
  onOpenTurn: (session: string, turn: string) => void
  onChanged?: () => void
}) {
  const { t } = useTranslation()
  const [data, setData] = useState<FoldedGroups | null>(null)
  const [err, setErr] = useState("")
  const [busy, setBusy] = useState<string | null>(null)
  const [opened, setOpened] = useState<Set<string>>(new Set())   // 펼쳐 본 채팅 묶음(세션 id)

  const load = useCallback(() => {
    setErr("")
    listHidden().then(setData).catch((e) => setErr(errText(t, e, "folded.loadFailed")))
  }, [t])
  useEffect(load, [load])

  const headingRef = useRef<HTMLHeadingElement>(null)
  async function run(key: string, fn: () => Promise<unknown>, apply: (d: FoldedGroups) => FoldedGroups) {
    // 줄은 요청이 끝난 뒤에 사라진다 - 누른 순간 이웃 줄(없으면 제목)을 잡아 두고, 사라진 뒤 그리로
    const moveFocus = rememberNeighborRow(headingRef.current)
    setBusy(key)
    try {
      await fn()
      setData((d) => (d ? apply(d) : d))
      moveFocus()
      onChanged?.()
    } catch (e) {
      setErr(errText(t, e, "folded.unfoldFailed"))
    } finally {
      setBusy(null)
    }
  }
  // 세션 전체 펼치기 - 접은 세션이든, 일부 접힌 세션의 '모두 펼치기'든 같은 요청이다.
  // 서버는 하위 에이전트 세션까지 함께 편다 - 그 줄들도 이 화면에서 빠지도록 펼친 뒤 목록을 다시 받는다.
  const unfoldSession = (sid: string) => run(sid, () => unhideSession(sid).then(load), (d) => ({
    ...d,
    sessions: d.sessions.filter((s) => s.session_id !== sid),
    chats: d.chats.filter((c) => c.session_id !== sid),
  }))
  const unfoldTurn = (sid: string, tid: string) => run(tid, () => unhideTurn(tid), (d) => ({
    ...d,
    chats: d.chats
      .map((c) => (c.session_id !== sid ? c
        : { ...c, folded: c.folded - 1, turns: c.turns.filter((x) => x.turn_id !== tid) }))
      .filter((c) => c.turns.length > 0),
  }))
  const toggle = (sid: string) => setOpened((prev) => {
    const next = new Set(prev)
    if (next.has(sid)) next.delete(sid)
    else next.add(sid)
    return next
  })

  const empty = data != null && data.sessions.length === 0 && data.chats.length === 0
  const chatCount = (data?.chats ?? []).reduce((n, c) => n + c.folded, 0)
  return (
    <div className="mx-auto max-w-3xl px-6 py-5">
      <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none">{t("folded.title")}</h2>
      <p className="mb-4 mt-1 text-[12.5px] text-muted-foreground">{t("folded.note")}</p>

      {err && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
          <span className="text-destructive">{err}</span>
          <button onClick={load} className="rounded-md border bg-card px-2 py-1 text-[12px] hover:text-foreground">{t("common.retry")}</button>
        </div>
      )}
      {data == null && !err && (
        <div className="grid h-40 place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>
      )}
      {empty && (
        <div className="grid h-40 place-items-center px-6 text-center text-sm text-muted-foreground">{t("folded.empty")}</div>
      )}

      {data && data.sessions.length > 0 && (
        <section aria-labelledby="folded-sessions" className="mb-6">
          <h3 id="folded-sessions" className="mb-2 text-[13px] font-semibold">
            {t("folded.sessions")} <span className="font-normal text-muted-foreground tabular-nums">{data.sessions.length}</span>
          </h3>
          <p className="mb-2 text-[11.5px] text-muted-foreground">{t("folded.sessionsHint")}</p>
          <div className="space-y-1.5">
            {data.sessions.map((s) => (
              <SessionRow key={s.session_id} s={s} busy={busy === s.session_id}
                onOpen={() => s.last_turn_id && onOpenTurn(s.session_id, s.last_turn_id)}
                onUnfold={() => unfoldSession(s.session_id)} />
            ))}
          </div>
        </section>
      )}

      {data && data.chats.length > 0 && (
        <section aria-labelledby="folded-chats">
          <h3 id="folded-chats" className="mb-2 text-[13px] font-semibold">
            {t("folded.chats")} <span className="font-normal text-muted-foreground tabular-nums">{chatCount}</span>
          </h3>
          <div className="space-y-1.5">
            {data.chats.map((c) => (
              <ChatGroup key={c.session_id} c={c} open={opened.has(c.session_id)} busy={busy}
                onToggle={() => toggle(c.session_id)}
                onOpenTurn={(tid) => onOpenTurn(c.session_id, tid)}
                onUnfoldTurn={(tid) => unfoldTurn(c.session_id, tid)}
                onUnfoldAll={() => unfoldSession(c.session_id)} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

const pill = "inline-flex shrink-0 items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground disabled:opacity-60"

function SessionRow({ s, busy, onOpen, onUnfold }: {
  s: FoldedSession; busy: boolean; onOpen: () => void; onUnfold: () => void
}) {
  const { t } = useTranslation()
  return (
    <div data-row className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
      <span className="min-w-0 flex-1 truncate text-[13px] text-foreground" title={s.headline}>{s.headline || t("folded.untitled")}</span>
      <span className="shrink-0 tabular-nums">{t("folded.turnCount", { count: s.total })} · {fmtTime(s.ended)}</span>
      {s.last_turn_id && (
        <button type="button" onClick={onOpen} className={pill}><ExternalLink className="size-3" />{t("folded.openSession")}</button>
      )}
      <button type="button" onClick={onUnfold} disabled={busy} className={pill}>
        {busy ? <Loader2 className="size-3 animate-spin" /> : <ChevronsUpDown className="size-3" />}{t("folded.unfoldSession")}
      </button>
    </div>
  )
}

function ChatGroup({ c, open, busy, onToggle, onOpenTurn, onUnfoldTurn, onUnfoldAll }: {
  c: FoldedChats; open: boolean; busy: string | null
  onToggle: () => void; onOpenTurn: (tid: string) => void; onUnfoldTurn: (tid: string) => void; onUnfoldAll: () => void
}) {
  const { t } = useTranslation()
  const listId = `folded-chats-${c.session_id}`
  return (
    <div data-row className="rounded-lg border border-dashed bg-muted/30 text-[11px] text-muted-foreground">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={listId}
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded text-left focus-visible:outline-2 focus-visible:outline-ring">
          <ChevronRight aria-hidden className={`size-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
          <span className="min-w-0 truncate text-[13px] text-foreground" title={c.headline}>{c.headline || t("folded.untitled")}</span>
        </button>
        <span className="shrink-0 tabular-nums">{t("folded.chatsInSession", { folded: c.folded, total: c.total })}</span>
        <button type="button" onClick={onUnfoldAll} disabled={busy === c.session_id} className={pill}>
          {busy === c.session_id ? <Loader2 className="size-3 animate-spin" /> : <ChevronsUpDown className="size-3" />}{t("folded.unfoldAll")}
        </button>
      </div>
      {open && (
        <ul id={listId} className="space-y-1 border-t border-dashed px-3 py-2">
          {c.turns.map((x) => (
            <li key={x.turn_id} data-row className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-[12.5px] text-foreground" title={x.headline}>{x.headline || t("folded.untitled")}</span>
              <span className="shrink-0 tabular-nums">{x.timestamp ? fmtTime(x.timestamp) : ""}</span>
              <button type="button" onClick={() => onOpenTurn(x.turn_id)} className={pill}><ExternalLink className="size-3" />{t("folded.openSession")}</button>
              <button type="button" onClick={() => onUnfoldTurn(x.turn_id)} disabled={busy === x.turn_id} className={pill}>
                {busy === x.turn_id ? <Loader2 className="size-3 animate-spin" /> : <ChevronsUpDown className="size-3" />}{t("chat.unfold")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
