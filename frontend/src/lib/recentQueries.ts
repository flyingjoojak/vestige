// 최근 검색어 — 이 기기에만 남는다(서버로 보내지 않음). 같은 걸 다시 찾는 일이 잦아서 둔다.
// localStorage 는 시크릿 창·사이트 데이터 차단·미리보기에서 빈 값이거나 접근 자체가 throw 할 수
// 있으므로, 모든 접근을 감싸고 실패하면 '없음'으로 다룬다(화면은 그대로 동작해야 한다).
const KEY = "vestige.recentQueries"
const MAX = 8

export function loadRecentQueries(): string[] {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return []
    const v = JSON.parse(raw)
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x).slice(0, MAX) : []
  } catch {
    return []
  }
}

/** 검색어를 맨 앞에 넣고(중복은 위로 끌어올림) 최대 MAX개만 유지. 갱신된 목록을 돌려준다. */
export function pushRecentQuery(q: string): string[] {
  const term = q.trim()
  if (!term) return loadRecentQueries()
  const next = [term, ...loadRecentQueries().filter((x) => x !== term)].slice(0, MAX)
  try {
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch { /* 저장 못 해도 이번 세션 화면에는 반영된다 */ }
  return next
}

export function clearRecentQueries(): void {
  try {
    localStorage.removeItem(KEY)
  } catch { /* 지울 수 없으면 그대로 */ }
}
