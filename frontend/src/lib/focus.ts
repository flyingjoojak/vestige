// 누른 버튼이 다시 그려지며 사라질 때(접기 → '펼치기'로 바뀜, 줄이 목록에서 빠짐) 키보드 포커스를
// 이어받을 곳으로 옮긴다. 안 옮기면 포커스가 문서 맨 앞으로 튀어 키보드·스크린리더 사용자가 보던
// 자리를 잃는다. 렌더가 끝난 뒤(두 프레임) 옮기고, 그 사이 사용자가 다른 곳에 포커스를 두었으면 건드리지 않는다.
function lost(): boolean {
  const a = document.activeElement
  return !a || a === document.body || !a.isConnected
}

// data-focus="<key>" 인 요소로
export function focusKeyAfterRender(key: string): void {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!lost()) return
    document.querySelector<HTMLElement>(`[data-focus="${CSS.escape(key)}"]`)?.focus()
  }))
}

// 지금 포커스가 든 줄([data-row])이 곧 사라질 때: 지금(누른 순간) 이웃 줄을 기억해 두고, 돌려준
// 함수를 줄이 사라진 뒤 부르면 그리로 옮긴다(다음 줄 → 이전 줄의 첫 버튼 → fallback). 누른 버튼이
// 요청 동안 disabled 가 되면 그때 이미 포커스가 빠지므로, 이웃은 누른 순간에 잡아야 한다.
export function rememberNeighborRow(fallback?: HTMLElement | null): () => void {
  const row = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>("[data-row]")
  const next = row?.nextElementSibling?.querySelector<HTMLElement>("button")
    ?? row?.previousElementSibling?.querySelector<HTMLElement>("button")
  return () => requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!lost()) return
    const target = next?.isConnected ? next : fallback
    target?.focus()
  }))
}
