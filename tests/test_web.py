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
    """턴 숨김/복원(#128) - 숨기면 세션/검색에서 빠지고, 복원하면 다시 보인다."""
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
    ids = [t["id"] for t in web.api_session(id="s1")["turns"]]
    assert ids == ["s1:u2"]   # 숨긴 턴은 세션 뷰에서 제외
    assert [h["turn_id"] for h in web.api_hidden()["hidden"]] == ["s1:u1"]

    web.api_unhide({"turn_id": "s1:u1"})
    ids = [t["id"] for t in web.api_session(id="s1")["turns"]]
    assert ids == ["s1:u1", "s1:u2"]   # 복원되어 다시 보임
    assert web.api_hidden()["hidden"] == []


def test_api_hide_session_removes_it_from_sessions_list(tmp_path, monkeypatch):
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
    sessions = [s["session"] for s in web.api_sessions()["sessions"]]
    assert sessions == ["s2"]   # 전 턴이 숨겨진 세션은 목록에서 아예 빠짐


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
    assert web.api_hidden()["hidden"] == []


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
