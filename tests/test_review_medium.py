"""v0.4.0 릴리스 전 풀리뷰 MEDIUM 수정의 회귀 검사."""
import os

import pytest

from vestige.models import Turn
from vestige.store import ArchiveDB


def test_vectors_db_waits_like_archive_db(tmp_path):
    """vectors.db 도 archive.db 처럼 잠금을 60초 기다리고 WAL 로 연다(기본 5초면 다른 프로세스가 쓰는 동안 검색이 실패)."""
    pytest.importorskip("sqlite_vec")
    from vestige.vectorindex import SqliteVecIndex
    vi = SqliteVecIndex(tmp_path / "v.db")
    assert vi.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
    assert vi.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    import numpy as np
    vi.add(["t1#0"], np.ones((1, 4), dtype=np.float32))   # WAL 에서도 vec0 가 동작
    assert vi.search(np.ones(4, dtype=np.float32), k=1)[0][0] == "t1#0"


def test_turn_ids_of_chunks_matches_one_by_one(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.conn.executemany("INSERT INTO chunks(chunk_key, turn_id, idx, text) VALUES(?,?,?,?)",
                        [(f"t{i}#0", f"t{i}", 0, "x") for i in range(1200)])
    keys = [f"t{i}#0" for i in range(1200)] + ["nope#0"]
    batched = db.turn_ids_of_chunks(keys)
    assert batched == {k: db.turn_id_of_chunk(k) for k in keys if db.turn_id_of_chunk(k)}


def test_enrich_runs_claude_without_tools_in_an_empty_folder(monkeypatch):
    """요약 AI 프롬프트엔 (다른 기기에서 온) 대화 원문이 들어간다 - 도구 없이, 빈 임시 폴더에서 실행."""
    from vestige import enrich
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "[]", ""

    def run(argv, **kw):
        seen["argv"], seen["cwd"] = argv, kw.get("cwd")
        seen["empty"] = os.listdir(kw["cwd"]) == []
        return R()

    monkeypatch.setattr(enrich, "resolve_claude_bin", lambda: "claude")
    monkeypatch.setattr(enrich.subprocess, "run", run)
    enrich._call_claude_cli("prompt", "haiku")
    i = seen["argv"].index("--tools")
    assert seen["argv"][i + 1] == "" and seen["empty"]
    assert not os.path.exists(seen["cwd"])   # 끝나면 지운다


def test_enrich_reports_a_session_that_got_no_summaries(tmp_path, monkeypatch):
    from vestige import enrich
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None, timestamp="2026-10-07T00:00:00Z",
                        project="p", question="질문", answer="답", actions=()), source="claude-code", source_file="f")
    db.commit()
    monkeypatch.setattr(enrich, "enrich_session", lambda *a, **k: 0)
    logs = []
    enrich.enrich_all(db, backend="claude", throttle=0, log_fn=logs.append)
    assert any(m.startswith("ERROR enrich s1") for m in logs), logs


def test_mcp_session_prefix_treats_wildcards_literally():
    from vestige.mcp_server import _like_prefix
    assert _like_prefix("a_b%") == "a!_b!%%"


def test_sync_tick_scans_for_conflicts_at_most_once_per_interval(tmp_path, monkeypatch):
    from vestige import session_sync
    calls = []
    monkeypatch.setattr(session_sync, "resolve_all", lambda root: calls.append(root) or [])
    session_sync._last_scan.clear()
    for _ in range(3):
        session_sync.sync_tick(tmp_path, min_scan_secs=60)
    assert len(calls) == 1
    session_sync.sync_tick(tmp_path)   # 기본(0) = 매번
    assert len(calls) == 2
