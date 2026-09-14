<div align="center">

<img src="docs/assets/banner.png" alt="Vestige" width="100%">

### AI는 세션이 끝나면 다 잊어버립니다. 이제 당신은 그러지 않아도 됩니다.

[![License: MIT](https://img.shields.io/badge/license-MIT-10b981.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-1f2937)
![Local & Offline](https://img.shields.io/badge/100%25-local%20%26%20offline-10b981)
![Built with](https://img.shields.io/badge/Python%20·%20React%20·%20Electron-47848F)

[English](README.en.md) · **한국어**

</div>

**Claude Code**와 **Codex**로 지금까지 수백 개의 문제를 풀었을 겁니다 - 그 골치 아픈 async 버그,
겨우 맞춘 Docker 설정, 드디어 먹힌 그 프롬프트. 그런데 세션을 닫는 순간 전부 사라집니다.
다음에 필요하면 기록을 하염없이 스크롤하거나, 그냥 처음부터 다시 물어보죠.

**Vestige은 당신의 AI에게 없는 장기 기억입니다.** 모든 대화를 내 컴퓨터에 백그라운드로 보관하고,
그중 무엇이든 몇 초 만에 찾아줍니다 - 정확한 단어가 아니라 **의미로**.

<div align="center">
  <img src="docs/assets/search.ko.png" alt="Vestige에서 과거 대화 검색 - 기억나는 대로 입력하면 정확한 답이 돌아온다" width="880">
</div>

<!-- 재생 영상도 넣고 싶으면 .mp4 를 GitHub 이슈/릴리스에 드래그 → github.com/user-attachments/... URL을
     이 자리에 한 줄로. (데모 데이터로 녹화 - docs/assets/README.md 참고.) -->

## 뭐가 나아지나요

| Vestige 없을 때 | Vestige 있을 때 |
|---|---|
| 분명 예전에 고쳤는데, 그 대화가 어디 갔는지 모릅니다. | **그 대화를 몇 초 만에 찾습니다.** |
| 같은 질문을 Claude에게 또 물어보며 토큰을 씁니다. | **이미 받았던 답을 다시 씁니다.** |
| 정확한 단어를 기억해야만 찾을 수 있습니다. | **어렴풋한 기억으로 검색합니다** - "그 깨지던 웹소켓 테스트 고친 거" 로 정확한 메시지를 찾습니다. |
| 기록이 수백 개 세션에 흩어져 있습니다. | **3D 지도 하나에서 주제별로 모아 봅니다.** |
| 클라우드 도구가 내 대화를 읽습니다. | **아무것도 기기를 벗어나지 않습니다.** 계정도, 클라우드도 없이. |

## 기능

- 🔍 **마음을 읽는 검색** - 의미로 찾아서, 어렴풋한 기억만으로도 정확한 메시지에 닿습니다(시맨틱 + 키워드 융합).
- ⚡ **즉시 회상** - Claude Code·Codex의 모든 대화가 검색창 하나에. 스크롤도, 다시 묻기도 끝.
- 🗺️ **내 작업의 지도** - 지금까지 한 모든 걸 주제별로 뭉쳐 보여주는 3D 뷰를 날아다니며 탐색.
- 🔒 **완전 로컬·오프라인** - 전부 내 컴퓨터에서. 계정·텔레메트리 없고, 비행기에서도 됩니다.
- ↔️ **여러 기기가 한 기억** - 노트북과 데스크탑이 P2P로 동기화(클라우드 없음).
- 🤖 **AI에게 기억을 돌려주기** - MCP로 Claude가 자기 과거 세션을 직접 검색.

## 실제 화면

**지금까지 나눈 모든 대화의 3D 지도** - 기록이 주제별로 뭉쳐, 날아다니며 탐색할 수 있습니다.

<div align="center">
  <img src="docs/assets/map.ko.gif" alt="Vestige의 3D 시맨틱 지도 - 대화가 주제별 군집으로 묶여 회전하는 3D 뷰" width="880">
</div>

**지난 세션도 클릭 한 번** - 날짜별로 정리돼 있고, 바로 검색됩니다.

<div align="center">
  <img src="docs/assets/sessions.ko.png" alt="Vestige의 세션 브라우저 - 과거 대화 목록" width="880">
</div>

## 다운로드

> **플랫폼 지원 현황:** **Windows**와 **macOS**는 빌드하고 실기기에서 테스트했습니다. **Linux**는 자동 빌드는 되지만 **아직 실기기에서 테스트하지 못했습니다.** 잘 동작하지 않을 수 있으니, 문제가 생기면 [이슈](https://github.com/flyingjoojak/vestige/issues)로 알려주시면 고치겠습니다.

설치본은 [**최신 릴리스**](https://github.com/flyingjoojak/vestige/releases/latest)에 올라옵니다. 릴리스 페이지에 파일이 여럿 보이지만 **받을 건 OS별 설치본 하나뿐**입니다:

| OS | 받을 파일 |
|----|-----------|
| 🪟 Windows | `Vestige-<버전>-Windows.exe` |
| 🍎 macOS | `Vestige-<버전>-macOS.dmg` (또는 아래 Homebrew) |
| 🐧 Linux | `Vestige-<버전>-Linux.AppImage` |

> 나머지 파일(`.blockmap`, `latest*.yml`)은 앱 **자동 업데이트용 내부 파일**이라 직접 받지 않아도 됩니다.

### 🪟 Windows - 검증됨

1. [최신 릴리스](https://github.com/flyingjoojak/vestige/releases/latest)에서 `Vestige-<버전>-Windows.exe` 를 받아 실행합니다.
2. "Windows의 PC 보호" 경고가 뜨면 **추가 정보 → 실행**. (아직 코드 서명을 안 해서 뜨는 경고일 뿐이며 안전합니다.)

### 🍎 macOS (Apple Silicon)

**Homebrew (권장)**

```bash
brew tap flyingjoojak/vestige
brew install --cask vestige
```

서드파티 tap이라 "untrusted tap" 경고가 뜨면 `brew trust flyingjoojak/vestige` 실행 후 다시 설치하세요. 업데이트는 `brew upgrade --cask vestige`.

**또는 `.dmg` 직접**: [Releases](https://github.com/flyingjoojak/vestige/releases)에서 받아 **Vestige** 를 Applications 로 드래그합니다. 처음 열 때 "확인되지 않은 개발자" 경고가 뜨면 앱을 우클릭 → **열기**를 한 번 눌러주세요. 최신 macOS 에서 우클릭 메뉴에 "열기"가 없으면 **시스템 설정 > 개인정보 보호 및 보안**에서 아래로 스크롤해 **"그래도 열기"**를 누르면 됩니다. (아직 Apple 인증서로 서명하진 않았지만 ad-hoc 서명이 들어가 있어 "손상됨"으로 막히진 않습니다.)

> Intel Mac은 아직 지원하지 않습니다(arm64 빌드).

### 🐧 Linux - 아직 테스트 안 됨

`.AppImage` 를 받아 `chmod +x` 로 실행 권한을 준 뒤 실행합니다.

**첫 실행:** 임베딩 모델을 하나 고르면(느린 기기용 경량 옵션 있음) 끝입니다. 이후 Vestige이 백그라운드에서 대화를 자동 색인하고, 왼쪽 레일에서 **검색 · 세션 · 3D 지도 · 설정**을 오갑니다.

## 어떻게 동작하나

Vestige은 Claude Code와 Codex가 **이미 내 기기에 남기는 로그**를 지켜봅니다 - 따로 설정할 게 없습니다.

```
Claude Code / Codex 로그  →  증분 읽기  →  대화(질문 + 답변 + 행동)
      →  로컬 임베딩(다국어 e5-large)  →  SQLite 아카이브 + 벡터 인덱스
      →  하이브리드 검색: 의미 ⊕ 키워드
```

Claude Code·Codex의 사람이 나눈 대화가 색인됩니다. `claude -p` 자동화(CI·cron·git 훅)로 생기는
일회성 SDK 세션은 **기본적으로 제외**됩니다 - 한 번 색인되면 지금은 되돌리기 어려워, 잃을 게 없는
쪽을 기본으로 뒀어요. SDK로 실제 작업해서 그 대화도 남기려면 `VESTIGE_SKIP_SDK_SESSIONS=0`으로 끄세요.

**원문 대화가 진실원본**이고 검색 인덱스는 재생성 가능한 파생물이라, 재색인·모델 교체는 항상 무손실입니다.

---

<details>
<summary><b>🔐  프라이버시 - 무엇이 남고, 무엇이 나갈 수 있나</b></summary>

<br>

기본은 **전부 로컬**이고 아무 데도 전송되지 않습니다. 데이터가 기기를 벗어나는 건 **직접 켜는 세 기능뿐:**

1. **클라우드 요약** - 선택적 요약에 클라우드 AI(`claude` 구독·Anthropic·OpenAI·Gemini)를 쓰면 대화 일부가 그 제공자로 갑니다. `ollama`(로컬)와 `off`만 아무것도 안 보냅니다.
2. **기기 동기화** - 기기 연결 시 **내 기기들끼리 P2P**로 로그를 동기화합니다. 제3자 서버를 거치지 않는 암호화 전송이며, 번들 Syncthing 바이너리는 SHA-256으로 검증됩니다.
3. **MCP** - 등록한 도구가 로컬 대화를 검색·열람할 수 있고, 그 도구가 클라우드 모델이면 반환된 텍스트가 그 모델로 갈 수 있습니다.

텔레메트리·사용통계·자동 오류 보고 없음. “문제 신고” 기능은 값이 가려진 형식 지문만 보내며 **대화 내용은 절대 보내지 않습니다.**

</details>

<details>
<summary><b>⌨️  개발자용 - CLI &amp; 소스 설치</b></summary>

<br>

Vestige은 파이썬 코어 + 얇은 CLI로 되어 있습니다. 소스에서 설치:

```bash
git clone https://github.com/flyingjoojak/vestige.git && cd vestige
pip install ".[web]"          # 코어 + 웹 UI.  전부: ".[all]"  ·  개발: pip install -e ".[all]"
vestige setup                 # 폴더·설정 + 10분마다 자동 색인하는 스케줄러
```

또는 [pipx](https://pipx.pypa.io):

```bash
pipx install "vestige[web] @ git+https://github.com/flyingjoojak/vestige.git"
vestige setup
```

```bash
mem "급여 계산 로직 어떻게 짰더라"      # 터미널에서 검색
vestige web                     # 웹 UI → http://127.0.0.1:8642
vestige search "..." -k 10 --since 2026-07-01 --session growth
vestige stats | config | progress           # 상태 · 설정 · 진행률
```

> Extras: `[web]` 웹 UI · `[enrich]` 클라우드/로컬 요약 백엔드 · `[mcp]` MCP 서버 · `[all]` 전부.
> (명령은 `vestige`, 별칭 `mem`. 데이터는 `~/vestige/data`에 저장됩니다.)

</details>

<details>
<summary><b>✨  선택 기능: 요약 - 플러그블 백엔드</b></summary>

<br>

요약/태그는 **선택 기능**이며 검색은 원문 기반입니다. `VESTIGE_ENRICH_BACKEND`로 백엔드 선택:

| 백엔드 | 설명 | 요건 |
|--------|------|-----------|
| `claude` (기본) | Claude Code 구독 (`claude -p`) | Claude Code 설치·로그인 |
| `anthropic` / `openai` / `gemini` | 클라우드 API | 해당 SDK + API 키 |
| `ollama` | 로컬 모델(오프라인·무료) | Ollama 실행 |
| `off` | 요약 없음(원문 검색만) | 없음 |

`openai`/`gemini`/`ollama`는 모두 OpenAI 호환 API입니다(LM Studio·vLLM·Groq도 연결 가능).

> macOS에서는 앱이 Finder로 실행돼 셸 `PATH`를 물려받지 못하므로, `claude` CLI가 설치돼 있어도 못 찾을 수 있습니다. Vestige이 흔한 위치(`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` 등)를 자동으로 확인하지만, 다른 곳에 있으면 `VESTIGE_CLAUDE_BIN=/claude/전체/경로`로 지정하세요.

```bash
VESTIGE_ENRICH_BACKEND=ollama VESTIGE_OLLAMA_MODEL=llama3.1 vestige enrich   # 로컬, 유출 0
```

</details>

<details>
<summary><b>🤖  MCP 서버 - 다른 AI가 내 과거 대화를 검색하게</b></summary>

<br>

MCP 서버를 등록하면 Claude Code·Desktop 등이 세션을 **검색·열람**합니다(로컬 하이브리드 검색 → 원문 + 요약).

> **가장 쉬운 방법:** 앱의 **설정 → MCP 연동**에서 대상별 등록 버튼.

```bash
claude mcp add vestige -- vestige-mcp
```

```json
{ "mcpServers": { "vestige": { "command": "vestige-mcp" } } }
```

도구: `search_memory` · `get_session` · `recent_sessions` · `stats`.

</details>

<details>
<summary><b>🚀  릴리스 &amp; 버저닝 (메인테이너)</b></summary>

<br>

태그(`vX.Y.Z`)를 push하면 GitHub Actions가 Windows/Linux/macOS 설치본을 빌드해 릴리스에 첨부합니다(자동 업데이트용 `latest.yml` 포함):

1. `electron/package.json`의 `version`과 **[homebrew-vestige](https://github.com/flyingjoojak/homebrew-vestige) tap 저장소의 `Casks/vestige.rb` `version`**을 함께 올리고, `CHANGELOG.md`에 변경 정리(날짜 포함). 카스크 버전을 빠뜨리면 이후 `brew install --cask`가 옛 dmg를 받아 404가 납니다. (cask는 tap 저장소에 있습니다 - 이 저장소가 아님)
2. `git tag v0.2.0 && git push origin v0.2.0`.
3. 릴리스가 만들어지면 **macOS `.dmg`가 실제로 첨부됐는지 확인**하세요 - mac 빌드는 미서명이라 CI에서 비차단(`continue-on-error`)이어서, 조용히 실패해도 릴리스는 green으로 생성됩니다(그러면 Homebrew가 404).
4. **릴리스 본문이 앱 업데이트 배너에 표시**됩니다 - `<!--lang:en-->` / `<!--lang:ko-->` 마커로 나누면 배너가 사용자 언어 섹션만 보여줍니다.

macOS: 미서명 앱은 자동 업데이트가 막혀 **Homebrew로 설치/업데이트**(Gatekeeper 경고 없음). Windows는 미서명이어도 배너에서 자동 업데이트됩니다.

</details>

## 기여하기

가장 효과가 큰 기여는 **Vestige이 새 도구의 로그를 읽게 하는 것**입니다(Aider·Cursor·Gemini CLI 등).
자족적인 어댑터 파일 하나면 되고 - 검색·지도·저장 파이프라인은 그대로입니다. 4-메서드 계약, 예제,
어댑터 보안 규칙은 **[CONTRIBUTING.md](CONTRIBUTING.md)** 를 참고하세요.

## 라이선스

**MIT** - [LICENSE](LICENSE) 참고. 기기 동기화를 위해 [Syncthing](https://syncthing.net/)(MPL-2.0) 엔진을 번들합니다. 다른 서드파티 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있습니다.

<div align="center"><br><sub>하루 종일 AI와 대화하는 사람들을 위해 - 그리고 그 대화를 기억하고 싶은 사람들을 위해.</sub><br><sub><a href="https://claude.com/claude-code">Claude Code</a>로 만들었습니다.</sub></div>
