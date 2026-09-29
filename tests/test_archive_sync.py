"""아카이브 export/import — 텍스트만 옮겨 없는 세션을 병합(벡터 제외)."""

from __future__ import annotations

import sqlite3

import pytest
from types import SimpleNamespace

from vestige import archive_sync as A
from vestige.models import Turn
from vestige.store import ArchiveDB


def _turn(tid: str, sid: str, q: str, a: str) -> Turn:
    return Turn(id=tid, session_id=sid, uuid=tid, parent_uuid="", timestamp="2026-01-01T00:00",
                project="proj", question=q, answer=a, actions=())


def _seed(db: ArchiveDB, tid: str, sid: str, chunks: list[str], summary: str | None = None) -> None:
    db.upsert_turn(_turn(tid, sid, f"q-{tid}", f"a-{tid}"), source_file=None)
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
    src.upsert_turn(_turn("t1", "s1", "빌드 고쳐줘", "완성된 긴 답변입니다 도구 실행 결과 포함 상세"), source_file=None)
    src.add_chunks([SimpleNamespace(turn_id="t1", index=0, text="완성된 긴 답변 앞"),
                    SimpleNamespace(turn_id="t1", index=1, text="완성된 긴 답변 뒤")])
    src.commit()
    A.export_archive(src, proj, "devA")

    # 로컬(dst): 같은 t1 인데 답변이 짧음 + 청크 1개 + 그 스테일 벡터.
    dst = ArchiveDB(tmp_path / "b.db")
    dst.upsert_turn(_turn("t1", "s1", "빌드 고쳐줘", "짧음"), source_file=None)
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
    src.upsert_turn(_turn("t2", "s2", "q", "a"), source_file=None)   # 기본 claude-code
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
    """한 방향 동기화. **반환값은 다시 연 dst 커넥션이다.**

    같은 커넥션으로 검사하면 커밋이 없어도 자기가 쓴 건 보인다(sqlite 자기 쓰기 읽기).
    앱은 호출마다 새 커넥션을 열므로, 커밋이 빠지면 실사용에서만 롤백된다 — 실제로 그
    버그가 있었고 이 헬퍼가 같은 커넥션을 쓰는 바람에 테스트 6개가 전부 통과했다.
    """
    A.export_archive(src, proj, src_id)
    A.import_archives(dst, proj, dst_id, log_fn=lambda *_: None)
    return ArchiveDB(dst.path)


def test_session_title_syncs_between_devices(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "내가 지은 제목")
    b = _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") == "내가 지은 제목"


def test_cleared_title_does_not_come_back(tmp_path):
    """제목을 지우면 상대의 옛 제목이 되살아나면 안 된다.

    행을 지우는 방식이면 상대에 남은 제목이 다시 이긴다 — #228 과 같은 문제.
    빈 제목을 시각과 함께 남겨 '늦게 바꾼 쪽이 이김'에 태운다.
    """
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "옛 제목")
    b = _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") == "옛 제목"

    a.set_session_title("s1", None)              # A 에서 지움
    b = _sync(a, "devA", b, "devB", proj)
    assert b.session_title("s1") is None         # B 에도 반영

    a = _sync(b, "devB", a, "devA", proj)            # 되돌아와도
    assert a.session_title("s1") is None         # 되살아나지 않는다


def test_newer_title_wins_both_directions(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.set_session_title("s1", "먼저")
    b = _sync(a, "devA", b, "devB", proj)
    b.set_session_title("s1", "나중")            # B 가 더 늦게 바꿈
    a = _sync(b, "devB", a, "devA", proj)
    assert a.session_title("s1") == "나중"

    b = _sync(a, "devA", b, "devB", proj)            # 옛 기록이 다시 와도 안 밀린다
    assert b.session_title("s1") == "나중"


def test_fold_and_unfold_sync(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    a.hide_turns(["s1:u1"]); a.commit()
    b = _sync(a, "devA", b, "devB", proj)
    assert b.hidden_turn_ids() == {"s1:u1"}

    a.unhide_turns(["s1:u1"])                    # 펼침도 전해져야 한다
    b = _sync(a, "devA", b, "devB", proj)
    assert b.hidden_turn_ids() == set()

    a = _sync(b, "devB", a, "devA", proj)            # 되돌아와도 다시 접히지 않는다
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


# --- 폴더 동기화 (#233 후반) ---------------------------------------------

def test_folder_and_items_sync(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    f = a.create_folder("모음")
    a.add_to_folder(f, "turn", "s1:u1")
    a.add_to_folder(f, "session", "s1")
    b = _sync(a, "devA", b, "devB", proj)

    got = b.list_folders()
    assert [x["name"] for x in got] == ["모음"]
    assert {(i["kind"], i["ref"]) for i in b.folder_items(got[0]["id"])} == {
        ("turn", "s1:u1"), ("session", "s1")}


def test_folder_id_collision_does_not_merge_two_folders(tmp_path):
    """양쪽에서 각각 만든 폴더는 로컬 id 가 똑같이 1 이어도 서로 다른 폴더다.

    folders.id 는 기기별 AUTOINCREMENT 라, uid 없이 id 로 맞추면 남의 폴더에 덮어쓴다.
    """
    a, b, proj = _two_devices(tmp_path)
    fa, fb = a.create_folder("A의 폴더"), b.create_folder("B의 폴더")
    assert fa == fb == 1                       # 로컬 id 가 같다
    b = _sync(a, "devA", b, "devB", proj)
    assert sorted(x["name"] for x in b.list_folders()) == ["A의 폴더", "B의 폴더"]


def test_deleted_folder_does_not_come_back(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    f = a.create_folder("지울 폴더")
    b = _sync(a, "devA", b, "devB", proj)
    assert len(b.list_folders()) == 1

    a.delete_folder(f)
    b = _sync(a, "devA", b, "devB", proj)
    assert b.list_folders() == []
    a = _sync(b, "devB", a, "devA", proj)          # 되돌아와도
    assert a.list_folders() == []


def test_removed_item_does_not_come_back(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    f = a.create_folder("F")
    a.add_to_folder(f, "turn", "s1:u1")
    a.add_to_folder(f, "turn", "s1:u2")
    b = _sync(a, "devA", b, "devB", proj)
    assert len(b.folder_items(b.list_folders()[0]["id"])) == 2

    a.remove_from_folder(f, "turn", "s1:u1")
    b = _sync(a, "devA", b, "devB", proj)
    bid = b.list_folders()[0]["id"]
    assert {i["ref"] for i in b.folder_items(bid)} == {"s1:u2"}
    a = _sync(b, "devB", a, "devA", proj)
    assert {i["ref"] for i in a.folder_items(f)} == {"s1:u2"}


def test_nested_folder_finds_parent_even_if_order_is_bad(tmp_path):
    """자식이 부모보다 먼저 와도, 한 번 더 돌면 제자리를 찾는다."""
    a, b, proj = _two_devices(tmp_path)
    top = a.create_folder("부모")
    kid = a.create_folder("자식", parent_id=top)
    b = _sync(a, "devA", b, "devB", proj)
    b = _sync(a, "devA", b, "devB", proj)          # 두 번째 회차
    by = {x["name"]: x for x in b.list_folders()}
    assert by["자식"]["parent_id"] == by["부모"]["id"]
    assert a.get_folder(kid)["parent_id"] == top


def test_newer_folder_rename_wins(tmp_path):
    a, b, proj = _two_devices(tmp_path)
    f = a.create_folder("처음")
    b = _sync(a, "devA", b, "devB", proj)
    bid = b.list_folders()[0]["id"]
    b.rename_folder(bid, "나중")
    a = _sync(b, "devB", a, "devA", proj)
    assert a.get_folder(f)["name"] == "나중"
    b = _sync(a, "devA", b, "devB", proj)
    assert b.get_folder(bid)["name"] == "나중"


def test_child_exported_before_parent_still_finds_parent(tmp_path):
    """자식이 부모보다 **먼저 export 되는** 경우. export 에 ORDER BY 가 없어 실제로 생긴다.

    기존 테스트는 부모를 먼저 만들고 재배치하지 않아 이 순서를 못 만들었다. 승자 판정이
    `>=` 라, 발신측이 그 폴더를 다시 안 건드리면 파킹된 자식이 영구 고아가 됐다.
    """
    a, b, proj = _two_devices(tmp_path)
    notes = a.create_folder("Notes")          # 먼저 생성 = export 에서 앞
    work = a.create_folder("Work")
    a.move_folder(notes, work)                # 자식이 더 최근에 바뀜

    b = _sync(a, "devA", b, "devB", proj)
    b = _sync(a, "devA", b, "devB", proj)     # 같은 스냅샷을 한 번 더
    by = {x["name"]: x for x in b.list_folders()}
    assert by["Notes"]["parent_id"] == by["Work"]["id"]


def test_folder_move_after_first_sync_propagates(tmp_path):
    """한 번 동기화한 뒤의 이동도 전해져야 한다.

    move_folder 가 updated_at 을 안 찍으면 승자 판정이 옛 값을 보고 '상대가 더 낡음'으로
    판단해 폴더 이동이 영원히 동기화되지 않는다.
    """
    a, b, proj = _two_devices(tmp_path)
    par = a.create_folder("Parent")
    kid = a.create_folder("Child")
    b = _sync(a, "devA", b, "devB", proj)

    a.move_folder(kid, par)
    b = _sync(a, "devA", b, "devB", proj)
    by = {x["name"]: x for x in b.list_folders()}
    assert by["Child"]["parent_id"] == by["Parent"]["id"]


def test_future_timestamp_record_is_rejected(tmp_path):
    """미래를 주장하는 기록은 반영하지 않는다.

    자르기(clamp)로 하면 더 나쁘다 — 매 import 마다 클램프가 새로 계산돼 사용자의 조작을
    **항상** 이긴다. 실측으로 펼쳐도 재동기화마다 다시 접혔다.
    """
    import json
    a, b, proj = _two_devices(tmp_path)
    d = proj / A.ARCHIVE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "evil.ndjson").write_text(
        json.dumps({"fold": ["s1:u1", 1, 9999999999.0]}) + "\n", encoding="utf-8")
    A.import_archives(b, proj, "devB", log_fn=lambda *_: None)
    assert ArchiveDB(b.path).hidden_turn_ids() == set()


def test_one_broken_line_does_not_drop_the_rest(tmp_path):
    """깨진 줄 하나가 그 뒤 전부를 버리면 안 된다.

    파일 단위로 잡으면 실측으로 5턴 중 3턴이 조용히 사라졌다. 줄 단위로 잡아야 한다.
    """
    a, b, proj = _two_devices(tmp_path)
    for i in range(2, 5):
        _seed(a, f"s1:u{i}", "s1", ["본문"])
    A.export_archive(a, proj, "devA")
    p = proj / A.ARCHIVE_DIRNAME / "devA.ndjson"
    lines = p.read_text(encoding="utf-8").splitlines()
    lines.insert(2, '{"t": [BROKEN')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    want = a.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    msgs: list[str] = []
    A.import_archives(b, proj, "devB", log_fn=msgs.append)
    fresh = ArchiveDB(b.path)
    # 깨진 줄 하나만 빠지고 나머지는 전부 들어와야 한다(A 가 가진 만큼).
    assert fresh.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == want
    assert any(m.startswith("ERROR ") for m in msgs)                             # 조용하지 않다


def test_broken_record_leaves_no_half_written_state(tmp_path):
    """레코드 하나는 전부 반영되거나 하나도 안 되거나여야 한다.

    줄 단위 try 만으로는 부족했다 — 턴을 upsert 한 뒤 청크 배열에서 예외가 나면 턴은
    '완전한' 상태로 커밋되고 청크만 반쪽이 된다. 그러면 upsert_turn 의 완성도 비교가
    '이미 더 완전함'으로 보고 스킵해 **재동기화로도 영영 안 고쳐진다**(실측: 20회 돌려도
    청크가 1개로 고정). SAVEPOINT 로 레코드 단위를 묶어야 한다.
    """
    import json
    a, b, proj = _two_devices(tmp_path)
    d = proj / A.ARCHIVE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    bad = {"t": ["s9:u1", "s9", "u1", "", "2026-01-01", "p", "질문" * 10, "답변" * 10,
                 "[]", None, None, "claude-code", None],
           "c": [[0, "청크0"], [1, "청크1", "칸이 하나 많다"], [2, "청크2"]]}
    ok = {"t": ["s9:u2", "s9", "u2", "", "2026-01-01", "p", "q2", "a2",
                "[]", None, None, "claude-code", None], "c": [[0, "청크"]]}
    (d / "peer.ndjson").write_text(
        json.dumps(bad, ensure_ascii=False) + "\n" + json.dumps(ok, ensure_ascii=False) + "\n",
        encoding="utf-8")

    A.import_archives(b, proj, "devB", log_fn=lambda *_: None)
    fresh = ArchiveDB(b.path)
    q = fresh.conn.execute
    assert q("SELECT 1 FROM turns WHERE id=?", ("s9:u1",)).fetchone() is None    # 흔적 없음
    assert q("SELECT COUNT(*) FROM chunks WHERE turn_id=?", ("s9:u1",)).fetchone()[0] == 0
    assert q("SELECT 1 FROM turns WHERE id=?", ("s9:u2",)).fetchone() is not None  # 정상은 들어감


def test_reparent_of_already_parented_folder_propagates(tmp_path):
    """**이미 부모가 있는** 폴더를 다른 부모로 옮기는 경우.

    앞의 test_folder_move_after_first_sync_propagates 는 허수였다 — '최상위 → 부모 밑'
    이동이라 apply_folder 의 needs_parent 우회(parent_id IS NULL)가 먼저 걸려,
    move_folder 의 updated_at 스탬프를 빼도 통과했다. 두 수정이 서로의 부재를 가렸다.
    여기서는 parent_id 가 이미 차 있어 우회가 안 걸리므로 updated_at 만으로 판정된다.
    """
    a, b, proj = _two_devices(tmp_path)
    p1, p2 = a.create_folder("부모1"), a.create_folder("부모2")
    kid = a.create_folder("자식", parent_id=p1)
    b = _sync(a, "devA", b, "devB", proj)
    by = {x["name"]: x for x in b.list_folders()}
    assert by["자식"]["parent_id"] == by["부모1"]["id"]     # 먼저 부모1 밑에 자리잡는다

    a.move_folder(kid, p2)                                  # 부모1 -> 부모2 로 재이동
    b = _sync(a, "devA", b, "devB", proj)
    by = {x["name"]: x for x in b.list_folders()}
    assert by["자식"]["parent_id"] == by["부모2"]["id"]


def test_folder_position_only_change_propagates(tmp_path):
    """부모는 그대로, 형제 순서만 바뀌는 경우도 전해져야 한다."""
    a, b, proj = _two_devices(tmp_path)
    f1, f2 = a.create_folder("가"), a.create_folder("나")
    b = _sync(a, "devA", b, "devB", proj)
    a.move_folder(f2, None, before_id=f1)                   # '나' 를 '가' 앞으로
    b = _sync(a, "devA", b, "devB", proj)
    assert [x["name"] for x in b.list_folders()][:2] == ["나", "가"]


@pytest.mark.parametrize("kind", ["title", "fold", "folder", "fitem", "folder_x", "fitem_x"])
def test_future_timestamp_rejected_on_every_record_kind(tmp_path, kind):
    """미래 시각 가드는 **여섯 경로 전부**에 있어야 한다.

    한 경로만 검사하면 나머지 다섯 곳에서 가드가 빠져도 아무도 모른다
    (실측: apply_folder 의 가드만 지워도 61개 테스트가 전부 통과했다).
    """
    import json
    FUT = 9999999999.0
    a, b, proj = _two_devices(tmp_path)
    f = a.create_folder("미리")
    a.add_to_folder(f, "turn", "s1:u1")
    b = _sync(a, "devA", b, "devB", proj)
    # **실제로 존재하는 uid** 를 써야 가드까지 도달한다. 없는 uid 면 그 앞에서 걸러져
    # 가드를 지워도 테스트가 통과한다(실측: 4개 경로가 그렇게 비어 있었다).
    uid = b.conn.execute("SELECT uid FROM folders LIMIT 1").fetchone()["uid"]
    payload = {
        "title": ["s1", "공격 제목", FUT],
        "fold": ["s1:u1", 1, FUT],
        "folder": [uid, "이름 바뀜", None, 9.0, FUT],
        "fitem": [uid, "turn", "s1:u2", None, 9.0, FUT],
        "folder_x": [uid, FUT],
        "fitem_x": [uid, "turn", "s1:u1", FUT],
    }[kind]
    before = _folder_state(b)

    d = proj / A.ARCHIVE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "evil.ndjson").write_text(json.dumps({kind: payload}) + "\n", encoding="utf-8")
    A.import_archives(b, proj, "devB", log_fn=lambda *_: None)

    fresh = ArchiveDB(b.path)
    assert _folder_state(fresh) == before          # 폴더·항목이 바뀌지 않았다
    assert fresh.session_title("s1") is None       # 제목도
    assert fresh.hidden_turn_ids() == set()        # 접힘도


def _folder_state(db):
    return (sorted((r["uid"], r["name"], r["parent_id"])
                   for r in db.conn.execute("SELECT uid, name, parent_id FROM folders")),
            sorted(tuple(r) for r in db.conn.execute(
                "SELECT folder_id, kind, ref FROM folder_items")))


def test_attaching_parent_does_not_clobber_local_edits(tmp_path):
    """부모를 뒤늦게 붙일 때 **로컬에서 바꾼 이름·위치를 덮으면 안 된다.**

    needs_parent 우회를 '시각 가드 전체 무력화'로 만들면, 파킹된 동안 사용자가 지은 이름이
    옛 레코드로 되돌아가고 updated_at 까지 과거로 박힌다(실측). 부모 링크만 메워야 한다.

    이 검사는 한 번 유실된 수정을 다시 지킨다 — 고쳐놓고 커밋 전에 덮여 사라진 적이 있다.
    """
    b = ArchiveDB(tmp_path / "b.db")
    b.apply_folder("uidN", "옛 이름", "uidW", 1.0, 100.0)      # 부모가 없어 최상위로 파킹
    b.commit()
    fid = b.conn.execute("SELECT id FROM folders WHERE uid='uidN'").fetchone()["id"]
    b.rename_folder(fid, "내가 지은 이름")                      # 파킹된 동안 로컬에서 변경

    b.apply_folder("uidW", "부모", None, 1.0, 100.0)           # 이제 부모가 도착
    b.apply_folder("uidN", "옛 이름", "uidW", 1.0, 100.0)      # 같은 옛 레코드 재전송
    b.commit()

    f = ArchiveDB(b.path).conn.execute(
        "SELECT name, parent_id, updated_at FROM folders WHERE uid='uidN'").fetchone()
    assert f["parent_id"] is not None          # 부모는 붙었고
    assert f["name"] == "내가 지은 이름"        # 이름은 지켜졌고
    assert f["updated_at"] > 100.0             # 시각도 안 되돌아갔다


def test_rollback_failure_aborts_the_file_instead_of_committing_half(tmp_path):
    """ROLLBACK TO 자체가 실패하면 **삼키지 말고 이 파일을 중단**해야 한다.

    삼키면 그 레코드의 반쪽 쓰기가 남은 채로 commit() 에 실려 가고, 로그는 '건너뛰었다'고
    거짓말을 한다. SAVEPOINT 장치가 막으려던 바로 그 상태가 좁은 경로로 재발한다.
    """
    import json

    class FlakyConn:
        """ROLLBACK TO 를 한 번만 실패시키는 껍데기."""
        def __init__(self, inner):
            self._inner, self._failed = inner, False
        def execute(self, sql, *a):
            if sql.startswith("ROLLBACK TO") and not self._failed:
                self._failed = True
                raise sqlite3.OperationalError("모의 롤백 실패")
            return self._inner.execute(sql, *a)
        def __getattr__(self, k):
            return getattr(self._inner, k)

    a, b, proj = _two_devices(tmp_path)
    d = proj / A.ARCHIVE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    bad = {"t": ["s9:u1", "s9", "u1", "", "2026-01-01", "p", "질문", "답변",
                 "[]", None, None, "claude-code", None],
           "c": [[0, "청크"], [1, "청크", "칸이 많다"]]}
    ok = {"t": ["s9:u2", "s9", "u2", "", "2026-01-01", "p", "q", "a",
                "[]", None, None, "claude-code", None], "c": []}
    (d / "peer.ndjson").write_text(
        json.dumps(bad, ensure_ascii=False) + "\n" + json.dumps(ok, ensure_ascii=False) + "\n",
        encoding="utf-8")

    real = b.conn
    b.conn = FlakyConn(real)
    msgs: list[str] = []
    A.import_archives(b, proj, "devB", log_fn=msgs.append)
    b.conn = real

    fresh = ArchiveDB(b.path)
    # 반쪽 레코드가 커밋되면 안 된다. 이 파일은 통째로 포기하고 다음 회차가 다시 읽는다.
    assert fresh.conn.execute("SELECT 1 FROM turns WHERE id=?", ("s9:u1",)).fetchone() is None
    assert any("롤백 실패" in m for m in msgs)          # 조용히 넘어가지 않는다
