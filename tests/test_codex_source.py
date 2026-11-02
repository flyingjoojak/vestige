"""Codex CLI 소스 어댑터 테스트.

Codex rollout JSONL 특징(실측 기반):
- 라인 = {timestamp, type, payload} 3키. 세션ID/cwd 는 첫 줄 session_meta 에만 존재.
- 대화가 이중 기록됨: event_msg(UI 깨끗) + response_item(API 원본) → event_msg 채택.
- 스키마 버전 2종:
  - 구형(0.133~0.134): event_msg/user_message · agent_message, response_item/function_call.
  - 신형(0.149+): 모든 항목이 event_msg/item_completed 안 payload.item 으로(item.type/ item.id).
- 파일명 rollout-<ISO>-<uuid>.jsonl 의 uuid == session_meta.payload.id.
"""
from pathlib import Path

from vestige.models import Turn
from vestige.sources import ADAPTERS, SourceAdapter
from vestige.sources.codex import CodexAdapter

SID = "019e80dc-1754-7422-b72f-2d176635efb2"

# ── 구형(0.133~0.134) 픽스처 ──────────────────────────────────
_META = (
    '{"timestamp":"2026-06-01T01:46:09.430Z","type":"session_meta",'
    '"payload":{"id":"' + SID + '","cwd":"/Users/ik/proj",'
    '"originator":"codex-tui","cli_version":"0.134.0"}}'
)
_USER1 = (
    '{"timestamp":"2026-06-01T01:46:09.952Z","type":"event_msg",'
    '"payload":{"type":"user_message","message":"목차 만들어줘","images":[]}}'
)
# response_item 쪽 assistant 텍스트 — event_msg/agent_message 와 중복 → 답변에 들어가면 안 됨
_RESP_ASSISTANT_DUP = (
    '{"timestamp":"2026-06-01T01:46:13.103Z","type":"response_item",'
    '"payload":{"type":"message","role":"assistant",'
    '"content":[{"type":"output_text","text":"DUP_SHOULD_NOT_APPEAR"}]}}'
)
_AGENT1 = (
    '{"timestamp":"2026-06-01T01:46:13.103Z","type":"event_msg",'
    '"payload":{"type":"agent_message","message":"먼저 확인할게요"}}'
)
_FUNC_CALL = (
    '{"timestamp":"2026-06-01T01:47:56.631Z","type":"response_item",'
    '"payload":{"type":"function_call","name":"exec_command",'
    '"arguments":"{\\"cmd\\":\\"sed -n 1,10p f\\",\\"workdir\\":\\"/x\\"}","call_id":"call_1"}}'
)
_CUSTOM_CALL = (
    '{"timestamp":"2026-06-01T01:47:57.000Z","type":"response_item",'
    '"payload":{"type":"custom_tool_call","name":"apply_patch",'
    '"input":"*** Begin Patch\\n*** Add File: a.md\\n+hi","call_id":"call_2"}}'
)
_FUNC_OUT = (
    '{"timestamp":"2026-06-01T01:47:56.715Z","type":"response_item",'
    '"payload":{"type":"function_call_output","call_id":"call_1","output":"ok"}}'
)
_USER2 = (
    '{"timestamp":"2026-06-01T01:48:00.000Z","type":"event_msg",'
    '"payload":{"type":"user_message","message":"고마워"}}'
)
_AGENT2 = (
    '{"timestamp":"2026-06-01T01:48:02.000Z","type":"event_msg",'
    '"payload":{"type":"agent_message","message":"천만에요"}}'
)
_LINES_OLD = [_META, _USER1, _RESP_ASSISTANT_DUP, _AGENT1, _FUNC_CALL, _CUSTOM_CALL, _FUNC_OUT, _USER2, _AGENT2]

# ── 신형(0.149+) 픽스처 ───────────────────────────────────────
_N_META = (
    '{"timestamp":"2026-08-21T03:52:00.400Z","type":"session_meta",'
    '"payload":{"id":"' + SID + '","cwd":"/c/proj","cli_version":"0.149.0"}}'
)
_N_USER = (
    '{"timestamp":"2026-08-21T03:52:00.911Z","ordinal":9,"type":"event_msg",'
    '"payload":{"type":"item_completed","thread_id":"' + SID + '","turn_id":"t1",'
    '"item":{"type":"UserMessage","id":"u_1","content":[{"type":"text","text":"안녕"}]}}}'
)
_N_REASON = (
    '{"timestamp":"2026-08-21T03:52:01.000Z","type":"event_msg",'
    '"payload":{"type":"item_completed","item":{"type":"Reasoning","id":"rs_1",'
    '"summary_text":[],"raw_content":[]}}}'
)
_N_AGENT = (
    '{"timestamp":"2026-08-21T03:52:02.000Z","type":"event_msg",'
    '"payload":{"type":"item_completed","item":{"type":"AgentMessage","id":"a_1",'
    '"content":[{"type":"Text","text":"안녕하세요!"}],"phase":"final_answer"}}}'
)
_N_CMD = (
    '{"timestamp":"2026-08-21T03:52:03.000Z","type":"event_msg",'
    '"payload":{"type":"item_completed","item":{"type":"CommandExecution","id":"exec-1",'
    '"command":["bash","-lc","ls -la"],"parsed_cmd":[{"type":"list","cmd":"ls -la"}],"status":"completed"}}}'
)
# 신형에서도 response_item/custom_tool_call 이 남지만 item_completed 와 중복 → 무시돼야 함
_N_RESP_DUP = (
    '{"timestamp":"2026-08-21T03:52:03.100Z","type":"response_item",'
    '"payload":{"type":"custom_tool_call","name":"apply_patch","input":"DUP","call_id":"c1"}}'
)
_LINES_NEW = [_N_META, _N_USER, _N_REASON, _N_AGENT, _N_CMD, _N_RESP_DUP]


def _write_rollout(dirpath: Path, lines, name_sid: str = SID) -> Path:
    d = dirpath / "2026" / "06" / "01"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-06-01T10-46-08-{name_sid}.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _turns_from_file(f: Path) -> list[Turn]:
    """인덱서의 _group_with_offsets 와 동일하게 턴 경계로 잘라 추출."""
    a = CodexAdapter()
    recs = [o for o, _e in a.read_records(f, 0)]
    starts = [i for i, o in enumerate(recs) if a.is_turn_start(o)]
    out: list[Turn] = []
    for k, si in enumerate(starts):
        sj = starts[k + 1] if k + 1 < len(starts) else len(recs)
        out += a.extract_turns(recs[si:sj])
    return out


# ── 등록/프로토콜 ──────────────────────────────────────────────
def test_codex_registered_and_satisfies_protocol():
    assert "codex" in ADAPTERS
    a = ADAPTERS["codex"]
    assert a.name == "codex"
    assert isinstance(a, SourceAdapter)


# ── discover ──────────────────────────────────────────────────
def test_discover_finds_rollout_and_skips_backups(tmp_path: Path):
    _write_rollout(tmp_path, _LINES_OLD)
    (tmp_path / ".stversions").mkdir()
    (tmp_path / ".stversions" / "rollout-old-x.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".vestige-archive").mkdir()
    (tmp_path / ".vestige-archive" / "rollout-arch-y.jsonl").write_text("{}\n", encoding="utf-8")
    # 레거시 아카이브 폴더도 계속 제외돼야 함(back-compat)
    (tmp_path / ".chatmem-archive").mkdir()
    (tmp_path / ".chatmem-archive" / "rollout-arch-z.jsonl").write_text("{}\n", encoding="utf-8")
    # rollout- 이 아닌 파일은 무시
    (tmp_path / "2026" / "06" / "01" / "notes.jsonl").write_text("{}\n", encoding="utf-8")

    found = {p.name for p in CodexAdapter().discover(tmp_path)}
    assert found == {f"rollout-2026-06-01T10-46-08-{SID}.jsonl"}


# ── is_turn_start ─────────────────────────────────────────────
def test_is_turn_start_old_and_new_and_negatives():
    a = CodexAdapter()
    # 구형
    assert a.is_turn_start({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}})
    # 신형
    assert a.is_turn_start({"type": "event_msg", "payload": {"type": "item_completed",
                            "item": {"type": "UserMessage", "content": [{"type": "text", "text": "hi"}]}}})
    # 부정
    assert not a.is_turn_start({"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}})
    assert not a.is_turn_start({"type": "event_msg", "payload": {"type": "item_completed",
                                "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": "x"}]}}})
    assert not a.is_turn_start({"type": "response_item", "payload": {"type": "message", "role": "user"}})
    assert not a.is_turn_start({"type": "session_meta", "payload": {"id": SID}})
    assert not a.is_turn_start({"type": "event_msg", "payload": {"type": "user_message", "message": "  "}})


# ── read_records: 컨텍스트 주입 + 오프셋 ──────────────────────
def test_read_records_annotates_session_cwd_offset(tmp_path: Path):
    f = _write_rollout(tmp_path, _LINES_OLD)
    recs = list(CodexAdapter().read_records(f, 0))
    assert recs
    for obj, end in recs:
        assert obj["_codex_session_id"] == SID
        assert obj["_codex_cwd"] == "/Users/ik/proj"
        assert obj["_codex_off"] == end
    offs = [end for _o, end in recs]
    assert offs == sorted(offs)
    assert offs[-1] == f.stat().st_size


def test_read_records_incremental_still_has_meta(tmp_path: Path):
    """start_offset>0 로 session_meta 를 건너뛰어도 세션ID/cwd 를 첫 줄에서 복원한다."""
    f = _write_rollout(tmp_path, _LINES_OLD)
    meta_len = len((_META + "\n").encode("utf-8"))
    recs = list(CodexAdapter().read_records(f, meta_len))
    assert recs
    assert "session_meta" not in [o.get("type") for o, _e in recs]
    for obj, _e in recs:
        assert obj["_codex_session_id"] == SID
        assert obj["_codex_cwd"] == "/Users/ik/proj"


def test_session_id_falls_back_to_filename_when_no_meta(tmp_path: Path):
    f = _write_rollout(tmp_path, [_USER1, _AGENT1])  # meta 없음
    for obj, _e in CodexAdapter().read_records(f, 0):
        assert obj["_codex_session_id"] == SID  # 파일명에서
        assert obj["_codex_cwd"] == ""


def test_session_id_falls_back_when_meta_missing_id(tmp_path: Path):
    """session_meta 는 있으나 payload 에 id/session_id 가 없으면 파일명 폴백."""
    meta_no_id = '{"timestamp":"t","type":"session_meta","payload":{"cwd":"/p"}}'
    f = _write_rollout(tmp_path, [meta_no_id, _USER1])
    obj = next(iter(CodexAdapter().read_records(f, 0)))[0]
    assert obj["_codex_session_id"] == SID
    assert obj["_codex_cwd"] == "/p"


def test_read_records_corrupt_first_line_falls_back(tmp_path: Path):
    f = _write_rollout(tmp_path, ["NOT JSON {{{", _USER1])
    obj = next(iter(CodexAdapter().read_records(f, 0)))[0]
    assert obj["_codex_session_id"] == SID
    assert obj["_codex_cwd"] == ""


# ── 자동화(codex exec) 세션은 기본 제외 ─────────────────────────
_META_EXEC = _META.replace('"originator":"codex-tui"', '"originator":"codex_exec","source":"exec"')


def test_exec_session_skipped_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VESTIGE_SKIP_SDK_SESSIONS", raising=False)
    f = _write_rollout(tmp_path, [_META_EXEC, _USER1, _AGENT1])
    assert list(CodexAdapter().read_records(f, 0)) == []


def test_exec_session_kept_when_skip_off(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VESTIGE_SKIP_SDK_SESSIONS", "0")
    f = _write_rollout(tmp_path, [_META_EXEC, _USER1, _AGENT1])
    assert list(CodexAdapter().read_records(f, 0))


def test_interactive_session_never_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VESTIGE_SKIP_SDK_SESSIONS", raising=False)
    f = _write_rollout(tmp_path, [_META, _USER1, _AGENT1])   # originator=codex-tui
    assert list(CodexAdapter().read_records(f, 0))


def test_automated_turns_counts_exec_only(tmp_path: Path, monkeypatch):
    from vestige.sources.codex import automated_turns
    monkeypatch.delenv("VESTIGE_SKIP_SDK_SESSIONS", raising=False)
    exec_f = _write_rollout(tmp_path, [_META_EXEC, _USER1, _AGENT1, _USER2, _AGENT2])
    tui_f = _write_rollout(tmp_path / "tui", [_META, _USER1, _AGENT1])
    assert automated_turns(exec_f) == 2
    assert automated_turns(tui_f) == 0


def test_skipped_exec_file_leaves_no_pending_and_no_work(tmp_path: Path, monkeypatch):
    """제외된 자동화 파일은 커서를 끝으로 둔다 - 안 그러면 '대기 N'과 has_new_data 가 영영 참이라
    색인 회차마다 임베딩 모델을 올린다."""
    from vestige.indexer import count_pending, has_new_data, index_file
    from vestige.store import ArchiveDB
    monkeypatch.delenv("VESTIGE_SKIP_SDK_SESSIONS", raising=False)
    root = tmp_path / "sessions"
    f = _write_rollout(root, [_META_EXEC, _USER1, _AGENT1])
    db = ArchiveDB(tmp_path / "a.db")
    monkeypatch.setattr("vestige.indexer._iter_all", lambda _p=None: [(CodexAdapter(), f)])
    assert has_new_data(db) is True                      # 색인 전에는 한 번 대기로 잡힌다
    assert index_file(f, db, object(), object(), adapter=CodexAdapter(), idle_secs=0) == 0
    assert db.get_cursor(str(f))[0] == f.stat().st_size
    assert db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 0
    assert has_new_data(db) is False
    assert count_pending(db, root)["files"] == 0


# ── extract_turns: 구형 ───────────────────────────────────────
def test_extract_turns_old_schema(tmp_path: Path):
    turns = _turns_from_file(_write_rollout(tmp_path, _LINES_OLD))
    assert len(turns) == 2
    t = turns[0]
    assert isinstance(t, Turn)
    assert t.session_id == SID
    assert t.project == "/Users/ik/proj"
    assert t.question == "목차 만들어줘"
    assert t.answer == "먼저 확인할게요"
    assert "DUP_SHOULD_NOT_APPEAR" not in t.answer  # response_item 중복 배제
    assert t.id == f"{SID}:{t.uuid}"
    assert t.uuid.startswith("@")  # 구형은 바이트 오프셋 기반
    tools = {act.tool for act in t.actions}
    assert "exec_command" in tools and "apply_patch" in tools
    assert any("sed -n 1,10p f" in act.detail for act in t.actions)
    assert turns[1].question == "고마워" and turns[1].answer == "천만에요"


def test_extract_turns_old_ids_unique_on_same_timestamp(tmp_path: Path):
    """같은 밀리초 타임스탬프의 두 사용자 턴도 오프셋 덕에 id 가 갈린다(덮어쓰기 방지)."""
    ts = '2026-06-01T01:46:09.952Z'
    u = lambda m: ('{"timestamp":"' + ts + '","type":"event_msg",'
                   '"payload":{"type":"user_message","message":"' + m + '"}}')
    f = _write_rollout(tmp_path, [_META, u("첫번째"), u("두번째")])
    turns = _turns_from_file(f)
    assert len(turns) == 2
    assert turns[0].id != turns[1].id


# ── extract_turns: 신형 ───────────────────────────────────────
def test_extract_turns_new_schema(tmp_path: Path):
    turns = _turns_from_file(_write_rollout(tmp_path, _LINES_NEW))
    assert len(turns) == 1
    t = turns[0]
    assert t.session_id == SID
    assert t.project == "/c/proj"
    assert t.question == "안녕"
    assert t.answer == "안녕하세요!"
    assert t.uuid == "u_1"  # 신형은 item.id 사용
    assert t.id == f"{SID}:u_1"
    # 행동: CommandExecution 만 잡히고, response_item/custom_tool_call(중복)은 무시
    assert [act.tool for act in t.actions] == ["exec"]
    assert "ls -la" in t.actions[0].detail


def test_extract_turns_empty_when_no_user():
    a = CodexAdapter()
    assert a.extract_turns([{"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}}]) == []
