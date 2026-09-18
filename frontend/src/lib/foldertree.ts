import type { Folder } from "./types"

// 평평한 폴더 목록을 '보이는 트리 순서'(부모 → 그 자식들 → 다음 형제)로 펼친다.
// 서버가 같은 부모 안의 순서로 정렬해 주므로, 여기선 부모-자식 관계로 다시 엮기만 하면 된다.
//
// 이게 없으면 목록이 부모와 무관한 순서로 나열되고 들여쓰기만 붙어, 어느 폴더가 어디 속하는지
// 읽을 수 없다(담기 드롭다운에서 실제로 그랬다).
export type FlatFolder = Folder & { depth: number }

export function childrenOf(folders: Folder[]): Map<number | null, Folder[]> {
  const m = new Map<number | null, Folder[]>()
  for (const f of folders) {
    const key = f.parent_id
    if (!m.has(key)) m.set(key, [])
    m.get(key)!.push(f)
  }
  return m
}

// 드래그 없이도 폴더를 옮길 수 있게, 네 방향 이동의 목표 위치를 미리 계산한다
// (WCAG 2.5.7 — 드래그로 되는 동작은 드래그 없이 단일 포인터로도 가능해야 한다.
//  터치 화면에서는 HTML5 네이티브 드래그 이벤트가 아예 안 와서 실질적으로도 필요하다).
//
// 서버 API 는 move_folder(folder_id, parent_id, before_id) 하나뿐이고,
// before_id = '내 뒤에 올 형제'(없으면 맨 끝)다. 네 방향을 그 형태로 바꿔준다.
export type MoveTarget = { parentId: number | null; beforeId: number | null }
export type TreeMoves = {
  up: MoveTarget | null        // 형제 중 한 칸 위로
  down: MoveTarget | null      // 형제 중 한 칸 아래로
  indent: MoveTarget | null    // 바로 위 형제의 자식으로(한 단계 안으로)
  outdent: MoveTarget | null   // 부모 밖으로(부모 바로 다음 자리)
}

export function treeMoves(folders: Folder[], id: number): TreeMoves {
  const none: TreeMoves = { up: null, down: null, indent: null, outdent: null }
  const me = folders.find((f) => f.id === id)
  if (!me) return none
  const byParent = childrenOf(folders)
  const sibs = byParent.get(me.parent_id) ?? []
  const i = sibs.findIndex((f) => f.id === id)
  if (i < 0) return none

  const prev = sibs[i - 1]
  // 한 칸 아래로 = 다음 형제 뒤로 = 'i+2 번째 형제 앞'(없으면 맨 끝)
  const afterNext = sibs[i + 2]
  const hasNext = i + 1 < sibs.length

  let outdent: MoveTarget | null = null
  if (me.parent_id != null) {
    const parent = folders.find((f) => f.id === me.parent_id)
    const grandId = parent ? parent.parent_id : null
    const pSibs = byParent.get(grandId) ?? []
    const pi = pSibs.findIndex((f) => f.id === me.parent_id)
    outdent = { parentId: grandId, beforeId: pi >= 0 ? (pSibs[pi + 1]?.id ?? null) : null }
  }

  return {
    up: prev ? { parentId: me.parent_id, beforeId: prev.id } : null,
    down: hasNext ? { parentId: me.parent_id, beforeId: afterNext?.id ?? null } : null,
    indent: prev ? { parentId: prev.id, beforeId: null } : null,   // 그 폴더의 마지막 자식으로
    outdent,
  }
}

export function flattenTree(folders: Folder[]): FlatFolder[] {
  const byParent = childrenOf(folders)
  const out: FlatFolder[] = []
  const seen = new Set<number>()   // 혹시 모를 순환에도 멈추도록(서버가 막지만 방어적으로)
  const walk = (parent: number | null, depth: number) => {
    for (const f of byParent.get(parent) ?? []) {
      if (seen.has(f.id)) continue
      seen.add(f.id)
      out.push({ ...f, depth })
      walk(f.id, depth + 1)
    }
  }
  walk(null, 0)
  // 부모가 사라진 고아가 있으면 최상위로 끌어올려 목록에서 누락되지 않게.
  for (const f of folders) if (!seen.has(f.id)) out.push({ ...f, depth: 0 })
  return out
}
