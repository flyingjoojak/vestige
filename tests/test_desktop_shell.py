"""Electron 셸·설치 스크립트의 계약 검사(#229).

JS 테스트 러너가 없어 소스에 대고 확인한다. 둘 다 '빠뜨려도 아무 에러가 안 나고,
사용자 기기에서만 조용히 앱이 죽는' 종류라 자동 검사가 값을 한다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_JS = ROOT / "electron" / "main.js"
INSTALLER_NSH = ROOT / "electron" / "installer.nsh"


def test_backend_spawn_has_error_handler():
    """spawn 실패(ENOENT)는 exit 가 아니라 error 로 온다.

    핸들러가 없으면 Node 가 uncaught exception 으로 올려 Electron 이
    'A JavaScript error occurred in the main process' 만 띄우고 끝난다 -
    앱이 켜지지 않으니 사용자가 앱 안에서 할 수 있는 게 없다(#229 실제 증상).
    """
    src = MAIN_JS.read_text(encoding="utf-8")
    assert 'backend.on("error"' in src, "백엔드 spawn 에 error 핸들러가 없다"
    assert "showBrokenInstall" in src, "설치 손상 시 사용자에게 알리는 경로가 없다"


def test_backend_existence_checked_before_spawn():
    """사이드카가 없으면 spawn 하지 않고 안내한다. 순서가 뒤집히면 검사가 무의미하다."""
    src = MAIN_JS.read_text(encoding="utf-8")
    guard = src.index("fs.existsSync(cmd)")
    spawn_at = src.index("backend = spawn(")
    assert guard < spawn_at, "존재 확인이 spawn 뒤에 있다"


def test_installer_pops_nsexec_results():
    """nsExec::Exec 는 종료 코드를 NSIS 스택에 push 한다.

    Pop 으로 비우지 않으면 값이 쌓여 이후 electron-builder 자신의 NSIS 코드가
    스택에서 엉뚱한 값을 집는다. 설치가 조용히 어긋나는 경로다.
    """
    src = INSTALLER_NSH.read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith(";"))
    assert body.count("nsExec::Exec") == len(re.findall(r"^\s*Pop\s+\$", body, re.M)), \
        "nsExec::Exec 호출 수와 Pop 수가 다르다"


def test_installer_does_not_tree_kill_the_app():
    """`taskkill /T` 는 프로세스 트리 전체를 죽인다.

    업데이트 설치 관리자는 quitAndInstall() 이 띄운 것이라 Vestige.exe 의 자손일 수 있다.
    그 트리를 죽이면 설치 관리자가 자기 자신을 죽여 설치가 중간에 끊긴다
    (#229 에서 backend 폴더만 비워진 채 끝났다).
    """
    body = "\n".join(l for l in INSTALLER_NSH.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith(";"))
    assert "/IM Vestige.exe" not in body, "설치 관리자가 자기 부모 트리를 죽일 수 있다"
    assert "/T" not in body, "트리 종료(/T)는 설치 관리자 자신을 포함할 수 있다"


BROWSE_TSX = ROOT / "frontend" / "src" / "components" / "Browse3Pane.tsx"


def test_decorative_row_elements_do_not_eat_clicks():
    """세션 목록 행의 장식 요소는 클릭을 가로채면 안 된다.

    행 전체를 클릭 영역으로 쓰려고 button 에 `after:inset-0` 오버레이를 깐다. 그런데
    `transform`(예: group-hover:translate-x)이 붙은 형제는 stacking context 를 만들어
    그 오버레이 **위로** 올라간다. 결과: 마우스를 올린 상태 - 즉 누르려는 바로 그때 -
    오른쪽 화살표가 클릭을 삼켜 세션이 안 열렸다(v0.3.1 부터 있던 버그).

    JS 테스트 러너가 없어 소스로 확인한다. 눈으로만 보면 '가끔 안 눌린다'로 끝나고
    원인에 도달하기 어려운 종류라 자동 검사가 값을 한다.
    """
    src = BROWSE_TSX.read_text(encoding="utf-8")
    hover_moved = [ln for ln in src.splitlines()
                   if "group-hover:translate-x" in ln and "className=" in ln]
    assert hover_moved, "대상을 못 찾았다 - 검사가 낡았는지 확인할 것"
    for ln in hover_moved:
        assert "pointer-events-none" in ln, f"transform 장식이 클릭을 가로챈다: {ln.strip()}"
