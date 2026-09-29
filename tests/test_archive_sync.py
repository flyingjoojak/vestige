"""아카이브 export/import — 텍스트만 옮겨 없는 세션을 병합(벡터 제외)."""

from __future__ import annotations

from types import SimpleNamespace

from vestige import archive_sync as A
from vestige.models import Turn
from vestige.store import ArchiveDB


def _turn(tid: str, sid: str, q: str, a: str) -> Turn:
    return Turn(id=tid, session_id=sid, uuid=tid, parent_uuid="", timestamp="2026-01-01T00:00",
                project="proj", question=q, answer=a, actions=())


def _seed(db: ArchiveDB, tid: str, sid: str, chunks: list[str], summary: str | None = None) -> None:
    db.upsert_turn(_turn(tid, sid, f"q-{tid}", f"a-{tid}"))
    db.add_chunks([SimpleNamespace(turn_id=tid, index=i, text=t) for i, t in enumerate(chunks)])
    if summary:
        db.set_enrichment(tid, summary, ["tag1", "tag2"])
    db.commit()


def test_export_import_roundtrip(tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    _seed(src, "t1", "s1", ["c-a", "c-b"], summary="요약1")
    _seed(src, "t2", "s2", ["c-c"])
    n = A.export_archive(src, proj, "devA")
    assert n == 2
    assert (proj / A.ARCHIVE_DIRNAME / "devA.ndjson").exists()

    dst = ArchiveDB(tmp_path / "b.db")
    added = A.import_archives(dst, proj, "devB")
    assert added == 2
    assert dst.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
    assert dst.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3
    # 정제(summary/tags)도 함께 이동
    s, tags = dst.get_enrichment("t1")
    assert s == "요약1" and tags == ["tag1", "tag2"]
    # 키워드(FTS) 인덱스도 채워짐 → 검색 가능
    assert dst.conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == 2


def test_import_skips_existing_and_own(tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    _seed(src, "t1", "s1", ["x"])
    _seed(src, "t2", "s2", ["y"])
    A.export_archive(src, proj, "devA")

    dst = ArchiveDB(tmp_path / "b.db")
    _seed(dst, "t1", "s1", ["x"])          # t1 이미 있음
    added = A.import_archives(dst, proj, "devB")
    assert added == 1                      # t2만 새로
    # 자기 자신 export는 건너뜀
    A.export_archive(dst, proj, "devB")
    assert A.import_archives(dst, proj, "devB") == 0


def test_import_updates_local_with_fuller_peer_turn(tmp_path):
    """superset-wins(#152): 로컬 미완성 턴을 더 완성된 상대 버전으로 갱신 + 청크 교체 + 스테일 벡터 제거."""
    import numpy as np

    from vestige.vectorindex import VectorIndex

    proj = tmp_path / "projects"
    proj.mkdir()

    # 상대(src): 같은 turn id 인데 답변이 김 + 청크 2개.
    src = ArchiveDB(tmp_path / "a.db")
    src.upsert_turn(_turn("t1", "s1", "빌드 고쳐줘", "완성된 긴 답변입니다 도구 실행 결과 포함 상세"))
    src.add_chunks([SimpleNamespace(turn_id="t1", index=0, text="완성된 긴 답변 앞"),
                    SimpleNamespace(turn_id="t1", index=1, text="완성된 긴 답변 뒤")])
    src.commit()
    A.export_archive(src, proj, "devA")

    # 로컬(dst): 같은 t1 인데 답변이 짧음 + 청크 1개 + 그 스테일 벡터.
    dst = ArchiveDB(tmp_path / "b.db")
    dst.upsert_turn(_turn("t1", "s1", "빌드 고쳐줘", "짧음"))
    dst.add_chunks([SimpleNamespace(turn_id="t1", index=0, text="짧음")])
    dst.commit()
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "v.json")
    vi.add(["t1#0"], np.ones((1, 4), dtype=np.float32))
    assert len(vi) == 1

    added = A.import_archives(dst, proj, "devB", vi=vi)
    assert added == 1
    assert dst.get_turn("t1").answer.startswith("완성된 긴 답변")          # 더 완성으로 갱신
    assert dst.conn.execute("SELECT COUNT(*) FROM chunks WHERE turn_id='t1'").fetchone()[0] == 2  # 청크 교체
    assert "t1#0" not in set(vi.keys())                                   # 스테일 벡터 제거 → backfill 재임베딩

    # 재실행: 이제 로컬==상대 → 갱신 안 함(불필요 재작업 방지)
    assert A.import_archives(dst, proj, "devB", vi=vi) == 0


def test_import_no_dir(tmp_path):
    dst = ArchiveDB(tmp_path / "b.db")
    assert A.import_archives(dst, tmp_path / "nope", "devB") == 0


def test_export_import_preserves_source(tmp_path):
    # #153: 병합으로 넘어온 codex 턴이 상대에서 claude-code 로 라벨되던 버그 회귀 방지.
    proj = tmp_path / "projects"
    proj.mkdir()
    src = ArchiveDB(tmp_path / "a.db")
    src.upsert_turn(_turn("t1", "s1", "q", "a"), source="codex",
                    source_file="/home/me/.codex/sessions/2026/01/01/rollout-x.jsonl")
    src.upsert_turn(_turn("t2", "s2", "q", "a"))   # 기본 claude-code
    src.commit()
    A.export_archive(src, proj, "devA")

    dst = ArchiveDB(tmp_path / "b.db")
    assert A.import_archives(dst, proj, "devB") == 2
    # codex 턴은 codex 로, source_file 까지 보존(재개·재색인에 필요).
    assert dst.session_source("s1") == (
        "codex", "/home/me/.codex/sessions/2026/01/01/rollout-x.jsonl", "proj")
    # 기본 소스 턴은 claude-code 유지.
    assert dst.session_source("s2")[0] == "claude-code"


def test_import_old_snapshot_without_source(tmp_path):
    # 하위호환: source 도입 전 스냅샷(t 11칸)은 claude-code 로 안전하게 import.
    import json
    proj = tmp_path / "projects"
    (proj / A.ARCHIVE_DIRNAME).mkdir(parents=True)
    old = {"t": ["t9", "s9", "u9", "", "2026-01-01T00:00", "proj", "q", "a", "[]", None, None],
           "c": [[0, "chunk"]]}
    (proj / A.ARCHIVE_DIRNAME / "devA.ndjson").write_text(
        json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    dst = ArchiveDB(tmp_path / "b.db")
    assert A.import_archives(dst, proj, "devB") == 1
    assert dst.session_source("s9")[0] == "claude-code"


# --- 정리 상태(제목·접힘) 기기 간 동기화 (#233) ---------------------------

def _two_devices(tmp_path):
    """같은 대화를 가진 기기 둘. 스냅샷 폴더는 공유한다(Syncthing 흉내)."""
    proj = tmp_path / "projects"
    a, b = ArchiveDB(tmp_path / "a.db"), ArchiveDB(tmp_path / "b.db")
    for db in (a, b):
        _seed(db, "s1:u1", "s1", ["본문"])
        _seed(db, "s1:u2", "s1", ["본문2"])
    return a, b, proj


def _sync(src, src_id, dst, dst_id, proj):
    A.export_archive(src, proj, src_id)
    return A.import_archives(dst, proj, dst_id, log_fn=lambda *_: None)


def test_session_title_syncs_between_devices(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "내가 지은 제목")
    _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") == "내가 지은 제목"


def test_cleared_title_does_not_come_back(tmp_path):
    """제목을 지우면 상대의 옛 제목이 되살아나면 안 된다.

    행을 지우는 방식이면 상대에 남은 제목이 다시 이긴다 — #228 과 같은 문제.
    빈 제목을 시각과 함께 남겨 '늦게 바꾼 쪽이 이김'에 태운다.
    """
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "옛 제목")
    _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") == "옛 제목"

    a.set_session_title("s1", None)              # A 에서 지움
    _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") is None         # B 에도 반영

    _sync(b, "devB", a, "devA", proj)            # 되돌아와도
    assert a.session_title("s1") is None         # 되살아나지 않는다


def test_newer_title_wins_both_directions(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "먼저")
    _sync(a, "devA", b, "devB", proj)
    b.set_session_title("s1", "나중")            # B 가 더 늦게 바꿈
    _sync(b, "devB", a, "devA", proj)
    assert a.session_title("s1") == "나중"

    _sync(a, "devA", b, "devB", proj)            # 옛 기록이 다시 와도 안 밀린다
    assert b.session_title("s1") == "나중"


def test_fold_and_unfold_sync(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.hide_turns(["s1:u1"]); a.commit()
    _sync(a, "devA", b, "devB", proj)
    assert b.hidden_turn_ids() == {"s1:u1"}

    a.unhide_turns(["s1:u1"])                    # 펼침도 전해져야 한다
    _sync(a, "devA", b, "devB", proj)
    assert b.hidden_turn_ids() == set()

    _sync(b, "devB", a, "devA", proj)            # 되돌아와도 다시 접히지 않는다
    assert a.hidden_turn_ids() == set()


def test_old_snapshot_without_meta_still_imports(tmp_path):
    """제목·접힘 줄이 없는 옛 스냅샷도 그대로 읽힌다(하위호환)."""
    a, b, proj = _two_devices(tmp_path)
    A.export_archive(a, proj, "devA")
    p = proj / A.ARCHIVE_DIRNAME / "devA.ndjson"
    kept = [l for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and '"t"' in l]
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    c = ArchiveDB(tmp_path / "c.db")
    assert A.import_archives(c, proj, "devC", log_fn=lambda *_: None) == 2
