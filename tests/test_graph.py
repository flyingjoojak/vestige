"""3D 의미 지도(build_graph) 테스트. n<6 이라 UMAP/HDBSCAN 없이 PCA/무군집 폴백 경로로 결정적."""

from __future__ import annotations

import numpy as np

from vestige.graph import build_graph
from vestige.models import Turn
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex


def _turn(tid, session="s1", q="질문"):
    return Turn(id=tid, session_id=session, uuid=tid, parent_uuid=None,
                timestamp="2026-07-24T00:00:00Z", project="p", question=q, answer="a", actions=())


def test_hidden_turn_excluded_from_points(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "i.json")
    db.upsert_turn(_turn("s1:u1", q="숨길 턴")); db.upsert_turn(_turn("s1:u2", q="남는 턴 A"))
    db.upsert_turn(_turn("s1:u3", q="남는 턴 B")); db.commit()
    vi.add(["s1:u1#0", "s1:u2#0", "s1:u3#0"],
           np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32))

    db.hide_turns(["s1:u1"])
    out = build_graph(vi, db, dims=2)
    ids = {p["t"] for p in out["points"]}
    assert ids == {"s1:u2", "s1:u3"}


def test_hidden_turn_vector_does_not_skew_projection(tmp_path):
    """숨긴 턴의 벡터가 투영·군집 계산에 아예 들어가지 않는지 - 남은 점만으로 계산한 것과 좌표가 같아야 함."""
    db = ArchiveDB(tmp_path / "a.db")
    vi_with_hidden = VectorIndex(tmp_path / "v1.npy", tmp_path / "i1.json")
    vi_without = VectorIndex(tmp_path / "v2.npy", tmp_path / "i2.json")
    db.upsert_turn(_turn("s1:u1", q="숨길 턴")); db.upsert_turn(_turn("s1:u2", q="남는 턴 A"))
    db.upsert_turn(_turn("s1:u3", q="남는 턴 B")); db.commit()
    mat = np.array([[5, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)   # u1 이 극단값 → 남아있으면 좌표를 크게 흔듦
    vi_with_hidden.add(["s1:u1#0", "s1:u2#0", "s1:u3#0"], mat)
    vi_without.add(["s1:u2#0", "s1:u3#0"], mat[1:])
    db.hide_turns(["s1:u1"])

    out_hidden = build_graph(vi_with_hidden, db, dims=2)
    out_clean = build_graph(vi_without, db, dims=2)
    by_turn_hidden = {p["t"]: (p["x"], p["y"]) for p in out_hidden["points"]}
    by_turn_clean = {p["t"]: (p["x"], p["y"]) for p in out_clean["points"]}
    assert by_turn_hidden == by_turn_clean
