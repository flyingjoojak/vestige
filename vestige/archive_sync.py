"""아카이브 export/import — 기기 간 '삭제된 원본 세션'까지 공유(내장 Syncthing 폴더 경유).

Claude Code는 세션 로그를 ~30일만 보관하지만 vestige 아카이브는 영구 보존한다. 그런데
archive.db(SQLite)는 직접 동기화하면 손상되므로, 세션 동기 설계와 같은 방식으로:
  - 각 기기가 자기 아카이브를 텍스트(NDJSON)로 <projects>/.vestige-archive/<device_id>.ndjson 에 스냅샷
    (이미 공유 중인 폴더라 Syncthing이 자동 전파, 기기 하나당 파일 하나 → 다중 writer 충돌 없음)
  - 다른 기기 파일을 읽어 **로컬에 없는 세션(턴/청크/정제)만** import
  - 벡터는 옮기지 않음 → 받은 기기가 자기 임베딩 모델로 backfill(모델·백엔드 달라도 됨)
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
from pathlib import Path

from .models import Turn
from .store import _actions_from_json

ARCHIVE_DIRNAME = ".vestige-archive"
# 레거시(이름 변경 전) 스냅샷 폴더. 신규 export 는 항상 위 폴더에 쓰지만, 예전에
# 동기화해 둔 .engram-archive·.chatmem-archive 스냅샷도 계속 import 할 수 있도록
# 읽기에서 함께 스캔한다. (하위호환 심볼 LEGACY_ARCHIVE_DIRNAME 유지 + engram 추가)
LEGACY_ARCHIVE_DIRNAME = ".chatmem-archive"
LEGACY_ARCHIVE_DIRNAMES = (".engram-archive", ".chatmem-archive")


def device_id(db) -> str:
    """이 기기의 안정적 식별자(export 파일명용). meta에 1회 생성·보관."""
    did = db.get_meta("device_id")
    if did:
        return did
    host = "".join(c for c in socket.gethostname() if c.isalnum())[:16] or "dev"
    did = f"{host}-{secrets.token_hex(3)}"
    db.set_meta("device_id", did)
    db.commit()
    return did


def _dir(projects_dir: str | Path) -> Path:
    return Path(projects_dir) / ARCHIVE_DIRNAME


def export_archive(db, projects_dir: str | Path, did: str) -> int:
    """이 기기 아카이브 전체(턴+청크+정제)를 NDJSON 스냅샷으로 원자적 저장. 반환: 턴 수."""
    d = _dir(projects_dir)
    d.mkdir(parents=True, exist_ok=True)
    chunks: dict[str, list] = {}
    for r in db.conn.execute("SELECT chunk_key, turn_id, idx, text FROM chunks"):
        chunks.setdefault(r["turn_id"], []).append([r["idx"], r["text"]])
    tmp = d / f"{did}.ndjson.tmp"
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for r in db.conn.execute(
            "SELECT id,session_id,uuid,parent_uuid,timestamp,project,question,answer,actions,summary,tags,"
            "source,source_file,queued,parser_version "
            "FROM turns",
        ):
            # t[11]=source, t[12]=source_file, t[13]=queued, t[14]=parser_version 을 뒤에 append
            # (옛 스냅샷은 11칸이라 import 에서 길이로 판별 — 칸은 끝에만 늘린다).
            rec = {
                "t": [r["id"], r["session_id"], r["uuid"], r["parent_uuid"], r["timestamp"],
                      r["project"], r["question"], r["answer"], r["actions"], r["summary"], r["tags"],
                      r["source"], r["source_file"], r["queued"], r["parser_version"] or 0],
                "c": chunks.get(r["id"], []),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
        # 대화 위에 얹은 정리 상태(#233). 줄 종류를 늘리는 방식이라 하위호환이 된다 —
        # 모르는 키를 무시하는 옛 버전도 이 파일을 계속 읽는다.
        for sid, title, at in db.sync_title_rows():
            f.write(json.dumps({"title": [sid, title, at]}, ensure_ascii=False) + "\n")
        fs = db.sync_folder_rows()
        for row in fs["folders"]:
            f.write(json.dumps({"folder": list(row)}, ensure_ascii=False) + "\n")
        for row in fs["items"]:
            f.write(json.dumps({"fitem": list(row)}, ensure_ascii=False) + "\n")
        for row in fs["gone"]:
            f.write(json.dumps({"folder_x": list(row)}, ensure_ascii=False) + "\n")
        for row in fs["gone_items"]:
            f.write(json.dumps({"fitem_x": list(row)}, ensure_ascii=False) + "\n")
        for tid, folded, at in db.sync_fold_rows():
            f.write(json.dumps({"fold": [tid, folded, at]}, ensure_ascii=False) + "\n")
    os.replace(tmp, d / f"{did}.ndjson")   # 원자적 교체
    return n


def _checked(rec: dict) -> dict:
    """쓰기 전에 레코드 모양을 확인한다. 어긋나면 ValueError — 그 줄만 건너뛰게 된다.

    이게 없으면 턴을 upsert 한 뒤 청크 배열에서 터져 **반쪽이 커밋된다.** 그 상태는
    upsert_turn 의 완성도 비교가 '이미 더 완전함'으로 보고 스킵해 재동기화로도 안 고쳐진다
    (실측: 20회 돌려도 청크 1개 고정).

    SAVEPOINT 로 묶어도 되지만 그쪽은 파이썬 sqlite3 의 암묵적 트랜잭션 관리와 얽혀
    환경에 따라 다르게 동작했다(로컬 통과·CI 실패). 쓰기 전에 보는 편이 단순하고 확실하다.
    상대 스냅샷은 **바깥에서 온 데이터**라 어차피 모양을 믿으면 안 된다.
    """
    def _num(x):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError(f"시각이 숫자가 아님: {type(x).__name__}")
        return float(x)

    SHAPES = {"title": 3, "fold": 3, "folder": 5, "fitem": 6, "folder_x": 2, "fitem_x": 4}
    for key, n in SHAPES.items():
        if key in rec:
            v = rec[key]
            if not isinstance(v, list) or len(v) != n:
                raise ValueError(f"{key} 레코드 모양이 다름: {n}칸이어야 하는데 {v!r}")
            _num(v[-1])                       # 마지막 칸은 항상 시각
            return rec
    if "t" not in rec:
        return rec                            # 모르는 줄 종류 — 호출부가 건너뛴다
    t = rec["t"]
    if not isinstance(t, list) or not (11 <= len(t) <= 15):
        raise ValueError(f"턴 레코드가 11~15칸이어야 하는데 {len(t) if isinstance(t, list) else t!r}")
    # 턴 id 는 보통 문자열이지만 옛 스냅샷에 정수로 든 것이 있다(테스트가 그 경우를 지킨다).
    if isinstance(t[0], bool) or not isinstance(t[0], (str, int)) or t[0] == "":
        raise ValueError(f"턴 id 가 이상함: {t[0]!r}")
    # 정제본도 쓰기 전에 본다 - 턴을 upsert 한 뒤 set_enrichment 에서 터지면 턴만 반쪽으로 커밋된다.
    if t[9] is not None and not isinstance(t[9], str):
        raise ValueError(f"요약이 문자열이 아님: {type(t[9]).__name__}")
    if t[9] and t[10]:   # 요약이 없으면 태그는 쓰지 않으므로 보지 않는다
        tags = json.loads(t[10])
        if not isinstance(tags, list):
            raise ValueError("태그가 목록이 아님")
    for idx, text in rec.get("c", []) or []:  # 청크는 [정수, 문자열] 쌍만
        if isinstance(idx, bool) or not isinstance(idx, int) or not isinstance(text, str):
            raise ValueError(f"청크 모양이 다름: {[idx, text]!r}")
    return rec


# 이미 끝까지 읽은 상대 export 의 (크기, 수정 시각). 그대로면 다시 읽지 않는다 - 색인 회차마다
# (새 대화가 없어도) 상대 아카이브 전체를 다시 파싱했다(실측 88MB 430ms, 아카이브에 비례해 커진다).
# 프로세스 메모리라 앱을 다시 켜면 한 번은 다시 읽는다. 키에 DB 경로를 넣어 다른 DB 와 섞이지 않게.
_imported: dict[tuple[str, str], tuple[int, int]] = {}
# 가져오는 동안 이 턴 수마다 커밋해 쓰기 잠금을 놓는다. 실사용 DB 사본(5,003턴 전부 갱신)에서 다른 쓰기가 막힌 최대 시간:
# 파일 끝에서만 6.6초, 200턴 1.2초, 50턴 0.55초, 25턴 0.33초(커밋 수가 늘어도 전체 시간은 비슷).
_COMMIT_EVERY = 50


def import_archives(db, projects_dir: str | Path, my_did: str, *, vi=None, log_fn=print) -> int:
    """다른 기기 export 파일에서 로컬에 없는(또는 더 완성된) 턴/청크/정제를 병합. 반환: 반영된 턴 수.

    superset-wins: 이미 있는 턴도 상대가 더 완성이면 갱신한다. vi 를 넘기면 갱신된 턴의 스테일
    벡터를 제거해 이어지는 backfill 이 재임베딩하게 한다(안 넘기면 청크/텍스트만 갱신, 벡터는
    다음 스윕에서 정리).

    벡터는 넣지 않음 → 이후 증분 색인의 backfill이 활성 모델로 임베딩(chunk_count>len(vi)이 되므로).
    """
    # 신규 폴더 + 레거시 폴더(.engram-archive·.chatmem-archive)를 함께 스캔 → 예전 스냅샷도 계속 import.
    dirs = [Path(projects_dir) / ARCHIVE_DIRNAME,
            *(Path(projects_dir) / name for name in LEGACY_ARCHIVE_DIRNAMES)]
    files = [p for d in dirs if d.exists() for p in d.glob("*.ndjson")]
    db_key = str(getattr(db, "path", ""))
    stamps: dict[Path, tuple[int, int]] = {}
    for p in files:
        try:
            st = p.stat()
            stamps[p] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    files = [p for p in files if p.stem != my_did and _imported.get((db_key, str(p))) != stamps.get(p)]
    if not files:
        return 0
    have = {row[0] for row in db.conn.execute("SELECT id FROM turns")}
    added = 0
    meta = 0          # 제목·접힘 등 정리 상태 반영 건수(#233)
    removed_any = False
    for p in sorted(files):
        if p.stem == my_did:
            continue   # 내 export는 건너뜀
        try:
            fh = open(p, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — 한 파일 못 열어도 다른 기기 것은 읽는다
            log_fn(f"ERROR 아카이브 열기 실패 {p.name}: {e}")
            continue
        bad_lines = 0
        file_added = file_meta = 0
        stale_keys: list[str] = []   # 이 파일에서 갱신한 턴의 옛 벡터 키 - 파일 끝에서 한 번에 지운다
        with fh:
            for lineno, line in enumerate(fh, 1):
                try:
                    line = line.strip()
                    if not line:
                        continue
                    rec = _checked(json.loads(line))   # **쓰기 전에** 모양을 본다
                    # 정리 상태(#233) — '늦게 바꾼 쪽이 이김'. 잘못 퍼져도 되돌릴 수 있어서
                    # 턴 삭제(#228)처럼 보수적으로 갈 이유가 없다.
                    if "title" in rec:
                        sid, title, at = rec["title"]
                        file_meta += db.apply_title(sid, title, float(at))
                        continue
                    if "fold" in rec:
                        tid, folded, at = rec["fold"]
                        file_meta += db.apply_fold(tid, int(folded), float(at))
                        continue
                    # 폴더는 uid 로 오간다(#233). 순서가 문제인데 — 항목/부모가 폴더보다
                    # 먼저 와도 그 회차엔 붙지 않고 다음 회차에 제자리를 찾는다.
                    if "folder" in rec:
                        uid, nm, pu, pos, at = rec["folder"]
                        file_meta += db.apply_folder(uid, nm, pu, pos, float(at))
                        continue
                    if "fitem" in rec:
                        fu, kind, ref, alias, pos, at = rec["fitem"]
                        file_meta += db.apply_folder_item(fu, kind, ref, alias, pos, float(at))
                        continue
                    if "folder_x" in rec:
                        uid, at = rec["folder_x"]
                        file_meta += db.apply_folder_removed(uid, float(at))
                        continue
                    if "fitem_x" in rec:
                        fu, kind, ref, at = rec["fitem_x"]
                        file_meta += db.apply_folder_item_removed(fu, kind, ref, float(at))
                        continue
                    if "t" not in rec:
                        continue            # 모르는 줄 종류(더 새 버전) — 건너뛴다
                    t = rec["t"]
                    tid = t[0]
                    existing = tid in have
                    # 이미 있는 턴은 상대가 '더 나을' 때만 갱신한다. 기준은 upsert_turn 과 같은
                    # (파서 버전, 내용 길이) — 더 새 파서가 이기고, 같으면 더 완성된 쪽이 이긴다.
                    # 길이만 보면 업그레이드 안 한 기기의 합쳐진(더 긴) 옛 턴이 고친 것을 되덮는다.
                    peer_v = int(t[14] or 0) if len(t) > 14 else 0
                    if existing:
                        peer_n = len(t[6] or "") + len(t[7] or "") + len(t[8] or "")
                        if (peer_v, peer_n) <= (db.turn_rank(tid) or (0, 0)):
                            continue
                    # source/source_file 는 신 스냅샷에만 있음(옛 스냅샷 t 는 11칸) → 길이로 판별.
                    src = t[11] if len(t) > 11 else None
                    src_file = t[12] if len(t) > 12 else None
                    turn = Turn(id=t[0], session_id=t[1], uuid=t[2], parent_uuid=t[3],
                                timestamp=t[4], project=t[5], question=t[6], answer=t[7],
                                actions=_actions_from_json(t[8]), source=src or "claude-code",
                                # 옛 스냅샷엔 없다 → 모르면 거짓. 안 실으면 동기화가 배지를 지운다.
                                queued=bool(t[13]) if len(t) > 13 else False,
                                parser_version=peer_v)
                    db.upsert_turn(turn, source=src or "claude-code", source_file=src_file)  # FTS 포함(신규거나 더 완성 → 기록)
                    if t[9]:                            # summary → 정제도 함께 보존
                        db.set_enrichment(tid, t[9], json.loads(t[10]) if t[10] else [])
                    if existing:
                        # 갱신: 기존 청크·벡터를 상대 것으로 교체(누적/스테일 방지). 벡터는
                        # vi.remove 로 무효화 → 이어지는 backfill 이 활성 모델로 재임베딩.
                        old_keys = [r[0] for r in db.conn.execute(
                            "SELECT chunk_key FROM chunks WHERE turn_id=?", (tid,))]
                        db.conn.execute("DELETE FROM chunks WHERE turn_id=?", (tid,))
                        stale_keys.extend(old_keys)
                    for idx, text in rec.get("c", []):
                        db.conn.execute(
                            "INSERT OR REPLACE INTO chunks(chunk_key,turn_id,idx,text) VALUES(?,?,?,?)",
                            (f"{tid}#{idx}", tid, idx, text))
                    have.add(tid)
                    file_added += 1
                    if file_added % _COMMIT_EVERY == 0:
                        # 상대 기기가 하나뿐이면 그 파일이 곧 전체다 - 파일 끝에서만 커밋하면 모든 턴이 갱신되는
                        # 첫 동기화(파서 버전 전환 등)에서 수십 초 쓰기 잠금을 쥔다. 벡터를 먼저 지우고 커밋.
                        if vi is not None and stale_keys:
                            vi.remove(stale_keys)
                            stale_keys.clear()
                            removed_any = True
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    # **줄 단위로 잡는다.** 파일 단위로 잡으면 깨진 한 줄이 그 뒤 전부를
                    # 버린다 — 실측으로 5턴 중 3턴이 조용히 사라졌다.
                    bad_lines += 1
                    if bad_lines <= 3:
                        log_fn(f"ERROR 아카이브 {p.name}:{lineno} 건너뜀: {e}")
        # 파일마다 벡터를 지우고 커밋한다. 전부 한 번에 커밋하면 쓰기 잠금을 끝까지 쥐어(첫 동기화·파서 버전
        # 전환 때 수십 초) 그동안 앱의 접기·제목 저장이 막힌다. 벡터를 지우는 쪽이 먼저다 - 커밋 뒤에 끊기면
        # 새 청크가 옛 벡터를 단 채 남는다(반대 순서는 벡터만 없어져 backfill 이 다시 만든다).
        if vi is not None and stale_keys:
            vi.remove(stale_keys)
            removed_any = True
        # meta(제목·접힘·폴더)도 커밋 대상이다. added 만 보면 '새 턴 없이 정리 상태만 온 회차'가
        # 통째로 롤백된다 — 커넥션이 닫힐 때 미완료 트랜잭션이 되돌려지기 때문이다.
        # 앱은 매 호출마다 새 커넥션을 열어서, 실사용에서는 사실상 항상 이 경우였다.
        if file_added or file_meta:
            db.commit()
        added += file_added
        meta += file_meta
        if bad_lines:
            log_fn(f"ERROR 아카이브 {p.name} — 읽을 수 없는 줄 {bad_lines}개를 건너뛰었어요")
        elif p in stamps:
            _imported[(db_key, str(p))] = stamps[p]   # 깨진 줄이 있으면 다음에도 읽어 오류를 계속 알린다
    if removed_any and vi is not None:
        vi.save()
    if meta:
        log_fn(f"정리 상태 {meta}건 반영(제목·접힘)")
    return added
