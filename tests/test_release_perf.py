"""v0.4.0 최종 풀리뷰(성능) 수정의 회귀 검사."""
import gzip
import json
import os
import time
from types import SimpleNamespace

import pytest

from vestige import raw_archive as R
from vestige import web
from vestige.indexer import _held_back_only, has_new_data
from vestige.store import ArchiveDB


def _write_turns(path, n):
    lines = []
    for i in range(n):
        lines.append(json.dumps({
            "type": "user", "uuid": f"u{i}", "parentUuid": None, "sessionId": "s1",
            "cwd": "C:/p", "timestamp": f"2026-08-26T00:0{i}:00Z",
            "message": {"role": "user", "content": f"질문 {i}"},
        }))
        lines.append(json.dumps({
            "type": "assistant", "sessionId": "s1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"답 {i}"}]},
        }))
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return str(path)


# --- 진행 중인 마지막 턴만 남았을 때는 '새 데이터'가 아니다 ---------------------------------------------

def test_has_new_data_ignores_a_lone_in_progress_turn(tmp_path):
    """활성 세션의 마지막 턴은 색인기가 2분간 보류한다. 그 턴만 남았는데도 True 면 Claude 를 쓰는 내내
    할 일 없는 회차마다 임베딩 모델(약 0.8GB)을 올리고, 유휴 언로드도 영영 일어나지 않는다."""
    proj = tmp_path / "projects"
    proj.mkdir()
    db = ArchiveDB(tmp_path / "a.db")
    f = _write_turns(proj / "s1.jsonl", 1)
    assert has_new_data(db, projects_dir=proj) is False          # 방금 쓴 단일 턴 = 보류 대상

    _write_turns(proj / "s1.jsonl", 2)
    assert has_new_data(db, projects_dir=proj) is True           # 완결된 앞 턴이 있다

    _write_turns(proj / "s1.jsonl", 1)
    old = time.time() - 10 * 60
    os.utime(f, (old, old))
    assert has_new_data(db, projects_dir=proj) is True           # 쉬는 파일은 마지막 턴도 색인 대상


class _Adapter:
    def __init__(self, n):
        self.n, self.read = n, 0

    def read_records(self, path, offset):
        for i in range(self.n):
            self.read += 1
            yield ({"i": i}, i)

    def is_turn_start(self, obj):
        return True


def test_held_back_only_stops_at_the_second_turn_start():
    """커서 뒤 로그 전체를 리스트로 올리면(재색인 직후 356MB) 8초마다 수 초 CPU 와 수백 MB 가 든다."""
    a = _Adapter(10_000)
    assert _held_back_only(a, "x", 0) is False
    assert a.read <= 3, a.read


# --- 원본 미러링은 조각으로 읽는다 ------------------------------------------------------------------

def test_mirror_file_reads_in_slices(tmp_path, monkeypatch):
    monkeypatch.setattr(R.C, "RAW_ARCHIVE_DIR", tmp_path / "raw")
    monkeypatch.setattr(R, "_MIRROR_SLICE", 64)
    db = ArchiveDB(tmp_path / "a.db")
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    body = b"".join(b'{"n":%d}\n' % i for i in range(100))      # 64B 보다 훨씬 크다
    f.write_bytes(body)

    writes = []
    real = gzip.GzipFile

    class Spy(real):
        def write(self, data):
            writes.append(len(data))
            return super().write(data)

    monkeypatch.setattr(R.gzip, "GzipFile", Spy)
    assert R.mirror_file(db, f, "claude-code") == len(body)
    assert max(writes) <= 64 and len(writes) > 1, writes        # 통째로 읽어 한 번에 쓰지 않는다
    assert R.read_mirror("claude-code", sid) == body
    assert db.get_raw_cursor(str(f)) == len(body)


# --- 설정 화면의 자동화 프롬프트 집계 ---------------------------------------------------------------

def test_sdk_stats_does_not_parse_files_without_the_marker(tmp_path, monkeypatch):
    """설정 화면을 열 때마다 원문 로그 전체를 JSON 으로 파싱했다(1.96GB 약 15초, GIL 을 쥔 채)."""
    plain = tmp_path / "plain.jsonl"
    _write_turns(plain, 3)
    sdk = tmp_path / "sdk.jsonl"
    sdk.write_text(
        json.dumps({"type": "user", "promptSource": "sdk", "uuid": "a", "sessionId": "s",
                    "message": {"role": "user", "content": "자동화가 보낸 질문입니다"}}) + "\n"
        + json.dumps({"type": "user", "promptSource": "sdk", "uuid": "b", "sessionId": "s",
                      "message": {"role": "user", "content": "자동화가 보낸 또 다른 질문"}}) + "\n",
        encoding="utf-8")
    # 표지는 있지만 plumbing(실제 질문이 아닌) 기록인 줄은 세지 않는다 - 실제 로그의 대부분이 이 경우다
    with open(sdk, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "promptSource": "sdk", "uuid": "c", "sessionId": "s",
                             "message": {"role": "user", "content": "<task-notification>x</task-notification>"}}) + "\n")
    loads = []
    real = json.loads
    monkeypatch.setattr(web.json, "loads", lambda s, *a, **k: loads.append(1) or real(s, *a, **k))

    web._sdk_file_cache.clear()
    assert web._sdk_turns_in(plain) == 0
    assert loads == []                                          # 표지가 없는 파일은 한 줄도 파싱하지 않는다
    assert web._sdk_turns_in(sdk) == 2
    assert len(loads) == 3                                      # 표지가 있는 줄만(전체 파일이 아니라)
    assert web._sdk_turns_in(sdk) == 2
    assert len(loads) == 3                                      # 안 바뀐 파일은 다시 읽지 않는다


# --- 수동 병합과 색인이 같은 DB 를 동시에 쓰지 않는다 ------------------------------------------------

def test_manual_archive_sync_waits_for_the_index_lock(monkeypatch):
    from fastapi import HTTPException
    assert web._index_lock.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as e:
            web.api_archive_sync()
        assert e.value.status_code == 409 and e.value.detail["code"] == "reindex_already_running"
    finally:
        web._index_lock.release()


# --- 재색인이 임베딩 모델을 두 벌 올리지 않는다 ------------------------------------------------------

def test_reindex_reuses_the_loaded_embedder(tmp_path, monkeypatch):
    from vestige import config as C
    from vestige import embedder as E
    from vestige import indexer as I
    from vestige import proclock as P
    from vestige.vectorindex import VectorIndex
    model = next(iter(web._EMBED_ALLOW))
    built = []

    class Emb:
        def __init__(self, name):
            self.model_name = name
            built.append(name)

    class Free:
        def acquire(self, *a, **k): return True
        def release(self): pass

    loaded = Emb(model)
    built.clear()
    monkeypatch.setattr(E, "Embedder", Emb)
    monkeypatch.setattr(C, "write_config", lambda *a, **k: None)         # 사용자 설정 파일을 건드리지 않는다
    monkeypatch.setattr(P, "IndexLock", lambda *a, **k: Free())
    monkeypatch.setattr(P, "is_locked", lambda *a, **k: False)
    monkeypatch.setattr(I, "index_all", lambda *a, **k: 0)
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    monkeypatch.setattr(web, "make_index", lambda: VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"))
    monkeypatch.setitem(web._state, "embedder", loaded)
    monkeypatch.setitem(web._reindex_state, "running", False)

    assert web.api_reindex({"model": model})["ok"] is True
    for _ in range(100):
        if not web._reindex_state["running"] and web._reindex_state["msg"].startswith(("완료", "오류")):
            break
        time.sleep(0.05)
    assert web._reindex_state["msg"].startswith("완료"), web._reindex_state["msg"]
    assert built == [], f"이미 올라간 모델이 있는데 새로 만들었다: {built}"
    assert web._state["embedder"] is loaded
