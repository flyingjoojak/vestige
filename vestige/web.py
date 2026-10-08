"""로컬 웹 검색 UI (FastAPI). 자체 앱 이전에 브라우저에서 검색을 테스트하는 용도.

- 임베딩 모델은 **지연 로드**(첫 검색/색인 때) + 유휴 시 언로드 → 평소 상주 RAM 최소화.
- DB·벡터 인덱스는 요청마다 새로 열어 최신 데이터 반영 + 스레드 안전.
- 코어 라이브러리(search/store/vectorindex/embedder)를 그대로 재사용.

실행: python -m vestige.web  → http://127.0.0.1:8642
"""

from __future__ import annotations

import contextlib
import json
import logging
import mmap
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .int8_model import INT8_MODEL_ID
from .search import search as run_search
from .store import ArchiveDB, _actions_from_json
from .vectorindex import make_index, vector_count

_state: dict = {}
# 빌드된 React 프론트(있으면 서빙, 없으면 인라인 _HTML 폴백).
# PyInstaller 번들이면 sys._MEIPASS 안의 임베드 경로, 아니면 저장소 상대경로.
import sys as _sys
_MEIPASS = getattr(_sys, "_MEIPASS", None)
_DIST = (Path(_MEIPASS) / "frontend" / "dist") if _MEIPASS \
    else (Path(__file__).resolve().parent.parent / "frontend" / "dist")


# 색인 상태(자동/수동 증분 색인 진행 — UI에 노출).
_autoindex_state: dict = {"enabled": False, "running": False, "phase": "대기", "indexed_total": 0,
                          "done_files": 0, "total_files": 0,
                          "done_chunks": 0, "total_chunks": 0, "last_error": None, "errors": [], "sync_errors": []}
# 증분색인·전체재색인 상호배제(자동 스레드/수동 트리거/재색인이 동시에 안 돌게).
_index_lock = threading.Lock()

# ── 임베더 지연 로드 + 유휴 언로드 ──
# 상시 앱(백엔드가 계속 떠 있음)에서 모델을 계속 물고 있으면 RAM이 잡혀 렉이 난다.
# 그래서 검색/색인할 때만 로드하고, 마지막 사용 후 IDLE_SECS 지나면 내려 RAM을 반환한다.
_embedder_lock = threading.Lock()
_embedder_last_used = [0.0]   # time.monotonic() 기준 마지막 사용 시각


def get_embedder():
    """임베더 지연 로드(현재 확정 모델 기준) + 마지막 사용 시각 갱신. 스레드 안전."""
    with _embedder_lock:
        emb = _state.get("embedder")
        if emb is None:
            from . import config as C
            from .embedder import Embedder
            # 저장 벡터가 만들어진 모델(meta)이 진실원본 — 그걸로 로드해야 검색/색인이 호환된다.
            model = None
            with contextlib.suppress(Exception):
                model = ArchiveDB().get_meta("embed_model")
            emb = Embedder(model) if model else Embedder(C.EMBED_MODEL)
            _state["embedder"] = emb
        _embedder_last_used[0] = time.monotonic()
        return emb


def _maybe_unload_embedder() -> None:
    """마지막 사용 후 IDLE_SECS 초과 + 색인/재색인 중이 아니면 모델을 내려 RAM 반환.
    진행 중인 검색/색인은 각자 로컬 참조를 들고 있어 참조카운트로 살아있으므로 안전."""
    from . import config as C
    with _embedder_lock:
        if _state.get("embedder") is None:
            return
        if _autoindex_state.get("running") or _reindex_state.get("running"):
            return
        if time.monotonic() - _embedder_last_used[0] < getattr(C, "IDLE_SECS", 120):
            return
        _state.pop("embedder", None)
    import gc
    gc.collect()   # onnxruntime 세션·텐서 해제 유도


_FULL_SWEEP_SECS = 300   # realtime 모드에서 peer 병합·자가복구 전체 스윕 최소 간격(초)
_QUICK_HEAVY_MIN_SECS = 60   # realtime 모드에서 무거운 색인 경로(벡터 로드+풀스캔) 최소 간격(초)
_last_full_sweep = [0.0]
_EXPORT_MIN_SECS = 300   # 아카이브 내보내기 최소 간격(초) - 매번 전체를 다시 쓴다
_export_due = [False]    # 내보낼 변경이 쌓여 있다
_last_export = [0.0]
_last_heavy_run = [0.0]


def _run_incremental(quick: bool = False) -> bool:
    """새 대화만 증분 색인(현재 임베더 재사용, 벡터 유지). 자동 스레드·수동 트리거 공용.
    다른 색인 작업이 이미 돌고 있으면 스킵. 반환: 실제로 실행했으면 True.
    quick=True(realtime 폴링)면 값싼 has_new_data(세션 파일 stat) 체크를 먼저 해,
    새 대화가 없고 전체 스윕 주기(_FULL_SWEEP_SECS)도 아니면 벡터 인덱스 로드 없이 즉시 반환한다.
    (10초 폴링이 전체 벡터 행렬 재로드+turns 풀스캔이 되지 않게 함)."""
    if not _index_lock.acquire(blocking=False):
        return False
    try:
        _xlock = None                       # 크로스-프로세스 락(무거운 경로 진입 시 획득)
        import time as _time
        from . import config as C
        from .archive_sync import device_id, export_archive, import_archives
        from .indexer import backfill_missing, has_new_data, index_all, reconcile
        db = ArchiveDB()
        if quick:
            # 값싼 게이트: 로컬 세션 파일 stat만 확인(벡터 로드·풀스캔 없음).
            now = _time.time()
            sweep_due = (now - _last_full_sweep[0]) >= _FULL_SWEEP_SECS
            heavy_due = (now - _last_heavy_run[0]) >= _QUICK_HEAVY_MIN_SECS
            # 무거운 경로(벡터 로드+turns 풀스캔+피어 재파싱)는 최소 간격을 둔다.
            # 진행 중 턴은 파일이 계속 자라 has_new_data=True가 유지되지만, 마지막 턴은
            # IDLE_SECS(120s)까지 홀드백되어 실제로 색인할 게 없으므로 10초마다 재로드하지 않는다.
            # sweep_due면(피어 병합/자가복구 주기) 무조건 통과.
            if not sweep_due and not (has_new_data(db) and heavy_due):
                _autoindex_state.update(running=False, phase="새 대화 없음")
                return True
        # 크로스-프로세스 상호배제: OS 스케줄러가 띄운 별도 `vestige index` 프로세스와 동시에
        # 같은 archive.db 를 색인하지 않게. 다른 프로세스가 색인 중이면 이번 회차는 건너뛴다.
        from .proclock import IndexLock
        _xlock = IndexLock()
        if not _xlock.acquire():
            _xlock = None
            _autoindex_state.update(running=False, phase="다른 프로세스 색인 중")
            return True
        vi = make_index()
        _last_heavy_run[0] = _time.time()   # 무거운 경로 진입 시각(quick 스로틀용)
        with contextlib.suppress(Exception):
            reconcile(db, vi, log_fn=lambda m: None)   # 고아 벡터 정리(값쌈)
        # 기기 간 아카이브 병합: 다른 기기가 보존한 세션(삭제된 원본 포함)을 먼저 가져온다.
        # 새로 들어온 청크는 아래 backfill이 활성 모델로 임베딩(chunk_count>len(vi)이 됨).
        with contextlib.suppress(Exception):
            # 로그를 버리면 동기화가 조용히 실패한다 — 색인 상태에 흘려보낸다('ERROR ' 접두만 UI 도달).
            # 동기화 오류는 별도 칸에 모은다. errors 는 아래 색인 단계가 회차마다 비우는데,
            # 같이 담으면 방금 남긴 동기화 오류가 UI 가 한 번 읽기도 전에 지워진다.
            _autoindex_state["sync_errors"] = []   # 이번 회차 것만 — 해소된 옛 오류가 남지 않게
            import_archives(db, C.PROJECTS_DIR, device_id(db), vi=vi,
                            log_fn=_capture_log(_autoindex_state, key="sync_errors"))
        new = has_new_data(db)
        # 활성 저장소에 빠진 청크가 있으면(백엔드 전환·유실·아카이브 import) 새 대화가 없어도 자가복구한다.
        chunk_count = db.conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        missing = len(vi) < chunk_count
        if not new and not missing:
            _last_full_sweep[0] = _time.time()   # 전체 스윕 성공 완료(peer 병합·고아 정리까지)
            _autoindex_state.update(running=False, phase="새 대화 없음")
            return True
        if _state.get("needs_onboarding"):
            return False   # 모델 미선택(온보딩 전) — 이후 회차에
        emb = get_embedder()   # 할 일이 있을 때만 지연 로드(유휴 언로드와 짝)

        _autoindex_state["errors"] = []   # 이번 회차 항목별 오류만 모음(스턱 항목이면 매 회차 재등장)
        total = 0
        if new:
            _autoindex_state.update(running=True, phase="색인 중", last_error=None,
                                    done_files=0, total_files=0, done_chunks=0, total_chunks=0)
            total = index_all(
                db, vi, emb,
                log_fn=_capture_log(_autoindex_state),
                progress_fn=lambda d, t: _autoindex_state.update(done_files=d, total_files=t),
            )
            _autoindex_state["indexed_total"] += total

        filled = 0
        if missing:
            _autoindex_state.update(running=True, phase="자가복구 중", last_error=None,
                                    done_chunks=0, total_chunks=0)
            filled = backfill_missing(
                db, vi, emb,
                log_fn=_capture_log(_autoindex_state),
                progress_fn=lambda d, t: _autoindex_state.update(done_chunks=d, total_chunks=t),
            )

        # 이 기기 아카이브를 공유 폴더로 export(다른 기기가 가져가게). 변경 있었을 때만, 그리고
        # _EXPORT_MIN_SECS 에 한 번만 - 매번 아카이브 전체를 다시 쓴다(실측 턴 4.9천에 약 1초). 실시간
        # 모드는 60초마다 돌아 그만큼 반복됐다. 미룬 export 는 다음 회차(늦어도 전체 스윕 주기)에 나간다.
        if total or filled:
            _export_due[0] = True
        if _export_due[0] and _time.time() - _last_export[0] >= _EXPORT_MIN_SECS:
            try:
                export_archive(db, C.PROJECTS_DIR, device_id(db))
                _export_due[0] = False
                _last_export[0] = _time.time()
            except Exception as ex:  # noqa: BLE001
                # 예전엔 조용히 삼켰다 - 가져오기 실패는 상태바에 떴는데 내보내기 실패는 안 떠서,
                # 이 기기 대화가 다른 기기로 안 넘어가도 알 수 없었다.
                _autoindex_state.setdefault("sync_errors", []).append(f"ERROR 아카이브 내보내기 실패: {ex}")

        _last_full_sweep[0] = _time.time()   # 전체 스윕(색인 포함) 성공 완료
        done_msg = f"최근 완료(+{total}턴" + (f", 복구 {filled}청크)" if filled else ")")
        _autoindex_state.update(running=False, phase=done_msg, done_chunks=0, total_chunks=0)
        return True
    except Exception as ex:                       # 한 번의 오류로 죽지 않게
        # 실패 시 재시도는 ~_QUICK_HEAVY_MIN_SECS(60s) 뒤로 미룬다(0으로 리셋하면 sweep_due가
        # 즉시 True가 되어 heavy_due 스로틀을 우회 → realtime에서 10초마다 재시도 스톰).
        _last_full_sweep[0] = _time.time() - _FULL_SWEEP_SECS + _QUICK_HEAVY_MIN_SECS
        # 백그라운드 스레드 예외는 FastAPI 예외핸들러(app.log)에 안 잡히므로 여기서 직접 트레이스백을 남긴다.
        import traceback
        traceback.print_exc(file=_sys.stderr)
        _autoindex_state.update(running=False, phase="오류", last_error=str(ex))
        return True
    finally:
        if _xlock is not None:
            _xlock.release()
        _index_lock.release()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    # 첫 실행 온보딩(프리즈 exe 전용): 아직 모델을 고른 적 없으면(=색인 이력 없음) 무거운 기본 모델을
    # 미리 로드하지 않는다. 저사양 기기가 6GB짜리 e5-large를 자동으로 물지 않게, 사용자가 먼저 고른다.
    _needs_onboarding = False
    with contextlib.suppress(Exception):
        if getattr(_sys, "frozen", False) and ArchiveDB().get_meta("embed_model") is None:
            _needs_onboarding = True
    _state["needs_onboarding"] = _needs_onboarding

    if not _needs_onboarding:
        # 임베더는 지연 로드(get_embedder) — 시작 시 미리 물지 않아 유휴 RAM 최소화.
        # 저장 벡터의 모델 ≠ 현재 설정 모델이면 검색이 조용히 엉터리가 됨 → 배너로 경고(재색인 유도).
        with contextlib.suppress(Exception):
            from . import config as _C
            stored = ArchiveDB().get_meta("embed_model")
            _state["model_mismatch"] = ({"stored": stored, "current": _C.EMBED_MODEL}
                                        if stored and stored != _C.EMBED_MODEL else None)

    # 자동 색인: 웹서버가 스스로 색인한다(사용자가 모드로 제어 — 자율성).
    # 모드는 매 틱 config에서 재평가(api_config_put이 importlib.reload로 갱신) → 설정 변경 즉시 반영.
    #   off       : 자동 색인 안 함
    #   interval  : INDEX_INTERVAL_MIN 분마다(기본)
    #   realtime  : 짧은 폴링으로 새 대화가 생기면 곧바로(quick 게이트로 새 대화 없으면 값싸게 즉시 반환)
    #   scheduled : INDEX_TIME(HH:MM)에 하루 1회
    def _autoindex():
        import time
        from datetime import datetime

        from . import config as C
        last_run = 0.0
        last_sched_day = None
        while True:
            mode = getattr(C, "INDEX_MODE", "interval")
            _autoindex_state["enabled"] = mode != "off"
            should = False
            if mode == "realtime":
                should = True
            elif mode == "scheduled":
                dt = datetime.now()
                hh, mm = 3, 0
                try:
                    _h, _m = (getattr(C, "INDEX_TIME", "03:00") or "03:00").split(":")
                    _h, _m = int(_h), int(_m)
                    if 0 <= _h <= 23 and 0 <= _m <= 59:   # 범위 밖("25:99")은 기본 03:00으로
                        hh, mm = _h, _m
                except (ValueError, AttributeError):
                    pass
                # 미스드윈도우 캐치업: 예약 시각 지나 그날 아직 안 돌았으면 실행(정확 분을 놓쳐도).
                if last_sched_day != dt.date() and (dt.hour, dt.minute) >= (hh, mm):
                    last_sched_day = dt.date()
                    should = True
            elif mode != "off":   # interval(기본)
                iv = max(60, int(getattr(C, "INDEX_INTERVAL_MIN", 10)) * 60)
                if time.time() - last_run >= iv:
                    should = True
            if should:
                try:
                    _run_incremental(quick=(mode == "realtime"))
                except Exception as ex:   # 백그라운드 스레드 — 흔적 없이 죽지 않게 최소 로깅
                    print(f"[autoindex] tick failed: {ex}", file=_sys.stderr)
                last_run = time.time()
            time.sleep(10 if mode == "realtime" else 20)

    # OS 스케줄러(vestige setup이 등록한 vestige-index)와 이중 색인 방지:
    #   - frozen(패키지 데스크톱 앱): OS 스케줄러 없음 → 웹앱이 자체 색인(INDEX_MODE 제어)
    #   - pip + `vestige setup`(OS 색인 태스크 등록됨): 그쪽이 색인 담당 → 인프로세스 루프 끔
    #   - dev / --no-scheduler(OS 태스크 없음): 인프로세스 루프가 담당(INDEX_MODE 제어)
    # 판정은 시작 시 1회만(태스크 조회 subprocess 1회) — 매 틱 비용 없음.
    _self_index = getattr(_sys, "frozen", False)
    if not _self_index:
        try:
            from . import scheduler
            _self_index = not scheduler.index_scheduled()
        except Exception as ex:
            # 스케줄러 상태를 확인 못 하면 "OS 스케줄러 확정"으로 오해해 색인이 아예 안 도는
            # 최악을 피한다 → 인프로세스 루프가 담당(색인 안 도는 것보다 안전). 흔적은 남긴다.
            _self_index = True
            print(f"[autoindex] scheduler probe failed, self-indexing: {ex}", file=_sys.stderr)
    if _self_index:
        threading.Thread(target=_autoindex, daemon=True).start()
    else:
        _autoindex_state["enabled"] = False   # OS 스케줄러가 색인 담당 중

    # 유휴 언로더: 마지막 사용 후 IDLE_SECS 지나면 임베더를 내려 RAM 반환(상시 앱 렉 방지).
    def _idle_unloader():
        while True:
            time.sleep(30)
            with contextlib.suppress(Exception):
                _maybe_unload_embedder()
    threading.Thread(target=_idle_unloader, daemon=True).start()

    # 의미 지도(3D)를 백그라운드에서 예열 + 주기적으로 갱신 → 사용자는 항상 즉시·최신.
    # 오래 사는 웹 서버에서 하므로 UMAP numba JIT은 1회만(짧은 인덱스 프로세스와 대조).
    def _warm():
        import time
        import traceback
        try:
            _graph3d_data()                 # 시작 시 1회 준비(캐시 있으면 즉시)
        except Exception:
            traceback.print_exc(file=_sys.stderr)
        while True:                          # 이후 주기적으로 벡터 수 바뀌면 조용히 재계산
            time.sleep(180)
            try:
                _graph3d_data()              # stale-while-revalidate: 바뀌었으면 백그라운드 갱신 트리거
            except Exception:
                traceback.print_exc(file=_sys.stderr)
    threading.Thread(target=_warm, daemon=True).start()

    # 로그 폴더를 뒤에서 미리 훑는다(하위 세션 로그 수백 MB 를 걸러 수 초). 세션 목록·대기 건수는 이 훑기를
    # 기다리지 않는다 - 예전엔 앱을 켠 뒤 첫 세션 목록이 이걸 기다리느라 44초 멈췄다.
    try:
        from .indexer import warm_walk_cache
        warm_walk_cache()
    except Exception:  # 실패하면 첫 요청이 그 자리에서 훑어 첫 목록이 다시 멈춘다 - 이유를 남긴다
        logging.getLogger(__name__).warning("로그 폴더 미리 훑기 실패", exc_info=True)

    # 이전에 켜둔 기기 연결(임베디드 Syncthing)이 있으면 자동 재개.
    # 충돌 정리 워커는 기기 연결에 종속 — _st_start_bg가 준비되면 함께 시작한다(별도 토글 없음).
    with contextlib.suppress(Exception):
        db = ArchiveDB()
        if db.get_meta("syncthing_enabled") == "1":
            _st_start_bg(persist=False)

    yield
    _sync_stop(persist=False)
    _st_stop(persist=False)
    _state.clear()


app = FastAPI(lifespan=_lifespan, title="Vestige")


# ── CSRF 보호(Fetch Metadata resource isolation) ──────────────────────────
# 서버가 127.0.0.1 에만 바인딩돼도, 사용자가 브라우저에서 연 악성 페이지가 이 서버로 폼/fetch를
# 자동 전송할 수 있다(CSRF). 특히 /api/resume 는 OS 프로세스를 띄우므로 방어가 필요.
# 정책: 상태변경 메서드에서 Sec-Fetch-Site == "cross-site" 면 차단.
#   허용 = same-origin(앱 자신) · none(직접 네비게이션) · same-site(dev 프록시 등) · 헤더 없음
#         (비브라우저 클라이언트: CLI·테스트·Electron 사이드카). 브라우저만 이 헤더를 보낸다.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})   # 루프백만 정상
_BLOCKED_SITES = frozenset({"cross-site", "same-site"})         # 앱 자신(same-origin)만 허용


def _host_of(value: str | None) -> str | None:
    """Host/Origin 값에서 호스트명만 추출(스킴·경로·포트 제거, [::1] 처리), 소문자."""
    if not value:
        return None
    v = value.strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):          # [::1]:port
        return v[1:].split("]", 1)[0].lower()
    if ":" in v:
        v = v.rsplit(":", 1)[0]
    return v.lower()


def _csrf_blocked(method: str, sec_fetch_site: str | None,
                  host: str | None = None, origin: str | None = None) -> bool:
    """차단 대상이면 True.

    - Host 검증(모든 메서드): Host 가 루프백이 아니면 차단 → DNS 리바인딩(evil.com→127.0.0.1)으로
      same-origin 을 위조하는 우회를 봉쇄.
    - 상태변경 메서드: Sec-Fetch-Site 가 cross-site/same-site 면 차단(앱 자신 same-origin만 허용).
      헤더가 없으면(구형 브라우저) Origin 으로 폴백 — 오리진이 루프백이 아니면 차단.
      Sec-Fetch-Site·Origin 둘 다 없으면 허용 = 비브라우저 클라(CLI·테스트·사이드카).
    """
    if host is not None and _host_of(host) not in _ALLOWED_HOSTS:
        return True
    if method.upper() in _SAFE_METHODS:
        return False
    if sec_fetch_site is not None:
        return sec_fetch_site in _BLOCKED_SITES
    if origin:
        return _host_of(origin) not in _ALLOWED_HOSTS
    return False


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):  # noqa: ANN001,ANN201 — Starlette 미들웨어 시그니처
    h = request.headers
    if _csrf_blocked(request.method, h.get("sec-fetch-site"), h.get("host"), h.get("origin")):
        return JSONResponse(
            status_code=403,
            content={"error": "허용되지 않은 출처의 요청이에요(CSRF 보호). 앱 안에서 실행해 주세요.", "code": "csrf_blocked"},
        )
    return await call_next(request)


# 방어심층 보안 헤더. SPA는 인라인 스크립트가 없어(script-src 'self') 엄격 CSP가 안전하다.
# 인라인 element style(style={})은 쓰므로 style-src 에 'unsafe-inline' 만 허용. 모두 동일 출처(로컬).
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
_SEC_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):  # noqa: ANN001,ANN201 — Starlette 시그니처
    resp = await call_next(request)
    for k, v in _SEC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


@app.exception_handler(Exception)
async def _friendly_error(request, exc):  # noqa: ANN001 — FastAPI 핸들러 시그니처
    """예상치 못한 서버 오류를 500 대신 사람이 읽는 한글 메시지로. (HTTPException은 별도 처리됨)"""
    import sqlite3
    import traceback

    from fastapi.responses import JSONResponse
    # 관측성: 삼키기 전에 전체 트레이스백을 로그(exe면 data/app.log)로 남긴다.
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    # code 를 붙여 화면이 번역하게 한다(없으면 영문 화면에도 이 한국어가 나간다).
    # 예전 문구는 "archive.db 가 손상됐을 수 있어요(삭제하면 재생성됩니다)" 였다. 이 오류는 다른 프로세스와
    # 잠깐 겹친 'database is locked' 에서도 나는데, 그 말을 믿고 지우면 폴더·제목·접힘이 사라지고 원본
    # 로그가 이미 지워진(Claude Code 는 30일 뒤 지운다) 대화는 영영 복구할 수 없다. 지우라고 하지 않는다.
    if isinstance(exc, (sqlite3.Error, OSError)):
        code = "server_db_error"
        msg = "데이터에 접근하지 못했어요. 잠시 후 다시 시도해 주세요. 계속되면 앱을 다시 시작해 주세요."
    else:
        code = "server_error"
        msg = "예상치 못한 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
    return JSONResponse(status_code=500, content={"code": code, "error": msg, "detail": str(exc)[:300]})


@app.get("/api/debug/index")
def api_debug_index():
    """기기 간 비교 진단: 세션별 (원본 JSONL 바이트 vs 색인된 턴 수).

    두 기기에서 열어 비교 → jsonl_total_kb가 다르면 '내용 동기화 지연'(B 파일이 짧음),
    같은데 turns가 다르면 '색인 불일치'(진짜 버그). 파일명 stem = 세션 uuid.
    """
    from . import config as C
    from .indexer import iter_jsonl
    db = ArchiveDB()
    turns = {r["session_id"]: r["n"] for r in
             db.conn.execute("SELECT session_id, COUNT(*) n FROM turns GROUP BY session_id")}
    files: dict[str, int] = {}
    total = 0
    if C.PROJECTS_DIR.exists():
        for p in iter_jsonl(C.PROJECTS_DIR):
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            total += sz
            files[p.stem] = files.get(p.stem, 0) + sz   # stem = 세션 uuid
    sids = set(turns) | set(files)
    rows = sorted(
        ({"session": s[:8], "turns": turns.get(s, 0), "file_kb": round(files.get(s, 0) / 1024)}
         for s in sids),
        key=lambda r: -max(r["turns"], r["file_kb"]))
    return {"jsonl_files": len(files), "jsonl_total_kb": round(total / 1024),
            "indexed_turns": sum(turns.values()), "indexed_sessions": len(turns),
            "top": rows[:40]}


@app.post("/api/quit")
def api_quit():
    """앱(백엔드) 종료 — windowed exe는 창·트레이가 없어 이 버튼으로 끈다.
    새 버전으로 교체·재실행하려면 먼저 여기서 종료해야 함(중복 기동 방지 때문)."""
    import os
    import time

    def bye():
        time.sleep(0.3)   # 응답 flush 후
        with contextlib.suppress(Exception):
            _st_stop()    # 내장 Syncthing 자식 프로세스 정리
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


@app.get("/api/system")
def api_system():
    """기기 메모리 + 모델/벡터 불일치 경고 + 소스 형식 드리프트(배너용)."""
    from .sysmem import available_mb, total_mb
    drift: list[str] = []
    try:
        raw = ArchiveDB().get_meta("drift_sources") or ""
        drift = [s.strip() for s in raw.split(",") if s.strip()]
    except Exception:  # noqa: BLE001 — 조회 실패해도 시스템 상태는 반환
        pass
    return {
        "ram_total_mb": total_mb(),
        "ram_avail_mb": available_mb(),
        "model_mismatch": _state.get("model_mismatch"),
        "drift_sources": drift,   # 로그 형식이 바뀌어 못 읽는 것으로 의심되는 소스
    }


@app.get("/api/report/schema")
def api_report_schema(source: str = "codex"):
    """'원클릭 형식 신고'용 리댁트 스키마 지문(대화 내용 없음). 소스 로그 포맷 변경 신고에 사용."""
    from .schema_report import build_report
    try:
        return build_report(source)
    except Exception as e:  # noqa: BLE001 — 신고 기능이라 어떤 파일 문제도 500 대신 JSON 오류로.
        return {"source": source, "error": str(e)}


def _hit_to_dict(h) -> dict:
    t = h.turn
    return {
        "id": t.id,
        "project": t.project,
        "timestamp": t.timestamp,
        "session": t.session_id[:8],
        "session_full": t.session_id,
        "question": t.question,
        # 답변·행동·앞뒤 턴은 보내지 않는다 - 화면은 결과 줄(요약/질문)만 그리고 고르면 채팅을 따로
        # 연다. 예전엔 이 셋이 응답의 80%였다(결과 100개 1.95MB 중 1.5MB).
        "cosine": h.cosine,
        "sources": list(h.sources),
        "source": t.source,   # 출처 도구(claude-code/codex) — 결과 배지·필터용
        "summary": h.summary,
        "tags": list(h.tags),
    }


@app.get("/api/search")
def api_search(
    q: str = Query(...),
    k: int = 8,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
    mode: str = "hybrid",          # hybrid | semantic | keyword
    sources: str | None = None,    # 쉼표 목록(claude-code,codex). 비면 전체 소스
    folder: int | None = None,     # 폴더 안에서만 검색(#201). 하위 폴더까지 포함
    semantic_only: bool = False,   # (구버전 호환)
):
    if semantic_only:
        mode = "semantic"
    if mode not in ("hybrid", "semantic", "keyword"):
        mode = "hybrid"   # 알 수 없는 값은 0건이 아니라 기본(하이브리드)으로
    want_sem = mode in ("hybrid", "semantic")
    want_kw = mode in ("hybrid", "keyword")
    tool_sources = {s.strip() for s in sources.split(",") if s.strip()} if sources else None
    embedder = None
    if want_sem:
        if _state.get("needs_onboarding"):
            return {"error": "먼저 임베딩 모델을 선택하세요", "hits": [], "code": "no_embed_model"}
        embedder = get_embedder()   # 지연 로드 — 유휴 후 첫 검색은 로딩에 몇 초 걸릴 수 있음
    db = ArchiveDB()
    vi = make_index()
    # 폴더 스코프: 그 폴더(+하위)가 가리키는 턴만. 빈 폴더면 빈 집합이라 결과도 0건이 맞다.
    # 없는 폴더는 0건으로 뭉개지 않고 404 — 지워진 폴더를 계속 들고 있는 화면을 드러내야 한다.
    allow_ids = None
    if folder is not None:
        _folder_or_404(db, folder)
        allow_ids = db.folder_turn_ids(folder)
    hits = run_search(q, db, vi, embedder, k=k, session=session or None,
                      since=since or None, until=until or None,
                      keyword=want_kw, semantic=want_sem, tool_sources=tool_sources,
                      allow_ids=allow_ids, with_thread=False)
    return {"query": q, "count": len(hits), "hits": [_hit_to_dict(h) for h in hits]}


@app.post("/api/embedder/warm")
def api_embedder_warm():
    """검색 화면을 열거나 검색창을 누르면 부른다 - 질문을 치는 동안 모델을 미리 올린다.
    모델은 2분 쓰지 않으면 내리므로(메모리 약 0.8GB 반환) 그 뒤 첫 검색은 로딩에 5~6초 걸렸다.
    시간을 늘리는 대신 '곧 검색한다'는 신호가 있을 때만 올린다. 이미 올라가 있으면 시각만 갱신된다."""
    if _state.get("needs_onboarding"):
        return {"ok": False, "loaded": False}
    loaded = _state.get("embedder") is not None
    if not loaded:
        threading.Thread(target=_warm_embedder, daemon=True).start()
    else:
        _embedder_last_used[0] = time.monotonic()
    return {"ok": True, "loaded": loaded}


def _warm_embedder() -> None:
    try:
        get_embedder()
    except Exception as e:  # noqa: BLE001 — 미리 올리기는 덤이다. 실패하면 검색이 그때 다시 올린다
        logging.getLogger(__name__).warning("모델 미리 올리기 실패: %s", e)


@app.get("/api/sources")
def api_sources():
    """색인된 턴이 있는 출처 목록(검색 필터 옵션). 데이터가 있는 소스만 나온다."""
    db = ArchiveDB()
    return {"sources": [{"source": s, "count": n} for s, n in db.distinct_sources()]}


@app.post("/api/sources/toggle")
def api_sources_toggle(payload: dict):
    """색인 소스 켜기/끄기(비파괴). enabled=false면 다음 색인부터 그 소스를 건너뛴다.
    기존에 색인된 데이터는 그대로 남아 검색된다(삭제하지 않음)."""
    from .sources import ADAPTERS, disabled_sources, is_substream
    name = str((payload or {}).get("source", "")).strip()
    enabled = bool((payload or {}).get("enabled", True))
    if name not in ADAPTERS:
        raise HTTPException(status_code=400, detail={"code": "unknown_source", "msg": "알 수 없는 소스"})
    if is_substream(name):
        # 하위 스트림(subagent)은 자기 토글이 없다 — 부모 출처(claude-code) 토글을 따른다.
        raise HTTPException(status_code=400, detail={"code": "substream_follows_parent",
                            "msg": "이 소스는 부모 출처 토글을 따릅니다"})
    cur = disabled_sources()
    cur.discard(name) if enabled else cur.add(name)
    db = ArchiveDB()
    db.set_meta("sources_disabled", ",".join(sorted(cur)))
    db.commit()
    _sources_cache["at"] = 0.0   # /api/config 소스 현황 캐시 무효화 → 즉시 반영
    return {"ok": True, "disabled": sorted(cur)}


@app.get("/api/session")
def api_session(id: str = Query(...), limit: int = 0):
    """한 세션의 모든 턴을 시간순으로 → 그 대화 전체 작업 내역. limit 0 = 전부.

    예전 기본값 2000 은 **오래된 쪽**을 남기고 최신 턴을 잘랐다(ORDER BY 시각 + LIMIT) - 사용자가
    가장 보려는 쪽이다. 게다가 db_count 가 잘린 개수라 /api/session/tail 의 COUNT(*) 와 영원히
    안 맞아, 2000턴이 넘는 활동 세션은 4초마다 세션 전체를 다시 받는 루프에 빠졌다.
    """
    db = ArchiveDB()
    # 접힌 턴(#128)도 빼지 않고 hidden 플래그만 달아 내려준다 — 화면에서 제자리에 '접힘' 한 줄로
    # 남겨 바로 펼칠 수 있게(검색·지도에서만 빠진다). 빼버리면 되돌릴 길이 멀어진다.
    rows = db.conn.execute(
        "SELECT t.id,t.timestamp,t.question,t.answer,t.actions,t.summary,t.tags,t.queued,"
        "       (h.turn_id IS NOT NULL) AS hidden "
        "FROM turns t LEFT JOIN hidden_turns h ON h.turn_id = t.id "
        "WHERE t.session_id=? ORDER BY t.timestamp, t.id LIMIT ?", (id, limit if limit > 0 else -1)
    ).fetchall()
    turns = []
    for r in rows:
        turns.append({
            "id": r["id"], "timestamp": r["timestamp"],
            "question": r["question"], "answer": r["answer"],
            "actions": [a.render() for a in _actions_from_json(r["actions"])],
            "summary": r["summary"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "hidden": bool(r["hidden"]),   # 접힘 — 화면에선 한 줄로, 검색·지도에선 제외
            # 작업 중 끼어든 질문(#246). 재색인 전 옛 행은 NULL — 모름은 거짓으로 취급한다.
            "queued": bool(r["queued"]),
        })
    info = db.session_source(id)
    if info is None:
        info = _unindexed_source(id, db)   # 색인 전 세션: 원문 전체가 '꼬리'다(커서가 없어 0부터 읽는다)
    source = info[0] if info else "claude-code"
    stored = info[1] if info else None
    project = (info[2] if info else "") or ""
    src_file = _find_source_file(source, id, stored)
    is_sub, parent = _subagent_info(stored)
    db_count = _db_count(db, id)    # 잘라 읽었어도 /api/session/tail 과 같은 기준이어야 한다
    live, live_skipped = _live_tail(db, source, stored, is_sub)
    at = {t["id"]: i for i, t in enumerate(turns)}
    for row in _live_rows(db, live):
        if row["id"] in at:
            turns[at[row["id"]]] = row      # 보류됐던 마지막 턴의 더 최신 판본
        else:
            turns.append(row)
    from . import raw_archive
    # 원문이 없어도(정리로 유실) 보존된 원본이 있으면 복구 가능 — 배경 대화는 애초에 재개가
    # 안 되니(위 resume_cmd) 복구도 의미 없어 제외.
    can_restore = (
        src_file is None and not is_sub and bool(_SID_RE.fullmatch(id))
        and raw_archive.has_mirror(source, id)
    )
    return {
        "session": id, "project": project, "count": len(turns), "turns": turns,
        "title": db.session_title(id),          # 사용자가 지은 제목(없으면 null)
        "source": source,
        # 배경 대화는 재개 명령이 없음(열기 차단) — 부모 세션 링크만 제공.
        "resume_cmd": "" if is_sub else (_resume_cmd_str(source, id) if _SID_RE.fullmatch(id) else ""),
        "source_file_exists": src_file is not None,
        "can_restore": can_restore,
        "subagent": is_sub,
        "parent": parent,
        "db_count": db_count,             # 색인된 턴 수 — 바뀌면 화면이 전체를 다시 받는다
        "active": _is_active(stored),     # 활동 중이면 화면이 /api/session/tail 을 주기적으로 부른다
        "live_skipped": live_skipped,     # 꼬리가 너무 커서 안 읽은 바이트 수(0 = 다 읽음)
    }


@app.get("/api/session/tail")
def api_session_tail(id: str = Query(...)):
    """활동 중인 세션의 색인 전 꼬리만. 몇 초마다 불리므로 세션 전체(수 MB)를 다시 보내지 않는다."""
    db = ArchiveDB()
    info = db.session_source(id) or _unindexed_source(id, db)
    if info is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    source, stored = info[0], info[1]
    is_sub, _parent = _subagent_info(stored)
    live, live_skipped = _live_tail(db, source, stored, is_sub)
    return {"turns": _live_rows(db, live), "db_count": _db_count(db, id),
            "active": _is_active(stored), "live_skipped": live_skipped}


def _turns_to_markdown(sid: str, project: str, turns: list[dict]) -> str:
    """세션 열람용 markdown 직렬화(#190) - 재개용 아님, 시간순 질문/행동/답변만."""
    lines = [f"# 세션 {sid}", ""]
    if project:
        lines += [f"- 프로젝트: `{project}`", ""]
    for t in turns:
        lines += [f"## {t['timestamp']}", ""]
        if t["question"]:
            lines += ["**질문**", "", t["question"], ""]
        if t["actions"]:
            lines += ["**행동**", *[f"- {a}" for a in t["actions"]], ""]
        if t["answer"]:
            lines += ["**답변**", "", t["answer"], ""]
    return "\n".join(lines)


@app.get("/api/session/export")
def api_session_export(id: str = Query(...)):
    """세션을 markdown으로 내보내기(#190) - 열람용, 재개 아님. 원문·미러 유무와 무관하게
    turns(아카이브)만 있으면 가능 - 30일 정리로 원문·미러 둘 다 없어진 레거시 세션의 유일한
    열람 수단."""
    if not _SID_RE.fullmatch(id):
        raise HTTPException(status_code=400, detail={"code": "invalid_session_id", "msg": "잘못된 세션 id"})
    data = api_session(id=id)   # 기존 turns/project 조회 로직 재사용(중복 없음)
    if not data["turns"]:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    # 접힌 턴은 내보내기에서도 뺀다(검색·지도와 같은 기준 — '접었다=결과물에서 빼둔다').
    turns = [t for t in data["turns"] if not t.get("hidden")]
    md = _turns_to_markdown(id, data["project"], turns)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                     headers={"Content-Disposition": f'attachment; filename="{id}.md"'})


@app.post("/api/hide")
def api_hide(payload: dict):
    """턴 또는 세션을 숨김(#128) - 비파괴, 원문·벡터는 그대로 두고 검색·세션목록·지도에서만 제외.
    payload: {turn_id} 또는 {session_id}(그 세션의 전 턴을 숨김)."""
    db = ArchiveDB()
    turn_id = (payload or {}).get("turn_id")
    session_id = (payload or {}).get("session_id")
    if turn_id:
        if db.get_turn(turn_id) is None:   # 없는 id를 조용히 숨김목록에 넣는 유령 행 방지
            raise HTTPException(status_code=404, detail={"code": "turn_not_found", "msg": "턴을 찾을 수 없음"})
        n = db.hide_turns([turn_id])
    elif session_id:
        n = db.hide_session(session_id)   # 하위 에이전트 세션까지 함께
        if n == 0 and db.session_source(session_id) is None:
            # 예전엔 없는(또는 색인 전) 세션도 {"ok": true, "hidden": 0} 이라 화면이 성공으로 알았다
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    else:
        raise HTTPException(status_code=400, detail={"code": "missing_target", "msg": "turn_id 또는 session_id 필요"})
    _graph3d_invalidate()   # 지도 캐시가 숨긴 턴을 계속 보여주지 않도록 즉시 폐기
    return {"ok": True, "hidden": n}


@app.post("/api/unhide")
def api_unhide(payload: dict):
    """숨김 해제(복원). payload: {turn_id} 또는 {session_id}."""
    db = ArchiveDB()
    turn_id = (payload or {}).get("turn_id")
    session_id = (payload or {}).get("session_id")
    if turn_id:
        db.unhide_turns([turn_id])
    elif session_id:
        db.unhide_session(session_id)
    else:
        raise HTTPException(status_code=400, detail={"code": "missing_target", "msg": "turn_id 또는 session_id 필요"})
    _graph3d_invalidate()   # 지도 캐시가 복원된 턴을 계속 빼놓지 않도록 즉시 폐기
    return {"ok": True}


# --- 폴더(#201): 사용자가 직접 만드는 수동 군집 -------------------------------
def _folder_id_arg(payload: dict, key: str) -> int:
    """payload 의 폴더 id 를 int 로. 잘못된 값(null·문자열)은 500 이 아니라 400으로 돌려준다."""
    try:
        return int((payload or {}).get(key, 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "invalid_folder_id", "msg": "잘못된 폴더 id"}) from None


def _folder_or_404(db, folder_id: int) -> dict:
    f = db.get_folder(folder_id)
    if f is None:
        raise HTTPException(status_code=404, detail={"code": "folder_not_found", "msg": "폴더를 찾을 수 없음"})
    return f


def _folder_target(payload: dict) -> tuple[str, str]:
    """payload 의 {turn_id} 또는 {session_id} → (kind, ref). 둘 다 없으면 400."""
    turn_id = (payload or {}).get("turn_id")
    session_id = (payload or {}).get("session_id")
    if turn_id:
        return "turn", str(turn_id)
    if session_id:
        return "session", str(session_id)
    raise HTTPException(status_code=400, detail={"code": "missing_target", "msg": "turn_id 또는 session_id 필요"})


@app.get("/api/folders")
def api_folders():
    """전체 폴더 목록(트리 구성은 프론트에서). 각 폴더의 직접 항목 수 포함."""
    return {"folders": ArchiveDB().list_folders()}


@app.post("/api/folders/create")
def api_folder_create(payload: dict):
    name = str((payload or {}).get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": "empty_name", "msg": "폴더 이름이 필요합니다"})
    db = ArchiveDB()
    parent_id = (payload or {}).get("parent_id")
    if parent_id is not None:
        _folder_or_404(db, _folder_id_arg(payload, "parent_id"))
    return {"ok": True, "id": db.create_folder(name, _folder_id_arg(payload, "parent_id") if parent_id is not None else None)}


@app.post("/api/folders/rename")
def api_folder_rename(payload: dict):
    name = str((payload or {}).get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": "empty_name", "msg": "폴더 이름이 필요합니다"})
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "id")
    _folder_or_404(db, fid)
    db.rename_folder(fid, name)
    return {"ok": True}


@app.post("/api/folders/move")
def api_folder_move(payload: dict):
    """폴더를 다른 폴더 밑으로(parent_id=null 이면 최상위). 자기 하위로는 못 옮긴다.
    before_id 를 주면 그 형제 '바로 앞'에 놓는다(순서까지 지정) — 없으면 맨 뒤."""
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "id")
    _folder_or_404(db, fid)
    parent_id = (payload or {}).get("parent_id")
    if parent_id is not None:
        _folder_or_404(db, _folder_id_arg(payload, "parent_id"))
    before_id = _folder_id_arg(payload, "before_id") if (payload or {}).get("before_id") is not None else None
    if not db.move_folder(fid, _folder_id_arg(payload, "parent_id") if parent_id is not None else None, before_id):
        raise HTTPException(status_code=400, detail={"code": "folder_cycle", "msg": "폴더를 자기 하위로 옮길 수 없습니다"})
    return {"ok": True}


@app.post("/api/folders/delete")
def api_folder_delete(payload: dict):
    """폴더와 하위 폴더를 삭제. 담긴 항목의 참조만 지우며 대화 원문은 그대로 남는다."""
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "id")
    _folder_or_404(db, fid)
    return {"ok": True, "deleted": db.delete_folder(fid)}


@app.post("/api/folders/add")
def api_folder_add(payload: dict):
    """폴더에 담기. payload: {folder_id, turn_id | session_id}."""
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "folder_id")
    _folder_or_404(db, fid)
    kind, ref = _folder_target(payload)
    if kind == "turn" and db.get_turn(ref) is None:   # 없는 턴을 담아 유령 항목이 생기지 않게
        raise HTTPException(status_code=404, detail={"code": "turn_not_found", "msg": "턴을 찾을 수 없음"})
    db.add_to_folder(fid, kind, ref)
    return {"ok": True}


@app.post("/api/folders/remove")
def api_folder_remove(payload: dict):
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "folder_id")
    _folder_or_404(db, fid)
    kind, ref = _folder_target(payload)
    # changed=0 은 '이미 없던 항목'이다. ok:true 만 주면 화면은 성공으로 보이는데 항목이
    # 그대로 남아 "X 버튼이 안 먹힌다"가 된다 → 호출부가 구분할 수 있게 내려준다.
    return {"ok": True, "changed": db.remove_from_folder(fid, kind, ref)}


@app.post("/api/folders/item/rename")
def api_folder_item_rename(payload: dict):
    """폴더 안에서만 쓰는 표시 이름(별칭). 원본 턴/세션 제목은 바뀌지 않는다.
    payload: {folder_id, turn_id|session_id, alias}. alias 가 비면 원래 제목으로 되돌린다."""
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "folder_id")
    _folder_or_404(db, fid)
    kind, ref = _folder_target(payload)
    alias = str((payload or {}).get("alias", "")).strip()
    db.set_item_alias(fid, kind, ref, alias or None)
    return {"ok": True}


@app.post("/api/folders/item/reorder")
def api_folder_item_reorder(payload: dict):
    """폴더 안 항목 순서 저장. payload: {folder_id, order: [{kind, ref}, …]} (보이는 순서 그대로)."""
    db = ArchiveDB()
    fid = _folder_id_arg(payload, "folder_id")
    _folder_or_404(db, fid)
    raw = (payload or {}).get("order")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail={"code": "invalid_order", "msg": "order 목록이 필요합니다"})
    order = []
    for it in raw:
        kind, ref = (it or {}).get("kind"), (it or {}).get("ref")
        if kind not in ("turn", "session") or not isinstance(ref, str) or not ref:
            raise HTTPException(status_code=400, detail={"code": "invalid_order", "msg": "잘못된 항목이 있습니다"})
        order.append((kind, ref))
    # count 는 '시도한 개수', changed 는 '실제 갱신된 개수'. 다르면 그 사이 항목이 바뀐 것이다.
    return {"ok": True, "count": len(order), "changed": db.reorder_folder(fid, order)}


@app.get("/api/folder")
def api_folder(id: int = Query(...)):
    """폴더 하나: 이름·상위 경로·하위 폴더·담긴 항목."""
    db = ArchiveDB()
    f = _folder_or_404(db, id)
    all_folders = db.list_folders()
    by_id = {x["id"]: x for x in all_folders}
    path, cur = [], f["parent_id"]           # 최상위까지의 경로(빵부스러기용)
    while cur is not None and cur in by_id:
        path.append({"id": by_id[cur]["id"], "name": by_id[cur]["name"]})
        cur = by_id[cur]["parent_id"]
    return {
        "folder": f,
        "path": list(reversed(path)),
        "children": [x for x in all_folders if x["parent_id"] == id],
        "items": db.folder_items(id),
        "turn_count": len(db.folder_turn_ids(id)),   # 하위까지 포함한 실제 턴 수(검색 범위)
    }


@app.get("/api/hidden")
def api_hidden(limit: int = 1):
    """접힘 화면(#128): 접은 세션 / 일부 접힌 세션의 채팅(세션별 묶음). limit=0 이면 개수만(좌측 배지)."""
    db = ArchiveDB()
    return db.folded_groups(with_items=limit != 0)


@app.post("/api/session/title")
def api_session_title(payload: dict):
    """세션 제목을 사용자가 직접 지정(빈 값이면 기본 제목으로 되돌림).
    원문 대화는 건드리지 않고 별도 테이블에만 기록한다 — 재색인·정제를 다시 돌려도 남는다."""
    sid = str((payload or {}).get("session_id", "")).strip()
    if not sid or not _SID_RE.fullmatch(sid):
        raise HTTPException(status_code=400, detail={"code": "invalid_session_id", "msg": "잘못된 세션 id"})
    db = ArchiveDB()
    # 제목은 별도 테이블이라 색인 전 세션에도 붙일 수 있다(색인되면 그대로 이어진다)
    if (db.conn.execute("SELECT 1 FROM turns WHERE session_id=? LIMIT 1", (sid,)).fetchone() is None
            and _unindexed_source(sid, db) is None):
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    db.set_session_title(sid, str((payload or {}).get("title", "")).strip() or None)
    return {"ok": True}


@app.get("/api/sessions")
def api_sessions(limit: int = 500):
    """세션 목록(최근순): id·턴수·시작/끝 시각·대표 헤드라인(첫 정제/질문)."""
    db = ArchiveDB()
    # 세션별 집계(턴수·시작/끝)를 GROUP BY 로 먼저 줄이고, 고른 세션에서만 대표 턴을 찾는다(N+1 없음).
    # 예전엔 turns 전체에 윈도우 함수를 걸고 나서야 rn=1 로 걸러, LIMIT 이 스캔을 못 줄였다(정렬용 임시
    # B-트리 두 번). 실측: 이 기기 4.2천 턴 83ms → 6ms, 합성 10만 턴 904ms → 140ms, 결과 동일.
    # 대표 턴은 folder_items 와 같은 방식 - 스칼라 서브쿼리로 id 하나만 고르고 본문은 바깥에서 조인한다.
    # 접힌 턴(#128)도 집계에 남긴다 - hidden_count == count 인 세션(전부 접힘)은 화면이 목록에서 뺀다.
    # 대표 헤드라인은 '접히지 않은' 턴에서 먼저 고른다 — 노이즈라 접은 첫 턴이 계속 세션 제목으로
    # 뜨면 접은 의미가 없다. 전부 접힌 세션만 접힌 턴에서 고르게 된다(그 외 대안 없음).
    # 마지막 시각이 같은 세션이 한도 경계에 걸려도 결과가 흔들리지 않게 session_id 로 한 번 더 정렬.
    rows = db.conn.execute(
        "SELECT g.session_id, t.summary, t.question, t.source, t.source_file,"
        "       g.n, g.n_hidden, g.started, g.ended FROM ("
        "  SELECT t.session_id, COUNT(*) AS n, SUM(h.turn_id IS NOT NULL) AS n_hidden,"
        "         MIN(t.timestamp) AS started, MAX(t.timestamp) AS ended"
        "  FROM turns t LEFT JOIN hidden_turns h ON h.turn_id = t.id"
        "  GROUP BY t.session_id ORDER BY ended DESC, t.session_id LIMIT ?"
        ") g JOIN turns t ON t.id = ("
        "  SELECT t2.id FROM turns t2 LEFT JOIN hidden_turns h2 ON h2.turn_id = t2.id"
        "  WHERE t2.session_id = g.session_id"
        "  ORDER BY (h2.turn_id IS NOT NULL), t2.timestamp, t2.id LIMIT 1)"
        " ORDER BY g.ended DESC, g.session_id", (limit,)
    ).fetchall()
    # 사용자가 지은 제목은 한 번에 읽어와 덮어쓴다(세션마다 조회하면 N+1).
    # title='' 은 '지정을 지웠다'는 기록이다(#233 소프트 삭제) — 제목으로 내보내지 않는다.
    titles = {t["session_id"]: t["title"]
              for t in db.conn.execute("SELECT session_id, title FROM session_titles WHERE title<>''")}
    out = []
    for r in rows:
        is_sub, parent = _subagent_info(r["source_file"])
        out.append({
            "session": r["session_id"], "count": r["n"],
            "hidden_count": r["n_hidden"] or 0,   # == count 면 세션 전체가 접힌 상태
            "started": r["started"], "ended": r["ended"],
            "headline": titles.get(r["session_id"]) or r["summary"] or r["question"] or "",
            "custom_title": titles.get(r["session_id"]),   # 지정 여부 표시용
            "source": r["source"] or "claude-code",
            "subagent": is_sub,      # 배경(서브에이전트) 대화 여부
            "parent": parent,        # 파생된 부모 세션 id(있으면)
        })
    try:
        known = {r["session"] for r in out}
        extra = [r for r in _unindexed_sessions(db) if r["session"] not in known]
        for r in extra:   # 색인 전에 지은 제목도 보여준다
            if titles.get(r["session"]):
                r["headline"] = r["custom_title"] = titles[r["session"]]
    except Exception as e:  # noqa: BLE001 — 덤이다. 실패해도 색인된 세션 목록은 그대로
        logging.getLogger(__name__).warning("색인 전 세션 조회 실패: %s", e)
        extra = []
    if extra:
        out = sorted(out + extra, key=lambda r: r["ended"] or "", reverse=True)[:limit]
    return {"sessions": out}


# 한 번도 색인 안 된 로그(커서 없음)도 세션 목록에 '색인 전'으로 띄운다. 색인은 10분 주기라
# 새로 시작한 세션은 그동안 목록에 없었다. 머리만 읽고(_UNINDEXED_HEAD_BYTES) 파일 크기·수정
# 시각이 같으면 다시 읽지 않는다 - 목록은 자주 불린다. path -> ((size, mtime), 줄 | None)
_UNINDEXED_HEAD_BYTES = 1024 * 1024
_unindexed_cache: dict[str, tuple[tuple[int, float], dict | None]] = {}


def _unindexed_row(adapter, path: str, size: int, mtime: float) -> dict | None:
    objs = []
    for obj, end in adapter.read_records(path, 0):
        objs.append(obj)
        if end > _UNINDEXED_HEAD_BYTES:
            break
    turns = adapter.extract_turns(objs)
    if not turns or not _SID_RE.fullmatch(turns[0].session_id):
        return None
    is_sub, parent = _subagent_info(path)
    from datetime import datetime, timezone
    first = turns[0]
    return {
        "session": first.session_id, "count": len(turns), "hidden_count": 0,
        "started": first.timestamp,
        # 진행 중인 세션이라 마지막 턴 시각보다 파일 수정 시각이 정확하다(정렬용)
        "ended": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "headline": first.question or "", "custom_title": None,
        "source": getattr(adapter, "source_name", adapter.name),
        "subagent": is_sub, "parent": parent,
        "unindexed": True,       # 화면: '색인 전' 배지, 접기·폴더 담기 없음
        "_path": path,           # api_session 이 원문을 찾는 데 쓴다(_ 로 시작하는 칸은 응답에서 뺀다)
        "_project": first.project or "",
    }


def _unindexed_sessions(db: ArchiveDB) -> list[dict]:
    from .indexer import iter_all_cached
    have = {r[0] for r in db.conn.execute("SELECT file_path FROM cursors")}
    files = iter_all_cached()
    # 색인됐거나 사라진 파일의 캐시는 버린다(세션마다 새 파일이 생겨 그대로 두면 계속 쌓인다)
    live = {str(p) for _a, p in files} - have
    for gone in [k for k in _unindexed_cache if k not in live]:
        _unindexed_cache.pop(gone, None)
    out = []
    for adapter, p in files:
        sp = str(p)
        if sp in have:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        key = (st.st_size, st.st_mtime)
        hit = _unindexed_cache.get(sp)
        if hit is None or hit[0] != key:
            try:
                row = _unindexed_row(adapter, sp, st.st_size, st.st_mtime)
            except Exception as e:  # noqa: BLE001 — 파일 하나가 깨져도 나머지는 보인다
                logging.getLogger(__name__).warning("색인 전 로그 읽기 실패 %s: %s", sp, e)
                row = None
            hit = (key, row)
            _unindexed_cache[sp] = hit
        if hit[1]:
            out.append({k: v for k, v in hit[1].items() if not k.startswith("_")})
    return out


def _unindexed_source(sid: str, db: ArchiveDB | None = None) -> tuple[str, str, str] | None:
    """색인 전 세션의 (출처, 원문 경로, 프로젝트). 캐시에 없으면(백엔드를 다시 켠 직후 등) 한 번 훑어
    채운다 - 안 그러면 열어 둔 색인 전 세션이 '0턴'으로 굳었다."""
    def find():
        for _key, row in list(_unindexed_cache.values()):
            if row and row["session"] == sid:
                return row["source"], row["_path"], row["_project"]
        return None
    hit = find()
    if hit is None and db is not None and _SID_RE.fullmatch(sid):
        _unindexed_sessions(db)
        hit = find()
    return hit


# 세션 id 화이트리스트. 선두 '-' 금지 → 재개 CLI(claude/codex)로의 인자(플래그) 주입 차단.
# (session_id 는 로그 파일 내용에서 오므로, 심어진 로그가 "--flag" 같은 값을 넣어도 거부된다.)
_SID_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


def _resume_argv(source: str, sid: str) -> list[str]:
    """출처별 세션 재개 명령 argv. sid는 호출 전 _SID_RE 로 검증됨."""
    if source == "codex":
        return ["codex", "resume", sid]
    return ["claude", "--resume", sid]   # claude-code(기본)


def _resume_cmd_str(source: str, sid: str) -> str:
    return " ".join(_resume_argv(source, sid))


# 실시간 표시(#249): 아직 색인 안 된 로그 꼬리를 그 자리에서 읽어 채팅에 붙인다.
# 색인은 10분 주기에 진행 중인 턴을 2분 보류하므로, 그것만으로는 작업 중인 세션이 안 보인다.
# 꼬리는 평소 수십~수백 KB(실측 1~23ms)다. 색인이 크게 밀리면 커지는데, 그땐 읽지 않고
# 크기만 알린다 — 몇 초마다 부르는 경로라 수백 MB 를 매번 파싱하면(실측 4~5초) 화면이 멈춘다.
_LIVE_MAX_BYTES = 5 * 1024 * 1024
_ACTIVE_SECS = 600   # 원문이 이 안에 바뀌었으면 '활동 중' → 화면이 꼬리를 주기적으로 다시 읽는다


def _live_tail(db: ArchiveDB, source: str, stored: str | None, is_sub: bool) -> tuple[list, int]:
    """색인 전 꼬리의 턴들(DB 에 쓰지 않는다). 반환 (턴들, 건너뛴 바이트 수).

    커서 키는 turns.source_file 과 같은 문자열이다(색인기가 같은 값을 쓴다). 보류된 마지막 턴이
    있으면 그 시작부터 읽으므로, DB 에 있던 그 턴의 더 최신 판본도 함께 나온다.
    """
    if not stored:
        return [], 0
    try:
        size = os.path.getsize(stored)
    except OSError:
        return [], 0
    offset, _, _ = db.get_cursor(stored)
    hold = db.get_hold(stored)
    start = hold if hold is not None else offset
    if start >= size:
        return [], 0
    if size - start > _LIVE_MAX_BYTES:
        return [], size - start
    from .sources import ADAPTERS
    adapter = ADAPTERS["codex" if source == "codex" else "subagent" if is_sub else "claude-code"]
    try:
        objs = [o for o, _end in adapter.read_records(stored, start)]
        return adapter.extract_turns(objs), 0
    except Exception as e:  # noqa: BLE001 — 실시간 표시는 덤이다. 실패해도 색인된 대화는 그대로 보인다
        logging.getLogger(__name__).warning("실시간 꼬리 읽기 실패 %s: %s", stored, e)
        return [], 0


def _live_rows(db: ArchiveDB, live: list) -> list[dict]:
    """꼬리 턴을 api_session 과 같은 모양으로. DB 에 이미 있는 턴(보류됐던 마지막 턴)은
    정제·접힘을 이어받고 live 가 아니다 — 접기·폴더 담기가 그대로 된다."""
    if not live:
        return []
    ids = [t.id for t in live]
    known = {r["id"]: r for r in db.conn.execute(
        f"SELECT t.id, t.summary, t.tags, (h.turn_id IS NOT NULL) AS hidden FROM turns t "
        f"LEFT JOIN hidden_turns h ON h.turn_id = t.id WHERE t.id IN ({','.join('?' * len(ids))})", ids)}
    rows = []
    for t in live:
        k = known.get(t.id)
        rows.append({
            "id": t.id, "timestamp": t.timestamp, "question": t.question, "answer": t.answer,
            "actions": [a.render() for a in t.actions],
            "summary": k["summary"] if k else None,
            "tags": json.loads(k["tags"]) if k and k["tags"] else [],
            "hidden": bool(k["hidden"]) if k else False,
            "queued": t.queued,
            "live": k is None,   # 아직 DB 에 없음 = 검색·지도에 안 나오고 접기·폴더 담기 불가
        })
    return rows


def _db_count(db: ArchiveDB, sid: str) -> int:
    """색인된 턴 수. /api/session 과 /api/session/tail 이 **같은 함수**로 센다 - 화면은 두 값이 다르면
    색인이 진행됐다고 보고 전체를 다시 받는다. 기준이 갈리면 끝나지 않는 재요청 루프가 된다."""
    return db.conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)).fetchone()[0]


def _is_active(stored: str | None) -> bool:
    try:
        return bool(stored) and (time.time() - os.path.getmtime(stored)) < _ACTIVE_SECS
    except OSError:
        return False


def _find_source_file(source: str, sid: str, stored: str | None) -> Path | None:
    """세션 원문 로그 파일 경로. 저장된 경로 우선, 없으면 출처별 탐색. 못 찾으면 None."""
    if not _SID_RE.fullmatch(sid):
        return None
    if stored:
        p = Path(stored)
        if p.is_file():
            return p
    from . import config as C
    if source == "codex":
        root = Path(C.CODEX_SESSIONS_DIR)
        if root.exists():
            for p in root.rglob(f"rollout-*-{sid}.jsonl"):
                if p.is_file():
                    return p
        return None
    from . import session_sync
    return session_sync.find_session_file(sid)   # claude: PROJECTS_DIR/**/<sid>.jsonl


def _subagent_info(stored: str | None) -> tuple[bool, str | None]:
    """세션이 배경(서브에이전트) 대화인지 + 부모 세션 id 를 source_file 경로로 파생.

    경로 형태: <projects>/<PARENT>/subagents/agent-<agentId>.jsonl
    → subagents 폴더 안이면 배경 대화, 그 상위 폴더명이 부모 세션 id.
    """
    if not stored:
        return False, None
    p = Path(stored)
    if p.parent.name != "subagents":
        return False, None
    parent = p.parent.parent.name or None
    return True, parent


def _safe_resume_cwd(project: str | None) -> str | None:
    """세션 로그의 cwd(project)는 신뢰 불가(동기화된 로그에 임의 값이 심길 수 있음).
    UNC/네트워크/디바이스 경로를 거부하고 실재하는 로컬 디렉터리일 때만 반환(아니면 None → 홈에서 재개).
    UNC 경로를 stat 하면 Windows가 SMB 로 해석해 강제 NTLM 인증(자격증명 유출)을 유발할 수 있어
    stat 전에 걸러낸다."""
    if not project:
        return None
    cwd = project.strip()
    if not cwd:
        return None
    if cwd.replace("/", "\\").startswith("\\\\"):   # UNC(\\server\share)·디바이스(\\?\) 경로 거부
        return None
    try:
        if not Path(cwd).is_dir():   # UNC 를 거른 뒤에만 stat
            return None
    except OSError:
        return None
    return cwd


@app.post("/api/resume")
def api_resume(session: str = Query(...), force: bool = False):
    """이 PC에서 새 터미널을 열어 그 세션의 작업 폴더에서 출처별 재개 명령 실행
    (claude-code=`claude --resume`, codex=`codex resume`). 로컬 전용. id는 화이트리스트+DB 검증.

    원문 로그가 없으면(삭제·이동) 실행하지 않고 missing 반환.
    활성 가드(M3): 세션이 최근 수정됐으면(다른 기기 진행 가능) force=false일 때 경고만 반환."""
    sid = session.strip()
    if not _SID_RE.fullmatch(sid):
        raise HTTPException(status_code=400, detail={"code": "invalid_session_id", "msg": "잘못된 세션 id"})
    db = ArchiveDB()
    info = db.session_source(sid) or _unindexed_source(sid, db)   # 색인 전 새 세션도 재개된다
    if info is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    source, stored, project = info

    # 배경(서브에이전트) 대화는 agentId 로 재개할 수 없음 → 열기 차단(검색·조회만 가능).
    is_sub, _parent = _subagent_info(stored)
    if is_sub:
        return {"ok": False, "subagent": True, "code": "resume_subagent",
                "warning": "배경 에이전트 대화는 직접 열 수 없어요 (검색·조회만 가능)."}

    cwd = _safe_resume_cwd(project)   # 로그의 cwd 는 신뢰 불가 → UNC/네트워크 경로 거부 + 실재 폴더만

    # 원문 존재 확인: 로그 파일이 없어졌으면 재개 불가(세션을 열 수 없음).
    src_file = _find_source_file(source, sid, stored)
    if src_file is None:
        return {"ok": False, "missing": True, "code": "resume_missing_log",
                "warning": "원문 로그 파일이 없어 세션을 열 수 없어요 (삭제·이동됐을 수 있어요)."}

    # 활성 가드: 최근 수정된 세션이면 이중 재개(분기) 위험을 경고(실행은 보류).
    from . import session_sync
    act = session_sync.session_activity(src_file)
    if act.active and not force:
        secs = int(act.seconds_since or 0)
        return {
            "ok": False, "active": True, "seconds_since": secs, "code": "resume_active",
            "warning": f"이 세션이 약 {secs}초 전에 수정됐어요 — 다른 기기에서 진행 중이면 "
                       "지금 재개 시 분기(fork)될 수 있어요.",
        }

    # 재개 직전, 그 폴더의 CLI '신뢰' 프롬프트를 미리 통과시켜 원클릭 재개(#181). best-effort:
    # 실패하면 조용히 넘어가고 CLI 가 원래대로 프롬프트를 띄운다. cwd 는 이미 _safe_resume_cwd 통과.
    if cwd:
        from . import resume_trust
        resume_trust.pretrust(source, cwd)

    try:
        _launch_resume(sid, cwd, source)
    except Exception as e:               # 실행 실패를 사용자에게 그대로 전달
        raise HTTPException(status_code=500, detail={"code": "resume_launch_failed", "msg": f"터미널 실행 실패: {e}", "detail": str(e)})
    return {"ok": True, "cwd": cwd, "source": source}


@app.post("/api/session/restore")
def api_session_restore(session: str = Query(...)):
    """원문 로그가 사라진(정리됨) 세션을, 보존해둔 원본(#163 P1)으로 되살린다.

    되살리면 실제 파일이 생기므로 이후 /api/resume 이 정상 동작(재개 가능). 이미 원문이
    있으면(경합 등) 건드리지 않고 그대로 성공 취급. 서브에이전트(배경 대화)는 애초에 재개가
    안 되니 복구 대상에서 제외."""
    sid = session.strip()
    if not _SID_RE.fullmatch(sid):
        raise HTTPException(status_code=400, detail={"code": "invalid_session_id", "msg": "잘못된 세션 id"})
    db = ArchiveDB()
    info = db.session_source(sid)
    if info is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "msg": "세션을 찾을 수 없음"})
    source, stored, _project = info

    is_sub, _parent = _subagent_info(stored)
    if is_sub:
        return {"ok": False, "subagent": True, "code": "restore_subagent",
                "warning": "배경 에이전트 대화는 복구 대상이 아니에요."}

    if _find_source_file(source, sid, stored) is not None:
        return {"ok": True, "already_exists": True}   # 이미 원문 있음 — no-op 성공

    from . import raw_archive
    try:
        got = raw_archive.restore(source, sid)
    except Exception as e:  # noqa: BLE001 — 파일시스템 오류 등을 사용자에게 그대로 전달
        raise HTTPException(status_code=500, detail={"code": "restore_failed", "msg": f"복구 실패: {e}", "detail": str(e)})
    if got is None:
        # 보존본 파일이 있는데 None 이면 '없음'이 아니라 '읽을 수 없음'이다. 뭉뚱그려
        # "원본이 없어요"라고 하면 사용자가 살아있을지도 모르는 .gz 를 지운다.
        if raw_archive.has_mirror(source, sid):
            return {"ok": False, "code": "restore_corrupt",
                    "warning": "보존본이 손상돼 읽을 수 없어요. 파일은 지우지 마세요 - 나중에 복구 방법이 생길 수 있어요."}
        return {"ok": False, "missing": True, "code": "restore_no_mirror",
                "warning": "보존된 원본이 없어 복구할 수 없어요(이 기능 이전에 유실된 세션일 수 있어요)."}
    target, intact = got
    # 보존본이 손상돼 일부만 복구된 경우를 '완료'로 뭉개지 않는다 — 잘린 대화를 그대로
    # 재개하면 사용자는 뒷부분이 왜 없는지 알 수 없다.
    if not intact:
        return {"ok": True, "path": str(target), "partial": True, "code": "restore_partial",
                "warning": "보존본 일부가 손상돼 앞부분만 복구했어요. 뒷부분 대화는 남아있지 않아요."}
    return {"ok": True, "path": str(target)}


def _resume_env() -> dict:
    """재개된 claude를 '평범한 터미널에서 새로 켠 것'과 동일하게 만드는 환경.
    이 서버가 Claude Code 세션 안에서 실행되면 부모가 심은 마커들을 상속하는데,
    그게 자식 claude로 전파되면:
      - CLAUDE_CODE_CHILD_SESSION → '중첩 자식'으로 보고 트랜스크립트 저장을 끔
      - NO_COLOR=1 → 모든 색 출력이 꺼져 흰 텍스트만 나옴
      - CLAUDECODE/CLAUDE_CODE_ENTRYPOINT → 중첩 실행 컨텍스트로 오인
    → 이 마커들을 제거하고 저장을 강제해 독립 세션처럼 동작하게 한다."""
    env = os.environ.copy()
    for k in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "NO_COLOR"):
        env.pop(k, None)
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    # cmd.exe 는 PATH 보다 현재 폴더를 먼저 본다. 재개 창의 cwd 는 세션 로그에 적힌 프로젝트 폴더라, 거기에
    # (예: git pull 로 들어온) claude.bat 이 있으면 '재개'를 누르는 순간 그게 실행된다.
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _launch_resume(sid: str, cwd: str | None, source: str = "claude-code") -> None:
    """플랫폼별로 새 터미널 창을 열어 출처별 재개 명령 실행(종료 후에도 창 유지)."""
    # 방어심층: 호출자(api_resume)가 이미 검증하지만, 이 함수 단독 오용에도 안전하도록 재검증.
    if not _SID_RE.fullmatch(sid):
        raise ValueError(f"안전하지 않은 세션 id: {sid!r}")
    argv = _resume_argv(source, sid)   # 고정 토큰 + 검증된 sid(주입 불가)
    plat = _sys.platform
    env = _resume_env()
    if plat == "win32":
        # 새 콘솔 창에서 실행 + 창 유지(/k).
        subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/k", *argv], cwd=cwd, env=env)
        return
    # mac/linux: 새 탭의 셸이 런처가 아니라 (싱글턴) 터미널 서버에서 spawn될 수 있어 env= 가
    # 안 먹을 수 있음 → 마커 정리를 inner 셸 안에서 직접 수행(확실). cwd 없으면 홈으로(~ 리터럴 금지).
    target = shlex.quote(cwd or str(Path.home()))
    prefix = ("unset CLAUDE_CODE_CHILD_SESSION CLAUDECODE CLAUDE_CODE_ENTRYPOINT NO_COLOR; "
              "export CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; ")
    if plat == "darwin":
        inner = f'{prefix}cd {target} && {shlex.join(argv)}'
        subprocess.Popen(["osascript", "-e", f"tell application \"Terminal\" to do script {json.dumps(inner)}"], env=env)
        return
    # linux: 흔한 터미널 emulator 순차 시도
    inner = f'{prefix}cd {target} && {shlex.join(argv)}; exec bash'
    for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
        try:
            subprocess.Popen(term + ["bash", "-lc", inner], env=env)
            return
        except FileNotFoundError:
            continue
    raise RuntimeError("사용 가능한 터미널을 찾지 못했습니다")


# ── 세션 동기화 감시(인프로세스, M4) ────────────────────────────────
# Syncthing 충돌 사본을 주기적으로 해소하는 경량 스레드. 색인은 하지 않음
# (기존 인덱싱 스케줄러에 위임 → 임베더 중복 로드/이중 색인 방지).
_sync: dict = {"thread": None, "stop": None, "running": False,
               "interval": 10.0, "resolved_total": 0, "last_error": None}


def _sync_loop() -> None:
    from . import session_sync
    st = _sync
    while st["stop"] is not None and not st["stop"].is_set():
        try:
            res = session_sync.sync_tick(min_scan_secs=60)   # 충돌 해소만(색인 없음), 폴더 훑기는 1분에 한 번
            st["resolved_total"] += len(res.outcomes)
            st["last_error"] = None
        except Exception as ex:                     # 한 번의 오류로 스레드가 죽지 않게
            st["last_error"] = str(ex)
        st["stop"].wait(st["interval"])


def _persist_sync_meta_bg(enabled: bool) -> None:
    """설정 저장(sync_enabled)을 백그라운드로 — busy_timeout(60s) 동안 DB 쓰기 락(자동 색인 등)에
    토글 요청이 막혀 스피너가 안 멈추던 문제 방지. 저장은 best-effort."""
    def _w() -> None:
        with contextlib.suppress(Exception):
            db = ArchiveDB()
            db.set_meta("sync_enabled", "1" if enabled else "0")
            if enabled:
                db.set_meta("sync_interval", str(_sync["interval"]))
            db.commit()
    threading.Thread(target=_w, daemon=True).start()


def _sync_start(interval: float | None = None, *, persist: bool = True) -> None:
    if _sync["running"]:
        if interval:
            _sync["interval"] = float(interval)
        return
    if interval:
        _sync["interval"] = float(interval)
    _sync["stop"] = threading.Event()
    _sync["running"] = True
    _sync["last_error"] = None
    t = threading.Thread(target=_sync_loop, daemon=True)
    _sync["thread"] = t
    t.start()
    if persist:
        _persist_sync_meta_bg(True)


def _sync_stop(*, persist: bool = True) -> None:
    if _sync["stop"] is not None:
        _sync["stop"].set()
    _sync["running"] = False
    if persist:
        _persist_sync_meta_bg(False)


def _sync_status() -> dict:
    from . import config as C
    return {
        "running": _sync["running"],
        "interval": _sync["interval"],
        "resolved_total": _sync["resolved_total"],
        "last_error": _sync["last_error"],
        "projects_dir": str(C.PROJECTS_DIR),
    }


@app.get("/api/sync/status")
def api_sync_status():
    return _sync_status()


@app.post("/api/sync/toggle")
def api_sync_toggle(payload: dict):
    """세션 동기화 감시 스레드 on/off. body: {enabled: bool, interval?: number}."""
    enabled = bool(payload.get("enabled"))
    interval = payload.get("interval")
    if enabled:
        _sync_start(float(interval) if interval else None)
    else:
        _sync_stop()
    return _sync_status()


# ── 임베디드 Syncthing (E3): 관리형 인스턴스 + 페어링 엔드포인트 ──────
# 지연 실행: 사용자가 "기기 연결"을 켤 때만 spawn(단일 기기는 오버헤드 0).
# running/starting = 기계 판독용 상태(프론트가 이걸 봄), phase = 사람용/로그 텍스트.
_st_state: dict = {"running": False, "starting": False, "phase": "중지", "my_id": None, "last_error": None}
_st: dict = {"inst": None}
_st_lock = threading.Lock()   # 웹 스레드 ↔ 백그라운드 스레드 상태 변경 상호배제
_ST_DEVID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")   # Syncthing Device ID 형식


def _st_start_bg(persist: bool = True) -> None:
    with _st_lock:   # check-and-set 원자화(이중 시작 방지)
        if _st_state["running"] or _st_state["starting"]:
            return
        _st_state.update(starting=True, phase="시작 중", last_error=None)

    def _w() -> None:
        try:
            from . import syncthing
            inst = _st.get("inst") or syncthing.Syncthing()
            _st["inst"] = inst
            inst.start(log_fn=lambda m: _st_state.__setitem__("phase", m))
            if inst.wait_ready():
                # rename(chatmem→vestige) 잔재 폴더 정리 — 같은 경로 중복 폴더가 있으면 동기화가
                # 0%에서 막히므로, REST 준비된 직후 새 폴더로 이관 후 옛 폴더 제거(자가복구·신규는 no-op).
                from . import config as C
                # 실패해도 기기 연결은 계속하되 흔적은 남긴다 - 예전엔 조용히 삼켜, Codex 세션만 동기화가
                # 안 되는 이유를 알 수 없었다. 기존 페어에도 codex 원본 폴더를 자가복구로 추가(#153).
                for fix in (lambda: inst.migrate_legacy_folder(C.PROJECTS_DIR),
                            lambda: inst.ensure_codex_folder(C.CODEX_SESSIONS_DIR)):
                    try:
                        fix()
                    except Exception as ex:  # noqa: BLE001
                        logging.getLogger(__name__).warning("동기화 폴더 자가복구 실패: %s", ex)
                        _st_state["last_error"] = f"동기화 폴더 정리 실패: {ex}"
                with _st_lock:
                    _st_state.update(running=True, starting=False, phase="실행 중", my_id=inst.device_id())
                _sync_start(persist=False)   # 기기 연결이 켜지면 충돌 정리 워커도 자동 시작(별도 토글 없음)
                if persist:
                    with contextlib.suppress(Exception):
                        db = ArchiveDB(); db.set_meta("syncthing_enabled", "1"); db.commit()
            else:
                # 실패 원인을 syncthing.log에서 뽑아 사용자에게 그대로 전달(락 충돌 등).
                reason = None
                with contextlib.suppress(Exception):
                    reason = syncthing.log_error(inst.home)
                with _st_lock:
                    _st_state.update(running=False, starting=False, phase="시작 실패",
                                     last_error=reason or "Syncthing이 준비되지 않음")
        except Exception as ex:  # noqa: BLE001
            with _st_lock:
                _st_state.update(running=False, starting=False, phase="오류", last_error=str(ex))

    threading.Thread(target=_w, daemon=True).start()


def _st_stop(persist: bool = True) -> None:
    _sync_stop(persist=False)   # 기기 연결을 끄면 충돌 정리 워커도 함께 정지
    inst = _st.get("inst")
    if inst is not None:
        with contextlib.suppress(Exception):
            inst.stop()
    with _st_lock:
        _st_state.update(running=False, starting=False, phase="중지", my_id=None)
    if persist:
        with contextlib.suppress(Exception):
            db = ArchiveDB(); db.set_meta("syncthing_enabled", "0"); db.commit()


# 기기 연결 요약 캐시. 상태바가 1초마다 부르는데 pair_summary 는 로컬 Syncthing REST 를 여러 번
# 순차로 부른다(상대 1대·폴더 2개면 약 7번). 다른 폴링 경로는 캐시+락이 있는데 여기만 없어서,
# Syncthing 이 대용량 스캔·동기화로 느릴 때 1초마다 새 요청이 겹쳐 쌓였다(#245 와 같은 모양).
_PAIR_TTL = 2.0
_pair_cache: dict = {"at": 0.0, "v": {}}
_pair_lock = threading.Lock()


def _pair_summary_cached(inst) -> dict:
    now = time.time()
    if now - _pair_cache["at"] < _PAIR_TTL:
        return _pair_cache["v"]
    if not _pair_lock.acquire(blocking=False):
        return _pair_cache["v"]   # 다른 요청이 갱신 중 - 겹쳐 부르지 말고 직전 값으로 답한다
    try:
        with contextlib.suppress(Exception):   # 실패하면 직전 값(시각은 안 올려 다음 요청이 다시 시도)
            _pair_cache.update(at=now, v=inst.pair_summary())
        return _pair_cache["v"]
    finally:
        _pair_lock.release()


@app.get("/api/syncthing/status")
def api_syncthing_status():
    out = dict(_st_state)
    inst = _st.get("inst")
    if _st_state["running"] and inst is not None:
        out.update(_pair_summary_cached(inst))
    return out


@app.post("/api/syncthing/start")
def api_syncthing_start():
    _st_start_bg()
    return {"ok": True, "phase": _st_state["phase"]}


@app.post("/api/syncthing/stop")
def api_syncthing_stop():
    _st_stop()
    return {"ok": True}


@app.post("/api/syncthing/pair")
def api_syncthing_pair(payload: dict):
    """상대 Device ID를 추가 + ~/.claude/projects 공유. body: {device_id, name?}."""
    from . import config as C
    inst = _st.get("inst")
    if not _st_state["running"] or inst is None:
        return {"ok": False, "error": "먼저 '기기 연결'을 시작하세요", "code": "sync_not_started"}
    did = str((payload or {}).get("device_id", "")).strip().upper().replace(" ", "")
    if not _ST_DEVID_RE.fullmatch(did):
        return {"ok": False, "error": "Device ID 형식이 올바르지 않아요(예: XXXXXXX-XXXXXXX-… 8묶음)", "code": "device_id_invalid"}
    if did == _st_state.get("my_id"):
        return {"ok": False, "error": "내 기기 ID예요 — 상대 기기의 ID를 넣어주세요", "code": "device_id_self"}
    try:
        name = str((payload or {}).get("name", "")).strip()
        inst.add_device(did, name)
        inst.share_projects(C.PROJECTS_DIR, [did])
        # 같은 상대에게 Codex rollout 원본 폴더도 공유(#153) — projects 상대 집합을 미러링.
        with contextlib.suppress(Exception):
            inst.ensure_codex_folder(C.CODEX_SESSIONS_DIR)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"연결 실패: {e}", "code": "pair_failed", "detail": str(e)}


@app.post("/api/syncthing/unpair")
def api_syncthing_unpair(payload: dict):
    """연결된 기기 해제. body: {device_id}. 리셋 등으로 유령이 된 페어링을 뗄 때 사용."""
    from . import config as C
    inst = _st.get("inst")
    if not _st_state["running"] or inst is None:
        return {"ok": False, "error": "먼저 '기기 연결'을 시작하세요", "code": "sync_not_started"}
    did = str((payload or {}).get("device_id", "")).strip().upper().replace(" ", "")
    if not _ST_DEVID_RE.fullmatch(did):
        return {"ok": False, "error": "Device ID 형식이 올바르지 않아요", "code": "device_id_invalid"}
    try:
        if inst.remove_device(did, C.PROJECTS_DIR):
            return {"ok": True}
        return {"ok": False, "error": "기기 해제 실패 — 잠시 후 다시 시도", "code": "unpair_failed"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"기기 해제 실패: {e}", "code": "unpair_failed", "detail": str(e)}


@app.get("/api/config")
def api_config():
    """현재 유효 설정(키 값은 존재 여부만). 설정 화면 표시용."""
    import os

    from . import config as C
    from . import parser as _parser
    from . import raw_archive
    try:
        from .enrich import resolve_claude_bin
        _claude = resolve_claude_bin()
    except Exception:  # noqa: BLE001 — 설정 화면이 죽지 않게(해석 실패=미발견 취급)
        _claude = None
    return {
        "enrich_backend": C.ENRICH_BACKEND,
        "models": {
            "anthropic": C.ENRICH_API_MODEL, "openai": C.ENRICH_OPENAI_MODEL,
            "gemini": C.ENRICH_GEMINI_MODEL, "ollama": C.ENRICH_OLLAMA_MODEL,
            "claude": C.ENRICH_CLI_MODEL,
        },
        "ollama_url": C.ENRICH_OLLAMA_URL,
        "enrich_time": C.ENRICH_TIME,
        "index_interval": C.INDEX_INTERVAL_MIN,
        "index_mode": getattr(C, "INDEX_MODE", "interval"),
        "index_time": getattr(C, "INDEX_TIME", "03:00"),
        "embed_model": C.EMBED_MODEL,
        "raw_archive_max_mb": C.RAW_ARCHIVE_MAX_MB,   # 빈값=무제한
        "raw_archive_bytes": _raw_size_cached(),
        # 원본 보존소 경로(직접 지정 가능 — 용량이 커서 외장/별도 디스크로 뺄 수 있게).
        "raw_archive_dir": str(C.RAW_ARCHIVE_DIR),
        "raw_archive_exists": C.RAW_ARCHIVE_DIR.exists(),
        "keys": {k: bool(os.environ.get(k)) for k in
                 ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")},
        "config_path": str(C.CONFIG_PATH),
        # claude CLI 경로: 사용자가 지정한 override(있으면) + 실제 해석된 경로/발견 여부.
        # macOS GUI 앱은 셸 PATH 미상속이라 여기서 직접 지정할 수 있게 노출한다.
        "claude_bin": os.environ.get("VESTIGE_CLAUDE_BIN", ""),
        "claude_resolved": _claude or "",
        "claude_found": _claude is not None,
        # Claude Code 로그 소스 — 각 사용자 홈 기준 자동 해석, 필요 시 직접 지정.
        "projects_dir": str(C.PROJECTS_DIR),
        "projects_exists": C.PROJECTS_DIR.exists(),
        # Codex 로그 루트(직접 지정 가능). 기본=$CODEX_HOME/sessions 또는 ~/.codex/sessions.
        "codex_dir": str(C.CODEX_SESSIONS_DIR),
        "codex_exists": C.CODEX_SESSIONS_DIR.exists(),
        # .stversions 제외 카운트 — 3s 폴링 대비 TTL 캐시(매번 전체 walk 방지).
        "jsonl_count": _jsonl_count_cached(),
        # 멀티소스 색인 현황(claude-code + codex …). 루트 없는 소스는 active=false.
        "sources": _sources_info_cached(),
        # 자동화(SDK/claude -p) 세션 제외 여부(기본 켜짐). 켜면 promptSource=sdk 프롬프트를 색인에서 뺀다.
        "skip_sdk": _parser.skip_sdk_enabled(),
    }


@app.get("/api/skip-sdk-stats")
def api_skip_sdk_stats():
    """자동화(sdk) 제외 대상 규모(세션/턴) + 현재 제외 설정. 설정 화면이 필요 시 1회 조회(폴링 아님)."""
    from .parser import skip_sdk_enabled
    return {**_skip_sdk_stats(), "enabled": skip_sdk_enabled()}


@app.put("/api/config")
def api_config_put(payload: dict):
    """설정 저장: config.env 갱신 + 실행 중 프로세스 반영 + 필요 시 스케줄러 재등록.

    payload = {"VESTIGE_ENRICH_BACKEND": "...", "OPENAI_API_KEY": "...", ...}
    빈 문자열 값은 해당 키 비활성(주석).
    """
    import importlib
    import os

    from . import config as C

    # 화이트리스트: VESTIGE_* 설정 + 알려진 키/경로만 허용(임의 env 주입 차단).
    _allowed_exact = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                      "GOOGLE_API_KEY", "CLAUDE_PROJECTS_DIR", "CODEX_SESSIONS_DIR"}
    # 레거시 back-compat: 이름 변경 전 프론트(캐시된 빌드)가 보낸 구 CHATMEM_ 키를
    # 신규 VESTIGE_ 로 정규화 → 화이트리스트·검증·저장이 모두 신규 키로 일관되게 처리.
    raw: dict[str, str] = {}
    for k, v in (payload or {}).items():
        k = str(k)
        if k.startswith("CHATMEM_"):
            k = "VESTIGE_" + k[len("CHATMEM_"):]
        raw[k] = str(v)
    updates = {k: v for k, v in raw.items() if k.startswith("VESTIGE_") or k in _allowed_exact}
    rejected = [k for k in raw if k not in updates]
    if not updates:
        return {"ok": True, "changed": [], "rejected": rejected}

    # 값 검증: 잘못된 INDEX_MODE/INDEX_TIME이 조용히 스케줄 색인을 영영 멈추지 않게 저장 전에 거른다.
    invalid: list[str] = []
    _mode = updates.get("VESTIGE_INDEX_MODE")
    if _mode not in (None, "") and _mode not in {"off", "interval", "realtime", "scheduled"}:
        invalid.append("VESTIGE_INDEX_MODE")
    _time = updates.get("VESTIGE_INDEX_TIME")
    if _time not in (None, ""):
        try:
            _h, _m = str(_time).split(":")
            if not (0 <= int(_h) <= 23 and 0 <= int(_m) <= 59):
                invalid.append("VESTIGE_INDEX_TIME")
        except (ValueError, AttributeError):
            invalid.append("VESTIGE_INDEX_TIME")
    # 색인 주기: 1 이상 정수만. 검증이 없어 '1.5' 가 그대로 저장됐고, 바로 아래 reload 와 다음
    # 실행의 import 가 config.py 의 int() 에서 터져 백엔드가 아예 안 떴다.
    _iv = updates.get("VESTIGE_INDEX_INTERVAL")
    if _iv not in (None, ""):
        try:
            if int(str(_iv).strip()) < 1:
                invalid.append("VESTIGE_INDEX_INTERVAL")
        except (ValueError, TypeError):
            invalid.append("VESTIGE_INDEX_INTERVAL")
    # 보존소 상한: 숫자·0 이상만. 검증을 안 하면 잘못된 값이 indexer 의 int() 에서 터지고,
    # 그 예외는 로그로만 사라져 '상한을 켰다고 믿는데 영구히 미적용'인 상태가 된다.
    _mb = updates.get("VESTIGE_RAW_ARCHIVE_MAX_MB")
    if _mb not in (None, ""):
        try:
            if int(str(_mb).strip()) < 0:
                invalid.append("VESTIGE_RAW_ARCHIVE_MAX_MB")
        except (ValueError, TypeError):
            invalid.append("VESTIGE_RAW_ARCHIVE_MAX_MB")
    # 보존소 경로: 실제로 만들 수 있고 쓸 수 있는 디렉터리여야 한다. 쓸 수 없는 경로를 저장하면
    # 이후 모든 미러링이 실패하고, 사용자는 설정 화면에 그 경로가 멀쩡히 적혀 있는 걸 본다.
    _dir = updates.get("VESTIGE_RAW_ARCHIVE_DIR")
    if _dir not in (None, ""):
        try:
            p = Path(str(_dir).strip())
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".vestige-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            invalid.append("VESTIGE_RAW_ARCHIVE_DIR")
    if invalid:
        return {"ok": False, "code": "invalid_config_value", "invalid": invalid, "rejected": rejected}

    C.write_config(updates)                       # 1) 파일 반영
    for k, v in updates.items():                  # 2) 실행 중 os.environ 반영
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(C)                            # 3) config 모듈 재평가(새 env 반영)
    if "VESTIGE_RAW_ARCHIVE_DIR" in updates:
        _raw_size_cache["at"] = 0.0                # 경로가 바뀌었으니 용량 캐시 폐기 → 즉시 반영

    # 4) 스케줄 관련 키가 바뀌면 스케줄러 재등록
    timing_keys = {"VESTIGE_ENRICH_TIME", "VESTIGE_INDEX_INTERVAL"}
    rescheduled = False
    if timing_keys & set(updates):
        try:
            from . import scheduler
            importlib.reload(scheduler)
            scheduler.install()
            rescheduled = True
        except Exception:
            pass
    return {"ok": True, "changed": list(updates), "rejected": rejected, "rescheduled": rescheduled}


# 임베딩 모델 카탈로그(한국어 대화용). ram_gb=임베딩 실행 중 피크 워킹셋 실측(MB→GB),
# cps=청크/초 처리량 실측(CPU 기준, 기기 성능에 따라 다름). 재색인 예상시간 산출에 사용.
# 순서 = 화면 표기 순서(첫 번째가 권장 기본). 용량(디스크 GB)은 note에 적지 않는다 —
# 프론트가 size_gb 로 따로 표기하므로 중복 방지.
# int8 e5-large = fp32와 검색 품질 사실상 동일(공정벤치 R@1 동일·MRR −0.4%)이면서 색인 ~2x 빠름. → 기본·권장.
# MiniLM = 경량(RAM ~0.8GB)이나 품질 낮음. → 저사양(32GB 미만) 옵션.
# ram_gb 실측(peak_wset, 색인 배치 32 기준): int8 로드~0.9GB·임베딩 피크~1.4GB(큰 배치 스파이크
#   대비 여유 2.0), MiniLM 피크~0.8GB(여유 1.0). ※ 과거 5.0/1.2는 과다 표기였음 — 실측으로 정정.
_EMBED_ALLOW = {
    INT8_MODEL_ID: {
        "note": "권장 기본 — e5-large 수준 품질에 색인 속도 약 2배 빠름.",
        "ram_gb": 2.0, "cps": 1.6,
        "tags": ["권장 기본", "품질 최상", "빠름"]},
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": {
        "note": "경량 — RAM이 적은 기기(32GB 미만)에 권장. 빠르지만 검색 품질은 다소 낮음.",
        "ram_gb": 1.0, "cps": 31.0,
        "tags": ["저사양 추천", "램 부하 적음", "속도 매우 빠름"]},
}

_reindex_state: dict = {"running": False, "done": 0, "msg": "", "done_files": 0, "total_files": 0,
                        "done_chunks": 0, "total_chunks": 0}


# 대기 집계 캐시: 상태 폴링(3s)마다 357개 파일 stat+쿼리를 다시 돌지 않게 짧게 캐싱.
_pending_cache: dict = {"at": 0.0, "index": {"new_sessions": 0, "updated_sessions": 0, "files": 0},
                        "enrich_turns": 0}
_PENDING_TTL = 8.0


# 갱신은 한 번에 하나만. 스캔이 TTL 보다 길어지면 폴링마다 새 스캔이 겹쳐 디스크·GIL 을
# 서로 뺏으며 점점 느려지고 영영 안 끝난다(실측: 같은 스캔에 갇힌 워커 스레드 4개 이상).
_pending_lock = threading.Lock()


def _pending_snapshot() -> dict:
    """색인·정제 대기 수를 값싸게(모델 로드 없이) 계산해 TTL 동안 캐싱."""
    now = time.time()
    if now - _pending_cache["at"] < _PENDING_TTL:
        return _pending_cache
    if not _pending_lock.acquire(blocking=False):
        return _pending_cache   # 다른 요청이 갱신 중 — 겹쳐 돌지 말고 직전 값으로 답한다
    try:
        from .indexer import count_pending
        try:
            db = ArchiveDB()
            idx = count_pending(db)   # 활성 소스 전체(claude-code + codex …) 합산
            enr = db.conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE summary IS NULL").fetchone()["c"]
        except Exception:  # noqa: BLE001 — 대기 조회 실패해도 UI가 죽지 않게 이전 값 유지
            if not _pending_cache.get("failing"):   # 8초마다 같은 줄이 쌓이지 않게 연속 실패는 한 번만
                logging.getLogger(__name__).warning("대기 건수 조회 실패 - 이전 값을 유지합니다", exc_info=True)
                _pending_cache["failing"] = True
            return _pending_cache
        _pending_cache.update(at=now, index=idx, enrich_turns=enr, failing=False)
        return _pending_cache
    finally:
        _pending_lock.release()


# 보존소 용량 캐시 — /api/config 가 3s 폴링되는데 mirror_size_bytes 는 보존소 전체를
# rglob+stat 한다(세션당 파일 1개라 수천 세션이면 초 단위). 옆의 jsonl_count 와 같은 TTL 공유.
_raw_size_cache: dict = {"at": 0.0, "n": 0}


def _raw_size_cached() -> int:
    from . import raw_archive
    now = time.time()
    if now - _raw_size_cache["at"] < _PENDING_TTL:
        return _raw_size_cache["n"]
    try:
        n = raw_archive.mirror_size_bytes()
    except Exception:  # noqa: BLE001 — 실패 시 이전 값 유지(설정 화면이 500 나지 않게)
        return _raw_size_cache["n"]
    _raw_size_cache.update(at=now, n=n)
    return n


# JSONL 총개수 캐시 — /api/config가 3s 폴링돼도 매번 전체 폴더 walk 안 하게(TTL 공유).
_jsonl_cache: dict = {"at": 0.0, "n": 0}


def _jsonl_count_cached() -> int:
    from . import config as C
    from .indexer import iter_jsonl
    now = time.time()
    if now - _jsonl_cache["at"] < _PENDING_TTL:
        return _jsonl_cache["n"]
    try:
        n = sum(1 for _ in iter_jsonl(C.PROJECTS_DIR)) if C.PROJECTS_DIR.exists() else 0
    except Exception:  # noqa: BLE001 — 실패 시 이전 값 유지
        return _jsonl_cache["n"]
    _jsonl_cache.update(at=now, n=n)
    return n


# 자동화(sdk) 제외 규모 캐시 — 원문 전체를 읽어 세는 비용이라 TTL 캐시 + 설정 화면 조회 시에만.
_skip_sdk_cache: dict = {"at": 0.0, "sessions": 0, "turns": 0}
_SKIP_SDK_TTL = 60.0


_SDK_MARK = re.compile(rb'"promptSource"\s*:\s*"sdk"')
# 파일별 (크기, 수정시각) -> 자동화 프롬프트 수. 안 바뀐 파일은 다시 읽지 않는다.
_sdk_file_cache: dict[str, tuple[tuple[int, int], int]] = {}


def _sdk_turns_in(f) -> int:
    """이 로그 파일 안의 자동화(sdk) 프롬프트 수. 표지(`"promptSource":"sdk"`)가 있는 줄만 파싱한다 -
    예전엔 설정 화면을 열 때마다 원문 로그 전체를 JSON 으로 파싱했다(실측 1.2GB 약 28초, GIL 을 쥔 채).
    표지는 plumbing 기록에도 붙어 파일 대부분(207/265)에 있어서, 파일 단위로 거르면 소용이 없다."""
    from .parser import is_sdk_prompt
    st = os.stat(f)
    key = (st.st_size, st.st_mtime_ns)
    got = _sdk_file_cache.get(str(f))
    if got and got[0] == key:
        return got[1]
    n = 0
    if st.st_size:
        with open(f, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
            done_to = -1   # 이미 본 줄의 끝 - 한 줄에 표지가 둘이어도 한 번만 센다
            for hit in _SDK_MARK.finditer(m):
                if hit.start() < done_to:
                    continue
                start = m.rfind(b"\n", 0, hit.start()) + 1
                end = m.find(b"\n", hit.end())
                end = len(m) if end == -1 else end
                done_to = end
                try:
                    obj = json.loads(m[start:end])
                except ValueError:
                    continue   # 아직 쓰이는 중인 마지막 줄 등
                if isinstance(obj, dict) and is_sdk_prompt(obj):
                    n += 1
    _sdk_file_cache[str(f)] = (key, n)
    return n


def _skip_sdk_stats() -> dict:
    """자동화(sdk) 프롬프트가 원문 로그에 몇 개(세션/턴) 있는지 — 제외 개수 표기용. 60s 캐시.
    파일=세션 단위(claude-code/codex 모두 파일 하나가 한 세션)."""
    now = time.time()
    if now - _skip_sdk_cache["at"] < _SKIP_SDK_TTL:
        return {"sessions": _skip_sdk_cache["sessions"], "turns": _skip_sdk_cache["turns"]}
    from .indexer import discover_files
    sessions = turns = 0
    try:
        for f, _adapter in discover_files(recent_first=False):
            try:
                n = _sdk_turns_in(f)
            except Exception:  # noqa: BLE001 — 한 파일 오류가 집계를 막지 않게
                continue
            turns += n
            sessions += 1 if n else 0
    except Exception:  # noqa: BLE001 — 집계 실패는 이전 값 유지
        return {"sessions": _skip_sdk_cache["sessions"], "turns": _skip_sdk_cache["turns"]}
    _skip_sdk_cache.update(at=now, sessions=sessions, turns=turns)
    return {"sessions": sessions, "turns": turns}


# 색인 소스 현황 캐시 — /api/config 폴링 대비(소스별 파일 walk를 매번 안 하게).
_sources_cache: dict = {"at": 0.0, "list": []}


def _sources_info_cached() -> list:
    """등록된 소스별 {name, root, exists, active, count}. active=현재 색인 대상인지."""
    now = time.time()
    if now - _sources_cache["at"] < _PENDING_TTL and _sources_cache["list"]:
        return _sources_cache["list"]
    from .sources import (ADAPTERS, active_sources, disabled_sources,
                          is_substream, parent_source, source_roots)
    active = {n for n, _a, _r in active_sources()}
    disabled = disabled_sources()
    roots = source_roots()

    def _count(name: str, adapter, root) -> int:
        if not (root and root.exists()):
            return 0
        try:
            return sum(1 for _ in adapter.discover(root))
        except Exception as e:  # noqa: BLE001 — walk 실패해도 UI 안 죽게(로그만)
            import logging
            logging.getLogger(__name__).warning("소스 %s 파일 카운트 실패: %s", name, e)
            return 0

    # 하위 스트림(subagent)은 부모 출처(claude-code)로 접힌다: 별도 토글로 노출하지 않고,
    # 그 파일 수는 부모 카운트에 합산해 "Claude Code N개"가 실제 색인 범위와 어긋나지 않게 한다.
    substream_counts: dict[str, int] = {}
    for name, adapter in ADAPTERS.items():
        if is_substream(name):
            substream_counts[parent_source(name)] = (
                substream_counts.get(parent_source(name), 0) + _count(name, adapter, roots.get(name)))

    out = []
    for name, adapter in ADAPTERS.items():
        if is_substream(name):
            continue   # 부모 토글을 따르므로 설정에 별도 표시 안 함
        root = roots.get(name)
        exists = bool(root and root.exists())
        count = _count(name, adapter, root) + substream_counts.get(name, 0)
        out.append({"name": name, "root": str(root) if root else None, "exists": exists,
                    "active": name in active, "disabled": name in disabled, "count": count})
    _sources_cache.update(at=now, list=out)
    return out


@app.get("/api/index/status")
def api_index_status():
    """증분 색인(자동/수동) 상태 + 대기(새 대화) 집계 — UI 표시용.

    이 프로세스가 색인 중이 아니면, 별도 프로세스(OS 스케줄러의 `vestige index`)가 색인 중인지
    크로스-프로세스 락으로 확인해 running/external 에 반영한다(배너·설정 버튼 상태 일관성)."""
    st = dict(_autoindex_state)
    external = False
    # 외부(OS 스케줄러의 별도 vestige index) 색인은 이 프로세스가 idle 이고, 인-프로세스 자동색인이
    # 담당자가 아닐 때(enabled=False = 스케줄러/off 모드)만 의미가 있다. interval/realtime 은 이
    # 프로세스가 색인하므로 매 폴링마다 락 파일 검사(파일열기+락+쓰기)를 돌릴 필요가 없다.
    if not (_autoindex_state.get("running") or _reindex_state.get("running")
            or _autoindex_state.get("enabled")):
        with contextlib.suppress(Exception):
            from .proclock import held_here, is_locked
            external = is_locked() and not held_here()   # 자기 프로세스 보유분은 외부로 오인하지 않음
        if external:
            st["running"] = True          # 설정 버튼 비활성·표시를 배너와 일치시킴(라벨은 프론트 i18n)
    st["external"] = external
    st["pending"] = _pending_snapshot()["index"]
    return st


@app.post("/api/index/run")
def api_index_run():
    """수동 증분 색인(새 대화만, 빠름). 이미 색인/재색인 중이면 busy."""
    if _autoindex_state.get("running") or _reindex_state.get("running"):
        return {"ok": False, "busy": True}
    from .proclock import held_here, is_locked
    if is_locked() and not held_here():      # 다른 프로세스(스케줄러)가 색인 중
        return {"ok": False, "busy": True}
    threading.Thread(target=_run_incremental, daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/archive/sync")
def api_archive_sync():
    """지금 즉시 기기 간 아카이브 병합: 다른 기기 export 가져오기 + 내 것 내보내기.
    가져온 세션의 벡터는 이어지는 증분 색인(backfill)이 활성 모델로 채운다."""
    from . import config as C
    from .archive_sync import device_id, export_archive, import_archives
    # 색인과 같은 DB 를 동시에 쓰지 않는다(쓰기 주체가 둘이면 서로의 잠금 대기가 길어진다).
    if not _index_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"code": "reindex_already_running", "msg": "이미 색인 중이에요"})
    try:
        db = ArchiveDB()
        did = device_id(db)
        vi = make_index()   # 더 완성된 상대 턴으로 갱신 시 스테일 벡터 제거용(backfill 이 재임베딩)
        notes: list[str] = []
        imported = import_archives(db, C.PROJECTS_DIR, did, vi=vi, log_fn=notes.append)
        exported = export_archive(db, C.PROJECTS_DIR, did)
    finally:
        _index_lock.release()
    if imported and not _autoindex_state.get("running") and not _reindex_state.get("running"):
        threading.Thread(target=_run_incremental, daemon=True).start()   # 가져온 청크 임베딩
    # 건너뛴 줄이 있으면 사용자에게 알린다 — ok:true 만 주면 부분 실패가 묻힌다.
    warn = [m for m in notes if m.startswith("ERROR ")]
    return {"ok": True, "imported": imported, "exported": exported,
            "warnings": [m[6:] for m in warn]}


@app.post("/api/verify-enrich")
def api_verify_enrich(payload: dict):
    """정제 백엔드 연결 검증(무료 models.list). payload={backend, model?, api_key?, ollama_url?}."""
    from .enrich import verify_backend
    backend = str((payload or {}).get("backend", "")).strip()
    ok, msg = verify_backend(
        backend,
        model=(payload or {}).get("model") or None,
        api_key=(payload or {}).get("api_key") or None,
        base_url=(payload or {}).get("ollama_url") or None,
    )
    return {"ok": ok, "message": msg}


# 수동 정제 상태.
_enrich_state: dict = {"running": False, "phase": "대기", "done_sessions": 0, "total_sessions": 0,
                       "enriched": 0, "last_error": None, "errors": []}


def _capture_log(state: dict, *, key: str = "errors"):
    """log_fn 래퍼: phase를 갱신하고 'ERROR' 로그는 bounded errors 목록에 모아 UI에 노출.
    (한 항목이 매 주기 조용히 실패하며 스턱되는 걸 사용자가 볼 수 있게)"""
    def log(m: str):
        state["phase"] = m
        if isinstance(m, str) and m.startswith("ERROR"):
            errs = state.setdefault(key, [])
            errs.append(m)
            del errs[:-8]   # 최근 8건만 유지
    return log


@app.get("/api/enrich/status")
def api_enrich_status():
    """정제 상태 + 아직 정제 안 된 턴 수(summary IS NULL)."""
    return {**_enrich_state, "pending_turns": _pending_snapshot()["enrich_turns"]}


@app.post("/api/enrich")
def api_enrich(payload: dict | None = None):
    """수동 정제(요약·태그). 설정된 백엔드가 가능할 때만. all=false면 아직 정제 안 된 것만."""
    from . import config as C
    from .enrich import backend_available, enrich_all
    if _enrich_state["running"]:
        return {"ok": False, "error": "이미 정제 중", "code": "enrich_already_running"}
    backend = C.ENRICH_BACKEND
    ok, why = backend_available(backend)
    if not ok:
        return {"ok": False, "error": why, "code": "enrich_unavailable", "detail": why}   # 예: "claude CLI 없음", "ANTHROPIC_API_KEY 미설정"
    only_missing = not bool((payload or {}).get("all"))

    def worker():
        _enrich_state.update(running=True, phase="시작", done_sessions=0, total_sessions=0,
                             enriched=0, last_error=None, errors=[])
        try:
            db = ArchiveDB()
            total = enrich_all(
                db, backend=backend, model=None, only_missing=only_missing,
                log_fn=_capture_log(_enrich_state),
                progress_fn=lambda d, t: _enrich_state.update(done_sessions=d, total_sessions=t),
            )
            if total:
                _graph3d_invalidate()   # 태그가 바뀌었으니 군집 라벨도 다시 계산되게 지도 캐시 폐기
            _enrich_state.update(phase=f"완료: {total}턴 정제", enriched=total)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc(file=_sys.stderr)   # 스레드 예외 → app.log 에 트레이스백 남김
            _enrich_state.update(phase="오류", last_error=str(e))
        finally:
            _enrich_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True, "backend": backend}


@app.get("/api/mcp")
def api_mcp():
    """MCP 클라이언트별 등록 상태 + 실행 커맨드."""
    from . import mcp_register as R
    cmd, args = R.mcp_command()
    return {"targets": R.targets(), "command": (cmd + (" " + " ".join(args) if args else ""))}


@app.post("/api/mcp/register")
def api_mcp_register(payload: dict):
    """대상 클라이언트 설정에 vestige MCP 서버 등록(파일은 .bak 백업 후 수정)."""
    from . import mcp_register as R
    tid = str((payload or {}).get("target", "")).strip()
    try:
        R.register(tid)
        return {"ok": True, "restart": True}
    except Exception as e:  # noqa: BLE001 — 사용자에게 원인 메시지 노출
        return {"ok": False, "error": str(e)}


@app.post("/api/mcp/unregister")
def api_mcp_unregister(payload: dict):
    from . import mcp_register as R
    tid = str((payload or {}).get("target", "")).strip()
    try:
        R.unregister(tid)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# 기기 RAM 기반 권장 임계치. 이 GB 이상이면 int8 e5-large(품질), 미만이면 경량(MiniLM).
# 30으로 두는 이유: 명목 32GB 기기도 OS/하드웨어 예약분 때문에 실측 총량이 ~31.6GB로 보고됨
# → 32로 두면 실제 32GB 기기가 걸러짐. 30이면 명목 32GB=int8, 명목 16/24GB=MiniLM로 의도대로.
_RECO_RAM_GB = 30
_MODEL_INT8 = INT8_MODEL_ID            # 기본·권장
_MODEL_MINI = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# fastembed 카탈로그에 없는 커스텀(번들) 모델의 dim/size 수동 지정.
_CUSTOM_META = {INT8_MODEL_ID: {"dim": 1024, "size_gb": 0.52}}


@app.get("/api/embed-models")
def api_embed_models():
    from fastembed import TextEmbedding

    from . import config as C
    from .sysmem import total_mb
    cat = {m["model"]: m for m in TextEmbedding.list_supported_models()}
    total_chunks = ArchiveDB().conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    # 기기 RAM 보고 권장: 32GB↑ → int8 e5-large(품질), 그 미만(≤16/24GB) → 경량(MiniLM).
    # RAM 미상이면 품질(int8)로(데스크탑에서 감지 실패는 드묾).
    tmb = total_mb()
    total_gb = round(tmb / 1024, 1) if tmb else None
    rec_model = _MODEL_MINI if (total_gb is not None and total_gb < _RECO_RAM_GB) else _MODEL_INT8
    out = []
    for name, meta in _EMBED_ALLOW.items():
        m = cat.get(name)
        if m:
            dim, size_gb = m.get("dim"), round(m.get("size_in_GB", 0), 2)
        elif name in _CUSTOM_META:      # 커스텀(번들) 모델 — fastembed 카탈로그에 없음
            dim, size_gb = _CUSTOM_META[name]["dim"], _CUSTOM_META[name]["size_gb"]
        else:
            continue
        cps = meta["cps"]
        out.append({
            "model": name, "dim": dim, "size_gb": size_gb,
            "ram_gb": meta["ram_gb"],                 # 임베딩 중 실사용 피크(실측/추정)
            "cps": cps,
            "est_reindex_min": round(total_chunks / cps / 60, 1) if cps else None,
            "note": meta["note"], "tags": meta.get("tags", []), "current": name == C.EMBED_MODEL,
            "recommended": name == rec_model,   # 기기 RAM 기반 권장(≥32GB→int8, 그 미만→MiniLM)
        })
    return {"models": out, "current": C.EMBED_MODEL, "recommended": rec_model,
            "ram_total_gb": total_gb, "total_chunks": total_chunks, "reindex": _reindex_state}


@app.get("/api/onboarding")
def api_onboarding():
    """첫 실행 여부 — True면 프론트가 모델 선택 화면을 먼저 보여준다."""
    return {"needed": bool(_state.get("needs_onboarding"))}


@app.post("/api/onboarding/choose")
def api_onboarding_choose(payload: dict):
    """첫 실행에서 임베딩 모델 확정 → 설정 저장 + 그 모델 로드(백그라운드) + 색인 시작."""
    from . import config as C
    model = str((payload or {}).get("model", "")).strip()
    if model not in _EMBED_ALLOW:
        return {"ok": False, "error": "알 수 없는 모델", "code": "unknown_model"}
    C.write_config({"VESTIGE_EMBED_MODEL": model})
    C.EMBED_MODEL = model   # 파일만 바꾸면 이 프로세스는 다시 켤 때까지 옛 기본값(큰 모델)을 쓴다
    # 확정 표시(먼저, 응답 전에) → get_embedder 가 이 모델로 로드. 커밋해야 남는다 - 예전엔 커밋 없이
    # 연결을 버려 기록이 되돌려졌고, 사용자가 가벼운 모델을 골라도 큰 기본 모델이 올라갔다.
    # 실패하면 성공이라고 답하지 않는다(500 → 화면이 오류를 보인다).
    db = ArchiveDB()
    db.set_meta("embed_model", model)
    db.commit()

    def _load():
        with contextlib.suppress(Exception):
            get_embedder()   # 다운로드/로드(가벼운 모델이면 빠름) + last_used 갱신

    _state["needs_onboarding"] = False
    threading.Thread(target=_load, daemon=True).start()   # 자동 색인 스레드가 임베더 로드되면 색인 시작
    return {"ok": True, "model": model}


def _must_reset_vectors(stored_model: str | None, model: str) -> bool:
    """재색인 때 벡터를 비워야 하나. 모델이 바뀌었거나 어떤 모델로 만든 벡터인지 모르면 비운다."""
    return stored_model != model


@app.post("/api/reindex")
def api_reindex(payload: dict):
    """전체 재색인(백그라운드). model 생략/빈값이면 **현재 모델로** 재색인, 지정하면 그 모델로 교체 후 재색인.
    로그를 처음부터 다시 읽는다. 같은 모델이면 벡터를 지우지 않고 바뀐 청크만 다시 임베딩하고,
    모델이 바뀌면 벡터를 비우고 전부 다시 임베딩한다.
    fast=true: 재파싱 없이 chunks에서 병렬 대량 임베딩(고RAM 기기 전용, parallel 프로세스 수)."""
    from . import config as C
    payload = payload or {}
    model = str(payload.get("model", "")).strip() or C.EMBED_MODEL   # 빈값=현재 모델
    if model not in _EMBED_ALLOW:
        return {"ok": False, "error": "알 수 없는 모델", "code": "unknown_model"}
    if _reindex_state["running"] or _autoindex_state.get("running"):
        return {"ok": False, "error": "이미 색인/재색인 중", "code": "reindex_already_running"}
    from .proclock import held_here, is_locked
    if is_locked() and not held_here():      # 다른 프로세스(스케줄러)가 색인 중
        return {"ok": False, "error": "다른 프로세스 색인 중 — 잠시 후 재시도", "code": "reindex_already_running"}
    fast = bool(payload.get("fast"))
    try:
        parallel = int(payload.get("parallel") or 2)
    except (TypeError, ValueError):
        parallel = 2
    parallel = max(2, min(parallel, 8))   # 안전 범위(프로세스당 모델 RAM 부담)

    def worker():
        from .embedder import Embedder
        from .indexer import backfill_missing, index_all
        if not _index_lock.acquire(blocking=False):   # 같은 프로세스 색인과 상호배제
            _reindex_state["msg"] = "다른 색인 진행 중 — 잠시 후 재시도"
            return
        from .proclock import IndexLock
        _xlock = IndexLock()
        if not _xlock.acquire():                       # 다른 프로세스(스케줄러) 색인과 상호배제
            _index_lock.release()
            _reindex_state["msg"] = "다른 프로세스 색인 중 — 잠시 후 재시도"
            return
        _reindex_state.update(running=True, done=0, msg="시작", done_files=0, total_files=0,
                              done_chunks=0, total_chunks=0)
        try:
            C.write_config({"VESTIGE_EMBED_MODEL": model})
            # 모델 교체든 현재모델 재색인이든, 기존 벡터 폐기 후 처음부터 재임베딩(백엔드 무관 reset).
            db = ArchiveDB()
            total_chunks = db.conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
            vi = make_index()
            emb = _state.get("embedder")
            if emb is None or getattr(emb, "model_name", None) != model:
                _state.pop("embedder", None)   # 다른 모델이면 내려 둔다 - 두 벌이 함께 올라 RAM 이 두 배가 된다
                emb = Embedder(model)
            _reindex_state["total_chunks"] = total_chunks

            def log(msg):
                _reindex_state["msg"] = msg

            # fast: chunks 테이블에서 재파싱 없이 병렬 임베딩(커서 보존 → 이후 증분 정상).
            if fast and total_chunks > 0:
                # OOM 하드 가드: 프로세스마다 모델을 로드하므로 가용 RAM/모델RAM 만큼만 허용.
                from .sysmem import available_mb
                ram_gb = _EMBED_ALLOW.get(model, {}).get("ram_gb") or 4.0
                avail = available_mb()
                if avail is not None:
                    safe = max(1, int(avail / (ram_gb * 1024)))
                    if parallel > safe:
                        log(f"RAM 여유상 병렬 {parallel}→{safe}로 자동 제한")
                        parallel = safe
                use_parallel = parallel if parallel >= 2 else None  # 1이면 병렬 이득 없음 → 순차
                vi.reset()
                _reindex_state["msg"] = (f"빠른 재색인(병렬 {parallel})…" if use_parallel
                                         else "재색인(병렬 불가 — RAM 부족, 순차 진행)…")
                try:
                    total = backfill_missing(
                        db, vi, emb, batch=512, parallel=use_parallel, log_fn=log,
                        progress_fn=lambda d, t: _reindex_state.update(done_chunks=d, total_chunks=t))
                except Exception as pe:  # noqa: BLE001 — 병렬 실패 시 순차로 폴백
                    log(f"병렬 실패({pe}) → 순차 재색인으로 전환")
                    vi.reset()
                    total = backfill_missing(
                        db, vi, emb, log_fn=log,
                        progress_fn=lambda d, t: _reindex_state.update(done_chunks=d, total_chunks=t))
            else:
                db.clear_cursors()
                # 같은 모델이면 벡터를 지우지 않는다 - 색인기가 텍스트가 그대로인 청크는 건너뛰므로
                # 바뀐 부분만 다시 임베딩된다. 모델이 바뀌면 벡터 공간이 달라 전부 다시 해야 한다.
                if _must_reset_vectors(db.get_meta("embed_model"), model):
                    vi.reset()
                else:
                    # 바뀐 청크만 임베딩하므로 '전체 청크 수'를 분모로 쓰면 진행률이 20%쯤에서 멈춘 듯
                    # 보이다 끝난다. 0 이면 화면이 파일 단위 진행률(정확하다)로 보여준다.
                    _reindex_state["total_chunks"] = 0
                total = index_all(
                    db, vi, emb, log_fn=log,
                    progress_fn=lambda d, t: _reindex_state.update(done_files=d, total_files=t),
                    chunk_progress_fn=lambda d: _reindex_state.__setitem__("done_chunks", d))
            db.set_meta("embed_model", model)
            db.commit()   # 커밋 없이 끝나면 연결이 닫힐 때 되돌려진다
            _state["embedder"] = emb  # 실행 중 검색도 새 모델로
            _embedder_last_used[0] = time.monotonic()  # 유휴 언로드 타이머 리셋
            _state["model_mismatch"] = None  # 재색인으로 해소 → 불일치 배너 즉시 내림
            _reindex_state.update(done=total, msg=f"완료: {total}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc(file=_sys.stderr)   # 스레드 예외 → app.log 에 트레이스백 남김
            _reindex_state["msg"] = f"오류: {e}"
        finally:
            _reindex_state["running"] = False
            _xlock.release()
            _index_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True}


_GRAPH3D_VER = 9   # 군집 n=고유 turn 수(청크 아님) → 구캐시 폐기·재계산
_graph3d_recomputing = {"on": False}
_GRAPH3D_DELTA_RATIO = 0.05   # 벡터 수 변화가 이 비율(또는 최소 개수) 미만이면 재계산 안 함(지도 흔들림 방지)
_GRAPH3D_MIN_DELTA = 50


def _graph3d_compute_and_cache(n: int) -> dict:
    from . import config as C
    from .graph import build_graph
    cache_path = C.DATA_DIR / "graph3d_cache.json"
    prev_members = None                          # 이전 군집 구성원 → id 승계 기준
    try:
        if cache_path.exists():
            old = json.loads(cache_path.read_text(encoding="utf-8"))
            if old.get("v") == _GRAPH3D_VER:
                prev_members = old.get("members")
    except Exception:
        prev_members = None
    data = build_graph(make_index(), ArchiveDB(), dims=3, prev_members=prev_members)
    members = data.pop("_members", [])           # 프론트로는 안 보냄(캐시에만)
    try:
        cache_path.write_text(
            json.dumps({"n": n, "v": _GRAPH3D_VER, "data": data, "members": members}, ensure_ascii=False),
            encoding="utf-8")
        _graph3d_stale_path().unlink(missing_ok=True)   # 새로 계산했으니 낡음 표시를 거둔다
    except Exception:
        pass
    return data


def _graph3d_recompute_bg(n: int) -> None:
    """백그라운드 재계산(중복 방지). 벡터 수 바뀌었을 때 조용히 캐시 갱신."""
    import threading
    if _graph3d_recomputing["on"]:
        return
    _graph3d_recomputing["on"] = True

    def work():
        try:
            _graph3d_compute_and_cache(n)
        except Exception:
            import traceback   # 계속 실패하면 지도가 옛 캐시로 남는다 - 이유를 남긴다
            traceback.print_exc(file=_sys.stderr)
        finally:
            _graph3d_recomputing["on"] = False
    threading.Thread(target=work, daemon=True).start()


def _graph3d_data(refresh: bool = False) -> dict:
    """의미 지도 3D 데이터. stale-while-revalidate: 캐시 있으면 즉시 반환하고,
    벡터 수가 달라졌으면 백그라운드로 재계산(여는 순간 대기 없음)."""
    from . import config as C

    # 개수만 필요하다(캐시가 낡았는지 판정용). make_index() 를 쓰면 npy 백엔드에서 벡터 행렬을
    # 통째로 올리는데, 이 함수는 예열 스레드가 3분마다 + /api/graph3d 요청마다 부른다.
    # 실제 행렬이 필요한 쪽은 _graph3d_compute_and_cache 뿐이고, 거기서 따로 만든다.
    n = vector_count()
    if n == 0:
        return {"points": [], "clusters": [], "method": None, "dims": 3}

    cache_path = C.DATA_DIR / "graph3d_cache.json"
    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("v") == _GRAPH3D_VER and cached.get("data"):
                cached_n = int(cached.get("n") or 0)
                # 임계값 이상 변했을 때만 재계산(작은 변화엔 지도 안 흔들리게) — stale-while-revalidate.
                # stale 표시(접기/펼치기·정제)는 개수가 그대로여도 내용이 바뀐 경우라 무조건 재계산.
                if cached.get("stale") or _graph3d_stale_path().exists() or (   # 옛 캐시의 stale 키도 계속 존중
                    cached_n > 0
                    and abs(n - cached_n) >= max(_GRAPH3D_MIN_DELTA, int(cached_n * _GRAPH3D_DELTA_RATIO))
                ):
                    _graph3d_recompute_bg(n)
                return cached["data"]
        except Exception:
            pass

    # 캐시 없음(최초) 또는 강제 → 동기 계산. (보통 시작 시 예열로 이미 채워짐)
    return _graph3d_compute_and_cache(n)


def _graph3d_stale_path():
    from . import config as C
    return C.DATA_DIR / "graph3d_cache.stale"


def _graph3d_invalidate() -> None:
    """지도 캐시를 '낡음'으로만 표시 → 다음 조회가 옛 데이터를 즉시 주고 뒤에서 재계산한다.

    파일을 지우면 안 되는 이유 두 가지:
    1) 캐시가 없으면 _graph3d_data 가 동기 경로로 떨어져 UMAP+HDBSCAN 이 그 자리에서 돈다.
       접기/펼치기는 일상 클릭이라 그때마다 군집 탭이 수십 초 멈춘다.
    2) prev_members 가 같이 사라져 군집 id 승계가 끊기고, 접기 한 번에 군집 색이 전부 바뀐다.

    표시는 **본문과 분리해 빈 파일로** 둔다. 예전엔 캐시 JSON 에 stale=True 를 넣으려고
    5MB 를 통째로 읽고 파싱하고 다시 썼다(실측 85ms). 접기/펼치기는 일상 클릭이라 그 비용이
    매번 들었고, 쓰는 중에 죽으면 캐시가 깨진 JSON 으로 남았다. 0바이트 생성은 원자적이다.
    """
    with contextlib.suppress(Exception):
        from . import config as C
        if not (C.DATA_DIR / "graph3d_cache.json").exists():
            return
        _graph3d_stale_path().touch()


_graph3d_body: dict = {"key": None, "body": b""}


@app.get("/api/graph3d")
def api_graph3d(refresh: bool = False):
    """의미 지도 3D: UMAP 3성분 투영 점 구름.

    점이 14,014개(5MB)라 매 요청마다 캐시 파일을 파싱하고 FastAPI 가 다시 직렬화하는 데
    206ms 가 들었다. 내용이 그대로면 직렬화 결과를 그냥 돌려준다.

    캐시 키는 파일의 (mtime, size) — 재계산이든 stale 표시든 파일이 바뀌면 키가 달라져
    _graph3d_data() 를 다시 타므로, 신선도 판정을 건너뛰지 않는다.
    """
    from . import config as C

    cache_path = C.DATA_DIR / "graph3d_cache.json"

    def key_of() -> tuple[int, int, bool] | None:
        # 낡음 표시는 별도 파일이라 캐시 본문이 안 바뀐다 → 키에 같이 넣어야 무효화를 알아챈다.
        try:
            st = cache_path.stat()
            return (st.st_mtime_ns, st.st_size, _graph3d_stale_path().exists())
        except OSError:
            return None

    key = None if refresh else key_of()
    if key is not None and _graph3d_body["key"] == key:
        return Response(content=_graph3d_body["body"], media_type="application/json")

    body = json.dumps(_graph3d_data(refresh), ensure_ascii=False).encode("utf-8")
    after = key_of()      # _graph3d_data 가 파일을 갱신했을 수 있으니 직렬화 뒤의 상태로 키를 잡는다
    if after is not None:
        _graph3d_body.update(key=after, body=body)
    return Response(content=body, media_type="application/json")


# 상태바가 1초 주기로 물어본다. enriched 카운트는 turns 풀스캔이라 매번 돌 만한 값이 아니고,
# 벡터 개수는 vector_count() 로 행렬 적재를 피한다(둘 다 초 단위로 바뀌지 않는 값).
_stats_cache: dict = {"at": 0.0, "v": None}
_STATS_TTL = 2.0


@app.get("/api/stats")
def api_stats():
    now = time.time()
    if _stats_cache["v"] is not None and now - _stats_cache["at"] < _STATS_TTL:
        return _stats_cache["v"]
    db = ArchiveDB()
    out = {
        "turns": db.conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"],
        "sessions": db.conn.execute("SELECT COUNT(DISTINCT session_id) c FROM turns").fetchone()["c"],
        "vectors": vector_count(),
        "enriched": db.conn.execute("SELECT COUNT(*) c FROM turns WHERE summary IS NOT NULL").fetchone()["c"],
    }
    _stats_cache.update(at=now, v=out)
    return out


@app.get("/")
def index():
    # 빌드된 React 앱이 있으면 그것을, 없으면 기존 인라인 HTML을 서빙.
    # index.html은 절대 캐시하지 않는다: 앱 업데이트로 청크 해시가 바뀌면 캐시된 옛 index가
    # 사라진 청크를 동적 import → "Failed to fetch dynamically imported module". 엔트리는 항상
    # 최신을 받게 하고, 해시된 /assets/* 만 캐시 가능(내용 해시라 불변).
    _no_store = {"Cache-Control": "no-store, must-revalidate"}
    if (_DIST / "index.html").exists():
        return FileResponse(str(_DIST / "index.html"), headers=_no_store)
    return HTMLResponse(_HTML, headers=_no_store)


_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vestige</title>
<style>
:root{
  --bg:#f5f6f8; --surface:#ffffff; --surface2:#eef0f4; --border:#e3e5ea;
  --text:#16181d; --muted:#697080; --accent:#2f6bed; --accent-soft:#e9f0fe;
  --shadow:0 1px 2px rgba(16,18,25,.04); --shadow-lg:0 6px 20px rgba(16,18,25,.10);
  --radius:13px; --radius-sm:9px; --z-bar:10;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0c0d10; --surface:#151619; --surface2:#1d1f25; --border:#292b32;
        --text:#e9eaee; --muted:#8b919d; --accent:#7ba2ff; --accent-soft:#172033;
        --shadow:0 1px 2px rgba(0,0,0,.3); --shadow-lg:0 8px 24px rgba(0,0,0,.45);}
}
:root[data-theme=light]{--bg:#f5f6f8;--surface:#fff;--surface2:#eef0f4;--border:#e3e5ea;
  --text:#16181d;--muted:#697080;--accent:#2f6bed;--accent-soft:#e9f0fe;
  --shadow:0 1px 2px rgba(16,18,25,.04);--shadow-lg:0 6px 20px rgba(16,18,25,.10);}
:root[data-theme=dark]{--bg:#0c0d10;--surface:#151619;--surface2:#1d1f25;--border:#292b32;
  --text:#e9eaee;--muted:#8b919d;--accent:#7ba2ff;--accent-soft:#172033;
  --shadow:0 1px 2px rgba(0,0,0,.3);--shadow-lg:0 8px 24px rgba(0,0,0,.45);}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.6 -apple-system,'Segoe UI',Roboto,'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:880px;margin:0 auto;padding:0 22px 96px}

/* 앱 상단바 */
.appbar{position:sticky;top:0;z-index:calc(var(--z-bar) + 1);background:var(--bg);
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 0 12px;border-bottom:1px solid transparent}
.brand{display:flex;align-items:center;gap:9px}
.brand .logo{width:22px;height:22px;border-radius:7px;background:var(--accent);
  display:grid;place-items:center;color:#fff;font-size:12px;font-weight:800;box-shadow:var(--shadow)}
h1{font-size:16px;margin:0;font-weight:700;letter-spacing:-.01em}
.bar-right{display:flex;align-items:center;gap:12px}
.stats{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.icon-btn{width:32px;height:32px;border-radius:8px;border:1px solid var(--border);
  background:var(--surface);color:var(--muted);cursor:pointer;font-size:14px;
  display:grid;place-items:center;transition:color .15s,border-color .15s,transform .1s}
.icon-btn:hover{color:var(--text);border-color:var(--accent)}
.icon-btn:active{transform:scale(.94)}

/* 검색 히어로 */
.bar{position:sticky;top:56px;z-index:var(--z-bar);background:var(--bg);padding:8px 0 12px}
.searchbox{position:relative;display:flex;align-items:center}
.search-ico{position:absolute;left:16px;color:var(--muted);pointer-events:none}
.bar input[type=search]{width:100%;padding:15px 16px 15px 44px;font-size:16px;color:var(--text);
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  box-shadow:var(--shadow);outline:none;transition:border-color .15s,box-shadow .15s}
.bar input[type=search]::placeholder{color:var(--muted)}
.bar input[type=search]:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
.opts{display:flex;align-items:center;gap:10px;margin-top:12px;flex-wrap:wrap;
  color:var(--muted);font-size:12.5px}
.opts .spacer{flex:1 1 auto}

.slider{position:relative;display:inline-flex;border:1px solid var(--border);
  border-radius:22px;background:var(--surface);cursor:pointer;user-select:none;box-shadow:var(--shadow)}
.slider .thumb{position:absolute;top:0;left:0;height:100%;width:50%;border-radius:22px;z-index:0;
  background:var(--accent-soft);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent) 40%,transparent);
  transition:transform .2s cubic-bezier(.16,1,.3,1)}
.slider[data-on="1"] .thumb{transform:translateX(100%)}
.slider .opt{position:relative;z-index:1;flex:1 1 0;min-width:96px;text-align:center;
  padding:6px 14px;font-size:12.5px;white-space:nowrap;transition:color .15s}
.slider .opt:nth-of-type(1){color:var(--accent);font-weight:600}
.slider .opt:nth-of-type(2){color:var(--muted)}
.slider[data-on="1"] .opt:nth-of-type(1){color:var(--muted);font-weight:400}
.slider[data-on="1"] .opt:nth-of-type(2){color:var(--accent);font-weight:600}
.opts select,.opts input[type=date]{font:inherit;color:var(--text);background:var(--surface);
  border:1px solid var(--border);border-radius:8px;padding:5px 8px;cursor:pointer;outline:none;
  box-shadow:var(--shadow)}
.opts input[type=date]{font-variant-numeric:tabular-nums;color-scheme:light dark}
.opts label{display:inline-flex;align-items:center;gap:6px}
.opts .dategrp{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap}
.opts .clr{cursor:pointer;color:var(--muted);border:1px solid var(--border);background:var(--surface);
  border-radius:8px;padding:5px 10px;font:inherit;transition:color .15s,border-color .15s}
.opts .clr:hover{color:var(--text);border-color:var(--accent)}
kbd{background:var(--surface2);border:1px solid var(--border);border-radius:5px;padding:1px 6px;font-size:11px}

/* 결과 요약 줄 */
.resultbar{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;
  margin:4px 2px 2px;min-height:16px}
.resultbar b{color:var(--text);font-weight:650}

.hits{margin-top:8px;display:flex;flex-direction:column;gap:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px;box-shadow:var(--shadow);
  transition:border-color .15s,box-shadow .18s,transform .18s}
.card:hover{border-color:color-mix(in srgb,var(--accent) 35%,var(--border));
  box-shadow:var(--shadow-lg);transform:translateY(-1px)}
.meta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:11.5px;
  color:var(--muted);margin-bottom:11px;font-variant-numeric:tabular-nums}
.meta .dot{opacity:.5}
.badge{padding:2px 9px;border-radius:20px;font-weight:600;font-size:10.5px;
  background:var(--accent-soft);color:var(--accent)}
.badge.kw{background:var(--surface2);color:var(--muted)}
.headline{font-size:15.5px;font-weight:650;line-height:1.55;margin:0;text-wrap:pretty;
  letter-spacing:-.005em}
.headline .mk,.enrich .mk,.st-head .mk{color:var(--accent);margin-right:5px}
.sub{color:var(--muted);font-weight:400}
.tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
.tag{padding:2px 9px;border-radius:6px;background:var(--surface2);color:var(--muted);font-size:11px}
.enrich{margin-top:10px;font-size:13px;color:var(--muted);text-wrap:pretty}
.toggle{margin-top:11px;margin-right:14px;font-size:12px;color:var(--muted);cursor:pointer;
  user-select:none;display:inline-block;transition:color .12s}
.toggle:hover{color:var(--accent)}
.fold{display:none;margin-top:10px}
.fold.open{display:block}
.raw .rq{margin:0 0 8px;color:var(--text);text-wrap:pretty}
.raw .ra{color:var(--muted);text-wrap:pretty}
.raw b,.thread b{color:var(--accent);font-weight:600;margin-right:5px}
.ra strong,.a strong,.rq strong{color:var(--text);font-weight:650}
.ra .hd,.a .hd{display:block;color:var(--text);font-weight:650;margin:9px 0 2px}
.ra .code,.a .code{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:12px;
  background:var(--surface2);padding:9px 12px;border-radius:var(--radius-sm);overflow-x:auto;white-space:pre;margin:7px 0}
.ra code,.a code,.rq code{font-family:ui-monospace,Consolas,monospace;font-size:.9em;
  background:var(--surface2);padding:1px 5px;border-radius:4px}
.actions{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:12px;
  color:var(--text);background:var(--surface2);padding:9px 12px;border-radius:var(--radius-sm);
  overflow-x:auto;white-space:pre}
.thread{border-left:2px solid var(--border);padding-left:13px}
.titem{margin:8px 0}
.tq{cursor:pointer;font-size:12.5px;color:var(--muted);text-wrap:pretty;transition:color .12s}
.tq:hover{color:var(--text)}
.ta{font-size:12.5px;color:var(--muted);margin-top:5px;padding-left:11px;
  border-left:2px solid var(--border);text-wrap:pretty}

/* 상태(빈/로딩/결과없음) */
.empty{text-align:center;padding:52px 20px;color:var(--muted)}
.empty .big{font-size:34px;line-height:1;margin-bottom:14px;opacity:.85}
.empty .msg{font-size:14px;margin-bottom:18px}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
.chip{padding:6px 13px;border-radius:20px;border:1px solid var(--border);background:var(--surface);
  color:var(--text);font-size:12.5px;cursor:pointer;box-shadow:var(--shadow);
  transition:border-color .15s,color .15s,transform .1s}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip:active{transform:scale(.96)}
.skel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px;box-shadow:var(--shadow)}
.skel .ln{height:11px;border-radius:6px;background:var(--surface2);margin:9px 0;animation:pulse 1.2s ease-in-out infinite}
.skel .ln.w1{width:38%}.skel .ln.w2{width:88%}.skel .ln.w3{width:66%}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

/* 세션 전체 보기 오버레이 */
.overlay{position:fixed;inset:0;z-index:100;background:var(--bg);overflow-y:auto;display:none}
.overlay.open{display:block}
.ov-head{position:sticky;top:0;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--border);
  padding:15px 22px;display:flex;align-items:center;gap:14px;z-index:1}
.ov-head .close{cursor:pointer;color:var(--accent);font-size:13.5px;font-weight:600;user-select:none}
.ov-head .close:hover{opacity:.75}
.ov-head .t{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.ov-body{max-width:880px;margin:0 auto;padding:18px 22px 96px;display:flex;flex-direction:column;gap:11px}
.sturn{border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;background:var(--surface);box-shadow:var(--shadow)}
.st-time{font-size:11px;color:var(--muted);margin-bottom:6px;font-variant-numeric:tabular-nums}
.st-head{font-size:14px;font-weight:600;line-height:1.55;margin:0;text-wrap:pretty}
.a{color:var(--muted);cursor:pointer;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.a.open{-webkit-line-clamp:unset;display:block}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header class="appbar">
    <div class="brand"><span class="logo">E</span><h1>Vestige</h1></div>
    <div class="bar-right">
      <span class="stats" id="stats"></span>
      <button id="themeBtn" class="icon-btn" aria-label="라이트/다크 테마 전환" title="테마 전환">◐</button>
    </div>
  </header>
  <div class="bar">
    <div class="searchbox">
      <svg class="search-ico" width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
        <circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>
      </svg>
      <input type="search" id="q" placeholder="대화 검색…  예: 급여 계산 · STAGE1 · 신선도 감쇠"
             autofocus autocomplete="off" spellcheck="false">
    </div>
    <div class="opts">
      <div class="slider" id="modeSlider" data-on="0" title="검색 모드">
        <div class="thumb"></div><span class="opt">🔀 하이브리드</span><span class="opt">🧠 의미만</span>
      </div>
      <div class="slider" id="dispSlider" data-on="0" title="표시 방식">
        <div class="thumb"></div><span class="opt">📝 정제 우선</span><span class="opt">📄 원문 우선</span>
      </div>
      <label>표시 <select id="k"><option>5</option><option selected>8</option><option>15</option></select></label>
      <span style="opacity:.65"><kbd>Enter</kbd></span>
      <span class="spacer"></span>
      <div class="dategrp">
        <label>이후 <input type="date" id="since"></label>
        <label>이전 <input type="date" id="until"></label>
        <button type="button" class="clr" id="clrDate" title="이후/이전 날짜 필터를 모두 지웁니다">초기화</button>
      </div>
    </div>
  </div>
  <div class="resultbar" id="resultbar"></div>
  <div class="hits" id="hits"></div>
</div>
<div class="overlay" id="overlay"></div>
<script>
const $=s=>document.querySelector(s);
let semOnly=false, rawFirst=false;
function stats(){fetch('/api/stats').then(r=>r.json()).then(s=>{
  $('#stats').textContent=`세션 ${s.sessions} · 턴 ${s.turns} · 벡터 ${s.vectors} · 정제 ${s.enriched}`;}).catch(()=>{});}
function esc(t){return (t||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
// 저장 타임스탬프는 UTC(...Z). 보는 사람의 로컬(한국이면 KST) 시간으로 표시.
function fmtTime(ts){
  const d=new Date(ts);
  if(isNaN(d)) return (ts||'').slice(0,16).replace('T',' ');
  const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
// 원문 마크다운을 안전하게 렌더(HTML escape 후 알려진 서식만 변환).
function md(t){
  t=esc(t);
  t=t.replace(/```([\s\S]*?)```/g,(m,c)=>`<div class="code">${c.replace(/^\n+|\n+$/g,'')}</div>`);
  t=t.replace(/`([^`]+)`/g,'<code>$1</code>');
  t=t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  t=t.replace(/^#{1,6}\s+(.+)$/gm,'<span class="hd">$1</span>');
  t=t.replace(/^\s*[-*]\s+(.+)$/gm,'· $1');
  t=t.replace(/\n/g,'<br>');
  return t;
}
function tog(el){el.nextElementSibling.classList.toggle('open');}
window.tog=tog;

function card(h){
  const src=(h.sources||[]).map(s=>`<span class="badge ${s==='keyword'?'kw':''}">${s==='keyword'?'키워드':'의미'}</span>`).join('');
  const cos=h.cosine!=null?`cos ${h.cosine.toFixed(3)}`:'키워드';
  const meta=`<div class="meta">${src}<span>${cos}</span>· ${esc(fmtTime(h.timestamp))} · 세션 ${esc(h.session)}</div>`;
  const tags=(h.tags||[]).length?`<div class="tags">${h.tags.map(t=>`<span class="tag">#${esc(t)}</span>`).join('')}</div>`:'';
  // 검색 응답엔 답변·행동·앞뒤 턴이 없다(#268) - 전체는 '이 세션 전체 작업 보기'로 연다.
  const acts='', thread='';
  const sess=`<div class="toggle" onclick="openSession('${h.session_full}')">▸ 이 세션 전체 작업 보기 ↗</div>`;
  const rawFold=`<div class="toggle" onclick="tog(this)">▸ 원문 Q&amp;A</div>
    <div class="fold raw"><p class="rq"><b>Q</b>${md(h.question)||'(질문 없음)'}</p></div>`;

  let body;
  if(rawFirst){
    body=`<p class="headline">${esc(h.question)||'<span class="sub">(질문 없음)</span>'}</p>
      ${h.summary?`<div class="enrich"><span class="mk">📝</span>${esc(h.summary)}</div>`:''}${tags}`;
  }else{
    const head=h.summary?`<span class="mk">📝</span>${esc(h.summary)}`:`${esc(h.question)||'<span class="sub">(요약 없음)</span>'}`;
    body=`<p class="headline">${head}</p>${tags}${rawFold}`;
  }
  return `<div class="card">${meta}${body}${acts}${thread}${sess}</div>`;
}
async function openSession(sid){
  const ov=$('#overlay'); ov.classList.add('open'); document.body.style.overflow='hidden';
  ov.innerHTML='<div class="ov-head"><span class="close" onclick="closeSession()">← 검색으로</span><span class="t">불러오는 중…</span></div>';
  try{
    const r=await (await fetch('/api/session?id='+encodeURIComponent(sid))).json();
    const head=`<div class="ov-head"><span class="close" onclick="closeSession()">← 검색으로</span>`+
      `<span class="t">세션 ${esc(sid).slice(0,8)} · ${r.count}턴</span></div>`;
    const rows=r.turns.map((t,i)=>{
      const hd=t.summary?`<span class="mk">📝</span>${esc(t.summary)}`:(esc(t.question)||'<span class="sub">(요약 없음)</span>');
      const acts=(t.actions||[]).length?`<div class="toggle" onclick="tog(this)">▸ 행동(bash 등) ${t.actions.length}개</div><div class="fold actions">${esc(t.actions.join('\n'))}</div>`:'';
      return `<div class="sturn"><div class="st-time">#${i+1} · ${esc(fmtTime(t.timestamp))}</div>`+
        `<p class="st-head">${hd}</p>`+
        `<div class="toggle" onclick="tog(this)">▸ 원문 Q&amp;A</div>`+
        `<div class="fold raw"><p class="rq"><b>Q</b>${md(t.question)||'(질문 없음)'}</p><div class="ra"><b>A</b>${md(t.answer)||'—'}</div></div>`+
        `${acts}</div>`;
    }).join('');
    ov.innerHTML=head+'<div class="ov-body">'+rows+'</div>';
    ov.scrollTop=0;
  }catch(e){ ov.innerHTML=`<div class="ov-head"><span class="close" onclick="closeSession()">← 검색으로</span><span class="t">오류: ${e}</span></div>`; }
}
function closeSession(){const o=$('#overlay');o.classList.remove('open');o.innerHTML='';document.body.style.overflow='';}
window.openSession=openSession; window.closeSession=closeSession;
const EXAMPLES=['급여 계산','STAGE1 우회','마이그레이션','sqlite-vec','정제 백엔드'];
let hits=[], searched=false;
function renderEmpty(){
  $('#resultbar').textContent='';
  $('#hits').innerHTML=`<div class="empty"><div class="big">🔎</div>
    <div class="msg">대화에서 찾을 내용을 입력하세요.</div>
    <div class="chips">${EXAMPLES.map(e=>`<span class="chip" onclick="pick('${e}')">${e}</span>`).join('')}</div></div>`;
}
function skeleton(){
  $('#hits').innerHTML=Array.from({length:3}).map(()=>
    '<div class="skel"><div class="ln w1"></div><div class="ln w2"></div><div class="ln w3"></div></div>').join('');
}
function render(){
  if(!hits.length){
    if(searched){
      $('#resultbar').innerHTML='결과 <b>0</b>개';
      $('#hits').innerHTML='<div class="empty"><div class="big">∅</div>'
        +'<div class="msg">결과가 없어요. 다른 표현이나 날짜 범위로 바꿔보세요.</div></div>';
    }else{ renderEmpty(); }
    return;
  }
  $('#resultbar').innerHTML=`결과 <b>${hits.length}</b>개`;
  $('#hits').innerHTML=hits.map(card).join('');
}
async function go(){
  const q=$('#q').value.trim();
  if(!q){searched=false;hits=[];renderEmpty();return;}
  searched=true; skeleton(); $('#resultbar').textContent='검색 중…';
  const p=new URLSearchParams({q,k:$('#k').value,semantic_only:semOnly});
  const since=$('#since').value, until=$('#until').value;
  if(since) p.set('since',since);
  if(until) p.set('until',until);
  try{const r=await (await fetch('/api/search?'+p)).json(); hits=r.hits||[]; render();}
  catch(e){$('#resultbar').textContent='';
    $('#hits').innerHTML='<div class="empty"><div class="msg">오류: '+esc(String(e))+'</div></div>';}
}
function pick(q){$('#q').value=q; go(); $('#q').focus();}
window.pick=pick;

// 테마: localStorage 우선, 없으면 OS(prefers-color-scheme) 따름.
function applyTheme(t){ if(t) document.documentElement.dataset.theme=t; else delete document.documentElement.dataset.theme; }
applyTheme(localStorage.getItem('cm-theme'));
$('#themeBtn').addEventListener('click',()=>{
  const cur=document.documentElement.dataset.theme;
  const dark = cur ? cur==='dark' : matchMedia('(prefers-color-scheme:dark)').matches;
  const next = dark ? 'light' : 'dark';
  applyTheme(next); localStorage.setItem('cm-theme',next);
});

$('#modeSlider').addEventListener('click',function(){semOnly=!semOnly;this.dataset.on=semOnly?'1':'0';go();});
$('#dispSlider').addEventListener('click',function(){rawFirst=!rawFirst;this.dataset.on=rawFirst?'1':'0';render();});
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
$('#k').addEventListener('change',go);
$('#since').addEventListener('change',go);
$('#until').addEventListener('change',go);
$('#clrDate').addEventListener('click',()=>{$('#since').value='';$('#until').value='';go();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSession();});
stats(); renderEmpty();
</script>
</body>
</html>"""


# 빌드된 프론트의 정적 자산(/assets/*.js, *.css, 폰트). API 라우트 뒤에 마운트.
if (_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")


def main() -> None:
    import uvicorn

    print("Vestige 웹 UI → http://127.0.0.1:8642  (모델 로딩 ~15초)")
    uvicorn.run(app, host="127.0.0.1", port=8642, log_level="warning")


if __name__ == "__main__":
    main()
