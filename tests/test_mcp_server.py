"""MCP 서버 오프로드 검증 — 동기 툴 블로킹을 워커 스레드로 뺐는지."""

from __future__ import annotations

import asyncio
import threading
import time

from vestige import mcp_server as M


def test_tools_are_async():
    # 툴이 async여야 FastMCP가 이벤트 루프를 안 막고 await한다.
    for fn in (M.search_memory, M.find_related, M.get_session, M.recent_sessions, M.stats):
        assert asyncio.iscoroutinefunction(fn), f"{fn.__name__} must be async"


def test_search_memory_source_filter(monkeypatch):
    """source 파라미터가 백엔드 tool_sources 로 연결되는지(#139). ""=전체, "claude" 별칭."""
    import vestige.search as S

    captured: dict = {}

    def fake_search(query, db, vi, emb, **kw):
        captured.clear()
        captured.update(kw)
        return []

    monkeypatch.setattr(S, "search", fake_search)
    monkeypatch.setattr(M, "_db", lambda: type("D", (), {"get_meta": lambda self, k: None})())
    monkeypatch.setattr(M, "_vi", lambda: [0])          # len 1 (비어있지 않음)
    monkeypatch.setattr(M, "_embedder", lambda: object())

    M._search_memory("q", 5, False, "", "", source="codex")
    assert captured["tool_sources"] == {"codex"}
    M._search_memory("q", 5, False, "", "", source="claude")   # 별칭 → claude-code
    assert captured["tool_sources"] == {"claude-code"}
    M._search_memory("q", 5, False, "", "")                     # 미지정 → 전체
    assert captured["tool_sources"] is None


def test_find_related_excludes_own_session(monkeypatch, tmp_path):
    """find_related(#140): 기준 세션은 결과에서 제외하고 비슷한 다른 세션만 반환."""
    import vestige.search as S
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="sA:u1", session_id="sA", uuid="u1", parent_uuid=None,
                        timestamp="2026-07-24T00:00:00Z", project="p",
                        question="비슷한 작업 질문", answer="답", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(M, "_db", lambda: db)
    monkeypatch.setattr(M, "_vi", lambda: [0])
    monkeypatch.setattr(M, "_embedder", lambda: object())

    def _hit(sid, tid, q):
        t = Turn(id=tid, session_id=sid, uuid="x", parent_uuid=None,
                 timestamp="2026-07-24T00:00:00Z", project="p", question=q, answer="a", actions=())
        return S.SearchHit(turn=t, score=1.0, sources=("semantic",))

    monkeypatch.setattr(S, "search", lambda *a, **k: [
        _hit("sA", "sA:u2", "자기 세션 결과"),        # 제외돼야
        _hit("sB", "sB:u1", "다른 세션 유사 작업"),    # 나와야
    ])
    out = M._find_related("sA:u1", 5)
    assert "다른 세션 유사 작업" in out and "session: sB" in out
    assert "자기 세션 결과" not in out                 # 기준 세션(sA) 제외


def test_offload_uses_single_non_main_worker():
    async def go():
        main = threading.get_ident()
        a, b = await asyncio.gather(
            M._offload(threading.get_ident),
            M._offload(threading.get_ident),
        )
        assert a == b            # 항상 동일한 단일 워커(sqlite 연결이 한 스레드에 고정됨)
        assert a != main         # 이벤트 루프(메인) 스레드가 아님
    asyncio.run(go())


def test_event_loop_not_blocked_during_offload():
    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(100):
                ticks += 1
                await asyncio.sleep(0.005)

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0)          # ticker가 먼저 한 번 돌게 양보
        await M._offload(lambda: time.sleep(0.2))   # 워커에서 0.2s 블록
        during = ticks
        t.cancel()
        # 루프가 안 막혔다면 0.2s 동안 ticker가 여러 번 진행(≈수십 회). 막혔다면 1 이하.
        assert during > 5, f"event loop appears blocked (ticks={during})"
    asyncio.run(go())
