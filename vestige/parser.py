"""JSONL 로그 → Turn 추출.

- iter_json_lines: 오프셋 커서 기반 tail-safe 증분 읽기 (미완결 꼬리줄 보류).
- 필터 1단계: 구조 노이즈·서브에이전트·명령 배관 완전 제거.
- extract_turns: 남은 줄을 턴(질문+응답+행동)으로 그룹핑.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Iterable, Iterator

from .models import Action, Turn

logger = logging.getLogger(__name__)

# promptSource="sdk" = 프로그램 구동(claude -p 자동화일 수도, 정식 SDK/통합 사용일 수도 있음).
# 기본은 '제외'(대다수는 버릴 자동화). 한 번 색인되면 (휴지통 기능 전까지) 되돌릴 수 없어,
# 손실 없는 쪽을 기본으로 둔다. SDK/통합으로 실제 작업하는 사람은 VESTIGE_SKIP_SDK_SESSIONS=0 로 끈다.
# (system=<task-notification> 등 주입 프롬프트는 promptSource 가 아니라 기존 plumbing/isMeta 필터가 처리한다.)
_SKIP_SDK_ENV = "VESTIGE_SKIP_SDK_SESSIONS"

# 파서 버전. **턴 경계나 내용 배치를 바꾸는 변경을 하면 올린다.**
#
# 저장소의 '완성도 축소 금지' 가드는 로그가 append-only 라 턴이 줄지 않는다는 전제에 선다.
# 그런데 파서가 턴을 새로 가르면(#246: 작업 중 끼어든 질문) 앞 턴이 정당하게 짧아진다.
# 버전이 없으면 가드가 그걸 부분 읽기로 오인해 거부하고, 갈라 나간 답변이 양쪽에 남는다.
# 버전이 오르면 옛 파서가 쓴 행에 한해 **한 번** 덮어쓰기를 허용하고, 그 뒤엔 가드가 원래대로 돈다.
#   1: 작업 중 끼어든 질문(queued_command)을 제 턴으로 가른다(#246)
# codex 어댑터는 자기 파서를 써서 0 에 머문다(그쪽 파싱은 바뀌지 않았다).
PARSER_VERSION = 1

# 대화가 아닌 메타/시스템 라인 타입.
_STRUCTURAL_TYPES = {
    "system",
    "mode",
    "permission-mode",
    "file-history-snapshot",
    "attachment",
    "last-prompt",
    "ai-title",
}

# 사용자 질문이 아닌 주입 텍스트 접두(슬래시명령 배관 + 시스템 이벤트).
_PLUMBING_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<command-stdout>",
    "<task-notification",              # 백그라운드 작업 완료 등 시스템 이벤트
    "[SYSTEM NOTIFICATION",            # 시스템 알림(NOT USER INPUT)
    "<<CHATMEM-ENRICH>>",              # 정제 claude -p 세션(자기오염 방지)
    "다음은 한 Claude Code 세션의 대화 턴들이다",  # 정제 프롬프트 구버전(sentinel 이전)
    "This session is being continued from a previous conversation",  # 컨텍스트 압축 요약(구버전 로그 폴백)
    "<bash-input",                     # `!` bash 모드 입력(터미널 명령, 대화 아님)
    "<bash-stdout",                    # `!` bash 모드 표준출력
    "<bash-stderr",                    # `!` bash 모드 표준오류
    "[Request interrupted",            # 사용자 중단 마커(주입 이벤트)
    "[Image: source:",                 # 이미지 첨부 시 뒤따르는 캐시 참조 레코드(별도 턴 아님).
)                                      #   → 경계로 안 봐야 어시스턴트 응답이 실제 질문 턴에 붙음.

# 행동 상세로 뽑을 우선 키 순서.
_ACTION_KEYS = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query")


# 건너뛴 깨진 줄: 경로 -> 그 줄 끝 오프셋들. 색인기가 파일을 색인한 뒤 꺼내 화면 오류로 올린다.
# 예전엔 경고 로그만 남겨, 그 턴이 영구히 색인에서 빠져도 앱 어디에도 보이지 않았다(로그 규약상
# "ERROR " 로 시작하는 log_fn 메시지만 화면에 닿는다). 같은 줄을 여러 경로가 읽어도(실시간 표시,
# 대기 수 계산 등) 오프셋 집합이라 한 번만 센다.
_bad_lines: dict[str, set[int]] = {}


def take_bad_lines(path: str | Path) -> list[int]:
    """path 에서 건너뛴 깨진 줄의 오프셋들을 꺼낸다(꺼내면 지워진다)."""
    return sorted(_bad_lines.pop(str(path), ()))


def iter_json_lines(path: str | Path, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
    """start_offset 바이트부터 '완결된' JSON 줄만 (obj, end_offset) 로 산출.

    end_offset = 그 줄의 개행 다음 위치 → 다음번 재개 커서.
    개행으로 끝나지 않은 마지막 조각(아직 쓰이는 중)은 산출하지 않는다.
    """
    with open(path, "rb") as f:
        f.seek(start_offset)
        data = f.read()

    offset = start_offset
    parts = data.split(b"\n")
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            break  # 개행 없이 끝난 미완결 꼬리 → 보류
        offset += len(part) + 1  # +1 = 개행
        text = part.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # 손상된(하지만 완결된) 줄은 건너뛸 수밖에 없다 — 다만 무로그로 사라지면 나중에
            # 검색 공백의 원인을 알 수 없으므로, 경로·오프셋·앞부분을 남겨 관측 가능하게 한다.
            logger.warning("손상 JSON 라인 스킵: %s @%d — %.80r", path, offset, text)
            _bad_lines.setdefault(str(path), set()).add(offset)
            continue
        yield obj, offset


def is_structural_noise(obj: dict) -> bool:
    """필터 1단계: 대화가 아니거나 서브에이전트 내부대화면 True."""
    if obj.get("type") in _STRUCTURAL_TYPES:
        return True
    if obj.get("isMeta"):
        return True
    if obj.get("isSidechain"):
        return True
    # 컨텍스트 압축 요약·트랜스크립트 전용 주입물 = 실제 대화 아님(하니스가 넣은 것).
    if obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
        return True
    return False


def _user_text(content) -> str | None:
    """사용자 메시지에서 실제 질문 텍스트. tool_result 전용이면 None."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if texts:
            return "\n".join(texts)
        return None
    return None


def _is_plumbing(text: str) -> bool:
    return text.lstrip().startswith(_PLUMBING_PREFIXES)


def skip_sdk_enabled() -> bool:
    """자동화(promptSource="sdk") 세션 제외 여부. 기본 켜짐.

    끄려면 VESTIGE_SKIP_SDK_SESSIONS=0(false/no/off). 그 외(미설정 포함)는 제외 켜짐.
    """
    return os.environ.get(_SKIP_SDK_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def _is_skipped_sdk_prompt(obj: dict) -> bool:
    """기본 켜짐: promptSource="sdk"(claude -p 등 자동화) 프롬프트를 제외한다.

    한 번 색인하면 휴지통 기능 전까지 못 지우므로, 손실 없는 쪽(제외)을 기본으로 둔다.
    SDK/통합으로 실제 작업하는 사람은 VESTIGE_SKIP_SDK_SESSIONS=0 으로 끈다.
    """
    if not skip_sdk_enabled():
        return False
    return obj.get("promptSource") == "sdk"


def _queued_text(prompt) -> str | None:
    """끼어든 질문의 본문. 보통 평문이지만 이미지가 붙으면 content 블록 리스트를
    문자열로 박제해 둔다("[{'type': 'text', ...}]") — 그 경우 풀어서 텍스트만 꺼낸다."""
    if not isinstance(prompt, str):
        return _user_text(prompt)
    s = prompt.strip()
    if s.startswith("[{") and "'type'" in s[:40]:
        with contextlib.suppress(Exception):   # 박제가 깨졌으면 평문으로 취급
            return _user_text(ast.literal_eval(s))
    return s or None


def queued_human_prompt(obj: dict) -> str | None:
    """작업 중(어시스턴트가 답하는 도중)에 사람이 끼어들어 친 질문이면 그 텍스트.

    Claude Code 는 이걸 일반 대화와 다른 형태로 남긴다 — type="user" 가 아니라
    type="attachment" + attachment.type="queued_command". 그래서 예전엔 구조 노이즈로
    통째로 버려졌고, 질문이 사라진 자리에 그 답변만 앞 턴에 눌어붙었다.

    **origin.kind 를 반드시 본다.** 같은 queued_command 모양으로 사람이 아닌 것도 들어온다
    (이 기기 실측: 사람 411 / <task-notification> 시스템 이벤트 290 / 하위 에이전트 보고 42).
    전부 받으면 시스템 알림과 모델이 쓴 보고서가 사용자 질문으로 둔갑한다.
    """
    if obj.get("type") != "attachment":
        return None
    a = obj.get("attachment") or {}
    if a.get("type") != "queued_command":
        return None
    origin = a.get("origin")
    if not (isinstance(origin, dict) and origin.get("kind") == "human"):
        return None
    text = _queued_text(a.get("prompt"))
    if text is None or _is_plumbing(text):
        return None
    return text


def is_turn_start(obj: dict) -> bool:
    """새 턴이 여기서 시작하는가. **extract_turns 와 색인기가 같은 기준을 써야 한다.**

    색인기(indexer._group_with_offsets)는 이 기준으로 파일을 구간으로 자르고 구간마다 턴 하나를
    기대한다. 두 기준이 어긋나면 구간 하나에서 턴이 여럿 나온다 — #246 직후 실제로 그랬고,
    끼어든 질문과 그 답변이 통째로 버려졌다.

    **반대 방향도 같은 사고다.** extract_turns 는 구조 노이즈(isMeta 등)를 먼저 버리는데, 예전
    이 함수는 노이즈인지 안 봐서 작업 중간에 끼는 isMeta user 기록 — 스킬 본문, 다른 세션이 보낸
    메시지, 이미지를 읽을 때 붙는 `[Image: original …]` — 을 새 턴의 시작으로 봤다. 색인기가 그
    자리에서 구간을 자르면 extract_turns 는 그 구간에서 턴을 하나도 못 만들고, **그 뒤의 답변과
    도구 호출이 어느 턴에도 안 붙은 채 색인에서 빠졌다**(실측: 세션 20개, 거짓 경계 364개).
    """
    if queued_human_prompt(obj) is not None:
        return True   # type=attachment 라 아래 노이즈 검사에 걸리지만, 사람이 친 질문이다
    return not is_structural_noise(obj) and is_real_user_prompt(obj)


def is_real_user_prompt(obj: dict) -> bool:
    """사람이 실제로 친 질문 턴의 시작인지."""
    if obj.get("type") != "user":
        return False
    msg = obj.get("message") or {}
    if msg.get("role") != "user":
        return False
    text = _user_text(msg.get("content"))
    if text is None:
        return False
    if _is_plumbing(text):
        return False
    if _is_skipped_sdk_prompt(obj):   # 기본 켜짐: claude -p 자동화(sdk) 제외(=0 으로 끄면 포함)
        return False
    return True


def is_sdk_prompt(obj: dict) -> bool:
    """제외 대상(promptSource="sdk") 이면서, 그 필터만 없었다면 실제 사용자 질문 턴이었을 프롬프트인지.

    skip 설정과 무관하게 'sdk 여부'만 본다 — 제외 개수 집계용(UI 표기).
    is_real_user_prompt 의 sdk 필터를 뺀 나머지 조건과 동일해야 집계가 정확하다.
    """
    if obj.get("promptSource") != "sdk":
        return False
    if obj.get("type") != "user":
        return False
    msg = obj.get("message") or {}
    if msg.get("role") != "user":
        return False
    text = _user_text(msg.get("content"))
    if text is None:
        return False
    return not _is_plumbing(text)


def _summarize_action(block: dict) -> Action:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    detail = ""
    for key in _ACTION_KEYS:
        if key in inp:
            detail = str(inp[key])
            break
    if not detail and inp:
        detail = ", ".join(inp.keys())
    detail = detail.replace("\n", " ").strip()[:120]
    return Action(tool=name, detail=detail)


def _assistant_parts(obj: dict) -> tuple[list[str], list[Action]]:
    msg = obj.get("message") or {}
    content = msg.get("content")
    texts: list[str] = []
    actions: list[Action] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                texts.append(b.get("text", ""))
            elif bt == "tool_use":
                actions.append(_summarize_action(b))
            # thinking 블록은 임베딩·아카이브에서 제외
    return texts, actions


def _finalize(cur: dict) -> Turn:
    answer = "\n".join(p for p in cur["answer_parts"] if p).strip()
    return Turn(
        id=f'{cur["session_id"]}:{cur["uuid"]}',
        session_id=cur["session_id"],
        uuid=cur["uuid"],
        parent_uuid=cur["parent_uuid"],
        timestamp=cur["timestamp"],
        project=cur["project"],
        question=cur["question"].strip(),
        answer=answer,
        actions=tuple(cur["actions"]),
        queued=cur.get("queued", False),
        parser_version=PARSER_VERSION,
    )


def extract_turns(objs: Iterable[dict]) -> list[Turn]:
    """구조 노이즈를 걸러내며 줄들을 턴으로 그룹핑한다."""
    turns: list[Turn] = []
    cur: dict | None = None
    for obj in objs:
        # 작업 중 끼어든 질문은 type=attachment 라 구조 노이즈 검사에 먼저 걸린다 — 그래서 앞에서 본다.
        queued = queued_human_prompt(obj)
        if queued is None and is_structural_noise(obj):
            continue
        if is_turn_start(obj):
            if cur is not None:
                turns.append(_finalize(cur))
            cur = {
                "session_id": obj.get("sessionId", ""),
                "uuid": obj.get("uuid", ""),
                "parent_uuid": obj.get("parentUuid"),
                "timestamp": obj.get("timestamp", ""),
                "project": obj.get("cwd", ""),
                "question": queued if queued is not None
                            else (_user_text((obj.get("message") or {}).get("content")) or ""),
                "answer_parts": [],
                "actions": [],
                "queued": queued is not None,
            }
        elif obj.get("type") == "assistant" and cur is not None:
            texts, actions = _assistant_parts(obj)
            cur["answer_parts"].extend(texts)
            cur["actions"].extend(actions)
        # tool_result 사용자 메시지·고아 어시스턴트는 무시
    if cur is not None:
        turns.append(_finalize(cur))
    return turns
