// 단독 검증: `node scripts/apiError.check.ts` (frontend/ 에서) (Node 22+ 는 TS 를 그대로 실행한다)
import assert from "node:assert/strict"
import { failure, type ApiError } from "../src/lib/api.ts"

const res = (status: number, body: unknown) => new Response(JSON.stringify(body), { status })
const caught = async (r: Response): Promise<ApiError> => {
  try { await failure(r) } catch (e) { return e as ApiError }
  throw new Error("failure 가 던지지 않았다")
}

// 1. 일반 500: 친절한 문구가 원시 예외보다 먼저. 예전엔 'database is locked' 가 화면에 그대로 나갔다
const e1 = await caught(res(500, { code: "server_db_error", error: "데이터에 접근하지 못했어요.", detail: "database is locked" }))
assert.equal(e1.message, "데이터에 접근하지 못했어요.")
assert.equal(e1.code, "server_db_error")

// 2. HTTPException(detail={code,msg}): msg 를 쓴다
const e2 = await caught(res(404, { detail: { code: "session_not_found", msg: "세션을 찾을 수 없음" } }))
assert.equal(e2.message, "세션을 찾을 수 없음")
assert.equal(e2.code, "session_not_found")

// 3. FastAPI 기본 HTTPException(detail="…") 처럼 문자열밖에 없으면 그걸 쓴다
const e3 = await caught(res(422, { detail: "Field required" }))
assert.equal(e3.message, "Field required")

// 4. 본문이 JSON 이 아니면 상태 코드라도
const e4 = await caught(new Response("<html>", { status: 502 }))
assert.equal(e4.message, "HTTP 502")

console.log("apiError: ok")
