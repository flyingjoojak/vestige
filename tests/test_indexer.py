"""인덱서(파일 단위 증분·체크포인트·재개) 테스트. 가짜 임베더로 fastembed 없이."""

from __future__ import annotations

import json

import numpy as np

from vestige.indexer import has_new_data, index_file
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex


class FakeEmbedder:
    model_name = "fake"

    def embed_passages(self, texts):
        # 결정적 8차원 벡터(정규화 불필요 — 테스트용).
        return np.array([[float(len(t) % 7) + 1] * 8 for t in texts], dtype=np.float32)


def _write_jsonl(path, n_turns):
    lines = []
    for i in range(n_turns):
        lines.append(json.dumps({
            "type": "user", "uuid": f"u{i}", "parentUuid": None, "sessionId": "s1",
            "cwd": "C:/proj", "timestamp": f"2026-07-24T00:0{i}:00Z",
            "message": {"role": "user", "content": f"질문 번호 {i} 입니다 상세 내용"},
        }))
        lines.append(json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"답변 {i}"}]},
        }))
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def test_index_file_basic(tmp_path):
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")

    n = index_file(f, db, vi, FakeEmbedder(), idle_secs=0, checkpoint_turns=1)
    assert n == 3
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 3
    assert len(vi) >= 3  # 턴당 최소 1청크
    # 커서가 파일 끝까지 전진했는지.
    offset, size, _ = db.get_cursor(str(f))
    assert offset == size


def test_index_file_idempotent_rerun(tmp_path):
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    e = FakeEmbedder()

    index_file(f, db, vi, e, idle_secs=0)
    v1 = len(vi)
    # 다시 돌려도 새로 처리할 게 없다(커서가 끝).
    n2 = index_file(f, db, vi, e, idle_secs=0)
    assert n2 == 0
    assert len(vi) == v1  # 중복 안 늘어남


def test_has_new_data(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    proj.mkdir()
    f = proj / "s1.jsonl"
    _write_jsonl(f, 2)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")

    # 아직 인덱싱 전 → 새 데이터 있음.
    assert has_new_data(db, projects_dir=proj) is True

    index_file(f, db, vi, FakeEmbedder(), idle_secs=0)
    # 다 처리 후 → 새 데이터 없음(모델 로드 스킵될 상황).
    assert has_new_data(db, projects_dir=proj) is False

    # 대화가 이어져 파일이 커지면 → 다시 새 데이터 있음.
    with open(f, "ab") as fh:
        import json as _j
        fh.write((_j.dumps({"type": "user", "uuid": "u9", "sessionId": "s1",
                            "message": {"role": "user", "content": "새 질문 추가됨 상세"}}) + "\n").encode())
    # 방금 이어 쓴 단일 턴은 진행 중이라 색인기가 보류한다(새 데이터 아님) - 쉬는 파일이 되면 대상.
    assert has_new_data(db, projects_dir=proj) is False
    import os, time
    old = time.time() - 10 * 60
    os.utime(f, (old, old))
    assert has_new_data(db, projects_dir=proj) is True


def test_index_file_resume_from_cursor(tmp_path):
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 4)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    e = FakeEmbedder()

    # 1턴만 처리되도록 인위적으로 끊는 대신, 전체 처리 후 커서 리셋해 재개 검증.
    index_file(f, db, vi, e, idle_secs=0)
    # 커서를 0으로 되돌리고 재실행 → 멱등(같은 턴 수, 벡터 중복 없음).
    db.set_cursor(str(f), 0, 0, 0.0)
    db.commit()
    before = len(vi)
    index_file(f, db, vi, e, idle_secs=0)
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 4
    assert len(vi) == before  # 재처리해도 키가 같아 교체(중복 X)


def _turn(session, uuid, text, ts="2026-07-24T00:00:00Z"):
    return json.dumps({"type": "user", "uuid": uuid, "parentUuid": None, "sessionId": session,
                       "cwd": "C:/proj", "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def _assistant(session, text, tool=None):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {"command": "run"}})
    return json.dumps({"type": "assistant", "sessionId": session,
                       "message": {"role": "assistant", "content": content}})


def test_idle_finalized_turn_recaptures_trailing_content(tmp_path):
    """긴 도구호출로 idle 확정된 턴에 나중에 붙은 뒷내용이 유실되지 않고 합쳐지는지(회귀)."""
    import os, time
    f = tmp_path / "s.jsonl"
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "v.json")
    emb = FakeEmbedder()

    # 1) 유저 + 어시스턴트(텍스트 + 도구호출, tool_result 아직 없음), 파일 idle.
    f.write_bytes(("\n".join([
        _turn("s1", "u1", "빌드를 고쳐줘 상세 내용입니다"),
        _assistant("s1", "빌드를 확인하겠습니다.", tool="Bash"),
    ]) + "\n").encode("utf-8"))
    old = time.time() - 300
    os.utime(f, (old, old))
    index_file(str(f), db, vi, emb, idle_secs=120)
    t1 = db.get_turn("s1:u1")
    assert t1 is not None and t1.answer == "빌드를 확인하겠습니다."
    assert db.get_hold(str(f)) is not None   # 열린 턴 시작이 기록됨

    # 2) 도구 완료 → 같은 턴에 뒷내용 추가(새 유저턴 없음).
    with open(f, "a", encoding="utf-8") as fp:
        fp.write(_assistant("s1", "빌드 완료. 이제 테스트를 돌리겠습니다.") + "\n")
    os.utime(f, (old, old))
    index_file(str(f), db, vi, emb, idle_secs=120)
    t2 = db.get_turn("s1:u1")
    assert "빌드 완료" in (t2.answer or ""), "idle 확정 턴의 뒷내용이 유실됨"

    # 3) 변화 없으면 스킵(무의미한 재처리 없음).
    assert index_file(str(f), db, vi, emb, idle_secs=120) == 0


def test_held_turn_completes_with_frequent_checkpoints(tmp_path):
    """checkpoint_turns=1(잦은 중간 체크포인트)에서도 idle-held 마지막 턴이 뒷내용으로 완성되는지(#151)."""
    import os, time
    f = tmp_path / "s.jsonl"
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "v.json")
    emb = FakeEmbedder()

    # 완결 턴(u1) + 긴 도구호출로 아직 열린 턴(u2), 파일 idle.
    f.write_bytes(("\n".join([
        _turn("s1", "u1", "이전 완결 질문 상세 내용"), _assistant("s1", "이전 답변입니다"),
        _turn("s1", "u2", "빌드를 고쳐줘 상세 내용"), _assistant("s1", "확인하겠습니다", tool="Bash"),
    ]) + "\n").encode("utf-8"))
    old = time.time() - 300
    os.utime(f, (old, old))
    index_file(str(f), db, vi, emb, idle_secs=120, checkpoint_turns=1)
    assert db.get_turn("s1:u2").answer == "확인하겠습니다"
    assert db.get_hold(str(f)) is not None      # held 턴 시작 기록됨(중간 체크포인트에도 유지)

    # 도구 완료 → 같은 턴에 뒷내용 append(새 유저턴 없음) → 재읽기로 완성.
    with open(f, "a", encoding="utf-8") as fp:
        fp.write(_assistant("s1", "빌드 완료. 테스트도 통과했습니다.") + "\n")
    os.utime(f, (old, old))
    index_file(str(f), db, vi, emb, idle_secs=120, checkpoint_turns=1)
    assert "빌드 완료" in (db.get_turn("s1:u2").answer or ""), "잦은 체크포인트에서 held 턴 뒷내용 유실"
    assert db.get_turn("s1:u1").answer == "이전 답변입니다"   # 앞 완결 턴은 그대로


def test_group_keeps_every_turn_when_boundaries_disagree():
    """구간 하나에서 턴이 여럿 나오면 전부 싣는다 - 예전엔 첫 턴만 남기고 조용히 버렸다.

    is_turn_start 와 extract_turns 의 기준이 어긋나면 생기는 일이다(#246 직후 실제로 끼어든
    질문과 그 답변이 DB 에서 사라졌다). 기준은 parser.is_turn_start 로 합쳤지만, 다시 어긋나도
    데이터가 사라지지는 않게 한다.
    """
    from vestige.indexer import _group_with_offsets

    class Disagreeing:
        def is_turn_start(self, o):
            return o.get("start", False)          # 첫 줄만 경계로 본다

        def extract_turns(self, objs):
            return [o["name"] for o in objs]       # 그런데 줄마다 턴을 만든다

    proc = [({"start": True, "name": "앞 턴"}, 0, 10), ({"name": "끼어든 턴"}, 10, 20)]
    out = _group_with_offsets(proc, 20, Disagreeing())
    assert [t for t, _ in out] == ["앞 턴", "끼어든 턴"], "구간 안의 뒤 턴을 버렸다"


def test_corrupt_line_is_reported_to_the_index_status(tmp_path, monkeypatch):
    """깨진 줄은 건너뛰되 화면 오류("ERROR " 로그)로 알린다 - 그 자리의 대화가 영구히 빠지므로.

    예전엔 경고 로그만 남겨 앱 어디에도 안 보였다. 나머지 턴은 정상으로 뽑혀 '0턴' 감지에도 안 걸린다.
    """
    from vestige import indexer as I
    from vestige.sources.claude_code import ClaudeCodeAdapter

    f = tmp_path / "s1.jsonl"
    good = _turn("s1", "u1", "정상 질문입니다 상세 내용") + "\n" + _assistant("s1", "답") + "\n"
    f.write_text(good + '{"type": "user", 깨진 줄\n' + _turn("s1", "u2", "두번째 질문입니다 상세", ts="2026-07-24T00:05:00Z")
                 + "\n" + _assistant("s1", "답2") + "\n", encoding="utf-8")
    import os
    import time
    old = time.time() - 3600
    os.utime(f, (old, old))   # 진행 중 턴 보류(2분)에 안 걸리게 - 마지막 턴까지 확정
    monkeypatch.setattr(I, "discover_files", lambda recent_first=True: [(str(f), ClaudeCodeAdapter())])
    monkeypatch.setattr(I.raw_archive, "mirror_file", lambda *a, **k: None)
    db = ArchiveDB(tmp_path / "a.db")
    logs = []
    I.index_all(db, VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"), FakeEmbedder(), log_fn=logs.append)

    errs = [m for m in logs if m.startswith("ERROR ") and "깨진 로그" in m]
    assert len(errs) == 1 and "1줄" in errs[0], logs
    assert db.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2   # 나머지는 정상


class _CountingEmbedder(FakeEmbedder):
    def __init__(self):
        self.texts = 0

    def embed_passages(self, texts):
        self.texts += len(texts)
        return super().embed_passages(texts)


def test_reparse_skips_chunks_whose_text_and_vector_are_unchanged(tmp_path):
    """재색인(커서를 비우고 로그를 처음부터 다시 읽기)은 바뀐 청크만 다시 임베딩한다.

    예전엔 내용이 그대로인 청크까지 전부 다시 임베딩해 이 기기 기준 1시간 20분이 걸렸다.
    """
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 4)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    first = _CountingEmbedder()
    index_file(f, db, vi, first, idle_secs=0)
    assert first.texts >= 4

    # 1) 그대로 다시 읽기 - 임베딩 0건
    db.clear_cursors()
    again = _CountingEmbedder()
    index_file(f, db, vi, again, idle_secs=0)
    assert again.texts == 0, f"안 바뀐 청크를 {again.texts}개 다시 임베딩했다"

    # 2) 한 턴만 바뀌면 그 턴의 청크만
    lines = f.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[3])   # 턴 1 의 답변. 한글이 \u 로 이스케이프돼 있어 문자열 치환은 안 먹는다
    rec["message"]["content"][0]["text"] += " - 나중에 이어 붙은 내용"
    lines[3] = json.dumps(rec)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.clear_cursors()
    changed = _CountingEmbedder()
    index_file(f, db, vi, changed, idle_secs=0)
    assert 1 <= changed.texts < first.texts, (changed.texts, first.texts)

    # 3) 벡터를 비우면(모델 교체) 전부 다시
    vi.reset()
    db.clear_cursors()
    full = _CountingEmbedder()
    index_file(f, db, vi, full, idle_secs=0)
    assert full.texts == first.texts


def test_reindex_keeps_vectors_only_for_the_same_model():
    from vestige import web
    assert web._must_reset_vectors("intfloat/multilingual-e5-large-int8", "intfloat/multilingual-e5-large-int8") is False
    assert web._must_reset_vectors("intfloat/multilingual-e5-large-int8", "BAAI/bge-m3") is True
    assert web._must_reset_vectors(None, "intfloat/multilingual-e5-large-int8") is True   # 모르면 비운다


def test_embedding_does_not_hold_the_db_write_lock(tmp_path):
    """임베딩(느림)하는 동안 다른 연결이 바로 쓸 수 있어야 한다. 예전엔 턴·청크를 쓴 트랜잭션을
    연 채로 임베딩해, 색인 중 앱의 접기·폴더·제목 저장이 55초씩 멈췄다(busy_timeout 60초)."""
    import sqlite3
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    path = tmp_path / "a.db"
    db = ArchiveDB(path)
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    blocked = []

    class _WritingEmbedder(FakeEmbedder):
        def embed_passages(self, texts):
            other = sqlite3.connect(str(path), timeout=0)   # 기다리지 않는다: 잠겨 있으면 즉시 실패
            try:
                other.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('probe','1')")
                other.commit()
            except sqlite3.OperationalError as e:
                blocked.append(str(e))
            finally:
                other.close()
            return super().embed_passages(texts)

    index_file(f, db, vi, _WritingEmbedder(), idle_secs=0, checkpoint_turns=50)
    assert blocked == []


class _RecordingEmbedder(FakeEmbedder):
    def __init__(self):
        self.seen = []

    def embed_passages(self, texts):
        self.seen.extend(texts)
        return super().embed_passages(texts)


def test_reparse_reembeds_chunks_whose_context_changed(tmp_path):
    """텍스트가 그대로여도 직전 질문(맥락)이 바뀌었으면 다시 임베딩한다. 끼어든 질문이 새로
    갈라지면 그 뒤 턴의 직전 질문이 바뀌는데, 텍스트만 비교하면 옛 맥락의 벡터가 남았다."""
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    index_file(f, db, vi, FakeEmbedder(), idle_secs=0)

    lines = f.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])   # 턴 0 의 질문만 바꾼다 → 턴 1 은 텍스트 그대로, 맥락만 바뀜
    rec["message"]["content"] += " 그리고 덧붙인 조건"
    lines[0] = json.dumps(rec)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    db.clear_cursors()
    again = _RecordingEmbedder()
    index_file(f, db, vi, again, idle_secs=0)

    assert any("질문 번호 0" in t and "덧붙인 조건" in t and "이전:" not in t for t in again.seen)   # 턴 0: 텍스트가 바뀜
    assert any(t.startswith("[") and "덧붙인 조건" in t and "답변 1" in t for t in again.seen)   # 턴 1: 맥락만 바뀜
    assert not any("답변 2" in t for t in again.seen)   # 턴 2: 맥락(턴 1 질문)도 텍스트도 그대로


def test_incremental_pass_gives_the_first_turn_its_previous_question(tmp_path):
    """증분 회차의 첫 턴도 DB 의 직전 질문을 맥락으로 받는다(예전엔 빈 맥락이었다)."""
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    # 첫 회차: 마지막 턴(2)은 진행 중일 수 있어 보류 → 커서가 턴 2 시작에 멈춘다
    index_file(f, db, vi, FakeEmbedder(), idle_secs=10**9)
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 2

    # 다음 회차는 턴 2 부터 읽는다 - 이 회차 안에는 직전 질문(턴 1)이 없다
    later = _RecordingEmbedder()
    index_file(f, db, vi, later, idle_secs=0)
    new = [t for t in later.seen if "답변 2" in t]
    assert new and all("이전: 질문 번호 1" in t for t in new), later.seen


def test_legacy_chunks_get_a_hash_so_later_context_changes_are_caught(tmp_path):
    """마이그레이션 전 청크(해시 없음)는 재색인 때 지금 입력의 해시를 받는다 → 그다음부터 맥락 변화를 잡는다."""
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 3)
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    index_file(f, db, vi, FakeEmbedder(), idle_secs=0)
    db.conn.execute("UPDATE chunks SET embed_hash=NULL")   # 옛 DB 흉내
    db.commit()

    db.clear_cursors()
    again = _RecordingEmbedder()
    index_file(f, db, vi, again, idle_secs=0)
    assert again.seen == []   # 텍스트가 같으니 다시 임베딩은 안 하고
    assert db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE embed_hash IS NULL").fetchone()["c"] == 0   # 해시만 찍는다


def _snapshot(db, path):
    turns = db.conn.execute("SELECT id, question, answer, actions, queued FROM turns ORDER BY id").fetchall()
    chunks = db.conn.execute("SELECT chunk_key, text FROM chunks ORDER BY chunk_key").fetchall()
    return [tuple(r) for r in turns], [tuple(r) for r in chunks], db.get_cursor(str(path))[0], db.get_hold(str(path))


def test_slicing_a_big_log_gives_the_same_result(tmp_path, monkeypatch):
    """로그를 구간으로 끊어 처리해도(메모리에 통째로 안 올림) 결과가 한 번에 처리한 것과 같다 -
    진행 중 마지막 턴 보류(idle 아님)와 idle 확정(hold) 둘 다."""
    import vestige.indexer as I
    whole = I._SLICE_BYTES
    for idle_secs in (10**9, 0):
        results = []
        for slice_bytes in (whole, 1):   # 1 = 턴이 시작할 때마다 끊는다
            monkeypatch.setattr(I, "_SLICE_BYTES", slice_bytes)
            d = tmp_path / f"{idle_secs}-{slice_bytes}"
            d.mkdir()
            f = d / "s1.jsonl"
            _write_jsonl(f, 12)
            db = ArchiveDB(d / "a.db")
            index_file(f, db, VectorIndex(d / "v.npy", d / "ids.json"), FakeEmbedder(),
                       idle_secs=idle_secs, checkpoint_turns=5)
            results.append(_snapshot(db, f))
        assert results[0] == results[1], idle_secs
        assert len(results[0][0]) == (12 if idle_secs == 0 else 11)   # idle 아니면 마지막 턴 보류


def test_idle_is_judged_on_a_fresh_mtime_after_long_slices(tmp_path, monkeypatch):
    """앞 구간 처리가 오래 걸리는 동안 파일이 쓰였으면, 마지막 턴을 끝난 것으로 확정하지 않는다."""
    import os
    import vestige.indexer as I
    monkeypatch.setattr(I, "_SLICE_BYTES", 1)
    f = tmp_path / "s1.jsonl"
    _write_jsonl(f, 4)
    old = os.path.getmtime(f) - 3600
    os.utime(f, (old, old))   # 시작할 땐 한 시간 조용했던 파일

    class TouchingEmbedder(FakeEmbedder):
        def embed_passages(self, texts):
            os.utime(f, None)   # 처리하는 사이 누가 이 로그에 쓴다
            return super().embed_passages(texts)

    db = ArchiveDB(tmp_path / "a.db")
    index_file(f, db, VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"), TouchingEmbedder(), idle_secs=60)
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 3   # 마지막 턴은 보류
    assert db.get_hold(str(f)) is None


def test_walk_cache_does_not_block_while_the_startup_warmup_runs(monkeypatch):
    """앱 시작 직후 뒤에서 처음 훑는 동안, 화면 요청은 기다리지 않고 빈 목록을 받는다(예전엔 44초 멈췄다)."""
    import time as _t
    import vestige.indexer as I
    monkeypatch.setattr(I, "_walk_cache", {"at": 0.0, "roots": None, "files": []})
    assert I._walk_lock.acquire(blocking=False)   # warm_walk_cache 가 훑는 중인 상태
    try:
        t = _t.perf_counter()
        assert I.iter_all_cached() == []
        assert _t.perf_counter() - t < 0.5
    finally:
        I._walk_lock.release()
