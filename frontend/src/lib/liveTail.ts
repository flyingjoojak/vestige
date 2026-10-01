// 실시간 표시(#249): 활동 중인 세션의 색인 전 꼬리를 화면의 대화에 합친다.
// 화면은 몇 초마다 꼬리만 받는다(세션 전체는 수 MB). 단독 검증: scripts/liveTail.check.ts
import type { SessionDetail, SessionTail, SessionTurn } from "./types"

// 같은 내용이면 같은 객체를 돌려줘야 그 턴을 다시 그리지 않는다(마크다운 렌더가 비싸다).
function same(a: SessionTurn, b: SessionTurn): boolean {
  return a.answer === b.answer && a.question === b.question && a.actions.length === b.actions.length
    && a.live === b.live && a.hidden === b.hidden && a.queued === b.queued
}

/** 꼬리를 합친 새 상세. 색인이 진행돼 DB 턴 수가 바뀌었으면 "refetch" — 꼬리였던 턴이 DB 로
 *  옮겨가 정제·접힘이 붙었을 수 있으니 전체를 다시 받아야 한다. */
export function mergeTail(d: SessionDetail, tail: SessionTail): SessionDetail | "refetch" {
  if (d.db_count !== undefined && tail.db_count !== d.db_count) return "refetch"
  const fresh = new Map(tail.turns.map((t) => [t.id, t]))
  // 꼬리에서 빠진 live 턴은 버린다(색인 전 턴은 꼬리에만 있다). DB 턴은 꼬리에 없어도 남는다.
  const turns = d.turns
    .filter((t) => !t.live || fresh.has(t.id))
    .map((t) => {
      const n = fresh.get(t.id)
      return n && !same(t, n) ? n : t
    })
  const have = new Set(turns.map((t) => t.id))
  for (const t of tail.turns) if (!have.has(t.id)) turns.push(t)
  const changed = turns.length !== d.turns.length || turns.some((t, i) => t !== d.turns[i])
  if (!changed && d.active === tail.active && d.live_skipped === tail.live_skipped) return d
  return { ...d, turns, count: turns.length, active: tail.active, live_skipped: tail.live_skipped }
}
