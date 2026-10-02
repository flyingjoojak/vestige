// 저장 타임스탬프는 UTC(...Z) → 보는 사람의 로컬(한국이면 KST)로 표시.
export function fmtTime(ts: string): string {
  const d = new Date(ts)
  if (isNaN(d.getTime())) return (ts || "").slice(0, 16).replace("T", " ")
  const p = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ── 마크다운 → 안전한 HTML ────────────────────────────────────────────
// 원칙: 텍스트는 항상 먼저 escape → 그 위에 알려진 서식만 태그로 치환(XSS 방지).
// 지원: 코드펜스/인라인코드, 표(GFM), 링크(스킴검증), 볼드/이탤릭/취소선, 헤더, 리스트.
function esc(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string))
}

function safeUrl(u: string): string | null {
  try {
    const p = new URL(u, "http://_")
    if (["http:", "https:", "mailto:"].includes(p.protocol)) return u
  } catch { /* 파싱 실패 */ }
  return null
}

// 인라인 서식. 인라인 코드는 먼저 분리해 그 안엔 다른 치환이 안 닿게 한다.
function inline(raw: string): string {
  return raw.split(/(`[^`]+`)/g).map((seg) => {
    if (seg.length > 1 && seg.startsWith("`") && seg.endsWith("`")) {
      return `<code class="cm-inline">${esc(seg.slice(1, -1))}</code>`
    }
    let s = esc(seg)
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) => {
      const u = safeUrl(url)
      return u ? `<a class="cm-a" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${txt}</a>` : m
    })
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    s = s.replace(/~~([^~]+)~~/g, '<span class="cm-del">$1</span>')
    return s
  }).join("")
}

const _isTableSep = (s: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(s)
const _cells = (r: string) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim())

export function mdToHtml(src: string): string {
  if (!src) return ""
  const lines = src.replace(/\r\n?/g, "\n").split("\n")
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]

    // 코드펜스 ```
    if (/^\s*```/.test(line)) {
      const buf: string[] = []; i++
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++ }
      i++   // 닫는 펜스 건너뜀
      out.push(`<pre class="cm-code">${esc(buf.join("\n"))}</pre>`)
      continue
    }

    // 표(GFM): 현재 줄에 |가 있고 다음 줄이 |---| 구분선.
    if (line.includes("|") && i + 1 < lines.length && _isTableSep(lines[i + 1])) {
      const header = _cells(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") { rows.push(_cells(lines[i])); i++ }
      const thead = `<thead><tr>${header.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead>`
      const tbody = `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`
      out.push(`<div class="cm-tblwrap"><table class="cm-tbl">${thead}${tbody}</table></div>`)
      continue
    }

    // 헤더 #
    const h = line.match(/^\s*#{1,6}\s+(.+)$/)
    if (h) { out.push(`<div class="cm-h">${inline(h[1])}</div>`); i++; continue }

    // 리스트(연속 항목 묶기). 번호 목록은 <ol> 에 적힌 번호 그대로(value) - 예전엔 점 목록으로 그려
    // "3. 커밋해줘" 의 3 이 사라졌다.
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(inline(lines[i].replace(/^\s*[-*+]\s+/, ""))); i++
      }
      out.push(`<ul class="cm-ul">${items.map((it) => `<li>${it}</li>`).join("")}</ul>`)
      continue
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = []
      let m: RegExpMatchArray | null
      while (i < lines.length && (m = lines[i].match(/^\s*(\d+)\.\s+(.*)$/))) {
        items.push(`<li value="${Number(m[1])}">${inline(m[2])}</li>`); i++
      }
      out.push(`<ol class="cm-ol">${items.join("")}</ol>`)
      continue
    }

    if (line.trim() === "") { out.push(""); i++; continue }   // 빈 줄
    out.push(inline(line)); i++                                // 일반 줄
  }

  // 블록요소(pre/table/ul/div) 뒤엔 <br>를 안 붙이고, 일반 줄만 줄바꿈.
  let html = ""
  for (const seg of out) {
    if (seg === "") continue
    html += /^<(pre|div|ul|ol|table)/.test(seg) ? seg : seg + "<br>"
  }
  return html.replace(/(<br>)+$/, "")
}
