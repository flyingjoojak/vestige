# Contributing to Vestige

Thanks for wanting to make Vestige better. This guide covers the one contribution that
matters most for the project's reach - **adding support for a new AI coding tool** - plus the
basics for any change.

Vestige indexes conversation logs from AI coding CLIs (Claude Code, Codex today) into a local
hybrid search + 3D map. Every new tool it can read makes it more useful to more people. The
architecture is built so that adding a tool is a **single self-contained file**, not a change
that ripples through the pipeline.

---

## Adding a new source adapter (e.g. Aider, Cursor, Gemini CLI)

A "source" is one tool whose logs Vestige reads. Adding one means answering four questions about
that tool's log format - nothing about chunking, embedding, storage, search, or the 3D map, all
of which are tool-agnostic and stay untouched.

### The contract

An adapter is a class satisfying `vestige/sources/base.py::SourceAdapter`:

| Member | What it does |
|---|---|
| `name` | Internal id - registry / settings / drift key (e.g. `"aider"`). |
| `source_name` | *(optional)* The origin shown in the DB and search filters. Defaults to `name`. Only set it differently when folding a sub-stream into another tool (as `subagent` → `"claude-code"`). |
| `discover(root)` | Yield each session file under the tool's log root. |
| `read_records(path, start_offset=0)` | Yield `(record_dict, end_offset)` for each **complete** record. Must be incremental and tail-safe: start reading at `start_offset`, and never yield a half-written trailing line (hold it back until the next read). |
| `is_turn_start(obj)` | Is this record the start of a human's turn (a user prompt)? |
| `extract_turns(objs)` | Turn a list of records into normalized `Turn` objects (`vestige/models.py`). |

A `Turn` is the universal unit the rest of Vestige understands: a user question + the assistant's
answer + the actions it took. Once your adapter emits `Turn`s, everything downstream just works.

### Steps

1. **Create `vestige/sources/aider.py`** with your adapter class. Read `vestige/sources/codex.py`
   as a worked example - it documents how it absorbs three Codex-specific quirks (context only on
   the first line, schema differs by version, double-logged messages) *inside the adapter* so the
   pipeline never sees them. That's the pattern: **weird tool details are absorbed by the adapter.**

2. **Register it** in `vestige/sources/__init__.py`:
   ```python
   from .aider import AiderAdapter
   ADAPTERS = { ..., AiderAdapter.name: AiderAdapter() }
   ```
   and add its log root in `source_roots()`:
   ```python
   "aider": Path(C.AIDER_SESSIONS_DIR),
   ```
   (add the corresponding config entry in `vestige/config.py`).

3. **Write a test** under `tests/` (copy `tests/test_codex_source.py`): feed a small real log
   sample through the adapter and assert the `Turn`s come out right. Please include a redacted
   sample of the real log format you're targeting.

That's it. No changes to the indexer, chunker, embedder, store, search, or UI.

### Security contract (please read)

Adapters run **in-process**, in the same trust boundary as the rest of the backend. An adapter
must **only** read logs from its own root and return `Turn`s. It must **not**:

- make network calls,
- write to arbitrary files,
- run `exec` / `eval` / subprocesses, or
- import a plugin-autoloader that discovers adapters from disk.

Registration is always through an **explicit import** in `sources/__init__.py`. There is
deliberately no dynamic plugin discovery: the only way a third-party adapter reaches a user's
machine is PR → code review → release. That review is the trust gate - keep adapters small and
easy to audit so it stays effective. PRs that add dynamic adapter loading will be declined.

Note the log *content* an adapter parses is untrusted, but the pipeline already defends against
that (FTS is injection-safe, markdown rendering is XSS-safe, resume paths are validated). Your
adapter just needs to parse defensively and not crash on malformed input - drop a bad record, don't
trust a field to exist.

---

## Any change

- **Setup:** `pip install -e ".[all]"` for the backend; `npm --prefix frontend ci` for the UI.
- **Tests:** `python -m pytest -q` (backend) must stay green. `npx tsc -b --noEmit` and
  `npm --prefix frontend run build` for frontend changes.
- **Database changes:** the user's `archive.db` is a permanent asset. Never edit `_SCHEMA` in a way
  that breaks existing DBs - add a versioned, idempotent step to `_MIGRATIONS` in `vestige/store.py`
  instead (see the comment there). Migrations only ever go forward and are appended to the end of
  the list. Adding something to `_SCHEMA` **and** a migration is the rule, not a choice: a schema-only
  change silently gives new installs and upgraded installs different databases, and only the latter
  breaks. `test_migrated_db_schema_matches_fresh_schema` guards this - keep it green.
- **Style:** small, focused files; explicit error handling; no secrets. Match the surrounding code.
- **Commits:** conventional prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- **PRs:** describe what changed and why, and note how you verified it. Keep unrelated changes out.
- **Link the issue:** put `Closes #123` (or `Fixes #123`) in the PR body when it resolves one. GitHub
  then closes the issue on merge and records which PR did it - closing by hand leaves no trail, so
  later you cannot tell when a feature shipped or in which release.

## Scope

Vestige is intentionally **local-first and offline**: no accounts, no telemetry, nothing leaves the
machine. Features that require a network service or send conversation data anywhere are out of scope.
Everything else - new adapters, search quality, the map, performance, platform support - is welcome.
