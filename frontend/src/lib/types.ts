export interface Hit {
  id: string
  session: string
  session_full: string
  project: string
  timestamp: string
  question: string
  answer: string
  actions: string[]
  summary: string | null
  tags: string[]
  cosine: number | null
  sources: string[]
  source?: SessionSource   // 출처 도구(claude-code/codex)
  thread: { question: string; answer: string }[]
}

export interface SearchResult {
  query: string
  count: number
  hits: Hit[]
  error?: string
  code?: string   // 검색이 200으로 실패를 알릴 때(예: no_embed_model) — errText로 표시
}

export interface SessionTurn {
  id: string
  timestamp: string
  question: string
  answer: string
  actions: string[]
  summary: string | null
  tags: string[]
}

export type SessionSource = "claude-code" | "codex"

export interface SessionDetail {
  session: string
  project: string
  count: number
  turns: SessionTurn[]
  source?: SessionSource       // 재개 명령이 달라짐
  resume_cmd?: string          // 출처별 재개 커맨드(예: "codex resume <id>")
  source_file_exists?: boolean // 원문 로그가 남아있는지(없으면 재개 불가)
  can_restore?: boolean        // 원문은 없지만 보존된 원본이 있어 복구 가능(#163 P1)
  subagent?: boolean           // 배경(서브에이전트) 대화 — 직접 재개 불가
  parent?: string | null       // 파생된 부모 세션 id(있으면 역링크)
}

export interface SessionRow {
  session: string
  count: number
  started: string
  ended: string
  headline: string
  source?: SessionSource
  subagent?: boolean           // 배경(서브에이전트) 대화 여부
  parent?: string | null       // 파생된 부모 세션 id
}

export interface Stats {
  turns: number
  sessions: number
  vectors: number
  enriched: number
}
