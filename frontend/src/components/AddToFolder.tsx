import { useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { Check, FolderPlus, Loader2 } from "lucide-react"
import { addToFolder, createFolder, listFolders, type FolderTarget } from "@/lib/api"
import { useDialogs } from "@/components/ui/dialogs"
import { errText } from "@/lib/errors"
import { flattenTree } from "@/lib/foldertree"
import type { Folder } from "@/lib/types"

// 검색 결과·대화·세션에서 폴더에 담는 버튼(#201). 여러 곳에서 같은 모양으로 쓰도록 한 컴포넌트로.
// 폴더 목록은 '열 때' 한 번만 불러온다 — 결과 30줄마다 미리 받아오면 열지도 않을 요청 30개가 된다.
// 담기 자체는 멱등(서버가 INSERT OR IGNORE)이라 이미 담긴 폴더를 눌러도 문제없다.
export function AddToFolder({ target, className, showLabel }: {
  target: FolderTarget; className?: string; showLabel?: boolean   // showLabel: 아이콘 옆에 글자도(헤더용)
}) {
  const { t } = useTranslation()
  const { prompt } = useDialogs()
  const [open, setOpen] = useState(false)
  const [folders, setFolders] = useState<Folder[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState("")      // 방금 담은 폴더 이름(잠깐 표시)
  const [err, setErr] = useState("")
  // 드롭다운은 목록의 스크롤 영역(overflow-y-auto) 안에 있으면 잘린다 → body 로 포털해서
  // 화면 좌표(fixed)로 띄운다. 아래 공간이 부족하면 위로 뒤집는다.
  const [pos, setPos] = useState<{ top: number; left: number; flip: boolean } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const doneTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const panelId = useId()

  const PANEL_W = 208, PANEL_MAX_H = 288
  function place() {
    const r = triggerRef.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom
    const flip = below < PANEL_MAX_H && r.top > below   // 아래가 좁으면 위로
    setPos({
      top: flip ? r.top - 4 : r.bottom + 4,
      left: Math.min(Math.max(8, r.right - PANEL_W), window.innerWidth - PANEL_W - 8),
      flip,
    })
  }

  function toggleOpen() {
    const next = !open
    setOpen(next)
    setErr("")
    if (next) place()
    if (next && folders == null) {
      listFolders().then((r) => setFolders(r.folders)).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
    }
  }

  useEffect(() => {
    if (!open) return
    const close = () => setOpen(false)
    window.addEventListener("scroll", close, true)   // capture: 내부 스크롤 영역까지
    window.addEventListener("resize", close)
    return () => {
      window.removeEventListener("scroll", close, true)
      window.removeEventListener("resize", close)
    }
  }, [open])

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
    setOpen(false)   // 모달이 뜨는 동안 드롭다운은 닫아둔다
    const name = await prompt({
      title: t("folders.newAndAdd"), description: t("folders.newPrompt"), confirmLabel: t("folders.create"),
    })
    if (!name) return
    setBusy(true)
    try {
      const r = await createFolder(name)
      await addToFolder(r.id, target)
      setFolders(null)          // 다음에 열 때 새 목록으로
      flashDone(name)
    } catch (e) {
      setErr(errText(t, e, "folders.saveFailed"))
    } finally {
      setBusy(false)
    }
  }

  // 화면의 폴더 트리와 같은 순서·깊이로 보여준다(부모 바로 아래에 그 자식들).
  // 예전엔 서버가 준 평평한 목록을 그대로 두고 들여쓰기만 붙여, 자식이 부모와 멀리 떨어져 보였다.
  const tree = folders ? flattenTree(folders) : []

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
      {open && pos && createPortal(
        <>
          {/* 바깥 클릭 시 닫힘(장식용, AT엔 숨김) */}
          <button type="button" aria-hidden="true" tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default" onClick={(e) => { e.stopPropagation(); setOpen(false) }} />
          <div id={panelId} role="group" aria-label={t("folders.addTo")}
            onClick={(e) => e.stopPropagation()}
            style={{ top: pos.top, left: pos.left, width: PANEL_W, maxHeight: PANEL_MAX_H,
                     transform: pos.flip ? "translateY(-100%)" : undefined }}
            className="fixed z-50 overflow-y-auto rounded-lg border bg-card p-1 text-[12px] shadow-lg">
            {folders == null && !err && (
              <div className="grid h-12 place-items-center text-muted-foreground"><Loader2 className="size-4 animate-spin" /></div>
            )}
            {err && <div className="px-2 py-1.5 text-destructive">{err}</div>}
            {folders?.length === 0 && (
              <div className="px-2 py-1.5 text-muted-foreground">{t("folders.empty")}</div>
            )}
            {tree.map((f) => (
              <button key={f.id} type="button" disabled={busy} onClick={() => add(f)}
                style={{ paddingLeft: `${f.depth * 12 + 8}px` }}
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
        </>,
        document.body,
      )}
    </span>
  )
}
