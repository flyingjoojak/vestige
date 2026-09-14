<div align="center">

<img src="docs/assets/banner.png" alt="Vestige" width="100%">

### Your AI coding assistant forgets everything. Now you don't have to.

[![License: MIT](https://img.shields.io/badge/license-MIT-10b981.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-1f2937)
![Local & Offline](https://img.shields.io/badge/100%25-local%20%26%20offline-10b981)
![Built with](https://img.shields.io/badge/Python%20·%20React%20·%20Electron-47848F)

**English** · [한국어](README.md)

</div>

You've solved hundreds of problems with **Claude Code** and **Codex** - that gnarly async bug, the exact
Docker config, the prompt that finally worked. Then the session closes and it's gone. Next time you
need it, you scroll through endless history, or just ask again from scratch.

**Vestige is the long-term memory your AI assistant doesn't have.** It keeps every conversation
on your own machine, in the background, and lets you find any of them in a second - by meaning, not just keywords.

<div align="center">
  <img src="docs/assets/search.png" alt="Searching past conversations in Vestige - type what you remember and the exact answer comes back" width="880">
</div>

<!-- Want a playable demo video too? Drag an .mp4 into a GitHub issue/release, then paste the
     github.com/user-attachments/... URL on its own line here. (Record on demo data - see docs/assets/README.md.) -->

## What changes for you

| Before Vestige | With Vestige |
|---|---|
| You know you fixed this before, but that chat is gone. | You **find the exact conversation in seconds.** |
| You re-ask Claude the same question and burn tokens. | You **reuse the answer you already got.** |
| You only find it if you remember the exact words. | You **search by a vague memory** - "that flaky websocket test fix" lands the right message. |
| Your history is scattered across hundreds of sessions. | You **see it all on one 3D map**, clustered by topic. |
| Cloud tools read your conversations. | **Nothing leaves your machine.** No account, no cloud. |

## Features

- 🔍 **Search that reads your mind** - finds by meaning, so half-remembered ideas still land the exact message (semantic + keyword, together).
- ⚡ **Instant recall** - every Claude Code & Codex conversation in one search box. No more scrolling or re-asking.
- 🗺️ **A map of your work** - a 3D view clusters everything you've done into topics you can fly through.
- 🔒 **100% local & offline** - runs entirely on your computer. No account, no telemetry, works on a plane.
- ↔️ **One memory across devices** - your laptop and desktop stay in sync, peer-to-peer (no cloud).
- 🤖 **Give your AI its memory back** - via MCP, Claude can search its own past sessions for you.

## See it in action

**A 3D map of everything you've discussed** - your history clustered into topics you can fly through.

<div align="center">
  <img src="docs/assets/map.gif" alt="Vestige's rotating 3D semantic map, with conversations clustered into labeled topics" width="880">
</div>

**Every session, one click away** - grouped, timestamped, and searchable.

<div align="center">
  <img src="docs/assets/sessions.png" alt="Vestige's session browser listing past conversations" width="880">
</div>

## Download

> **Platform status:** **Windows** and **macOS** are built and tested on real hardware. **Linux** builds are produced automatically but **haven't been tested on real hardware yet**, so it may not work smoothly. If something breaks, please [open an issue](https://github.com/flyingjoojak/vestige/issues) and I'll fix it.

Installers land on the [**latest release**](https://github.com/flyingjoojak/vestige/releases/latest). The release page lists several files, but **you only need one installer for your OS**:

| OS | Download |
|----|----------|
| 🪟 Windows | `Vestige-<version>-Windows.exe` |
| 🍎 macOS | `Vestige-<version>-macOS.dmg` (or Homebrew below) |
| 🐧 Linux | `Vestige-<version>-Linux.AppImage` |

> The other files (`.blockmap`, `latest*.yml`) are **internal auto-update files** - you don't need to download them.

### 🪟 Windows - verified

1. Download `Vestige-<version>-Windows.exe` from the [latest release](https://github.com/flyingjoojak/vestige/releases/latest) and run it.
2. If Windows shows a "protected your PC" warning, click **More info → Run anyway**. (It's just because the app isn't code-signed yet - it's safe.)

### 🍎 macOS (Apple Silicon)

**Homebrew (recommended)**

```bash
brew tap flyingjoojak/vestige
brew install --cask vestige
```

It's a third-party tap, so if you see an "untrusted tap" warning, run `brew trust flyingjoojak/vestige` and install again. Update with `brew upgrade --cask vestige`.

**Or the `.dmg` directly**: download from [Releases](https://github.com/flyingjoojak/vestige/releases) and drag **Vestige** to Applications. The first time you open it, if you see an "unidentified developer" warning, right-click the app and choose **Open** once. On recent macOS, if the right-click menu has no "Open", go to **System Settings > Privacy & Security**, scroll down and click **"Open Anyway"**. (It isn't signed with an Apple certificate yet, but it carries an ad-hoc signature, so Apple Silicon no longer blocks it as "damaged".)

> Intel Macs aren't supported yet (arm64 build).

### 🐧 Linux - not tested yet

Download the `.AppImage`, make it executable with `chmod +x`, and run it.

**First launch:** pick an embedding model (a lightweight option is offered for slower machines) and you're set. Vestige then indexes your conversations in the background; use the left rail for **Search · Sessions · 3D map · Settings**.

## How it works

Vestige watches the logs Claude Code and Codex **already write on your machine**, so there's nothing to set up.

```
Claude Code / Codex logs  →  read incrementally  →  conversations (question + answer + actions)
      →  local embeddings (multilingual e5-large)  →  SQLite archive + vector index
      →  hybrid search: meaning ⊕ keywords
```

Your human conversations with Claude Code and Codex get indexed. One-shot SDK sessions from `claude -p`
automation (CI, cron, git hooks) are **excluded by default** - once something is indexed it's hard to remove
today, so the default errs on the side of not keeping throwaway runs. If you actually work through the SDK and
want those conversations kept, turn it off with `VESTIGE_SKIP_SDK_SESSIONS=0`.

Your **raw conversations are the source of truth**; the search index is just a regenerable derivative, so
re-indexing or switching models is always lossless.

---

<details>
<summary><b>🔐  Privacy - what stays, what can leave</b></summary>

<br>

By default **everything is local** and nothing is sent anywhere. Data leaves your device **only via three features you turn on yourself:**

1. **Cloud summaries** - if you use a cloud AI for optional summaries (`claude` subscription, Anthropic, OpenAI, or Gemini), parts of conversations go to that provider. Only `ollama` (local) and `off` send nothing.
2. **Device sync** - connecting devices syncs logs **peer-to-peer between your own machines**, encrypted, through no third-party server. The bundled Syncthing binary is verified via SHA-256.
3. **MCP** - a tool you register can search/view your local conversations; if it's a cloud model, returned text may reach that model.

No telemetry, no usage stats, no automatic error reports. The “report an issue” feature sends only a masked format fingerprint - never conversation content.

</details>

<details>
<summary><b>⌨️  For developers - CLI &amp; source install</b></summary>

<br>

Vestige is built on a Python core with a thin CLI. Install from source:

```bash
git clone https://github.com/flyingjoojak/vestige.git && cd vestige
pip install ".[web]"          # core + web UI.  Everything: ".[all]"  ·  dev: pip install -e ".[all]"
vestige setup                 # folders, config, and a scheduler that auto-indexes every 10 min
```

Or with [pipx](https://pipx.pypa.io):

```bash
pipx install "vestige[web] @ git+https://github.com/flyingjoojak/vestige.git"
vestige setup
```

```bash
mem "how did I write the payroll calc logic"   # search from the terminal
vestige web                            # web UI → http://127.0.0.1:8642
vestige search "..." -k 10 --since 2026-07-01 --session growth
vestige stats | config | progress                 # status · config · progress
```

> Extras: `[web]` web UI · `[enrich]` cloud/local summary backends · `[mcp]` MCP server · `[all]` everything.
> (The command is `vestige`, alias `mem`; data lives in `~/vestige/data`.)

</details>

<details>
<summary><b>✨  Optional summaries - pluggable backends</b></summary>

<br>

Summaries/tags are **optional** (search runs on the raw text). Pick a backend via `VESTIGE_ENRICH_BACKEND`:

| Backend | Description | Requirements |
|--------|------|-----------|
| `claude` (default) | Claude Code subscription (`claude -p`) | Claude Code installed & logged in |
| `anthropic` / `openai` / `gemini` | Cloud APIs | that SDK + API key |
| `ollama` | Local model (offline, free) | Ollama running |
| `off` | No summaries (raw search only) | none |

`openai`/`gemini`/`ollama` all speak the OpenAI-compatible API (LM Studio, vLLM, Groq, … work too).

> On macOS the app is launched from Finder and doesn't inherit your shell `PATH`, so it may not find the `claude` CLI even when it's installed. Vestige looks in the usual spots (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, …); if yours lives elsewhere, point at it with `VESTIGE_CLAUDE_BIN=/full/path/to/claude`.

```bash
VESTIGE_ENRICH_BACKEND=ollama VESTIGE_OLLAMA_MODEL=llama3.1 vestige enrich   # local, zero leakage
```

</details>

<details>
<summary><b>🤖  MCP server - let other AIs search your past conversations</b></summary>

<br>

Registering the MCP server lets Claude Code, Desktop, etc. **search and view** your sessions (local hybrid search → raw text + summary).

> **Easiest:** in the app, **Settings → MCP integration**, use the register buttons per target.

```bash
claude mcp add vestige -- vestige-mcp
```

```json
{ "mcpServers": { "vestige": { "command": "vestige-mcp" } } }
```

Tools: `search_memory` · `get_session` · `recent_sessions` · `stats`.

</details>

<details>
<summary><b>🚀  Release &amp; versioning (maintainers)</b></summary>

<br>

Pushing a tag (`vX.Y.Z`) makes GitHub Actions build the Windows/Linux/macOS installers and attach them to the release (with `latest.yml` for auto-update):

1. Bump `version` in `electron/package.json` **and in the `Casks/vestige.rb` of the [homebrew-vestige](https://github.com/flyingjoojak/homebrew-vestige) tap repo**, and summarize changes (with the date) in `CHANGELOG.md`. If you forget the cask version, later `brew install --cask` fetches the old dmg and 404s. (The cask lives in the tap repo, not this one.)
2. `git tag v0.2.0 && git push origin v0.2.0`.
3. Once the release exists, **confirm the macOS `.dmg` is actually attached** - the mac build is unsigned and runs with `continue-on-error` in CI, so a silent failure still produces a green release (and then Homebrew 404s).
4. **The release body shows in the app's update banner** - split it with `<!--lang:en-->` / `<!--lang:ko-->` markers and the banner shows the section matching the user's language.

macOS: unsigned apps can't auto-update, so install/update via **Homebrew** (no Gatekeeper warning). Windows auto-updates from the banner even while unsigned.

</details>

## Contributing

The most valuable contribution is **teaching Vestige to read a new tool's logs** (Aider, Cursor,
Gemini CLI, …). It's a single self-contained adapter file - the search, map, and storage pipeline
stay untouched. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the four-method contract, a worked
example, and the security rules for adapters.

## License

**MIT** - see [LICENSE](LICENSE). Vestige bundles the [Syncthing](https://syncthing.net/) (MPL-2.0) engine for device sync; other third-party licenses are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

<div align="center"><br><sub>Built for people who talk to their AI all day - and want to remember what they said.</sub><br><sub>Built with <a href="https://claude.com/claude-code">Claude Code</a>.</sub></div>
