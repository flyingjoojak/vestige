// 단독 검증: `node scripts/mdList.check.ts` (frontend/ 에서)
import assert from "node:assert/strict"
import { mdToHtml } from "../src/lib/format.ts"

// 적힌 번호가 그대로 남는다(3 부터 시작, 문장 속 "4." 는 목록 아님)
const h = mdToHtml("3. 커밋해줘. 4. 하나로 맞춰야겠네.")
assert.equal(h, '<ol class="cm-ol"><li value="3">커밋해줘. 4. 하나로 맞춰야겠네.</li></ol>')
// 번호와 점 목록은 따로 묶인다
assert.match(mdToHtml("1. a\n2. b\n- c"), /^<ol class="cm-ol"><li value="1">a<\/li><li value="2">b<\/li><\/ol><ul class="cm-ul"><li>c<\/li><\/ul>$/)
console.log("mdList ok")
