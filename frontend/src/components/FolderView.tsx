import { Fragment, useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, ChevronUp, FolderPlus,
  Folder as FolderIcon, GripVertical, Loader2,
  MessagesSquare, Pencil, Search as SearchIcon, Tag, Trash2, X,
} from "lucide-react"
import { Input } from "@/components/ui/input"
import {
  createFolder, deleteFolder, getFolder, hideSession, hideTurn, listFolders, moveFolder,
  removeFromFolder, renameFolder, renameFolderItem, reorderFolder, search, unhideSession, unhideTurn,
} from "@/lib/api"
import { ChatThread } from "./ChatThread"
import { useDialogs } from "@/components/ui/dialogs"
import { errText } from "@/lib/errors"
import { childrenOf, flattenTree } from "@/lib/foldertree"
import { fmtTime } from "@/lib/format"
import type { Folder, FolderDetail, FolderItem, Hit } from "@/lib/types"

// 폴더(#201) = 사용자가 직접 만드는 수동 군집. 자동 군집(의미 지도)이 알아서 묶어주는 것과 달리
// 원하는 것만 모아두고, 그 안에서만 검색한다. 왼쪽 트리에서 고르고 오른쪽에서 내용·검색.

// 폴더를 끌어다 놓을 때, 행의 어느 높이에 놓았는지로 '순서'와 '뎁스'를 구분한다
// (Finder·VS Code 방식). 위/아래 가장자리 = 그 자리로, 가운데 = 그 폴더 안으로.
type DropZone = "before" | "inside" | "after"
const EDGE = 0.3   // 위·아래 각각 30% 는 순서 이동, 가운데 40% 는 안으로 넣기
function zoneOf(e: React.DragEvent<HTMLElement>): DropZone {
  const r = e.currentTarget.getBoundingClientRect()
  const ratio = (e.clientY - r.top) / (r.height || 1)
  if (ratio < EDGE) return "before"
  if (ratio > 1 - EDGE) return "after"
  return "inside"
}

// 계층을 눈으로 바로 알 수 있게 트리 가이드(├ └ │)를 그린다. 들여쓰기만으로는 깊이가 헷갈린다.
// lines[i] = i번째 조상이 아래로 더 이어지는지(=그 조상에게 다음 형제가 있는지) → │ 를 이어 그림.
function TreeGuide({ lines, last }: { lines: boolean[]; last: boolean }) {
  if (lines.length === 0) return null
  return (
    <span aria-hidden className="flex shrink-0 select-none font-mono text-[11px] leading-none text-muted-foreground/80">
      {lines.slice(0, -1).map((cont, i) => (
        <span key={i} className="inline-block w-3.5 text-center">{cont ? "│" : ""}</span>
      ))}
      <span className="inline-block w-3.5 text-center">{last ? "└" : "├"}</span>
    </span>
  )
}

function FolderTree({ parent, byParent, sel, collapsed, depth, lines = [], onPick, onToggle, drag }: {
  parent: number | null
  byParent: Map<number | null, Folder[]>
  sel: number | null
  collapsed: Set<number>
  depth: number
  lines?: boolean[]        // 조상들이 아래로 이어지는지 — 가이드 세로선 연결용
  onPick: (id: number) => void
  onToggle: (id: number) => void
  // 폴더 드래그: 행 위/아래 가장자리에 놓으면 '그 자리로'(순서), 가운데면 '그 안으로'(뎁스).
  drag: {
    id: number | null
    over: { id: number; zone: DropZone } | null
    start: (id: number) => void
    // rect/depth 를 함께 넘긴다 — 부모가 이걸로 '레이아웃을 밀지 않는' 표시선 위치를 계산한다.
    over_: (v: { id: number; zone: DropZone; rect: DOMRect; depth: number } | null) => void
    drop: (target: number, zone: DropZone) => void
    movingName?: string                      // 자리표시(고스트) 행에 쓸 이름
    blocked: Set<number>                     // 놓을 수 없는 대상(자기 자신 + 자기 하위)
    end: () => void
  }
}) {
  const rows = byParent.get(parent) ?? []
  return (
    <>
      {rows.map((f, i) => {
        const isLast = i === rows.length - 1
        const kids = byParent.get(f.id) ?? []
        const isCollapsed = collapsed.has(f.id)
        const over = drag.over?.id === f.id && drag.id !== f.id ? drag.over.zone : null
        return (
          <div key={f.id}>
            <div
              draggable
              onDragStart={(e) => { drag.start(f.id); e.dataTransfer.effectAllowed = "move" }}
              onDragOver={(e) => {
                if (drag.id == null) return
                // stopPropagation 필수: 이게 없으면 이벤트가 패널까지 올라가 '빈 곳' 핸들러가
                // 대상 폴더를 지워버려(=항상 최상위로) 판정이 뭉개진다.
                e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = "move"
                // 자기 자신·자기 하위: 놓아도 서버가 막는 자리다. 미리보기를 띄우면
                // '된다'고 약속해놓고 실패하는 셈이라, 표시를 지우고 불가로 알린다.
                if (drag.blocked.has(f.id)) {
                  e.dataTransfer.dropEffect = "none"
                  if (drag.over) drag.over_(null)
                  return
                }
                const zone = zoneOf(e)
                if (drag.over?.id !== f.id || drag.over.zone !== zone) {
                  drag.over_({ id: f.id, zone, rect: e.currentTarget.getBoundingClientRect(), depth })
                }
              }}
              onDrop={(e) => {
                e.preventDefault(); e.stopPropagation()
                if (!drag.blocked.has(f.id)) drag.drop(f.id, zoneOf(e))
              }}
              onDragEnd={drag.end}
              className={`flex cursor-grab items-center gap-1 rounded-md pr-2 text-sm transition-colors active:cursor-grabbing ${
                sel === f.id ? "bg-primary/10 text-primary" : "hover:bg-muted"} ${
                // 끌고 있는 원본은 흐리게(어디서 집었는지 표시). 숨기면 브라우저가 드래그를 취소한다.
                drag.id === f.id ? "opacity-40" : ""} ${
                drag.id != null && drag.id !== f.id && drag.blocked.has(f.id) ? "opacity-40" : ""} ${
                over === "inside" ? "ring-1 ring-primary/60" : ""}`}
              style={{ paddingLeft: "4px" }}
            >
              <TreeGuide lines={lines} last={isLast} />
              <button type="button" onClick={() => onToggle(f.id)} aria-label={String(f.name)}
                className={`grid size-4 shrink-0 place-items-center rounded ${kids.length ? "hover:bg-muted-foreground/20" : "invisible"}`}>
                <ChevronRight className={`size-3 transition-transform ${isCollapsed ? "" : "rotate-90"}`} />
              </button>
              <button type="button" onClick={() => onPick(f.id)}
                className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left">
                <FolderIcon className="size-3.5 shrink-0" />
                <span className="min-w-0 flex-1 truncate">{f.name}</span>
                {f.items > 0 && <span className="shrink-0 text-[10.5px] text-muted-foreground tabular-nums">{f.items}</span>}
              </button>
            </div>
            {!isCollapsed && kids.length > 0 && (
              <FolderTree parent={f.id} byParent={byParent} sel={sel} collapsed={collapsed}
                depth={depth + 1} lines={[...lines, !isLast]}
                onPick={onPick} onToggle={onToggle} drag={drag} />
            )}
          </div>
        )
      })}
    </>
  )
}

export function FolderView() {
  const { t } = useTranslation()
  const { confirm, prompt } = useDialogs()
  const [folders, setFolders] = useState<Folder[] | null>(null)
  const [sel, setSel] = useState<number | null>(null)
  const [detail, setDetail] = useState<FolderDetail | null>(null)
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [err, setErr] = useState("")
  const [q, setQ] = useState("")
  const [hits, setHits] = useState<Hit[] | null>(null)   // null = 검색 안 한 상태(폴더 내용 표시)
  const [searching, setSearching] = useState(false)
  // 이 화면 안에서 열어 볼 대화(세션 탭으로 넘어가지 않는다 — 폴더를 보다가 맥락이 끊기지 않게).
  const [openConv, setOpenConv] = useState<{ session: string; turn?: string } | null>(null)
  // 드래그 정렬 상태: drag=집어든 항목, over=지금 놓일 자리(그 위에 표시선).
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [overIdx, setOverIdx] = useState<number | null>(null)
  const [itemLine, setItemLine] = useState<number | null>(null)   // 항목 표시선 y(컨테이너 기준)
  const itemsRef = useRef<HTMLDivElement | null>(null)
  // 폴더 트리 드래그(폴더를 다른 폴더 밑으로 이동).
  const [dragFolder, setDragFolder] = useState<number | null>(null)
  // 표시선은 절대 위치로 띄운다(react-arborist 의 Cursor, dnd-kit 의 DragOverlay 와 같은 방식).
  // 인라인으로 자리를 끼워 넣으면 행들이 밀리고, 그 이동이 커서 판정을 바꿔 진동·오작동이 생긴다.
  const [overFolder, setOverFolder] =
    useState<{ id: number; zone: DropZone; top: number; left: number } | null>(null)
  const treeRef = useRef<HTMLDivElement | null>(null)
  const reqId = useRef(0)   // 최신 검색만 반영

  const loadFolders = useCallback(() => {
    listFolders().then((r) => setFolders(r.folders)).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
  }, [t])
  useEffect(loadFolders, [loadFolders])

  // 아무것도 고르지 않았으면 트리 맨 위 폴더를 열어둔다 — 빈 오른쪽 패널로 시작하지 않게.
  // (폴더를 지워 선택이 풀렸을 때도 같은 규칙으로 다음 폴더가 열린다)
  useEffect(() => {
    if (sel != null || !folders?.length) return
    setSel(flattenTree(folders)[0]?.id ?? folders[0].id)
  }, [folders, sel])

  // 폴더를 바꾸면 그 폴더 내용을 불러오고 검색 상태는 초기화.
  useEffect(() => {
    setHits(null); setQ(""); setOpenConv(null)
    if (sel == null) { setDetail(null); return }
    setDetail(null)
    getFolder(sel).then(setDetail).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
  }, [sel, t])

  // 폴더만 열고 오른쪽을 '고르세요'로 두지 않는다 — 첫 항목의 대화를 미리 띄운다.
  // 원문이 사라진 항목(유령 참조)은 열 수 없으니 건너뛰고, 열 수 있는 첫 항목을 고른다.
  useEffect(() => {
    if (openConv != null || !detail?.items.length) return
    const first = detail.items.find((it) => !!it.session_id)
    if (!first) return
    setOpenConv(first.kind === "turn"
      ? { session: first.session_id!, turn: first.ref }
      : { session: first.session_id! })       // 세션 항목은 turn 없이 → 마지막 대화로 착지
  }, [detail, openConv])

  function reload() {
    loadFolders()
    if (sel != null) getFolder(sel).then(setDetail).catch(() => {})
  }

  async function addFolder(parentId: number | null) {
    const name = await prompt({
      title: parentId == null ? t("folders.new") : t("folders.newChild"),
      description: parentId == null ? t("folders.newPrompt") : t("folders.newChildPrompt"),
      confirmLabel: t("folders.create"),
    })
    if (!name) return
    try {
      const r = await createFolder(name, parentId)
      loadFolders(); setSel(r.id)
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  async function rename(f: Folder) {
    const name = await prompt({
      title: t("folders.rename"), description: t("folders.renamePrompt"),
      defaultValue: f.name, confirmLabel: t("common.save"),
    })
    if (!name || name === f.name) return
    try { await renameFolder(f.id, name); reload() }
    catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  async function remove(f: Folder) {
    const ok = await confirm({
      title: t("folders.deleteTitle", { name: f.name }),
      description: t("folders.deleteConfirm", { name: f.name }),
      confirmLabel: t("folders.delete"), danger: true,
    })
    if (!ok) return
    try {
      await deleteFolder(f.id)
      setSel(null); loadFolders()
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  // 폴더에서만 쓰는 이름. 원본 턴/세션 제목은 그대로다.
  async function renameItem(it: FolderItem) {
    if (sel == null) return
    const alias = await prompt({
      title: t("folders.itemRename"),
      description: t("folders.itemRenameHint", { original: it.original_headline || it.headline }),
      // 별칭이 없으면 빈 칸에서 시작한다 — 원본 제목을 채워두면 확인만 눌러도 그 문자열이
      // 별칭으로 박혀 '별칭 있음' 상태가 되고, 되돌릴 방법이 없었다.
      defaultValue: it.alias ?? "", placeholder: it.original_headline || it.headline,
      confirmLabel: t("common.save"),
      allowEmpty: true,   // 비워서 저장 = 원본 제목으로 되돌리기(백엔드 계약)
    })
    if (alias == null) return
    try {
      await renameFolderItem(sel, it.kind === "turn" ? { turnId: it.ref } : { sessionId: it.ref }, alias)
      reload()
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  // 화면에 보이는 순서를 그대로 저장(낙관적 반영 후 실패하면 되돌림). 드래그·버튼이 공용.
  async function applyOrder(next: FolderItem[]) {
    if (sel == null || !detail) return
    setDetail({ ...detail, items: next })
    try {
      await reorderFolder(sel, next.map((x) => ({ kind: x.kind, ref: x.ref })))
    } catch (e) {
      setErr(errText(t, e, "folders.saveFailed")); reload()
    }
  }

  // 위/아래로 한 칸 — 드래그를 못 쓰는 경우(키보드)를 위한 경로라 드래그가 생겨도 남겨둔다.
  function moveItem(idx: number, dir: -1 | 1) {
    if (!detail) return
    const to = idx + dir
    if (to < 0 || to >= detail.items.length) return
    const next = [...detail.items]
    ;[next[idx], next[to]] = [next[to], next[idx]]
    applyOrder(next)
  }

  // 놓은 위치를 '부모 + 그 앞에 올 형제'로 번역한다.
  //   inside → 그 폴더의 자식으로(맨 뒤)
  //   before → 그 폴더와 같은 부모, 그 폴더 바로 앞
  //   after  → 같은 부모, 그 폴더 '다음' 형제의 앞(없으면 맨 뒤)
  // 자기 하위로 옮기는 건 서버가 400 으로 막는다(순환 방지).
  async function dropOnFolder(targetId: number, zone: DropZone) {
    const moving = dragFolder
    setDragFolder(null); setOverFolder(null)
    if (moving == null || moving === targetId || !folders) return
    const target = folders.find((f) => f.id === targetId)
    if (!target) return

    let parent: number | null
    let before: number | null = null
    if (zone === "inside") {
      parent = targetId
      // 접힌 폴더에 넣으면 방금 옮긴 게 화면에서 사라져 버린다 → 넣는 순간 펼쳐 보여준다.
      setCollapsed((prev) => {
        if (!prev.has(targetId)) return prev
        const next = new Set(prev); next.delete(targetId); return next
      })
    } else {
      parent = target.parent_id
      const sibs = folders.filter((f) => f.parent_id === parent && f.id !== moving)
      const at = sibs.findIndex((f) => f.id === targetId)
      before = zone === "before" ? targetId : (sibs[at + 1]?.id ?? null)
    }
    try {
      await moveFolder(moving, parent, before)
      loadFolders()
    } catch (e) { setErr(errText(t, e, "folders.moveFailed")) }
  }

  // 빈 곳에 놓으면 최상위 맨 뒤로(하위 폴더를 밖으로 빼내는 길).
  async function dropOnRoot() {
    const moving = dragFolder
    setDragFolder(null); setOverFolder(null)
    if (moving == null) return
    try {
      await moveFolder(moving, null)
      loadFolders()
    } catch (e) { setErr(errText(t, e, "folders.moveFailed")) }
  }

  // 폴더 안에서 바로 접기/펼치기(#128) — 접으려고 세션 화면까지 가지 않게.
  async function toggleFold(it: FolderItem) {
    try {
      if (it.kind === "turn") await (it.hidden ? unhideTurn(it.ref) : hideTurn(it.ref))
      else await (it.hidden ? unhideSession(it.ref) : hideSession(it.ref))
      reload()
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  // 드래그해서 원하는 자리에 놓기(HTML5 기본 드래그 — 라이브러리 없이).
  function dropItem(from: number, to: number) {
    if (!detail || from === to) return
    const next = [...detail.items]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    applyOrder(next)
  }

  async function removeItem(kind: "turn" | "session", ref: string) {
    if (sel == null) return
    try {
      await removeFromFolder(sel, kind === "turn" ? { turnId: ref } : { sessionId: ref })
      reload()
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  // 폴더 안에서만 검색(서버가 그 폴더+하위의 턴으로 범위를 좁힌다).
  async function runSearch(term: string) {
    setQ(term)
    if (sel == null) return
    if (!term.trim()) { reqId.current++; setHits(null); return }
    const my = ++reqId.current
    setSearching(true)
    try {
      const r = await search({ q: term.trim(), k: 30, folder: sel })
      if (my !== reqId.current) return
      // 임베딩 모델 미선택 등은 200 + {error, code} 로 온다 — 안 보면 실패가 '결과 없음'으로 보인다.
      if (r.code || r.error) { setHits([]); setErr(errText(t, r, "folders.searchFailed")); return }
      setHits(r.hits || [])
    } catch (e) {
      if (my !== reqId.current) return
      setHits([]); setErr(errText(t, e, "folders.searchFailed"))
    } finally {
      if (my === reqId.current) setSearching(false)
    }
  }

  const byParent = childrenOf(folders ?? [])
  const cur = detail?.folder

  // 끌고 있는 폴더와 그 하위 전체 = 놓을 수 없는 자리(트리가 순환하므로 서버도 막는다).
  const blocked = (() => {
    const out = new Set<number>()
    if (dragFolder == null || !folders) return out
    const stack = [dragFolder]
    while (stack.length) {
      const id = stack.pop()!
      if (out.has(id)) continue
      out.add(id)
      for (const c of folders) if (c.parent_id === id) stack.push(c.id)
    }
    return out
  })()


  return (
    <div className="grid h-full grid-cols-[minmax(200px,240px)_minmax(300px,360px)_1fr] overflow-hidden">
      {/* 왼쪽: 폴더 트리 */}
      <div className="flex min-h-0 flex-col border-r">
        <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2.5">
          <h2 className="flex-1 text-sm font-semibold">{t("folders.title")}</h2>
          <button type="button" onClick={() => addFolder(null)} title={t("folders.new")} aria-label={t("folders.new")}
            className="grid size-7 place-items-center rounded-md border transition-colors hover:bg-muted">
            <FolderPlus className="size-3.5" />
          </button>
        </div>
        {/* 빈 곳에 놓으면 최상위로 — 하위 폴더를 밖으로 빼낼 방법이 필요하다. */}
        <div ref={treeRef} className="relative min-h-0 flex-1 overflow-y-auto p-2"
          onDragOver={(e) => {
            if (dragFolder == null) return
            e.preventDefault()
            // '진짜 빈 곳'일 때만 대상을 지운다. 자리표시(pointer-events-none) 위를 지날 때는
            // 이벤트가 그 뒤의 이 패널로 통과하는데, 여기서 무조건 지우면 자리표시가 사라졌다
            // 다시 생기며 깜빡인다(상·하단 30% 구역에서만 나던 증상).
            if (e.target === e.currentTarget) setOverFolder(null)
          }}
          onDrop={(e) => {
            if (dragFolder == null) return
            e.preventDefault()
            // 자리표시 위에서 놓아도 미리 보여준 그 자리로 간다(최상위로 튕기지 않게).
            if (overFolder) dropOnFolder(overFolder.id, overFolder.zone)
            else dropOnRoot()
          }}
          // 안전장치: 끌던 행은 invisible 로 숨겨두므로, 드래그가 취소돼도 상태가 반드시
          // 풀려야 한다(안 그러면 그 폴더가 계속 안 보인다). dragend 는 위로 버블링된다.
          onDragEnd={() => { setDragFolder(null); setOverFolder(null) }}>
          {!folders && <div className="grid h-24 place-items-center text-muted-foreground"><Loader2 className="size-4 animate-spin" /></div>}
          {folders && folders.length === 0 && (
            <div className="px-2 py-6 text-center text-[12.5px] text-muted-foreground">{t("folders.empty")}</div>
          )}
          {folders && folders.length > 0 && (
            <FolderTree parent={null} byParent={byParent} sel={sel} collapsed={collapsed} depth={0}
              drag={{ id: dragFolder, over: overFolder, start: setDragFolder,
                      drop: dropOnFolder, blocked,
                      over_: (v) => {
                        if (!v) { setOverFolder(null); return }
                        const box = treeRef.current
                        if (!box) return
                        const b = box.getBoundingClientRect()
                        // 컨테이너 내부 좌표(스크롤 포함)로 변환 — 행이 움직이지 않으므로 안정적이다.
                        const y = (v.zone === "before" ? v.rect.top : v.rect.bottom) - b.top + box.scrollTop
                        setOverFolder({ id: v.id, zone: v.zone, top: y, left: v.depth * 14 + 20 })
                      },
                      end: () => { setDragFolder(null); setOverFolder(null) } }}
              onPick={setSel}
              onToggle={(id) => setCollapsed((prev) => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })} />
          )}
          {/* 놓일 자리 표시선 — 절대 위치라 행을 밀지 않는다(밀면 커서 판정이 흔들려 진동한다).
              '안으로 넣기'는 선이 아니라 대상 행의 테두리로 표시된다. */}
          {overFolder && overFolder.zone !== "inside" && (
            <div aria-hidden className="pointer-events-none absolute right-2 z-10 h-0.5 rounded-full bg-primary"
              style={{ top: overFolder.top - 1, left: overFolder.left }}>
              <span className="absolute -left-1 top-1/2 size-2 -translate-y-1/2 rounded-full bg-primary" />
            </div>
          )}
          {/* 트리가 패널을 가득 채우면 '빈 곳'이 없어 최상위로 뺄 방법이 사라진다 →
              드래그 중에는 맨 아래에 전용 드롭 자리를 띄운다. */}
          {dragFolder != null && folders?.some((f) => f.id === dragFolder && f.parent_id !== null) && (
            <div
              onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setOverFolder(null) }}
              onDrop={(e) => { e.preventDefault(); e.stopPropagation(); dropOnRoot() }}
              className="mt-2 rounded-md border border-dashed border-primary/50 bg-primary/5 px-2 py-2 text-center text-[11px] text-muted-foreground">
              {t("folders.dropToRootHint")}
            </div>
          )}
        </div>
      </div>

      {/* 가운데: 선택한 폴더의 내용 + 폴더 내 검색 */}
      <div className="flex min-h-0 flex-col border-r">
        {err && (
          <div className="flex shrink-0 items-center gap-2 border-b border-destructive/30 bg-destructive/5 px-4 py-2 text-[12px] text-destructive">
            <span className="flex-1">{err}</span>
            <button onClick={() => setErr("")} aria-label={t("common.close")}><X className="size-3.5" /></button>
          </div>
        )}
        {sel == null ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground">{t("folders.pickPrompt")}</div>
        ) : !detail ? (
          <div className="grid h-full place-items-center text-muted-foreground"><Loader2 className="size-5 animate-spin" /></div>
        ) : (
          <>
            <div className="shrink-0 border-b px-4 py-3">
              <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                {detail.path.map((p) => (
                  <span key={p.id} className="flex items-center gap-1.5">
                    <button type="button" onClick={() => setSel(p.id)} className="hover:text-foreground">{p.name}</button>
                    <ChevronRight className="size-3 opacity-50" />
                  </span>
                ))}
                <span className="font-medium text-foreground">{cur!.name}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <span className="text-[11.5px] text-muted-foreground tabular-nums">
                  {t("folders.turnCount", { count: detail.turn_count })}
                </span>
                <button type="button" onClick={() => addFolder(cur!.id)}
                  className="inline-flex items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
                  <FolderPlus className="size-3.5" />{t("folders.newChild")}
                </button>
                <button type="button" onClick={() => rename(cur!)}
                  className="inline-flex items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
                  <Pencil className="size-3.5" />{t("folders.rename")}
                </button>
                <button type="button" onClick={() => remove(cur!)}
                  className="inline-flex items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] text-destructive transition-colors hover:bg-destructive/10">
                  <Trash2 className="size-3.5" />{t("folders.delete")}
                </button>
              </div>
              <div className="relative mt-2">
                <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input value={q} onChange={(e) => runSearch(e.target.value)}
                  aria-label={t("folders.searchPlaceholder", { name: cur!.name })}
                  placeholder={t("folders.searchPlaceholder", { name: cur!.name })}
                  className="h-8 rounded-lg pl-8 text-[13px]" />
                {searching && <Loader2 className="absolute right-2.5 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" />}
              </div>
            </div>

            {/* 카드 사이 여백·목록 아래 빈 공간에서도 드롭을 허용해야 한다 — 여기서 preventDefault
                가 없으면 그 구간을 지날 때마다 '드롭 불가' 커서가 번쩍인다(카드 위에선 멀쩡한데). */}
            <div ref={itemsRef} className="relative min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3"
              onDragOver={(e) => { if (dragIdx != null) e.preventDefault() }}
              onDrop={(e) => {
                if (dragIdx == null) return
                e.preventDefault()
                // 자리표시 위에서 놓으면 이벤트가 여기로 통과해 온다 → 미리 보여준 그 자리로.
                // 진짜 빈 곳(목록 아래)에 놓았을 때만 맨 뒤로 보낸다.
                if (overIdx != null) dropItem(dragIdx, overIdx)
                else if (detail) dropItem(dragIdx, detail.items.length - 1)
                setDragIdx(null); setOverIdx(null); setItemLine(null)
              }}
              // 안전장치: 끌던 항목을 invisible 로 숨기므로 취소돼도 상태가 반드시 풀려야 한다.
              onDragEnd={() => { setDragIdx(null); setOverIdx(null); setItemLine(null) }}>
              {/* 놓일 자리 표시선(절대 위치 — 카드를 밀지 않는다) */}
              {dragIdx != null && itemLine != null && (
                <div aria-hidden className="pointer-events-none absolute inset-x-3 z-10 h-0.5 rounded-full bg-primary"
                  style={{ top: itemLine - 1 }} />
              )}
              {/* 검색 중이면 검색 결과, 아니면 폴더에 담긴 것들 */}
              {hits !== null ? (
                hits.length === 0 && !searching
                  ? <div className="py-10 text-center text-sm text-muted-foreground">{t("folders.noResults")}</div>
                  : hits.map((h) => (
                    <button key={h.id} onClick={() => setOpenConv({ session: h.session_full, turn: h.id })}
                      className={`w-full rounded-lg border p-3 text-left transition-colors ${
                        openConv?.turn === h.id ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
                      <div className="mb-1 text-[10.5px] text-muted-foreground tabular-nums">
                        {fmtTime(h.timestamp)} · {t("search.session", { n: h.session })}
                      </div>
                      <div className="line-clamp-2 text-[13.5px] font-medium leading-snug">{h.summary || h.question}</div>
                    </button>
                  ))
              ) : (
                <>
                  {detail.children.map((c) => (
                    <button key={`f${c.id}`} onClick={() => setSel(c.id)}
                      className="flex w-full items-center gap-2 rounded-lg border bg-card p-2.5 text-left transition-colors hover:bg-muted/50">
                      <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{c.name}</span>
                      <span className="shrink-0 text-[11px] text-muted-foreground tabular-nums">{c.items}</span>
                      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                    </button>
                  ))}
                  {detail.items.map((it, idx) => {
                    const active = it.kind === "session"
                      ? openConv?.session === it.ref && !openConv?.turn
                      : openConv?.turn === it.ref
                    return (
                      <Fragment key={`${it.kind}:${it.ref}`}>
                      <div
                        draggable
                        onDragStart={(e) => { setDragIdx(idx); e.dataTransfer.effectAllowed = "move" }}
                        onDragOver={(e) => {
                          if (dragIdx == null) return
                          e.preventDefault()                    // preventDefault 해야 드롭이 허용된다
                          e.dataTransfer.dropEffect = "move"
                          if (overIdx !== idx) {
                            const box = itemsRef.current
                            if (box) {
                              const r = e.currentTarget.getBoundingClientRect()
                              const b = box.getBoundingClientRect()
                              // 끌어올릴 땐 그 행 위, 내릴 땐 아래에 선을 그린다.
                              const y = (dragIdx > idx ? r.top : r.bottom) - b.top + box.scrollTop
                              setItemLine(y)
                            }
                            setOverIdx(idx)
                          }
                        }}
                        onDrop={(e) => {
                          e.preventDefault()
                          if (dragIdx != null) dropItem(dragIdx, idx)
                          setDragIdx(null); setOverIdx(null)
                        }}
                        onDragEnd={() => { setDragIdx(null); setOverIdx(null); setItemLine(null) }}
                        className={`group relative flex cursor-grab items-center gap-1.5 rounded-lg border p-2.5 transition-colors active:cursor-grabbing ${
                          active ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"} ${
                          dragIdx === idx ? "opacity-40" : ""}`}>
                        {/* 순서: 카드를 끌어 옮기거나(드래그), 키보드/정밀 조정용으로 위·아래 한 칸.
                            z-10: 아래 '열기' 버튼이 카드 전체로 펼친 클릭 영역보다 위에 오게. */}
                        <GripVertical className="relative z-10 size-3.5 shrink-0 text-muted-foreground/50 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
                        <span className="relative z-10 flex shrink-0 flex-col opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                          <button type="button" onClick={() => moveItem(idx, -1)} disabled={idx === 0}
                            title={t("folders.moveUp")} aria-label={t("folders.moveUp")}
                            className="rounded p-0.5 hover:bg-muted disabled:opacity-30">
                            <ChevronUp className="size-3" />
                          </button>
                          <button type="button" onClick={() => moveItem(idx, 1)} disabled={idx === detail.items.length - 1}
                            title={t("folders.moveDown")} aria-label={t("folders.moveDown")}
                            className="rounded p-0.5 hover:bg-muted disabled:opacity-30">
                            <ChevronDown className="size-3" />
                          </button>
                        </span>
                        {/* 클릭 영역을 카드 전체로(after:inset-0) — 여백이나 아이콘 자리를 눌러도 열리게.
                            버튼 자체를 카드로 키우면 안쪽 버튼이 중첩되므로 겹침 레이어로 넓힌다. */}
                        <button onClick={() => setOpenConv({
                          session: it.kind === "session" ? it.ref : (it.session_id ?? ""),
                          turn: it.kind === "turn" ? it.ref : undefined,
                        })} className="flex min-w-0 flex-1 items-center gap-2 text-left after:absolute after:inset-0 after:content-['']">
                          {it.kind === "session"
                            ? <MessagesSquare className="size-4 shrink-0 text-muted-foreground" />
                            : <span className="size-1.5 shrink-0 rounded-full bg-muted-foreground/60" />}
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-1">
                              <span className={`min-w-0 truncate text-[13px] font-medium ${it.hidden ? "text-muted-foreground line-through decoration-muted-foreground/40" : ""}`}>
                                {it.headline || t("folders.untitled")}
                              </span>
                              {it.alias && <Tag className="size-3 shrink-0 text-primary/70" aria-label={t("folders.aliasBadge")} />}
                              {it.hidden && <span className="shrink-0 rounded bg-muted px-1 text-[9.5px] font-medium text-muted-foreground">{t("browse.folded")}</span>}
                            </span>
                            <span className="block truncate text-[10.5px] text-muted-foreground tabular-nums">
                              {it.kind === "session" ? t("folders.sessionItem", { count: it.count ?? 0 }) : t("folders.turnItem")}
                              {it.timestamp ? ` · ${fmtTime(it.timestamp)}` : ""}
                            </span>
                          </span>
                        </button>
                        <span className="relative z-10 flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                          {/* 접기/펼치기(#128) — 폴더에 담아둔 채로 검색·지도에서만 빼둘 수 있게. */}
                          <button type="button" onClick={() => toggleFold(it)}
                            title={it.hidden ? t("chat.unfold") : t("folders.foldItem")}
                            aria-label={it.hidden ? t("chat.unfold") : t("folders.foldItem")}
                            className="rounded p-1 hover:bg-muted">
                            {it.hidden ? <ChevronsUpDown className="size-3.5" /> : <ChevronsDownUp className="size-3.5" />}
                          </button>
                          <button type="button" onClick={() => renameItem(it)}
                            title={t("folders.itemRename")} aria-label={t("folders.itemRename")}
                            className="rounded p-1 hover:bg-muted">
                            <Pencil className="size-3.5" />
                          </button>
                          <button type="button" onClick={() => removeItem(it.kind, it.ref)}
                            title={t("folders.removeItem")} aria-label={t("folders.removeItem")}
                            className="rounded p-1 hover:bg-muted">
                            <X className="size-3.5" />
                          </button>
                        </span>
                      </div>
                      </Fragment>
                    )
                  })}
                  {detail.children.length === 0 && detail.items.length === 0 && (
                    <div className="py-10 text-center text-sm text-muted-foreground">{t("folders.emptyItems")}</div>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </div>

      {/* 오른쪽: 고른 항목의 대화를 이 화면 안에서(세션 탭으로 넘어가지 않는다 — 맥락이 끊기지 않게) */}
      <div className="min-h-0 overflow-hidden">
        {openConv?.session
          // 턴 항목은 그 턴으로, 세션 항목은(지목한 턴 없음) 마지막 대화로.
          ? <ChatThread key={`${openConv.session}:${openConv.turn ?? ""}`}
              session={openConv.session} focusTurn={openConv.turn} focusLast={!openConv.turn} />
          : <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground">
              {t("folders.pickItemPrompt")}
            </div>}
      </div>
    </div>
  )
}
