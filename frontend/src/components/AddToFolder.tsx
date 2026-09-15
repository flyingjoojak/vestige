import { useId, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, FolderPlus, Loader2 } from "lucide-react"
import { addToFolder, createFolder, listFolders, type FolderTarget } from "@/lib/api"
import { errText } from "@/lib/errors"
import type { Folder } from "@/lib/types"

// 검색 결과·대화·세션에서 폴더에 담는 버튼(#201). 여러 곳에서 같은 모양으로 쓰도록 한 컴포넌트로.
// 폴더 목록은 '열 때' 한 번만 불러온다 — 결과 30줄마다 미리 받아오면 열지도 않을 요청 30개가 된다.
// 담기 자체는 멱등(서버가 INSERT OR IGNORE)이라 이미 담긴 폴더를 눌러도 문제없다.
export function AddToFolder({ target, className, showLabel }: {
  target: FolderTarget; className?: string; showLabel?: boolean   // showLabel: 아이콘 옆에 글자도(헤더용)
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [folders, setFolders] = useState<Folder[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState("")      // 방금 담은 폴더 이름(잠깐 표시)
  const [err, setErr] = useState("")
  const triggerRef = useRef<HTMLButtonElement>(null)
  const doneTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const panelId = useId()

  function toggleOpen() {
    const next = !open
    setOpen(next)
    setErr("")
    if (next && folders == null) {
      listFolders().then((r) => setFolders(r.folders)).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
    }
  }

  function flashDone(name: string) {
    setDone(name)
    if (doneTimer.current) clearTimeout(doneTimer.current)
    doneTimer.current = setTimeout(() => setDone(""), 1800)
  }

  async function add(f: Folder) {
    setBusy(true)
    try {
      await addToFolder(f.id, target)
      setOpen(false)
      flashDone(f.name)
    } catch (e) {
      setErr(errText(t, e, "folders.saveFailed"))
    } finally {
      setBusy(false)
    }
  }

  async function addToNew() {
    const name = window.prompt(t("folders.newPrompt"))
    if (!name?.trim()) return
    setBusy(true)
    try {
      const r = await createFolder(name.trim())
      await addToFolder(r.id, target)
      setFolders(null)          // 다음에 열 때 새 목록으로
      setOpen(false)
      flashDone(name.trim())
    } catch (e) {
      setErr(errText(t, e, "folders.saveFailed"))
    } finally {
      setBusy(false)
    }
  }

  // 트리 들여쓰기용 깊이(부모를 따라 올라가며 셈).
  const depthOf = (f: Folder, all: Folder[]) => {
    let d = 0
    let cur = f.parent_id
    const byId = new Map(all.map((x) => [x.id, x]))
    while (cur != null && d < 20) { d++; cur = byId.get(cur)?.parent_id ?? null }
    return d
  }

  return (
    <span className="relative" onKeyDown={(e) => { if (e.key === "Escape" && open) { e.stopPropagation(); setOpen(false); triggerRef.current?.focus() } }}>
      <button ref={triggerRef} type="button"
        onClick={(e) => { e.stopPropagation(); toggleOpen() }}
        aria-expanded={open} aria-controls={open ? panelId : undefined}
        title={done ? t("folders.addedTo", { name: done }) : t("folders.addTo")}
        aria-label={t("folders.addTo")}
        className={className ?? "inline-flex shrink-0 items-center rounded p-0.5 transition-colors hover:bg-muted hover:text-foreground"}>
        {done ? <Check className="size-3.5 text-primary" /> : <FolderPlus className="size-3.5" />}
        {showLabel && (done ? t("folders.addedTo", { name: done }) : t("folders.addTo"))}
      </button>
      {open && (
        <>
          {/* 바깥 클릭 시 닫힘(장식용, AT엔 숨김) */}
          <button type="button" aria-hidden="true" tabIndex={-1}
            className="fixed inset-0 z-10 cursor-default" onClick={(e) => { e.stopPropagation(); setOpen(false) }} />
          <div id={panelId} role="group" aria-label={t("folders.addTo")}
            onClick={(e) => e.stopPropagation()}
            className="absolute right-0 z-20 mt-1 max-h-72 min-w-52 overflow-y-auto rounded-lg border bg-card p-1 text-[12px] shadow-lg">
            {folders == null && !err && (
              <div className="grid h-12 place-items-center text-muted-foreground"><Loader2 className="size-4 animate-spin" /></div>
            )}
            {err && <div className="px-2 py-1.5 text-destructive">{err}</div>}
            {folders?.length === 0 && (
              <div className="px-2 py-1.5 text-muted-foreground">{t("folders.empty")}</div>
            )}
            {folders?.map((f) => (
              <button key={f.id} type="button" disabled={busy} onClick={() => add(f)}
                style={{ paddingLeft: `${depthOf(f, folders) * 12 + 8}px` }}
                className="flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left transition-colors hover:bg-muted disabled:opacity-60">
                <span className="min-w-0 flex-1 truncate text-foreground">{f.name}</span>
                {f.items > 0 && <span className="shrink-0 tabular-nums text-[10.5px] text-muted-foreground">{f.items}</span>}
              </button>
            ))}
            <button type="button" disabled={busy} onClick={addToNew}
              className="mt-1 flex w-full items-center gap-1.5 rounded-md border-t px-2 py-1.5 text-left text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-60">
              <FolderPlus className="size-3.5" />{t("folders.newAndAdd")}
            </button>
          </div>
        </>
      )}
    </span>
  )
}
