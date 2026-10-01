"""실시간 표시(#249): 색인 전 로그 꼬리를 채팅에 바로 붙인다. DB 에는 쓰지 않는다."""
import json
import os
import time

from vestige import web
from vestige.indexer import index_file
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex

from tests.test_indexer import FakeEmbedder

SID = "11110000-0000-4000-8000-00000000live"


def _line(o):
    return json.dumps(o, ensure_ascii=False) + "\n"


def _user(text, uuid, ts):
    return _line({"type": "user", "sessionId": SID, "uuid": uuid, "parentUuid": None, "timestamp": ts,
                  "cwd": "/c/p", "message": {"role": "user", "content": text}})


def _asst(text):
    return _line({"type": "assistant", "sessionId": SID,
                  "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def _queued(text, uuid, ts):
    return _line({"type": "attachment", "sessionId": SID, "uuid": uuid, "parentUuid": None, "timestamp": ts,
                  "cwd": "/c/p", "attachment": {"type": "queued_command", "prompt": text,
                                                "origin": {"kind": "human"}}})


def _setup(tmp_path, monkeypatch):
    """첫 턴만 색인된 세션. 색인 직후 로그에 내용이 더 붙는다(작업이 진행 중인 상황)."""
    log = tmp_path / f"{SID}.jsonl"
    log.write_text(_user("갱신해줘.", "u1", "2026-10-01T00:00:00Z") + _asst("요청서를 다시 씁니다."),
                   encoding="utf-8")
    db = ArchiveDB(tmp_path / "a.db")
    index_file(str(log), db, VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"),
               FakeEmbedder(), idle_secs=0)   # idle → 마지막 턴을 보류(hold)로 남긴다
    db.commit()
    with open(log, "a", encoding="utf-8") as f:
        f.write(_asst("요청서 작성 완료.")                                    # 보류된 첫 턴이 이어진다
                + _queued("그리고 테스트 범위 알려줘.", "q1", "2026-10-01T00:01:00Z")
                + _asst("범위는 이렇습니다.")
                + _user("다음 작업 하자.", "u2", "2026-10-01T00:02:00Z")
                + _asst("진행 중..."))
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    return log


def test_session_shows_unindexed_tail(tmp_path, monkeypatch):
    """색인 전 턴이 채팅에 보이고, 보류됐던 첫 턴은 최신 판본으로 바뀐다."""
    _setup(tmp_path, monkeypatch)
    d = web.api_session(id=SID)

    assert [t["id"] for t in d["turns"]] == [f"{SID}:u1", f"{SID}:q1", f"{SID}:u2"]
    head, mid, last = d["turns"]
    assert "요청서 작성 완료." in head["answer"], "보류된 턴의 이어진 내용이 안 보인다"
    assert head["live"] is False, "DB 에 있는 턴은 live 가 아니다(접기·폴더 담기가 돼야 한다)"
    assert mid["live"] is True and mid["queued"] is True
    assert last["live"] is True and last["answer"] == "진행 중..."
    assert d["db_count"] == 1 and d["count"] == 3
    assert d["active"] is True and d["live_skipped"] == 0


def test_live_view_does_not_write_db(tmp_path, monkeypatch):
    """읽기만 한다 - 턴·커서가 그대로여야 검색에 안 나오고 색인기의 진행도 안 꼬인다."""
    log = _setup(tmp_path, monkeypatch)
    db = ArchiveDB(tmp_path / "a.db")
    before = (db.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
              db.get_cursor(str(log)), db.get_hold(str(log)))

    web.api_session(id=SID)
    web.api_session_tail(id=SID)

    db = ArchiveDB(tmp_path / "a.db")
    after = (db.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
             db.get_cursor(str(log)), db.get_hold(str(log)))
    assert before == after


def test_tail_endpoint_sends_only_the_tail(tmp_path, monkeypatch):
    """몇 초마다 부르는 경로 - 세션 전체가 아니라 꼬리만 보낸다."""
    _setup(tmp_path, monkeypatch)
    r = web.api_session_tail(id=SID)
    assert [t["id"] for t in r["turns"]] == [f"{SID}:u1", f"{SID}:q1", f"{SID}:u2"]
    assert r["db_count"] == 1 and r["active"] is True and r["live_skipped"] == 0


def test_huge_tail_is_not_parsed(tmp_path, monkeypatch):
    """색인이 크게 밀려 꼬리가 크면 읽지 않고 크기만 알린다(몇 초마다 수백 MB 파싱 방지)."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "_LIVE_MAX_BYTES", 10)
    d = web.api_session(id=SID)
    assert [t["id"] for t in d["turns"]] == [f"{SID}:u1"]
    assert d["live_skipped"] > 10


def test_quiet_session_is_not_active(tmp_path, monkeypatch):
    """한동안 안 바뀐 세션은 화면이 주기적으로 다시 읽지 않는다."""
    log = _setup(tmp_path, monkeypatch)
    old = time.time() - web._ACTIVE_SECS - 60
    os.utime(log, (old, old))
    assert web.api_session(id=SID)["active"] is False
