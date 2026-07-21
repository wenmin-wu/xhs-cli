# `xhs read <xhslink>` bounced to a `website-login/captcha` wall (mis-reported as "needs QR re-login")

**Date:** 2026-07-22
**Files:** `xhs_cli/client.py` (`get_note_detail`, new `_logged_in_host`), `xhs_cli/cli.py` (`read`)

## Symptom

`xhs read "http://xhslink.com/o/…"` failed with:

```
security-verification wall while establishing browser session (attempt 1/3):
redirected to verification URL:
https://www.rednote.com/website-login/captcha?...verifyType=216...datactry:SG
(persisted across 3 attempts — session may need a human QR re-login)
```

…even though the shared chrome-dev browser was **fully logged in** (rednote.com cookies
carried `id_token` + `web_session`; the RedNote UI showed the user's own profile). The
"may need a human QR re-login" message was a **mis-diagnosis** — it was never a login
problem.

## Root cause (narrowed in three steps)

1. ❌ *"xhs-cli spawns an isolated session"* — **wrong.** It already attaches to the
   logged-in chrome-dev via `connect_over_cdp` + `contexts[0]` (same as a hand-rolled
   script that succeeded).
2. ⚠️ *"CN domain (xiaohongshu.com) works, overseas (rednote.com) is a bot-wall"* —
   **only half-true.** rednote.com serves the note fine **when handed the full share URL**.
3. ✅ **The real cause:** `get_note_detail` **threw away the fully-resolved long URL** and
   **reconstructed a bare** `https://{host}/explore/{note_id}?xsec_token=…&xsec_source=…`,
   then re-navigated. That reconstruction **drops the app-share query params**
   (`xsec_source=app_sh`, `share_from_user_hidden`, and the note-scoped `xsec_token`).

   Those params are the **share-access grant**. Without them the note bounces to
   `website-login/captcha` (a verification wall that fires on logged-in sessions too —
   independent of auth). It also defaulted the host to `www.xiaohongshu.com`, where an
   intl (rednote.com-logged-in) account is a **guest**.

Empirically (all via the same logged-in CDP browser):

| navigation target | result |
|---|---|
| `goto(<short link>)` — full redirect chain, all params | ✅ note + content |
| full long URL on `xiaohongshu.com` (all params) | ✅ note + content |
| full long URL on `rednote.com` (all params, domain-swapped) | ✅ note + content |
| stripped `/explore/{id}` (params dropped) | ❌ bounce to feed / login wall |

→ **params, not domain.** Both domains work *with* the share params; both fail *without*.

## The fix (thorough — not a band-aid)

Keep the **whole resolved long URL** and swap **only the domain** to the host this session
is actually logged in on:

1. `cli.py read()` now passes the full URL that `resolve_share_link()` already landed on
   into `get_note_detail(..., resolved_url=resolved)`.
2. `get_note_detail(..., resolved_url="")`: when `resolved_url` is set, navigate to it
   **verbatim** (`urlsplit`/`urlunsplit`, preserving `path` + **every** query param),
   swapping only the netloc — instead of reconstructing `/explore/{id}?xsec_token=…`.
   The old reconstructed path is kept as the fallback for bare-id / direct-URL reads.
3. New `_logged_in_host()`: detects the authenticated domain from live context cookies
   (`rednote.com` if it carries `id_token`/`web_session`, else `xiaohongshu.com`), so the
   note opens on the domain that will serve it. The OTHER-domain retry is still there.

Net read flow for a share link:
`短链 → resolve_share_link (in-browser, mints valid token) → full long URL → swap domain to logged-in host, preserve ALL params → navigate → scrape .note-container`.

## Verification

```
$ xhs read "http://xhslink.com/o/2ArGSXRZ2TR"
华尔街精心布局全球科技领域精准猎杀
by 杨明晓回味经典 · 四川
华尔街通过精心布局和杠杆操作，短短三周内引发韩国股市崩盘… ❤️ 16 ⭐ 15 💬 11
# exit 0 — no verification wall
```

## Follow-ups (not done here)

- The error string `"session may need a human QR re-login"` should be reworded — a
  verification wall ≠ logged-out. Better: *"share context lost / anti-bot wall; pass the
  full share URL or solve the check in the CDP window."*
- On a genuine wall, try extracting the SSR `.note-content` before aborting (the note text
  is often server-rendered in the DOM even behind a login overlay).
