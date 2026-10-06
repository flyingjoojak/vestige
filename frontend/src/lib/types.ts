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
  hidden?: boolean   // 접힘(#128) - 제자리에 한 줄로만 표시되고 검색·지도에선 빠짐
  queued?: boolean   // 작업 중 끼어들어 친 질문(#246). 재색인 전 옛 턴은 undefined
  live?: boolean     // 아직 색인 전(#249) - 로그에서 바로 읽음. 검색·지도에 없고 접기·폴더 담기 불가
}

export type SessionSource = "claude-code" | "codex"

export interface SessionDetail {
  session: string
  title?: string | null          // 사용자가 지은 제목(없으면 null)
  project: string
  count: number
  turns: SessionTurn[]
  source?: SessionSource       // 재개 명령이 달라짐
  resume_cmd?: string          // 출처별 재개 커맨드(예: "codex resume <id>")
  source_file_exists?: boolean // 원문 로그가 남아있는지(없으면 재개 불가)
  can_restore?: boolean        // 원문은 없지만 보존된 원본이 있어 복구 가능(#163 P1)
  subagent?: boolean           // 배경(서브에이전트) 대화 — 직접 재개 불가
  parent?: string | null       // 파생된 부모 세션 id(있으면 역링크)
  db_count?: number            // 색인된 턴 수(#249) - 바뀌면 전체를 다시 받는다
  active?: boolean             // 원문이 최근 바뀜 → 꼬리를 주기적으로 다시 읽는다
  live_skipped?: number        // 꼬리가 너무 커서 안 읽은 바이트 수(0 = 다 읽음)
}

// /api/session/tail - 활동 중인 세션의 색인 전 꼬리만(#249)
export interface SessionTail {
  turns: SessionTurn[]
  db_count: number
  active: boolean
  live_skipped: number
}

export interface SessionRow {
  session: string
  count: number
  started: string
  ended: string
  headline: string
  custom_title?: string | null   // 사용자가 지은 제목(있으면 headline 이 이 값)
  hidden_count?: number        // 접힌 턴 수(count 와 같으면 세션 전체가 접힌 상태, #128)
  source?: SessionSource
  subagent?: boolean           // 배경(서브에이전트) 대화 여부
  parent?: string | null       // 파생된 부모 세션 id
  unindexed?: boolean          // 한 번도 색인 안 된 세션(로그에서 바로 읽음) - 접기·폴더 담기 불가
}

export interface Stats {
  turns: number
  sessions: number
  vectors: number
  enriched: number
}

// 접힌 턴 모아보기(#128) - 검색에서 접으면 어느 세션이었는지 잊기 쉬워 한곳에서 다시 찾는다.
// 접힘 화면(#128 개편) - 접힌 것을 세션별로 묶어 받는다(500턴 세션을 접어도 한 줄).
export interface FoldedSession {
  session_id: string
  headline: string
  total: number              // 그 세션의 전체 대화 수
  folded: number             // 그중 접힌 수(sessions 에선 total 과 같다)
  started: string
  ended: string
  last_hidden: number        // 마지막으로 접은 시각(최근 접은 순 정렬)
  last_turn_id: string | null
}
export interface FoldedTurn { turn_id: string; headline: string; timestamp: string }
export interface FoldedChats extends FoldedSession { turns: FoldedTurn[] }   // 일부만 접힌 세션
export interface FoldedGroups {
  sessions: FoldedSession[]  // 통째로 접은 세션
  chats: FoldedChats[]       // 일부만 접힌 세션의 채팅(세션별 묶음)
  count: number              // 접은 세션 수 + 접힌 채팅 수(좌측 배지)
}

// 폴더(#201) - 사용자가 직접 만드는 수동 군집. 중첩 허용(parent_id), 담는 단위는 턴·세션.
export interface Folder {
  id: number
  name: string
  parent_id: number | null
  created_at: number
  items: number          // 이 폴더에 '직접' 담긴 항목 수(하위 폴더 제외)
}

export interface FolderItem {
  kind: "turn" | "session"
  ref: string            // turn id 또는 session id
  added_at: number
  session_id: string | null
  headline: string       // 별칭이 있으면 별칭, 없으면 원본 제목
  original_headline?: string   // 원본 제목(별칭을 지웠을 때 돌아갈 이름)
  alias?: string | null  // 이 폴더에서만 쓰는 이름
  position?: number | null
  hidden?: boolean       // 접힘(#128) - 폴더에서도 접기/펼치기
  timestamp: string | null
  count?: number         // kind=session 일 때 그 세션의 현재 턴 수
}

export interface FolderDetail {
  folder: Folder
  path: { id: number; name: string }[]   // 최상위 → 부모 순(빵부스러기)
  children: Folder[]
  items: FolderItem[]
  turn_count: number     // 하위 폴더까지 포함한 검색 범위 턴 수
}

