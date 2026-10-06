"""Codex rollout 포맷 드리프트 카나리.

목적: Codex CLI 가 rollout JSONL 스키마를 바꿔 CodexAdapter 가 조용히 대화를 놓치는 일을
개발자가 (사용자보다 먼저) 알아채게 한다.

두 모드:
- ``--remote`` (기본, CI용): npm 레지스트리의 최신 ``@openai/codex`` 버전을 확인해
  검증 완료 버전(``TESTED_VERSION``)보다 새로우면 신호를 낸다. 새 버전이 반드시 포맷 변경은
  아니지만 "가서 확인하라"는 넛지. GitHub Actions 가 이 신호로 이슈를 자동 생성한다.
  (CI 는 인증이 없어 실제 Codex 세션을 만들 수 없으므로 원격은 '버전 감시'만 한다.)
- ``--local`` (개발자 기기용): ``~/.codex/sessions`` 의 실제 로그를 CodexAdapter 로 돌려
  '레코드(event_msg)는 있는데 턴 0개'(=포맷 깨짐 의심) 파일을 보고한다. 실데이터 검증은 여기서.

검증한 새 버전이 생기면 ``TESTED_VERSION`` 을 올려 알림을 멈춘다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# rollout 포맷을 검증(픽스처/실데이터)한 최신 codex 버전(= npm ``@openai/codex`` 버전).
# 새 버전 포맷을 확인하고 필요한 어댑터 케이스·픽스처를 반영했으면 이 값을 올릴 것.
#
# 주의: npm 패키지 버전과 rollout ``session_meta.cli_version`` 은 번호 체계가 갈라져 있다.
#   예) 설치 CLI = npm 0.154.0 인데 rollout 은 ``cli_version: 0.149.1`` 로 스탬프됨.
#   원격 감시는 npm 버전을, 실제 포맷 검증(--local)은 rollout 을 본다. 그래서 npm 이 올라가도
#   로그 포맷은 그대로일 수 있다(#136: npm 0.154.0 로 만든 로그를 CodexAdapter 가 정상 처리 확인).
#   0.160.1 부터는 rollout 도 ``cli_version: 0.160.1`` 로 npm 과 같다(#232 #263: 질문·답변·실행된 명령
#   ``CommandExecution`` 정상 추출, 새 줄 종류 ``world_state``·``token_usage_record`` 는 대화가 아니라 무시).
TESTED_VERSION = "0.160.1"
NPM_LATEST_URL = "https://registry.npmjs.org/@openai/codex/latest"


def _parse(version: str) -> tuple[int, int, int]:
    """'0.149.0' / '0.150.0-alpha.2' → (0,149,0). 프리릴리스 접미는 무시."""
    core = str(version).split("-", 1)[0]
    parts: list[int] = []
    for p in core.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def is_newer(latest: str, tested: str) -> bool:
    return _parse(latest) > _parse(tested)


def fetch_latest_version(url: str = NPM_LATEST_URL, timeout: int = 20) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (고정 https URL)
        data = json.load(resp)
    return data["version"]


def _emit_output(**kv: str) -> None:
    """GitHub Actions step output 으로 값 전달(있을 때만)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def remote_check() -> int:
    try:
        latest = fetch_latest_version()
    except Exception as ex:  # noqa: BLE001 — 네트워크 실패가 워크플로를 죽이지 않게(경고만)
        print(f"WARN: 최신 codex 버전 조회 실패: {ex}")
        _emit_output(new_version="false", latest="", tested=TESTED_VERSION)
        return 0
    new = is_newer(latest, TESTED_VERSION)
    print(f"latest={latest} tested={TESTED_VERSION} new_version={'true' if new else 'false'}")
    _emit_output(new_version="true" if new else "false", latest=latest, tested=TESTED_VERSION)
    return 0


def local_check(root: str | Path | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from vestige.sources.codex import CodexAdapter

    root = Path(root) if root else (Path.home() / ".codex" / "sessions")
    if not root.exists():
        print(f"codex 세션 폴더 없음: {root}")
        return 0

    adapter = CodexAdapter()
    suspects: list[str] = []
    checked = 0
    for f in root.rglob("rollout-*.jsonl"):
        checked += 1
        recs = [o for o, _e in adapter.read_records(f, 0)]
        starts = [i for i, o in enumerate(recs) if adapter.is_turn_start(o)]
        turns = 0
        for k, si in enumerate(starts):
            sj = starts[k + 1] if k + 1 < len(starts) else len(recs)
            turns += len(adapter.extract_turns(recs[si:sj]))
        has_event = any(o.get("type") == "event_msg" for o in recs)
        # event_msg 는 있는데(=대화가 오간 파일) 턴이 0개면 포맷 변경 의심.
        if has_event and turns == 0:
            suspects.append(str(f))

    if suspects:
        print(f"DRIFT SUSPECTED: {len(suspects)}개 파일에 event_msg 는 있으나 턴 0개(포맷 변경 가능성)")
        for s in suspects[:20]:
            print(f"  {s}")
        return 1
    print(f"local check OK: {checked}개 파일 점검, 드리프트 의심 없음")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codex rollout 포맷 드리프트 카나리")
    ap.add_argument("--local", action="store_true", help="~/.codex/sessions 실데이터로 드리프트 자가점검")
    args = ap.parse_args(argv)
    return local_check() if args.local else remote_check()


if __name__ == "__main__":
    raise SystemExit(main())
