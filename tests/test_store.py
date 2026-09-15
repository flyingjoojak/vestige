"""아카이브(SQLite) + 벡터 인덱스 단위 테스트."""

from __future__ import annotations

import numpy as np

from vestige.chunker import Chunk
from vestige.models import Action, Turn
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex


def _turn(tid, session="s1", ts="2026-07-24T00:00:00Z", q="질문", a="답변", actions=()):
    return Turn(id=tid, session_id=session, uuid=tid.split(":")[-1], parent_uuid=None,
                timestamp=ts, project="p", question=q, answer=a, actions=actions)


def test_delete_turns_removes_turn_chunks_fts(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="포트 8088 설정", a="백엔드"))
    db.upsert_turn(_turn("s1:u2", q="남길 질문", a="남길 답변"))
    db.add_chunks([Chunk(turn_id="s1:u1", index=0, text="포트 8088 설정")])
    db.commit()
    removed = db.delete_turns(["s1:u1"])
    db.commit()
    assert removed == ["s1:u1#0"]                       # 벡터 정리용 청크키 반환
    assert db.get_turn("s1:u1") is None
    assert db.get_turn("s1:u2") is not None
    assert db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE turn_id='s1:u1'").fetchone()["c"] == 0
    # FTS에서도 사라짐
    assert db.keyword_search("8088") == []


def test_upsert_turn_never_shrinks_content(tmp_path):
    """완성도 축소 금지(#151): 더 짧은 재파싱본은 기존 완성 턴을 덮지 않는다. 반환 bool로 구분."""
    db = ArchiveDB(tmp_path / "a.db")
    full = "도구 실행이 끝난 뒤의 완성된 긴 답변입니다 상세 내용 " * 3
    # 1) 완성 턴 저장 → 기록됨(True)
    assert db.upsert_turn(_turn("s1:u1", q="빌드 고쳐줘", a=full)) is True
    # 2) 더 짧은 재파싱본(같은 id) → 유지(False), 내용 안 바뀜
    assert db.upsert_turn(_turn("s1:u1", q="빌드 고쳐줘", a="짧음")) is False
    assert db.get_turn("s1:u1").answer == full
    # 3) 더 긴 내용 → 갱신(True)
    longer = full + " 추가로 붙은 뒷내용"
    assert db.upsert_turn(_turn("s1:u1", q="빌드 고쳐줘", a=longer)) is True
    db.commit()
    assert db.get_turn("s1:u1").answer == longer
    assert db.keyword_search("뒷내용") != []          # FTS도 완성본과 일치


def test_vectorindex_remove(tmp_path):
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    vi.add(["a#0", "b#0", "c#0"], np.eye(3, dtype=np.float32))
    assert len(vi) == 3
    n = vi.remove(["b#0", "zzz#0"])                      # 없는 키는 무시
    assert n == 1
    assert len(vi) == 2
    assert [k for k, _ in vi.search(np.array([1, 0, 0], dtype=np.float32), k=3)]  # 검색 정상
    assert "b#0" not in vi.ids


def test_turn_upsert_idempotent(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    t = _turn("s1:u1", actions=(Action("Edit", "x.py"),))
    db.upsert_turn(t)
    db.upsert_turn(t)  # 두 번 넣어도 하나
    db.commit()
    got = db.get_turn("s1:u1")
    assert got is not None
    assert got.question == "질문"
    assert got.actions[0].tool == "Edit"
    n = db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
    assert n == 1


def test_cursor_roundtrip(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    assert db.get_cursor("f.jsonl") == (0, 0, 0.0)
    db.set_cursor("f.jsonl", 128, 200, 1234.5)
    db.commit()
    assert db.get_cursor("f.jsonl") == (128, 200, 1234.5)


def test_enrichment_additive(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1"))
    db.set_enrichment("s1:u1", "요약본", ["태그1", "태그2"])
    db.commit()
    summary, tags = db.get_enrichment("s1:u1")
    assert summary == "요약본"
    assert tags == ["태그1", "태그2"]
    # 원문은 그대로(대체 안 함).
    assert db.get_turn("s1:u1").question == "질문"


def test_fresh_db_stamped_at_latest_schema_version(tmp_path):
    from vestige.store import _SCHEMA_VERSION
    db = ArchiveDB(tmp_path / "a.db")
    ver = db.conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == _SCHEMA_VERSION   # 신규 DB는 곧장 최신으로 스탬프(마이그레이션 재적용 안 함)


def test_migrates_legacy_v0_db_up_to_latest(tmp_path):
    import sqlite3

    from vestige.store import _SCHEMA_VERSION
    # 마이그레이션 시스템 이전의 '구' DB 흉내: turns/cursors 를 신규 컬럼 없이, user_version=0 으로 생성.
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE turns(id TEXT PRIMARY KEY, session_id TEXT, uuid TEXT, parent_uuid TEXT,"
        " timestamp TEXT, project TEXT, question TEXT, answer TEXT, actions TEXT, summary TEXT, tags TEXT);"
        "CREATE TABLE chunks(chunk_key TEXT PRIMARY KEY, turn_id TEXT, idx INTEGER, text TEXT);"
        "CREATE TABLE cursors(file_path TEXT PRIMARY KEY, offset INTEGER, size INTEGER, mtime REAL, updated_at REAL);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
    )
    con.commit(); con.close()

    db = ArchiveDB(p)   # 열기만 해도 v0 → 최신으로 끌어올려야 한다
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
    tcols = {r["name"] for r in db.conn.execute("PRAGMA table_info(turns)")}
    assert {"source", "source_file"} <= tcols
    ccols = {r["name"] for r in db.conn.execute("PRAGMA table_info(cursors)")}
    assert "hold_offset" in ccols
    # 업그레이드 후에도 정상 동작(upsert/조회).
    db.upsert_turn(_turn("s1:u1")); db.commit()
    assert db.get_turn("s1:u1").question == "질문"


def test_reopen_is_noop_idempotent(tmp_path):
    from vestige.store import _SCHEMA_VERSION
    p = tmp_path / "a.db"
    ArchiveDB(p).commit()
    db = ArchiveDB(p)   # 두 번째 열기 = 이미 최신 → 마이그레이션 재적용 없이 그대로
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION


def test_set_enrichment_returns_rowcount(tmp_path):
    # 매칭되는 turn이 있으면 1, 없으면(예: LLM이 id를 잘못 복사) 0을 돌려줘야
    # enrich 루프가 "완료"로 오인하지 않는다 → summary IS NULL 무한 재시도 방지.
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1"))
    db.commit()
    assert db.set_enrichment("s1:u1", "요약", ["t"]) == 1
    assert db.set_enrichment("s1:does-not-exist", "요약", ["t"]) == 0


def test_thread_window(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    for i in range(5):
        db.upsert_turn(_turn(f"s1:u{i}", ts=f"2026-07-24T00:0{i}:00Z"))
    db.commit()
    thread = db.thread("s1:u2", window=1)
    assert [t.id for t in thread] == ["s1:u1", "s1:u2", "s1:u3"]


def test_chunk_mapping(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.add_chunks([Chunk("s1:u1", 0, "텍스트")])
    db.commit()
    assert db.turn_id_of_chunk("s1:u1#0") == "s1:u1"


# --- 벡터 인덱스 --------------------------------------------------------
def test_vectorindex_add_search_persist(tmp_path):
    vpath, ipath = tmp_path / "v.npy", tmp_path / "ids.json"
    vi = VectorIndex(vpath, ipath)
    m = np.array([[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0]], dtype=np.float32)
    vi.add(["a", "b", "c"], m)
    res = vi.search(np.array([1, 0, 0], dtype=np.float32), k=2)
    assert res[0][0] == "a"
    assert res[1][0] == "c"  # 가장 가까운 순
    vi.save()

    reopened = VectorIndex(vpath, ipath)
    assert len(reopened) == 3
    assert reopened.search(np.array([0, 1, 0], dtype=np.float32), k=1)[0][0] == "b"


def test_vectorindex_replace_existing_key(tmp_path):
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    vi.add(["a"], np.array([[1, 0]], dtype=np.float32))
    vi.add(["a"], np.array([[0, 1]], dtype=np.float32))  # 같은 키 → 교체
    assert len(vi) == 1
    assert vi.search(np.array([0, 1], dtype=np.float32), k=1)[0][0] == "a"


def test_reconcile_removes_orphan_vectors(tmp_path):
    from vestige.indexer import reconcile
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    # 턴 2개 + 각 벡터. 이후 한 턴만 turns에서 삭제해 고아 생성.
    db.upsert_turn(_turn("s1:u1")); db.upsert_turn(_turn("s1:u2")); db.commit()
    vi.add(["s1:u1#0", "s1:u2#0"], np.eye(2, 3, dtype=np.float32)); vi.save()
    db.conn.execute("DELETE FROM turns WHERE id='s1:u2'"); db.commit()  # 원문만 사라진 고아
    n = reconcile(db, vi, log_fn=lambda m: None)
    assert n == 1
    assert vi.keys() == ["s1:u1#0"]
    # 정리할 게 없으면 0
    assert reconcile(db, vi, log_fn=lambda m: None) == 0


# --- 숨김(#128) ----------------------------------------------------------
def test_hide_unhide_turns(tmp_path):
    """턴 단위 숨김/복원 - 비파괴(turns 는 그대로, hidden_turn_ids 에만 반영)."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.upsert_turn(_turn("s1:u2")); db.commit()

    assert db.hidden_turn_ids() == set()
    db.hide_turns(["s1:u1"])
    assert db.hidden_turn_ids() == {"s1:u1"}
    assert db.get_turn("s1:u1") is not None   # 원문은 그대로

    db.unhide_turns(["s1:u1"])
    assert db.hidden_turn_ids() == set()


def test_hide_session_hides_all_its_turns(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", session="s1")); db.upsert_turn(_turn("s1:u2", session="s1"))
    db.upsert_turn(_turn("s2:u1", session="s2")); db.commit()

    n = db.hide_session("s1")
    assert n == 2
    assert db.hidden_turn_ids() == {"s1:u1", "s1:u2"}

    db.unhide_session("s1")
    assert db.hidden_turn_ids() == set()


def test_hide_turns_idempotent_and_empty_list(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    assert db.hide_turns(["s1:u1"]) == 1
    assert db.hide_turns(["s1:u1"]) == 0   # 재숨김 - 실제로 새로 숨겨진 건 0개(정확한 카운트)
    assert db.hidden_turn_ids() == {"s1:u1"}
    assert db.hide_turns([]) == 0   # 빈 목록 no-op


def test_hidden_turns_survive_reupsert(tmp_path):
    """접힘 상태는 turns 재기록(재색인)과 무관한 별도 테이블 — 다시 색인돼도 접힌 채로 남는다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="첫질문")); db.commit()
    db.hide_turns(["s1:u1"])

    db.upsert_turn(_turn("s1:u1", q="첫질문 더 길어진 재파싱본")); db.commit()
    assert db.hidden_turn_ids() == {"s1:u1"}


# --- 폴더(#201) ----------------------------------------------------------
def test_folder_create_nest_and_move_rejects_cycle(tmp_path):
    """중첩 폴더: 자기 자신/자기 하위로 옮기는 건 거부해야 트리가 순환하지 않는다."""
    db = ArchiveDB(tmp_path / "a.db")
    root = db.create_folder("Vestige")
    child = db.create_folder("배포 삽질", parent_id=root)
    grand = db.create_folder("gzip", parent_id=child)
    other = db.create_folder("회사 일")

    assert set(db.folder_descendants(root)) == {root, child, grand}
    assert db.move_folder(root, grand) is False                 # 자기 하위(손자)로는 거부
    assert db.move_folder(root, root) is False                  # 자기 자신도 거부
    assert db.get_folder(root)["parent_id"] is None             # 거부됐으니 그대로
    assert db.move_folder(child, other) is True                 # 다른 가지로 이동은 OK
    assert db.get_folder(child)["parent_id"] == other
    assert set(db.folder_descendants(root)) == {root}            # 손자까지 같이 따라갔다


def test_folder_turn_ids_expands_session_dynamically(tmp_path):
    """세션은 참조만 담기므로, 담은 뒤 그 대화가 이어져도 새 턴이 자동 포함된다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", session="s1")); db.upsert_turn(_turn("s2:u1", session="s2"))
    db.commit()
    f = db.create_folder("모음")
    db.add_to_folder(f, "session", "s1")
    db.add_to_folder(f, "turn", "s2:u1")
    assert db.folder_turn_ids(f) == {"s1:u1", "s2:u1"}

    db.upsert_turn(_turn("s1:u2", session="s1")); db.commit()   # 세션이 이어짐
    assert db.folder_turn_ids(f) == {"s1:u1", "s1:u2", "s2:u1"}


def test_folder_turn_ids_includes_descendants(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.upsert_turn(_turn("s2:u1", session="s2")); db.commit()
    parent = db.create_folder("부모")
    child = db.create_folder("자식", parent_id=parent)
    db.add_to_folder(parent, "turn", "s1:u1")
    db.add_to_folder(child, "turn", "s2:u1")

    assert db.folder_turn_ids(parent) == {"s1:u1", "s2:u1"}                      # 하위 포함
    assert db.folder_turn_ids(parent, include_descendants=False) == {"s1:u1"}    # 직접만


def test_delete_folder_removes_subtree_but_keeps_turns(tmp_path):
    """폴더 삭제는 '참조'만 지운다 - 대화 원문은 그대로(비파괴)."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    parent = db.create_folder("부모")
    child = db.create_folder("자식", parent_id=parent)
    db.add_to_folder(child, "turn", "s1:u1")

    assert db.delete_folder(parent) == 2          # 부모 + 자식
    assert db.list_folders() == []
    assert db.get_turn("s1:u1") is not None       # 턴은 살아있다


def test_add_to_folder_idempotent_and_multi_folder(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    a, b = db.create_folder("A"), db.create_folder("B")
    db.add_to_folder(a, "turn", "s1:u1")
    db.add_to_folder(a, "turn", "s1:u1")          # 재추가 - 에러 없이 그대로
    db.add_to_folder(b, "turn", "s1:u1")          # 같은 항목을 여러 폴더에

    assert len(db.folder_items(a)) == 1
    assert sorted(db.folders_of("turn", "s1:u1")) == sorted([a, b])
    db.remove_from_folder(a, "turn", "s1:u1")
    assert db.folders_of("turn", "s1:u1") == [b]
