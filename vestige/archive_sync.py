"""아카이브 export/import — 기기 간 '삭제된 원본 세션'까지 공유(내장 Syncthing 폴더 경유).

Claude Code는 세션 로그를 ~30일만 보관하지만 vestige 아카이브는 영구 보존한다. 그런데
archive.db(SQLite)는 직접 동기화하면 손상되므로, 세션 동기 설계와 같은 방식으로:
  - 각 기기가 자기 아카이브를 텍스트(NDJSON)로 <projects>/.vestige-archive/<device_id>.ndjson 에 스냅샷
    (이미 공유 중인 폴더라 Syncthing이 자동 전파, 기기 하나당 파일 하나 → 다중 writer 충돌 없음)
  - 다른 기기 파일을 읽어 **로컬에 없는 세션(턴/청크/정제)만** import
  - 벡터는 옮기지 않음 → 받은 기기가 자기 임베딩 모델로 backfill(모델·백엔드 달라도 됨)
"""

from __future__ import annotations

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
            "source,source_file "
            "FROM turns",
        ):
            # t[11]=source, t[12]=source_file 를 뒤에 append(옛 스냅샷은 11칸이라 import 에서 길이로 판별).
            rec = {
                "t": [r["id"], r["session_id"], r["uuid"], r["parent_uuid"], r["timestamp"],
                      r["project"], r["question"], r["answer"], r["actions"], r["summary"], r["tags"],
                      r["source"], r["source_file"]],
                "c": chunks.get(r["id"], []),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
        # 대화 위에 얹은 정리 상태(#233). 줄 종류를 늘리는 방식이라 하위호환이 된다 —
        # 모르는 키를 무시하는 옛 버전도 이 파일을 계속 읽는다.
        for sid, title, at in db.sync_title_rows():
            f.write(json.dumps({"title": [sid, title, at]}, ensure_ascii=False) + "\n")
        for tid, folded, at in db.sync_fold_rows():
            f.write(json.dumps({"fold": [tid, folded, at]}, ensure_ascii=False) + "\n")
    os.replace(tmp, d / f"{did}.ndjson")   # 원자적 교체
    return n


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
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    # 정리 상태(#233) — '늦게 바꾼 쪽이 이김'. 잘못 퍼져도 되돌릴 수 있어서
                    # 턴 삭제(#228)처럼 보수적으로 갈 이유가 없다.
                    if "title" in rec:
                        sid, title, at = rec["title"]
                        meta += db.apply_title(sid, title, float(at))
                        continue
                    if "fold" in rec:
                        tid, folded, at = rec["fold"]
                        meta += db.apply_fold(tid, int(folded), float(at))
                        continue
                    if "t" not in rec:
                        continue            # 모르는 줄 종류(더 새 버전) — 건너뛴다
                    t = rec["t"]
                    tid = t[0]
                    existing = tid in have
                    # superset-wins: 이미 있는 턴은 상대가 '더 완성'(질문+답변+행동 길이가 더 큼)
                    # 일 때만 갱신. 동일/더 짧으면 유지(불필요 재작업·축소 방지).
                    if existing:
                        peer_n = len(t[6] or "") + len(t[7] or "") + len(t[8] or "")
                        if peer_n <= (db.turn_content_len(tid) or 0):
                            continue
                    # source/source_file 는 신 스냅샷에만 있음(옛 스냅샷 t 는 11칸) → 길이로 판별.
                    src = t[11] if len(t) > 11 else None
                    src_file = t[12] if len(t) > 12 else None
                    turn = Turn(id=t[0], session_id=t[1], uuid=t[2], parent_uuid=t[3],
                                timestamp=t[4], project=t[5], question=t[6], answer=t[7],
                                actions=_actions_from_json(t[8]), source=src or "claude-code")
                    db.upsert_turn(turn, source=src or "claude-code", source_file=src_file)  # FTS 포함(신규거나 더 완성 → 기록)
                    if t[9]:                            # summary → 정제도 함께 보존
                        db.set_enrichment(tid, t[9], json.loads(t[10]) if t[10] else [])
                    if existing:
                        # 갱신: 기존 청크·벡터를 상대 것으로 교체(누적/스테일 방지). 벡터는
                        # vi.remove 로 무효화 → 이어지는 backfill 이 활성 모델로 재임베딩.
                        old_keys = [r[0] for r in db.conn.execute(
                            "SELECT chunk_key FROM chunks WHERE turn_id=?", (tid,))]
                        db.conn.execute("DELETE FROM chunks WHERE turn_id=?", (tid,))
                        if vi is not None and old_keys:
                            vi.remove(old_keys)
                            removed_any = True
                    for idx, text in rec.get("c", []):
                        db.conn.execute(
                            "INSERT OR REPLACE INTO chunks(chunk_key,turn_id,idx,text) VALUES(?,?,?,?)",
                            (f"{tid}#{idx}", tid, idx, text))
                    have.add(tid)
                    added += 1
        except Exception as e:  # noqa: BLE001 — 한 파일 실패가 전체를 막지 않게
            log_fn(f"아카이브 import 실패 {p.name}: {e}")
    if added:
        db.commit()
    if removed_any and vi is not None:
        vi.save()
    if meta:
        log_fn(f"정리 상태 {meta}건 반영(제목·접힘)")
    return added
