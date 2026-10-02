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
