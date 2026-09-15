"""하이브리드 검색: 의미(벡터) + 키워드(FTS5 BM25)를 RRF로 융합.

- 의미검색: 개념·의역·한↔영에 강함.
- 키워드검색: 정확한 토큰(포트번호·식별자·함수명)에 강함.
- RRF(Reciprocal Rank Fusion): 두 순위를 rank 기반으로 합쳐 양쪽 강점을 취함.
반환물 = 원문(verbatim) + 정제본 + 스레드. 사람용 검색창.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Turn

_RRF_K = 60  # RRF 표준 상수
_KST = timezone(timedelta(hours=9))  # 한국 표준시(고정 오프셋, 서머타임 없음)


def _parse_ts(ts: str) -> datetime | None:
    """저장 타임스탬프(ISO UTC, 'Z' 접미) → aware datetime. Py3.10은 'Z' 미지원이라 치환."""
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _kst_lower_bound(s: str) -> datetime | None:
    """since 하한(포함). 'YYYY-MM-DD'는 그 KST 달력일의 00:00(KST)→UTC. 전체 타임스탬프면 그대로."""
    if len(s) == 10:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_KST)
        return d.astimezone(timezone.utc)
    return _parse_ts(s)


def _kst_upper_bound(s: str) -> datetime | None:
    """until 상한(배타). 'YYYY-MM-DD'는 그날 전체 포함 → 다음 KST일 00:00(KST)→UTC. 전체 타임스탬프면 그대로."""
    if len(s) == 10:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_KST) + timedelta(days=1)
        return d.astimezone(timezone.utc)
    return _parse_ts(s)


@dataclass(frozen=True)
class SearchHit:
    turn: Turn
    score: float                 # RRF 융합 점수(정렬용)
    cosine: float | None = None  # 의미 유사도(의미검색에 잡혔을 때)
    sources: tuple[str, ...] = ()  # 안정 코드: "semantic" / "keyword" (표시 라벨은 프론트에서 번역)
    summary: str | None = None
    tags: tuple[str, ...] = ()
    thread: tuple[Turn, ...] = ()


def _norm_q(q: str) -> str:
    return " ".join(q.lower().split())


def _semantic_turn_ranks(query, db, vi, embedder, depth):
    """의미검색 → 순서 유지 turn_id 리스트 + turn별 최고 cosine."""
    qv = embedder.embed_query(query)
    order: list[str] = []
    cosine: dict[str, float] = {}
    for chunk_key, score in vi.search(qv, k=depth):
        tid = db.turn_id_of_chunk(chunk_key) or chunk_key.rsplit("#", 1)[0]
        if tid not in cosine:
            cosine[tid] = score
            order.append(tid)
    return order, cosine


def search(
    query: str,
    db,
    vi,
    embedder,
    k: int = 5,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    window: int = 2,
    keyword: bool = True,
    semantic: bool = True,
    tool_sources: set[str] | None = None,   # None=전체, 아니면 이 출처(claude-code/codex)만
) -> list[SearchHit]:
    # 세션 스코프면 후보를 크게 잡아 그 세션 턴이 전역 상위 밖이어도 표면화되게 함.
    depth = max(k * 8, 1000) if session else k * 8
    # 의미 끄면(키워드 전용) 임베더 불필요.
    sem_order, cosine = _semantic_turn_ranks(query, db, vi, embedder, depth) if semantic else ([], {})
    kw_order = [tid for tid, _ in db.keyword_search(query, limit=depth)] if keyword else []
    hidden = db.hidden_turn_ids()   # 숨김(#128) - 검색 결과에서 완전히 제외(비파괴, 표시만)

    # RRF 융합
    fused: dict[str, float] = {}
    srcs: dict[str, set] = {}
    for rank, tid in enumerate(sem_order, 1):
        fused[tid] = fused.get(tid, 0.0) + 1.0 / (_RRF_K + rank)
        srcs.setdefault(tid, set()).add("semantic")
    for rank, tid in enumerate(kw_order, 1):
        fused[tid] = fused.get(tid, 0.0) + 1.0 / (_RRF_K + rank)
        srcs.setdefault(tid, set()).add("keyword")

    ranked = sorted(fused, key=lambda t: -fused[t])

    # 날짜 필터는 KST 달력일 기준 → UTC 구간으로 변환(정확한 자정 경계).
    since_dt = _kst_lower_bound(since) if since else None  # 포함 하한
    until_dt = _kst_upper_bound(until) if until else None  # 배타 상한

    hits: list[SearchHit] = []
    seen_questions: set[str] = set()
    for tid in ranked:
        if tid in hidden:
            continue
        turn = db.get_turn(tid)
        if turn is None:
            continue
        if tool_sources and (turn.source or "claude-code") not in tool_sources:
            continue
        if session and not (turn.session_id.startswith(session) or session in turn.project):
            continue
        if since_dt or until_dt:
            tt = _parse_ts(turn.timestamp)
            if tt is not None:
                if since_dt and tt < since_dt:
                    continue
                if until_dt and tt >= until_dt:
                    continue
        nq = _norm_q(turn.question)
        if nq and nq in seen_questions:  # 근접중복 다양화
            continue
        if nq:
            seen_questions.add(nq)
        summary, tags = db.get_enrichment(tid)
        hits.append(
            SearchHit(
                turn=turn,
                score=fused[tid],
                cosine=cosine.get(tid),
                sources=tuple(sorted(srcs.get(tid, set()))),
                summary=summary,
                tags=tuple(tags),
                thread=tuple(db.thread(tid, window)),
            )
        )
        if len(hits) >= k:
            break
    return hits
