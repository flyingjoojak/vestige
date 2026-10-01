"""웹 UI 회귀 테스트: _HTML 정의 순서 버그(index가 NameError) 방지."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # 웹 전용 의존성 없으면 스킵

from vestige import web  # noqa: E402


def test_index_html_fallback():
    # 빌드된 프론트가 없을 때 서빙되는 인라인 HTML 폴백 회귀 테스트.
    assert "<!doctype html>" in web._HTML.lower()
    assert "Vestige" in web._HTML


def test_index_returns_response():
    # dist 유무와 무관하게 라우트가 응답 객체를 반환.
    assert web.index() is not None


def test_index_html_no_store():
    # index.html은 no-store로 서빙돼야 한다(업데이트로 청크 해시가 바뀌어도 캐시된 옛 엔트리가
    # 사라진 청크를 import하는 "Failed to fetch dynamically imported module" 방지).
    resp = web.index()
    assert "no-store" in resp.headers.get("cache-control", "").lower()


def test_config_put_rejects_invalid_index_values():
    # 잘못된 INDEX_MODE/INDEX_TIME은 저장(write_config) 전에 거부돼야 한다(조용히 스케줄 색인이 멈추지 않게).
    r1 = web.api_config_put({"VESTIGE_INDEX_MODE": "bogus"})
    assert r1["ok"] is False and r1["code"] == "invalid_config_value"
    assert "VESTIGE_INDEX_MODE" in r1["invalid"]

    r2 = web.api_config_put({"VESTIGE_INDEX_TIME": "25:99"})
    assert r2["ok"] is False and "VESTIGE_INDEX_TIME" in r2["invalid"]

    r3 = web.api_config_put({"VESTIGE_INDEX_TIME": "9"})   # 콜론 없음
    assert r3["ok"] is False and "VESTIGE_INDEX_TIME" in r3["invalid"]


def test_hit_to_dict_shape():
    from vestige.models import Action, Turn

    t = Turn(id="s1:u1", session_id="s1abcdef", uuid="u1", parent_uuid=None,
             timestamp="2026-07-24T00:00:00Z", project="p", question="질문",
             answer="답변", actions=(Action("Edit", "x.py"),))

    class H:
        turn = t
        score = 0.1
        cosine = 0.87
        sources = ("semantic", "keyword")
        summary = "요약"
        tags = ("t1",)
        thread = ()

    d = web._hit_to_dict(H())
    assert d["question"] == "질문"
    assert d["actions"] == ["Edit(x.py)"]
    assert d["sources"] == ["semantic", "keyword"]
    assert d["cosine"] == 0.87


def test_api_sessions_aggregates_without_n_plus_1(tmp_path, monkeypatch):
    """세션 목록: 윈도우 함수 단일 쿼리로 세션별 턴수·대표 헤드라인·최근순 정렬을 올바로 산출(N+1 제거 후 형태 보존)."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    def _t(tid, sid, ts, q):
        return Turn(id=tid, session_id=sid, uuid=tid, parent_uuid=None,
                    timestamp=ts, project="p", question=q, answer="a", actions=())

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_t("A:u0", "Asession", "2026-07-24T00:00:00Z", "A첫질문"), source_file=None)
    db.upsert_turn(_t("A:u1", "Asession", "2026-07-24T00:01:00Z", "A둘째"), source_file=None)
    db.upsert_turn(_t("B:u0", "Bsession", "2026-07-24T05:00:00Z", "B첫질문"), source_file=None)  # 더 나중에 끝남
    db.commit()

    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    out = web.api_sessions()["sessions"]
    assert [s["session"] for s in out] == ["Bsession", "Asession"]   # ended DESC
    by = {s["session"]: s for s in out}
    assert by["Asession"]["count"] == 2 and by["Asession"]["headline"] == "A첫질문"   # 첫 턴 헤드라인
    assert by["Bsession"]["count"] == 1 and by["Bsession"]["headline"] == "B첫질문"


def test_turns_to_markdown_basic():
    # 질문/행동/답변이 순서대로 markdown 섹션으로 직렬화되는지(#190).
    md = web._turns_to_markdown("sess1", "p", [
        {"timestamp": "2026-07-24T00:00:00Z", "question": "질문1",
         "actions": ["Edit(x.py)"], "answer": "답변1"},
    ])
    assert "# 세션 sess1" in md
    assert "`p`" in md
    assert "## 2026-07-24T00:00:00Z" in md
    assert "질문1" in md and "- Edit(x.py)" in md and "답변1" in md


def test_api_session_export_returns_markdown_attachment(tmp_path, monkeypatch):
    """turns만 있으면(원문·미러 유무 무관) markdown 첨부파일로 내보내기 가능(#190)."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="legacy:u0", session_id="legacysession", uuid="u0",
                         parent_uuid=None, timestamp="2026-07-24T00:00:00Z",
                         project="myproj", question="질문", answer="답변", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    resp = web.api_session_export(id="legacysession")
    assert "text/markdown" in resp.media_type
    assert "legacysession.md" in resp.headers["content-disposition"]
    body = resp.body.decode("utf-8")
    assert "myproj" in body and "질문" in body and "답변" in body


def test_api_session_export_404_when_no_turns(tmp_path, monkeypatch):
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    with pytest.raises(web.HTTPException) as ei:
        web.api_session_export(id="nosuchsession")
    assert ei.value.status_code == 404


def test_api_session_export_400_for_invalid_id():
    with pytest.raises(web.HTTPException) as ei:
        web.api_session_export(id="../../etc/passwd")
    assert ei.value.status_code == 400


def test_api_hide_unhide_turn_roundtrip(tmp_path, monkeypatch):
    """턴 접기/펼치기(#128) - 접어도 세션 뷰엔 hidden 표시로 남고(제자리 펼치기용), 펼치면 해제된다."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()), source_file=None)
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="q2", answer="a2", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    r = web.api_hide({"turn_id": "s1:u1"})
    assert r["ok"] is True and r["hidden"] == 1
    turns = web.api_session(id="s1")["turns"]
    assert [t["id"] for t in turns] == ["s1:u1", "s1:u2"]        # 자리엔 그대로 남고
    assert [t["hidden"] for t in turns] == [True, False]         # 접힘 표시만 붙는다

    web.api_unhide({"turn_id": "s1:u1"})
    assert [t["hidden"] for t in web.api_session(id="s1")["turns"]] == [False, False]


def test_api_hide_session_keeps_it_listed_as_folded(tmp_path, monkeypatch):
    """세션 전체를 접어도 목록에선 사라지지 않는다 - 사라지면 다시 펼칠 길이 없어진다."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()), source_file=None)
    db.upsert_turn(Turn(id="s2:u1", session_id="s2", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q2", answer="a2", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"session_id": "s1"})
    by = {r["session"]: r for r in web.api_sessions()["sessions"]}
    assert set(by) == {"s1", "s2"}                                  # 둘 다 목록에 남고
    assert by["s1"]["hidden_count"] == by["s1"]["count"] == 1       # s1 은 '전부 접힘'으로 구분
    assert by["s2"]["hidden_count"] == 0


def test_session_headline_prefers_unfolded_turn(tmp_path, monkeypatch):
    """세션 대표 제목은 접히지 않은 턴에서 먼저 고른다 - 노이즈라 접은 첫 턴이 계속 제목이면 접은 의미가 없다."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="접을노이즈", answer="a", actions=()), source_file=None)
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="진짜작업", answer="a", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"turn_id": "s1:u1"})   # 시간상 첫 턴을 접음
    row = web.api_sessions()["sessions"][0]
    assert row["headline"] == "진짜작업"          # 접힌 턴 대신 다음 턴이 제목
    assert row["hidden_count"] == 1 and row["count"] == 2

    web.api_hide({"turn_id": "s1:u2"})   # 전부 접히면 고를 게 없으니 접힌 턴에서라도 제목을 낸다
    row = web.api_sessions()["sessions"][0]
    assert row["headline"] == "접을노이즈" and row["hidden_count"] == 2


def test_export_skips_folded_turns(tmp_path, monkeypatch):
    """접힌 턴은 markdown 내보내기에서도 빠진다(검색·지도와 같은 기준)."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="접을질문", answer="a1", actions=()), source_file=None)
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="남길질문", answer="a2", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"turn_id": "s1:u1"})
    body = web.api_session_export(id="s1").body.decode("utf-8")
    assert "남길질문" in body and "접을질문" not in body


def test_api_hidden_lists_recent_first_with_count(tmp_path, monkeypatch):
    """접힘 화면(#128): 세션 묶음이 최근 접은 순 + 배지용 count. limit=0 이면 개수만.

    묶는 방식 자체는 test_folded_groups.py 가 본다. 여기선 순서와 엔드포인트 모양만.
    """
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    for sid, q in (("s1", "먼저접음"), ("s2", "나중접음")):
        db.upsert_turn(Turn(id=f"{sid}:u1", session_id=sid, uuid="u1", parent_uuid=None,
                             timestamp="2026-07-24T01:00:00Z", project="p", question=q, answer="a", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"turn_id": "s1:u1"}); web.api_hide({"turn_id": "s2:u1"})
    # time.time() 해상도로 순서가 흔들리지 않게 접은 시각을 벌려둔다.
    db.conn.execute("UPDATE hidden_turns SET hidden_at=100 WHERE turn_id='s1:u1'")
    db.conn.execute("UPDATE hidden_turns SET hidden_at=200 WHERE turn_id='s2:u1'")
    db.commit()

    r = web.api_hidden()
    assert r["count"] == 2
    assert [g["session_id"] for g in r["sessions"]] == ["s2", "s1"]     # 최근 접은 순
    assert r["sessions"][0]["headline"] == "나중접음"

    assert web.api_hidden(limit=0) == {"sessions": [], "chats": [], "count": 2}   # 배지용 경량 호출

    web.api_unhide({"turn_id": "s1:u1"})                                  # 펼치면 묶음에서 빠진다
    r = web.api_hidden()
    assert r["count"] == 1 and [g["session_id"] for g in r["sessions"]] == ["s2"]


def test_api_hide_rejects_missing_target():
    with pytest.raises(web.HTTPException) as ei:
        web.api_hide({})
    assert ei.value.status_code == 400


def test_api_hide_rejects_unknown_turn_id(tmp_path, monkeypatch):
    """존재하지 않는 turn_id 는 유령 숨김 행을 만들지 않고 404."""
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    with pytest.raises(web.HTTPException) as ei:
        web.api_hide({"turn_id": "no-such-turn"})
    assert ei.value.status_code == 404
    assert db.hidden_turn_ids() == set()   # 유령 행이 생기지 않았다


def test_safe_resume_cwd_rejects_unc_and_missing(tmp_path):
    """세션 로그의 cwd(신뢰 불가)에서 UNC/네트워크·디바이스·없는 경로를 거부(강제 NTLM 인증 등 차단)."""
    from vestige.web import _safe_resume_cwd
    # 거부돼야 하는 것들
    assert _safe_resume_cwd(r"\attacker.example.com\share") is None   # UNC
    assert _safe_resume_cwd("//attacker/share") is None                # UNC(슬래시)
    assert _safe_resume_cwd(r"\?\C:\x") is None                       # 디바이스/확장 경로
    assert _safe_resume_cwd(r"C:\NoSuchDir_zzz_absent") is None        # 없는 폴더
    assert _safe_resume_cwd("") is None and _safe_resume_cwd(None) is None
    # 실재하는 로컬 폴더만 허용
    assert _safe_resume_cwd(str(tmp_path)) == str(tmp_path)


# --- 폴더(#201) ----------------------------------------------------------
def _seed_folder_db(tmp_path, monkeypatch):
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(Turn(id="s1:u1", session_id="s1", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()), source_file=None)
    db.upsert_turn(Turn(id="s2:u1", session_id="s2", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T01:00:00Z", project="p", question="q2", answer="a2", actions=()), source_file=None)
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))
    return db


def test_folder_crud_and_detail(tmp_path, monkeypatch):
    """폴더 생성·중첩·담기 후 상세 조회에 경로/하위/항목이 제대로 실린다."""
    _seed_folder_db(tmp_path, monkeypatch)
    root = web.api_folder_create({"name": "Vestige"})["id"]
    child = web.api_folder_create({"name": "배포", "parent_id": root})["id"]

    web.api_folder_add({"folder_id": child, "turn_id": "s1:u1"})
    web.api_folder_add({"folder_id": child, "session_id": "s2"})

    d = web.api_folder(id=child)
    assert d["folder"]["name"] == "배포"
    assert [p["id"] for p in d["path"]] == [root]                  # 빵부스러기
    assert {i["kind"] for i in d["items"]} == {"turn", "session"}
    assert d["turn_count"] == 2                                    # 턴 1 + 세션 s2 의 턴 1

    assert [f["id"] for f in web.api_folder(id=root)["children"]] == [child]
    web.api_folder_rename({"id": child, "name": "배포 삽질"})
    assert web.api_folder(id=child)["folder"]["name"] == "배포 삽질"


def test_folder_move_rejects_cycle_and_delete_removes_subtree(tmp_path, monkeypatch):
    _seed_folder_db(tmp_path, monkeypatch)
    root = web.api_folder_create({"name": "부모"})["id"]
    child = web.api_folder_create({"name": "자식", "parent_id": root})["id"]

    with pytest.raises(web.HTTPException) as ei:
        web.api_folder_move({"id": root, "parent_id": child})      # 자기 하위로 이동
    assert ei.value.status_code == 400

    assert web.api_folder_delete({"id": root})["deleted"] == 2     # 하위까지 통째로
    assert web.api_folders()["folders"] == []


def test_folder_add_rejects_unknown_turn_and_missing_folder(tmp_path, monkeypatch):
    _seed_folder_db(tmp_path, monkeypatch)
    f = web.api_folder_create({"name": "F"})["id"]

    with pytest.raises(web.HTTPException) as ei:
        web.api_folder_add({"folder_id": f, "turn_id": "no-such-turn"})
    assert ei.value.status_code == 404                              # 유령 항목 방지

    with pytest.raises(web.HTTPException) as ei:
        web.api_folder_add({"folder_id": 9999, "turn_id": "s1:u1"})
    assert ei.value.status_code == 404                              # 없는 폴더

    with pytest.raises(web.HTTPException) as ei:
        web.api_folder_add({"folder_id": f})                        # 대상 미지정
    assert ei.value.status_code == 400


def test_search_scoped_to_folder(tmp_path, monkeypatch):
    """폴더 안에서 검색: 그 폴더가 가리키는 턴만 결과에 나온다."""
    import vestige.web as W

    db = _seed_folder_db(tmp_path, monkeypatch)
    f = db.create_folder("모음")
    db.add_to_folder(f, "turn", "s1:u1")

    captured = {}

    def fake_search(*a, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(W, "run_search", fake_search)
    monkeypatch.setattr(W, "make_index", lambda *a, **k: None)

    W.api_search(q="아무거나", mode="keyword", folder=f)
    assert captured["allow_ids"] == {"s1:u1"}       # 폴더 범위로 제한해 넘어간다

    W.api_search(q="아무거나", mode="keyword")
    assert captured["allow_ids"] is None            # 폴더 미지정이면 전체


def test_folder_endpoints_reject_bad_input_and_unknown_folder(tmp_path, monkeypatch):
    """잘못된 폴더 id 는 500(예상치 못한 오류)이 아니라 400, 없는 폴더는 404."""
    import vestige.web as W

    db = _seed_folder_db(tmp_path, monkeypatch)

    for bad in (None, "abc"):
        with pytest.raises(web.HTTPException) as ei:
            web.api_folder_rename({"id": bad, "name": "x"})
        assert ei.value.status_code == 400

    # 검색에서 없는 폴더를 가리키면 조용한 0건이 아니라 404(지워진 폴더를 든 화면을 드러냄)
    monkeypatch.setattr(W, "make_index", lambda *a, **k: None)
    monkeypatch.setattr(W, "run_search", lambda *a, **kw: [])
    with pytest.raises(web.HTTPException) as ei:
        W.api_search(q="x", mode="keyword", folder=9999)
    assert ei.value.status_code == 404

    f = db.create_folder("빈 폴더")                      # 비어있는 건 정상 응답 0건
    assert W.api_search(q="x", mode="keyword", folder=f)["count"] == 0


def test_folder_item_alias_and_reorder(tmp_path, monkeypatch):
    """폴더 별칭은 그 폴더 안에서만 제목을 바꾸고(원본 불변), 순서는 지정한 대로 유지된다."""
    _seed_folder_db(tmp_path, monkeypatch)
    f = web.api_folder_create({"name": "F"})["id"]
    web.api_folder_add({"folder_id": f, "turn_id": "s1:u1"})
    web.api_folder_add({"folder_id": f, "turn_id": "s2:u1"})

    assert [i["ref"] for i in web.api_folder(id=f)["items"]] == ["s1:u1", "s2:u1"]   # 담은 순

    web.api_folder_item_reorder({"folder_id": f, "order": [
        {"kind": "turn", "ref": "s2:u1"}, {"kind": "turn", "ref": "s1:u1"}]})
    assert [i["ref"] for i in web.api_folder(id=f)["items"]] == ["s2:u1", "s1:u1"]   # 바꾼 순서

    web.api_folder_item_rename({"folder_id": f, "turn_id": "s1:u1", "alias": "내가 붙인 이름"})
    it = next(i for i in web.api_folder(id=f)["items"] if i["ref"] == "s1:u1")
    assert it["headline"] == "내가 붙인 이름"
    assert it["original_headline"] == "q1"                       # 원본 제목은 그대로
    assert web.api_session(id="s1")["turns"][0]["question"] == "q1"   # 대화 자체도 불변

    web.api_folder_item_rename({"folder_id": f, "turn_id": "s1:u1", "alias": ""})   # 비우면 원복
    it = next(i for i in web.api_folder(id=f)["items"] if i["ref"] == "s1:u1")
    assert it["headline"] == "q1"


def test_folder_reorder_rejects_bad_payload(tmp_path, monkeypatch):
    _seed_folder_db(tmp_path, monkeypatch)
    f = web.api_folder_create({"name": "F"})["id"]
    for bad in ({"folder_id": f, "order": "nope"},
                {"folder_id": f, "order": [{"kind": "bogus", "ref": "x"}]},
                {"folder_id": f, "order": [{"kind": "turn"}]}):
        with pytest.raises(web.HTTPException) as ei:
            web.api_folder_item_reorder(bad)
        assert ei.value.status_code == 400


def test_folder_items_report_folded_state(tmp_path, monkeypatch):
    """폴더 항목에도 접힘 상태가 실린다 - 폴더 화면에서 바로 접기/펼치기 하려면 필요.
    세션 항목은 전 턴이 접혔을 때만 '접힘'(세션 목록과 같은 기준)."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = _seed_folder_db(tmp_path, monkeypatch)
    db.upsert_turn(Turn(id="s2:u2", session_id="s2", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T02:00:00Z", project="p", question="q3", answer="a3", actions=()), source_file=None)
    db.commit()
    f = web.api_folder_create({"name": "F"})["id"]
    web.api_folder_add({"folder_id": f, "turn_id": "s1:u1"})
    web.api_folder_add({"folder_id": f, "session_id": "s2"})

    by = {i["ref"]: i for i in web.api_folder(id=f)["items"]}
    assert by["s1:u1"]["hidden"] is False and by["s2"]["hidden"] is False

    web.api_hide({"turn_id": "s1:u1"})
    web.api_hide({"turn_id": "s2:u1"})        # 세션의 일부만 접음
    by = {i["ref"]: i for i in web.api_folder(id=f)["items"]}
    assert by["s1:u1"]["hidden"] is True
    assert by["s2"]["hidden"] is False        # 아직 전부는 아니므로

    web.api_hide({"turn_id": "s2:u2"})        # 나머지도 접으면
    by = {i["ref"]: i for i in web.api_folder(id=f)["items"]}
    assert by["s2"]["hidden"] is True


def test_folder_move_sets_sibling_order(tmp_path, monkeypatch):
    """형제 순서까지 드래그 한 번으로 — before_id 로 '그 앞'에 꽂고, 목록은 그 순서로 나온다."""
    _seed_folder_db(tmp_path, monkeypatch)
    a = web.api_folder_create({"name": "가"})["id"]
    b = web.api_folder_create({"name": "나"})["id"]
    c = web.api_folder_create({"name": "다"})["id"]

    order = lambda: [f["id"] for f in web.api_folders()["folders"] if f["parent_id"] is None]
    assert order() == [a, b, c]                       # 처음엔 이름순

    web.api_folder_move({"id": c, "parent_id": None, "before_id": a})
    assert order() == [c, a, b]                       # 맨 앞으로

    web.api_folder_move({"id": c, "parent_id": None})  # before 없으면 맨 뒤
    assert order() == [a, b, c]


def test_folder_move_into_and_out_keeps_order(tmp_path, monkeypatch):
    """다른 폴더 안으로 넣었다가 다시 최상위로 빼도 순서 지정이 유지된다."""
    _seed_folder_db(tmp_path, monkeypatch)
    a = web.api_folder_create({"name": "가"})["id"]
    b = web.api_folder_create({"name": "나"})["id"]

    web.api_folder_move({"id": b, "parent_id": a})            # a 안으로
    assert [f["id"] for f in web.api_folder(id=a)["children"]] == [b]

    web.api_folder_move({"id": b, "parent_id": None, "before_id": a})   # 다시 밖으로, a 앞에
    tops = [f["id"] for f in web.api_folders()["folders"] if f["parent_id"] is None]
    assert tops == [b, a]


def test_session_title_override_and_reset(tmp_path, monkeypatch):
    """사용자가 지은 세션 제목은 목록·상세에 반영되고, 원문 대화는 그대로다. 비우면 기본 제목으로 복귀."""
    _seed_folder_db(tmp_path, monkeypatch)

    assert web.api_sessions()["sessions"][0]["headline"] in ("q1", "q2")   # 기본=첫 턴 질문
    web.api_session_title({"session_id": "s1", "title": "내가 지은 제목"})

    row = next(r for r in web.api_sessions()["sessions"] if r["session"] == "s1")
    assert row["headline"] == "내가 지은 제목" and row["custom_title"] == "내가 지은 제목"
    assert web.api_session(id="s1")["title"] == "내가 지은 제목"
    assert web.api_session(id="s1")["turns"][0]["question"] == "q1"        # 대화는 불변

    web.api_session_title({"session_id": "s1", "title": ""})               # 비우면 원복
    row = next(r for r in web.api_sessions()["sessions"] if r["session"] == "s1")
    assert row["headline"] == "q1" and row["custom_title"] is None


def test_session_title_rejects_unknown_session():
    with pytest.raises(web.HTTPException) as ei:
        web.api_session_title({"session_id": "", "title": "x"})
    assert ei.value.status_code == 400


# ── 지도 캐시 무효화(#128 회귀) ───────────────────────────────
def test_graph3d_invalidate_marks_stale_instead_of_deleting(tmp_path, monkeypatch):
    """접기/펼치기는 캐시를 지우면 안 된다.

    지우면 (1) 다음 조회가 동기 UMAP 경로로 떨어져 군집 탭이 수십 초 멈추고,
    (2) prev_members 가 사라져 군집 id 승계가 끊겨 색이 전부 바뀐다.
    '낡음' 표시만 남기고 데이터·members 는 보존해야 한다.
    """
    import json
    from vestige import config as C
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    cache = tmp_path / "graph3d_cache.json"
    cache.write_text(json.dumps({
        "n": 100, "v": web._GRAPH3D_VER,
        "data": {"points": [{"id": "s1:u1"}], "clusters": [{"id": 3}]},
        "members": [["s1:u1"]],
    }), encoding="utf-8")

    before = cache.read_bytes()
    web._graph3d_invalidate()

    # 표시는 별도 빈 파일로 둔다 — 5MB 를 다시 쓰면 접기 한 번에 85ms 가 들고,
    # 쓰는 중에 죽으면 캐시가 깨진 JSON 으로 남는다. 본문은 한 바이트도 건드리지 않는다.
    assert cache.read_bytes() == before
    stale = tmp_path / "graph3d_cache.stale"
    assert stale.exists() and stale.stat().st_size == 0
    after = json.loads(cache.read_text(encoding="utf-8"))
    assert after["data"]["clusters"] == [{"id": 3}]   # 옛 데이터는 그대로 제공 가능
    assert after["members"] == [["s1:u1"]]            # 군집 id 승계 기준 보존


def test_graph3d_stale_mark_triggers_recompute_and_is_cleared(tmp_path, monkeypatch):
    """표시 파일이 실제로 재계산을 부르고, 재계산 뒤엔 거둬진다.

    표시를 본문에서 분리했으므로 '읽는 쪽이 그 파일을 보는가'가 끊기면 무효화가 조용히
    사라진다(접어도 지도가 안 바뀜). 그 연결을 직접 건다.
    """
    import json
    from vestige import config as C
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "vector_count", lambda: 100)
    cache = tmp_path / "graph3d_cache.json"
    cache.write_text(json.dumps({"n": 100, "v": web._GRAPH3D_VER,
                                 "data": {"points": [], "clusters": []}}), encoding="utf-8")

    called: list[int] = []
    monkeypatch.setattr(web, "_graph3d_recompute_bg", lambda n: called.append(n))

    web._graph3d_data()                        # 표시 없음 → 재계산 안 부름
    assert called == []

    web._graph3d_invalidate()
    web._graph3d_data()                        # 표시 있음 → 재계산 부름
    assert called == [100]

    # 재계산이 끝나면 표시를 거둔다 — 안 거두면 매 조회가 영원히 재계산을 부른다.
    from vestige import graph as G
    monkeypatch.setattr(G, "build_graph", lambda *a, **k: {"points": [], "clusters": [], "_members": []})
    monkeypatch.setattr(web, "make_index", lambda: None)
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: None)
    assert (tmp_path / "graph3d_cache.stale").exists()
    web._graph3d_compute_and_cache(100)
    assert not (tmp_path / "graph3d_cache.stale").exists()


def test_graph3d_invalidate_survives_missing_cache(tmp_path, monkeypatch):
    from vestige import config as C
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    web._graph3d_invalidate()                  # 캐시 없어도 예외 없이 지나간다
    assert not (tmp_path / "graph3d_cache.json").exists()


# ── /api/stats (상태바 1초 폴링) ──────────────────────────────
def test_api_stats_does_not_load_vector_matrix(monkeypatch):
    """개수 하나 보여주려고 수백 MB 행렬을 올리지 않는다(상태바가 1초마다 호출)."""
    called = []
    monkeypatch.setattr(web, "make_index", lambda *a, **k: called.append(1) or _Boom())
    monkeypatch.setattr(web, "vector_count", lambda *a, **k: 7)
    web._stats_cache.update(at=0.0, v=None)    # 캐시 비우고 시작
    out = web.api_stats()
    assert out["vectors"] == 7
    assert called == []                        # make_index 를 아예 안 부른다


def test_api_stats_uses_ttl_cache(monkeypatch):
    monkeypatch.setattr(web, "vector_count", lambda *a, **k: 1)
    web._stats_cache.update(at=0.0, v=None)
    first = web.api_stats()
    monkeypatch.setattr(web, "vector_count", lambda *a, **k: 999)   # 값이 바뀌어도
    assert web.api_stats() is first                                  # TTL 안에선 같은 객체
    web._stats_cache.update(at=0.0, v=None)                          # 만료시키면 재계산
    assert web.api_stats()["vectors"] == 999


class _Boom:
    def __len__(self):
        raise AssertionError("make_index() 가 불리면 안 된다")


def test_graph3d_data_does_not_load_vector_matrix_for_staleness_check(tmp_path, monkeypatch):
    """캐시가 신선하면 벡터 행렬을 올리지 않는다.

    이 함수는 예열 스레드가 3분마다 + /api/graph3d 요청마다 부른다. 낡았는지 판정할 개수
    하나 때문에 make_index() 로 행렬(수십 MB)을 통째로 올리고 버리던 것을 막는다.
    """
    import json
    from vestige import config as C
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    (tmp_path / "graph3d_cache.json").write_text(json.dumps({
        "n": 100, "v": web._GRAPH3D_VER,
        "data": {"points": [{"id": "s1:u1"}], "clusters": [], "method": "umap", "dims": 3},
        "members": [["s1:u1"]],
    }), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("make_index() 가 불리면 안 된다")
    monkeypatch.setattr(web, "make_index", boom)
    monkeypatch.setattr(web, "vector_count", lambda *a, **k: 100)   # 변화 없음 → 재계산 불필요

    out = web._graph3d_data()
    assert out["points"] == [{"id": "s1:u1"}]


def test_graph3d_data_returns_empty_without_vectors(tmp_path, monkeypatch):
    from vestige import config as C
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "make_index", lambda *a, **k: (_ for _ in ()).throw(AssertionError("불리면 안 됨")))
    monkeypatch.setattr(web, "vector_count", lambda *a, **k: 0)
    assert web._graph3d_data() == {"points": [], "clusters": [], "method": None, "dims": 3}


# ── /api/config 신규 키 검증 ─────────────────────────────────
# 거부 경로는 write_config 전에 반환하므로, 유효값 확인도 '잘못된 키 하나를 끼워'
# 조기 반환시켜 검사한다 — 실제 설정 파일·환경변수를 건드리지 않게.
def test_config_put_rejects_bad_raw_archive_max_mb():
    """잘못된 값을 통과시키면 indexer 의 int() 에서 조용히 터지고, 사용자는 상한을
    켰다고 믿는데 영구히 미적용인 상태가 된다."""
    for bad in ("abc", "-5", "1.5"):
        r = web.api_config_put({"VESTIGE_RAW_ARCHIVE_MAX_MB": bad})
        assert r["ok"] is False, bad
        assert r["code"] == "invalid_config_value"
        assert "VESTIGE_RAW_ARCHIVE_MAX_MB" in r["invalid"], bad


def test_config_put_rejects_unwritable_raw_archive_dir(tmp_path):
    """쓸 수 없는 경로를 저장하면 이후 모든 미러링이 실패하는데, 설정 화면에는
    그 경로가 멀쩡히 적혀 있다."""
    afile = tmp_path / "notadir"
    afile.write_text("x", encoding="utf-8")          # 파일을 디렉터리로 지정
    r = web.api_config_put({"VESTIGE_RAW_ARCHIVE_DIR": str(afile)})
    assert r["ok"] is False and "VESTIGE_RAW_ARCHIVE_DIR" in r["invalid"]


def test_config_put_accepts_creatable_raw_archive_dir(tmp_path):
    good = tmp_path / "raw" / "nested"               # 아직 없지만 만들 수 있는 경로
    r = web.api_config_put({
        "VESTIGE_RAW_ARCHIVE_DIR": str(good),
        "VESTIGE_INDEX_MODE": "bogus",               # 조기 반환용(저장까지 가지 않게)
    })
    assert r["ok"] is False
    assert "VESTIGE_RAW_ARCHIVE_DIR" not in r["invalid"]   # 경로는 유효 판정
    assert r["invalid"] == ["VESTIGE_INDEX_MODE"]
    assert good.is_dir()                             # 검증 과정에서 만들어진다
    assert not (good / ".vestige-write-test").exists()     # 쓰기 시험 흔적은 안 남긴다


def test_config_put_allows_clearing_raw_archive_max():
    """빈 값 = 무제한(설정 UI 계약) — 거부하면 상한을 끌 수가 없다."""
    r = web.api_config_put({"VESTIGE_RAW_ARCHIVE_MAX_MB": "", "VESTIGE_INDEX_MODE": "bogus"})
    assert "VESTIGE_RAW_ARCHIVE_MAX_MB" not in r["invalid"]


def test_raw_size_cached_reuses_value_within_ttl(monkeypatch):
    """설정 화면이 3초 폴링인데 보존소 전체를 매번 rglob+stat 하면 안 된다."""
    calls = []
    from vestige import raw_archive
    monkeypatch.setattr(raw_archive, "mirror_size_bytes", lambda: calls.append(1) or 123)
    web._raw_size_cache.update(at=0.0, n=0)
    assert web._raw_size_cached() == 123
    assert web._raw_size_cached() == 123
    assert len(calls) == 1                  # 두 번째는 캐시
    web._raw_size_cache["at"] = 0.0         # 만료시키면 다시 센다
    assert web._raw_size_cached() == 123
    assert len(calls) == 2


class _FreeLock:
    """항상 잡히는 색인 락 스텁(테스트가 다른 프로세스의 실행 여부에 좌우되지 않게)."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        pass


def test_sync_errors_reach_the_index_status_payload(tmp_path, monkeypatch):
    """동기화 오류가 만들어지는 곳부터 /api/index/status 응답까지 배선이 살아있는가.

    이 배선은 두 번 조용히 끊겼다 — 처음엔 log_fn 이 버려져서, 두 번째는 13줄 뒤
    _autoindex_state["errors"] = [] 에 씻겨서. 둘 다 코드를 읽어야만 알 수 있었다.
    _capture_log 의 key= 인자를 빼먹는 식의 리팩터링을 CI 가 잡게 한다.
    """
    from vestige import archive_sync as A
    MSG = "ERROR 아카이브 peer.ndjson:3 건너뜀: 모의"

    def fake_import(db, projects_dir, did, *, vi=None, log_fn=print):
        log_fn(MSG)
        log_fn("정리 상태 2건 반영")                 # ERROR 아닌 것은 안 쌓인다
        return 0

    # **실제 호출 경로**를 태운다. _capture_log 를 직접 부르면 web.py 의 배선(key= 인자)을
    # 안 타서, 그 인자를 빼먹어도 테스트가 통과한다(실측).
    from vestige import indexer as I, proclock as P
    monkeypatch.setattr(A, "import_archives", fake_import)
    monkeypatch.setattr(I, "has_new_data", lambda db: False)      # 함수 안에서 import 하므로 원본 모듈을 패치
    monkeypatch.setattr(I, "reconcile", lambda *a, **k: 0)
    # 크로스-프로세스 색인 락은 이 테스트의 관심사가 아니다. 스텁하지 않으면 **Vestige 가
    # 실행 중일 때** 그 앱이 락을 쥐고 있어 _run_incremental 이 오류를 만드는 지점에
    # 닿기도 전에 조기 반환한다 → 거짓 실패. CI 는 앱이 없어 통과하므로 로컬에서만 깨졌다.
    monkeypatch.setattr(P, "IndexLock", lambda *a, **k: _FreeLock())
    monkeypatch.setattr(web, "make_index", lambda: _FakeVI())
    web._autoindex_state["sync_errors"] = []
    web._autoindex_state["errors"] = []
    web._run_incremental()

    assert web._autoindex_state["sync_errors"] == [MSG]
    assert web._autoindex_state["errors"] == []      # 색인 오류와 섞이지 않는다
    assert web.api_index_status()["sync_errors"] == [MSG]
    web._autoindex_state["sync_errors"] = []


def test_archive_sync_endpoint_returns_warnings(monkeypatch):
    """수동 '지금 병합' 이 건너뛴 줄을 warnings 로 돌려주는가.

    백엔드가 만든 경고를 응답에 안 실으면 사용자는 ok:true 만 본다.
    """
    from vestige import archive_sync as A

    def fake_import(db, projects_dir, did, *, vi=None, log_fn=print):
        log_fn("정리 상태 2건 반영(제목·접힘)")       # 정보성 — warnings 에 안 들어가야
        log_fn("ERROR 아카이브 peer.ndjson — 읽을 수 없는 줄 1개를 건너뛰었어요")
        return 0

    monkeypatch.setattr(A, "import_archives", fake_import)
    monkeypatch.setattr(A, "export_archive", lambda *a, **k: 0)
    monkeypatch.setattr(web, "make_index", lambda: None)
    r = web.api_archive_sync()
    assert r["ok"] is True
    assert r["warnings"] == ["아카이브 peer.ndjson — 읽을 수 없는 줄 1개를 건너뛰었어요"]


class _FakeVI:
    """make_index() 대역 — _run_incremental 이 len()/keys() 만 쓴다."""
    def __len__(self): return 0
    def keys(self): return []


def test_config_put_rejects_bad_index_interval():
    """색인 주기는 1 이상 정수만. 예전엔 '1.5' 가 그대로 저장돼 다음 실행부터 백엔드가 안 떴다."""
    for bad in ("1.5", "0", "-5", "abc"):
        r = web.api_config_put({"VESTIGE_INDEX_INTERVAL": bad})
        assert r["ok"] is False and "VESTIGE_INDEX_INTERVAL" in r["invalid"], bad


def test_bad_index_interval_in_config_file_does_not_stop_startup(tmp_path):
    """이미 잘못 저장된 값이 있어도 config 를 import 할 수 있어야 한다(기본값으로 읽는다)."""
    import os
    import subprocess
    import sys

    cfg = tmp_path / "config.env"
    cfg.write_text("VESTIGE_INDEX_INTERVAL=1.5\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "VESTIGE_INDEX_INTERVAL"}
    env.update(VESTIGE_CONFIG=str(cfg), VESTIGE_DATA_DIR=str(tmp_path / "data"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", "import vestige.config as C; print(C.INDEX_INTERVAL_MIN)"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, r.stderr[-400:]
    assert r.stdout.strip() == "10"


def test_syncthing_status_does_not_pile_up_rest_calls(monkeypatch):
    """1초 폴링이 겹쳐도 Syncthing 요약은 한 번만 부르고, 짧은 TTL 안에서는 다시 부르지 않는다.

    pair_summary 는 로컬 Syncthing REST 를 여러 번 순차로 부른다. 캐시·락이 없어서 Syncthing 이
    느릴 때 상태바 폴링마다 새 요청이 겹쳐 쌓였다(#245 의 482MB 재파싱과 같은 모양).
    """
    import threading
    import time as _t

    calls = []
    started = threading.Event()

    class SlowInst:
        def pair_summary(self):
            calls.append(1)
            started.set()
            _t.sleep(0.5)
            return {"sync": {"state": "idle"}}

    monkeypatch.setitem(web._st, "inst", SlowInst())
    monkeypatch.setitem(web._st_state, "running", True)
    web._pair_cache.update(at=0.0, v={})

    th = threading.Thread(target=web.api_syncthing_status, daemon=True)
    th.start()
    assert started.wait(5)
    web.api_syncthing_status()          # 겹친 폴링 - 직전 값으로 즉시
    web.api_syncthing_status()
    th.join(10)
    assert len(calls) == 1, f"겹친 폴링이 Syncthing 을 또 불렀다({len(calls)}회)"

    assert web.api_syncthing_status()["sync"] == {"state": "idle"}   # TTL 안 - 캐시 값
    assert len(calls) == 1
