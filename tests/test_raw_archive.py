"""원본 로그 바이트 보존(#163 P1) — 턴 추출과 무관한 순수 append 미러링."""
from __future__ import annotations

import json
import os

import numpy as np

from vestige import config
from vestige import raw_archive as R
from vestige.indexer import index_all
from vestige.store import ArchiveDB


def _db(tmp_path):
    return ArchiveDB(tmp_path / "a.db")


# ── session_id_for / raw_path ────────────────────────────────
def test_session_id_for_extracts_uuid_from_filename():
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    assert R.session_id_for(f"/home/me/.claude/projects/x/{sid}.jsonl") == sid
    assert R.session_id_for(f"rollout-2026-08-21T10-00-00-{sid}.jsonl") == sid


def test_session_id_for_falls_back_to_stem_without_uuid():
    assert R.session_id_for("/tmp/weird-name.jsonl") == "weird-name"


def test_raw_path_sanitizes_traversal_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    p = R.raw_path("../../etc", "../../passwd")
    assert p.is_relative_to(tmp_path / "raw")
    assert ".." not in p.name and ".." not in p.parent.name


# ── mirror_file ───────────────────────────────────────────────
def test_mirror_file_copies_new_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b'{"type":"session_meta"}\n')

    written = R.mirror_file(db, f, "claude-code")
    assert written == len(b'{"type":"session_meta"}\n')
    assert R.has_mirror("claude-code", sid)
    assert R.read_mirror("claude-code", sid) == b'{"type":"session_meta"}\n'


def test_mirror_file_is_idempotent_with_no_new_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"line1\n")
    R.mirror_file(db, f, "claude-code")
    assert R.mirror_file(db, f, "claude-code") == 0   # 새 바이트 없음 → 재호출 no-op


def test_mirror_file_appends_only_new_bytes_across_calls(tmp_path, monkeypatch):
    # 멀티멤버 gzip: 두 번에 걸쳐 append 한 내용이 이어서 온전히 복원돼야 함.
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"line1\n")
    R.mirror_file(db, f, "claude-code")
    with open(f, "ab") as fh:
        fh.write(b"line2\n")
    written2 = R.mirror_file(db, f, "claude-code")
    assert written2 == len(b"line2\n")
    assert R.read_mirror("claude-code", sid) == b"line1\nline2\n"


def test_mirror_file_recovers_from_rotation_truncation(tmp_path, monkeypatch):
    # 파일이 줄어들면(회전/절단) 커서가 파일 크기보다 커지므로 0부터 다시 미러링.
    # 기존 보존분은 지우지 않고 새 멤버로 이어 씀(안전 쪽).
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"aaaaaaaaaa\n")
    R.mirror_file(db, f, "claude-code")
    f.write_bytes(b"bb\n")   # 절단(더 짧아짐)
    written = R.mirror_file(db, f, "claude-code")
    assert written == len(b"bb\n")
    assert R.read_mirror("claude-code", sid) == b"aaaaaaaaaa\nbb\n"   # 이전 보존분 보존 + 새 내용 이어붙음


def test_mirror_file_creates_no_output_for_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"")
    assert R.mirror_file(db, f, "claude-code") == 0
    assert not R.has_mirror("claude-code", sid)


def test_read_mirror_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    assert R.read_mirror("claude-code", "no-such-session") is None


def test_mirror_file_groups_by_source(tmp_path, monkeypatch):
    # 같은 세션 id라도 source 가 다르면 별개 경로(사실상 발생 안 하지만 경계 확인).
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"x\n")
    R.mirror_file(db, f, "codex")
    assert R.has_mirror("codex", sid)
    assert not R.has_mirror("claude-code", sid)


def test_mirror_size_bytes_sums_all_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    assert R.mirror_size_bytes() == 0   # 디렉터리 없음 → 0
    db = _db(tmp_path)
    f1 = tmp_path / "019e80dc-1754-7422-b72f-2d176635efb2.jsonl"
    f1.write_bytes(b"x" * 500)
    R.mirror_file(db, f1, "claude-code")
    f2 = tmp_path / "129e80dc-1754-7422-b72f-2d176635efb3.jsonl"
    f2.write_bytes(b"y" * 500)
    R.mirror_file(db, f2, "codex")
    assert R.mirror_size_bytes() > 0


# ── enforce_quota (#163 PR3) ─────────────────────────────────
def _seed_mirror(tmp_path, db, sid: str, size: int, random_bytes: bool = False):
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(os.urandom(size) if random_bytes else b"x" * size)
    R.mirror_file(db, f, "claude-code")
    return R.raw_path("claude-code", sid)


def test_enforce_quota_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    _seed_mirror(tmp_path, db, "019e80dc-1754-7422-b72f-2d176635efb2", 1000)
    assert R.enforce_quota(0) == 0   # 0 이하=무제한 취급, 아무것도 안 지움
    assert R.mirror_size_bytes() > 0


def test_enforce_quota_noop_when_under_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    p = _seed_mirror(tmp_path, db, "019e80dc-1754-7422-b72f-2d176635efb2", 100)
    assert R.enforce_quota(10_000_000) == 0
    assert p.exists()


def test_enforce_quota_deletes_oldest_first(tmp_path, monkeypatch):
    # 반복 바이트는 gzip이 거의 다 압축해버려 크기 예측이 어려우니, 파일마다 다른 내용을 채워
    # 압축 후에도 실제 용량이 남게 한다.
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    old = _seed_mirror(tmp_path, db, "019e80dc-1754-7422-b72f-2d176635efb2", 2000, random_bytes=True)
    os.utime(old, (1_000_000_000, 1_000_000_000))   # 더 오래됨
    new = _seed_mirror(tmp_path, db, "129e80dc-1754-7422-b72f-2d176635efb3", 2000, random_bytes=True)
    new_size = new.stat().st_size
    removed = R.enforce_quota(new_size + 1)   # 새 파일 하나만 겨우 남을 상한
    assert removed == 1
    assert not old.exists()   # 오래된 것부터 제거
    assert new.exists()


# ── index_all 훅 통합 테스트 ─────────────────────────────────
class _FakeEmbedder:
    model_name = "fake"

    def embed_passages(self, texts, parallel=None):
        return np.array([[float(len(t) % 7) + 1] * 8 for t in texts], dtype=np.float32)


def test_index_all_mirrors_raw_bytes_alongside_indexing(monkeypatch, tmp_path):
    """index_all 이 턴 색인과 별개로 원본 바이트도 보존하는지(#163 훅 배선 확인)."""
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(config, "PROJECTS_DIR", claude_root)
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "codex_unused")
    monkeypatch.setattr(config, "SOURCES_ENV", "")

    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    d = claude_root / "proj"
    d.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "uuid": "u0", "parentUuid": None, "sessionId": sid,
                    "cwd": "/c/proj", "timestamp": "2026-08-21T00:00:00Z",
                    "message": {"role": "user", "content": "질문 상세 내용입니다 테스트용"}}),
        json.dumps({"type": "assistant", "sessionId": sid,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "답변"}]}}),
    ]
    p = d / f"{sid}.jsonl"
    raw_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    p.write_bytes(raw_bytes)
    os.utime(p, (1_000_000_000, 1_000_000_000))   # 진행중 보류 방지 — 세션 종료로 간주

    db = ArchiveDB(tmp_path / "a.db")
    from vestige.vectorindex import VectorIndex
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    n = index_all(db, vi, _FakeEmbedder())

    assert n == 1   # 턴 색인은 평소대로
    assert R.has_mirror("claude-code", sid)   # 원본도 함께 보존됨
    assert R.read_mirror("claude-code", sid) == raw_bytes   # 바이트 그대로(파싱 안 거침)


def test_index_all_mirror_failure_does_not_block_indexing(monkeypatch, tmp_path):
    """mirror_file 이 예외를 던져도 턴 색인은 계속 진행(격리)."""
    claude_root = tmp_path / "claude"
    monkeypatch.setattr(config, "PROJECTS_DIR", claude_root)
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "codex_unused")
    monkeypatch.setattr(config, "SOURCES_ENV", "")

    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    d = claude_root / "proj"
    d.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "uuid": "u0", "parentUuid": None, "sessionId": sid,
                    "cwd": "/c/proj", "timestamp": "2026-08-21T00:00:00Z",
                    "message": {"role": "user", "content": "질문 상세 내용입니다 테스트용"}}),
        json.dumps({"type": "assistant", "sessionId": sid,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "답변"}]}}),
    ]
    p = d / f"{sid}.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    os.utime(p, (1_000_000_000, 1_000_000_000))

    import vestige.indexer as indexer_mod

    def _boom(*a, **k):
        raise RuntimeError("디스크 꽉 참(가정)")
    monkeypatch.setattr(indexer_mod.raw_archive, "mirror_file", _boom)

    db = ArchiveDB(tmp_path / "a.db")
    from vestige.vectorindex import VectorIndex
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")
    n = index_all(db, vi, _FakeEmbedder())
    assert n == 1   # mirror 실패와 무관하게 턴 색인은 정상 완료


# ── 복구(restore) ────────────────────────────────────────────
def test_encode_claude_project_dir_matches_real_claude_code_naming():
    # 실기 확인된 실제 사례 3건(Claude Code가 실제로 만든 프로젝트 폴더명과 대조).
    assert R._encode_claude_project_dir(r"C:\Users\JHJOO\chat-memory") == "C--Users-JHJOO-chat-memory"
    assert R._encode_claude_project_dir(r"C:\growth_report") == "C--growth-report"
    assert (R._encode_claude_project_dir(r"C:\Users\main\Downloads\chatmem-backend (3)")
            == "C--Users-main-Downloads-chatmem-backend--3-")


def test_first_cwd_finds_field_in_early_lines():
    raw = (
        b'{"type":"summary","summary":"x"}\n'
        b'{"type":"user","cwd":"/home/me/proj","message":{}}\n'
        b'{"type":"assistant"}\n'
    )
    assert R._first_cwd(raw, key_path=("cwd",)) == "/home/me/proj"


def test_first_cwd_none_when_absent():
    raw = b'{"type":"assistant"}\n{"type":"assistant"}\n'
    assert R._first_cwd(raw, key_path=("cwd",)) is None


def test_first_cwd_skips_bad_json_lines():
    raw = b'not json at all\n{"type":"user","cwd":"/p"}\n'
    assert R._first_cwd(raw, key_path=("cwd",)) == "/p"


def test_restore_claude_code_writes_to_encoded_project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    raw_bytes = (
        b'{"type":"user","cwd":"C:\\\\growth_report","sessionId":"' + sid.encode() + b'"}\n'
        b'{"type":"assistant"}\n'
    )
    f.write_bytes(raw_bytes)
    R.mirror_file(db, f, "claude-code")

    target = R.restore("claude-code", sid)
    assert target == config.PROJECTS_DIR / "C--growth-report" / f"{sid}.jsonl"
    assert target.read_bytes() == raw_bytes


def test_restore_does_not_overwrite_existing_original(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b'{"type":"user","cwd":"C:\\\\growth_report"}\n')
    R.mirror_file(db, f, "claude-code")

    existing_dir = config.PROJECTS_DIR / "C--growth-report"
    existing_dir.mkdir(parents=True)
    existing = existing_dir / f"{sid}.jsonl"
    existing.write_bytes(b"ALREADY THERE - DO NOT TOUCH")

    target = R.restore("claude-code", sid)
    assert target == existing
    assert existing.read_bytes() == b"ALREADY THERE - DO NOT TOUCH"   # 안 덮어씀


def test_restore_claude_code_without_cwd_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b'{"type":"assistant"}\n')   # cwd 없음
    R.mirror_file(db, f, "claude-code")
    assert R.restore("claude-code", sid) is None


def test_restore_codex_writes_under_restored_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "CODEX_SESSIONS_DIR", tmp_path / "codex_sessions")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"rollout-2026-08-21T10-00-00-{sid}.jsonl"
    raw_bytes = b'{"type":"session_meta","payload":{"id":"x","cwd":"/p"}}\n'
    f.write_bytes(raw_bytes)
    R.mirror_file(db, f, "codex")

    target = R.restore("codex", sid)
    assert target == config.CODEX_SESSIONS_DIR / "restored" / f"rollout-restored-{sid}.jsonl"
    assert target.read_bytes() == raw_bytes


def test_restore_unsupported_source_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    db = _db(tmp_path)
    sid = "019e80dc-1754-7422-b72f-2d176635efb2"
    f = tmp_path / f"{sid}.jsonl"
    f.write_bytes(b"x\n")
    R.mirror_file(db, f, "weird-source")
    assert R.restore("weird-source", sid) is None


def test_restore_no_mirror_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    assert R.restore("claude-code", "no-such-session") is None


# ── raw_cursors 왕복(store.py) ───────────────────────────────
def test_raw_cursor_roundtrip(tmp_path):
    db = _db(tmp_path)
    assert db.get_raw_cursor("/x/a.jsonl") == 0
    db.set_raw_cursor("/x/a.jsonl", 42, "sid1", "claude-code")
    db.commit()
    assert db.get_raw_cursor("/x/a.jsonl") == 42
    db.set_raw_cursor("/x/a.jsonl", 100, "sid1", "claude-code")   # upsert
    db.commit()
    assert db.get_raw_cursor("/x/a.jsonl") == 100
