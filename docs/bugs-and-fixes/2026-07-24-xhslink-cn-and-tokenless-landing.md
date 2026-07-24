# 2026-07-24 — xhslink.cn short links + token-less discovery/item landings

## Symptoms

1. `xhs read "http://xhslink.cn/o/…"` → `.note-container not found after 30s`
   on BOTH domains, every time. (Two real links from app shares that day.)
2. Even when the short link resolved in-browser, the landing was
   `xiaohongshu.com/discovery/item/<id>?app_platform=ios&…` — a login-wall page
   whose `location.href` carries **no `xsec_token`**.

## Root causes

1. Short-link detection was hard-coded to `"xhslink.com" in ref` in TWO places
   (`cli.read`, `cli._resolve_share_link`). A `.cn` link fell through to the
   full-URL path, regex found no note id, and the whole share URL was pasted
   into `https://<host>/explore/<share-url>` → guaranteed container timeout.
   Misleading extra datum: `curl -I` on the .cn link returns **404** (it rejects
   non-browser requests) — do NOT diagnose these links with curl.
2. The `/discovery/item/` landing renders no `.note-container` on rednote.com,
   and with no token in the URL the domain-swap replay could never work. BUT the
   landed page's HTML still embeds `app_share`-minted note links WITH fresh
   tokens (same browser handshake → live tokens).

## Fix (commit fadf2d3)

- `_is_short_link()` — `xhslink\.[a-z]+`, any TLD.
- `Client.harvest_xsec_token()` — regex the landed page HTML for a fresh token
  when `location.href` has none; caller rebuilds `/explore/<id>` on the
  logged-in host with `xsec_source=app_share`.
- `_load()` normalizes `/discovery/item/<id>` → `/explore/<id>`.

## Verification

- Manual CDP reproduction of the harvest path (before patching) successfully
  scraped the full note (`6a61df0a…a84`).
- **E2E PASS (2026-07-24 14:1x, after captcha cool-down): all three legs** —
  ① `xhslink.cn/o/6Gsh12cxSP5` (the original failing link) → full note ✓
  ② `xhslink.cn/o/1qU2BnDjsz0` → full note (10 imgs) ✓
  ③ `xhslink.com/o/7iwK6WHltrh` (.com regression) → full note ✓
- ⚠️ Deployment gotcha found during E2E: `uv tool install --force --from .` REUSES
  the cached wheel when the version number is unchanged — the first "install" of
  this fix silently deployed the OLD build. Bump `version` in pyproject.toml (or
  `--reinstall --no-cache`) for every local install. (0.1.4 → 0.1.5.)
- Follow-up fix in the same session: all 27 `❌` error prints moved from stdout
  to stderr (`err_console = Console(stderr=True)`) per agent-cli-standards —
  verified stdout stays 0 bytes on failure, exit code 1.
