"""v0.4.0 최종 풀리뷰(보안 · 무음 실패) 수정의 회귀 검사."""
import json
import logging
import mmap
import time

from vestige import web


# --- Windows 재개: 작업 폴더의 claude.bat 이 먼저 실행되지 않는다 ---------------------------------------

def test_resume_env_does_not_search_the_current_folder_first():
    """cmd.exe 는 PATH 보다 현재 폴더를 먼저 본다. 재개 창의 cwd 는 세션 로그에 적힌 프로젝트 폴더라,
    거기(예: git pull 로 들어온 저장소)에 claude.bat 이 있으면 '재개'를 누르는 순간 그게 실행된다."""
    assert web._resume_env()["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_windows_resume_passes_that_env_to_cmd(monkeypatch):
    seen = {}
    monkeypatch.setattr(web._sys, "platform", "win32")
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    web._launch_resume("0f8b1c2e-1111-2222-3333-444455556666", "C:\\proj")
    assert seen["env"]["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert seen["argv"][:3] == ["cmd", "/c", "start"]


# --- 하위 에이전트 판정: 일시적 읽기 오류를 '대상 아님'으로 굳히지 않는다 ------------------------------

def _agent_file(tmp_path):
    def u(text, meta):
        o = {"type": "user", "sessionId": "p", "uuid": text, "isSidechain": True, "agentId": "a1",
             "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        if meta:
            o["isMeta"] = True
        return o
    lines = [u("최초 Task 프롬프트입니다", False), u("후속 지시 하나입니다", True), u("후속 지시 둘입니다", True)]
    p = tmp_path / "proj" / "subagents" / "agent-a1.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n", encoding="utf-8")
    return p


def test_subagent_gate_does_not_cache_a_transient_read_error(tmp_path, monkeypatch, caplog):
    """백신이 파일을 잡고 있는 순간 한 번 실패한 것이 '후속 지시 부족'과 같이 저장되면, 다 쓰인 하위 로그는
    다시 안 바뀌어 앱을 다시 켤 때까지 색인되지 않았다(로그도 없이)."""
    from vestige.sources import subagent as S
    p = _agent_file(tmp_path)
    S._gate_cache.clear()
    a = S.SubagentAdapter()

    def boom(*_a, **_k):
        raise OSError("sharing violation")

    with monkeypatch.context() as m:
        m.setattr(S.mmap, "mmap", boom)
        with caplog.at_level(logging.WARNING):
            assert a._qualifies(p) is False
    assert any("agent-a1.jsonl" in r.getMessage() for r in caplog.records), "읽기 실패가 기록돼야 한다"
    assert a._qualifies(p) is True                              # 다음엔 다시 읽어 통과한다


def test_subagent_scan_survives_a_marker_line_that_is_not_an_object(tmp_path):
    from vestige.sources import subagent as S
    p = tmp_path / "agent-x.jsonl"
    p.write_text('[{"isMeta": true}]\n"isMeta": true\n', encoding="utf-8")
    assert S.SubagentAdapter._scan(p) is False


# --- config.env 를 못 읽으면 말한다 ------------------------------------------------------------------

def test_unreadable_config_file_is_reported(tmp_path, capsys):
    """읽기 오류(인코딩, 권한)로 모든 설정이 기본값으로 돌아가는데 아무 표시가 없었다."""
    from vestige import config
    p = tmp_path / "config.env"
    p.write_bytes(b"VESTIGE_X=\xff\xfe\x00bad\n")
    config._load_config_file(p)
    assert str(p) in capsys.readouterr().err


# --- 백그라운드 실패가 기록에 남는다 ----------------------------------------------------------------

def test_pending_failure_is_logged_once_per_streak(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(web, "ArchiveDB", boom)
    monkeypatch.setitem(web._pending_cache, "at", 0.0)
    monkeypatch.setitem(web._pending_cache, "failing", False)
    with caplog.at_level(logging.WARNING):
        web._pending_snapshot()
        monkeypatch.setitem(web._pending_cache, "at", 0.0)
        web._pending_snapshot()
    msgs = [r for r in caplog.records if "대기 건수" in r.getMessage()]
    assert len(msgs) == 1, [r.getMessage() for r in caplog.records]   # 8초마다 같은 줄이 쌓이지 않게


def test_graph3d_background_failure_leaves_a_traceback(monkeypatch, capsys):
    def boom(n):
        raise RuntimeError("umap 터짐")

    monkeypatch.setattr(web, "_graph3d_compute_and_cache", boom)
    monkeypatch.setitem(web._graph3d_recomputing, "on", False)
    web._graph3d_recompute_bg(1)
    for _ in range(100):
        if not web._graph3d_recomputing["on"]:
            break
        time.sleep(0.02)
    assert "umap 터짐" in capsys.readouterr().err
