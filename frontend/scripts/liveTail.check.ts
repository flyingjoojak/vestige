// 단독 검증: `node scripts/liveTail.check.ts` (frontend/ 에서) (Node 22+ 는 TS 를 그대로 실행한다)
import assert from "node:assert/strict"
import { mergeTail } from "../src/lib/liveTail.ts"
import type { SessionDetail, SessionTail, SessionTurn } from "../src/lib/types.ts"

const turn = (id: string, answer: string, live = false): SessionTurn =>
  ({ id, timestamp: "", question: `q-${id}`, answer, actions: [], summary: null, tags: [], hidden: false, live })
const detail = (turns: SessionTurn[], db_count: number): SessionDetail =>
  ({ session: "s", project: "", count: turns.length, turns, db_count, active: true, live_skipped: 0 })
const tail = (turns: SessionTurn[], db_count: number): SessionTail =>
  ({ turns, db_count, active: true, live_skipped: 0 })

const t1 = turn("t1", "답1")
const held = turn("t2", "진행 중")
const d0 = detail([t1, held], 2)

// 1. 바뀐 게 없으면 같은 객체 - 화면이 아무것도 다시 그리지 않는다
assert.equal(mergeTail(d0, tail([held], 2)), d0, "변화 없으면 그대로 돌려줘야 한다")

// 2. 보류됐던 턴이 자라면 그 턴만 새 객체, 나머지는 같은 객체
const grown = turn("t2", "진행 중... 완료")
const d1 = mergeTail(d0, tail([grown], 2))
assert.notEqual(d1, "refetch")
if (d1 === "refetch") throw new Error()
assert.equal(d1.turns[0], t1, "안 바뀐 턴까지 새로 그리면 안 된다")
assert.equal(d1.turns[1].answer, "진행 중... 완료")

// 3. 새 색인 전 턴은 뒤에 붙는다
const fresh = turn("t3", "새 작업", true)
const d2 = mergeTail(d1, tail([grown, fresh], 2))
if (d2 === "refetch") throw new Error()
assert.deepEqual(d2.turns.map((t) => t.id), ["t1", "t2", "t3"])
assert.equal(d2.count, 3)

// 4. 색인이 진행돼 DB 턴 수가 바뀌면 전체를 다시 받는다(꼬리였던 턴에 정제·접힘이 붙었을 수 있다)
assert.equal(mergeTail(d2, tail([], 3)), "refetch")

// 5. 꼬리에서 빠진 live 턴은 버리고, DB 턴은 꼬리에 없어도 남는다
const d3 = mergeTail(d2, tail([], 2))
if (d3 === "refetch") throw new Error()
assert.deepEqual(d3.turns.map((t) => t.id), ["t1", "t2"])

// 6. 활동이 멈추면(active=false) 그것도 반영돼야 화면이 다시 읽기를 멈춘다
const d4 = mergeTail(d0, { ...tail([held], 2), active: false })
if (d4 === "refetch") throw new Error()
assert.equal(d4.active, false)

console.log("liveTail: ok")
