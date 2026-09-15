import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  ChevronRight, FolderPlus, Folder as FolderIcon, Loader2, MessagesSquare,
  Pencil, Search as SearchIcon, Trash2, X,
} from "lucide-react"
import { Input } from "@/components/ui/input"
import {
  createFolder, deleteFolder, getFolder, listFolders, removeFromFolder, renameFolder, search,
} from "@/lib/api"
import { useDialogs } from "@/components/ui/dialogs"
import { errText } from "@/lib/errors"
import { fmtTime } from "@/lib/format"
import type { Folder, FolderDetail, Hit } from "@/lib/types"

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

export function FolderView({ onOpen }: { onOpen: (session: string, turn?: string) => void }) {
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
  const reqId = useRef(0)   // 최신 검색만 반영

  const loadFolders = useCallback(() => {
    listFolders().then((r) => setFolders(r.folders)).catch((e) => setErr(errText(t, e, "folders.loadFailed")))
  }, [t])
  useEffect(loadFolders, [loadFolders])

  // 폴더를 바꾸면 그 폴더 내용을 불러오고 검색 상태는 초기화.
  useEffect(() => {
    setHits(null); setQ("")
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
    <div className="grid h-full grid-cols-[minmax(220px,280px)_1fr] overflow-hidden">
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

      {/* 오른쪽: 선택한 폴더의 내용 + 폴더 내 검색 */}
      <div className="flex min-h-0 flex-col">
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
                    <button key={h.id} onClick={() => onOpen(h.session_full, h.id)}
                      className="w-full rounded-lg border bg-card p-3 text-left transition-colors hover:bg-muted/50">
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
                  {detail.items.map((it) => (
                    <div key={`${it.kind}:${it.ref}`}
                      className="group flex items-center gap-2 rounded-lg border bg-card p-2.5 transition-colors hover:bg-muted/50">
                      <button onClick={() => onOpen(it.session_id ?? "", it.kind === "turn" ? it.ref : undefined)}
                        className="flex min-w-0 flex-1 items-center gap-2 text-left">
                        {it.kind === "session"
                          ? <MessagesSquare className="size-4 shrink-0 text-muted-foreground" />
                          : <span className="size-1.5 shrink-0 rounded-full bg-muted-foreground/60" />}
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[13px] font-medium">{it.headline || t("folders.untitled")}</span>
                          <span className="block truncate text-[10.5px] text-muted-foreground tabular-nums">
                            {it.kind === "session" ? t("folders.sessionItem", { count: it.count ?? 0 }) : t("folders.turnItem")}
                            {it.timestamp ? ` · ${fmtTime(it.timestamp)}` : ""}
                          </span>
                        </span>
                      </button>
                      <button type="button" onClick={() => removeItem(it.kind, it.ref)}
                        title={t("folders.removeItem")} aria-label={t("folders.removeItem")}
                        className="shrink-0 rounded p-1 opacity-0 transition-opacity hover:bg-muted group-hover:opacity-100 focus-visible:opacity-100">
                        <X className="size-3.5" />
                      </button>
                    </div>
                  ))}
                  {detail.children.length === 0 && detail.items.length === 0 && (
                    <div className="py-10 text-center text-sm text-muted-foreground">{t("folders.emptyItems")}</div>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
