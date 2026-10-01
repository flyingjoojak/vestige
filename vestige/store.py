"""아카이브(SQLite) = 진실원본. 턴 원문·행동·정제본·청크메타·커서·메타.

멱등: 턴/청크는 id 기준 INSERT OR REPLACE. 커서는 저장 성공 후에만 전진.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .config import DB_PATH
from .models import Action, Turn

logger = logging.getLogger(__name__)

# FTS MATCH 용 토큰: ASCII 영숫자 런 + 한글 런. '_'는 제외(FTS unicode61이 _로 분리하므로).
_FTS_TOKEN = re.compile(r"[A-Za-z0-9]+|[가-힣]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  id TEXT PRIMARY KEY, session_id TEXT, uuid TEXT, parent_uuid TEXT,
  timestamp TEXT, project TEXT, question TEXT, answer TEXT, actions TEXT,
  summary TEXT, tags TEXT, source TEXT, source_file TEXT, queued INTEGER,
  parser_version INTEGER
);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_key TEXT PRIMARY KEY, turn_id TEXT, idx INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS cursors(
  file_path TEXT PRIMARY KEY, offset INTEGER, size INTEGER, mtime REAL, updated_at REAL,
  hold_offset INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS raw_cursors(
  file_path TEXT PRIMARY KEY, mirrored_offset INTEGER, session_id TEXT, source TEXT, updated_at REAL,
  mirror_bytes INTEGER
);
CREATE TABLE IF NOT EXISTS unfolded(
      turn_id TEXT PRIMARY KEY, at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS hidden_turns(
  turn_id TEXT PRIMARY KEY, hidden_at REAL
);
CREATE TABLE IF NOT EXISTS session_titles(
  session_id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS folders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  parent_id INTEGER REFERENCES folders(id), created_at REAL,
  position REAL,            -- 같은 부모 안에서의 순서(작을수록 위)
  uid TEXT,                 -- 기기를 넘나드는 식별자(#233) — id 는 기기별이라 충돌한다
  updated_at REAL           -- 병합 판정용('늦게 바꾼 쪽이 이김')
);
CREATE TABLE IF NOT EXISTS folder_items(
  folder_id INTEGER NOT NULL REFERENCES folders(id),
  kind TEXT NOT NULL,      -- 'turn' | 'session'
  ref TEXT NOT NULL,       -- turn id 또는 session id
  added_at REAL,
  alias TEXT,              -- 이 폴더에서만 쓰는 표시 이름(원본 제목은 그대로)
  position REAL,           -- 폴더 안 정렬 순서(작을수록 위)
  updated_at REAL,         -- 병합 판정용(#233)
  PRIMARY KEY(folder_id, kind, ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_uid ON folders(uid);
CREATE TABLE IF NOT EXISTS folder_removed(
  uid TEXT PRIMARY KEY, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS folder_item_removed(
  folder_uid TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY(folder_uid, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp);
CREATE INDEX IF NOT EXISTS idx_turns_session_ts ON turns(session_id, timestamp, id);
CREATE INDEX IF NOT EXISTS idx_chunks_turn ON chunks(turn_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_folder_items_folder ON folder_items(folder_id);
"""

# 스키마 버전 = 아래 _MIGRATIONS 길이. 새 DB는 _SCHEMA(최신 형태)로 만든 뒤 곧장 이 번호로 스탬프하고,
# 기존 DB는 PRAGMA user_version 부터 여기까지의 마이그레이션만 순서대로 적용한다.
#
# 왜 이게 필요한가: 로컬-퍼스트 앱에서 사용자의 archive.db 는 영구 자산이다. 사용자가 퍼진 뒤엔
# "그냥 다시 만들기"가 불가능하므로, 스키마 변경은 반드시 버전이 매겨진·되돌릴 수 없는 앞으로만 가는
# 단계로 관리해야 한다. 각 단계는 (a) 멱등하게 짜고(부분 적용 후 재시도 안전), (b) 자체 트랜잭션으로 감싼다.
#
# 규칙:
#   - 마이그레이션은 오직 이 리스트 '끝에만' 추가한다(기존 항목의 순서/내용을 바꾸지 말 것).
#   - 컬럼 추가 같은 건 ADD COLUMN + IF NOT EXISTS 관용구로 멱등하게.
#   - _SCHEMA(신규 DB의 시작 형태)에 이미 반영된 변경도, 구 DB를 끌어올리기 위해 여기 단계로 남긴다.
_Migration = Callable[[sqlite3.Connection], None]


def _mig_0001_source_columns(conn: sqlite3.Connection) -> None:
    """turns 에 멀티소스용 source / source_file 컬럼 추가(구 단일소스 DB → 멀티소스)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    for col in ("source", "source_file"):
        if col not in cols:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {col} TEXT")


def _mig_0002_cursor_hold_offset(conn: sqlite3.Connection) -> None:
    """cursors 에 hold_offset 추가(idle 확정된 '열린 마지막 턴' 뒷내용 재포착용)."""
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(cursors)")}
    if "hold_offset" not in ccols:
        conn.execute("ALTER TABLE cursors ADD COLUMN hold_offset INTEGER")


def _mig_0003_raw_cursors(conn: sqlite3.Connection) -> None:
    """raw_cursors 테이블 추가(#163 P1: 원본 로그 바이트 보존 미러링 오프셋 추적)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS raw_cursors(
      file_path TEXT PRIMARY KEY, mirrored_offset INTEGER, session_id TEXT, source TEXT, updated_at REAL
    )""")


def _mig_0004_hidden_turns(conn: sqlite3.Connection) -> None:
    """hidden_turns 테이블 추가(#128: 숨김 처리 - 비파괴, 원문·벡터는 그대로 두고 표시만 제외)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS hidden_turns(
      turn_id TEXT PRIMARY KEY, hidden_at REAL
    )""")


def _mig_0005_folders(conn: sqlite3.Connection) -> None:
    """folders/folder_items 추가(#201: 사용자가 직접 만드는 폴더 = 수동 군집).

    자동 군집(의미 기반)과 달리 사용자가 원하는 것만 모은다. 중첩 허용(parent_id).
    담는 단위는 턴과 세션 두 가지 — 세션은 참조만 두고 읽을 때 펼쳐, 그 대화가
    이어져 턴이 늘어도 폴더에 자동 포함된다.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS folders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      parent_id INTEGER REFERENCES folders(id), created_at REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS folder_items(
      folder_id INTEGER NOT NULL REFERENCES folders(id),
      kind TEXT NOT NULL, ref TEXT NOT NULL, added_at REAL,
      PRIMARY KEY(folder_id, kind, ref)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folder_items_folder ON folder_items(folder_id)")


def _mig_0006_folder_item_alias_position(conn: sqlite3.Connection) -> None:
    """folder_items 에 alias/position 추가(#201 후속).

    alias = 폴더 안에서만 보이는 이름. 원본 턴/세션 제목은 건드리지 않는다 — 모아놓고 보기 좋게
    내가 붙이는 라벨일 뿐이라, 같은 대화를 다른 폴더에서 다르게 불러도 된다.
    position = 사용자가 정한 순서(작을수록 위). NULL 이면 added_at 순으로 밀려난다.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(folder_items)")}
    if "alias" not in cols:
        conn.execute("ALTER TABLE folder_items ADD COLUMN alias TEXT")
    if "position" not in cols:
        conn.execute("ALTER TABLE folder_items ADD COLUMN position REAL")


def _mig_0007_folder_position(conn: sqlite3.Connection) -> None:
    """folders 에 position 추가 — 형제 폴더의 순서를 사용자가 정할 수 있게(#201 후속).

    이게 없으면 항상 이름순이라 드래그로 바꿀 수 있는 건 부모(뎁스)뿐이었다.
    기존 행은 NULL 로 남고, 읽을 때 NULL 은 이름순으로 밀려난다.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(folders)")}
    if "position" not in cols:
        conn.execute("ALTER TABLE folders ADD COLUMN position REAL")


def _mig_0008_session_titles(conn: sqlite3.Connection) -> None:
    """session_titles 추가 — 사용자가 직접 지은 세션 제목.

    기본 제목은 '첫 턴의 요약/질문'을 매번 계산해 쓰는 파생값이라 고쳐 쓸 데가 없었다.
    별도 테이블에 두면 재색인·정제가 다시 돌아도 사용자가 지은 이름이 살아남는다.
    (원문 대화는 건드리지 않는다 — 폴더 별칭과 같은 성격이되 이쪽은 앱 전체에 적용)
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS session_titles(
      session_id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at REAL
    )""")


def _mig_0009_core_indexes(conn: sqlite3.Connection) -> None:
    """_SCHEMA 에만 있고 마이그레이션에는 없던 핵심 인덱스 3개를 보강.

    마이그레이션 시스템 이전(user_version=0)부터 쓰던 DB 는 이 인덱스 없이 올라온다.
    신규 설치는 _SCHEMA 로 만들어지니 있고, 기존 사용자에게만 없는 상태였다 —
    같은 앱인데 DB 모양이 갈리는, append-only 패턴에서 가장 위험한 종류의 누락이다.
    특히 idx_turns_session 이 없으면 세션 조회마다 turns 풀스캔이 된다.
    (test_migrated_db_schema_matches_fresh_schema 가 이걸 잡아냈다)
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_turn ON chunks(turn_id)")


def _mig_0010_raw_mirror_bytes(conn: sqlite3.Connection) -> None:
    """raw_cursors.mirror_bytes — (지금은 쓰지 않는다)

    보존본의 깨진 꼬리를 이 값 기준으로 잘라내려고 넣었는데, 키가 틀렸다(소스 로그 경로 기준
    인데 잘라낼 대상은 보존본이라 1:N). 그 잘라내기가 실제 데이터 유실을 만들어 제거했고,
    지금은 보존본 파일을 아예 건드리지 않는다(append 전용 — raw_archive.mirror_file 주석 참고).

    컬럼은 append-only 규칙상 남긴다. **다시 쓰지 말 것** — 이 값 하나로는 '어느 소스가
    보존본의 어느 구간을 넣었는지'를 표현할 수 없고, 그게 유실 5건의 공통 원인이었다.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(raw_cursors)")}
    if "mirror_bytes" not in cols:
        conn.execute("ALTER TABLE raw_cursors ADD COLUMN mirror_bytes INTEGER")


def _mig_0011_turns_session_ts(conn: sqlite3.Connection) -> None:
    """turns(session_id, timestamp, id) — thread() 의 앞뒤 윈도우 조회용.

    idx_turns_session 은 session_id 만이라 '세션 안에서 시간순 앞뒤 N개'를 뽑을 때마다
    매칭 행 전체를 정렬해야 했다. 826턴 세션에서 7.39ms → 0.12ms(실측).
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_ts "
                 "ON turns(session_id, timestamp, id)")


def _mig_0012_unfolded(conn: sqlite3.Connection) -> None:
    """unfolded — '펼침'을 시각과 함께 남긴다(#233 기기 간 동기화용).

    접힘은 hidden_turns 에 행이 있으면 접힘이고, 펼치면 행을 지운다. 그 구조로는 '펼쳤다'를
    다른 기기에 전할 수 없다 — 상대에 행이 남아 있으면 다시 접힌 채로 돌아온다(#228 과 같은 문제).

    hidden_turns 의 의미는 건드리지 않는다. '접힘' 판정이 7군데에 흩어져 있어 컬럼을 더하면
    전부 고쳐야 하고, 하나만 빠뜨려도 접힘 상태가 조용히 틀어진다. 해제 시각만 여기 따로 둔다.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS unfolded("
                 "turn_id TEXT PRIMARY KEY, at REAL NOT NULL)")


def _mig_0013_folder_sync(conn: sqlite3.Connection) -> None:
    """폴더를 기기 간에 옮길 수 있게 하는 것들(#233 후반).

    folders.id 는 기기별 AUTOINCREMENT 라 **다른 기기의 다른 폴더가 같은 id 를 쓴다.**
    기기를 넘나드는 식별자(uid)를 따로 붙이고, 병합 판정용 시각을 단다.

    삭제는 tombstone 으로 남긴다 — 행을 지우면 상대에 남은 폴더가 다시 이겨 되살아난다.
    folder_items 를 읽는 곳이 12군데라 소프트 삭제 컬럼을 넣으면 전부 고쳐야 하고,
    하나만 빠뜨려도 지운 항목이 되살아난다. 그래서 tombstone 을 옆 테이블로 둔다.
    """
    import secrets
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(folders)")}
    if "uid" not in cols:
        conn.execute("ALTER TABLE folders ADD COLUMN uid TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE folders ADD COLUMN updated_at REAL")
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(folder_items)")}
    if "updated_at" not in icols:
        conn.execute("ALTER TABLE folder_items ADD COLUMN updated_at REAL")
    # 기존 폴더에 uid 백필. 없으면 export 에서 빠져 영영 동기화되지 않는다.
    for r in conn.execute("SELECT id, created_at FROM folders WHERE uid IS NULL").fetchall():
        conn.execute("UPDATE folders SET uid=?, updated_at=COALESCE(updated_at, created_at, 0) WHERE id=?",
                     (secrets.token_hex(8), r["id"]))
    conn.execute("UPDATE folder_items SET updated_at=COALESCE(updated_at, added_at, 0) "
                 "WHERE updated_at IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_uid ON folders(uid)")
    conn.execute("CREATE TABLE IF NOT EXISTS folder_removed("
                 "uid TEXT PRIMARY KEY, at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS folder_item_removed("
                 "folder_uid TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, at REAL NOT NULL,"
                 " PRIMARY KEY(folder_uid, kind, ref))")


def _mig_0014_turn_queued(conn: sqlite3.Connection) -> None:
    """turns 에 queued 추가(#246: 작업 중 끼어든 질문인지 표시).

    기존 행은 NULL 로 남는다 — 재색인 전까지는 '모름'이 정직하다. 읽을 때 거짓으로 취급한다.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    if "queued" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN queued INTEGER")


def _mig_0015_turn_parser_version(conn: sqlite3.Connection) -> None:
    """turns 에 parser_version 추가. 기존 행은 NULL = 옛 파서가 쓴 것(0 으로 읽는다).

    파서가 턴을 새로 가를 때 앞 턴이 정당하게 짧아지는 것을 허용하기 위한 표지.
    자세한 이유는 parser.PARSER_VERSION.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    if "parser_version" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN parser_version INTEGER")


# 순서 고정 — 끝에만 추가한다. len(_MIGRATIONS) 가 곧 최신 스키마 버전.
_MIGRATIONS: tuple[_Migration, ...] = (
    _mig_0001_source_columns,
    _mig_0002_cursor_hold_offset,
    _mig_0003_raw_cursors,
    _mig_0004_hidden_turns,
    _mig_0005_folders,
    _mig_0006_folder_item_alias_position,
    _mig_0007_folder_position,
    _mig_0008_session_titles,
    _mig_0009_core_indexes,
    _mig_0010_raw_mirror_bytes,
    _mig_0011_turns_session_ts,
    _mig_0012_unfolded,
    _mig_0013_folder_sync,
    _mig_0014_turn_queued,
    _mig_0015_turn_parser_version,
)
_SCHEMA_VERSION = len(_MIGRATIONS)


# 폴더 하위 트리(자신 + 모든 하위 폴더 id)를 SQL 안에서 훑는 재귀 CTE(#201).
# 파이썬에서 id 목록을 만들어 IN(?,?,…) 으로 넘기지 않으려는 것 — 바인딩 변수 한도를 타지 않고,
# 쿼리 문자열이 전부 정적이라 값이 끼어들 자리가 없다. 바인딩 파라미터는 폴더 id 하나뿐.
_SUBTREE_CTE = (
    "WITH RECURSIVE sub(id) AS ("
    "  SELECT id FROM folders WHERE id=?"
    "  UNION SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id"
    ") "
)


def _actions_to_json(actions: tuple[Action, ...]) -> str:
    return json.dumps([{"tool": a.tool, "detail": a.detail} for a in actions], ensure_ascii=False)


def _actions_from_json(s: str | None) -> tuple[Action, ...]:
    if not s:
        return ()
    return tuple(Action(tool=d.get("tool", ""), detail=d.get("detail", "")) for d in json.loads(s))


def _row_to_turn(row: sqlite3.Row) -> Turn:
    keys = row.keys()
    source = (row["source"] if "source" in keys else None) or "claude-code"
    return Turn(
        id=row["id"], session_id=row["session_id"], uuid=row["uuid"],
        parent_uuid=row["parent_uuid"], timestamp=row["timestamp"], project=row["project"],
        question=row["question"], answer=row["answer"], actions=_actions_from_json(row["actions"]),
        source=source,
        queued=bool(row["queued"]) if "queued" in keys else False,
        parser_version=(row["parser_version"] or 0) if "parser_version" in keys else 0,
    )


class ArchiveDB:
    def __init__(self, path: str | Path | None = None):
        path = Path(path) if path is not None else DB_PATH   # 호출 시점에 DB_PATH 조회(설정/테스트 반영)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path        # 같은 DB 를 새 커넥션으로 다시 열 때 쓴다(영속성 검증 등)
        self.conn = sqlite3.connect(str(path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기/쓰기 허용
        except sqlite3.OperationalError:
            pass  # 쓰기중이라 잠기면 스킵(다음 기회에 적용됨)
        # 스키마 생성(쓰기)은 없을 때만 → 읽기전용 명령이 쓰기락과 충돌하지 않도록.
        fresh = not self._has_schema()
        if fresh:
            self.conn.executescript(_SCHEMA)   # 신규 DB = 이미 최신 형태
        self._migrate(fresh)                   # 버전 스탬프 + 구 DB 순차 업그레이드
        self.fts_enabled = self._ensure_fts()

    def _migrate(self, fresh: bool) -> None:
        """PRAGMA user_version 기반 순차 마이그레이션. 신규 DB는 곧장 최신 버전으로 스탬프.

        - 신규(_SCHEMA로 방금 생성): 이미 최신 형태이므로 마이그레이션을 '적용 없이' 버전만 올린다.
        - 기존: 현재 user_version 다음 단계부터 끝까지, 각 단계를 자체 트랜잭션으로 적용.
        각 단계는 멱등하게 작성돼 있어(부분 적용 후 재시도 안전) 쓰기 락 등으로 중단돼도 다음 열기 때 이어진다.
        """
        cur = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if fresh:
            # 신규 DB는 단계를 돌릴 필요가 없다(이미 최신). 버전만 확정.
            if cur != _SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            return
        if cur >= _SCHEMA_VERSION:
            if cur > _SCHEMA_VERSION:
                # 더 새 버전이 만든 DB를 구 앱으로 연 경우 → 파괴적 작업은 안 하되, 앞으로 못 감을 알린다.
                logger.warning("DB 스키마 버전(%d)이 이 앱(%d)보다 높음 — 앱 업데이트를 권장", cur, _SCHEMA_VERSION)
            return
        for ver in range(cur, _SCHEMA_VERSION):
            migrate = _MIGRATIONS[ver]   # 0-기반: user_version=N 이면 다음은 인덱스 N
            try:
                with self.conn:          # 단계별 트랜잭션(실패 시 이 단계만 롤백)
                    migrate(self.conn)
                    self.conn.execute(f"PRAGMA user_version = {ver + 1}")
            except sqlite3.OperationalError as e:
                # 쓰기 락이면 다음 열기 때 이어서 적용됨(무해). 그 전엔 이후 쿼리가 'no such column' 등으로
                # 터질 수 있으므로 원인 추적용 경고를 남기고 멈춘다(더 진행하지 않음).
                logger.warning("스키마 마이그레이션 v%d 적용 실패(다음 열기 때 재시도): %s", ver + 1, e)
                return

    def _has_schema(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='turns'"
        ).fetchone()
        return row is not None

    def _ensure_fts(self) -> bool:
        """FTS5 키워드 인덱스(BM25) 준비. FTS5 미지원 빌드면 False(의미검색만 폴백)."""
        has = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='turns_fts'"
        ).fetchone()
        if has:
            return True
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE turns_fts USING fts5(turn_id UNINDEXED, text)"
            )
            return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _fts_text(question: str, answer: str, actions: tuple[Action, ...]) -> str:
        return "\n".join([question or "", answer or "", "; ".join(a.render() for a in actions)])

    def rebuild_fts(self) -> int:
        """기존 turns 전체로 FTS 인덱스를 재구축(백필/최초 1회)."""
        if not self.fts_enabled:
            return 0
        self.conn.execute("DELETE FROM turns_fts")
        n = 0
        for r in self.conn.execute("SELECT id,question,answer,actions FROM turns"):
            text = self._fts_text(r["question"], r["answer"], _actions_from_json(r["actions"]))
            self.conn.execute(
                "INSERT INTO turns_fts(turn_id,text) VALUES(?,?)", (r["id"], text)
            )
            n += 1
        self.conn.commit()
        return n

    def keyword_search(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        """FTS5 BM25 키워드 검색 → [(turn_id, score)] (score 낮을수록 관련↑)."""
        if not self.fts_enabled:
            return []
        terms = _FTS_TOKEN.findall(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.conn.execute(
                "SELECT turn_id, bm25(turns_fts) AS s FROM turns_fts "
                "WHERE turns_fts MATCH ? ORDER BY s LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["turn_id"], r["s"]) for r in rows]

    def close(self) -> None:
        self.conn.close()

    # --- 턴 -------------------------------------------------------------
    def turn_rank(self, turn_id: str) -> tuple[int, int] | None:
        """저장된 턴의 (파서 버전, 내용 길이). 없으면 None.

        턴끼리 '어느 쪽이 더 나은가'는 이 튜플의 사전식 비교로 정한다 — 더 새 파서가
        이기고, 같은 파서끼리는 더 긴(완성된) 쪽이 이긴다. upsert_turn 과 동기화가 같은
        기준을 써야 한 기기가 고친 것을 다른 기기가 되돌리지 않는다.
        """
        row = self.conn.execute(
            "SELECT coalesce(parser_version,0) AS v, length(coalesce(question,''))"
            "+length(coalesce(answer,''))+length(coalesce(actions,'')) AS n "
            "FROM turns WHERE id=?", (turn_id,),
        ).fetchone()
        return None if row is None else (int(row["v"]), int(row["n"]))

    def upsert_turn(self, turn: Turn, source: str = "claude-code", *,
                    source_file: str | None) -> bool:
        # source_file 은 **기본값 없는 키워드 인자**다(#227). 예전엔 None 기본값이 있어
        #   db = ArchiveDB(); db.upsert_turn(turn); db.commit()
        # 세 줄이면 임시 스크립트가 실사용 DB 에 조용히 행을 넣을 수 있었다(실제로 합성 데이터
        # 25개가 그렇게 들어왔다). 이제 안 넘기면 호출하는 순간 터진다.
        #
        # 울타리가 아니라 표지판이다 — 더미 값을 넣으면 그대로 뚫린다. 다만 '출처 없는 턴은
        # 저장하지 않는다'를 시그니처에 박아두면, 깜빡한 경로가 조용히 지나가지는 않는다.
        """턴 저장(멱등). **완성도 축소 금지**: 이미 저장된 턴이 더 완성(질문+답변+행동 길이가
        더 큼)이면 더 짧은 재파싱본으로 덮지 않고 그대로 둔다. 반환값 = 실제로 기록됐으면 True,
        기존을 유지(스킵)했으면 False. (긴 도구호출로 짧게 확정된 턴을 kill/재색인/기기병합이
        되돌리는 것 방지. 대화 로그는 append-only 라 '줄지 않는다'가 안전한 불변식.)"""
        actions_json = _actions_to_json(turn.actions)
        stored = self.turn_rank(turn.id)
        if stored is not None:
            new_n = len(turn.question or "") + len(turn.answer or "") + len(actions_json)
            # 같은 파서끼리는 예전 그대로 '더 짧으면 거부'. 파서가 올라가면 한 번은 짧아져도
            # 덮는다(턴을 새로 갈랐을 때). 더 옛 파서는 새 파서가 쓴 것을 못 덮는다 —
            # 업그레이드 안 한 기기가 합쳐진 옛 턴을 동기화로 되밀어 넣는 걸 막는다.
            if (turn.parser_version, new_n) < stored:
                return False
        self.conn.execute(
            """INSERT INTO turns(id,session_id,uuid,parent_uuid,timestamp,project,
                 question,answer,actions,source,source_file,queued,parser_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 question=excluded.question, answer=excluded.answer, actions=excluded.actions,
                 source=excluded.source, source_file=excluded.source_file,
                 queued=excluded.queued, parser_version=excluded.parser_version""",
            (turn.id, turn.session_id, turn.uuid, turn.parent_uuid, turn.timestamp,
             turn.project, turn.question, turn.answer, actions_json,
             source, source_file, 1 if turn.queued else 0, turn.parser_version),
        )
        if self.fts_enabled:  # 키워드 인덱스 동기화(멱등)
            self.conn.execute("DELETE FROM turns_fts WHERE turn_id=?", (turn.id,))
            self.conn.execute(
                "INSERT INTO turns_fts(turn_id,text) VALUES(?,?)",
                (turn.id, self._fts_text(turn.question, turn.answer, turn.actions)),
            )
        return True

    def delete_turns(self, turn_ids: list[str]) -> list[str]:
        """턴·청크·FTS 행을 삭제하고, 제거된 chunk_key 목록을 돌려준다(벡터 인덱스 정리용)."""
        removed: list[str] = []
        for tid in turn_ids:
            rows = self.conn.execute(
                "SELECT chunk_key FROM chunks WHERE turn_id=?", (tid,)).fetchall()
            removed.extend(r["chunk_key"] for r in rows)
            self.conn.execute("DELETE FROM chunks WHERE turn_id=?", (tid,))
            self.conn.execute("DELETE FROM turns WHERE id=?", (tid,))
            if self.fts_enabled:
                self.conn.execute("DELETE FROM turns_fts WHERE turn_id=?", (tid,))
        return removed

    def get_turn(self, turn_id: str) -> Turn | None:
        row = self.conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return _row_to_turn(row) if row else None

    # --- 숨김(#128) --------------------------------------------------
    # 비파괴: 원문·청크·벡터는 그대로 두고 hidden_turns 에 있으면 검색/세션목록/지도에서만 제외.
    # 재색인·reconcile 은 turns 를 지우지 않으므로(append-only 갱신) 이 테이블만 별도로 두면 그대로 살아남는다.
    def hide_turns(self, turn_ids: list[str]) -> int:
        """반환값 = 실제로 새로 숨겨진 개수(이미 숨겨져 있던 건 제외 - 멱등 재시도 시 정확한 카운트)."""
        if not turn_ids:
            return 0
        now = time.time()
        inserted = 0
        for tid in turn_ids:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO hidden_turns(turn_id, hidden_at) VALUES(?,?)", (tid, now))
            self.conn.execute("DELETE FROM unfolded WHERE turn_id=?", (tid,))   # 최신 상태는 '접힘'
            inserted += cur.rowcount
        self.conn.commit()
        return inserted

    def hide_session(self, session_id: str) -> int:
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM turns WHERE session_id=?", (session_id,))]
        return self.hide_turns(ids)

    def unhide_turns(self, turn_ids: list[str]) -> None:
        if not turn_ids:
            return
        now = time.time()
        self.conn.executemany(
            "DELETE FROM hidden_turns WHERE turn_id=?", [(tid,) for tid in turn_ids])
        # '펼쳤다'를 시각과 함께 남긴다 — 이게 없으면 다른 기기의 접힘이 다시 이긴다(#233).
        self.conn.executemany(
            "INSERT INTO unfolded(turn_id, at) VALUES(?,?) "
            "ON CONFLICT(turn_id) DO UPDATE SET at=excluded.at", [(tid, now) for tid in turn_ids])
        self.conn.commit()

    def unhide_session(self, session_id: str) -> None:
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM turns WHERE session_id=?", (session_id,))]
        self.unhide_turns(ids)

    def hidden_turn_ids(self) -> set[str]:
        """읽을 때 필터용 전체 숨김 turn id 집합(검색·지도 등에서 공유)."""
        return {r["turn_id"] for r in self.conn.execute("SELECT turn_id FROM hidden_turns")}

    def folded_groups(self, *, with_items: bool = True) -> dict:
        """접힘 화면(#128): 통째로 접은 세션과, 일부만 접힌 세션의 채팅을 **세션별로 묶어** 돌려준다.

        예전엔 접힌 턴을 하나씩 최근 접은 순으로 200개까지 늘어놨다. 500턴짜리 세션을 접으면 화면이
        500줄이 됐고, 200개를 넘는 부분은 아예 안 보였다.

        - sessions: 그 세션의 턴이 **전부** 접힌 것. 한 줄로(대화 수만)
        - chats   : 일부만 접힌 세션. 세션별 묶음 안에 접힌 턴 목록
        - count   : 접은 세션 수 + 일부 접힌 세션의 접힌 턴 수(좌측 배지 - 화면에 보이는 단위와 같다)
        턴이 사라진 고아 행(hidden_turns 만 남은 것)은 펼칠 대상이 없어 뺀다.
        """
        groups = self.conn.execute(
            """WITH f AS (
                 SELECT t.session_id AS sid, COUNT(*) AS folded, MAX(h.hidden_at) AS last_hidden
                 FROM hidden_turns h JOIN turns t ON t.id = h.turn_id GROUP BY t.session_id),
               s AS (
                 SELECT session_id AS sid, COUNT(*) AS total, MIN(timestamp) AS started,
                        MAX(timestamp) AS ended FROM turns
                 WHERE session_id IN (SELECT sid FROM f) GROUP BY session_id)
               SELECT f.sid, f.folded, f.last_hidden, s.total, s.started, s.ended
               FROM f JOIN s ON s.sid = f.sid ORDER BY f.last_hidden DESC"""
        ).fetchall()
        full = [g for g in groups if g["folded"] >= g["total"]]
        part = [g for g in groups if g["folded"] < g["total"]]
        count = len(full) + sum(g["folded"] for g in part)
        if not with_items:
            return {"sessions": [], "chats": [], "count": count}

        sids = [g["sid"] for g in groups]
        heads: dict[str, dict] = {}
        if sids:
            marks = ",".join("?" * len(sids))
            for r in self.conn.execute(
                f"""SELECT session_id, COALESCE(NULLIF(summary,''), question) AS h, id, rn, rd FROM (
                      SELECT session_id, summary, question, id,
                             ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY timestamp, id) AS rn,
                             ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY timestamp DESC, id DESC) AS rd
                      FROM turns WHERE session_id IN ({marks}))
                    WHERE rn = 1 OR rd = 1""", sids):
                d = heads.setdefault(r["session_id"], {})
                if r["rn"] == 1:
                    d["headline"] = r["h"] or ""
                if r["rd"] == 1:
                    d["last_turn_id"] = r["id"]
            for r in self.conn.execute(
                    f"SELECT session_id, title FROM session_titles WHERE title<>'' AND session_id IN ({marks})", sids):
                heads.setdefault(r["session_id"], {})["headline"] = r["title"]   # 사용자가 지은 제목 우선

        def base(g) -> dict:
            h = heads.get(g["sid"], {})
            return {"session_id": g["sid"], "headline": h.get("headline", ""), "total": g["total"],
                    "folded": g["folded"], "started": g["started"], "ended": g["ended"],
                    "last_hidden": g["last_hidden"], "last_turn_id": h.get("last_turn_id")}

        chats = [dict(base(g), turns=[]) for g in part]
        if chats:
            by_sid = {c["session_id"]: c for c in chats}
            marks = ",".join("?" * len(by_sid))
            for r in self.conn.execute(
                f"""SELECT t.id, t.session_id, COALESCE(NULLIF(t.summary,''), t.question) AS h, t.timestamp
                    FROM hidden_turns h JOIN turns t ON t.id = h.turn_id
                    WHERE t.session_id IN ({marks}) ORDER BY t.timestamp, t.id""", list(by_sid)):
                by_sid[r["session_id"]]["turns"].append(
                    {"turn_id": r["id"], "headline": r["h"] or "", "timestamp": r["timestamp"]})
        return {"sessions": [base(g) for g in full], "chats": chats, "count": count}

    def distinct_sources(self) -> list[tuple[str, int]]:
        """색인된 턴이 있는 출처와 개수(검색 필터 옵션용). NULL(레거시)은 claude-code로 취급."""
        rows = self.conn.execute(
            "SELECT COALESCE(source, 'claude-code') AS s, COUNT(*) AS n "
            "FROM turns GROUP BY s ORDER BY n DESC"
        ).fetchall()
        return [(r["s"], r["n"]) for r in rows]

    def session_source(self, session_id: str) -> tuple[str, str | None, str | None] | None:
        """세션의 (source, source_file, project). 재개 명령·원문 존재 확인용.
        source_file 있는 행을 우선(재색인 전 레거시 행은 NULL). 세션 없으면 None."""
        row = self.conn.execute(
            "SELECT source, source_file, project FROM turns WHERE session_id=? "
            "ORDER BY (source_file IS NULL), timestamp, id LIMIT 1", (session_id,)
        ).fetchone()
        if not row:
            return None
        return (row["source"] or "claude-code", row["source_file"], row["project"])

    def get_enrichment(self, turn_id: str) -> tuple[str | None, list[str]]:
        row = self.conn.execute("SELECT summary,tags FROM turns WHERE id=?", (turn_id,)).fetchone()
        if not row:
            return None, []
        return row["summary"], (json.loads(row["tags"]) if row["tags"] else [])

    def set_enrichment(self, turn_id: str, summary: str, tags: list[str]) -> int:
        """정제본 저장. 실제 갱신된 행 수 반환(0 = 매칭 turn 없음, 예: LLM이 id를 잘못 복사)."""
        cur = self.conn.execute(
            "UPDATE turns SET summary=?, tags=? WHERE id=?",
            (summary, json.dumps(tags, ensure_ascii=False), turn_id),
        )
        return cur.rowcount

    def thread(self, turn_id: str, window: int = 2) -> list[Turn]:
        """같은 세션에서 시간순 앞뒤 window 개 턴을 포함해 반환.

        세션 전체를 SELECT * 로 읽어 파이썬에서 잘라내던 코드였다. 검색 결과 한 건마다
        호출되므로(search.py) 20건이면 세션 20개를 통째로 메모리에 올렸고, 그게 검색
        시간의 92%였다(826턴 세션 24.85ms → 0.12ms, idx_turns_session_ts 와 함께).

        (timestamp, id) 행 값 비교로 경계를 잡는다 — ORDER BY 와 같은 키라야 LIMIT 가
        인덱스만 읽고 끝난다. 정렬 키를 바꾸면 이 인덱스도 같이 바꿔야 한다.
        """
        turn = self.get_turn(turn_id)
        if not turn:
            return []
        key = (turn.session_id, turn.timestamp, turn_id)
        before = self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND (timestamp, id) < (?, ?) "
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (*key, window)).fetchall()
        after = self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND (timestamp, id) >= (?, ?) "
            "ORDER BY timestamp, id LIMIT ?", (*key, window + 1)).fetchall()
        if not after:                      # 자신이 안 잡히면(행 불일치) 최소한 자기 턴은 준다
            return [turn]
        return [_row_to_turn(r) for r in reversed(before)] + [_row_to_turn(r) for r in after]

    # --- 청크 -----------------------------------------------------------
    def trim_chunks(self, turn_id: str, keep: int) -> list[str]:
        """turn_id 의 청크 중 순번 keep 이상을 지우고 지운 chunk_key 를 돌려준다(벡터 정리용).

        add_chunks 는 같은 키를 덮을 뿐이라, 턴이 짧아져 청크 수가 줄면 뒤쪽이 옛 내용으로
        남는다. 턴이 절대 안 줄던 동안엔 드러나지 않았다(파서 버전이 오르면 줄 수 있다).
        """
        rows = self.conn.execute(
            "SELECT chunk_key FROM chunks WHERE turn_id=? AND idx>=?", (turn_id, keep)).fetchall()
        if rows:
            self.conn.execute("DELETE FROM chunks WHERE turn_id=? AND idx>=?", (turn_id, keep))
        return [r["chunk_key"] for r in rows]

    def add_chunks(self, chunks) -> None:
        self.conn.executemany(
            """INSERT INTO chunks(chunk_key,turn_id,idx,text) VALUES(?,?,?,?)
               ON CONFLICT(chunk_key) DO UPDATE SET text=excluded.text""",
            [(f"{c.turn_id}#{c.index}", c.turn_id, c.index, c.text) for c in chunks],
        )

    def turn_id_of_chunk(self, chunk_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT turn_id FROM chunks WHERE chunk_key=?", (chunk_key,)
        ).fetchone()
        return row["turn_id"] if row else None

    # --- 커서 -----------------------------------------------------------
    def get_cursor(self, file_path: str) -> tuple[int, int, float]:
        row = self.conn.execute(
            "SELECT offset,size,mtime FROM cursors WHERE file_path=?", (file_path,)
        ).fetchone()
        return (row["offset"], row["size"], row["mtime"]) if row else (0, 0, 0.0)

    def get_hold(self, file_path: str) -> int | None:
        """idle 확정된 '열린 마지막 턴'의 시작 offset(뒷내용이 붙으면 여기부터 다시 읽음). 없으면 None."""
        row = self.conn.execute(
            "SELECT hold_offset FROM cursors WHERE file_path=?", (file_path,)
        ).fetchone()
        return row["hold_offset"] if row else None

    def set_cursor(self, file_path: str, offset: int, size: int, mtime: float,
                   hold_offset: int | None = None) -> None:
        self.conn.execute(
            """INSERT INTO cursors(file_path,offset,size,mtime,updated_at,hold_offset)
                 VALUES(?,?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                 offset=excluded.offset, size=excluded.size, mtime=excluded.mtime,
                 updated_at=excluded.updated_at, hold_offset=excluded.hold_offset""",
            (file_path, offset, size, mtime, time.time(), hold_offset),
        )

    def clear_cursors(self) -> None:
        """모든 파일 커서 초기화 → 다음 인덱싱이 전 세션을 처음부터 재처리(모델 교체 재색인용)."""
        self.conn.execute("DELETE FROM cursors")
        self.conn.commit()

    # --- 원본 미러 커서(#163 P1) — turns 의 hold/재처리와 무관한 순수 append 추적 -------
    def get_raw_cursor(self, file_path: str) -> int:
        row = self.conn.execute(
            "SELECT mirrored_offset FROM raw_cursors WHERE file_path=?", (file_path,)
        ).fetchone()
        return row["mirrored_offset"] if row else 0

    def set_raw_cursor(self, file_path: str, mirrored_offset: int, session_id: str, source: str) -> None:
        self.conn.execute(
            """INSERT INTO raw_cursors(file_path,mirrored_offset,session_id,source,updated_at)
                 VALUES(?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                 mirrored_offset=excluded.mirrored_offset, session_id=excluded.session_id,
                 source=excluded.source, updated_at=excluded.updated_at""",
            (file_path, mirrored_offset, session_id, source, time.time()),
        )

    def clear_raw_cursors(self, source: str, session_ids: list[str]) -> int:
        """이 세션들의 미러 커서를 지운다 → 다음 회차에 0부터 다시 미러링.
        보존본(.gz)을 지웠는데 커서가 남으면, 이어지는 로그의 '꼬리'만 새 파일에 쌓여
        머리가 잘린 보존본이 되고 has_mirror 는 그걸 복구 가능으로 표시한다."""
        if not session_ids:
            return 0
        n = 0
        for sid in session_ids:
            cur = self.conn.execute(
                "DELETE FROM raw_cursors WHERE source=? AND session_id=?", (source, sid))
            n += cur.rowcount or 0
        self.conn.commit()
        return n

    # --- 폴더(#201) ------------------------------------------------------
    # 사용자가 직접 만드는 수동 군집. 자동 군집(의미 기반)과 달리 원하는 것만 모은다.
    # (_SUBTREE_CTE = 자신+하위 폴더 id 집합 'sub'. 아래 조회들이 공통으로 앞에 붙여 쓴다)
    # 세션은 참조만 담아 읽을 때 펼친다 → 그 대화가 이어져 턴이 늘어도 폴더에 자동 포함된다.
    def create_folder(self, name: str, parent_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO folders(name, parent_id, created_at) VALUES(?,?,?)",
            (name, parent_id, time.time()))
        self.conn.execute("UPDATE folders SET uid=?, updated_at=? WHERE id=?",
                          (secrets.token_hex(8), time.time(), cur.lastrowid))
        self.conn.commit()
        return int(cur.lastrowid)

    def list_folders(self) -> list[dict]:
        """전체 폴더(트리 구성은 호출부에서). 각 폴더의 '직접' 담긴 항목 수를 함께."""
        rows = self.conn.execute(
            "SELECT f.id, f.name, f.parent_id, f.created_at, f.position,"
            "       (SELECT COUNT(*) FROM folder_items i WHERE i.folder_id = f.id) AS n_items "
            # 같은 부모 안에서 사용자가 정한 순서 우선, 아직 없으면(NULL) 이름순으로 뒤에.
            "FROM folders f ORDER BY (f.position IS NULL), f.position, f.name, f.id"
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "parent_id": r["parent_id"],
                 "created_at": r["created_at"], "position": r["position"],
                 "items": r["n_items"]} for r in rows]

    def get_folder(self, folder_id: int) -> dict | None:
        r = self.conn.execute(
            "SELECT id, name, parent_id, created_at FROM folders WHERE id=?", (folder_id,)).fetchone()
        return None if r is None else {"id": r["id"], "name": r["name"],
                                       "parent_id": r["parent_id"], "created_at": r["created_at"]}

    def folder_descendants(self, folder_id: int) -> list[int]:
        """자신 + 모든 하위 폴더 id. 중첩 폴더의 내용을 한 번에 훑을 때 쓴다."""
        out, stack = [], [folder_id]
        seen = set()
        while stack:
            fid = stack.pop()
            if fid in seen:      # 혹시 모를 순환에도 멈추도록(생성 시 막지만 방어적으로)
                continue
            seen.add(fid)
            out.append(fid)
            stack += [r["id"] for r in self.conn.execute(
                "SELECT id FROM folders WHERE parent_id=?", (fid,))]
        return out

    def rename_folder(self, folder_id: int, name: str) -> None:
        self.conn.execute("UPDATE folders SET name=?, updated_at=? WHERE id=?",
                          (name, time.time(), folder_id))
        self.conn.commit()

    def move_folder(self, folder_id: int, parent_id: int | None, before_id: int | None = None) -> bool:
        """폴더를 parent_id 밑으로 옮긴다. 자기 자신/자기 하위로는 못 옮긴다(순환 방지).

        before_id 를 주면 그 형제 '바로 앞'에, 없으면 맨 뒤에 놓는다 — 부모(뎁스)만이 아니라
        형제 사이 순서까지 한 번의 드래그로 정하기 위한 것. 옮긴 뒤 그 부모의 형제들에게
        1,2,3… 을 다시 매겨(NULL 이었던 기존 폴더 포함) 순서를 확정한다.
        """
        if parent_id is not None and parent_id in self.folder_descendants(folder_id):
            return False
        # 이동도 updated_at 을 찍어야 다른 기기에 전해진다(#233). 안 찍으면 승자 판정이 옛 값을
        # 보고 "상대가 더 낡음"으로 판단해 이동이 영원히 동기화되지 않는다.
        #
        # 아래 형제 재정렬이 옮긴 폴더까지 포함해 다시 찍으므로 여기서 찍는 건 **중복이다.**
        # 그래도 남긴다 — 형제 재정렬을 건드리는 사람이 이 의존을 모른 채 바꾸면 이동 동기화가
        # 조용히 멈춘다. 둘 중 하나만 지워도 테스트가 안 잡히는 것을 확인했다(둘 다 지워야 잡힘).
        now = time.time()
        self.conn.execute("UPDATE folders SET parent_id=?, updated_at=? WHERE id=?",
                          (parent_id, now, folder_id))

        sibs = [r["id"] for r in self.conn.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND id<>? "
            "ORDER BY (position IS NULL), position, name, id", (parent_id, folder_id))]
        at = sibs.index(before_id) if before_id in sibs else len(sibs)
        sibs.insert(at, folder_id)
        self.conn.executemany("UPDATE folders SET position=?, updated_at=? WHERE id=?",
                              [(i, now, fid) for i, fid in enumerate(sibs, start=1)])
        self.conn.commit()
        return True

    def delete_folder(self, folder_id: int) -> int:
        """폴더와 그 하위 폴더를 통째로 삭제. 담긴 항목의 '참조'만 지우며 원문·턴은 그대로.
        반환: 삭제된 폴더 수. (폴더 수는 적어 한 건씩 지워도 충분 — SQL 조립을 피한다)"""
        ids = [(i,) for i in self.folder_descendants(folder_id)]
        now = time.time()
        # tombstone — 안 남기면 상대에 있는 폴더가 다시 이겨 되살아난다(#233).
        for (fid,) in ids:
            u = self.conn.execute("SELECT uid FROM folders WHERE id=?", (fid,)).fetchone()
            if u and u["uid"]:
                self.conn.execute("INSERT INTO folder_removed(uid, at) VALUES(?,?) "
                                  "ON CONFLICT(uid) DO UPDATE SET at=excluded.at", (u["uid"], now))
        self.conn.executemany("DELETE FROM folder_items WHERE folder_id=?", ids)
        self.conn.executemany("DELETE FROM folders WHERE id=?", ids)
        self.conn.commit()
        return len(ids)

    def add_to_folder(self, folder_id: int, kind: str, ref: str) -> None:
        """새로 담기면 목록 맨 아래로(position = 현재 최대 + 1)."""
        nxt = self.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM folder_items WHERE folder_id=?",
            (folder_id,)).fetchone()["p"]
        self.conn.execute(
            "INSERT OR IGNORE INTO folder_items(folder_id, kind, ref, added_at, position, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (folder_id, kind, ref, time.time(), nxt, time.time()))
        self.conn.commit()

    def set_item_alias(self, folder_id: int, kind: str, ref: str, alias: str | None) -> None:
        """이 폴더에서만 쓸 표시 이름. 원본 턴/세션 제목은 건드리지 않는다(빈 값이면 원래 제목으로)."""
        self.conn.execute(
            "UPDATE folder_items SET alias=?, updated_at=? WHERE folder_id=? AND kind=? AND ref=?",
            (alias or None, time.time(), folder_id, kind, ref))
        self.conn.commit()

    def reorder_folder(self, folder_id: int, order: list[tuple[str, str]]) -> int:
        """폴더 안 항목 순서를 통째로 다시 매긴다. order = [(kind, ref), …] 화면에 보이는 순서.
        반환: 실제로 갱신된 행 수(0이면 그 사이 항목이 사라진 것 — 호출부가 구분할 수 있게).

        목록에 없는 항목(그 사이 다른 창에서 담은 것 등)은 주어진 목록 뒤로 밀어 번호를 잇는다.
        예전엔 주어진 것에만 1..n 을 매겨, 남은 항목의 position 과 정면 충돌했다
        (같은 번호가 둘 생겨 정렬이 added_at 타이브레이크 운에 맡겨졌다).
        """
        changed = 0
        for i, (kind, ref) in enumerate(order, start=1):
            cur = self.conn.execute(
                "UPDATE folder_items SET position=?, updated_at=? WHERE folder_id=? AND kind=? AND ref=?",
                (i, time.time(), folder_id, kind, ref))
            changed += cur.rowcount or 0
        given = set(order)
        rest = [(r["kind"], r["ref"]) for r in self.conn.execute(
            "SELECT kind, ref FROM folder_items WHERE folder_id=? "
            "ORDER BY (position IS NULL), position, added_at", (folder_id,))
            if (r["kind"], r["ref"]) not in given]
        if rest:
            self.conn.executemany(
                "UPDATE folder_items SET position=?, updated_at=? WHERE folder_id=? AND kind=? AND ref=?",
                [(len(order) + i, time.time(), folder_id, k, r)
                 for i, (k, r) in enumerate(rest, start=1)])
        self.conn.commit()
        return changed

    def remove_from_folder(self, folder_id: int, kind: str, ref: str) -> int:
        """반환: 지운 행 수(0이면 이미 없던 항목 — '눌렀는데 안 먹힌다'를 구분하려면 필요)."""
        cur = self.conn.execute(
            "DELETE FROM folder_items WHERE folder_id=? AND kind=? AND ref=?", (folder_id, kind, ref))
        u = self.conn.execute("SELECT uid FROM folders WHERE id=?", (folder_id,)).fetchone()
        if u and u["uid"]:      # tombstone — 안 남기면 뺀 항목이 다시 들어온다(#233)
            self.conn.execute(
                "INSERT INTO folder_item_removed(folder_uid, kind, ref, at) VALUES(?,?,?,?) "
                "ON CONFLICT(folder_uid, kind, ref) DO UPDATE SET at=excluded.at",
                (u["uid"], kind, ref, time.time()))
        self.conn.commit()
        return cur.rowcount or 0

    def folder_items(self, folder_id: int) -> list[dict]:
        """폴더에 '직접' 담긴 항목(하위 폴더 제외). 표시용 헤드라인을 붙여 돌려준다.

        종류별로 쿼리 1회씩, 총 2회만 쓴다(항목마다 2~3회 돌면 100개짜리 폴더가 300 왕복).
        세션 쪽은 web.api_sessions 와 같은 윈도우 함수 패턴 — 같은 값을 같은 방식으로 뽑아
        두 화면의 제목이 갈리지 않게 한다.
        """
        # 턴 항목: 턴 본문 + 접힘 여부를 조인 한 번으로.
        turn_rows = self.conn.execute(
            "SELECT fi.kind, fi.ref, fi.added_at, fi.alias, fi.position,"
            "       t.session_id, t.summary, t.question, t.timestamp,"
            "       (h.turn_id IS NOT NULL) AS hidden"
            "  FROM folder_items fi"
            "  LEFT JOIN turns t ON t.id = fi.ref"                  # 없는 턴(유령)도 목록에서 빠지지 않게
            "  LEFT JOIN hidden_turns h ON h.turn_id = fi.ref"
            " WHERE fi.folder_id=? AND fi.kind='turn'", (folder_id,)).fetchall()

        # 세션 항목: 담긴 건 참조뿐이라 개수·대표 제목을 현재 기준으로 매번 계산한다.
        #
        # 집계(GROUP BY)와 대표 턴 고르기를 나눈다. 예전엔 윈도우 함수 한 방으로 둘 다 했는데,
        # ROW_NUMBER 의 ORDER BY 가 식이라 인덱스를 못 타 세션의 모든 턴을 임시 B-tree 로
        # 정렬했다(세션 90개 폴더에서 30.2ms). 서브쿼리로는 **대표 턴의 id 만** 고르고
        # 본문은 바깥에서 한 번 조인한다 — 1.86ms(16배). 쿼리 수는 1회 그대로다.
        # (본문 컬럼까지 서브쿼리로 끌면 서브쿼리를 컬럼마다 돌아 이득이 사라진다.)
        #
        # GROUP BY 를 fi.ref 로 잡는 이유 — 턴이 0개인 참조는 t.* 가 NULL 인데, t.session_id 로
        # 묶으면 그 NULL 들이 한 그룹에 뭉쳐 개수가 틀어진다. COUNT(t.id) 도 같은 이유(NULL 제외).
        sess_rows = self.conn.execute(
            "SELECT g.ref, g.added_at, g.alias, g.position, g.n, g.n_hidden, g.ended, g.title,"
            "       ht.summary, ht.question FROM ("
            "  SELECT fi.ref, fi.added_at, fi.alias, fi.position, st.title,"
            "         COUNT(t.id) AS n,"
            "         SUM(h.turn_id IS NOT NULL) AS n_hidden,"
            "         MAX(t.timestamp) AS ended,"
            # 대표 헤드라인은 '접히지 않은' 턴에서 먼저 고른다(api_sessions 와 동일 기준 —
            # 접은 첫 턴이 계속 제목으로 뜨면 접은 의미가 없다). 정렬 기준을 바꾸면 두 화면의
            # 제목이 갈리므로 api_sessions 와 함께 바꿔야 한다.
            "         (SELECT t2.id FROM turns t2"
            "            LEFT JOIN hidden_turns h2 ON h2.turn_id = t2.id"
            "           WHERE t2.session_id = fi.ref"
            "           ORDER BY (h2.turn_id IS NOT NULL), t2.timestamp, t2.id LIMIT 1) AS head_id"
            "    FROM folder_items fi"
            "    LEFT JOIN turns t ON t.session_id = fi.ref"
            "    LEFT JOIN hidden_turns h ON h.turn_id = t.id"
            "    LEFT JOIN session_titles st ON st.session_id = fi.ref"
            "   WHERE fi.folder_id=? AND fi.kind='session'"
            "   GROUP BY fi.ref"
            ") g LEFT JOIN turns ht ON ht.id = g.head_id", (folder_id,)).fetchall()

        out = []
        for r in turn_rows:
            out.append({
                "kind": "turn", "ref": r["ref"], "added_at": r["added_at"],
                "alias": r["alias"], "position": r["position"],
                "session_id": r["session_id"],
                "headline": (r["summary"] or r["question"] or ""),
                "timestamp": r["timestamp"],
                "hidden": bool(r["hidden"]),   # 접힘(#128) — 폴더에서도 접기/펼치기 하도록
            })
        for r in sess_rows:
            n = r["n"] or 0
            out.append({
                "kind": "session", "ref": r["ref"], "added_at": r["added_at"],
                "alias": r["alias"], "position": r["position"],
                "session_id": r["ref"], "count": n,
                # 사용자가 지은 세션 제목이 있으면 그게 우선(/api/sessions 와 같은 기준).
                # 안 보면 제목을 바꿔도 폴더 화면에만 옛 제목이 남는다.
                "headline": (r["title"] or r["summary"] or r["question"] or ""),
                "timestamp": r["ended"],
                # 세션은 전 턴이 접혔을 때만 '접힘'(세션 목록과 같은 기준)
                "hidden": n > 0 and (r["n_hidden"] or 0) == n,
            })
        # 사용자가 정한 순서(position) 우선, 아직 없으면 담은 순. NULL 은 뒤로.
        out.sort(key=lambda i: (i["position"] is None, i["position"] or 0, i["added_at"] or 0))
        for item in out:
            # 폴더에서 붙인 이름이 있으면 그걸 제목으로(원본은 original_headline 으로 함께 내려줌).
            item["original_headline"] = item["headline"]
            if item["alias"]:
                item["headline"] = item["alias"]
        return out

    def folder_turn_ids(self, folder_id: int, include_descendants: bool = True) -> set[str]:
        """폴더가 가리키는 모든 턴 id(폴더 내 검색용). 세션 참조는 지금의 턴 전체로 펼친다.

        하위 폴더는 재귀 CTE로, 세션 참조는 turns 조인으로 훑는다 — id 목록을 파이썬에서
        만들어 IN(?,?,…) 으로 넘기지 않으므로 (a) 바인딩 변수 한도(구 SQLite 기본 999)와
        무관하고 (b) SQL 문자열이 전부 정적이라 값이 끼어들 자리가 없다.
        """
        turn_q, sess_q = (
            (_SUBTREE_CTE + "SELECT ref FROM folder_items "
                            "WHERE folder_id IN (SELECT id FROM sub) AND kind='turn'",
             _SUBTREE_CTE + "SELECT t.id FROM turns t JOIN folder_items i ON i.ref = t.session_id "
                            "WHERE i.folder_id IN (SELECT id FROM sub) AND i.kind='session'")
            if include_descendants else
            ("SELECT ref FROM folder_items WHERE folder_id=? AND kind='turn'",
             "SELECT t.id FROM turns t JOIN folder_items i ON i.ref = t.session_id "
             "WHERE i.folder_id=? AND i.kind='session'")
        )
        turn_ids = {r["ref"] for r in self.conn.execute(turn_q, (folder_id,))}
        turn_ids |= {r["id"] for r in self.conn.execute(sess_q, (folder_id,))}
        return turn_ids

    def folders_of(self, kind: str, ref: str) -> list[int]:
        """이 항목이 담긴 폴더 id들(같은 항목을 여러 폴더에 담을 수 있다)."""
        return [r["folder_id"] for r in self.conn.execute(
            "SELECT folder_id FROM folder_items WHERE kind=? AND ref=?", (kind, ref))]

    # --- 세션 제목(사용자 지정) ------------------------------------------
    def set_session_title(self, session_id: str, title: str | None) -> None:
        """빈 값이면 지정을 지워 기본 제목(첫 턴 요약)으로 되돌린다."""
        if title:
            self.conn.execute(
                "INSERT INTO session_titles(session_id, title, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at",
                (session_id, title, time.time()))
        else:
            # 행을 지우면 '지웠다'를 다른 기기에 전할 수 없어 옛 제목이 되살아난다(#233).
            # 빈 제목을 시각과 함께 남겨 '늦게 바꾼 쪽이 이김'에 그대로 태운다.
            self.conn.execute(
                "INSERT INTO session_titles(session_id, title, updated_at) VALUES(?,'',?) "
                "ON CONFLICT(session_id) DO UPDATE SET title='', updated_at=excluded.updated_at",
                (session_id, time.time()))
        self.conn.commit()

    # --- 기기 간 동기화용(#233) -------------------------------------
    # 어느 쪽이 이기는지는 '늦게 바꾼 쪽'으로 단순하게 간다. 제목·접힘은 잘못 퍼져도
    # 되돌릴 수 있어서, 턴 삭제(#228)처럼 보수적으로 갈 이유가 없다.

    def sync_folder_rows(self) -> dict:
        """폴더 관련 동기화 페이로드. uid 기준이라 기기별 id 와 무관하다."""
        folders = [(r["uid"], r["name"], r["parent_uid"], r["position"], r["updated_at"] or 0.0)
                   for r in self.conn.execute(
                       "SELECT f.uid, f.name, f.position, f.updated_at,"
                       "       (SELECT p.uid FROM folders p WHERE p.id=f.parent_id) AS parent_uid"
                       "  FROM folders f WHERE f.uid IS NOT NULL")]
        items = [(r["uid"], r["kind"], r["ref"], r["alias"], r["position"], r["updated_at"] or 0.0)
                 for r in self.conn.execute(
                     "SELECT f.uid, i.kind, i.ref, i.alias, i.position, i.updated_at"
                     "  FROM folder_items i JOIN folders f ON f.id=i.folder_id WHERE f.uid IS NOT NULL")]
        gone = [(r["uid"], r["at"]) for r in self.conn.execute("SELECT uid, at FROM folder_removed")]
        gone_items = [(r["folder_uid"], r["kind"], r["ref"], r["at"])
                      for r in self.conn.execute(
                          "SELECT folder_uid, kind, ref, at FROM folder_item_removed")]
        return {"folders": folders, "items": items, "gone": gone, "gone_items": gone_items}

    def _folder_id_by_uid(self, uid: str) -> int | None:
        r = self.conn.execute("SELECT id FROM folders WHERE uid=?", (uid,)).fetchone()
        return r["id"] if r else None

    def apply_folder(self, uid: str, name: str, parent_uid: str | None,
                     position: float | None, at: float) -> bool:
        """폴더 하나를 반영(없으면 만들고, 있으면 더 새로울 때만 덮는다).

        부모는 uid 로 받아 로컬 id 로 옮긴다. 상대에만 있는 부모는 아직 없을 수 있어
        그때는 최상위로 두고, 부모가 도착한 다음 회차에 제자리를 찾는다.
        """
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        r = self.conn.execute("SELECT id, updated_at, parent_id FROM folders WHERE uid=?",
                              (uid,)).fetchone()
        gone = self.conn.execute("SELECT at FROM folder_removed WHERE uid=?", (uid,)).fetchone()
        if gone and (gone["at"] or 0.0) >= at:
            return False                       # 여기서 지운 게 더 최신 — 되살리지 않는다
        pid = self._folder_id_by_uid(parent_uid) if parent_uid else None
        if r is None:
            self.conn.execute(
                "INSERT INTO folders(name, parent_id, created_at, position, uid, updated_at)"
                " VALUES(?,?,?,?,?,?)", (name, pid, at, position, uid, at))
            return True
        # 같은 시각이라도 **부모를 아직 못 붙인 상태면 다시 시도한다.** 안 그러면 자식이 부모보다
        # 먼저 도착했을 때 최상위로 파킹된 채 영구 고아가 된다 — 발신측이 그 폴더를 다시
        # 건드리지 않는 한 at 이 그대로라 '이미 반영됨'으로 걸러지기 때문이다(실측).
        # 파킹된 폴더는 **부모만 따로 붙인다.**
        #
        # 처음엔 needs_parent 일 때 시각 가드 전체를 우회하게 했는데 그게 더 나빴다 — 그 사이
        # 사용자가 로컬에서 바꾼 이름·위치까지 옛 레코드로 덮어쓰고 updated_at 을 과거로
        # 되돌렸다(실측: 방금 지은 이름이 사라졌다). 부모 링크만 메우고 나머지는 건드리지 않는다.
        if parent_uid is not None and r["parent_id"] is None and pid is not None:
            self.conn.execute("UPDATE folders SET parent_id=? WHERE id=?", (pid, r["id"]))
            if (r["updated_at"] or 0.0) >= at:
                return True        # 부모만 붙이고 이름·위치·시각은 로컬 것을 지킨다
        if (r["updated_at"] or 0.0) >= at:
            return False
        self.conn.execute("UPDATE folders SET name=?, parent_id=?, position=?, updated_at=? WHERE id=?",
                          (name, pid, position, at, r["id"]))
        return True

    def apply_folder_item(self, folder_uid: str, kind: str, ref: str,
                          alias: str | None, position: float | None, at: float) -> bool:
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        fid = self._folder_id_by_uid(folder_uid)
        if fid is None:
            return False                       # 폴더가 아직 안 왔다 — 다음 회차에 붙는다
        gone = self.conn.execute(
            "SELECT at FROM folder_item_removed WHERE folder_uid=? AND kind=? AND ref=?",
            (folder_uid, kind, ref)).fetchone()
        if gone and (gone["at"] or 0.0) >= at:
            return False
        r = self.conn.execute(
            "SELECT updated_at FROM folder_items WHERE folder_id=? AND kind=? AND ref=?",
            (fid, kind, ref)).fetchone()
        if r is not None and (r["updated_at"] or 0.0) >= at:
            return False
        self.conn.execute(
            "INSERT INTO folder_items(folder_id, kind, ref, added_at, alias, position, updated_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(folder_id, kind, ref) DO UPDATE SET"
            "   alias=excluded.alias, position=excluded.position, updated_at=excluded.updated_at",
            (fid, kind, ref, at, alias, position, at))
        return True

    def apply_folder_removed(self, uid: str, at: float) -> bool:
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        r = self.conn.execute("SELECT id, updated_at FROM folders WHERE uid=?", (uid,)).fetchone()
        self.conn.execute("INSERT INTO folder_removed(uid, at) VALUES(?,?) "
                          "ON CONFLICT(uid) DO UPDATE SET at=MAX(at, excluded.at)", (uid, at))
        if r is None or (r["updated_at"] or 0.0) >= at:
            return False                       # 여기서 더 늦게 고쳤다 — 지우지 않는다
        self.conn.execute("DELETE FROM folder_items WHERE folder_id=?", (r["id"],))
        self.conn.execute("UPDATE folders SET parent_id=NULL WHERE parent_id=?", (r["id"],))
        self.conn.execute("DELETE FROM folders WHERE id=?", (r["id"],))
        return True

    def apply_folder_item_removed(self, folder_uid: str, kind: str, ref: str, at: float) -> bool:
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        self.conn.execute(
            "INSERT INTO folder_item_removed(folder_uid, kind, ref, at) VALUES(?,?,?,?) "
            "ON CONFLICT(folder_uid, kind, ref) DO UPDATE SET at=MAX(at, excluded.at)",
            (folder_uid, kind, ref, at))
        fid = self._folder_id_by_uid(folder_uid)
        if fid is None:
            return False
        r = self.conn.execute(
            "SELECT updated_at FROM folder_items WHERE folder_id=? AND kind=? AND ref=?",
            (fid, kind, ref)).fetchone()
        if r is None or (r["updated_at"] or 0.0) >= at:
            return False
        self.conn.execute("DELETE FROM folder_items WHERE folder_id=? AND kind=? AND ref=?",
                          (fid, kind, ref))
        return True

    # 미래 시각 레코드는 **자르지 않고 버린다.**
    #
    # 기기 하나가 9999999999 같은 값을 보내면 상대의 접힘·제목·폴더를 영구히 고정할 수 있다
    # (실측: 사용자가 펼쳐도 재동기화마다 다시 접혔다). 처음엔 now+하루로 잘랐는데 그게 더
    # 나빴다 — 매 import 마다 클램프가 새로 계산돼 사용자의 조작을 **항상** 이긴다.
    # 미래를 주장하는 기록은 믿을 근거가 없으니 반영하지 않는 쪽이 맞다.
    _CLOCK_SKEW = 86400.0      # 하루. 기기 시계 오차는 이만큼까지 봐준다.

    def _future(self, at: float) -> bool:
        """미래를 주장하는 기록인가. 맞으면 반영하지 않는다.

        **여기까지가 한계다.** 상대가 매 주기 `지금+하루-1초`처럼 신선한 값을 계속 보내면
        검사를 매번 통과하고, 사용자의 조작(`지금`)은 항상 그보다 작아 영원히 진다.
        시각이든 논리 시계든, 값을 스스로 정하는 상대와의 비교로는 못 막는다.

        그래서 이 기능의 전제를 분명히 해둔다 — **동기화 폴더에 쓸 수 있는 기기는 신뢰한다.**
        그 폴더에는 이미 모든 대화가 평문 NDJSON 으로 들어 있어서, 거기에 쓸 수 있는 상대는
        접힘 상태를 조작하는 것보다 훨씬 많은 것을 이미 할 수 있다. 신뢰 경계가 아니다.

        이 검사가 실제로 막는 것은 **시계가 고장난 기기**다(RTC 배터리 방전 등). 흔하고,
        사고이고, 고칠 수 있다. 그래서 조용히 버리지 않고 로그를 남긴다 — 안 그러면
        "동기화가 계속 안 된다"는 증상만 남고 원인을 찾을 길이 없다.
        """
        ahead = float(at) - time.time()
        if ahead <= self._CLOCK_SKEW:
            return False
        logger.warning("미래 시각 기록 거부 — %.0f시간 앞섬. 보낸 기기의 시계를 확인하세요",
                       ahead / 3600)
        return True

    def sync_title_rows(self) -> list[tuple[str, str, float]]:
        """(session_id, title, updated_at). title='' 은 '지웠다'는 기록이라 함께 내보낸다."""
        return [(r["session_id"], r["title"], r["updated_at"] or 0.0)
                for r in self.conn.execute("SELECT session_id, title, updated_at FROM session_titles")]

    def apply_title(self, session_id: str, title: str, at: float) -> bool:
        """상대 기록이 더 새로우면 반영. 반영했으면 True."""
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        r = self.conn.execute(
            "SELECT updated_at FROM session_titles WHERE session_id=?", (session_id,)).fetchone()
        if r is not None and (r["updated_at"] or 0.0) >= at:
            return False
        self.conn.execute(
            "INSERT INTO session_titles(session_id, title, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at",
            (session_id, title, at))
        return True

    def sync_fold_rows(self) -> list[tuple[str, int, float]]:
        """(turn_id, 접힘여부, 시각). 접힘은 hidden_turns, 펼침은 unfolded 에서 온다."""
        rows = [(r["turn_id"], 1, r["hidden_at"] or 0.0)
                for r in self.conn.execute("SELECT turn_id, hidden_at FROM hidden_turns")]
        rows += [(r["turn_id"], 0, r["at"])
                 for r in self.conn.execute("SELECT turn_id, at FROM unfolded")]
        return rows

    def _fold_at(self, turn_id: str) -> float:
        """이 턴의 접힘/펼침 중 마지막 시각(둘 다 없으면 0)."""
        a = self.conn.execute("SELECT hidden_at FROM hidden_turns WHERE turn_id=?", (turn_id,)).fetchone()
        b = self.conn.execute("SELECT at FROM unfolded WHERE turn_id=?", (turn_id,)).fetchone()
        return max((a["hidden_at"] or 0.0) if a else 0.0, (b["at"] or 0.0) if b else 0.0)

    def apply_fold(self, turn_id: str, folded: int, at: float) -> bool:
        """상대 기록이 더 새로우면 반영. 반영했으면 True."""
        if self._future(at):
            return False        # 미래를 주장하는 기록은 반영하지 않는다
        if self._fold_at(turn_id) >= at:
            return False
        if folded:
            self.conn.execute(
                "INSERT INTO hidden_turns(turn_id, hidden_at) VALUES(?,?) "
                "ON CONFLICT(turn_id) DO UPDATE SET hidden_at=excluded.hidden_at", (turn_id, at))
            self.conn.execute("DELETE FROM unfolded WHERE turn_id=?", (turn_id,))
        else:
            self.conn.execute("DELETE FROM hidden_turns WHERE turn_id=?", (turn_id,))
            self.conn.execute(
                "INSERT INTO unfolded(turn_id, at) VALUES(?,?) "
                "ON CONFLICT(turn_id) DO UPDATE SET at=excluded.at", (turn_id, at))
        return True

    def session_title(self, session_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT title FROM session_titles WHERE session_id=? AND title<>''",
            (session_id,)).fetchone()
        return r["title"] if r else None

    # --- 메타 -----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def commit(self) -> None:
        self.conn.commit()
