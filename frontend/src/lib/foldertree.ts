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
