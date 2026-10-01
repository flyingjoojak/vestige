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
