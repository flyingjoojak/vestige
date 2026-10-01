"""작업 중간에 끼는 isMeta user 기록이 턴을 자르지 않는다.

Claude Code 는 답하는 도중에도 user 기록을 끼워 넣는다. 스킬 본문, 다른 세션이 보낸 메시지,
이미지를 읽을 때 붙는 `[Image: original …]` 이 전부 `isMeta: true` 다. 예전엔 색인기가 이걸
새 턴의 시작으로 보고 구간을 잘랐고, extract_turns 는 같은 기록을 노이즈로 버려서 **그 뒤의
답변과 도구 호출이 어느 턴에도 안 붙었다**(실측: 세션 20개, 거짓 경계 364개).
"""
import json

import pytest

from vestige.indexer import index_file
from vestige.sources.claude_code import ClaudeCodeAdapter
from vestige.store import ArchiveDB
from vestige.vectorindex import VectorIndex

from tests.test_indexer import FakeEmbedder

SID = "22220000-0000-4000-8000-0000000meta1"

# 실제 로그에서 본 모양 그대로(내용만 줄였다)
META = {
    "스킬 본문": "Base directory for this skill: C:\\Users\\x\\.claude\\skills\\obsidian-log\n\n# 규칙 ...",
    "다른 세션 메시지": "Another Claude session sent a message: 빌드 끝났어",
    "이미지 메타": "[Image: original 2560x1000, displayed at 2000x781. Multiply coordinates by 1.28]",
}


def _user(text, uuid, *, meta=False, ts="2026-10-01T00:00:00Z"):
    o = {"type": "user", "sessionId": SID, "uuid": uuid, "parentUuid": None, "timestamp": ts,
         "cwd": "/c/p", "message": {"role": "user", "content": text}}
    if meta:
        o["isMeta"] = True
    return o


def _tool_use(name):
    return {"type": "assistant", "sessionId": SID, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": {"file_path": "/c/p/x.md"}}]}}


def _tool_result():
    return {"type": "user", "sessionId": SID, "uuid": "tr", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def _say(text):
    return {"type": "assistant", "sessionId": SID,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _log(kind):
    return [
        _user("옵시디언에 작업 내역 기록해줘", "u1"),
        _tool_use("Skill"), _tool_result(),
        _user(META[kind], "m1", meta=True),              # ← 작업 중간에 끼는 isMeta 기록
        _tool_use("Edit"), _tool_result(),
        _say("기록 완료했습니다."),
        _user("다음 질문", "u2", ts="2026-10-01T00:05:00Z"),
        _say("답"),
    ]


@pytest.mark.parametrize("kind", list(META))
def test_meta_record_is_not_a_turn_boundary(kind):
    """색인기가 세는 경계 수와 extract_turns 가 만드는 턴 수가 같다(진짜 질문 2개)."""
    a = ClaudeCodeAdapter()
    objs = _log(kind)
    assert sum(a.is_turn_start(o) for o in objs) == len(a.extract_turns(objs)) == 2


@pytest.mark.parametrize("kind", list(META))
def test_answer_after_meta_record_is_indexed(tmp_path, kind):
    """실제 색인 경로로: isMeta 기록 뒤의 도구 호출과 답변이 그 턴에 남는다."""
    f = tmp_path / f"{SID}.jsonl"
    f.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in _log(kind)), encoding="utf-8")
    db = ArchiveDB(tmp_path / "a.db")
    index_file(str(f), db, VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json"),
               FakeEmbedder(), idle_secs=0)
    db.commit()

    t = db.get_turn(f"{SID}:u1")
    assert t is not None
    assert "기록 완료했습니다." in t.answer, f"{kind} 뒤의 답변이 빠졌다"
    assert [x.tool for x in t.actions] == ["Skill", "Edit"], f"{kind} 뒤의 도구 호출이 빠졌다"
    assert db.get_turn(f"{SID}:m1") is None, "isMeta 기록이 턴이 됐다"
