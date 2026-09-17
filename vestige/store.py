"""아카이브(SQLite) = 진실원본. 턴 원문·행동·정제본·청크메타·커서·메타.

멱등: 턴/청크는 id 기준 INSERT OR REPLACE. 커서는 저장 성공 후에만 전진.
"""

from __future__ import annotations

import json
import logging
import re
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
  summary TEXT, tags TEXT, source TEXT, source_file TEXT
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
  file_path TEXT PRIMARY KEY, mirrored_offset INTEGER, session_id TEXT, source TEXT, updated_at REAL
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
  position REAL             -- 같은 부모 안에서의 순서(작을수록 위)
);
CREATE TABLE IF NOT EXISTS folder_items(
  folder_id INTEGER NOT NULL REFERENCES folders(id),
  kind TEXT NOT NULL,      -- 'turn' | 'session'
  ref TEXT NOT NULL,       -- turn id 또는 session id
  added_at REAL,
  alias TEXT,              -- 이 폴더에서만 쓰는 표시 이름(원본 제목은 그대로)
  position REAL,           -- 폴더 안 정렬 순서(작을수록 위)
  PRIMARY KEY(folder_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(timestamp);
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
    )


class ArchiveDB:
    def __init__(self, path: str | Path | None = None):
        path = Path(path) if path is not None else DB_PATH   # 호출 시점에 DB_PATH 조회(설정/테스트 반영)
        path.parent.mkdir(parents=True, exist_ok=True)
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
    def turn_content_len(self, turn_id: str) -> int | None:
        """저장된 턴의 내용 길이(질문+답변+행동 JSON 문자수). 없으면 None. 완성도 비교용."""
        row = self.conn.execute(
            "SELECT length(coalesce(question,''))+length(coalesce(answer,''))"
            "+length(coalesce(actions,'')) AS n FROM turns WHERE id=?", (turn_id,),
        ).fetchone()
        return None if row is None else int(row["n"])

    def upsert_turn(self, turn: Turn, source: str = "claude-code",
                    source_file: str | None = None) -> bool:
        """턴 저장(멱등). **완성도 축소 금지**: 이미 저장된 턴이 더 완성(질문+답변+행동 길이가
        더 큼)이면 더 짧은 재파싱본으로 덮지 않고 그대로 둔다. 반환값 = 실제로 기록됐으면 True,
        기존을 유지(스킵)했으면 False. (긴 도구호출로 짧게 확정된 턴을 kill/재색인/기기병합이
        되돌리는 것 방지. 대화 로그는 append-only 라 '줄지 않는다'가 안전한 불변식.)"""
        actions_json = _actions_to_json(turn.actions)
        row = self.conn.execute(
            "SELECT length(coalesce(question,''))+length(coalesce(answer,''))"
            "+length(coalesce(actions,'')) AS n FROM turns WHERE id=?", (turn.id,),
        ).fetchone()
        if row is not None:
            new_n = len(turn.question or "") + len(turn.answer or "") + len(actions_json)
            if row["n"] > new_n:
                return False   # 기존이 더 완성 → 유지
        self.conn.execute(
            """INSERT INTO turns(id,session_id,uuid,parent_uuid,timestamp,project,
                 question,answer,actions,source,source_file)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 question=excluded.question, answer=excluded.answer, actions=excluded.actions,
                 source=excluded.source, source_file=excluded.source_file""",
            (turn.id, turn.session_id, turn.uuid, turn.parent_uuid, turn.timestamp,
             turn.project, turn.question, turn.answer, actions_json,
             source, source_file),
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
        self.conn.executemany(
            "DELETE FROM hidden_turns WHERE turn_id=?", [(tid,) for tid in turn_ids])
        self.conn.commit()

    def unhide_session(self, session_id: str) -> None:
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM turns WHERE session_id=?", (session_id,))]
        self.unhide_turns(ids)

    def hidden_turn_ids(self) -> set[str]:
        """읽을 때 필터용 전체 숨김 turn id 집합(검색·지도 등에서 공유)."""
        return {r["turn_id"] for r in self.conn.execute("SELECT turn_id FROM hidden_turns")}

    def hidden_count(self) -> int:
        """접힌 턴 수(좌측 메뉴 배지용 — 목록 전체를 실어 나르지 않으려고 따로 둠)."""
        return self.conn.execute("SELECT COUNT(*) c FROM hidden_turns").fetchone()["c"]

    def list_hidden(self, limit: int = 200) -> list[dict]:
        """접힌 턴 모아보기(#128): 최근 접은 순. 검색에서 접으면 어느 세션이었는지 잊기 쉬워
        한곳에서 다시 찾아 펼칠 수 있어야 한다. turns 가 사라진 고아 행은 내용이 None 으로 온다."""
        rows = self.conn.execute(
            "SELECT h.turn_id, h.hidden_at, t.session_id, t.question, t.summary, t.timestamp "
            "FROM hidden_turns h LEFT JOIN turns t ON t.id = h.turn_id "
            "ORDER BY h.hidden_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{
            "turn_id": r["turn_id"], "session_id": r["session_id"],
            "headline": r["summary"] or r["question"] or "",
            "timestamp": r["timestamp"], "hidden_at": r["hidden_at"],
        } for r in rows]


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
        """같은 세션에서 시간순 앞뒤 window 개 턴을 포함해 반환."""
        turn = self.get_turn(turn_id)
        if not turn:
            return []
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY timestamp, id", (turn.session_id,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if turn_id not in ids:
            return [turn]
        i = ids.index(turn_id)
        lo, hi = max(0, i - window), min(len(rows), i + window + 1)
        return [_row_to_turn(r) for r in rows[lo:hi]]

    # --- 청크 -----------------------------------------------------------
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
        self.conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
        self.conn.commit()

    def move_folder(self, folder_id: int, parent_id: int | None, before_id: int | None = None) -> bool:
        """폴더를 parent_id 밑으로 옮긴다. 자기 자신/자기 하위로는 못 옮긴다(순환 방지).

        before_id 를 주면 그 형제 '바로 앞'에, 없으면 맨 뒤에 놓는다 — 부모(뎁스)만이 아니라
        형제 사이 순서까지 한 번의 드래그로 정하기 위한 것. 옮긴 뒤 그 부모의 형제들에게
        1,2,3… 을 다시 매겨(NULL 이었던 기존 폴더 포함) 순서를 확정한다.
        """
        if parent_id is not None and parent_id in self.folder_descendants(folder_id):
            return False
        self.conn.execute("UPDATE folders SET parent_id=? WHERE id=?", (parent_id, folder_id))

        sibs = [r["id"] for r in self.conn.execute(
            "SELECT id FROM folders WHERE parent_id IS ? AND id<>? "
            "ORDER BY (position IS NULL), position, name, id", (parent_id, folder_id))]
        at = sibs.index(before_id) if before_id in sibs else len(sibs)
        sibs.insert(at, folder_id)
        self.conn.executemany("UPDATE folders SET position=? WHERE id=?",
                              [(i, fid) for i, fid in enumerate(sibs, start=1)])
        self.conn.commit()
        return True

    def delete_folder(self, folder_id: int) -> int:
        """폴더와 그 하위 폴더를 통째로 삭제. 담긴 항목의 '참조'만 지우며 원문·턴은 그대로.
        반환: 삭제된 폴더 수. (폴더 수는 적어 한 건씩 지워도 충분 — SQL 조립을 피한다)"""
        ids = [(i,) for i in self.folder_descendants(folder_id)]
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
            "INSERT OR IGNORE INTO folder_items(folder_id, kind, ref, added_at, position) VALUES(?,?,?,?,?)",
            (folder_id, kind, ref, time.time(), nxt))
        self.conn.commit()

    def set_item_alias(self, folder_id: int, kind: str, ref: str, alias: str | None) -> None:
        """이 폴더에서만 쓸 표시 이름. 원본 턴/세션 제목은 건드리지 않는다(빈 값이면 원래 제목으로)."""
        self.conn.execute(
            "UPDATE folder_items SET alias=? WHERE folder_id=? AND kind=? AND ref=?",
            (alias or None, folder_id, kind, ref))
        self.conn.commit()

    def reorder_folder(self, folder_id: int, order: list[tuple[str, str]]) -> None:
        """폴더 안 항목 순서를 통째로 다시 매긴다. order = [(kind, ref), …] 화면에 보이는 순서.
        목록에 없는 항목(그 사이 다른 창에서 추가된 것 등)은 건드리지 않아 뒤쪽에 남는다."""
        self.conn.executemany(
            "UPDATE folder_items SET position=? WHERE folder_id=? AND kind=? AND ref=?",
            [(i, folder_id, kind, ref) for i, (kind, ref) in enumerate(order, start=1)])
        self.conn.commit()

    def remove_from_folder(self, folder_id: int, kind: str, ref: str) -> None:
        self.conn.execute(
            "DELETE FROM folder_items WHERE folder_id=? AND kind=? AND ref=?", (folder_id, kind, ref))
        self.conn.commit()

    def folder_items(self, folder_id: int) -> list[dict]:
        """폴더에 '직접' 담긴 항목(하위 폴더 제외). 표시용 헤드라인을 붙여 돌려준다."""
        rows = self.conn.execute(
            # 사용자가 정한 순서(position) 우선, 아직 없으면 담은 순. NULL 은 뒤로.
            "SELECT kind, ref, added_at, alias, position FROM folder_items WHERE folder_id=? "
            "ORDER BY (position IS NULL), position, added_at",
            (folder_id,)).fetchall()
        out = []
        for r in rows:
            item = {"kind": r["kind"], "ref": r["ref"], "added_at": r["added_at"],
                    "alias": r["alias"], "position": r["position"]}
            if r["kind"] == "turn":
                t = self.conn.execute(
                    "SELECT session_id, summary, question, timestamp FROM turns WHERE id=?",
                    (r["ref"],)).fetchone()
                folded = self.conn.execute(
                    "SELECT 1 FROM hidden_turns WHERE turn_id=?", (r["ref"],)).fetchone() is not None
                item |= {"session_id": t["session_id"] if t else None,
                         "headline": ((t["summary"] or t["question"]) if t else "") or "",
                         "timestamp": t["timestamp"] if t else None,
                         "hidden": folded}   # 접힘(#128) — 폴더에서도 접기/펼치기 하도록
            else:   # session — 담긴 건 참조뿐이라 현재 기준으로 개수·대표 제목을 매번 계산
                t = self.conn.execute(
                    "SELECT COUNT(*) n, MIN(timestamp) started, MAX(timestamp) ended FROM turns "
                    "WHERE session_id=?", (r["ref"],)).fetchone()
                head = self.conn.execute(
                    "SELECT summary, question FROM turns WHERE session_id=? ORDER BY timestamp, id LIMIT 1",
                    (r["ref"],)).fetchone()
                n_hidden = self.conn.execute(
                    "SELECT COUNT(*) c FROM turns t JOIN hidden_turns h ON h.turn_id = t.id "
                    "WHERE t.session_id=?", (r["ref"],)).fetchone()["c"]
                n = t["n"] if t else 0
                # 사용자가 지은 세션 제목이 있으면 그게 우선(/api/sessions 와 같은 기준).
                # 안 보면 제목을 바꿔도 폴더 화면에만 옛 제목이 남는다.
                custom = self.session_title(r["ref"])
                item |= {"session_id": r["ref"], "count": n,
                         "headline": custom or (((head["summary"] or head["question"]) if head else "") or ""),
                         "timestamp": t["ended"] if t else None,
                         # 세션은 전 턴이 접혔을 때만 '접힘'(세션 목록과 같은 기준)
                         "hidden": n > 0 and n_hidden == n}
            # 폴더에서 붙인 이름이 있으면 그걸 제목으로(원본은 original_headline 으로 함께 내려줌).
            item["original_headline"] = item["headline"]
            if r["alias"]:
                item["headline"] = r["alias"]
            out.append(item)
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
            self.conn.execute("DELETE FROM session_titles WHERE session_id=?", (session_id,))
        self.conn.commit()

    def session_title(self, session_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT title FROM session_titles WHERE session_id=?", (session_id,)).fetchone()
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
