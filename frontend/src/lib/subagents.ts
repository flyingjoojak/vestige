// 하위 에이전트(서브에이전트) 세션을 부모 아래로 모으는 순수 로직.
// Browse3Pane 에서 쓰지만 판단 부분만 떼어내 단독으로 검증할 수 있게 둔다(subagents.check.ts).

export type Nestable = { id: string; subagent?: boolean; parent?: string | null }

/** 하위 세션을 최상위에서 빼 부모 아래로 모은다.
 *
 * 부모를 못 찾으면(부모 로그가 정리돼 색인에 없는 경우) **최상위에 그대로 남긴다** —
 * 빼버리면 그 세션에 도달할 길이 아예 없어진다. 기존 동작과 같아 퇴행도 아니다. */
export function nestSubagents<T extends Nestable>(list: T[]): { top: T[]; kids: Map<string, T[]> } {
  const kids = new Map<string, T[]>()
  const ids = new Set(list.map((g) => g.id))
  const top: T[] = []
  for (const g of list) {
    if (g.subagent && g.parent && ids.has(g.parent)) {
      const a = kids.get(g.parent)
      if (a) a.push(g); else kids.set(g.parent, [g])
    } else top.push(g)
  }
  return { top, kids }
}

/** 목록에 실제로 그릴 하위들.
 *
 * 검색 중이면 **토글과 무관하게** 맞는 것만 보여준다. 접힌 하위가 검색어에 맞는데도
 * 안 보이면 그 세션은 검색으로 도달할 수 없다. 검색이 아니면 토글이 켜졌을 때만 전부. */
export function kidsToShow<T>(all: T[], term: string, hits: (x: T) => boolean, showSub: boolean): T[] {
  if (!all.length) return []
  if (term) return all.filter(hits)
  return showSub ? all : []
}

/** 최상위 목록 필터. 하위가 맞으면 **부모를 남긴다**(그래야 펼쳐서 보여줄 수 있다). */
export function filterTop<T extends Nestable>(
  top: T[], kids: Map<string, T[]>, term: string, hits: (x: T) => boolean,
): T[] {
  if (!term) return top
  return top.filter((g) => hits(g) || (kids.get(g.id) ?? []).some(hits))
}
