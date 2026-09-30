"""테스트 전체를 실사용 데이터에서 떼어놓는다.

vestige.config 는 **import 시점에** 환경변수로 경로를 정한다(DATA_DIR, CONFIG_PATH …). 그래서
경로를 안 바꾼 테스트가 ArchiveDB() 를 인자 없이 부르면 사용자의 진짜 archive.db 를 연다 —
열기만 해도 마이그레이션이 돈다. 실제로 테스트 5개 파일이 그렇게 실사용 DB 를 작업 트리의
미커밋 마이그레이션까지 올려버렸다(데이터 행은 안 썼지만, 잘못된 마이그레이션이었다면 그대로
적용됐을 것이다).

테스트마다 monkeypatch 로 막는 방식은 빠뜨리면 끝이다. 여기서 한 번에 막는다. pytest 는
conftest 를 테스트 모듈보다 먼저 읽으므로 이 대입이 vestige import 보다 앞선다.
"""
import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="vestige-test-"))

# 덮어쓴다(setdefault 아님) — 사용자가 셸에 실경로를 export 해 뒀어도 테스트는 거기 닿지 않게.
os.environ["VESTIGE_DATA_DIR"] = str(_ROOT / "data")
os.environ["VESTIGE_RAW_ARCHIVE_DIR"] = str(_ROOT / "data" / "raw")
os.environ["VESTIGE_CONFIG"] = str(_ROOT / "config.env")
# 대화 로그 루트도 뗀다. 동기화 export 는 이 아래(.vestige-archive/)에 쓰고, 그 폴더는
# Syncthing 으로 다른 기기에 퍼진다.
os.environ["CLAUDE_PROJECTS_DIR"] = str(_ROOT / "projects")
os.environ["CODEX_SESSIONS_DIR"] = str(_ROOT / "codex")
