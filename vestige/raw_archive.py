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
import json
import logging
import re
from pathlib import Path

from . import config as C

logger = logging.getLogger(__name__)


def raw_dir() -> Path:
    """보존소 루트. 호출 시점에 설정을 조회한다 — 설정 화면에서 경로를 바꾸면(config reload)
    재시작 없이 바로 새 경로에 쌓인다. 경로를 바꿔도 기존 보존분을 옮기지는 않는다."""
    return C.RAW_ARCHIVE_DIR


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
    return raw_dir() / _sanitize(source) / f"{_sanitize(session_id)}.jsonl.gz"


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
    root = raw_dir()
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*.jsonl.gz") if p.is_file())


def enforce_quota(max_bytes: int) -> int:
    """보존소가 max_bytes 를 넘으면 오래된 세션(mtime 기준)부터 지워 상한 아래로.
    기본은 무제한(설정 UI에서 켤 때만 호출됨). 반환: 삭제한 파일 수."""
    root = raw_dir()
    if max_bytes <= 0 or not root.exists():
        return 0
    files = sorted(root.rglob("*.jsonl.gz"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = 0
    for p in files:
        if total <= max_bytes:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        removed += 1
    return removed


# ── 복구 (#163 P1) ─────────────────────────────────────────
# Claude Code 는 실행 중인 cwd 를 스스로 인코딩해 ~/.claude/projects/<encoded>/ 밑에서 세션을
# 찾는다(실측 확인: "C:\Users\me\chat-memory" → "C--Users-me-chat-memory", "C:\growth_report"
# → "C--growth-report" — 영숫자가 아닌 문자는 전부 '-'). Vestige 의 재개(_launch_resume)는
# 그 cwd 에서 그대로 claude 를 띄우므로, 복구본을 이 규칙대로 같은 폴더에 둬야 실제 재개가 된다.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def _encode_claude_project_dir(cwd: str) -> str:
    return _NON_ALNUM_RE.sub("-", cwd)


def _first_cwd(raw: bytes, *, key_path: tuple[str, ...]) -> str | None:
    """복구분(raw)의 앞부분 줄들에서 cwd 를 찾는다. key_path 로 중첩 위치 지정(예: ("cwd",) 또는
    ("payload","cwd")). 앞쪽 몇 줄만 보면 충분(cwd 는 세션 내내 안 바뀜)."""
    text = raw.decode("utf-8", errors="replace")
    for i, line in enumerate(text.splitlines()):
        if i >= 20:   # 앞부분만 - 대형 세션이어도 값싸게
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        cur = obj
        for k in key_path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, str) and cur:
            return cur
    return None


def restore(source: str, session_id: str) -> Path | None:
    """보존된 원본을 실제 로그 위치로 되써넣어 재개 가능하게 만든다.

    이미 원본이 있으면(다른 경로로 복구됐거나 아직 안 지워졌으면) 건드리지 않고 그 경로를
    그대로 반환(덮어쓰지 않음 - 실수로 최신본을 과거 보존분으로 되돌리는 사고 방지).
    cwd 를 못 찾거나 지원 안 하는 source면 None.
    """
    raw = read_mirror(source, session_id)
    if raw is None:
        return None
    if source == "codex":
        target = C.CODEX_SESSIONS_DIR / "restored" / f"rollout-restored-{session_id}.jsonl"
    elif source == "claude-code":
        cwd = _first_cwd(raw, key_path=("cwd",))
        if not cwd:
            return None
        target = C.PROJECTS_DIR / _encode_claude_project_dir(cwd) / f"{session_id}.jsonl"
    else:
        return None
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target
