import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, RotateCcw } from "lucide-react"
import { listHidden, unhideTurn } from "@/lib/api"
import { errText } from "@/lib/errors"
import { fmtTime } from "@/lib/format"
import type { HiddenItem } from "@/lib/types"

// 설정 > 일반의 '숨김' 목록(#128) - 검색·세션·지도에서 숨긴 턴을 모아 보여주고 복원.
// 비파괴 기능이므로 여기서도 삭제가 아니라 hidden_turns 행 제거(=복원)만 한다.
export function HiddenPanel() {
  const { t } = useTranslation()
  const [items, setItems] = useState<HiddenItem[] | null>(null)
  const [err, setErr] = useState("")
  const [restoring, setRestoring] = useState<string | null>(null)

  function load() {
    setErr("")
    listHidden().then((r) => setItems(r.hidden)).catch((e) => setErr(errText(t, e, "settings.hiddenLoadFailed")))
  }
  useEffect(load, [t])

  async function restore(turnId: string) {
    setRestoring(turnId)
    try {
      await unhideTurn(turnId)
      setItems((prev) => (prev ? prev.filter((h) => h.turn_id !== turnId) : prev))
    } catch (e) {
      setErr(errText(t, e, "settings.hiddenRestoreFailed"))
    } finally {
      setRestoring(null)
    }
  }

  if (items == null && !err) {
    return <div className="flex items-center gap-2 py-3.5 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />{t("common.loading")}</div>
  }
  if (err) {
    return <div className="py-3.5 text-sm text-destructive">{err}</div>
  }
  if (items && items.length === 0) {
    return <div className="py-3.5 text-sm text-muted-foreground">{t("settings.hiddenEmpty")}</div>
  }
  return (
    <div className="divide-y">
      {items!.map((h) => (
        <div key={h.turn_id} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm">{h.question || t("settings.hiddenUntitled")}</div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {h.session_id ? h.session_id.slice(0, 8) : ""}{h.timestamp ? ` · ${fmtTime(h.timestamp)}` : ""}
            </div>
          </div>
          <button type="button" onClick={() => restore(h.turn_id)} disabled={restoring === h.turn_id}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors hover:bg-muted disabled:opacity-60">
            {restoring === h.turn_id ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />}{t("settings.hiddenRestore")}
          </button>
        </div>
      ))}
    </div>
  )
}
