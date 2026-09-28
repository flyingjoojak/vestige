// 단독 검증: `node scripts/subagents.check.ts` (frontend/ 에서) (Node 22+ 는 TS 를 그대로 실행한다)
import assert from "node:assert/strict"
import { filterTop, kidsToShow, nestSubagents } from "../src/lib/subagents.ts"

type G = { id: string; label: string; subagent?: boolean; parent?: string | null }
const g = (id: string, label: string, parent?: string): G =>
  parent ? { id, label, subagent: true, parent } : { id, label }

const list: G[] = [
  g("p1", "Vestige 속도 개선"),
  g("s1", "로그 형식 리서치", "p1"),
  g("s2", "계약 일치 검토", "p1"),
  g("p2", "HRLM-EMS 설계"),
  g("orphan", "부모 없는 하위", "gone"),   // 부모가 색인에 없음
]
const hits = (term: string) => (x: G) => x.label.toLowerCase().includes(term)

// 1. 하위는 부모 아래로, 부모 없는 하위는 최상위에 남는다
const { top, kids } = nestSubagents(list)
assert.deepEqual(top.map((x) => x.id), ["p1", "p2", "orphan"], "고아 하위는 최상위에 남아야 한다")
assert.deepEqual(kids.get("p1")!.map((x) => x.id), ["s1", "s2"])
assert.equal(kids.has("gone"), false)

// 2. 평소엔 하위가 전부 나온다(부모 아래 접힌 채로 — 펼치기 한 번이면 된다)
assert.deepEqual(kidsToShow(kids.get("p1")!, "", hits("")).map((x) => x.id), ["s1", "s2"])

// 3. 검색 중이면 맞는 것만 — 안 그러면 접힌 하위에 도달할 수 없다
const term = "리서치"
assert.deepEqual(kidsToShow(kids.get("p1")!, term, hits(term)).map((x) => x.id), ["s1"])

// 4. 하위가 맞으면 부모가 목록에 남는다(부모 제목은 안 맞아도)
assert.deepEqual(filterTop(top, kids, term, hits(term)).map((x) => x.id), ["p1"])
// 5. 아무것도 안 맞으면 빈 목록
assert.deepEqual(filterTop(top, kids, "zzz", hits("zzz")), [])

console.log("subagents: 5개 검사 통과")
