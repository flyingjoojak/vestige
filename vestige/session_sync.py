"""세션 동기화 — 충돌 리졸버 코어 (Phase 1 / M1).

Syncthing이 같은 세션 파일이 두 기기에서 갈라지면 `<name>.sync-conflict-...`
사본을 만든다. 세션 jsonl은 **append-only 로그**라, 대부분 한쪽이 다른쪽의
prefix(상위집합)다 → 긴 쪽을 채택(superset-wins)하면 무손실로 해소된다.
진짜 분기(어느쪽도 prefix 아님)만 새 세션 id 파일로 보존(fork)한다.

이 모듈은 전송(Syncthing)과 무관한 **순수 로직 + 파일 적용**이라 단독 테스트 가능.
핵심: 충돌은 superset-wins/fork 로 자동 해소한다.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Resolution = Literal["identical", "base_wins", "conflict_wins", "fork"]

# Syncthing 충돌 파일명: <stem>.sync-conflict-<YYYYMMDD>-<HHMMSS>-<DEVID>.<ext>
_CONFLICT_RE = re.compile(
    r"^(?P<stem>.+)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+(?P<ext>\.[^.]+)$"
)


def _read_lines(path: Path) -> list[str]:
    """파일을 논리 줄 리스트로. 끝의 빈 줄(개행 잔여)은 무시."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_strict_prefix(a: list[str], b: list[str]) -> bool:
    """a가 b의 진(strict) prefix인가 — 길이가 더 짧고 앞부분이 일치."""
    return len(a) < len(b) and b[: len(a)] == a


def classify(base_lines: list[str], conflict_lines: list[str]) -> Resolution:
    """append-only 로그 두 버전을 비교해 해소 방식 판정.

    - identical: 완전 동일
    - conflict_wins: base ⊂ conflict (conflict가 최신 상위집합)
    - base_wins: conflict ⊂ base
    - fork: 어느 쪽도 prefix 아님(진짜 분기)
    """
    if base_lines == conflict_lines:
        return "identical"
    if _is_strict_prefix(base_lines, conflict_lines):
        return "conflict_wins"
    if _is_strict_prefix(conflict_lines, base_lines):
        return "base_wins"
    return "fork"


def base_for_conflict(path: Path) -> Path | None:
    """`.sync-conflict-...` 파일 경로 → 원본 세션 파일 경로. 형식이 아니면 None."""
    m = _CONFLICT_RE.match(Path(path).name)
    if not m:
        return None
    return Path(path).with_name(m.group("stem") + m.group("ext"))


@dataclass(frozen=True)
class ConflictOutcome:
    """충돌 해소 1건의 결과(무엇이 남고/지워지고/분기됐는지)."""

    resolution: Resolution
    kept: str                    # 정본으로 남은 세션 파일 경로
    removed: list[str]           # 삭제된 경로
    forked_to: str | None        # fork 시 새 세션 파일 경로(그 외 None)


def resolve_conflict_file(
    base_path: Path, conflict_path: Path, *, new_id: str | None = None
) -> ConflictOutcome:
    """충돌 사본 하나를 정책대로 해소. 파일시스템을 원자적으로 갱신.

    new_id: fork 시 쓸 세션 id(테스트 결정성용). 미지정 시 uuid4.
    """
    base_path = Path(base_path)
    conflict_path = Path(conflict_path)

    if not base_path.exists():
        # 원본이 없으면(희귀) 충돌본을 정본으로 승격.
        os.replace(conflict_path, base_path)
        return ConflictOutcome("conflict_wins", str(base_path), [], None)

    r = classify(_read_lines(base_path), _read_lines(conflict_path))

    if r in ("identical", "base_wins"):
        os.remove(conflict_path)                       # base 유지, 충돌본만 제거
        return ConflictOutcome(r, str(base_path), [str(conflict_path)], None)

    if r == "conflict_wins":
        os.replace(conflict_path, base_path)           # base←conflict(원자적) + 충돌본 제거
        return ConflictOutcome(r, str(base_path), [str(conflict_path)], None)

    # fork: 분기본을 새 세션 id 파일로 보존(무손실). 원본은 그대로 둔다.
    nid = new_id or str(uuid.uuid4())
    fork_path = base_path.with_name(f"{nid}.jsonl")
    os.replace(conflict_path, fork_path)
    return ConflictOutcome(r, str(base_path), [], str(fork_path))


def resolve_all(root: Path) -> list[ConflictOutcome]:
    """root 아래 모든 `.sync-conflict-*` 를 찾아 해소(감시 데몬 M2가 호출)."""
    root = Path(root)
    outcomes: list[ConflictOutcome] = []
    for cf in sorted(root.rglob("*.sync-conflict-*")):
        if not cf.is_file():
            continue
        base = base_for_conflict(cf)
        if base is None:
            continue
        outcomes.append(resolve_conflict_file(base, cf))
    return outcomes


# ── M2: 감시 데몬 ──────────────────────────────────────────────────
# Syncthing이 파일을 나르고, 이 데몬이 도착분의 '충돌'을 정리한다(고유 역할).
# 색인은 기존 인덱싱 스케줄러가 커서로 이미 처리하므로 기본 off — 이중 색인 충돌 방지.
# 스케줄러를 안 쓰는 사용자는 index_fn을 주입해 동기 직후 색인을 붙일 수 있다.


@dataclass(frozen=True)
class SyncTickResult:
    outcomes: list[ConflictOutcome]   # 이번 틱에 해소한 충돌들
    indexed: bool                     # 이번 틱에 색인을 돌렸는지


_last_scan: dict[str, float] = {}   # root -> 마지막 충돌 사본 훑기(monotonic)


def sync_tick(root: Path | None = None, *, index_fn: Callable[[], bool] | None = None,
              min_scan_secs: float = 0.0) -> SyncTickResult:
    """한 번의 점검: 충돌 해소 + (선택) 색인. 데몬/수동(--once) 공용, 단독 테스트 가능."""
    from . import config as C

    root = Path(root) if root is not None else C.PROJECTS_DIR
    # 충돌 사본 찾기는 로그 폴더 전체를 훑는다(디스크 캐시가 식으면 수 초). 앱의 감시 루프는 10초마다
    # 부르므로 min_scan_secs 에 한 번만 훑는다 - 충돌은 드물고, 늦어도 그만큼 뒤에 해소된다.
    now = time.monotonic()
    if now - _last_scan.get(str(root), -1e9) >= min_scan_secs:
        outcomes = resolve_all(root)
        _last_scan[str(root)] = now   # 성공한 뒤에만 - 실패하면 다음 틱에 바로 다시(오류도 그때 다시 드러난다)
    else:
        outcomes = []
    indexed = False
    if index_fn is not None:
        try:
            indexed = bool(index_fn())
        except Exception as ex:  # noqa: BLE001
            print(f"[sync] 색인 실패: {ex}", file=sys.stderr)   # 예전엔 아무 데도 안 남았다
            indexed = False
    return SyncTickResult(outcomes, indexed)


def watch(
    root: Path | None = None,
    *,
    interval: float = 10.0,
    index_fn: Callable[[], bool] | None = None,
    stop: Callable[[], bool] | None = None,
    log_fn: Callable[[str], None] = print,
) -> None:
    """폴더를 주기적으로 점검(충돌 해소 + 선택 색인). stop()이 True를 반환하면 종료."""
    from . import config as C

    root = Path(root) if root is not None else C.PROJECTS_DIR
    log_fn(f"[sync] 감시 시작: {root} (간격 {interval}s)")
    while not (stop and stop()):
        try:
            res = sync_tick(root, index_fn=index_fn)
            for o in res.outcomes:
                extra = f" → fork {o.forked_to}" if o.forked_to else ""
                log_fn(f"[sync] 충돌 해소 {o.resolution}: {o.kept}{extra}")
        except Exception as ex:                       # 데몬은 한 번의 오류로 죽지 않게
            log_fn(f"[sync] 오류: {ex}")
        # stop에 빠르게 반응하도록 잘게 쪼개 대기
        waited = 0.0
        while waited < interval and not (stop and stop()):
            time.sleep(min(0.5, interval - waited))
            waited += 0.5


# ── M3: 활성 세션 가드 ─────────────────────────────────────────────
# 세션 jsonl의 mtime을 '마지막 활동 시각'으로 사용(Syncthing이 원본 mtime을 보존하므로
# 다른 기기의 진행도 그 파일 mtime에 반영됨). 최근 window초 내 수정 = 어딘가에서
# 진행 중일 수 있음 → 이중 재개 시 분기(fork) 위험 경고용. 별도 하트비트 파일 불필요.
# Phase 1은 '경고만'(하드 차단 아님).

ACTIVE_WINDOW_SEC = 300.0   # 이 시간 내 수정이면 '활성 가능성'으로 간주(경고 임계값)


@dataclass(frozen=True)
class Activity:
    active: bool
    seconds_since: float | None   # 마지막 수정 이후 경과초(파일 없으면 None)


def find_session_file(sid: str, root: Path | None = None) -> Path | None:
    """세션 id의 원본 jsonl을 projects 아래에서 찾는다(폴더명 무관 전역 검색)."""
    from . import config as C

    root = Path(root) if root is not None else C.PROJECTS_DIR
    hits = list(root.rglob(f"{sid}.jsonl"))
    return hits[0] if hits else None


def session_activity(
    path: Path | None, *, now: float | None = None, window: float = ACTIVE_WINDOW_SEC
) -> Activity:
    """세션 파일의 최근 수정 여부로 활성 가능성 판정. now는 테스트 주입용."""
    if path is None or not Path(path).exists():
        return Activity(False, None)
    ref = time.time() if now is None else now
    age = ref - Path(path).stat().st_mtime
    return Activity(active=age < window, seconds_since=age)


def default_index(log_fn: Callable[[str], None] = print) -> bool:
    """--index 옵션용 게이트형 증분 색인(새 데이터 있을 때만 모델 로드). 기존 파이프라인 재사용."""
    from .indexer import has_new_data, index_all, reconcile
    from .store import ArchiveDB
    from .vectorindex import make_index

    db = ArchiveDB()
    vi = make_index()
    try:
        reconcile(db, vi, log_fn=log_fn)
    except Exception as ex:
        log_fn(f"[sync] reconcile 오류: {ex}")
    if not has_new_data(db):
        return False
    from .embedder import Embedder

    embedder = Embedder()
    index_all(db, vi, embedder, log_fn=log_fn)
    return True
