import type { HiddenItem, SearchResult, SessionDetail, SessionRow, SessionSource, Stats } from "./types"

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) return failure(r)
  return r.json() as Promise<T>
}

// 실패 응답을 code(errors.* 번역키)와 params(detail 등)를 실은 Error로 던진다.
// 표시부(errText)가 code를 t()로 번역하고, code가 없으면 message를 그대로 쓴다.
export type ApiError = Error & { code?: string; params?: Record<string, unknown> }
async function failure(r: Response): Promise<never> {
  const body = await r.json().catch(() => null)
  const detail = body?.detail
  const detailObj = detail && typeof detail === "object" ? detail : null
  const code: string | undefined = detailObj?.code ?? body?.code
  const rawMsg = detailObj?.msg ?? (typeof detail === "string" ? detail : undefined) ?? body?.error
  const e = new Error(typeof rawMsg === "string" ? rawMsg : `HTTP ${r.status}`) as ApiError
  if (code) e.code = code
  const d = detailObj?.detail ?? body?.detail_text
  if (d != null) e.params = { detail: d }
  throw e
}

export type SearchMode = "hybrid" | "semantic" | "keyword"
export interface SearchParams {
  q: string
  k?: number
  mode?: SearchMode
  since?: string
  until?: string
  session?: string
  sources?: string[]   // 비거나 전체면 생략(=모든 출처). 부분집합일 때만 전달
}

export function search(p: SearchParams): Promise<SearchResult> {
  const usp = new URLSearchParams({ q: p.q, k: String(p.k ?? 8) })
  if (p.mode && p.mode !== "hybrid") usp.set("mode", p.mode)
  if (p.since) usp.set("since", p.since)
  if (p.until) usp.set("until", p.until)
  if (p.session) usp.set("session", p.session)
  if (p.sources && p.sources.length) usp.set("sources", p.sources.join(","))
  return getJSON<SearchResult>(`/api/search?${usp}`)
}

export interface SourceOption { source: string; count: number }
export const getSources = () =>
  getJSON<{ sources: SourceOption[] }>(`/api/sources`)

export const getSession = (id: string) =>
  getJSON<SessionDetail>(`/api/session?id=${encodeURIComponent(id)}`)

export const listSessions = () =>
  getJSON<{ sessions: SessionRow[] }>(`/api/sessions`)

// 이 PC에서 새 터미널로 `claude --resume <id>` 실행(로컬 전용).
// 활성 가드: force=false에서 세션이 최근 수정됐으면 실행하지 않고 {ok:false, active, warning} 반환.
export interface ResumeResult {
  ok: boolean
  cwd?: string | null
  active?: boolean
  seconds_since?: number
  warning?: string
  missing?: boolean            // 원문 로그가 없어 재개 불가
  source?: SessionSource
  code?: string                // 백엔드 에러/경고 코드(errText로 번역)
}
export async function resumeSession(id: string, force = false): Promise<ResumeResult> {
  const r = await fetch(`/api/resume?session=${encodeURIComponent(id)}&force=${force}`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}

// 원문 로그가 사라진(정리됨) 세션을 보존해둔 원본으로 복구(#163 P1). 성공하면 그 뒤 resumeSession 가능.
export interface RestoreResult {
  ok: boolean
  path?: string
  already_exists?: boolean
  missing?: boolean            // 보존된 원본도 없음(이 기능 이전에 유실됨)
  subagent?: boolean
  warning?: string
  code?: string
}
export async function restoreSession(id: string): Promise<RestoreResult> {
  const r = await fetch(`/api/session/restore?session=${encodeURIComponent(id)}`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}

// 숨김(#128) - 비파괴. turn_id 또는 session_id 중 하나로 대상 지정(세션이면 그 전 턴을 숨김).
async function postHide(url: string, target: { turnId?: string; sessionId?: string }): Promise<{ ok: boolean }> {
  const body = target.turnId ? { turn_id: target.turnId } : { session_id: target.sessionId }
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })
  if (!r.ok) return failure(r)
  return r.json()
}
export const hideTurn = (turnId: string) => postHide("/api/hide", { turnId })
export const hideSession = (sessionId: string) => postHide("/api/hide", { sessionId })
export const unhideTurn = (turnId: string) => postHide("/api/unhide", { turnId })
export const unhideSession = (sessionId: string) => postHide("/api/unhide", { sessionId })
// limit=0 이면 개수만 받는다(좌측 메뉴 배지용 가벼운 호출).
export const listHidden = (limit = 200) =>
  getJSON<{ hidden: HiddenItem[]; count: number }>(`/api/hidden?limit=${limit}`)

// 세션 동기화 감시(Syncthing 충돌 해소) 상태·토글.
export interface SyncStatus {
  running: boolean
  interval: number
  resolved_total: number
  last_error: string | null
  projects_dir: string
}
export const getSyncStatus = () => getJSON<SyncStatus>(`/api/sync/status`)

// 임베디드 Syncthing(앱 내장 P2P) — 기기 연결/페어링.
export interface SyncthingSync {
  state: string          // idle(최신)/scanning/syncing/error 등
  completion: number     // 0~100, 로컬이 글로벌 대비 받은 비율(수신)
  need_items: number     // 아직 받아야 할 파일·폴더 수
  need_bytes: number
  global_bytes: number
  peers_connected?: number       // 지금 연결된 상대 기기 수
  remote_complete?: number | null // 연결된 상대들이 내 폴더를 받은 최소 %(전송). null=연결 상대 없음
}
export interface SyncthingStatus {
  running: boolean
  starting: boolean
  phase: string
  my_id: string | null
  last_error: string | null
  devices?: { id: string; name: string; connected: boolean }[]
  folders?: { id: string; path: string; shared_with: string[] }[]
  sync?: SyncthingSync | null   // 공유 폴더 동기 진행(폴더 미설정 시 null)
}
export const getSyncthingStatus = () => getJSON<SyncthingStatus>(`/api/syncthing/status`)
export async function syncthingStart(): Promise<{ ok: boolean; phase?: string }> {
  const r = await fetch(`/api/syncthing/start`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}
export async function syncthingStop(): Promise<{ ok: boolean }> {
  const r = await fetch(`/api/syncthing/stop`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}
export async function syncthingPair(deviceId: string, name = ""): Promise<{ ok: boolean; error?: string; code?: string; detail?: string }> {
  const r = await fetch(`/api/syncthing/pair`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: deviceId, name }),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

// 연결된 기기 해제(리셋 등으로 유령이 된 페어링 제거).
export async function syncthingUnpair(deviceId: string): Promise<{ ok: boolean; error?: string; code?: string; detail?: string }> {
  const r = await fetch(`/api/syncthing/unpair`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: deviceId }),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

// 자동 색인(프리즈 exe) 상태.
export interface IndexPending { new_sessions: number; updated_sessions: number; files: number }
export interface IndexStatus {
  enabled: boolean
  running: boolean
  phase: string
  indexed_total: number
  done_files: number
  total_files: number
  done_chunks: number    // 자가복구(backfill) 청크 진행
  total_chunks: number
  last_error: string | null
  errors?: string[]      // 이번 회차 항목별(파일) 실패 — 조용한 스턱 방지
  pending?: IndexPending   // 새 바이트가 있는 로그 파일(=대화) 집계
  external?: boolean       // 다른 프로세스(OS 스케줄러의 vestige index)가 색인 중
}
export const getIndexStatus = () => getJSON<IndexStatus>(`/api/index/status`)

export interface SkipSdkStats { sessions: number; turns: number; enabled: boolean }
// 자동화(sdk) 제외 대상 규모. 원문 스캔이라 백엔드가 60s 캐시 → 설정 화면 마운트 시 1회 조회(폴링 아님).
export const getSkipSdkStats = () => getJSON<SkipSdkStats>(`/api/skip-sdk-stats`)

// 수동 증분 색인(새 대화만).
export async function runIndex(): Promise<{ ok: boolean; started?: boolean; busy?: boolean }> {
  const r = await fetch(`/api/index/run`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}

// 수동 정제(요약·태그). all=true면 이미 된 것도 다시.
export interface EnrichStatus {
  running: boolean
  phase: string
  done_sessions: number
  total_sessions: number
  enriched: number
  last_error: string | null
  errors?: string[]        // 이번 회차 세션별 정제 실패
  pending_turns?: number   // 아직 요약·태그 없는 턴 수(summary IS NULL)
}
export const getEnrichStatus = () => getJSON<EnrichStatus>(`/api/enrich/status`)
export async function runEnrich(all = false): Promise<{ ok: boolean; started?: boolean; backend?: string; error?: string; code?: string; detail?: string }> {
  const r = await fetch(`/api/enrich`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ all }),
  })
  if (!r.ok) return failure(r)
  return r.json()
}
export async function toggleSync(enabled: boolean, interval?: number): Promise<SyncStatus> {
  const r = await fetch(`/api/sync/toggle`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, interval }),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

export const getStats = () => getJSON<Stats>(`/api/stats`)

// 기기 메모리 + 모델/벡터 불일치(배너용).
export interface SystemInfo {
  ram_total_mb: number | null
  ram_avail_mb: number | null
  model_mismatch: { stored: string; current: string } | null
  drift_sources?: string[]   // 로그 형식이 바뀌어 못 읽는 것으로 의심되는 소스
}
export const getSystem = () => getJSON<SystemInfo>(`/api/system`)

// 기기 간 아카이브 병합(다른 기기가 보존한 삭제-원본 세션까지 가져오기). 벡터는 이후 색인이 채움.
export async function archiveSync(): Promise<{ ok: boolean; imported: number; exported: number }> {
  const r = await fetch(`/api/archive/sync`, { method: "POST" })
  if (!r.ok) return failure(r)
  return r.json()
}

// 앱(백엔드) 종료 — windowed exe는 창/트레이가 없어 이 버튼으로 끈다.
export async function quitApp(): Promise<{ ok: boolean }> {
  const r = await fetch(`/api/quit`, { method: "POST" })
  return r.json()
}

// 의미 지도(3D). 점=임베딩 청크, 군집=주제.
export interface GraphPoint3D { x: number; y: number; z: number; c: number; s: string; h: string; t: string }
export interface GraphCluster3D { id: number; label: string; x: number; y: number; z: number; n: number }
// paths: 같은 세션 점들을 시간순으로 잇는 인덱스 경로(성좌 선)
export interface Graph3DData { points: GraphPoint3D[]; clusters: GraphCluster3D[]; paths?: number[][]; method: string | null }
// refresh=true면 캐시 무시하고 군집·라벨 재계산(정제 후 등).
export const getGraph3D = (refresh = false) =>
  getJSON<Graph3DData>(`/api/graph3d${refresh ? "?refresh=true" : ""}`)

export interface Config {
  enrich_backend: string
  models: Record<string, string>
  ollama_url: string
  enrich_time: string
  index_interval: number
  index_mode: string        // off|interval|realtime|scheduled
  index_time: string        // scheduled 모드 색인 시각 HH:MM
  embed_model: string
  raw_archive_max_mb: string   // 원본 로그 보존소 상한(MB). 빈값=무제한(#163)
  raw_archive_bytes: number    // 현재 보존소 용량(바이트)
  raw_archive_dir: string      // 보존소 경로(직접 지정 가능)
  raw_archive_exists: boolean
  keys: Record<string, boolean>
  config_path: string
  projects_dir: string
  projects_exists: boolean
  codex_dir: string
  codex_exists: boolean
  jsonl_count: number
  sources?: SourceInfo[]
  claude_bin?: string        // 사용자가 지정한 claude CLI 경로 override(빈값=자동 탐색)
  claude_resolved?: string   // 실제 해석된 claude 경로
  claude_found?: boolean     // claude CLI 를 찾았는지
  skip_sdk?: boolean         // 자동화(SDK/claude -p) 세션 색인 제외 여부
}
export interface SourceInfo {
  name: string
  root: string | null
  exists: boolean
  active: boolean
  disabled?: boolean   // UI 토글로 껐는지(색인만 중단, 기존 데이터 유지)
  count: number
}

export const getConfig = () => getJSON<Config>(`/api/config`)

// 색인 소스 켜기/끄기(비파괴). 끄면 다음 색인부터 그 소스를 건너뜀, 기존 데이터는 유지.
export async function toggleSource(source: string, enabled: boolean): Promise<{ ok: boolean; disabled: string[] }> {
  const r = await fetch(`/api/sources/toggle`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, enabled }),
  })
  return r.json()
}

export async function putConfig(updates: Record<string, string>): Promise<{ ok: boolean; rescheduled?: boolean; code?: string; invalid?: string[] }> {
  const r = await fetch(`/api/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

export async function verifyEnrich(p: {
  backend: string; model?: string; api_key?: string; ollama_url?: string
}): Promise<{ ok: boolean; message: string }> {
  const r = await fetch(`/api/verify-enrich`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

export interface EmbedModel {
  model: string
  dim: number
  size_gb: number
  ram_gb: number          // 임베딩 중 실사용 피크 RAM(실측)
  cps: number             // 청크/초 처리량(실측)
  est_reindex_min: number | null
  note: string
  tags: string[]          // 특징 라벨(예: "램 부하 적음", "품질 최상")
  current: boolean
  recommended?: boolean   // 기기 RAM 기반 권장 모델(미리 강조)
}
export interface ReindexState {
  running: boolean; done: number; msg: string
  done_files: number; total_files: number
  done_chunks: number; total_chunks: number   // 청크 단위 진행(거대 파일에도 부드럽게)
}

export const getEmbedModels = () =>
  getJSON<{ models: EmbedModel[]; current: string; recommended: string; ram_total_gb: number | null; reindex: ReindexState }>(`/api/embed-models`)

// 첫 실행 온보딩(임베딩 모델 선택)
export const getOnboarding = () => getJSON<{ needed: boolean }>(`/api/onboarding`)
export async function chooseModel(model: string): Promise<{ ok: boolean; model?: string; error?: string; code?: string }> {
  const r = await fetch(`/api/onboarding/choose`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  })
  if (!r.ok) return failure(r)
  return r.json()
}

// MCP 클라이언트 등록
export interface McpTarget {
  id: string
  label: string
  method: string
  installed: boolean
  registered: boolean
  path: string
  snippet: string
}

export const getMcp = () => getJSON<{ targets: McpTarget[]; command: string }>(`/api/mcp`)

export async function mcpRegister(target: string): Promise<{ ok: boolean; restart?: boolean; error?: string }> {
  const r = await fetch(`/api/mcp/register`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }),
  })
  return r.json()
}

export async function mcpUnregister(target: string): Promise<{ ok: boolean; error?: string }> {
  const r = await fetch(`/api/mcp/unregister`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }),
  })
  return r.json()
}

// model 생략 시 현재 모델로 재색인(모델 교체 없이 인덱스만 다시 빌드).
// fast=true: 재파싱 없이 병렬(멀티프로세싱)로 대량 임베딩 — RAM 여유 있는 고성능 기기용.
export async function reindex(
  model?: string, opts: { fast?: boolean; parallel?: number } = {},
): Promise<{ ok: boolean; started?: boolean; error?: string; code?: string }> {
  const body: Record<string, unknown> = {}
  if (model) body.model = model
  if (opts.fast) { body.fast = true; if (opts.parallel) body.parallel = opts.parallel }
  const r = await fetch(`/api/reindex`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!r.ok) return failure(r)   // 실제 HTTP 에러는 failure()가 detail.code/msg를 ApiError로 추출
  return r.json()
}

// 원클릭 형식 신고: 대화 내용 없는 리댁트 스키마 지문(소스 로그 포맷 변경 신고용).
export type SchemaSource = "codex" | "claude-code"
export interface SchemaReport {
  source: string
  vestige_version: string
  repo: string
  error?: string
  root?: string | null
  root_exists?: boolean
  files?: number
  files_scanned?: number
  files_with_turns?: number
  unreadable_files?: number
  cli_versions?: string[]
  drift_suspected?: boolean
  suspect_files?: number
  type_counts?: Record<string, number>
  payload_type_counts?: Record<string, number>
  item_type_counts?: Record<string, number>
  redacted_samples?: unknown[]
}
export const getSchemaReport = (source: SchemaSource) =>
  getJSON<SchemaReport>(`/api/report/schema?source=${encodeURIComponent(source)}`)
