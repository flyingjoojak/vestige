import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronsUpDown, ExternalLink, Loader2 } from "lucide-react"
import { listHidden, unhideTurn } from "@/lib/api"
import { errText } from "@/lib/errors"
import { fmtTime } from "@/lib/format"
import type { HiddenItem } from "@/lib/types"

// 접힌 턴 모아보기(#128). 검색 결과에서 접으면 어느 세션이었는지 잊기 쉬워, 접은 것들을
// 한곳에서 다시 찾아 펼치거나 원본 세션으로 갈 수 있어야 한다(설정이 아니라 전용 화면).
export function FoldedView({ onOpenTurn, onChanged }: {
  onOpenTurn: (session: string, turn: string) => void
  onChanged?: () => void
}) {
  const { t } = useTranslation()
  const [items, setItems] = useState<HiddenItem[] | null>(null)
  const [err, setErr] = useState("")
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(() => {
    setErr("")
    listHidden().then((r) => setItems(r.hidden)).catch((e) => setErr(errText(t, e, "folded.loadFailed")))
  }, [t])
  useEffect(load, [load])

  async function unfold(turnId: string) {
    setBusy(turnId)
    try {
      await unhideTurn(turnId)
      setItems((prev) => (prev ? prev.filter((h) => h.turn_id !== turnId) : prev))
      onChanged?.()
    } catch (e) {
      setErr(errText(t, e, "folded.unfoldFailed"))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-5">
      <h2 className="text-lg font-semibold">{t("folded.title")}</h2>
      <p className="mb-4 mt-1 text-[12.5px] text-muted-foreground">{t("folded.note")}</p>

      {err && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
          <span className="text-destructive">{err}</span>
          <button onClick={load} className="rounded-md border bg-card px-2 py-1 text-[12px] hover:text-foreground">{t("common.retry")}</button>
        </div>
      )}
      {items == null && !err && (
        <div className="grid h-40 place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>
      )}
      {items && items.length === 0 && (
        <div className="grid h-40 place-items-center px-6 text-center text-sm text-muted-foreground">{t("folded.empty")}</div>
      )}

      <div className="space-y-1.5">
        {items?.map((h) => (
          <div key={h.turn_id}
            className="flex flex-wrap items-center gap-2 rounded-lg border border-dashed bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-medium">{t("browse.folded")}</span>
            <span className="min-w-0 flex-1 truncate text-[13px] text-foreground" title={h.headline}>
              {h.headline || t("folded.untitled")}
            </span>
            <span className="shrink-0 tabular-nums">{h.timestamp ? fmtTime(h.timestamp) : ""}</span>
            {h.session_id && (
              <button type="button" onClick={() => onOpenTurn(h.session_id!, h.turn_id)}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground">
                <ExternalLink className="size-3" />{t("folded.openSession")}
              </button>
            )}
            <button type="button" onClick={() => unfold(h.turn_id)} disabled={busy === h.turn_id}
              className="inline-flex shrink-0 items-center gap-1 rounded-md border bg-card px-1.5 py-0.5 transition-colors hover:bg-muted hover:text-foreground disabled:opacity-60">
              {busy === h.turn_id ? <Loader2 className="size-3 animate-spin" /> : <ChevronsUpDown className="size-3" />}{t("chat.unfold")}
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
