"""정제: 세션 단위로 턴 요약·태그 생성. 원문 불변, 정제본(summary/tags)만 부가.

정제 LLM 호출은 **백엔드 플러그블**:
- "claude"    : Claude Code 구독(`claude -p`). 토큰당 $ 없음, 쿼터 소모. (기본)
- "anthropic" : Anthropic API(`ANTHROPIC_API_KEY`) + 공식 SDK. 토큰당 과금.
- "off"       : 정제 안 함. 시스템은 정제 없이도 완전 동작.

세션 단위 1회 호출(턴 많으면 작은 배치). 실패 세션은 건너뛰고 로그만 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .models import Turn

logger = logging.getLogger(__name__)

# windowed(콘솔 없는) exe에서 claude CLI 서브프로세스가 콘솔 창을 띄우지 않게(Windows 전용 플래그).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _claude_search_dirs() -> list[str]:
    """claude CLI 가 흔히 설치되는 위치들(PATH 미상속 GUI 앱용).

    macOS Finder/Dock 로 띄운 앱은 셸 PATH(~/.zshrc 등)를 물려받지 못해 `which claude` 가
    실패한다 → 아래 표준 위치들을 직접 확인한다. 순서 = 우선순위.
    """
    home = Path.home()
    return [
        "/opt/homebrew/bin",                 # macOS Apple Silicon Homebrew
        "/usr/local/bin",                    # macOS Intel Homebrew / 수동 설치
        str(home / ".local/bin"),            # 네이티브 인스톨러
        str(home / ".claude/local"),         # Claude Code 로컬 설치
        str(home / ".npm-global/bin"),       # npm 전역(사용자)
        "/usr/bin",
    ]


def resolve_claude_bin() -> str | None:
    """claude 실행파일 경로 해석: (1) VESTIGE_CLAUDE_BIN 명시 → (2) PATH → (3) 표준 설치 위치.

    반환은 실행 가능한 절대/명령 경로, 못 찾으면 None. 설정 즉시 반영을 위해 호출 시점에 조회한다.
    """
    override = os.environ.get("VESTIGE_CLAUDE_BIN", "").strip()
    if override:
        # 파일 경로면 존재 확인, 아니면 PATH 에서 그 이름을 찾음(둘 다 허용).
        if Path(override).is_file() or shutil.which(override):
            return override
        logger.warning("VESTIGE_CLAUDE_BIN=%r 를 찾을 수 없음 → 자동 탐색으로 폴백", override)
    found = shutil.which("claude")            # 터미널/개발 실행은 보통 여기서 잡힘
    if found:
        return found
    name = "claude.exe" if os.name == "nt" else "claude"
    for d in _claude_search_dirs():           # GUI 앱: 표준 위치 직접 확인
        p = Path(d) / name
        if p.is_file():
            return str(p)
    return None


def _claude_env(bin_path: str) -> dict:
    """claude 서브프로세스용 환경: 실행파일 폴더(+표준 bin)를 PATH 앞에 붙인다.

    claude 는 내부적으로 node 등을 PATH 에서 찾으므로, PATH 를 못 물려받은 GUI 앱에서 절대경로로
    실행할 때 실행파일과 같은 폴더(예: /opt/homebrew/bin 의 node)를 PATH 에 넣어줘야 동작한다.
    """
    env = os.environ.copy()
    extra = [str(Path(bin_path).parent), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    return env

# 창이 크면 LLM이 긴 JSON 목록에서 일부 턴을 누락함 → 작게 잡아 커버리지 확보.
_MAX_TURNS_PER_CALL = 20
_FIELD_CHARS = 400

# OpenAI 호환 백엔드 프리셋 — SDK 하나(openai)로 base_url·키·모델만 다르게.
# ollama는 키 불필요(로컬), gemini는 Google의 OpenAI 호환 엔드포인트.
# 설정 변경(모델·Ollama URL)이 재시작 없이 반영되도록 **호출 시점에 config를 읽는다**(import 고정 X).
def _presets() -> dict:
    from . import config as C
    return {
        "openai": {"base_url": None, "key_envs": ["OPENAI_API_KEY"], "default_model": C.ENRICH_OPENAI_MODEL},
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                   "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "default_model": C.ENRICH_GEMINI_MODEL},
        "ollama": {"base_url": C.ENRICH_OLLAMA_URL, "key_envs": [], "default_model": C.ENRICH_OLLAMA_MODEL},
    }


def _build_prompt(turns: list[Turn]) -> str:
    head = [
        # sentinel: claude -p 는 세션 로그를 남기므로 이 프롬프트가 새 세션으로 기록된다.
        # 파서가 이 접두를 보고 인덱싱에서 제외한다(자기오염 방지).
        "<<CHATMEM-ENRICH>> vestige enrichment task (auto-generated — excluded from indexing).",
        "Below are the conversation turns of one AI coding session.",
        "For each turn, produce a one-line summary and 2-4 tags.",
        # 출력 언어를 UI가 아니라 '대화 언어'에 맞춘다 → 한국어 대화는 한국어, 영어 대화는 영어 요약.
        # (외국인 사용자의 대화가 강제로 한글 요약이 되던 문제 해결)
        "Write the summary and tags in the SAME LANGUAGE as that turn's conversation "
        "(Korean turns -> Korean, English turns -> English, and so on).",
        "Output ONLY JSON in exactly this form — no prose, no greetings, no code fences:",
        '{"turns":[{"id":"<copy verbatim>","summary":"one line","tags":["tag","tag"]}]}',
        "",
        "=== TURNS ===",
    ]
    for t in turns:
        q = " ".join(t.question.split())[:_FIELD_CHARS]
        a = " ".join(t.answer.split())[:_FIELD_CHARS]
        acts = t.action_summary()[:150]
        block = f"[id={t.id}]\nQ: {q}\nA: {a}"
        if acts:
            block += f"\nActions: {acts}"
        head.append(block)
    return "\n".join(head)


# --- 백엔드 ------------------------------------------------------------
def _call_claude_cli(prompt: str, model: str, timeout: int = 240) -> str:
    bin_path = resolve_claude_bin()
    if not bin_path:
        raise RuntimeError(
            "claude CLI 를 찾을 수 없습니다 — Claude Code 설치, 또는 VESTIGE_CLAUDE_BIN 로 경로 지정")
    # 프롬프트에는 대화 원문이 들어간다 - 동기화로 들어온 다른 기기 기록일 수도 있다. 거기 심어진
    # 지시를 따르더라도 할 수 있는 게 없게: 빈 임시 폴더에서, 도구 없이(요약은 글만 쓰면 된다).
    import tempfile
    with tempfile.TemporaryDirectory(prefix="vestige-enrich-") as cwd:
        r = subprocess.run(
            [bin_path, "-p", prompt, "--model", model, "--tools", ""],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=_NO_WINDOW, env=_claude_env(bin_path), cwd=cwd,
        )
    if r.returncode != 0:
        raise RuntimeError(f"claude -p 실패(rc={r.returncode}): {(r.stderr or '')[:200]}")
    return (r.stdout or "").strip()


def _call_anthropic_api(prompt: str, model: str, max_tokens: int = 4096) -> str:
    try:
        import anthropic  # 선택적 의존성
    except ImportError as e:
        raise RuntimeError("anthropic 패키지가 필요합니다: pip install anthropic") from e
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _openai_key_for(preset: dict) -> str | None:
    for env in preset["key_envs"]:
        if os.environ.get(env):
            return os.environ[env]
    return None


def _call_openai_compatible(prompt: str, model: str, base_url: str | None,
                            api_key: str | None, max_tokens: int = 4096) -> str:
    """OpenAI 호환 API 호출(OpenAI/Gemini/Ollama/LM Studio/vLLM 등 공통)."""
    try:
        from openai import OpenAI  # 선택적 의존성
    except ImportError as e:
        raise RuntimeError("openai 패키지가 필요합니다: pip install openai") from e
    client = OpenAI(api_key=api_key or "not-needed", base_url=base_url)
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


def _resolve_model(backend: str, model: str | None) -> str:
    if model:
        return model
    from . import config as C   # 설정 변경 즉시 반영(import 고정 X)
    if backend == "claude":
        return C.ENRICH_CLI_MODEL
    if backend == "anthropic":
        return C.ENRICH_API_MODEL
    presets = _presets()
    if backend in presets:
        return presets[backend]["default_model"]
    return model or ""


def _generate(prompt: str, backend: str, model: str) -> str:
    if backend == "claude":
        return _call_claude_cli(prompt, model)
    if backend == "anthropic":
        return _call_anthropic_api(prompt, model)
    if backend in _presets():
        p = _presets()[backend]
        return _call_openai_compatible(prompt, model, p["base_url"], _openai_key_for(p))
    raise RuntimeError(f"알 수 없는 정제 백엔드: {backend}")


def _short(e: Exception) -> str:
    s = str(e).replace("\n", " ")
    return s[:160] if s else e.__class__.__name__


def verify_backend(backend: str, model: str | None = None, api_key: str | None = None,
                   base_url: str | None = None) -> tuple[bool, str]:
    """실제 연결 검증(무료·가벼운 models.list 호출). (ok, 메시지).

    api_key/base_url이 주어지면 그것으로, 없으면 환경변수/프리셋으로 검증.
    """
    if backend == "off":
        return True, "정제 안 함"
    if backend == "claude":
        if resolve_claude_bin() is None:
            return False, "claude CLI 없음 (Claude Code 설치, 또는 VESTIGE_CLAUDE_BIN 로 경로 지정)"
        return True, "claude CLI 확인됨"
    if backend == "anthropic":
        try:
            import anthropic
        except ImportError:
            return False, "anthropic 패키지 없음 (pip install anthropic)"
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return False, "API 키 없음"
        try:
            anthropic.Anthropic(api_key=key).models.list()
            return True, "연결 확인됨"
        except Exception as e:  # noqa: BLE001
            return False, _short(e)
    if backend in _presets():
        p = _presets()[backend]
        url = base_url or p["base_url"]
        key = api_key or _openai_key_for(p) or ("not-needed" if backend == "ollama" else None)
        if key is None:
            return False, "API 키 없음"
        try:
            from openai import OpenAI
        except ImportError:
            return False, "openai 패키지 없음 (pip install openai)"
        try:
            OpenAI(api_key=key, base_url=url).models.list()
            return True, "연결 확인됨"
        except Exception as e:  # noqa: BLE001
            if backend == "ollama":
                return False, f"Ollama 연결 실패 — 실행 중인지 확인: {_short(e)}"
            return False, _short(e)
    return False, f"알 수 없는 백엔드: {backend}"


def backend_available(backend: str) -> tuple[bool, str]:
    """백엔드가 실제로 쓸 수 있는지 + 안 되면 이유(사용자 안내용)."""
    if backend == "off":
        return False, "정제 비활성화(off)"
    if backend == "claude":
        if resolve_claude_bin() is None:
            return False, ("claude CLI 없음 — Claude Code 설치, VESTIGE_CLAUDE_BIN 로 경로 지정, "
                           "또는 VESTIGE_ENRICH_BACKEND=anthropic")
        return True, ""
    if backend == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic 패키지 없음 — pip install anthropic"
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY 미설정"
        return True, ""
    if backend in _presets():
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "openai 패키지 없음 — pip install openai"
        p = _presets()[backend]
        if p["key_envs"] and _openai_key_for(p) is None:
            return False, f"{'/'.join(p['key_envs'])} 미설정"
        return True, ""  # ollama는 키 불필요(단, 로컬 서버가 떠 있어야 함)
    return False, f"알 수 없는 백엔드: {backend}"


def _parse_json(out: str) -> list[dict]:
    s = out.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            s = s.split("\n", 1)[1]
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i : j + 1]
    data = json.loads(s)
    return data.get("turns", []) if isinstance(data, dict) else []


# --- 정제 ------------------------------------------------------------
def enrich_session(session_id: str, db, backend: str | None = None,
                   model: str | None = None, missing_only: bool = True) -> int:
    from . import config as C   # 기본값도 런타임 읽기(재시작 없이 설정 반영)
    backend = backend or C.ENRICH_BACKEND
    model = _resolve_model(backend, model)
    # 증분: missing_only면 아직 요약 없는 턴만 처리(재실행 시 구멍만 채움).
    where = "session_id=?" + (" AND summary IS NULL" if missing_only else "")
    rows = db.conn.execute(
        f"SELECT id FROM turns WHERE {where} ORDER BY timestamp, id", (session_id,)
    ).fetchall()
    turns = [t for t in (db.get_turn(r["id"]) for r in rows) if t]
    if not turns:
        return 0
    done = 0
    for i in range(0, len(turns), _MAX_TURNS_PER_CALL):
        batch = turns[i : i + _MAX_TURNS_PER_CALL]
        out = _generate(_build_prompt(batch), backend, model)
        for item in _parse_json(out):
            tid = item.get("id")
            if not tid:
                continue
            tags = item.get("tags", [])
            if not isinstance(tags, list):
                logger.warning("enrich: LLM이 tags를 리스트 아닌 %s로 반환(id=%s) → 빈 태그로 저장",
                               type(tags).__name__, tid)
                tags = []
            # 매칭되는 turn이 있을 때만 완료로 카운트. 0행(=LLM이 긴 turn id를 잘못 복사)을 done으로 세면
            # summary IS NULL 필터가 그 턴을 계속 재선택 → 무한 재시도로 쿼터만 태운다.
            if db.set_enrichment(tid, item.get("summary", ""), tags):
                done += 1
            else:
                logger.warning("enrich: id=%s 가 어떤 turn과도 매칭 안 됨 → 건너뜀(재시도 루프 방지)", tid)
        db.commit()
    return done


def enrich_all(db, backend: str | None = None, model: str | None = None,
               throttle: float = 1.0, only_missing: bool = True,
               limit: int | None = None, log_fn=print, progress_fn=None) -> int:
    from . import config as C   # 기본값도 런타임 읽기(재시작 없이 설정 반영)
    backend = backend or C.ENRICH_BACKEND
    if only_missing:
        rows = db.conn.execute(
            "SELECT DISTINCT session_id FROM turns WHERE summary IS NULL"
        ).fetchall()
    else:
        rows = db.conn.execute("SELECT DISTINCT session_id FROM turns").fetchall()
    sessions = [r["session_id"] for r in rows]
    if limit:
        sessions = sessions[:limit]

    total_sessions = len(sessions)
    total = 0
    for i, sid in enumerate(sessions):
        try:
            want = db.conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id=? AND summary IS NULL" if only_missing
                else "SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)).fetchone()[0]
            n = enrich_session(sid, db, backend=backend, model=model, missing_only=only_missing)
            total += n
            log_fn(f"enriched {n} turns  session {sid[:8]}")
            if want and n == 0:
                # 모델이 형식에 안 맞게 답했거나 턴 id 를 잘못 베꼈다 - 예전엔 '완료'로만 보였다
                log_fn(f"ERROR enrich {sid[:8]}: 요약을 하나도 받지 못했다({want}턴) - 다음 정제 때 다시 시도")
        except Exception as ex:  # 한 세션 실패가 전체를 막지 않도록
            log_fn(f"ERROR enrich {sid[:8]}: {ex}")
        if progress_fn:
            try:
                progress_fn(i + 1, total_sessions)
            except Exception:  # noqa: BLE001 — 진행 콜백 오류가 정제를 막지 않게
                pass
        time.sleep(throttle)
    return total
