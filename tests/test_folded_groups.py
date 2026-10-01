"""접힘 화면: 통째로 접은 세션 / 일부 접힌 세션의 채팅을 세션별로 묶는다(#128 개편).

예전엔 접힌 턴을 하나씩 최근순 200개까지 늘어놨다 - 500턴 세션을 접으면 500줄이 됐고 200개를
넘는 부분은 아예 안 보였다.
"""
from vestige import web
from vestige.models import Turn
from vestige.store import ArchiveDB


def _add(db, sid, n, *, start=0):
    for i in range(start, start + n):
        db.upsert_turn(Turn(id=f"{sid}:u{i:04d}", session_id=sid, uuid=f"u{i:04d}", parent_uuid=None,
                            timestamp=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}Z", project="/p",
                            question=f"{sid} 질문 {i}", answer="답", actions=()), source_file=None)


def _db(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    _add(db, "full", 3)          # 통째로 접는다
    _add(db, "part", 5)          # 2개만 접는다
    _add(db, "none", 4)          # 안 접는다
    db.commit()
    db.hide_session("full")
    db.hide_turns(["part:u0001", "part:u0003"])
    db.commit()
    return db


def test_full_and_partial_folds_are_separated_and_grouped(tmp_path):
    g = _db(tmp_path).folded_groups()

    assert [s["session_id"] for s in g["sessions"]] == ["full"]
    assert g["sessions"][0]["total"] == 3 and g["sessions"][0]["last_turn_id"] == "full:u0002"

    assert [c["session_id"] for c in g["chats"]] == ["part"]
    c = g["chats"][0]
    assert (c["folded"], c["total"]) == (2, 5)
    assert [t["turn_id"] for t in c["turns"]] == ["part:u0001", "part:u0003"]   # 시간순, 그 세션 것만

    assert g["count"] == 1 + 2, "배지는 화면에 보이는 단위(접은 세션 1 + 접힌 채팅 2)여야 한다"


def test_big_folded_session_is_one_row_not_hundreds(tmp_path):
    """500턴 세션을 접어도 한 줄. 예전엔 200개까지만 보였고 나머지는 사라졌다."""
    db = ArchiveDB(tmp_path / "a.db")
    _add(db, "big", 500)
    db.commit()
    db.hide_session("big")
    db.commit()

    g = db.folded_groups()
    assert len(g["sessions"]) == 1 and g["sessions"][0]["total"] == 500
    assert g["chats"] == [] and g["count"] == 1


def test_custom_title_is_used_as_headline(tmp_path):
    db = _db(tmp_path)
    db.set_session_title("full", "내가 지은 제목")
    db.commit()
    assert db.folded_groups()["sessions"][0]["headline"] == "내가 지은 제목"


def test_endpoint_count_only_mode(tmp_path, monkeypatch):
    """좌측 배지는 limit=0 으로 개수만 받는다(목록을 실어 나르지 않는다)."""
    _db(tmp_path)
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    r = web.api_hidden(limit=0)
    assert r["count"] == 3 and r["sessions"] == [] and r["chats"] == []
    full = web.api_hidden()
    assert len(full["sessions"]) == 1 and len(full["chats"]) == 1


def test_session_list_headline_skips_folded_turns(tmp_path, monkeypatch):
    """세션 목록의 대표 제목은 접히지 않은 턴에서 먼저 고르고, 전부 접힌 세션만 접힌 턴에서 고른다.

    /api/sessions 쿼리를 윈도우 함수에서 GROUP BY + 대표 턴 서브쿼리로 바꾸면서 이 규칙을 고정한다
    (노이즈라 접은 첫 턴이 세션 제목으로 계속 뜨면 접은 의미가 없다).
    """
    db = _db(tmp_path)                      # full: 전부 접힘, part: u1·u3 접힘, none: 안 접힘
    db.hide_turns(["none:u0000"])           # 첫 턴만 접으면 제목은 두 번째 턴에서
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    rows = {r["session"]: r for r in web.api_sessions()["sessions"]}
    assert rows["none"]["headline"] == "none 질문 1", "접은 첫 턴이 제목으로 떴다"
    assert rows["part"]["headline"] == "part 질문 0"
    assert rows["full"]["headline"] == "full 질문 0", "전부 접힌 세션은 접힌 턴에서라도 제목을"
    assert (rows["full"]["count"], rows["full"]["hidden_count"]) == (3, 3)
    assert (rows["part"]["count"], rows["part"]["hidden_count"]) == (5, 2)
