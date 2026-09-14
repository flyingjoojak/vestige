"""원본 로그 바이트 보존 (#163 P1).

Claude Code 는 오래된 세션 로그를 자동 정리한다(기본 ~30일). 정리되면 원문 .jsonl 이
사라져 재개(`claude --resume`)가 불가해진다. 색인(턴 추출)과 완전히 분리된 별도 패스로,
세션 파일이 자란 만큼의 바이트를 그대로 압축 보관해뒀다가 필요할 때 되써넣는다(복구는
``restore()`` — 다음 PR).

설계 원칙 - 파싱하지 않는다: 턴 추출은 어댑터가 로그 포맷을 이해해야 하고, 그 포맷이
바뀌면 깨질 수 있다(#125류 드리프트). 원본 미러링은 파일을 바이트로만 보므로 포맷이
뭐든 상관없다 — 오히려 드리프트에 대한 안전망이 된다.

저장 형식 - 멀티멤버 gzip: 한 세션 파일에 새 줄이 추가될 때마다, 새로 늘어난 바이트만
gzip 멤버 하나로 이어 쓴다(``gzip.open`` 은 이어붙인 여러 멤버를 투명하게 순서대로 풀어
읽는다). 매번 전체를 다시 압축할 필요가 없어 대형 세션에서도 값싸다.

오프셋 추적은 ``raw_cursors``(store.py) — turns 의 hold/재처리 로직과 무관한 순수 tail
추적이라 단순하다.
"""
from __future__ import annotations

import gzip
import logging
import re
from pathlib import Path

from . import config as C

logger = logging.getLogger(__name__)

RAW_DIR = C.DATA_DIR / "raw"

# 세션 id는 두 소스 다 파일명에 UUID로 박혀 있다(claude-code: <sid>.jsonl,
# codex: rollout-...-<sid>.jsonl) — 어댑터별 파싱 없이 파일명만으로 뽑아 파싱 의존을 없앤다.
_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def session_id_for(path: str | Path) -> str:
    """파일명에서 세션 id 추출(UUID 패턴 우선, 없으면 stem 폴백). 어댑터/파싱 의존 없음."""
    name = Path(path).name
    m = _UUID_RE.search(name)
    return m.group(1) if m else Path(path).stem


def _sanitize(s: str) -> str:
    # 문자 화이트리스트만으로도 경로 이탈은 불가능(슬래시가 안 남음)하지만, ".." 잔존은 위생상
    # 추가로 없앤다(파일명에 혼란의 소지가 있는 걸 남기지 않는다).
    out = _SAFE_RE.sub("_", s)
    while ".." in out:
        out = out.replace("..", "_")
    return out or "unknown"


def raw_path(source: str, session_id: str) -> Path:
    """이 세션의 압축 원본 경로. source/session_id 는 파일명·UUID에서만 나와 경로이탈 위험이
    없지만, 방어적으로 한 번 더 sanitize."""
    return RAW_DIR / _sanitize(source) / f"{_sanitize(session_id)}.jsonl.gz"


def mirror_file(db, path: str | Path, source: str) -> int:
    """path 의 새 바이트(마지막 미러링 이후분)를 세션별 압축 원본에 append.

    반환: 이번에 미러링한 바이트 수(0이면 새 바이트 없음 — 멱등).
    파일 회전/절단(현재 크기가 이미 미러링한 오프셋보다 작음)이면 처음부터 다시 미러링한다
    (이미 보존해둔 이전 바이트는 그대로 두고 새 멤버로 이어 씀 — 잘라내 덮지 않는 쪽이
    안전: 회전으로 다른 내용이 왔더라도 옛 보존분을 잃지 않는다).
    한 파일 실패가 인덱싱 전체를 막지 않도록 호출자가 감싸는 것을 전제로, 여기서도
    예외를 삼키지 않고 그대로 던진다(호출자의 try/except 격리에 맡김).
    """
    path = str(path)
    size = Path(path).stat().st_size
    mirrored = db.get_raw_cursor(path)
    if mirrored > size:
        mirrored = 0
    if mirrored == size:
        return 0
    with open(path, "rb") as f:
        f.seek(mirrored)
        chunk = f.read()
    if not chunk:
        return 0
    sid = session_id_for(path)
    out = raw_path(source, sid)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "ab") as raw_f, gzip.GzipFile(fileobj=raw_f, mode="wb") as gz:
        gz.write(chunk)
    db.set_raw_cursor(path, size, sid, source)
    db.commit()
    return len(chunk)


def has_mirror(source: str, session_id: str) -> bool:
    return raw_path(source, session_id).exists()


def read_mirror(source: str, session_id: str) -> bytes | None:
    """이 세션의 보존된 원본 바이트 전체(압축 해제). 없으면 None."""
    p = raw_path(source, session_id)
    if not p.exists():
        return None
    with gzip.open(p, "rb") as gz:   # 멀티멤버를 이어서 투명하게 풀어 읽음
        return gz.read()


def mirror_size_bytes() -> int:
    """보존소 전체 용량(압축 상태 기준, 설정 UI 표시용)."""
    if not RAW_DIR.exists():
        return 0
    return sum(p.stat().st_size for p in RAW_DIR.rglob("*.jsonl.gz") if p.is_file())
