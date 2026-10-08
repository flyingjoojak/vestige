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


# --- FTS: 턴 하나 지우고 다시 넣을 때 FTS 전체를 훑지 않는다 -----------------------------------------

def _fts_deletes(db, fn):
    seen = []
    db.conn.set_trace_callback(lambda sql: seen.append(sql) if "DELETE FROM turns_fts WHERE" in sql else None)
    try:
        fn()
    finally:
        db.conn.set_trace_callback(None)
    return seen


def test_fts_is_updated_by_rowid_not_by_scanning(tmp_path):
    """turn_id 는 FTS 의 UNINDEXED 칸이라 `DELETE ... WHERE turn_id=?` 는 매번 FTS 전체를 훑는다
    (5천 턴 38ms, 5만 턴 370ms - 재색인이 턴 수의 제곱으로 느려지고 그동안 쓰기 잠금을 쥔다)."""
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn("t1", a="사과 이야기"), source_file=None)
    seen = _fts_deletes(db, lambda: db.upsert_turn(_turn("t1", a="바나나 이야기가 더 길게"), source_file=None))
    assert seen and all("rowid" in s and "turn_id" not in s for s in seen), seen
    assert [t for t, _ in db.keyword_search("바나나")] == ["t1"]
    assert db.keyword_search("사과") == []                  # 옛 본문은 빠졌다

    seen = _fts_deletes(db, lambda: db.delete_turns(["t1"]))
    assert seen and all("rowid" in s and "turn_id" not in s for s in seen), seen
    assert db.keyword_search("바나나") == []


def test_fts_rowid_map_is_built_for_a_db_that_already_has_fts_rows(tmp_path):
    """0.3.1 DB 는 매핑 표가 없다 - 열 때 기존 FTS 행으로 채워서, 다시 넣어도 중복이 생기지 않아야 한다."""
    p = tmp_path / "a.db"
    db = ArchiveDB(p)
    for i in range(3):
        db.upsert_turn(_turn(f"t{i}", a=f"포도 {i}"), source_file=None)
    db.conn.execute("DROP TABLE turns_fts_map")
    db.conn.execute("PRAGMA user_version = 16")
    db.conn.commit()
    db.close()

    db = ArchiveDB(p)                                            # 마이그레이션 0017
    assert db.conn.execute("SELECT COUNT(*) FROM turns_fts_map").fetchone()[0] == 3
    db.upsert_turn(_turn("t1", a="포도 1 에 딸기가 더"), source_file=None)
    ids = [t for t, _ in db.keyword_search("포도")]
    assert sorted(ids) == ["t0", "t1", "t2"], ids                # t1 이 두 번 나오지 않는다
    assert db.conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == 3
    assert [t for t, _ in db.keyword_search("딸기가")] == ["t1"]


def test_rebuild_fts_keeps_the_map_in_step(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    for i in range(3):
        db.upsert_turn(_turn(f"t{i}", a=f"수박 {i}"), source_file=None)
    assert db.rebuild_fts() == 3
    assert db.conn.execute("SELECT COUNT(*) FROM turns_fts_map").fetchone()[0] == 3
    db.upsert_turn(_turn("t0", a="수박 0 그리고 멜론"), source_file=None)
    assert db.conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == 3


def test_import_of_one_big_peer_file_commits_in_batches(tmp_path):
    """상대 기기가 하나뿐이면(보통) 그 파일 하나가 전체다. 파일 끝에서만 커밋하면 파서 버전 전환처럼 모든 턴이
    갱신되는 첫 동기화에서 수십 초 쓰기 잠금을 쥐어 접기·제목 저장이 막힌다 - 일정 턴마다도 커밋한다."""
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    dst = ArchiveDB(tmp_path / "b.db")
    n = 450
    for i in range(n):
        tid = f"t{i}"
        src.upsert_turn(_turn(tid, a="완성된 긴 답변입니다 도구 실행 결과 포함 상세"), source_file=None)
        src.add_chunks([_chunk(tid, 0, "새 청크")])
        dst.upsert_turn(_turn(tid, a="짧음"), source_file=None)
        dst.add_chunks([_chunk(tid, 0, "짧음")])
    src.commit()
    dst.commit()
    A.export_archive(src, proj, "devA")

    removes = []

    class VI:
        def remove(self, keys):
            removes.append(len(keys))

        def save(self):
            pass

    commits = []
    dst.conn.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    assert A.import_archives(dst, proj, "devB", vi=VI()) == n
    assert len(commits) >= 3, commits                  # 배치마다 + 마지막
    assert sum(removes) == n and len(removes) >= 3, removes   # 벡터 삭제도 묶음으로, 커밋 앞에서


def test_fts_map_heals_rows_written_by_an_older_app(tmp_path):
    """업데이트 도중 옛 버전 프로세스(스케줄러·MCP)가 같은 DB 에 색인을 쓰면 FTS 행이 표 없이 생긴다.
    다시 열 때 한 번 바로잡는다: 표에 없는 행을 등록하고, 같은 턴의 옛 행(중복)은 지운다."""
    from vestige import store
    p = tmp_path / "a.db"
    db = ArchiveDB(p)
    for t in ("t1", "t2"):
        db.upsert_turn(_turn(t, a=f"사과 {t}"), source_file=None)
    # 옛 앱처럼: turn_id 로 지우고 rowid 를 지정하지 않고 다시 넣는다(표는 그대로) + 새 턴은 표 없이 넣는다
    db.conn.execute("DELETE FROM turns_fts WHERE turn_id='t1'")
    db.conn.execute("INSERT INTO turns_fts(turn_id,text) VALUES('t1','사과 바나나 t1')")
    db.upsert_turn(_turn("t7", a="딸기 t7"), source_file=None)                 # 저장이 도중에 실패해 FTS 행이 아예 없는 턴
    db.conn.execute("DELETE FROM turns_fts_map WHERE turn_id='t7'")
    db.conn.execute("DELETE FROM turns_fts WHERE turn_id='t7'")
    db.upsert_turn(_turn("t9", a="멜론 t9"), source_file=None)
    db.conn.execute("DELETE FROM turns_fts_map WHERE turn_id='t9'")           # t9 는 표 없이 들어온 상태
    db.conn.execute("DELETE FROM turns_fts WHERE turn_id='t9'")
    db.conn.execute("INSERT INTO turns_fts(turn_id,text) VALUES('t9','멜론 t9')")
    db.conn.commit()
    db.close()

    store._fts_healed.clear()                                                  # 새 프로세스가 처음 여는 것처럼
    db = ArchiveDB(p)
    q = lambda s: db.conn.execute(s).fetchone()[0]
    assert q("SELECT COUNT(*) FROM turns_fts") == q("SELECT COUNT(*) FROM turns_fts_map") == 4
    assert q("SELECT COUNT(*) FROM turns_fts WHERE rowid NOT IN (SELECT rid FROM turns_fts_map)") == 0
    assert [t for t, _ in db.keyword_search("바나나")] == ["t1"]
    assert [t for t, _ in db.keyword_search("멜론")] == ["t9"]
    assert [t for t, _ in db.keyword_search("딸기")] == ["t7"]                  # 빠졌던 턴도 다시 찾아진다
    db.upsert_turn(_turn("t9", a="멜론 t9 그리고 수박이 더 길게"), source_file=None)   # 다시 넣어도 중복이 안 생긴다
    assert q("SELECT COUNT(*) FROM turns_fts") == 4
    assert [t for t, _ in db.keyword_search("수박이")] == ["t9"]
