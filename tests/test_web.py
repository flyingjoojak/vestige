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
    db.upsert_turn(_t("A:u0", "Asession", "2026-07-24T00:00:00Z", "A첫질문"))
    db.upsert_turn(_t("A:u1", "Asession", "2026-07-24T00:01:00Z", "A둘째"))
    db.upsert_turn(_t("B:u0", "Bsession", "2026-07-24T05:00:00Z", "B첫질문"))  # 더 나중에 끝남
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
                         project="myproj", question="질문", answer="답변", actions=()))
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
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()))
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="q2", answer="a2", actions=()))
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
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()))
    db.upsert_turn(Turn(id="s2:u1", session_id="s2", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q2", answer="a2", actions=()))
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
                         timestamp="2026-07-24T00:00:00Z", project="p", question="접을노이즈", answer="a", actions=()))
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="진짜작업", answer="a", actions=()))
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
                         timestamp="2026-07-24T00:00:00Z", project="p", question="접을질문", answer="a1", actions=()))
    db.upsert_turn(Turn(id="s1:u2", session_id="s1", uuid="u2", parent_uuid=None,
                         timestamp="2026-07-24T00:01:00Z", project="p", question="남길질문", answer="a2", actions=()))
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"turn_id": "s1:u1"})
    body = web.api_session_export(id="s1").body.decode("utf-8")
    assert "남길질문" in body and "접을질문" not in body


def test_api_hidden_lists_recent_first_with_count(tmp_path, monkeypatch):
    """접힌 턴 모아보기(#128): 최근 접은 순 + 배지용 count. limit=0 이면 개수만."""
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "a.db")
    for i, q in ((1, "먼저접음"), (2, "나중접음")):
        db.upsert_turn(Turn(id=f"s1:u{i}", session_id="s1", uuid=f"u{i}", parent_uuid=None,
                             timestamp=f"2026-07-24T0{i}:00:00Z", project="p", question=q, answer="a", actions=()))
    db.commit()
    monkeypatch.setattr(web, "ArchiveDB", lambda *a, **k: ArchiveDB(tmp_path / "a.db"))

    web.api_hide({"turn_id": "s1:u1"}); web.api_hide({"turn_id": "s1:u2"})
    # time.time() 해상도로 순서가 흔들리지 않게 접은 시각을 벌려둔다.
    db.conn.execute("UPDATE hidden_turns SET hidden_at=100 WHERE turn_id='s1:u1'")
    db.conn.execute("UPDATE hidden_turns SET hidden_at=200 WHERE turn_id='s1:u2'")
    db.commit()

    r = web.api_hidden()
    assert r["count"] == 2
    assert [h["turn_id"] for h in r["hidden"]] == ["s1:u2", "s1:u1"]   # 최근 접은 순
    assert r["hidden"][0]["headline"] == "나중접음" and r["hidden"][0]["session_id"] == "s1"

    assert web.api_hidden(limit=0) == {"hidden": [], "count": 2}       # 배지용 경량 호출

    web.api_unhide({"turn_id": "s1:u2"})
    assert web.api_hidden()["count"] == 1


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
                         timestamp="2026-07-24T00:00:00Z", project="p", question="q1", answer="a1", actions=()))
    db.upsert_turn(Turn(id="s2:u1", session_id="s2", uuid="u1", parent_uuid=None,
                         timestamp="2026-07-24T01:00:00Z", project="p", question="q2", answer="a2", actions=()))
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
