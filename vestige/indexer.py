"""인덱싱 파이프라인: JSONL 커서 증분 → 턴 → 필터 → 청킹 → 임베딩 → 저장.

핵심 안전장치:
- 미완결 마지막 턴 보류: 파일이 최근 변경됐으면(세션 진행중일 수 있음) 마지막
  사용자 프롬프트 이전까지만 확정하고, 커서를 그 프롬프트 시작에 둔다. 다음 배치가
  멱등(turn id)으로 재처리하여 완성본으로 교체 → 반쪽 저장·누락 없음.
- 파일이 idle_secs 이상 잠잠하면 세션 종료로 보고 마지막 턴까지 확정.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from . import raw_archive
from .chunker import chunk_turn
from .config import (
    CHECKPOINT_TURNS,
    CONTEXT_PREV_CHARS,
    EMBED_BATCH,
    IDLE_SECS,
)
from .filters import should_embed
from .models import Turn
from .sources import active_sources, default_adapter

if TYPE_CHECKING:
    from .sources.base import SourceAdapter


def iter_jsonl(root: Path):
    """(하위호환) 기본 어댑터(Claude Code)로 root 안 세션 파일 순회. 특정 폴더 카운트용."""
    return default_adapter().discover(root)


def _source_pairs(projects_dir: str | Path | None) -> list[tuple[SourceAdapter, Path]]:
    """색인할 (어댑터, 루트) 쌍.

    projects_dir 명시 → 그 Claude Code 루트만(하위호환·테스트).
    None → active_sources()(claude-code + codex + …, 루트 존재하는 것만).
    """
    if projects_dir is not None:
        return [(default_adapter(), Path(projects_dir))]
    return [(adapter, root) for _n, adapter, root in active_sources()]


def _iter_all(projects_dir: str | Path | None) -> Iterator[tuple[SourceAdapter, Path]]:
    """활성 소스들의 세션 파일 전부를 (어댑터, path)로 산출."""
    for adapter, root in _source_pairs(projects_dir):
        if not root.exists():
            continue
        for p in adapter.discover(root):
            yield adapter, p


def has_new_data(db, projects_dir: str | Path | None = None) -> bool:
    """커서 이후 새 바이트가 있는 파일이 하나라도 있으면 True(모델 로드 전 값싼 확인)."""
    for _adapter, p in _iter_all(projects_dir):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        offset, _, _ = db.get_cursor(str(p))
        if size > offset:
            return True
    return False


def _held_back_only(adapter, path: str, offset: int) -> bool:
    """커서 이후 남은 내용이 '진행 중인 마지막 턴 하나뿐'이면 True.
    index_file은 활성(비-idle) 파일의 마지막 턴을 IDLE_SECS까지 홀드백하므로, 그 턴만 남은
    경우 지금은 색인할 게 없다 → UI에서 "대기"로 세면 안 됨(색인 눌러도 안 사라져 혼란).
    파싱 실패/불명확하면 False(=대기로 유지, 실제 대기를 놓치지 않게 보수적)."""
    try:
        recs = list(adapter.read_records(path, offset))
    except Exception:  # noqa: BLE001 — 파싱 불가 파일은 바이트 기반 판정으로 폴백
        return False
    if not recs:
        return False
    last_up = None
    for i, (obj, _end) in enumerate(recs):
        if adapter.is_turn_start(obj):
            last_up = i
    # last_up>=1 이면 마지막 턴 앞에 완결 턴이 있어 지금 색인 가능 → 홀드백-only 아님.
    # last_up==0 이면 커서 이후 turn-start가 진행 중 턴 하나뿐 → 홀드백-only.
    return last_up == 0


def count_pending(
    db, projects_dir: str | Path | None = None, idle_secs: int = IDLE_SECS,
) -> dict:
    """대기(새 대화) 집계. 활성 소스 전체 합산. JSONL 파일 1개 = 세션(대화) 1개.

    반환: {"new_sessions": 아직 한 번도 색인 안 된 파일 수, "updated_sessions": 이어져 새
    내용이 생긴 파일 수, "files": 지금 색인 가능한 파일 총수}.

    활성 세션(최근 수정)의 마지막(진행 중) 턴은 index_file이 홀드백하므로, 그 턴만 남은
    파일은 대기로 세지 않는다 → 증분 색인 직후 "최신"이 정확히 표시됨."""
    new = updated = 0
    now = time.time()
    for adapter, p in _iter_all(projects_dir):
        try:
            stt = p.stat()
            size, mtime = stt.st_size, stt.st_mtime
        except OSError:
            continue
        offset, _, _ = db.get_cursor(str(p))
        if size <= offset:
            continue
        # 활성(비-idle) 파일에서 남은 게 진행 중 턴 하나뿐이면 지금 색인 대상 아님 → 제외.
        if (now - mtime) <= idle_secs and _held_back_only(adapter, str(p), offset):
            continue
        if offset == 0:
            new += 1
        else:
            updated += 1
    return {"new_sessions": new, "updated_sessions": updated, "files": new + updated}


def discover_files(
    projects_dir: str | Path | None = None, recent_first: bool = True,
) -> list[tuple[str, SourceAdapter]]:
    """활성 소스 전체(또는 명시 루트)의 (path, 어댑터) 쌍을 mtime 순으로."""
    pairs = [(str(p), adapter) for adapter, p in _iter_all(projects_dir)]
    pairs.sort(key=lambda t: os.path.getmtime(t[0]), reverse=recent_first)
    return pairs


def _contextual(ctx: str, chunk_text: str, project: str) -> str:
    """맥락 임베딩(값싼 실시간판): 직전 질문·프로젝트를 앞에 덧대 임베딩용 텍스트 생성.

    저장되는 원문(chunk.text)은 건드리지 않는다 — 임베딩 입력에만 붙는다.
    """
    head = f"[{project}]" if project else ""
    if ctx:
        head = f"{head} 이전: {ctx[:CONTEXT_PREV_CHARS]}".strip()
    return f"{head}\n{chunk_text}" if head else chunk_text


def _group_with_offsets(proc: list[tuple], final_offset: int, adapter: SourceAdapter) -> list[tuple[Turn, int]]:
    """처리 대상 레코드를 (턴, resume_offset) 목록으로. resume=다음 턴 시작(=재개 지점)."""
    starts = [i for i, (o, _s, _e) in enumerate(proc) if adapter.is_turn_start(o)]
    out: list[tuple[Turn, int]] = []
    for k, si in enumerate(starts):
        sj = starts[k + 1] if k + 1 < len(starts) else len(proc)
        turns = adapter.extract_turns([proc[t][0] for t in range(si, sj)])
        if not turns:
            continue
        resume = proc[sj][1] if sj < len(proc) else final_offset
        out.append((turns[0], resume))
    return out


def index_file(
    path: str | Path, db, vi, embedder, *, adapter: SourceAdapter | None = None,
    idle_secs: int = IDLE_SECS, batch: int = EMBED_BATCH,
    checkpoint_turns: int = CHECKPOINT_TURNS, on_flush=None,
) -> int:
    """한 JSONL 파일을 커서 이후부터 증분 처리. 처리한 턴 수 반환.

    adapter 등은 키워드 전용(순서 실수로 idle_secs가 adapter에 바인딩되는 것 방지).
    adapter 미지정 시 기본 소스(Claude Code) — 하위호환. 멀티소스에선 index_all 이 파일별
    올바른 어댑터를 넘긴다. 턴 경계마다 resume offset을 알고, checkpoint_turns 턴마다 커서
    전진 + 벡터 저장 → 대형 파일도 중간 재개 가능(kill 돼도 최대 checkpoint_turns 턴만 재처리).
    """
    adapter = adapter or default_adapter()
    path = str(path)
    size = os.path.getsize(path)
    mtime = os.path.getmtime(path)
    offset, _, _ = db.get_cursor(path)
    hold = db.get_hold(path)   # idle 확정된 '열린 마지막 턴'의 시작(있으면 여기부터 재읽기)
    if offset > size:  # 파일 회전/절단 → 처음부터
        offset = 0
        hold = None
    if hold is not None and hold > size:  # 절단으로 hold가 파일 밖 → 무시
        hold = None
    if offset == size:   # 새 바이트 없음 → 스킵(열린 턴도 안 변했음)
        return 0

    # 열린 턴(hold)이 있으면 그 시작부터 다시 읽어 뒤에 붙은 내용까지 합쳐 '완성된 턴'으로 재저장(멱등).
    read_from = hold if hold is not None else offset
    records = []  # (obj, start, end)
    prev = read_from
    for obj, end in adapter.read_records(path, read_from):
        records.append((obj, prev, end))
        prev = end
    if not records:
        return 0

    last_up = None
    for i, (obj, _s, _e) in enumerate(records):
        if adapter.is_turn_start(obj):
            last_up = i

    idle = (time.time() - mtime) > idle_secs
    if last_up is None or idle:
        proc, final_offset = records, prev
        # idle 로 마지막 턴을 확정하지만, 그 턴은 아직 안 끝났을 수 있다(긴 도구호출 중).
        # 그 턴의 시작을 hold 로 남겨, 나중에 뒷내용이 붙으면 여기서부터 다시 읽어 완성한다.
        new_hold = records[last_up][1] if (idle and last_up is not None) else None
    else:
        # 마지막(진행중일 수 있는) 턴 보류: 그 프롬프트 시작을 최종 경계로.
        proc, final_offset = records[:last_up], records[last_up][1]
        new_hold = None
    if not proc:
        return 0

    turns = _group_with_offsets(proc, final_offset, adapter)
    if not turns:  # 노이즈만 있었으면 커서만 전진(hold 정리)
        db.set_cursor(path, final_offset, size, mtime, new_hold)
        db.commit()
        return 0

    prev_q: dict[str, str] = {}
    buf_texts: list[str] = []
    buf_keys: list[str] = []
    count = 0
    since_ckpt = 0
    last_resume = offset

    def flush_vectors() -> None:
        if buf_texts:
            n = len(buf_texts)
            vi.add(buf_keys, embedder.embed_passages(buf_texts))
            buf_texts.clear()
            buf_keys.clear()
            if on_flush:
                on_flush(n)   # 청크 단위 진행 보고(임베딩 배치가 저장될 때마다)

    def checkpoint(off: int, hold: int | None = None) -> None:
        flush_vectors()
        db.set_cursor(path, off, size, mtime, hold)
        db.set_meta("embed_model", embedder.model_name)
        db.commit()
        vi.save()

    # 저장 출처: 어댑터가 source_name 을 주면 그걸(예: subagent 어댑터 → 'claude-code'), 없으면 name.
    src = getattr(adapter, "source_name", adapter.name)
    last_i = len(turns) - 1
    for i, (turn, resume) in enumerate(turns):
        written = db.upsert_turn(turn, source=src, source_file=path)   # 출처·원문경로 기록(재개용)
        count += 1
        if written and should_embed(turn):   # 축소로 스킵된 턴은 청크/벡터도 기존 그대로(일관 유지)
            ctx = prev_q.get(turn.session_id, "")
            for c in chunk_turn(turn):
                db.add_chunks([c])
                buf_texts.append(_contextual(ctx, c.text, turn.project))
                buf_keys.append(f"{c.turn_id}#{c.index}")
                if len(buf_texts) >= batch:
                    flush_vectors()
        if turn.question:
            prev_q[turn.session_id] = turn.question
        last_resume = resume
        # idle 로 held 된 마지막 턴은 중간 체크포인트로 커서를 넘기지 않는다: 오직 최종
        # checkpoint(hold 포함)에서만 커밋 → 프로세스가 kill 돼도 다음 pass가 그 턴을 다시 읽어 완성.
        if new_hold is not None and i == last_i:
            continue
        since_ckpt += 1
        if since_ckpt >= checkpoint_turns:
            checkpoint(last_resume)   # 중간 체크포인트: hold 없음(확정된 경계까지만)
            since_ckpt = 0
    checkpoint(final_offset, new_hold)   # 최종: idle 확정이면 열린 턴 시작을 hold 로 남김
    return count


def reconcile(db, vi, log_fn=print) -> int:
    """원문(turns)에 없는 고아 벡터를 인덱스·chunks·FTS에서 정리.

    소스에서 사라졌거나 삭제된 턴의 벡터가 남아 지도/검색에 유령으로 뜨는 것 방지.
    모델 로드 불필요·값쌈 → 인덱싱 회차마다 안전하게 호출 가능.
    """
    keys = vi.keys()
    if not keys:
        return 0
    have = {r["id"] for r in db.conn.execute("SELECT id FROM turns").fetchall()}
    orphan_keys = [k for k in keys if k.rsplit("#", 1)[0] not in have]
    if not orphan_keys:
        return 0
    orphan_tids = sorted({k.rsplit("#", 1)[0] for k in orphan_keys})
    db.delete_turns(orphan_tids)   # chunks·FTS 정리(turns엔 이미 없음)
    db.commit()
    n = vi.remove(orphan_keys)
    vi.save()
    log_fn(f"reconcile: 고아 벡터 {n}개 정리(턴 {len(orphan_tids)})")
    return n


def index_all(db, vi, embedder, recent_first: bool = True, log_fn=print,
              progress_fn=None, chunk_progress_fn=None) -> int:
    """모든 활성 소스(claude-code + codex …)의 세션 파일을 최근순으로 증분 인덱싱.

    참고: 여러 소스가 한 turns 테이블을 공유한다(PK=`session_id:uuid`). 두 도구의
    session_id 는 독립 생성된 128비트 UUID라 교차 충돌은 사실상 불가능(YAGNI로 소스 접두 미부여).

    progress_fn(done_files, total_files): 파일 단위 진행 콜백.
    chunk_progress_fn(done_chunks): 임베딩된 청크 누계 콜백(전체 재색인 진행바용 — 거대 파일
      하나가 대부분을 차지해도 부드럽게 진행이 보이게). 둘 다 선택.
    """
    total = 0
    files = list(discover_files(recent_first=recent_first))
    total_files = len(files)
    embedded = 0

    def _on_flush(n):
        nonlocal embedded
        embedded += n
        if chunk_progress_fn:
            try:
                chunk_progress_fn(embedded)
            except Exception:  # noqa: BLE001 — 진행 콜백 오류가 색인을 막지 않게
                pass

    turns_by_src: dict[str, int] = {}     # 이번 회차 소스별 추출 턴 수
    newdata_by_src: dict[str, int] = {}   # 이번 회차 새 바이트가 있던 파일 수(소스별)
    for i, (f, adapter) in enumerate(files):
        had_new = False
        try:
            off, _s, _m = db.get_cursor(f)
            # size != off = 새 바이트 or 회전/절단(index_file이 처음부터 재처리) — 둘 다 '새 데이터'로 본다.
            had_new = os.path.getsize(f) != off
        except Exception:  # noqa: BLE001 — 커서/스탯 실패가 색인 회차를 중단시키지 않게(파일별 격리와 동일)
            pass
        try:   # 원본 미러링(#163 P1): 턴 추출과 완전히 별개 — 여기서 실패해도 색인은 계속.
            raw_archive.mirror_file(db, f, getattr(adapter, "source_name", adapter.name), log_fn=log_fn)
        except Exception as ex:  # noqa: BLE001
            # "ERROR " 접두사는 _capture_log 가 /api/index/status 로 올리는 규약이다.
            # 이게 빠지면 보존 실패가 어디에도 안 남아, 원본이 지워진 뒤에야 발견된다.
            log_fn(f"ERROR raw mirror {os.path.basename(str(f))}: {ex}")
        try:
            n = index_file(f, db, vi, embedder, adapter=adapter, on_flush=_on_flush)
            if had_new:
                newdata_by_src[adapter.name] = newdata_by_src.get(adapter.name, 0) + 1
            turns_by_src[adapter.name] = turns_by_src.get(adapter.name, 0) + (n or 0)
            if n:
                log_fn(f"indexed {n} turns  {os.path.basename(f)}")
                total += n
        except Exception as ex:  # 한 파일 실패가 전체를 막지 않도록
            log_fn(f"ERROR {os.path.basename(str(f))}: {ex}")
        if progress_fn:
            try:
                progress_fn(i + 1, total_files)
            except Exception:  # noqa: BLE001
                pass
    _update_drift(db, turns_by_src, newdata_by_src, log_fn=log_fn)
    from . import config as C
    if C.RAW_ARCHIVE_MAX_MB.strip():
        try:
            n = raw_archive.enforce_quota(int(C.RAW_ARCHIVE_MAX_MB) * 1024 * 1024, db)
            if n:
                log_fn(f"ERROR raw archive 용량 초과 - 오래된 세션 {n}개 정리(보존본 삭제)")
        except Exception as ex:  # noqa: BLE001
            log_fn(f"ERROR raw archive quota 정리 실패: {ex}")
    return total


def _update_drift(db, turns_by_src: dict[str, int], newdata_by_src: dict[str, int], log_fn=print) -> None:
    """소스 로그 형식 변경(어댑터가 못 읽음) 자동 감지 → meta 'drift_sources'.

    신호: 이번 회차 그 소스에 새 바이트가 있었는데 턴을 0개 뽑았다 → build_report 로 확정
    ('스캔한 모든 파일이 레코드는 있으나 0턴'). 턴이 나온 소스는 회복으로 간주해 플래그 해제.
    비용: build_report(bounded) 는 '새 데이터 있는데 0턴'인 드문 경우에만 호출.
    """
    try:
        cur = {s.strip() for s in (db.get_meta("drift_sources") or "").split(",") if s.strip()}
    except Exception as e:  # noqa: BLE001 — 감지 실패가 색인을 막지 않게(가시성 위해 로그만)
        log_fn(f"drift 감지 스킵(meta 읽기 실패): {e}")
        return
    changed = False
    for name, t in turns_by_src.items():
        if t > 0 and name in cur:   # 다시 정상적으로 읽힘 → 해제
            cur.discard(name)
            changed = True
    suspects = [n for n, nd in newdata_by_src.items() if nd > 0 and turns_by_src.get(n, 0) == 0 and n not in cur]
    if suspects:
        from .schema_report import build_report
        for name in suspects:
            try:
                if build_report(name).get("drift_suspected"):
                    cur.add(name)
                    changed = True
                    log_fn(f"⚠️ drift 감지: '{name}' 로그를 못 읽음(형식 변경 의심)")
            except Exception as e:  # noqa: BLE001
                log_fn(f"drift 확인 실패({name}): {e}")
    if changed:
        db.set_meta("drift_sources", ",".join(sorted(cur)))
        db.commit()


def backfill_missing(db, vi, embedder, batch: int = EMBED_BATCH,
                     log_fn=print, progress_fn=None, parallel: int | None = None) -> int:
    """활성 벡터 저장소에 벡터가 없는 기존 청크를 임베딩해 채운다(전체 재색인 없이 자가복구).

    백엔드 전환(npy↔sqlite-vec)이나 벡터 파일 유실로 archive.db엔 청크가 있는데 벡터가 비어
    있을 때, 증분만으로 복구되게 한다. 맥락 입력(직전 질문+프로젝트)은 index_file과 동일 재구성.
    또한 vi.reset() 직후 호출하면 '재파싱 없는 전체 재임베딩'이 된다(재색인 fast 경로가 재사용).
    progress_fn(done, total): 청크 단위 진행 콜백. parallel=N: 멀티프로세싱 가속(고RAM 기기).
    """
    from itertools import groupby

    have = set(vi.keys())
    rows = db.conn.execute(
        "SELECT t.id AS tid, t.session_id AS sid, t.project AS project, t.question AS question, "
        "       c.chunk_key AS ck, c.text AS text "
        "FROM turns t LEFT JOIN chunks c ON c.turn_id = t.id "
        "ORDER BY t.session_id, t.timestamp, t.id, c.idx",
    ).fetchall()
    total = sum(1 for r in rows if r["ck"] and r["ck"] not in have)
    if total == 0:
        return 0
    log_fn(f"자가복구: 벡터 없는 청크 {total}개 임베딩")

    prev_q: dict[str, str] = {}
    buf_keys: list[str] = []
    buf_texts: list[str] = []
    done = 0

    def flush() -> None:
        nonlocal done
        if not buf_texts:
            return
        vi.add(buf_keys, embedder.embed_passages(buf_texts, parallel=parallel))
        done += len(buf_texts)
        buf_keys.clear()
        buf_texts.clear()
        vi.save()
        if progress_fn:
            try:
                progress_fn(done, total)
            except Exception:  # noqa: BLE001
                pass

    for _tid, group in groupby(rows, key=lambda r: r["tid"]):
        grp = list(group)
        first = grp[0]
        ctx = prev_q.get(first["sid"], "")
        for r in grp:
            ck = r["ck"]
            if ck and ck not in have:
                buf_keys.append(ck)
                buf_texts.append(_contextual(ctx, r["text"], first["project"] or ""))
                if len(buf_texts) >= batch:
                    flush()
        if first["question"]:
            prev_q[first["sid"]] = first["question"]
    flush()
    db.set_meta("embed_model", embedder.model_name)
    db.commit()
    return done
