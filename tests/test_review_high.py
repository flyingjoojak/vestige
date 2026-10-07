"""v0.4.0 릴리스 전 풀리뷰 HIGH 수정의 회귀 검사."""
import json
import time

from vestige import web
from vestige.indexer import index_file
from vestige.models import Turn
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex

from tests.test_indexer import FakeEmbedder


def _turn(sid, uuid, q="질문", source_file=None):
    return Turn(id=f"{sid}:{uuid}", session_id=sid, uuid=uuid, parent_uuid=None,
                timestamp="2026-10-07T00:00:00Z", project="p", question=q, answer="답", actions=())


def test_onboarding_choice_is_committed_and_loaded(tmp_path, monkeypatch):
    """온보딩에서 고른 모델이 DB 에 남고, 그 모델이 올라간다(예전엔 기록이 되돌려져 큰 기본 모델이 올라갔다)."""
    from vestige import config as C
    db_path = tmp_path / "a.db"
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(db_path))
    monkeypatch.setattr(C, "write_config", lambda updates: None)
    monkeypatch.setattr(C, "EMBED_MODEL", "intfloat/multilingual-e5-large-int8")
    loaded = []

    class Emb:
        def __init__(self, name):
            loaded.append(name)
            self.model_name = name

    import vestige.embedder as E
    monkeypatch.setattr(E, "Embedder", Emb)
    monkeypatch.setitem(web._state, "embedder", None)
    small = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert web.api_onboarding_choose({"model": small})["ok"]
    for _ in range(50):
        if loaded:
            break
        time.sleep(0.05)
    assert ArchiveDB(db_path).get_meta("embed_model") == small
    assert loaded == [small]
    assert C.EMBED_MODEL == small


def test_hide_session_takes_its_subagent_sessions_along(tmp_path, monkeypatch):
    """세션 단위 접기·펼치기는 하위 에이전트 세션까지 - 어느 화면에서 접든 하위가 고아로 튀어나오지 않게."""
    db = ArchiveDB(tmp_path / "a.db")
    parent = "aaaa0000-0000-4000-8000-000000000001"
    db.upsert_turn(_turn(parent, "p1"), source="claude-code", source_file=f"C:\\p\\C--x\\{parent}.jsonl")
    db.upsert_turn(_turn("agent-1", "k1"), source="claude-code",
                   source_file=f"C:\\p\\C--x\\{parent}\\subagents\\agent-1.jsonl")
    db.upsert_turn(_turn("other", "o1"), source="claude-code", source_file="C:\\p\\C--x\\other.jsonl")
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    assert web.api_hide({"session_id": parent})["hidden"] == 2
    assert ArchiveDB(tmp_path / "a.db").hidden_turn_ids() == {f"{parent}:p1", "agent-1:k1"}
    web.api_unhide({"session_id": parent})
    assert ArchiveDB(tmp_path / "a.db").hidden_turn_ids() == set()


def test_hide_unknown_session_is_404_not_silent_ok(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    with pytest.raises(HTTPException) as e:
        web.api_hide({"session_id": "no-such-session"})
    assert e.value.status_code == 404


def test_unindexed_session_can_be_titled_and_resumed(tmp_path, monkeypatch):
    """색인 전 세션에서 제목 바꾸기·열기(재개)가 '세션을 찾을 수 없음'으로 막히지 않는다."""
    from vestige import config as C
    monkeypatch.setattr(C, "PROJECTS_DIR", tmp_path / "projects")
    sid = "33330000-0000-4000-8000-0000000000aa"
    proj = C.PROJECTS_DIR / "C--new"
    proj.mkdir(parents=True)
    rec = lambda o: json.dumps(o, ensure_ascii=False) + "\n"  # noqa: E731
    (proj / f"{sid}.jsonl").write_text(
        rec({"type": "user", "sessionId": sid, "uuid": "u1", "parentUuid": None, "timestamp": "2026-10-07T00:00:00Z",
             "cwd": str(tmp_path), "message": {"role": "user", "content": "새 세션"}})
        + rec({"type": "assistant", "sessionId": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": "답"}]}}),
        encoding="utf-8")
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    web._unindexed_cache.clear()   # 목록을 안 불렀어도(백엔드 재시작 직후) 찾아야 한다

    assert web.api_session_title({"session_id": sid, "title": "내 제목"}) == {"ok": True}
    row = [r for r in web.api_sessions()["sessions"] if r["session"] == sid][0]
    assert row["headline"] == "내 제목" and row["unindexed"] is True

    launched = []
    monkeypatch.setattr(web, "_launch_resume", lambda s, cwd, source="claude-code": launched.append(s))
    monkeypatch.setattr(web, "_is_active", lambda stored: False)
    r = web.api_resume(session=sid, force=True)
    assert r.get("ok") is True and launched == [sid], r


def test_export_failure_reaches_the_status_bar_and_is_throttled(tmp_path, monkeypatch):
    """아카이브 내보내기 실패가 상태바(sync_errors)에 뜨고, 성공한 내보내기는 최소 간격을 지킨다."""
    from vestige import archive_sync
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("디스크 가득 참")

    monkeypatch.setattr(archive_sync, "export_archive", boom)
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    monkeypatch.setattr(web, "make_index", lambda: VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"))
    monkeypatch.setattr("vestige.indexer.has_new_data", lambda db, *a, **k: True)
    monkeypatch.setattr("vestige.indexer.index_all", lambda *a, **k: 1)   # 새 턴 1개
    monkeypatch.setattr(web, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setitem(web._state, "needs_onboarding", False)
    monkeypatch.setattr(web, "_last_export", [0.0])
    monkeypatch.setattr(web, "_export_due", [False])
    web._run_incremental(quick=False)
    assert calls == [1]
    assert any("내보내기 실패" in e for e in web._autoindex_state["sync_errors"])
    assert web._export_due[0] is True   # 실패한 건 다음에 다시

    ok = []
    monkeypatch.setattr(archive_sync, "export_archive", lambda *a, **k: ok.append(1) or 0)
    web._run_incremental(quick=False)
    web._run_incremental(quick=False)   # 바로 또 - 최소 간격 안이라 건너뛴다
    assert ok == [1]


def test_checkpoint_does_not_split_a_slice_that_yields_several_turns(tmp_path, monkeypatch):
    """한 구간이 턴 둘을 낳을 때(경계 판정이 어긋난 경우) 첫 턴 뒤에 커서를 넘기지 않는다 - 그 사이에
    죽으면 둘째 턴이 영영 빠졌다."""
    from vestige.sources import claude_code
    sid = "s1"
    lines = []
    for i in (1, 2):
        lines.append(json.dumps({"type": "user", "uuid": f"u{i}", "parentUuid": None, "sessionId": sid,
                                 "cwd": "C:/p", "timestamp": f"2026-10-07T00:0{i}:00Z",
                                 "message": {"role": "user", "content": f"질문 {i} 입니다 충분히 긴 내용"}}))
        lines.append(json.dumps({"type": "assistant", "sessionId": sid,
                                 "message": {"role": "assistant", "content": [{"type": "text", "text": f"답변 {i}"}]}}))
    f = tmp_path / "s1.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    orig = claude_code.ClaudeCodeAdapter.is_turn_start
    # 두 번째 질문을 '경계 아님'으로 위장 → 한 구간에서 턴 둘(#246 같은 어긋남)
    monkeypatch.setattr(claude_code.ClaudeCodeAdapter, "is_turn_start",
                        lambda self, o: orig(self, o) and o.get("uuid") != "u2")
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    real_upsert = db.upsert_turn

    def crash_on_second(turn, **kw):
        if turn.uuid == "u2":
            raise RuntimeError("강제 종료 흉내")
        return real_upsert(turn, **kw)

    monkeypatch.setattr(db, "upsert_turn", crash_on_second)
    try:
        index_file(str(f), db, vi, FakeEmbedder(), idle_secs=0, checkpoint_turns=1)
    except RuntimeError:
        pass
    offset, _, _ = db.get_cursor(str(f))
    assert offset < f.stat().st_size, "같은 구간의 둘째 턴을 쓰기 전에 커서가 구간 끝으로 넘어갔다"
