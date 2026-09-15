"""하이브리드 검색(의미+키워드 RRF) 테스트. 가짜 임베더로 fastembed 없이."""

from __future__ import annotations

import numpy as np

from vestige.chunker import chunk_turn
from vestige.models import Action, Turn
from vestige.search import search
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex


class FakeEmbedder:
    model_name = "fake"

    def embed_passages(self, texts):
        # 인덱스별로 조금씩 다른 벡터(결정적) → 의미 순위가 존재하도록.
        return np.array([[float(i + 1), 1.0, 0.0, 0.0] for i, _ in enumerate(texts)], dtype=np.float32)

    def embed_query(self, q):
        return np.array([1.0, 1.0, 0.0, 0.0], dtype=np.float32)


def _seed(db, vi, turns):
    e = FakeEmbedder()
    for t in turns:
        db.upsert_turn(t)
        chunks = chunk_turn(t)
        db.add_chunks(chunks)
        keys = [f"{c.turn_id}#{c.index}" for c in chunks]
        if keys:
            vi.add(keys, e.embed_passages([c.text for c in chunks]))
    db.commit()


def _turn(tid, q, a="", actions=(), ts="2026-07-24T00:00:00Z"):
    return Turn(id=tid, session_id="s1", uuid=tid, parent_uuid=None,
                timestamp=ts, project="p", question=q, answer=a, actions=actions)


def test_keyword_search_finds_exact_token(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    assert db.fts_enabled
    _seed(db, VectorIndex(tmp_path / "v.npy", tmp_path / "i.json"), [
        _turn("s1:u1", "서버 실행", "백엔드가 포트 8088 에서 떴어"),
        _turn("s1:u2", "점심 뭐 먹지", "글쎄"),
    ])
    res = db.keyword_search("8088")
    assert res and res[0][0] == "s1:u1"


def test_hybrid_surfaces_keyword_only_turn(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    _seed(db, vi, [
        _turn("s1:u1", "환경변수 설정", "STAGE1_BYPASS_EMAILS 로 특정 계정만 허용"),
        _turn("s1:u2", "일반 대화", "아무 내용"),
        _turn("s1:u3", "또 다른 대화", "관계 없는 내용"),
    ])
    e = FakeEmbedder()
    hits = search("STAGE1", db, vi, e, k=5)
    ids = [h.turn.id for h in hits]
    assert "s1:u1" in ids
    top = next(h for h in hits if h.turn.id == "s1:u1")
    assert "keyword" in top.sources


def test_date_range_since_until(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    _seed(db, vi, [
        _turn("s1:u1", "STAGE1 예전", "내용", ts="2026-07-10T09:00:00Z"),
        _turn("s1:u2", "STAGE1 최근", "내용", ts="2026-07-22T09:00:00Z"),
    ])
    e = FakeEmbedder()
    # since: 7-15 이후 → u2만
    ids = [h.turn.id for h in search("STAGE1", db, vi, e, k=5, since="2026-07-15")]
    assert ids == ["s1:u2"]
    # until: 7-15 이전 → u1만
    ids = [h.turn.id for h in search("STAGE1", db, vi, e, k=5, until="2026-07-15")]
    assert ids == ["s1:u1"]
    # until 날짜만 주면 그날 포함(7-22 이전에 7-22 09:00 포함)
    ids = [h.turn.id for h in search("STAGE1", db, vi, e, k=5, until="2026-07-22")]
    assert "s1:u2" in ids


def test_kst_boundary_precise(tmp_path):
    # KST 달력일 2026-07-22 하루(00:00~24:00 KST)만 걸러지는지 — UTC 저장값 경계 검증.
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    _seed(db, vi, [
        _turn("s1:uA", "STAGE1 A", "x", ts="2026-07-21T15:30:00Z"),  # KST 07-22 00:30 (포함)
        _turn("s1:uB", "STAGE1 B", "x", ts="2026-07-22T14:30:00Z"),  # KST 07-22 23:30 (포함)
        _turn("s1:uC", "STAGE1 C", "x", ts="2026-07-22T15:30:00Z"),  # KST 07-23 00:30 (제외)
        _turn("s1:uZ", "STAGE1 Z", "x", ts="2026-07-21T14:30:00Z"),  # KST 07-21 23:30 (제외)
    ])
    e = FakeEmbedder()
    ids = {h.turn.id for h in search("STAGE1", db, vi, e, k=10,
                                     since="2026-07-22", until="2026-07-22")}
    assert ids == {"s1:uA", "s1:uB"}


def test_semantic_only_flag_disables_keyword(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    _seed(db, vi, [_turn("s1:u1", "질문 내용입니다", "포트 8088 관련 답변")])
    e = FakeEmbedder()
    hits = search("8088", db, vi, e, k=5, keyword=False)
    # 키워드 끄면 소스에 'keyword' 없음.
    assert all("keyword" not in h.sources for h in hits)


def test_hidden_turn_excluded_from_results(tmp_path):
    """숨김(#128) 처리된 턴은 의미·키워드 어느 경로로도 결과에 나오지 않는다(비파괴 - turns 는 그대로)."""
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    _seed(db, vi, [
        _turn("s1:u1", "STAGE1 숨길 턴", "포트 8088 관련"),
        _turn("s1:u2", "STAGE1 남는 턴", "다른 내용"),
    ])
    db.hide_turns(["s1:u1"])
    e = FakeEmbedder()
    ids = {h.turn.id for h in search("STAGE1", db, vi, e, k=10)}
    assert ids == {"s1:u2"}
    assert db.get_turn("s1:u1") is not None   # 원문은 그대로 남아있음(비파괴)
