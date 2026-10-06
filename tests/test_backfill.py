"""backfill_missing — 벡터 없는 기존 청크만, 맥락(직전질문)까지 재구성해 임베딩하는지."""

from __future__ import annotations

import sqlite3

import numpy as np

from vestige.indexer import backfill_missing


class _FakeVI:
    def __init__(self, existing):
        self._keys = list(existing)
        self.added = []            # (keys, n) 기록

    def keys(self):
        return list(self._keys)

    def add(self, keys, matrix):
        self._keys.extend(keys)
        self.added.append(list(keys))

    def save(self):
        pass


class _FakeEmbedder:
    model_name = "test-model"

    def __init__(self):
        self.seen = []             # embed_passages로 넘어온 텍스트

    def embed_passages(self, texts, parallel=None, batch_size=None):
        self.seen.extend(texts)
        return np.zeros((len(texts), 3), dtype=np.float32)


class _FakeDB:
    def __init__(self, conn):
        self.conn = conn
        self.meta = {}

    def set_meta(self, k, v):
        self.meta[k] = v

    def set_embed_hashes(self, pairs):
        pass

    def commit(self):
        self.conn.commit()


def _db_with(turns, chunks):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE turns(id TEXT, session_id TEXT, timestamp TEXT, project TEXT, question TEXT)")
    conn.execute("CREATE TABLE chunks(chunk_key TEXT, turn_id TEXT, idx INT, text TEXT)")
    conn.executemany("INSERT INTO turns VALUES(?,?,?,?,?)", turns)
    conn.executemany("INSERT INTO chunks VALUES(?,?,?,?)", chunks)
    conn.commit()
    return _FakeDB(conn)


def test_backfill_only_missing_and_reconstructs_context():
    # 세션 s, 시간순: t1(질문 q1)→ 청크 t1#0(이미 있음), t2(질문 q2)→ 청크 t2#0,t2#1(누락)
    turns = [
        ("t1", "s", "2026-01-01T00:00", "proj", "q1"),
        ("t2", "s", "2026-01-01T00:01", "proj", "q2"),
    ]
    chunks = [
        ("t1#0", "t1", 0, "chunkA"),
        ("t2#0", "t2", 0, "chunkB"),
        ("t2#1", "t2", 1, "chunkC"),
    ]
    db = _db_with(turns, chunks)
    vi = _FakeVI({"t1#0"})            # t1#0만 이미 벡터 있음
    emb = _FakeEmbedder()
    prog = []
    n = backfill_missing(db, vi, emb, progress_fn=lambda d, t: prog.append((d, t)))

    assert n == 2                     # 누락 2개만
    assert set(vi._keys) == {"t1#0", "t2#0", "t2#1"}   # 채워짐
    joined = "\n".join(emb.seen)
    assert "chunkB" in joined and "chunkC" in joined
    assert "chunkA" not in joined     # 이미 있던 건 재임베딩 안 함
    assert "q1" in joined             # t2 청크의 맥락 = 직전 턴(t1) 질문
    assert prog and prog[-1] == (2, 2)
    assert emb.model_name == db.meta.get("embed_model")


def test_backfill_noop_when_complete():
    turns = [("t1", "s", "2026-01-01T00:00", "p", "q")]
    chunks = [("t1#0", "t1", 0, "x")]
    db = _db_with(turns, chunks)
    vi = _FakeVI({"t1#0"})            # 이미 다 있음
    emb = _FakeEmbedder()
    assert backfill_missing(db, vi, emb) == 0
    assert emb.seen == []             # 임베더 호출 없음
