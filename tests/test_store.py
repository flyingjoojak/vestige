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


def test_thread_edges_ties_and_session_isolation(tmp_path):
    """윈도우를 SQL 로 잡으므로 파이썬 슬라이싱이 공짜로 해줬던 것들을 직접 확인한다.

    경계 잘림(양끝), timestamp 동률일 때 id 로 갈리는 순서, 다른 세션 턴 미포함.
    """
    db = ArchiveDB(tmp_path / "a.db")
    for i in range(5):
        db.upsert_turn(_turn(f"s1:u{i}", ts=f"2026-07-24T00:0{i}:00Z"))
    for i in range(3):                       # 같은 시각 - id 로 순서가 갈려야 한다
        db.upsert_turn(_turn(f"s1:t{i}", ts="2026-07-24T00:09:00Z"))
    db.upsert_turn(_turn("s2:u0", session="s2", ts="2026-07-24T00:02:00Z"))
    db.commit()

    ids = lambda tid, w: [t.id for t in db.thread(tid, window=w)]   # noqa: E731
    assert ids("s1:u0", 2) == ["s1:u0", "s1:u1", "s1:u2"]          # 앞 경계 - before 없음
    assert ids("s1:t2", 2) == ["s1:t0", "s1:t1", "s1:t2"]          # 뒤 경계 - after 자신뿐
    assert ids("s1:t1", 1) == ["s1:t0", "s1:t1", "s1:t2"]          # 동률 - id 로 갈림
    assert ids("s2:u0", 2) == ["s2:u0"]                            # 다른 세션 턴 안 섞임
    assert ids("s1:u2", 2) == ["s1:u0", "s1:u1", "s1:u2", "s1:u3", "s1:u4"]
    assert db.thread("s1:없는턴") == []


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


def test_folder_session_item_uses_custom_session_title(tmp_path):
    """폴더에 담긴 세션도 사용자가 지은 제목을 따른다(/api/sessions 와 같은 기준).
    안 보면 제목을 바꿔도 폴더 화면에만 옛 제목이 남는다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="첫 질문")); db.commit()
    f = db.create_folder("모음")
    db.add_to_folder(f, "session", "s1")

    it = db.folder_items(f)[0]
    assert it["headline"] == "첫 질문"          # 기본: 첫 턴에서 뽑은 제목

    db.set_session_title("s1", "내가 지은 제목")
    it = db.folder_items(f)[0]
    assert it["headline"] == "내가 지은 제목"
    assert it["original_headline"] == "내가 지은 제목"

    # 폴더 별칭이 있으면 그게 최우선(원본은 original_headline 으로 유지)
    db.set_item_alias(f, "session", "s1", "폴더용 이름")
    it = db.folder_items(f)[0]
    assert it["headline"] == "폴더용 이름"
    assert it["original_headline"] == "내가 지은 제목"


def test_clear_raw_cursors_only_touches_given_session(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.set_raw_cursor("/x/a.jsonl", 10, "sid-a", "claude-code")
    db.set_raw_cursor("/x/b.jsonl", 20, "sid-b", "claude-code")
    db.set_raw_cursor("/x/c.jsonl", 30, "sid-a", "codex")   # 같은 sid, 다른 소스
    db.commit()

    assert db.clear_raw_cursors("claude-code", ["sid-a"]) == 1
    assert db.get_raw_cursor("/x/a.jsonl") == 0
    assert db.get_raw_cursor("/x/b.jsonl") == 20    # 다른 세션은 그대로
    assert db.get_raw_cursor("/x/c.jsonl") == 30    # 다른 소스도 그대로
    assert db.clear_raw_cursors("claude-code", []) == 0


def test_folder_session_title_is_order_independent(tmp_path):
    """'제목 바꾸고 담기'와 '담고 제목 바꾸기'의 결과가 같아야 한다.

    폴더의 세션 항목은 스냅샷이 아니라 참조라, 담은 시점과 무관하게 현재 제목을 따른다.
    """
    def build(rename_first: bool):
        db = ArchiveDB(tmp_path / f"{rename_first}.db")
        db.upsert_turn(_turn("s1:u1", q="원래 첫 질문")); db.commit()
        f = db.create_folder("F")
        if rename_first:
            db.set_session_title("s1", "내가 지은 제목")
            db.add_to_folder(f, "session", "s1")
        else:
            db.add_to_folder(f, "session", "s1")
            db.set_session_title("s1", "내가 지은 제목")
        return db.folder_items(f)[0]

    a, b = build(True), build(False)
    assert a["headline"] == b["headline"] == "내가 지은 제목"
    assert a["original_headline"] == b["original_headline"] == "내가 지은 제목"


def test_folder_alias_outranks_session_title_and_both_reset(tmp_path):
    """별칭 > 세션 제목 > 첫 턴 요약 순으로 이기고, 각각 비우면 한 단계씩 되돌아간다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="원래 첫 질문")); db.commit()
    f = db.create_folder("F")
    db.add_to_folder(f, "session", "s1")
    db.set_item_alias(f, "session", "s1", "폴더용 별칭")
    db.set_session_title("s1", "내가 지은 제목")

    it = db.folder_items(f)[0]
    assert it["headline"] == "폴더용 별칭"            # 별칭이 최우선
    assert it["original_headline"] == "내가 지은 제목"  # 별칭을 떼면 보일 이름

    db.set_item_alias(f, "session", "s1", None)
    assert db.folder_items(f)[0]["headline"] == "내가 지은 제목"

    db.set_session_title("s1", None)
    assert db.folder_items(f)[0]["headline"] == "원래 첫 질문"


def test_folder_alias_is_per_folder(tmp_path):
    """같은 세션을 두 폴더에 담고 한쪽에만 별칭을 붙이면, 다른 폴더는 영향받지 않는다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="원래 첫 질문")); db.commit()
    f1, f2 = db.create_folder("F1"), db.create_folder("F2")
    db.add_to_folder(f1, "session", "s1"); db.add_to_folder(f2, "session", "s1")
    db.set_session_title("s1", "공통 제목")
    db.set_item_alias(f1, "session", "s1", "F1 전용")

    assert db.folder_items(f1)[0]["headline"] == "F1 전용"
    assert db.folder_items(f2)[0]["headline"] == "공통 제목"


def test_folder_turn_item_ignores_session_title(tmp_path):
    """턴 항목의 제목은 그 턴의 요약/질문이다 — 세션 제목을 바꿔도 영향받지 않는다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", q="턴 자신의 질문")); db.commit()
    f = db.create_folder("F")
    db.add_to_folder(f, "turn", "s1:u1")
    db.set_session_title("s1", "내가 지은 세션 제목")
    assert db.folder_items(f)[0]["headline"] == "턴 자신의 질문"


def _schema_fingerprint(conn):
    """스키마 전체 지문: 테이블·인덱스 목록 + 각 테이블의 컬럼 정의.

    sqlite_master 의 sql 원문을 그대로 비교하면 주석·공백 차이로 깨지므로,
    PRAGMA 로 읽은 '구조'만 본다.
    """
    out = {}
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in names:
        cols = [(r["name"], (r["type"] or "").upper(), bool(r["notnull"]), r["pk"])
                for r in conn.execute(f"PRAGMA table_info({t})")]
        idx = sorted(
            (r["name"], bool(r["unique"]),
             tuple(c["name"] for c in conn.execute(f"PRAGMA index_info({r['name']})")))
            for r in conn.execute(f"PRAGMA index_list({t})")
        )
        fks = sorted((r["table"], r["from"], r["to"]) for r in conn.execute(f"PRAGMA foreign_key_list({t})"))
        out[t] = {"cols": cols, "idx": idx, "fks": fks}
    return out


def test_migrated_db_schema_matches_fresh_schema(tmp_path):
    """레거시 DB에 마이그레이션을 전부 적용한 결과 == _SCHEMA 로 새로 만든 DB.

    append-only 마이그레이션 패턴에서 실제로 위험한 건 user_version 숫자가 아니라
    "_SCHEMA 에만 컬럼/인덱스를 추가하고 마이그레이션을 빼먹는 것"이다. 그러면 신규 사용자와
    기존 사용자의 DB 모양이 갈리고, 기존 사용자에게서만 터진다. 기존 테스트는 버전 숫자만 봐서
    이걸 못 잡는다.
    """
    import sqlite3
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

    migrated = ArchiveDB(p)                      # v0 → 최신까지 마이그레이션
    fresh = ArchiveDB(tmp_path / "fresh.db")     # _SCHEMA 로 바로 생성

    a, b = _schema_fingerprint(migrated.conn), _schema_fingerprint(fresh.conn)
    assert set(a) == set(b), f"테이블 목록 불일치: 마이그레이션만={set(a)-set(b)}, _SCHEMA만={set(b)-set(a)}"
    for t in sorted(a):
        assert a[t]["cols"] == b[t]["cols"], f"{t} 컬럼 불일치\n  마이그레이션={a[t]['cols']}\n  _SCHEMA={b[t]['cols']}"
        assert a[t]["idx"] == b[t]["idx"], f"{t} 인덱스 불일치\n  마이그레이션={a[t]['idx']}\n  _SCHEMA={b[t]['idx']}"
        assert a[t]["fks"] == b[t]["fks"], f"{t} 외래키 불일치\n  마이그레이션={a[t]['fks']}\n  _SCHEMA={b[t]['fks']}"


def test_folder_items_uses_constant_number_of_queries(tmp_path):
    """항목 수와 무관하게 쿼리는 2회(종류별 1회). 항목마다 돌면 100개 폴더가 300 왕복이 된다."""
    db = ArchiveDB(tmp_path / "a.db")
    for si in range(20):
        for ti in range(3):
            db.upsert_turn(_turn(f"s{si}:u{ti}", session=f"s{si}",
                                 ts=f"2026-07-{si % 28 + 1:02d}T0{ti}:00:00Z", q=f"질문{si}-{ti}"))
    db.commit()
    f = db.create_folder("big")
    for si in range(20):
        db.add_to_folder(f, "session", f"s{si}")
        for ti in range(3):
            db.add_to_folder(f, "turn", f"s{si}:u{ti}")

    n = [0]
    db.conn.set_trace_callback(lambda _s: n.__setitem__(0, n[0] + 1))
    items = db.folder_items(f)
    db.conn.set_trace_callback(None)

    assert len(items) == 80          # 세션 20 + 턴 60
    assert n[0] == 2


def test_folder_items_keeps_dangling_refs_visible(tmp_path):
    """없는 턴·세션을 가리키는 항목도 목록에서 빠지지 않는다(빼면 지울 방법이 사라진다)."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    f = db.create_folder("F")
    db.add_to_folder(f, "session", "없는세션")
    db.add_to_folder(f, "turn", "없는턴")

    by = {(i["kind"], i["ref"]): i for i in db.folder_items(f)}
    assert len(by) == 2
    ghost_s = by[("session", "없는세션")]
    assert ghost_s["count"] == 0 and ghost_s["headline"] == "" and ghost_s["hidden"] is False
    assert ghost_s["timestamp"] is None
    ghost_t = by[("turn", "없는턴")]
    assert ghost_t["headline"] == "" and ghost_t["session_id"] is None


def test_folder_session_headline_prefers_unfolded_turn(tmp_path):
    """접은 첫 턴이 계속 제목으로 뜨면 접은 의미가 없다 — /api/sessions 와 같은 기준."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1", ts="2026-07-24T00:00:00Z", q="접을 첫 질문"))
    db.upsert_turn(_turn("s1:u2", ts="2026-07-24T01:00:00Z", q="두 번째 질문"))
    db.commit()
    f = db.create_folder("F")
    db.add_to_folder(f, "session", "s1")
    assert db.folder_items(f)[0]["headline"] == "접을 첫 질문"

    db.hide_turns(["s1:u1"])
    it = db.folder_items(f)[0]
    assert it["headline"] == "두 번째 질문"   # 접힌 턴은 제목 후보에서 밀린다
    assert it["hidden"] is False              # 전부 접힌 게 아니라 세션은 '접힘' 아님

    db.hide_turns(["s1:u2"])
    it = db.folder_items(f)[0]
    assert it["hidden"] is True               # 전 턴이 접히면 세션도 접힘
    assert it["headline"] == "접을 첫 질문"    # 다 접혔으면 그중 첫 턴(대안 없음)


def test_reorder_folder_pushes_unlisted_items_behind(tmp_path):
    """주어진 목록에 없는 항목은 뒤로 밀려 position 이 충돌하지 않아야 한다.

    예전엔 주어진 것에만 1..n 을 매겨 남은 항목과 같은 번호가 생겼고, 정렬이 added_at
    타이브레이크 운에 맡겨졌다(docstring 은 '건드리지 않아 뒤에 남는다'고 약속했지만 아니었다).
    """
    db = ArchiveDB(tmp_path / "a.db")
    for i in range(4):
        db.upsert_turn(_turn(f"s1:u{i}"))
    db.commit()
    f = db.create_folder("F")
    for i in range(4):
        db.add_to_folder(f, "turn", f"s1:u{i}")

    # 뒤 두 개만 순서를 뒤집어 보낸다(앞 두 개는 목록에 없음)
    changed = db.reorder_folder(f, [("turn", "s1:u3"), ("turn", "s1:u2")])
    assert changed == 2

    items = db.folder_items(f)
    assert [i["ref"] for i in items] == ["s1:u3", "s1:u2", "s1:u0", "s1:u1"]
    positions = [i["position"] for i in items]
    assert positions == sorted(positions) and len(set(positions)) == 4   # 충돌 없음


def test_reorder_folder_reports_zero_when_items_gone(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    f = db.create_folder("F")
    assert db.reorder_folder(f, [("turn", "없는턴")]) == 0


def test_remove_from_folder_reports_rowcount(tmp_path):
    """0을 돌려줘야 '눌렀는데 아무 일도 안 일어남'을 화면이 구분할 수 있다."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("s1:u1")); db.commit()
    f = db.create_folder("F")
    db.add_to_folder(f, "turn", "s1:u1")
    assert db.remove_from_folder(f, "turn", "s1:u1") == 1
    assert db.remove_from_folder(f, "turn", "s1:u1") == 0      # 이미 없음
    assert db.remove_from_folder(f, "session", "s1") == 0      # 틀린 kind
