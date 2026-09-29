"""작업 중 끼어든 질문(#246).

Claude Code 는 어시스턴트가 답하는 도중에 친 메시지를 type="user" 가 아니라
type="attachment" + attachment.type="queued_command" 로 남긴다. 예전엔 구조 노이즈로
통째로 버려져서, 질문이 사라진 자리에 그 답변만 앞 턴에 눌어붙었다.
"""
from vestige.parser import extract_turns, queued_human_prompt

SID = "20dd97cc-b910-4123-b416-f1774a542631"


def _user(text, uuid):
    return {"type": "user", "sessionId": SID, "uuid": uuid, "parentUuid": None,
            "timestamp": "2026-09-29T00:00:00Z", "cwd": "/c/proj",
            "message": {"role": "user", "content": text}}


def _assistant(text):
    return {"type": "assistant", "sessionId": SID,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _queued(prompt, uuid, *, origin={"kind": "human"}, ts="2026-09-29T00:01:00Z"):
    return {"type": "attachment", "sessionId": SID, "uuid": uuid, "parentUuid": None,
            "timestamp": ts, "cwd": "/c/proj",
            "attachment": {"type": "queued_command", "prompt": prompt,
                           "origin": origin, "source_uuid": "x", "timestamp": ts}}


def test_midturn_question_becomes_its_own_turn():
    """끼어든 질문 이후의 답변은 앞 턴이 아니라 새 턴에 붙는다."""
    turns = extract_turns([
        _user("갱신해줘.", "u1"),
        _assistant("요청서 파일은 재클론 때 사라졌습니다."),
        _queued("그리고 테스트 할 수 있는 범위와 못하는 범위 알려줘.", "q1"),
        _assistant("라우트 상황 확정했습니다."),
    ])
    assert len(turns) == 2, f"턴이 갈리지 않았다: {[t.question for t in turns]}"
    assert turns[0].question == "갱신해줘."
    assert turns[0].answer == "요청서 파일은 재클론 때 사라졌습니다."
    assert turns[1].question == "그리고 테스트 할 수 있는 범위와 못하는 범위 알려줘."
    assert turns[1].answer == "라우트 상황 확정했습니다."
    assert turns[1].id == f"{SID}:q1"
    assert turns[0].queued is False and turns[1].queued is True


def test_system_notification_is_not_a_question():
    """<task-notification> 은 같은 queued_command 모양으로 들어오지만 사람이 아니다.

    실측(이 기기): 사람 411 / 시스템 이벤트 290 / 하위 에이전트 보고 42. origin 을 안 보면
    시스템 알림 290건이 전부 '사용자 질문'으로 둔갑한다.
    """
    obj = _queued("<task-notification>\n<task-id>bie18bu1h</task-id>", "n1", origin=None)
    assert queued_human_prompt(obj) is None
    assert len(extract_turns([_user("질문", "u1"), _assistant("답"), obj, _assistant("계속")])) == 1


def test_subagent_handback_is_not_a_question():
    """하위 에이전트 보고는 모델 출력이다 - 사용자 질문으로 넣으면 권한을 위조하게 된다."""
    obj = _queued("[Subagent hand-back] 최종 보고입니다", "p1",
                  origin={"kind": "peer", "from": "a810222027cc72fc1"})
    assert queued_human_prompt(obj) is None


def test_image_prompt_is_unwrapped():
    """이미지가 붙으면 prompt 가 content 블록 리스트를 문자열로 박제한 형태로 온다."""
    obj = _queued("[{'type': 'text', 'text': '이거 왜 이렇게 다르니'}]", "i1")
    assert queued_human_prompt(obj) == "이거 왜 이렇게 다르니"


def test_broken_repr_falls_back_to_raw_text():
    """박제가 깨져도 조용히 버리지 않고 원문을 남긴다 - 질문 유실이 더 나쁘다."""
    obj = _queued("[{'type': 'text', 'text': 끊김", "b1")
    assert queued_human_prompt(obj) == "[{'type': 'text', 'text': 끊김"


def test_other_attachments_stay_noise():
    """queued_command 가 아닌 attachment(총 3만건 넘는 토큰 알림 등)는 계속 노이즈다."""
    obj = {"type": "attachment", "sessionId": SID, "uuid": "t1",
           "attachment": {"type": "total_tokens_reminder", "tokens": 123}}
    assert queued_human_prompt(obj) is None
    assert len(extract_turns([_user("질문", "u1"), _assistant("답"), obj, _assistant("계속")])) == 1


def test_reindex_recovers_question_but_leaves_old_answer_duplicated(tmp_path):
    """재색인은 잃어버린 질문을 되살리지만, 앞 턴의 답변은 줄지 않는다.

    upsert_turn 에는 '완성도 축소 금지' 가드가 있다(긴 도구호출로 확정된 턴을 재색인·기기병합이
    되돌리는 것을 막는 장치). 그래서 갈라진 뒤 짧아진 앞 턴은 덮이지 않고, 갈라 나간 답변이
    양쪽에 남는다. 의도된 동작이므로 여기 고정해 둔다 - 바꾸려면 가드부터 다시 논의해야 한다.
    """
    from vestige.models import Turn
    from vestige.store import ArchiveDB

    db = ArchiveDB(tmp_path / "t.db")
    merged = Turn(id="S:u1", session_id="S", uuid="u1", parent_uuid=None, timestamp="T",
                  project="/p", question="갱신해줘.", answer="답변1\n답변2", actions=())
    db.upsert_turn(merged, source_file="f.jsonl")

    split1 = Turn(id="S:u1", session_id="S", uuid="u1", parent_uuid=None, timestamp="T",
                  project="/p", question="갱신해줘.", answer="답변1", actions=())
    split2 = Turn(id="S:q1", session_id="S", uuid="q1", parent_uuid=None, timestamp="T2",
                  project="/p", question="그리고 범위 알려줘", answer="답변2", actions=(), queued=True)
    assert db.upsert_turn(split1, source_file="f.jsonl") is False, "가드가 사라졌다"
    assert db.upsert_turn(split2, source_file="f.jsonl") is True
    db.commit()

    rows = {r["id"]: r for r in db.conn.execute("SELECT id,answer,queued FROM turns")}
    assert rows["S:q1"]["queued"] == 1
    assert rows["S:u1"]["answer"] == "답변1\n답변2"   # 안 줄어든다 = 답변2 가 양쪽에 있다
