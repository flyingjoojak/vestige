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



# --- 파서 버전: 턴을 새로 가를 때 앞 턴이 줄어드는 것을 허용한다 --------------------------

def _t(tid, answer, *, v, queued=False):
    from vestige.models import Turn
    return Turn(id=tid, session_id="S", uuid=tid.split(":")[1], parent_uuid=None, timestamp="T",
                project="/p", question="질문", answer=answer, actions=(),
                queued=queued, parser_version=v)


def test_newer_parser_may_shrink_older_turn(tmp_path):
    """옛 파서가 합쳐 쓴 턴은 새 파서의 짧은 판본으로 덮인다 - 갈라 나간 답변이 양쪽에 안 남는다."""
    from vestige.store import ArchiveDB
    db = ArchiveDB(tmp_path / "t.db")
    db.upsert_turn(_t("S:u1", "답변1\n답변2", v=0), source_file="f")
    assert db.upsert_turn(_t("S:u1", "답변1", v=1), source_file="f") is True
    assert db.get_turn("S:u1").answer == "답변1"


def test_same_parser_still_refuses_to_shrink(tmp_path):
    """같은 파서끼리는 가드가 그대로다 - 부분 읽기로 짧아진 판본은 거부한다."""
    from vestige.store import ArchiveDB
    db = ArchiveDB(tmp_path / "t.db")
    db.upsert_turn(_t("S:u1", "완성된 긴 답변", v=1), source_file="f")
    assert db.upsert_turn(_t("S:u1", "잘림", v=1), source_file="f") is False
    assert db.get_turn("S:u1").answer == "완성된 긴 답변"


def test_older_parser_cannot_overwrite_newer(tmp_path):
    """옛 파서 판본은 더 길어도 새 파서 판본을 못 덮는다 - 합쳐진 턴이 되돌아오지 않게."""
    from vestige.store import ArchiveDB
    db = ArchiveDB(tmp_path / "t.db")
    db.upsert_turn(_t("S:u1", "답변1", v=1), source_file="f")
    assert db.upsert_turn(_t("S:u1", "답변1\n답변2", v=0), source_file="f") is False
    assert db.get_turn("S:u1").answer == "답변1"


def test_full_reindex_splits_merged_turn_without_leftovers(tmp_path, monkeypatch):
    """실제 재색인 경로 끝에서 끝까지: 옛 파서로 색인된 DB 를 새 파서로 다시 읽는다.

    확인하는 것:
    - 앞 턴이 짧아진다(중복 해소)
    - 끼어든 질문이 제 턴으로 생긴다
    - **앞 턴의 뒤쪽 옛 청크가 남지 않는다.** add_chunks 는 같은 키를 덮을 뿐이라,
      짧아진 턴의 청크 수가 줄면 뒤쪽이 옛 내용(갈라 나간 답변)으로 남아 의미 검색에 계속 뜬다.
    """
    import json
    from vestige import parser as P
    from vestige.indexer import index_file
    from vestige.store import ArchiveDB
    from vestige.vectorindex import VectorIndex
    from tests.test_indexer import FakeEmbedder

    late = "끼어든 질문에 대한 긴 답변 " * 400          # 여러 청크가 되게
    f = tmp_path / f"{SID}.jsonl"
    f.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in [
        _user("갱신해줘.", "u1"),
        _assistant("요청서를 다시 씁니다."),
        _queued("그리고 범위 알려줘.", "q1"),
        _assistant(late),
    ]) + "\n", encoding="utf-8")
    db = ArchiveDB(tmp_path / "a.db")
    vi = VectorIndex(tmp_path / "v.npy", tmp_path / "ids.json")

    # 1) 옛 파서: 끼어든 질문을 못 알아보고, 버전 표지도 없던 시절.
    with monkeypatch.context() as m:
        m.setattr(P, "queued_human_prompt", lambda obj: None)
        m.setattr(P, "PARSER_VERSION", 0)
        index_file(str(f), db, vi, FakeEmbedder(), idle_secs=0)
    db.commit()
    before = db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE turn_id=?", (f"{SID}:u1",)).fetchone()["c"]
    assert before > 1, "전제: 합쳐진 앞 턴이 여러 청크여야 뒤쪽 잔여를 볼 수 있다"
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 1

    # 2) 전체 재색인: 커서를 비우고 새 파서로 처음부터.
    db.clear_cursors()
    index_file(str(f), db, vi, FakeEmbedder(), idle_secs=0)
    db.commit()

    head = db.get_turn(f"{SID}:u1")
    assert head.answer == "요청서를 다시 씁니다.", "앞 턴이 줄지 않았다(중복 남음)"
    mid = db.conn.execute("SELECT queued FROM turns WHERE id=?", (f"{SID}:q1",)).fetchone()
    assert mid is not None and mid["queued"] == 1

    left = db.conn.execute("SELECT idx,text FROM chunks WHERE turn_id=?", (f"{SID}:u1",)).fetchall()
    assert all("긴 답변" not in r["text"] for r in left), "앞 턴에 갈라 나간 답변 청크가 남았다"
    stale = {f"{SID}:u1#{i}" for i in range(len(left), before)}
    assert not (stale & set(vi.keys())), "지운 청크의 벡터가 인덱스에 남았다"


def test_adapter_boundaries_match_extract_turns():
    """색인기가 쓰는 턴 경계와 extract_turns 가 만드는 턴 수가 같다.

    색인기는 is_turn_start 로 파일을 자르고 구간마다 재개 지점을 정한다. 기준이 어긋나면
    데이터는 (방어 덕에) 안 사라져도, 재개 지점과 진행 중 턴 보류가 틀린 곳을 가리킨다.
    """
    from vestige.sources.claude_code import ClaudeCodeAdapter

    a = ClaudeCodeAdapter()
    objs = [
        _user("갱신해줘.", "u1"), _assistant("답1"),
        _queued("그리고 범위 알려줘.", "q1"), _assistant("답2"),
        _queued("<task-notification>x</task-notification>", "n1", origin=None), _assistant("계속"),
        {"type": "attachment", "sessionId": SID, "uuid": "t1",
         "attachment": {"type": "total_tokens_reminder"}},
        _user("다음 질문", "u2"), _assistant("답3"),
    ]
    starts = sum(a.is_turn_start(o) for o in objs)
    assert starts == len(a.extract_turns(objs)) == 3
