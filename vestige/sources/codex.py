"""OpenAI Codex CLI 소스 어댑터: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``.

Claude Code 와 다른 세 가지를 이 어댑터가 흡수한다(파이프라인은 그대로):

1. **세션 컨텍스트가 첫 줄에만 있다.** 라인은 ``{timestamp, type, payload}`` 3키뿐이고
   세션ID·cwd 는 파일 첫 줄 ``session_meta`` 에만 있다. 인덱서는 턴 슬라이스만
   ``extract_turns`` 로 넘기므로(session_meta 제외), ``read_records`` 가 파일 첫 줄에서
   컨텍스트를 읽어 각 레코드에 ``_codex_session_id`` / ``_codex_cwd`` / ``_codex_off``(바이트
   오프셋)로 주입한다. 증분 읽기(start_offset>0)로 첫 줄을 건너뛰어도 첫 줄을 따로 읽어 복원.

2. **스키마가 버전마다 다르다(둘 다 지원).**
   - 구형(cli 0.133~0.134): ``event_msg/user_message`` · ``event_msg/agent_message`` ·
     ``response_item/function_call|custom_tool_call``.
   - 신형(cli 0.149+): 모든 항목이 ``event_msg/item_completed`` 안 ``payload.item`` 으로 옴
     (``item.type`` ∈ UserMessage/AgentMessage/Reasoning/CommandExecution/Extension), ``item.id`` 보유.
   턴 시작을 어느 쪽 사용자메시지로 잡았는지에 따라 그 턴의 수집 모드를 고정한다.

3. **대화가 이중 기록된다.** ``event_msg/*`` (UI 깨끗) 와 ``response_item/*`` (API 원본+환경 래핑)에
   같은 발화가 둘 다 남는다. 구형 턴은 event_msg(질문/답변)+response_item(행동만), 신형 턴은
   item_completed 만 사용(response_item 전부 무시)해 **중복·이중계산을 피한다.** 추론은 암호화라 제외.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable, Iterator

from ..models import Action, Turn
from ..parser import iter_json_lines

logger = logging.getLogger(__name__)
_WARNED_ITEMS: set = set()   # 한 번씩만 경고

# 버전 백업/아카이브 폴더 제외(Claude Code 어댑터와 동일 정책).
_SKIP_DIRS = {".stversions", ".chatmem-archive", ".vestige-archive"}

# 파일명 rollout-<ISO>-<uuid>.jsonl 의 세션 UUID(8-4-4-4-12) 추출.
_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# function_call arguments(JSON 문자열)에서 행동 상세로 뽑을 우선 키.
_ARG_KEYS = ("cmd", "command", "file_path", "path", "query", "url", "pattern", "workdir")

_SID_KEY = "_codex_session_id"
_CWD_KEY = "_codex_cwd"
_OFF_KEY = "_codex_off"


def _sid_from_name(path: str | Path) -> str:
    m = _UUID_RE.search(Path(path).name)
    return m.group(1) if m else Path(path).stem


def _read_session_ctx(path: str | Path) -> tuple[str, str]:
    """파일 첫 줄에서 (session_id, cwd) 복원. 첫 줄이 session_meta 가 아니면 파일명 폴백.

    ``read_records`` 가 index_file 호출당 파일마다 한 번만 부르는 것을 전제로 한 값싼 첫 줄 읽기.
    """
    sid_fallback = _sid_from_name(path)
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return sid_fallback, ""
    if not first.strip():
        return sid_fallback, ""
    try:
        obj = json.loads(first.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return sid_fallback, ""
    if obj.get("type") != "session_meta":
        return sid_fallback, ""
    payload = obj.get("payload") or {}
    sid = payload.get("id") or payload.get("session_id") or sid_fallback
    return sid, (payload.get("cwd") or "")


def _payload_type(obj: dict) -> str:
    payload = obj.get("payload")
    return payload.get("type", "") if isinstance(payload, dict) else ""


def _blocks_text(content) -> str:
    """content 블록 리스트에서 텍스트를 뽑는다(text/Text/input_text/output_text 모두 커버)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = [
            b["text"]
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str) and b.get("text")
        ]
        return "\n".join(out)
    return ""


def _summarize_call(payload: dict) -> Action:
    """구형 response_item/function_call · custom_tool_call → Action 요약."""
    name = payload.get("name", "tool")
    detail = ""
    args = payload.get("arguments")
    if isinstance(args, str) and args.strip().startswith("{"):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in _ARG_KEYS:
                if key in parsed and parsed[key]:
                    detail = str(parsed[key])
                    break
            if not detail and parsed:
                detail = ", ".join(parsed.keys())
    if not detail:
        # custom_tool_call(apply_patch 등)은 input(문자열)만 있음.
        raw = payload.get("input") or payload.get("arguments") or ""
        detail = str(raw)
    detail = detail.replace("\n", " ").strip()[:120]
    return Action(tool=name, detail=detail)


def _summarize_item(item: dict) -> Action:
    """신형 item_completed 의 CommandExecution / Extension → Action 요약."""
    itt = item.get("type", "tool")
    if itt == "CommandExecution":
        detail = ""
        parsed = item.get("parsed_cmd")
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            detail = parsed[0].get("cmd") or parsed[0].get("name") or ""
        if not detail:
            cmd = item.get("command")
            detail = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
        return Action(tool="exec", detail=detail.replace("\n", " ").strip()[:120])
    if itt == "Extension":
        kind = item.get("kind") or "extension"
        query = item.get("query") or ""
        return Action(tool=str(kind), detail=str(query).replace("\n", " ").strip()[:120])
    return Action(tool=str(itt), detail="")


class CodexAdapter:
    name = "codex"
    source_name = "codex"   # 저장·검색에 노출되는 출처명(= name)

    def discover(self, root: Path) -> Iterator[Path]:
        for p in root.rglob("rollout-*.jsonl"):
            if _SKIP_DIRS.isdisjoint(p.parts):
                yield p

    def read_records(self, path: str | Path, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
        sid, cwd = _read_session_ctx(path)
        for obj, end in iter_json_lines(path, start_offset):
            obj[_SID_KEY] = sid
            obj[_CWD_KEY] = cwd
            obj[_OFF_KEY] = end  # 라인의 절대 바이트 위치 = append-only 파일에서 안정·유일한 턴 앵커.
            yield obj, end

    def _user_message(self, obj: dict) -> str | None:
        """이 레코드가 사람의 질문이면 그 텍스트, 아니면 None(구형·신형 둘 다)."""
        if obj.get("type") != "event_msg":
            return None
        pt = _payload_type(obj)
        payload = obj.get("payload") or {}
        if pt == "user_message":  # 구형
            msg = payload.get("message")
            return msg if isinstance(msg, str) else None
        if pt == "item_completed":  # 신형
            item = payload.get("item") or {}
            if item.get("type") == "UserMessage":
                return _blocks_text(item.get("content"))
        return None

    def is_turn_start(self, obj: dict) -> bool:
        text = self._user_message(obj)
        return text is not None and bool(text.strip())

    def extract_turns(self, objs: Iterable[dict]) -> list[Turn]:
        """event_msg(신형=item_completed) 스트림 기준으로 턴(질문+답변+행동)을 만든다.

        슬라이스는 사용자 메시지로 시작한다고 가정하되 안전하게 방어한다.
        세션ID/cwd 는 주입값 → 없으면 슬라이스 내 session_meta → 그래도 없으면 "".
        """
        turns: list[Turn] = []
        cur: dict | None = None
        ctx_sid, ctx_cwd = "", ""

        for obj in objs:
            # 슬라이스에 session_meta 가 섞여 온 경우(직접 호출 등)도 컨텍스트로 인정.
            if obj.get("type") == "session_meta":
                payload = obj.get("payload") or {}
                ctx_sid = payload.get("id") or payload.get("session_id") or ctx_sid
                ctx_cwd = payload.get("cwd") or ctx_cwd
                continue

            sid = obj.get(_SID_KEY) or ctx_sid
            cwd = obj.get(_CWD_KEY) or ctx_cwd
            pt = _payload_type(obj)

            question = self._user_message(obj)
            if question is not None and question.strip():
                if cur is not None:
                    turns.append(_finalize(cur))
                cur = {
                    "session_id": sid,
                    "uuid": _turn_uuid(obj, pt),
                    "timestamp": obj.get("timestamp", ""),
                    "project": cwd,
                    "mode": "new" if pt == "item_completed" else "old",
                    "question": question.strip(),
                    "answer_parts": [],
                    "actions": [],
                }
                continue

            if cur is None:
                continue

            _collect(cur, obj, pt)

        if cur is not None:
            turns.append(_finalize(cur))
        return turns


def _turn_uuid(obj: dict, pt: str) -> str:
    """턴의 안정·유일 키. 신형=item.id, 구형=바이트 오프셋(타임스탬프는 큐잉 시 충돌 가능)."""
    if pt == "item_completed":
        item_id = ((obj.get("payload") or {}).get("item") or {}).get("id")
        if item_id:
            return str(item_id)
    off = obj.get(_OFF_KEY)
    if off is not None:
        return f"@{off}"
    return obj.get("timestamp", "")  # 최후 폴백(주입 안 된 직접호출 등)


def _collect(cur: dict, obj: dict, pt: str) -> None:
    """진행 중인 턴에 답변/행동을 모드에 맞게 수집한다."""
    otype = obj.get("type")
    payload = obj.get("payload") or {}
    if cur["mode"] == "old":
        if otype == "event_msg" and pt == "agent_message":
            text = payload.get("message") or ""
            if text.strip():
                cur["answer_parts"].append(text)
        elif otype == "response_item" and pt in ("function_call", "custom_tool_call"):
            cur["actions"].append(_summarize_call(payload))
        return
    # 신형: item_completed 만 사용(response_item 은 중복이라 무시).
    if otype == "event_msg" and pt == "item_completed":
        item = payload.get("item") or {}
        itt = item.get("type")
        if itt == "AgentMessage":
            text = _blocks_text(item.get("content"))
            if text.strip():
                cur["answer_parts"].append(text)
        elif itt in ("CommandExecution", "Extension"):
            cur["actions"].append(_summarize_item(item))
        elif itt not in ("UserMessage", "Reasoning"):
            # 새 버전이 모르는 항목을 내면 그 내용이 턴에서 빠진다 - 디버그 로그는 아무 데도 안 남았다
            if itt not in _WARNED_ITEMS:
                _WARNED_ITEMS.add(itt)
                logger.warning("codex: 처리 안 하는 item_completed 종류 %r - 그 내용은 색인에서 빠진다", itt)


def _finalize(cur: dict) -> Turn:
    answer = "\n".join(p for p in cur["answer_parts"] if p).strip()
    sid = cur["session_id"]
    uuid = cur["uuid"]
    return Turn(
        id=f"{sid}:{uuid}",
        session_id=sid,
        uuid=uuid,
        parent_uuid=None,  # Codex 라인엔 턴 링크용 uuid 가 없다(스레드 링크 미사용).
        timestamp=cur["timestamp"],
        project=cur["project"],
        question=cur["question"],
        answer=answer,
        actions=tuple(cur["actions"]),
    )
