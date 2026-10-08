"""배경(서브에이전트) 대화 소스 어댑터.

사용자가 오래 운전한 서브에이전트 transcript(`<부모>/subagents/agent-<id>.jsonl`)를 색인한다.
Claude Code 는 서브에이전트 대화를 부모 세션과 같은 폴더의 별도 파일에 남기는데:

- 모든 레코드가 `isSidechain: true` (→ 기본 파서는 서브에이전트 내부대화로 보고 전량 제외)
- 사용자가 SendMessage 로 보낸 지시는 `isMeta: true` user 메시지로 기록됨
  (메인 세션에선 isMeta=하니스 노이즈지만, 서브에이전트에선 그게 곧 진짜 대화)
- 사용자 지시엔 "The user sent a new message while you were working: … This is how
  Claude Code surfaces messages …" 래퍼가 붙는다 → 벗겨서 질문만 남긴다.

일회성 헬퍼 봇(code-reviewer·build-resolver 등)은 최초 Task 프롬프트 1개뿐이라 노이즈다.
그래서 **사람이 보낸 실제 후속 지시(isMeta user)가 THRESHOLD 개 이상인 에이전트만** 색인한다.

세션 분리: session_id 를 부모가 아니라 **agentId** 로 매핑 → 메인 세션과 안 섞인다.
출처(source)는 claude-code(같은 도구) 그대로 — 구분은 세션 단위(source_file 경로로 파생).
"""
from __future__ import annotations

import json
import logging
import mmap
import os
import re
from pathlib import Path
from typing import Iterable, Iterator

from ..models import Turn
from ..parser import (
    _STRUCTURAL_TYPES,
    _user_text,
    extract_turns as _extract_turns,
    is_real_user_prompt,
    iter_json_lines,
)

# 일회성 헬퍼 봇 걸러내기: 사람 후속 지시가 이 수 미만이면 색인 안 함.
_MIN_FOLLOWUPS = 2
_META_TRUE = re.compile(rb'"isMeta"\s*:\s*true')   # 후속 지시 줄의 표지(파싱 전에 거른다)

# 게이트 결과 캐시: 경로 -> ((크기, mtime), 통과 여부).
# discover() 는 /api/index/status 폴링마다 불린다. 게이트를 통과하는 파일은 두 번째
# 후속 지시에서 멈추지만, **통과 못 하는 파일은 조기 종료가 안 들어 매번 전문을 파싱**했다.
# 실측(이 기기): subagents 435개 중 통과 11개, 나머지 424개 482MB 를 폴링마다 다시 읽어
# 1회 5.9초. TTL 8초보다 길어 폴링이 겹치며 서로를 느리게 만들었다.
# ponytail: 삭제된 파일 항목은 남는다(세션당 1개, 수백 규모라 무해).
_gate_cache: dict[str, tuple[tuple[int, float], bool]] = {}

# SendMessage 하니스 래퍼(질문 텍스트에서 제거).
_WRAP_PRE = "The user sent a new message while you were working:"   # 콜론 뒤는 줄바꿈
_WRAP_MARK = "This is how Claude Code surfaces messages the user sends mid-turn"


def _strip_wrapper(text: str) -> str:
    """SendMessage 래퍼를 벗겨 사용자가 실제로 친 문장만 남긴다."""
    t = text.lstrip()
    if t.startswith(_WRAP_PRE):
        t = t[len(_WRAP_PRE):]
    i = t.find(_WRAP_MARK)
    if i != -1:
        t = t[:i]
    return t.strip()


def _is_meta_user_prompt(obj: dict) -> bool:
    """서브에이전트 파일에서 '사람이 보낸 후속 지시'(isMeta user, 실텍스트, 비-plumbing)인지."""
    return isinstance(obj, dict) and bool(obj.get("isMeta")) and is_real_user_prompt(obj)


def _agent_id(objs: Iterable[dict]) -> str | None:
    for o in objs:
        aid = o.get("agentId")
        if aid:
            return str(aid)
    return None


def _is_noise(obj: dict) -> bool:
    """서브에이전트용 노이즈 필터: 구조/압축요약/트랜스크립트전용만 제외.
    isSidechain·isMeta 는 여기선 노이즈가 아님(진짜 대화)."""
    if obj.get("type") in _STRUCTURAL_TYPES:
        return True
    if obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
        return True
    return False


class SubagentAdapter:
    name = "subagent"          # 레지스트리·루트·활성화·drift 키(내부용)
    source_name = "claude-code"  # 저장되는 출처(같은 도구 — 검색 필터/집계엔 claude-code로 나옴)

    def discover(self, root: Path) -> Iterator[Path]:
        """`subagents/` 아래 agent-*.jsonl 중 후속 지시 THRESHOLD 이상인 것만 산출."""
        for p in root.rglob("*.jsonl"):
            if p.parent.name != "subagents":
                continue
            if self._qualifies(p):
                yield p

    def _qualifies(self, path: Path) -> bool:
        """사람 후속 지시가 _MIN_FOLLOWUPS 이상이면 True.

        같은 (크기, mtime) 면 다시 읽지 않는다. 내용이 그대로면 답도 그대로다.
        """
        key = str(path)
        try:
            stt = path.stat()
        except OSError:
            return False
        sig = (stt.st_size, stt.st_mtime)
        hit = _gate_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        ok = self._scan(path)
        if ok is None:
            return False   # 읽기 실패는 캐시하지 않는다 - '후속 지시 부족'으로 굳으면 앱을 다시 켤 때까지 색인되지 않는다
        _gate_cache[key] = (sig, ok)
        return ok

    @staticmethod
    def _scan(path: Path) -> bool | None:
        """조기 종료로 통과 파일은 저렴. 통과 못 하는 파일은 전문을 읽는다(그래서 캐시가 필요).
        읽지 못했으면 None(일시적 오류일 수 있어 캐시하지 않는다)."""
        # "isMeta": true 가 없는 줄은 후속 지시일 수 없다 - 그런 줄은 JSON 파싱을 건너뛴다. 예전엔 모든 줄을
        # 파싱해, 앱을 켠 뒤 처음 훑을 때 하위 세션 505개(6.4만 줄)에 12초(설치본은 색인과 겹쳐 44초)
        # 걸렸고 그동안 세션 목록이 멈췄다.
        seen = 0
        try:
            with open(path, "rb") as f:
                if os.fstat(f.fileno()).st_size == 0:
                    return False
                # 줄로 나누지 않고 파일을 매핑한 채 표지만 찾는다 - 하위 로그는 합쳐 수백 MB 다.
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                    done_to = -1   # 이미 본 줄의 끝 - 한 줄에 표지가 둘이어도 한 번만 센다
                    for hit in _META_TRUE.finditer(m):
                        if hit.start() < done_to:
                            continue
                        start = m.rfind(b"\n", 0, hit.start()) + 1
                        end = m.find(b"\n", hit.end())
                        if end == -1:
                            break   # 개행 없이 끝난 마지막 조각은 아직 쓰이는 중 - 세지 않는다
                        done_to = end
                        try:
                            obj = json.loads(m[start:end])
                        except ValueError:
                            continue
                        if _is_meta_user_prompt(obj):
                            seen += 1
                            if seen >= _MIN_FOLLOWUPS:
                                return True
        except (OSError, ValueError) as e:  # 읽을 수 없는 파일은 이번엔 색인 대상에서 제외하되 남긴다
            logging.getLogger(__name__).warning("하위 에이전트 로그를 읽지 못함 %s: %s", path.name, e)
            return None
        return False

    def read_records(self, path: str | Path, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
        return iter_json_lines(path, start_offset)

    def is_turn_start(self, obj: dict) -> bool:
        # 최초 Task 프롬프트(플래그 없음)와 이후 SendMessage 지시(isMeta) 둘 다 사람 질문.
        return is_real_user_prompt(obj)

    def extract_turns(self, objs: Iterable[dict]) -> list[Turn]:
        objs = list(objs)
        aid = _agent_id(objs)
        prepared = []
        for o in objs:
            if _is_noise(o):
                continue
            o = dict(o)  # 원본 불변 — 얕은 복사 후 정규화
            if aid:
                o["sessionId"] = aid   # 부모 세션과 분리(session_id = agentId)
            o.pop("isSidechain", None)  # 기본 파서가 노이즈로 걸러내지 않도록 플래그 제거
            o.pop("isMeta", None)
            if o.get("type") == "user":
                _strip_wrapper_in_place(o)
            prepared.append(o)
        return _extract_turns(prepared)


def _strip_wrapper_in_place(obj: dict) -> None:
    """user 메시지의 text 블록에서 SendMessage 래퍼 제거(내용 보존)."""
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = _strip_wrapper(content)
    elif isinstance(content, list):
        new = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                b = {**b, "text": _strip_wrapper(b.get("text", ""))}
            new.append(b)
        msg["content"] = new
    obj["message"] = msg
