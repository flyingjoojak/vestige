import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  ChevronDown, ChevronRight, ChevronUp, FolderPlus, Folder as FolderIcon, Loader2,
  MessagesSquare, Pencil, Search as SearchIcon, Tag, Trash2, X,
} from "lucide-react"
import { Input } from "@/components/ui/input"
import {
  createFolder, deleteFolder, getFolder, listFolders, removeFromFolder, renameFolder,
  renameFolderItem, reorderFolder, search,
} from "@/lib/api"
import { ChatThread } from "./ChatThread"
import { useDialogs } from "@/components/ui/dialogs"
import { errText } from "@/lib/errors"
import { fmtTime } from "@/lib/format"
import type { Folder, FolderDetail, FolderItem, Hit } from "@/lib/types"

// 폴더(#201) = 사용자가 직접 만드는 수동 군집. 자동 군집(의미 지도)이 알아서 묶어주는 것과 달리
// 원하는 것만 모아두고, 그 안에서만 검색한다. 왼쪽 트리에서 고르고 오른쪽에서 내용·검색.

// 평평한 목록 → 부모별 자식 맵(트리 렌더용).
function childrenOf(folders: Folder[]): Map<number | null, Folder[]> {
  const m = new Map<number | null, Folder[]>()
  for (const f of folders) {
    const key = f.parent_id
    if (!m.has(key)) m.set(key, [])
    m.get(key)!.push(f)
  }
  return m
}

function FolderTree({ parent, byParent, sel, collapsed, depth, onPick, onToggle }: {
  parent: number | null
  byParent: Map<number | null, Folder[]>
  sel: number | null
  collapsed: Set<number>
  depth: number
  onPick: (id: number) => void
  onToggle: (id: number) => void
}) {
  const rows = byParent.get(parent) ?? []
  return (
    <>
      {rows.map((f) => {
        const kids = byParent.get(f.id) ?? []
        const isCollapsed = collapsed.has(f.id)
        return (
          <div key={f.id}>
            <div
              className={`flex items-center gap-1 rounded-md pr-2 text-sm transition-colors ${
                sel === f.id ? "bg-primary/10 text-primary" : "hover:bg-muted"}`}
              style={{ paddingLeft: `${depth * 14 + 4}px` }}
            >
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
                depth={depth + 1} onPick={onPick} onToggle={onToggle} />
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
  const reqId = useRef(0)   // 최신 검색만 반영

  const loadFolders = useCallback(() => {
    listFolders().then((r) => setFolders(r.folders)).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
  }, [t])
  useEffect(loadFolders, [loadFolders])

  // 폴더를 바꾸면 그 폴더 내용을 불러오고 검색 상태는 초기화.
  useEffect(() => {
    setHits(null); setQ(""); setOpenConv(null)
    if (sel == null) { setDetail(null); return }
    setDetail(null)
    getFolder(sel).then(setDetail).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
  }, [sel, t])

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
      defaultValue: it.alias ?? it.headline, confirmLabel: t("common.save"),
    })
    if (alias == null) return
    try {
      await renameFolderItem(sel, it.kind === "turn" ? { turnId: it.ref } : { sessionId: it.ref }, alias)
      reload()
    } catch (e) { setErr(errText(t, e, "folders.saveFailed")) }
  }

  // 위/아래로 한 칸. 화면에 보이는 순서를 그대로 저장한다(낙관적 반영 후 실패하면 되돌림).
  async function moveItem(idx: number, dir: -1 | 1) {
    if (sel == null || !detail) return
    const next = [...detail.items]
    const to = idx + dir
    if (to < 0 || to >= next.length) return
    ;[next[idx], next[to]] = [next[to], next[idx]]
    setDetail({ ...detail, items: next })
    try {
      await reorderFolder(sel, next.map((x) => ({ kind: x.kind, ref: x.ref })))
    } catch (e) {
      setErr(errText(t, e, "folders.saveFailed")); reload()
    }
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
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {!folders && <div className="grid h-24 place-items-center text-muted-foreground"><Loader2 className="size-4 animate-spin" /></div>}
          {folders && folders.length === 0 && (
            <div className="px-2 py-6 text-center text-[12.5px] text-muted-foreground">{t("folders.empty")}</div>
          )}
          {folders && folders.length > 0 && (
            <FolderTree parent={null} byParent={byParent} sel={sel} collapsed={collapsed} depth={0}
              onPick={setSel}
              onToggle={(id) => setCollapsed((prev) => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })} />
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

            <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
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
                      <div key={`${it.kind}:${it.ref}`}
                        className={`group relative flex items-center gap-1.5 rounded-lg border p-2.5 transition-colors ${
                          active ? "border-primary/50 bg-primary/5" : "bg-card hover:bg-muted/50"}`}>
                        {/* 순서 바꾸기 — 드래그 대신 위/아래 한 칸(작은 목록엔 이게 더 확실하다).
                            z-10: 아래 '열기' 버튼이 카드 전체로 펼친 클릭 영역보다 위에 오게. */}
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
                              <span className="min-w-0 truncate text-[13px] font-medium">{it.headline || t("folders.untitled")}</span>
                              {it.alias && <Tag className="size-3 shrink-0 text-primary/70" aria-label={t("folders.aliasBadge")} />}
                            </span>
                            <span className="block truncate text-[10.5px] text-muted-foreground tabular-nums">
                              {it.kind === "session" ? t("folders.sessionItem", { count: it.count ?? 0 }) : t("folders.turnItem")}
                              {it.timestamp ? ` · ${fmtTime(it.timestamp)}` : ""}
                            </span>
                          </span>
                        </button>
                        <span className="relative z-10 flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
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
          ? <ChatThread key={`${openConv.session}:${openConv.turn ?? ""}`}
              session={openConv.session} focusTurn={openConv.turn} />
          : <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground">
              {t("folders.pickItemPrompt")}
            </div>}
      </div>
    </div>
  )
}
