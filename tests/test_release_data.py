"""v0.4.0 최종 풀리뷰(DB) 수정의 회귀 검사 - 데이터 정확성."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from vestige import archive_sync as A
from vestige.models import Turn
from vestige.store import ArchiveDB


def _turn(tid, q="질문", a="답"):
    return Turn(id=tid, session_id="s1", uuid=tid, parent_uuid="", timestamp="2026-01-01T00:00",
                project="p", question=q, answer=a, actions=())


def _chunk(tid, idx, text):
    return SimpleNamespace(turn_id=tid, index=idx, text=text)


def test_changed_chunk_text_drops_its_embed_hash(tmp_path):
    """텍스트가 바뀐 청크는 옛 해시(또는 해시 없음)로 '벡터 그대로 OK' 판정되면 안 된다.

    해시 없는 0.3.1 청크의 텍스트만 먼저 커밋하고 임베딩 전에 앱이 꺼지면, 다음 회차가
    텍스트 비교로 옛 벡터를 새 텍스트의 것으로 확정했다. NULL 이 아닌 '' 로 둬서 어떤 해시와도 안 맞게 한다.
    """
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("t1"), source_file=None)
    db.add_chunks([_chunk("t1", 0, "T1")])
    assert db.chunk_state("t1")[0] == ("T1", None)          # 0.3.1 에서 올라온 청크: 해시 없음
    db.add_chunks([_chunk("t1", 0, "T2")])
    assert db.chunk_state("t1")[0] == ("T2", "")

    db.set_embed_hashes([("t1#0", "h")])
    db.add_chunks([_chunk("t1", 0, "T2")])                   # 같은 텍스트면 해시를 지키고
    assert db.chunk_state("t1")[0] == ("T2", "h")
    db.add_chunks([_chunk("t1", 0, "T3")])                   # 바뀌면 지운다
    assert db.chunk_state("t1")[0] == ("T3", "")


def test_vec_reset_is_all_or_nothing(tmp_path):
    """모델 교체 초기화가 중간에 끊겨도 'vec 은 비었는데 vkeys 는 남은' 상태가 되지 않는다."""
    pytest.importorskip("sqlite_vec")
    from vestige.vectorindex import SqliteVecIndex
    vi = SqliteVecIndex(tmp_path / "v.db")
    vi.add(["t1#0", "t1#1"], np.ones((2, 4), dtype=np.float32))
    real = vi.conn

    class Boom:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if "DELETE FROM vmeta" in sql:
                raise RuntimeError("끊김")
            return real.execute(sql, *a)

    vi.conn = Boom()
    with pytest.raises(RuntimeError):
        vi.reset()
    vi.conn = real
    assert real.execute("SELECT COUNT(*) FROM vec").fetchone()[0] == 2     # DROP 도 되돌려졌다
    assert len(vi) == 2


@pytest.mark.parametrize("summary,tags", [("요약", "{깨진 json"), (["리스트"], "[]"), ("요약", '{"a": 1}')])
def test_import_rejects_a_bad_enrichment_before_writing_the_turn(tmp_path, summary, tags):
    """요약·태그가 이상한 줄은 턴을 쓰기 전에 걸러야 한다(턴만 상대 판본으로 반쪽 커밋되지 않게)."""
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    for tid in ("bad", "good"):
        src.upsert_turn(_turn(tid), source_file=None)
        src.add_chunks([_chunk(tid, 0, "c")])
        src.set_enrichment(tid, "요약", ["x"])
    src.commit()
    A.export_archive(src, proj, "devA")
    path = proj / A.ARCHIVE_DIRNAME / "devA.ndjson"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("t", [None])[0] == "bad":
            rec["t"][9], rec["t"][10] = summary, tags
        lines.append(json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dst = ArchiveDB(tmp_path / "b.db")
    logs = []
    assert A.import_archives(dst, proj, "devB", log_fn=logs.append) == 1
    ids = {r[0] for r in dst.conn.execute("SELECT id FROM turns")}
    assert ids == {"good"}, ids
    assert any("bad" in m or "건너뜀" in m for m in logs)


def _two_peer_files(tmp_path, n_each):
    proj = tmp_path / "projects"
    proj.mkdir()
    for dev in ("devA", "devC"):
        src = ArchiveDB(tmp_path / f"{dev}.db")
        for i in range(n_each):
            tid = f"{dev}-{i}"
            src.upsert_turn(_turn(tid, a="긴 답변 " * 5), source_file=None)
            src.add_chunks([_chunk(tid, 0, f"{tid} 청크")])
        src.commit()
        A.export_archive(src, proj, dev)
    return proj


def test_import_commits_per_peer_file(tmp_path):
    """상대 파일을 다 읽을 때까지 쓰기 잠금을 쥐면 그동안 앱의 접기·제목 저장이 막힌다 - 파일마다 커밋."""
    proj = _two_peer_files(tmp_path, 2)
    dst = ArchiveDB(tmp_path / "b.db")
    commits = []
    dst.conn.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    assert A.import_archives(dst, proj, "devB") == 4
    assert len(commits) >= 2, commits


def test_import_removes_stale_vectors_in_one_call_per_file(tmp_path):
    """갱신된 턴마다 vi.remove 를 부르면 sqlite-vec 은 매번 커밋+fsync 한다 - 파일당 한 번으로 묶는다."""
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    dst = ArchiveDB(tmp_path / "b.db")
    for i in range(3):
        tid = f"t{i}"
        src.upsert_turn(_turn(tid, a="완성된 긴 답변입니다 도구 실행 결과 포함 상세"), source_file=None)
        src.add_chunks([_chunk(tid, 0, "새 청크")])
        dst.upsert_turn(_turn(tid, a="짧음"), source_file=None)
        dst.add_chunks([_chunk(tid, 0, "짧음")])
    src.commit()
    dst.commit()
    A.export_archive(src, proj, "devA")

    calls = []

    class VI:
        def remove(self, keys):
            calls.append(list(keys))

        def save(self):
            pass

    assert A.import_archives(dst, proj, "devB", vi=VI()) == 3
    assert len(calls) == 1 and sorted(calls[0]) == ["t0#0", "t1#0", "t2#0"], calls
