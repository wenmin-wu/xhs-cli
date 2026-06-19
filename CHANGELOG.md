# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Changed

- **Switched the browser backend from camoufox to Playwright Chromium and
  rewrote data extraction to DOM scraping** (ported from the proven RedNote-MCP
  approach). Modern Xiaohongshu no longer populates
  `window.__INITIAL_STATE__.search.feeds` / `.feed.feeds`, so the old JS-state
  extraction timed out on every command. `search`/`read`/`comments`/`feed` now
  scrape the rendered DOM (`.feeds-container .note-item`, `#noteContainer`,
  `.note-container`, etc.). Public method signatures and CLI output shapes are
  unchanged. Run `playwright install chromium` once. Chromium also renders CJK
  correctly (camoufox's Firefox showed garbled Chinese) and is ~150 MB vs
  camoufox's ~712 MB. This supersedes the earlier camoufox `coreBundle.js`
  self-heal (the Firefox-driver crash doesn't exist on Chromium).
- **Headed by default.** XHS serves a limited/guest page to headless automation,
  so only a visible browser mints a real session and returns real feeds. Set
  `XHS_HEADLESS=1` to force headless (not recommended). The login also skips the
  terminal ASCII QR in headed mode — scan the crisp on-page QR in the window.
- Added `XHS_PROXY` (route the browser through a SOCKS5/HTTP proxy) and
  `XHS_TIMEOUT` (override the default 30s data/selector wait, in seconds).
- The post-login usability probe now uses a lightweight login-status check (the
  `我` sidebar link) instead of loading a feed, so a confirmed login is no longer
  falsely discarded as "limited" when a feed is slow to render.

## v0.1.4 - 2026-03-11

### Changed

- Reworked `xhs login --qrcode` to use a browser-assisted network-response flow.
- Removed the legacy DOM/screenshot-based QR extraction path.
- Synced README and README_EN to the current QR login behavior.

### Validation

- `python -m compileall xhs_cli` passes.
- `uv run pytest tests/test_auth.py tests/test_cli.py` passes (`61 passed`).

## v0.1.2 - 2026-03-06

### Added

- Added terminal QR rendering with half-block characters (`▀`, `▄`, `█`) for QR login.
- Added post-login session usability probing (feed/search) to detect limited/guest sessions.
- Added stricter and broader test coverage for auth, CLI login flows, and publish heuristics.
- Added `qrcode` dependency for terminal QR rendering.

### Changed

- Strengthened cookie requirements for saved/manual auth:
  - Required cookies are now `a1` + `web_session`.
- Improved QR login robustness:
  - Switched `xhs login --qrcode` to a browser-assisted network-response flow.
  - Export QR URL from `login/qrcode/create` instead of scraping page DOM.
  - Export session cookies after `login/qrcode/status` instead of guessing from page state.
- Improved login success detection:
  - Treat guest sessions as invalid.
  - Wait for post-login browser session stabilization before persisting cookies.
- Improved operation reliability:
  - Tightened success criteria for publish/comment/delete flows.
  - Added strict data-wait timeout path to reduce silent empty results.
- Updated `whoami --json` to include normalized top-level fields when resolvable.
- Updated docs (`README.md` and `README_EN.md`) to match current login/auth behavior.

### Fixed

- Fixed transient cookie verification flow to avoid unintended QR login fallback.
- Fixed favorites note ID extraction regex to support alphanumeric note IDs.
- Fixed cross-platform cookie save behavior by handling `chmod` failures safely.
- Fixed multiple false-positive success cases in interaction and publish flows.

### Validation

- `ruff check .` passes.
- `pytest -q` passes (`66 passed, 21 deselected`).
