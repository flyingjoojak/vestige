import { useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { Check, FolderPlus, Loader2, X } from "lucide-react"
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
  const panelRef = useRef<HTMLDivElement>(null)
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
    // capture 로 받는 이유: 패널이 fixed 라 바깥 스크롤 컨테이너가 움직이면 좌표가 어긋난다.
    // 단 패널 자신의 목록 스크롤은 제외해야 한다 — 안 그러면 폴더가 많을 때 목록을 굴리는
    // 순간 닫혀서 아래쪽 폴더를 고를 수가 없다.
    const onScroll = (e: Event) => {
      const el = e.target as Node | null
      if (el && panelRef.current?.contains(el)) return
      setOpen(false)
    }
    const close = () => setOpen(false)
    window.addEventListener("scroll", onScroll, true)
    window.addEventListener("resize", close)
    return () => {
      window.removeEventListener("scroll", onScroll, true)
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
      try {
        await addToFolder(r.id, target)
      } catch (e) {
        // 부분 실패: 폴더는 만들어졌고 담기만 실패했다. 그냥 '저장 실패'로 뭉개면
        // 사용자는 빈 폴더가 왜 생겼는지 알 수 없다.
        setFolders(null)
        setErr(errText(t, e, "folders.createdButNotAdded"))
        return
      }
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
      {/* 패널이 닫힌 상태의 실패는 여기서 보여준다. '새 폴더 만들어 담기'는 모달을 띄우려고
          드롭다운을 먼저 닫으므로, 에러를 패널 안에만 두면 아무 데도 표시되지 않는다
          (폴더는 만들어졌는데 담기만 실패한 부분 실패가 특히 조용해진다). */}
      {err && !open && (
        <span role="alert" className="ml-1 inline-flex items-center gap-1 align-middle text-[11px] text-destructive">
          {err}
          <button type="button" onClick={(e) => { e.stopPropagation(); setErr("") }}
            aria-label={t("common.close")} className="rounded p-0.5 hover:bg-destructive/10">
            <X className="size-3" />
          </button>
        </span>
      )}
      {open && pos && createPortal(
        <>
          {/* 바깥 클릭 시 닫힘(장식용, AT엔 숨김) */}
          <button type="button" aria-hidden="true" tabIndex={-1}
            className="fixed inset-0 z-40 cursor-default" onClick={(e) => { e.stopPropagation(); setOpen(false) }} />
          <div id={panelId} ref={panelRef} role="group" aria-label={t("folders.addTo")}
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
