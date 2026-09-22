# Changelog

이 파일은 사용자에게 보이는 변경을 기록합니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/),
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

릴리스 방법은 [README의 릴리스 섹션](README.md)을 참고하세요. 새 버전을 태그하면 GitHub 릴리스가
만들어지고, **그 릴리스 본문이 앱의 업데이트 배너에 그대로 표시**됩니다. 아래처럼
`<!--lang:ko-->` / `<!--lang:en-->` 마커로 나눠 두면, 배너가 사용자 언어에 맞는 섹션만 보여줍니다.

## [0.3.1] - 2026-09-22

<!--lang:ko-->

### Fixed
- **검색이 훨씬 빨라졌습니다.** 결과 한 건의 앞뒤 대화를 가져올 때 그 세션의 모든 대화를 읽고 있었습니다. 결과가 20건이면 세션 20개를 통째로 읽던 셈입니다(검색 시간의 92%). 실측 467ms → 64ms.
- **3D 지도가 즉시 뜹니다.** 지도를 열 때마다 점 구름 전체를 다시 만들어 보내고 있었습니다. 내용이 그대로면 만들어둔 것을 재사용합니다. 실측 206ms → 5ms.
- **보존해둔 원본 로그가 손상됐을 때 그 부분을 지우지 않습니다.** 깨진 자리를 고쳐보려고 파일을 잘라내던 코드가 있었는데, 그게 오히려 멀쩡한 부분까지 없앨 수 있었습니다. 이제 보존본은 덧붙이기만 하고, 읽을 수 없는 부분이 있으면 "일부만 복구했습니다"라고 알려줍니다.
- 복구가 일부만 됐을 때 그 알림이 4초 뒤 사라져 놓칠 수 있었습니다. 이제 직접 닫기 전까지 남아있습니다.

### Added
- **폴더를 드래그 없이 옮길 수 있습니다.** 키보드와 버튼으로 위/아래 이동, 상위/하위로 넣기가 됩니다(드래그가 어려운 환경을 위해).
- **검색 전·폴더 선택 전 화면이 비어있지 않습니다.** 가장 최근 대화와 최근 검색어를 먼저 보여줍니다.

### Changed
- 색인하지 않을 때 메모리 사용이 줄었습니다(지도 캐시 판정에 벡터 전체를 올리지 않게).
- 조용히 실패하던 경로 6곳이 이제 화면에 오류를 표시합니다.

<!--lang:en-->

### Fixed
- **Search is much faster.** Fetching the conversations around a result was reading every turn in that session - with 20 results, that meant reading 20 whole sessions (92% of search time). Measured 467ms → 64ms.
- **The 3D map opens instantly.** The whole point cloud was rebuilt and re-sent on every open. It is now reused when nothing changed. Measured 206ms → 5ms.
- **A damaged raw-log archive no longer loses data.** Code that tried to repair a broken spot by truncating the file could remove intact data along with it. The archive is now append-only, and unreadable parts are reported as "partially restored" instead.
- A partial-restore notice disappeared after 4 seconds and could be missed. It now stays until you dismiss it.

### Added
- **Folders can be moved without dragging** - keyboard and buttons for up/down and in/out.
- **Search and folder tabs are no longer empty before you act** - the most recent conversation and your recent searches are shown first.

### Changed
- Lower memory use when not indexing (the map cache check no longer loads the full vector matrix).
- Six silently failing paths now surface errors in the UI.

## [0.3.0] - 2026-09-17

<!--lang:ko-->

### Added
- **접기** - 지금 필요 없는 대화를 접어 검색·지도에서 빼둡니다. 지우는 게 아니라 제자리에 한 줄로 남아 언제든 다시 펼칠 수 있고, 다시 색인해도 접어둔 상태가 유지됩니다.
- **접힘 모아보기** - 접어둔 것만 따로 모아 보고, 거기서 바로 그 대화로 이동하거나 펼칠 수 있습니다.
- **폴더** - 원하는 세션·대화만 골라 담는 내 손으로 만드는 군집입니다. 하위 폴더, 드래그로 순서·위치 바꾸기, 그 폴더 범위 안에서만 검색하기를 지원합니다.
- **폴더 안에서만 쓰는 이름** - 담아둔 항목에 별칭을 붙일 수 있습니다. 원본 제목은 그대로라 다른 화면에는 영향이 없고, 같은 대화를 여러 폴더에 담아 각각 다른 이름을 붙여도 됩니다.
- **원본 로그 보존과 세션 복구** - Claude Code가 오래된 로그를 정리해도 이어서 대화할 수 있도록, 로그가 늘어난 만큼을 따로 압축 보관합니다. 원문이 사라진 세션은 복구 버튼으로 되살립니다. 보존소 경로와 용량 상한은 설정에서 정할 수 있습니다.
- **세션·군집 목록 검색창** - 세션은 제목이나 세션 ID로, 군집은 이름으로 목록을 걸러냅니다.
- **세션 제목 직접 짓기** - 자동으로 붙는 제목 대신 원하는 이름을 붙일 수 있습니다. 비워서 저장하면 원래 제목으로 돌아갑니다.
- **markdown으로 내보내기** - 원문도 보존본도 없는 예전 세션을 파일로 받아둘 수 있습니다.

### Changed
- 확인·입력 창이 앱 안에서 뜹니다(전에는 브라우저 기본 팝업이 따로 떴습니다).
- 하단 상태바가 1초 간격으로 갱신됩니다.

### Fixed
- 접기·펼치기 후 의미 지도가 다시 뜨기까지 오래 걸리고 군집 색이 전부 바뀌던 문제.
- 폴더에 담아둔 세션의 제목을 바꿔도 폴더 화면에만 옛 제목이 남던 문제.
- 검색이 실패했는데 "결과 없음"으로 보이던 문제.
- 담기 드롭다운이 잘리거나, 목록을 스크롤하면 닫히던 문제.
- 원본 보존이 실패해도 아무 표시가 없던 문제. 이제 색인 로그에 오류로 남습니다.
- 진행 중인 세션을 보존할 때 같은 내용이 중복 기록될 수 있던 문제.

<!--lang:en-->

### Added
- **Fold** - Fold away conversations you don't need right now so they drop out of search and the map. Nothing is deleted: a single line stays in place so you can unfold anytime, and the folded state survives re-indexing.
- **Folded view** - See everything you've folded in one place, and jump to the conversation or unfold it from there.
- **Folders** - Hand-made clusters: pick the sessions and conversations you want and collect them. Supports subfolders, drag to reorder or re-nest, and search scoped to a folder.
- **Folder-only names** - Give a collected item an alias. The original title is untouched, so other screens are unaffected, and the same conversation can carry a different name in each folder.
- **Raw log preservation and session restore** - So you can keep talking even after Claude Code prunes old logs, Vestige archives each log's new bytes separately. Sessions whose originals are gone can be restored with one click. The archive path and size cap are configurable.
- **Session and cluster list search** - Filter sessions by title or session ID, clusters by name.
- **Custom session titles** - Name a session yourself instead of the auto-generated title. Save it empty to restore the original.
- **Markdown export** - Save older sessions to a file when neither the original nor an archived copy exists.

### Changed
- Confirmation and input dialogs now appear inside the app (previously separate browser popups).
- The status bar refreshes every second.

### Fixed
- The semantic map took a long time to reappear after folding or unfolding, and cluster colors all changed.
- Renaming a session left the old title showing in the folder view.
- Failed searches were shown as "no results".
- The add-to-folder dropdown was clipped, and closed when you scrolled its list.
- Failures to preserve raw logs were silent. They now surface as errors in the indexing log.
- Preserving an in-progress session could record the same content twice.

## [0.2.0] - 2026-09-10

<!--lang:ko-->

### Changed
- **프로젝트 이름 변경: Engram → Vestige.** 같은 이름의 다른 프로젝트와 겹쳐 이름을 바꿨습니다. 명령은 `vestige`(짧은 별칭 `vst`), MCP 서버는 `vestige-mcp`, 데이터 폴더는 `~/vestige`, 설정 키는 `VESTIGE_*`.
- **기존 사용자는 그대로 이어집니다(무손실 하위호환).** 구 `~/engram` 데이터 폴더는 첫 실행 때 `~/vestige`로 자동 이전되고, 구 `ENGRAM_*` 환경변수·설정과 예전에 동기화해 둔 `.engram-archive` 스냅샷도 계속 인식합니다. `mem` 명령도 당분간 유지됩니다.

<!--lang:en-->

### Changed
- **Renamed the project: Engram → Vestige.** Changed to avoid a clash with another project of the same name. The command is `vestige` (short alias `vst`), the MCP server is `vestige-mcp`, the data folder is `~/vestige`, and config keys are `VESTIGE_*`.
- **Existing users carry over losslessly.** An old `~/engram` data folder is auto-migrated to `~/vestige` on first run, and old `ENGRAM_*` env vars/config plus previously synced `.engram-archive` snapshots are still recognized. The `mem` command still works for now.

## [0.1.0] - 2026-09-09

<!--lang:ko-->

### Added
- **Claude Code와 Codex 로그를 함께 색인** - 두 도구가 기기에 남기는 대화 로그를 자동으로 읽어 소스별로 분류.
- **첫 실행 언어 선택** - 온보딩에서 언어(한국어/영어)를 먼저 고르면 이어지는 화면이 그 언어로 표시.
- **첫 색인 진행 안내** - 처음 대화를 색인하는 동안 상단 배너로 진행 상황(색인된 개수)을 보여줌.
- **자동화(SDK·claude -p) 세션 기본 제외** - 스크립트·헤드리스로 돌린 일회성 세션은 색인에서 제외(설정에서 끄면 포함). 한 번 색인하면 되돌리기 어려워 잃을 게 없는 쪽을 기본으로 함.
- **로그 폴더 경로 직접 지정** - 자동 탐색에 더해 색인 소스에서 경로를 직접 바꿀 수 있음.
- **Claude CLI 경로 설정(요약용)** - 자동 탐색 + 직접 지정(macOS에서 앱이 셸 PATH를 못 볼 때 대비).
- **백그라운드(서브에이전트) 대화 색인** - 오래 운전한 배경 에이전트와의 대화를 **별도 세션**으로 검색·조회(일회성 도구 봇은 자동 제외).
- **소스별 세션 재개** - claude → `claude --resume`, codex → `codex resume`. 원문 로그가 없으면 열 수 없음으로 처리.
- **검색 소스 필터·결과 소스 배지**, 색인 소스 켜기/끄기.
- **로그 형식 변화 자동 감지** + 원클릭 GitHub 이슈 신고(대화 내용은 안 보내고 형식 지문만).
- **자동 업데이트 알림 배너**(선택) - 새 버전과 릴리스 노트를 표시.
- **3-OS(Windows·Linux·macOS) 릴리스 빌드 CI** + 테스트 CI(GitHub Actions).
- **macOS Homebrew cask** - 서명 없이도 `brew install/upgrade --cask`로 설치·업데이트(격리 해제로 Gatekeeper 경고 없음).
- **미서명 macOS 안내형 업데이트** - 새 버전 감지 시 배너로 알리고 다운로드 페이지를 열어줌(자동 교체는 서명 필요).
- **다국어(한국어·영어)** - OS 로케일 자동 감지, 설정 → 모양에서 전환·저장. 모든 UI 문자열을 번역 리소스로 전환.
- **자동 색인 모드 선택** - 끄기 / 주기(기본) / 실시간 / 특정 시각.

### Changed
- Electron 32 → 43(최신 Chromium으로 창 리사이즈 매끄러움 개선).
- 리사이즈 성능 - 하단바 뷰포트 고정, 3D 지도 리사이즈 디바운스, 긴 목록/채팅에 `content-visibility`.
- 설정 '일반' 탭 정리 - 로그 폴더를 접이식 한 줄로, 개발 용어를 평이한 문구로.
- 임베딩 모델을 int8 e5-large(기본)·MiniLM(저사양) 2종으로 정리, RAM 표기를 실측값으로 정정(int8 약 2.0GB).
- 앱 아이콘을 1024px 고해상도로 교체.

### Fixed
- 마우스 뒤로가기(4번 버튼)로 시작 화면("엔진 불러오는 중")에 갇히던 문제.
- Syncthing 고아 프로세스가 폴더 락을 쥐어 기기 동기화가 안 켜지던 문제.
- MCP 의존성 `mcp` 2.x 비호환(FastMCP 분리) → `<2` 고정.
- 백그라운드(스케줄러) 색인이 도는데도 설정에서 색인 상태가 안 보이고 "지금 색인/전체 재색인" 버튼이 눌리던 문제(배너와 상태 불일치). 색인 상태를 프로세스 간 공유하고, 크로스-프로세스 락으로 동시 색인을 막음.

### Security
- 서빙 화면에 Content-Security-Policy 및 보안 헤더 추가.
- 설정 저장 시 키까지 검증하여 환경변수 주입 차단.
- Syncthing 바이너리를 공식 SHA-256으로 무결성 검증.
- 자동업데이트 산출물 파일명을 고정하여 업데이트 404 방지.

<!--lang:en-->

### Added
- **Indexes both Claude Code and Codex logs** - automatically reads the conversation logs both tools leave on your machine, tagged by source.
- **First-run language pick** - onboarding asks for your language (Korean/English) first, then shows the rest in that language.
- **First-index progress banner** - while your conversations are indexed for the first time, a top banner shows progress (how many indexed).
- **Automation (SDK · claude -p) sessions excluded by default** - one-shot script/headless sessions are kept out of the index (turn it off in settings to include them). Once indexed, entries are hard to remove today, so the default errs toward not keeping throwaway runs.
- **Editable log-folder paths** - set a source's log folder directly, on top of auto-detection.
- **Claude CLI path setting (for summaries)** - auto-detect plus manual override (for macOS where the app can't see your shell PATH).
- **Background (sub-agent) conversation indexing** - long-running background-agent chats become their own **searchable sessions** (one-off tool bots are excluded automatically).
- **Source-aware session resume** - claude → `claude --resume`, codex → `codex resume`; sessions whose source log is gone are marked as non-openable.
- **Search source filter & result source badges**, plus per-source indexing on/off.
- **Automatic log-format drift detection** + one-click GitHub issue report (sends only a masked format fingerprint, never conversation content).
- **Update notification banner** (optional) - shows the new version and release notes.
- **3-OS (Windows/Linux/macOS) release-build CI** + test CI (GitHub Actions).
- **macOS Homebrew cask** - install/update via `brew install/upgrade --cask` even unsigned (quarantine removed, no Gatekeeper warning).
- **Assisted update for unsigned macOS** - on a new version, the banner opens the download page (auto-replace needs signing).
- **Localization (Korean & English)** - auto-detects OS locale, switch/persist in Settings → Appearance. All UI strings moved to translation resources.
- **Auto-index mode** - off / interval (default) / realtime / scheduled.

### Changed
- Electron 32 → 43 (smoother window resize on the latest Chromium).
- Resize performance - pinned status bar, debounced 3D-map resize, `content-visibility` on long lists/chats.
- Tidied Settings "General" - log folders collapsed to one line, developer jargon rewritten in plain words.
- Consolidated embedding models to int8 e5-large (default) & MiniLM (low-spec); corrected RAM figures to measured values (int8 ≈ 2.0GB).
- Replaced the app icon with a 1024px high-resolution version.

### Fixed
- Getting stuck on the loading screen ("loading engine") after a mouse back-button (button 4) press.
- Device sync failing to start because an orphaned Syncthing process held the folder lock.
- MCP dependency `mcp` 2.x incompatibility (FastMCP split out) → pinned to `<2`.
- Settings not showing indexing status (and leaving the "Index now / Full re-index" buttons clickable) while a background scheduler index was running — the banner and settings disagreed. Index status is now shared across processes, and a cross-process lock prevents concurrent indexing.

### Security
- Added Content-Security-Policy and hardening headers to served pages.
- Validate keys on settings save to block environment-variable injection.
- Verify the Syncthing binary against the official SHA-256.
- Fixed auto-update artifact filenames to prevent update 404s.

[0.1.0]: https://github.com/flyingjoojak/vestige/releases/tag/v0.1.0
