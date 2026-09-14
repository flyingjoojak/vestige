# Homebrew Cask — Vestige (macOS)
#
# macOS는 미서명 앱의 자동 업데이트(Squirrel.Mac)를 막으므로, mac 사용자는 Homebrew로
# 설치·업데이트하는 것을 권장한다. Homebrew가 다운로드·교체를 대신 처리하고 격리(quarantine)를
# 떼주므로, 코드 서명 없이도 설치·업데이트가 되고 Gatekeeper 경고도 뜨지 않는다.
#
# 설치:
#   brew tap flyingjoojak/vestige https://github.com/flyingjoojak/vestige
#   brew install --cask flyingjoojak/vestige/vestige
# 업데이트:
#   brew upgrade --cask vestige
#
# 참고: sha256 :no_check 는 무결성 해시를 고정하지 않는다는 뜻이다(HTTPS·livecheck로 최신
# 릴리스를 자동 감지). 더 강한 보안이 필요하면 릴리스마다 실제 dmg 의 sha256 으로 고정하면 되며,
# 이 갱신은 릴리스 CI로 자동화할 수 있다.
cask "vestige" do
  version "0.2.1"
  sha256 :no_check

  url "https://github.com/flyingjoojak/vestige/releases/download/v#{version}/Vestige-#{version}-macOS.dmg",
      verified: "github.com/flyingjoojak/vestige/"
  name "Vestige"
  desc "Local semantic search over your AI coding CLI conversations"
  homepage "https://github.com/flyingjoojak/vestige"

  # 새 GitHub 릴리스가 뜨면 `brew upgrade` 가 감지하도록.
  livecheck do
    url :url
    strategy :github_latest
  end

  app "Vestige.app"

  zap trash: [
    "~/Library/Application Support/Vestige",
    "~/Library/Preferences/com.vestige.app.plist",
    "~/Library/Logs/Vestige",
  ]
end
