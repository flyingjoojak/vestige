"""불변 데이터 모델. 대화의 최소 의미 단위 = Turn(질문+응답+행동)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    """어시스턴트가 한 도구 행동 하나 (코드편집·명령 등)의 요약."""

    tool: str
    detail: str  # 핵심 파라미터 요약 (파일경로/명령 등)

    def render(self) -> str:
        return f"{self.tool}({self.detail})" if self.detail else self.tool


@dataclass(frozen=True)
class Turn:
    """한 턴 = 사용자 질문 1개 + 그에 대한 어시스턴트 응답(행동 포함) 전체.

    id = "<sessionId>:<uuid>" 로 멱등 키. parent_uuid 로 스레드 링크.
    """

    id: str
    session_id: str
    uuid: str
    parent_uuid: str | None
    timestamp: str
    project: str
    question: str
    answer: str
    actions: tuple[Action, ...]
    source: str = "claude-code"   # 출처 도구(claude-code / codex …). DB에서 읽을 때 채워짐.
    queued: bool = False          # 작업 중 끼어든 질문(#246). 화면에서 배지로 구분.

    def action_summary(self) -> str:
        return "; ".join(a.render() for a in self.actions)

    def embed_text(self) -> str:
        """임베딩·검색에 쓰는 텍스트. 원문은 별도 보관되며 이건 파생물."""
        parts = []
        if self.question:
            parts.append(f"Q: {self.question}")
        if self.answer:
            parts.append(f"A: {self.answer}")
        if self.actions:
            parts.append(f"[actions] {self.action_summary()}")
        return "\n".join(parts)
