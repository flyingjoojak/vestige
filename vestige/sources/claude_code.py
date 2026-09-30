"""Claude Code 소스 어댑터: ~/.claude/projects/**/*.jsonl 세션 로그.

기존 parser.py 로직을 그대로 위임한다 — 동작 100% 동일(파일 이동/경계선 긋기일 뿐).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

from ..models import Turn
from ..parser import extract_turns as _extract_turns
from ..parser import is_turn_start as _is_turn_start
from ..parser import iter_json_lines

# 색인·카운트 제외 폴더: Syncthing 버전 백업(.stversions), vestige 아카이브 스냅샷
# (신규 .vestige-archive + 레거시 .chatmem-archive 둘 다 — 예전 스냅샷도 대화로 재색인되지 않게).
# 버전 백업본은 기기마다 달라 세션 수를 부풀리고 중복을 만든다 → 걸러낸다.
_SKIP_DIRS = {".stversions", ".chatmem-archive", ".vestige-archive"}


class ClaudeCodeAdapter:
    name = "claude-code"
    source_name = "claude-code"   # 저장·검색에 노출되는 출처명(= name)

    def discover(self, root: Path) -> Iterator[Path]:
        for p in root.rglob("*.jsonl"):
            # 백업 폴더 제외 + 서브에이전트 대화는 SubagentAdapter 담당이라 여기선 건너뜀.
            if _SKIP_DIRS.isdisjoint(p.parts) and p.parent.name != "subagents":
                yield p

    def read_records(self, path: str | Path, start_offset: int = 0) -> Iterator[tuple[dict, int]]:
        return iter_json_lines(path, start_offset)

    def is_turn_start(self, obj: dict) -> bool:
        return _is_turn_start(obj)   # extract_turns 와 같은 기준(parser.is_turn_start 참고)

    def extract_turns(self, objs: Iterable[dict]) -> list[Turn]:
        return _extract_turns(objs)
