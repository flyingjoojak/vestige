"""배경(서브에이전트) 대화 어댑터: 게이트(일회성 봇 제외)·세션 분리·래퍼 제거."""
import json
import os
import time
from pathlib import Path

from vestige.sources.subagent import SubagentAdapter, _strip_wrapper

PARENT = "4a545a78-parent-session"
AID = "a461background00"

_WRAP = ("The user sent a new message while you were working:\n{msg}\n\n"
         "This is how Claude Code surfaces messages the user sends mid-turn — "
         "within the running turn. Address the message above as you continue this turn.")


def _user(text, *, meta=False, wrap=False):
    content = _WRAP.format(msg=text) if wrap else text
    o = {"type": "user", "sessionId": PARENT, "uuid": f"u-{text[:6]}",
         "parentUuid": None, "timestamp": "2026-08-21T00:00:00Z",
         "cwd": "/c/proj", "isSidechain": True, "agentId": AID,
         "message": {"role": "user", "content": [{"type": "text", "text": content}]}}
    if meta:
        o["isMeta"] = True
    return o


def _assistant(text):
    return {"type": "assistant", "sessionId": PARENT, "isSidechain": True, "agentId": AID,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _write(path: Path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")


def _interactive_agent():
    """Task 프롬프트 1 + 사람 후속 지시(isMeta) 2 → 게이트 통과 대상."""
    return [
        _user("Research CLI log formats"),           # 최초 Task 프롬프트(플래그 없음)
        _assistant("조사 시작합니다"),
        _user("48 머지하고 진행하자", meta=True, wrap=True),
        _assistant("머지했습니다"),
        _user("재빌드해줘", meta=True, wrap=True),
        _assistant("재빌드 완료"),
    ]


def _oneshot_helper():
    """Task 프롬프트 1개 + 응답뿐(사람 후속 지시 0) → 일회성 봇, 제외 대상."""
    return [
        _user("Review this code for bugs"),
        _assistant("리뷰 결과: 문제 없음"),
    ]


def test_strip_wrapper_removes_harness_text():
    wrapped = _WRAP.format(msg="머지해줘")
    assert _strip_wrapper(wrapped) == "머지해줘"
    assert _strip_wrapper("플래그 없는 원문") == "플래그 없는 원문"


def test_discover_gates_out_oneshot_helpers(tmp_path):
    root = tmp_path / "projects"
    _write(root / PARENT / "subagents" / f"agent-{AID}.jsonl", _interactive_agent())
    _write(root / PARENT / "subagents" / "agent-helper00000000.jsonl", _oneshot_helper())
    # 부모 세션 본체(서브에이전트 아님)도 하나 — discover 대상 아님.
    _write(root / PARENT / f"{PARENT}.jsonl", [_user("메인 질문")])

    found = {p.name for p in SubagentAdapter().discover(root)}
    assert found == {f"agent-{AID}.jsonl"}   # 상호작용 에이전트만, 일회성 봇 제외


def test_extract_turns_separates_session_and_strips_wrapper():
    a = SubagentAdapter()
    turns = a.extract_turns(_interactive_agent())
    # Task 프롬프트 1 + 후속 2 = 3턴
    assert len(turns) == 3
    # 세션이 부모가 아니라 agentId 로 분리됨
    assert all(t.session_id == AID for t in turns)
    assert all(t.id.startswith(AID + ":") for t in turns)
    # 래퍼가 벗겨져 사용자 원문만 질문에 남음
    assert turns[1].question == "48 머지하고 진행하자"
    assert turns[2].question == "재빌드해줘"


def test_source_name_is_claude_code():
    # 배경 에이전트도 결국 claude-code 도구 콘텐츠 → 저장 출처는 claude-code.
    assert SubagentAdapter.source_name == "claude-code"
    assert SubagentAdapter.name == "subagent"


def test_gate_does_not_reparse_unchanged_file(tmp_path, monkeypatch):
    """게이트를 통과 못 하는 파일을 폴링마다 다시 읽지 않는다.

    통과하는 파일은 두 번째 후속 지시에서 멈추지만, 통과 못 하는 파일은 끝까지 읽는다.
    discover() 는 /api/index/status 폴링마다 불리므로 이게 매번 전문 파싱이 됐다.
    """
    from vestige.sources import subagent as S

    p = tmp_path / "proj" / "subagents" / "agent-oneshot.jsonl"
    _write(p, [_user("한 번만 시킨 헬퍼 봇"), _assistant("끝")])   # 후속 지시 0 → 통과 못 함

    reads = []
    real = S.SubagentAdapter._scan
    monkeypatch.setattr(S.SubagentAdapter, "_scan",
                        staticmethod(lambda path: (reads.append(str(path)), real(path))[1]))
    S._gate_cache.clear()

    a = SubagentAdapter()
    assert a._qualifies(p) is False
    assert len(reads) == 1, "첫 호출은 읽어야 한다"
    assert a._qualifies(p) is False
    assert a._qualifies(p) is False
    assert len(reads) == 1, f"안 바뀐 파일을 다시 읽었다({len(reads)}회)"


def test_gate_rescans_when_file_grows(tmp_path, monkeypatch):
    """파일이 자라면 다시 읽는다 - 캐시가 '영영 제외'로 굳으면 안 된다."""
    from vestige.sources import subagent as S

    p = tmp_path / "proj" / "subagents" / "agent-grows.jsonl"
    _write(p, [_user("최초 Task"), _assistant("시작")])
    S._gate_cache.clear()

    a = SubagentAdapter()
    assert a._qualifies(p) is False

    # 사람 후속 지시 2개가 붙어 이제 색인 대상이 된다.
    _write(p, [_user("최초 Task"), _assistant("시작"),
               _user("이것도 해줘", meta=True), _assistant("네"),
               _user("저것도 해줘", meta=True), _assistant("네")])
    os.utime(p, (time.time() + 2, time.time() + 2))   # mtime 해상도에 안 기대게 명시적으로
    assert a._qualifies(p) is True, "자란 파일을 낡은 캐시로 계속 제외했다"


def test_gate_ignores_an_unterminated_last_record(tmp_path):
    """개행 없이 끝난 마지막 기록(아직 쓰이는 중)은 후속 지시로 세지 않는다 - 예전 줄 단위 읽기와 같게."""
    import json as _json
    from vestige.sources import subagent as S
    p = tmp_path / "proj" / "subagents" / "agent-x.jsonl"
    _write(p, [_user("처음 지시"), _assistant("응"), _user("두 번째 지시", meta=True, wrap=True), _assistant("응")])
    with open(p, "a", encoding="utf-8") as f:
        f.write(_json.dumps(_user("세 번째 - 아직 쓰이는 중", meta=True, wrap=True)))   # 개행 없음
    assert S.SubagentAdapter._scan(p) is False
    with open(p, "a", encoding="utf-8") as f:
        f.write("
")   # 줄이 끝나면 센다
    assert S.SubagentAdapter._scan(p) is True
