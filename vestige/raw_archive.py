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

import contextlib
import gzip
import json
import logging
import os
import re
import time
import zlib
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


# 보존소 소유 표시. enforce_quota 가 이 마커 없는 폴더에서는 아무것도 지우지 않는다 —
# 보존소 경로는 설정 화면에서 사용자가 직접 고를 수 있어서, 홈 폴더나 기존 백업 폴더를
# 지정한 채 용량 상한을 켜면 앱이 만들지 않은 .jsonl.gz 까지 삭제 대상이 되기 때문.
_MARKER = ".vestige-raw"


def _ensure_marker(root: Path) -> None:
    """보존소 소유 표시를 남긴다 — 단 '비어 있던 폴더'에만.

    무조건 찍으면 가드가 스스로 무력화된다: 사용자가 기존 백업 폴더를 보존소로 지정해도
    첫 미러링이 마커를 만들고, 그 뒤로 enforce_quota 가 그 폴더를 우리 것으로 오인해
    남의 .jsonl.gz 까지 지운다. 기존 내용이 있으면 미러링은 하되(추가는 안전) 소유권은
    주장하지 않는다 → 그 폴더에서는 정리가 영영 거부된다.
    """
    m = root / _MARKER
    if m.exists():
        return
    if any(root.rglob("*.jsonl.gz")):
        return
    m.write_text("vestige raw archive\n", encoding="utf-8")


def _lost_marker(mirror: Path) -> Path:
    """이 보존본에서 '확정 구간'이 유실됐다는 표시. 있으면 복구는 반드시 부분 복구로 보고한다."""
    return mirror.with_name(mirror.name + ".lost")


def raw_path(source: str, session_id: str) -> Path:
    """이 세션의 압축 원본 경로. source/session_id 는 파일명·UUID에서만 나와 경로이탈 위험이
    없지만, 방어적으로 한 번 더 sanitize."""
    return raw_dir() / _sanitize(source) / f"{_sanitize(session_id)}.jsonl.gz"


def _repair_mirror(db, out: Path, log_fn=None) -> int | None:
    """보존본의 깨진 꼬리를 걷어내고, **이 보존본이 실제로 담고 있는 소스 바이트 수**를 돌려준다.
    (손댈 필요가 없으면 None — 빠른 경로)

    이 함수가 존재하는 이유는 하나다 — **잘라낸 바이트와 커서가 따로 놀면 안 된다.**
    이 파일의 유실은 지금까지 전부 그 불변식이 깨져서 났다. 잘라낸 구간은 "아직 안 읽은 것"이
    아니라 "이미 읽었는데 보존에 실패한 것"이라, 커서를 그대로 두면 소스에 멀쩡히 남아 있는
    대화가 영영 안 돌아온다(그리고 남은 파일은 gzip 으로 온전해서 '복구 완료'로 보고된다).

    그래서 호출부가 커서를 여기에 맞출 수 있도록 '담고 있는 양'을 돌려준다.
    **보존본을 통째로 비우는 연산은 절대 넣지 않는다.** 한 보존본에 소스가 둘 붙을 수 있고
    (codex 복구본), 소스가 회전으로 짧아졌을 수도 있어서, 비우는 순간 되살릴 수 없는 것까지
    잃는다(실측으로 두 경우 모두 전손을 확인했다). 걷어내는 건 '깨진 꼬리'까지만이다.
    """
    if not out.exists():
        return None
    cur_size = out.stat().st_size
    # 빠른 경로: 우리가 마지막으로 쓴 그대로(크기 일치)이고 손상 표시도 없으면 손대지 않는다.
    # 마커를 함께 보는 이유 — 크기가 그대로인 손상(전원 차단으로 마지막 블록이 0으로 채워지는
    # 경우 등)은 크기만으로는 안 잡힌다. 읽기 경로가 손상을 발견하면 마커를 남겨두므로,
    # 값싼 존재 확인만으로 전체 검사로 내려올 수 있다.
    if db.mirror_ok_bytes(str(out)) == cur_size and not _lost_marker(out).exists():
        return None
    data, good_end, intact = _walk_members(out.read_bytes(), out.name)
    if not intact and good_end < cur_size:
        msg = f"보존본 꼬리 {cur_size - good_end}바이트를 걷어냅니다(손상): {out.name}"
        if log_fn:   # 색인 로그 규약: "ERROR " 접두사만 /api/index/status 로 UI 에 올라간다
            log_fn(f"ERROR raw mirror {msg}")
        logger.warning("%s", msg)
        with open(out, "r+b") as f:
            f.truncate(good_end)
        cur_size = good_end
        _lost_marker(out).unlink(missing_ok=True)   # 걷어냈으니 표시도 지운다
    # 방금 검증한 크기를 기록해 다음부터 빠른 경로를 타게 한다. 이게 없으면 더 자라지 않는
    # (끝난) 세션은 기록이 영영 안 생겨 매 색인 회차마다 전체 압축해제를 반복한다.
    db.set_mirror_ok_bytes(str(out), cur_size)
    db.commit()
    return len(data)


def mirror_file(db, path: str | Path, source: str, log_fn=None) -> int:
    """path 의 새 바이트(마지막 미러링 이후분)를 세션별 압축 원본에 append.

    반환: 이번에 미러링한 바이트 수(0이면 새 바이트 없음 — 멱등).
    파일 회전/절단(현재 크기가 이미 미러링한 오프셋보다 작음)이면 처음부터 다시 미러링한다
    (이미 보존해둔 이전 바이트는 그대로 두고 새 멤버로 이어 씀 — 잘라내 덮지 않는 쪽이
    안전: 회전으로 다른 내용이 왔더라도 옛 보존분을 잃지 않는다).
    한 파일 실패가 인덱싱 전체를 막지 않도록 호출자가 감싸는 것을 전제로, 여기서도
    예외를 삼키지 않고 그대로 던진다(호출자의 try/except 격리에 맡김).
    """
    path = str(path)
    # Syncthing 충돌본(`<sid>.sync-conflict-...jsonl`)은 미러링하지 않는다.
    # 파일명에 원본과 같은 UUID 가 박혀 있어 session_id_for 가 같은 값을 내고, 그대로 두면
    # 원본과 충돌본의 바이트가 한 보존본에 각자의 커서로 섞여 들어간다(복구 시 중복 줄).
    # 충돌 해소기(session_sync)가 곧 정리하고, 그 결과는 어느 쪽이든 안전하다 —
    # conflict_wins/base_wins 는 한쪽이 다른 쪽의 줄 prefix 라 원본 커서가 그대로 유효하고,
    # fork 는 새 세션 id 파일로 떨어져 별도 보존본이 된다. 여기서 건너뛰어 잃는 건 없다.
    from .session_sync import base_for_conflict
    if base_for_conflict(path) is not None:
        return 0
    size = Path(path).stat().st_size
    sid = session_id_for(path)
    out = raw_path(source, sid)
    out.parent.mkdir(parents=True, exist_ok=True)
    _ensure_marker(raw_dir())
    mirrored = db.get_raw_cursor(path)
    if mirrored > size:
        mirrored = 0
    # 깨진 꼬리를 걷어내고, 보존본이 실제로 담고 있는 양을 받는다.
    held = _repair_mirror(db, out, log_fn)
    if held is not None and held < mirrored:
        # 걷어낸 만큼 커서를 되감아 소스에서 그 구간을 다시 채운다. 이걸 안 하면 잘라낸 구간이
        # '이미 미러링함'으로 남아 소스에 멀쩡히 있는 대화가 영영 안 돌아온다(가운데 구멍).
        # held > mirrored 인 경우(한 보존본에 소스가 둘)는 건드리지 않는다 — 남의 몫이다.
        msg = f"보존본이 {mirrored - held}바이트 모자라 소스에서 다시 채웁니다: {out.name}"
        if log_fn:   # 색인 로그 규약: "ERROR " 접두사만 /api/index/status 로 UI 에 올라간다
            log_fn(f"ERROR raw mirror {msg}")
        logger.warning("%s", msg)
        mirrored = held
    if mirrored == size:
        return 0
    with open(path, "rb") as f:
        f.seek(mirrored)
        chunk = f.read()
    if not chunk:
        return 0
    with open(out, "ab") as raw_f:
        with gzip.GzipFile(fileobj=raw_f, mode="wb") as gz:
            gz.write(chunk)
        # 디스크까지 내려보낸 뒤에야 크기를 '정상'으로 기록한다. 이게 없으면 전원이 나갔을 때
        # DB 커밋(fsync)만 살아남고 .gz 꼬리는 0으로 채워진 채 크기만 맞는 상태가 되어,
        # 빠른 경로가 '기록==크기니 온전하다'고 오판할 수 있다.
        raw_f.flush()
        os.fsync(raw_f.fileno())   # 실패하면 던진다 — 삼키고 크기를 '정상'으로 적으면 안 된다
    # 커서는 stat() 때 크기(size)가 아니라 '실제로 기록한 만큼'만 전진시킨다.
    # stat() 과 read() 사이에 claude/codex 가 로그를 이어 쓰면 chunk 가 size 를 넘겨 읽는데,
    # size 로 저장하면 다음 회차가 겹친 구간을 다시 미러링해 gz 에 중복 줄이 쌓인다.
    db.set_raw_cursor(path, mirrored + len(chunk), sid, source)
    db.set_mirror_ok_bytes(str(out), out.stat().st_size)
    db.commit()
    return len(chunk)


def has_mirror(source: str, session_id: str) -> bool:
    return raw_path(source, session_id).exists()


def read_mirror(source: str, session_id: str) -> bytes | None:
    """이 세션의 보존된 원본 바이트 전체(압축 해제). 없으면 None.

    꼬리가 손상돼 있으면(중단된 append 의 잘린 멤버) 거기서 멈추고 **그때까지 읽은 만큼을
    돌려준다**. gzip.open(...).read() 한 방이면 EOFError 로 앞의 멀쩡한 멤버까지 전부 날아간다 —
    복구는 일부라도 되는 쪽이 낫다. mirror_file 이 다음 회차에 잔재를 잘라내고 이어 쓰므로
    보통은 여기까지 오지 않는다(이 경로는 그 전에 만들어진 파일·외부 손상용 안전망).
    """
    got = read_mirror_checked(source, session_id)
    return None if got is None else got[0]


def read_mirror_checked(source: str, session_id: str) -> tuple[bytes, bool] | None:
    """(온전하게 읽은 바이트, 전부 온전한가). 보존본이 아예 없으면 None.

    손상 여부를 반환값으로 내보내는 이유: 부분만 읽고도 '성공'으로 처리하면 복구가 잘린 대화를
    써놓고 완료라고 말한다(실제로 그랬다). 판단은 호출부가 해야 한다.
    """
    p = raw_path(source, session_id)
    if not p.exists():
        return None
    data, _end, intact = _walk_members(p.read_bytes(), p.name)
    if intact and _lost_marker(p).exists():
        intact = False       # gzip 으로는 멀쩡해도 가운데가 뚫린 상태 — 완료로 보고하면 안 된다
    elif not intact:
        # 손상을 '실제로 감지하는 유일한 지점'이 여기다. 표시를 남겨두면 (a) 다음 미러링의
        # 빠른 경로가 값싼 존재 확인만으로 전체 검사로 내려오고(크기가 그대로인 손상도 잡힌다),
        # (b) 복구 가능 여부 표시도 같은 표시를 볼 수 있다.
        with contextlib.suppress(OSError):
            _lost_marker(p).write_text(f"{time.time():.0f}\n", encoding="utf-8")
    return data, intact


def _walk_members(raw: bytes, name: str = "") -> tuple[bytes, int, bool]:
    """멀티멤버 gzip 을 멤버 단위로 푼다.

    반환: (온전하게 푼 바이트, **마지막 온전한 멤버의 끝 오프셋**, 전부 온전한가)

    두 번째 값이 핵심이다 — 잘라낼 지점을 DB 숫자가 아니라 파일 자신에서 구하기 위한 것.
    지금까지 이 파일의 유실은 두 번 다 'DB 에 적어둔 숫자를 믿고 파일을 잘라서' 났다.
    gzip.open(...).read() 한 방이면 버퍼를 채우려 손상 지점을 넘어가 EOFError 로 앞의 멀쩡한
    멤버까지 전부 날린다. zlib 으로 끊어 읽으면 어디까지가 온전한지 정확히 알 수 있다.
    """
    mv = memoryview(raw)                 # 멤버마다 raw[pos:] 를 복사하면 O(멤버수 × 파일크기)
    out = bytearray()
    pos, bad = 0, None
    while pos < len(raw):
        d = zlib.decompressobj(31)       # 31 = gzip 헤더 포함
        try:
            chunk = d.decompress(mv[pos:]) + d.flush()
        except zlib.error as ex:
            bad = str(ex)
            break
        if not d.eof:                    # 멤버가 제대로 끝나지 않았다 = 잘림
            bad = "트레일러 없음(중단된 append)"
            break
        consumed = len(raw) - pos - len(d.unused_data)
        if consumed <= 0:                # 진행이 없으면 무한루프 방지
            bad = "진행 불가"
            break
        out += chunk                     # 멤버가 온전할 때만 확정한다(pos 와 out 이 늘 짝)
        pos += consumed
    if bad is not None:
        logger.warning("보존본 꼬리 손상 - %d바이트까지만 온전합니다 (%s): %s", pos, name or "?", bad)
    return bytes(out), pos, bad is None


def mirror_size_bytes() -> int:
    """보존소 전체 용량(압축 상태 기준, 설정 UI 표시용)."""
    root = raw_dir()
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*.jsonl.gz"):
        with contextlib.suppress(OSError):   # 색인이 동시에 정리 중일 수 있음 — 설정 화면이 500 나지 않게
            total += p.stat().st_size
    return total


def enforce_quota(max_bytes: int, db=None) -> int:
    """보존소가 max_bytes 를 넘으면 오래된 세션(mtime 기준)부터 지워 상한 아래로.
    기본은 무제한(설정 UI에서 켤 때만 호출됨). 반환: 삭제한 파일 수.

    db 를 주면 지운 세션의 미러 커서도 함께 비운다 — 커서만 남으면 그 세션이 이어질 때
    '머리가 잘린' 보존본이 만들어지고, has_mirror 는 그걸 복구 가능으로 잘못 표시한다."""
    root = raw_dir()
    if max_bytes <= 0 or not root.exists():
        return 0
    if not (root / _MARKER).exists():
        # 우리가 만든 보존소가 아니다. 사용자가 고른 폴더일 수 있으므로 한 파일도 지우지 않는다.
        raise RuntimeError(f"보존소 마커({_MARKER})가 없는 폴더라 정리를 건너뜁니다: {root}")

    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:      # 색인이 동시에 정리했을 수 있음 — 없는 파일은 0으로 본다
            return 0

    files = sorted(root.rglob("*.jsonl.gz"), key=lambda p: (p.stat().st_mtime if p.exists() else 0.0))
    total = sum(_size(p) for p in files)
    removed, dropped, paths = 0, [], []
    for p in files:
        if total <= max_bytes:
            break
        total -= _size(p)
        dropped.append((p.parent.name, p.name[: -len(".jsonl.gz")]))
        paths.append(str(p))
        p.unlink(missing_ok=True)
        _lost_marker(p).unlink(missing_ok=True)   # 보존본을 지웠으면 유실 표시도 함께
        removed += 1
    if db is not None:
        by_source: dict[str, list[str]] = {}
        for src, sid in dropped:
            by_source.setdefault(src, []).append(sid)
        for src, sids in by_source.items():
            db.clear_raw_cursors(src, sids)
        # 경계 기록도 같이 — 같은 경로에 새 보존본이 생겼을 때 옛 숫자로 자르지 않게.
        db.clear_mirror_ok_bytes(paths)
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


def restore(source: str, session_id: str) -> tuple[Path, bool] | None:
    """보존된 원본을 실제 로그 위치로 되써넣어 재개 가능하게 만든다.

    반환: (되써넣은 경로, 보존본이 온전했는가). 손상돼 일부만 복구했으면 두 번째가 False —
    호출부가 "복구 완료"로 뭉개지 말고 잘렸다는 걸 알려야 한다.
    이미 원본이 있으면(다른 경로로 복구됐거나 아직 안 지워졌으면) 건드리지 않고 그 경로를
    그대로 반환(덮어쓰지 않음 - 실수로 최신본을 과거 보존분으로 되돌리는 사고 방지).
    한 바이트도 못 읽거나, cwd 를 못 찾거나, 지원 안 하는 source면 None.
    """
    got = read_mirror_checked(source, session_id)
    if got is None:
        return None
    raw, intact = got
    # 한 바이트도 못 읽었으면 복구가 아니다. 예전엔 여기서 빈 파일을 써놓고 '복구 완료'를
    # 돌려줬다(첫 멤버부터 손상된 보존본). 원본이 이미 지워진 세션이라 되돌릴 수도 없다.
    if not raw:
        logger.warning("보존본을 한 바이트도 읽지 못해 복구를 중단합니다: %s/%s", source, session_id)
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
        return target, intact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target, intact
