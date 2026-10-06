#!/usr/bin/env bash
# vestige 백엔드 사이드카 빌드 (Electron 데스크탑 앱에 동봉).
#
# FastAPI + fastembed(onnxruntime·tokenizers 네이티브)를 단일 폴더 exe로 번들한다.
# 기본·권장 모델(int8 e5-large, 0.52GB 디스크 / 로딩 시 약 2GB RAM)은 아래에서 생성해 동봉 → 설치 즉시 오프라인 동작.
#   (선택 옵션인 MiniLM 등 다른 모델만 첫 사용 시 다운로드/캐시)
# 결과: dist/vestige-backend/  (onedir; Electron이 이 폴더를 resources로 포함)
#
# 사전: pip install ".[all]" pyinstaller onnx
#   ("onnx"는 아래 make_int8(양자화)에만 필요. 이미 생성된 e5int8 폴더가 있으면 불필요)
# 크로스플랫폼: 각 OS에서 그 OS로 실행해야 함(Win→.exe, mac→mach-o, linux→elf).
set -e
cd "$(dirname "$0")/.."

# 프론트가 빌드돼 있어야 함(백엔드가 이 dist를 / 에서 서빙). 없으면 빌드.
if [ ! -f frontend/dist/index.html ]; then
  echo "frontend 빌드 중…"; (cd frontend && npm run build)
fi

# --add-data 경로 구분자: Windows=';', Unix=':'
SEP=":"
case "${OS:-}${OSTYPE:-}" in *Windows*|*msys*|*cygwin*) SEP=";" ;; esac

# 기본·권장 모델(int8 e5-large, 0.52GB 디스크 / 로딩 시 약 2GB RAM)을 생성해 동봉 → 설치 즉시 오프라인 동작(첫 실행 다운로드 없음).
if [ ! -f packaging/build/e5int8/model.onnx ]; then
  echo "int8 e5-large 생성 중…"; python packaging/make_int8.py packaging/build/e5int8
fi

# pywebview(webview)는 뺀다: Electron 사이드카는 창을 열지 않는데 `--collect-submodules vestige` 가
# desktop.py 의 import 를 따라 pywebview·pythonnet(약 5MB)을 끌고 왔다. 그중 win-arm64 WebView2Loader.dll 은
# NSIS 압축 해제기가 못 풀어 설치 때마다 조용히 빠졌다(설치 파일 백엔드 1556개 vs 설치 후 1555개).
# `vestige-backend app`(pywebview 창)은 '필요합니다' 안내로 끝난다 - 데스크탑 앱은 Electron 이 창이다.
pyinstaller --noconfirm --onedir --name vestige-backend \
  --noconsole \
  --paths . \
  --add-data "packaging/build/e5int8${SEP}e5int8" \
  --collect-all fastembed \
  --collect-all onnxruntime \
  --collect-all tokenizers \
  --collect-all huggingface_hub \
  --collect-all uvicorn \
  --collect-all sqlite_vec \
  --collect-submodules mcp.server \
  --collect-submodules mcp.shared \
  --collect-submodules vestige \
  --exclude-module webview \
  --add-data "frontend/dist${SEP}frontend/dist" \
  packaging/backend_entry.py

echo ""
echo "완료: dist/vestige-backend/vestige-backend.exe  (인자=포트, 예: vestige-backend.exe 8765)"
