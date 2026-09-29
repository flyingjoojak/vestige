"""출처(source) 인지 세션 재개: 저장 라운드트립 · 명령 argv · 원문 파일 탐색."""
from __future__ import annotations

from pathlib import Path

from vestige import config, web
from vestige.models import Turn
from vestige.store import ArchiveDB

SID = "019e80dc-1754-7422-b72f-2d176635efb2"


def _turn(sid: str = SID) -> Turn:
    return Turn(id=f"{sid}:u1", session_id=sid, uuid="u1", parent_uuid=None,
                timestamp="2026-08-21T00:00:00Z", project="/proj",
                question="q", answer="a", actions=())


# ── store: source/source_file 라운드트립 ──────────────────────
def test_session_source_roundtrip(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn(), source="codex", source_file="/x/rollout.jsonl")
    db.commit()
    assert db.session_source(SID) == ("codex", "/x/rollout.jsonl", "/proj")


def test_session_source_defaults_when_legacy(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    db.upsert_turn(_turn(), source_file=None)   # source 미지정 → 기본 claude-code, source_file NULL
    db.commit()
    assert db.session_source(SID) == ("claude-code", None, "/proj")


def test_session_source_none_when_missing(tmp_path):
    db = ArchiveDB(tmp_path / "a.db")
    assert db.session_source("no-such") is None


# ── 재개 명령 argv(출처별) ────────────────────────────────────
def test_sid_regex_rejects_flag_injection():
    # 선두 '-' 금지: 심어진 로그의 session_id가 CLI 플래그로 주입되는 것 차단(보안 H1).
    assert web._SID_RE.fullmatch(SID)                       # 정상 UUID 허용
    assert web._SID_RE.fullmatch("claude-s1")               # 일반 id 허용
    assert not web._SID_RE.fullmatch("--dangerously-skip")  # 선두 대시 거부
    assert not web._SID_RE.fullmatch("-x")
    assert not web._SID_RE.fullmatch("a b")                 # 공백 거부
    assert not web._SID_RE.fullmatch("a;b")                 # 메타문자 거부


def test_find_source_file_flag_sid_is_none():
    # 위험한 sid는 파일 탐색 단계에서도 거부(None).
    assert web._find_source_file("codex", "--flag", None) is None


def test_resume_argv_by_source():
    assert web._resume_argv("claude-code", SID) == ["claude", "--resume", SID]
    assert web._resume_argv("codex", SID) == ["codex", "resume", SID]
    assert web._resume_argv("unknown", SID) == ["claude", "--resume", SID]  # 기본 폴백
    assert web._resume_cmd_str("codex", SID) == f"codex resume {SID}"


# ── 원문 파일 탐색(출처별) ────────────────────────────────────
def test_find_source_file_prefers_stored(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    assert web._find_source_file("codex", SID, str(f)) == f


def test_find_source_file_codex_glob(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    d = root / "2026" / "08" / "21"
    d.mkdir(parents=True)
    f = d / f"rollout-2026-08-21T10-00-00-{SID}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", root)
    # 저장 경로가 없어졌어도(None) 출처별 탐색으로 찾아냄
    assert web._find_source_file("codex", SID, None) == f


def test_find_source_file_claude_glob(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    d = root / "proj"
    d.mkdir(parents=True)
    f = d / f"{SID}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    assert web._find_source_file("claude-code", SID, None) == f


def test_find_source_file_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "nope")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "nope2")
    assert web._find_source_file("codex", SID, str(tmp_path / "gone.jsonl")) is None
    assert web._find_source_file("claude-code", SID, None) is None
