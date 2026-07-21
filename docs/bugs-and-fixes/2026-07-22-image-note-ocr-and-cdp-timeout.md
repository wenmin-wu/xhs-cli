# `--ocr` extracted nothing from image notes (CDN 403), and reads intermittently hit the session-warmup captcha

**Date:** 2026-07-22
**Files:** `xhs_cli/client.py` (`capture_carousel_screenshots` new, CDP connect timeout), `xhs_cli/cli.py` (`read` OCR block)

## Two bugs, one session

### A. `--ocr` never got the pixels — image CDN 403s a download AND blocks in-page fetch

`read --ocr` collected the note's `imageList` URLs and called `_download_images(urls)` →
`_ocr_image_bytes()`. But **`_download_images` gets `403 Forbidden`** from the XHS image
CDNs (`*.rednotecdn.com` / `sns-img*.xhscdn.com`) — they require a signed URL + browser
fingerprint. An **in-page `fetch()` also fails** (`TypeError: Failed to fetch` — CORS/CSP).

So for image notes (research cards, options-scan cards, MS report screenshots) `read`
printed an empty/near-empty body — the whole point (the text is baked into the images) was
lost. It also only *tried* when `desc < 40` chars, so a text+image note (long caption **and**
data-bearing images, e.g. an MU 0DTE post) skipped OCR entirely.

**Fix:** the only path to the pixels is to **photograph the already-rendered `<img>`**.
New `client.capture_carousel_screenshots()` walks the swiper on the open note page (by its
pagination-bullet count), screenshots each note image (`spectrum` src, scoped to the media
carousel so page chrome is excluded), deduped by src so a wrap-around never double-counts,
and returns PNG bytes. `read` now:
- treats **any** note with images as OCR-eligible (`is_image_note = bool(imgs)`), gated by `--ocr`;
- captures via `capture_carousel_screenshots()` first, falling back to `_download_images` only
  if screenshots came back empty;
- OCRs the screenshot bytes.

### B. Intermittent `establishing browser session` captcha → hard abort

Some reads failed at *"security-verification wall while establishing browser session … may
need a human QR re-login"*. Root cause: `start()` attaches over CDP with `timeout=5000`; a
**busy chrome-dev (many tabs) takes >5 s to attach**, the connect times out, and it falls
back to the **own-profile path**, whose *"navigate to homepage to establish session"* warmup
(`self._goto(f"https://{_xhs_host()}")`) is a bare-homepage nav that trips the anti-bot
captcha. So the message is doubly wrong — the account is logged in, and it's not even the CDP
session that failed, just the connect timeout.

**Fix:** raise the CDP connect timeout to **15000 ms** so a busy browser still attaches to the
logged-in session (and `start()` returns before the own-profile warmup). Housekeeping: close
leftover automation tabs — they add to the tab count that slows the attach.

## Verification

```
$ xhs read "http://xhslink.com/o/9J4L3LmGvxB"      # an MU 0DTE options post (5-page carousel)
…full caption…
📷 7 images — extracting text via OCR…
SUPER爱投资·末日期权异动追踪 … 单合约最高权利金：$1050 CALL 达 $205.6万 …
背景分析·$SNDK是谁·闪迪·西部数据分拆 … $SNDK 报前出现类似CALL端偏重的末日期权异动，
随后财报超预期，股价单日涨幅超18% … 信号解读·三个核心异动 …
```

The carousel pages that a text-only read misses (here: the whole SNDK background + the 3-trade
breakdown) now come through. Combined with the 2026-07-22 share-URL fix, `xhs read <xhslink>`
now works for both text and image notes.

## Known residue / follow-ups
- A screenshot or two can still catch a sliver of sidebar chrome; the media-scoped selector
  reduces it, but an OCR-side chrome-keyword filter would clean it fully.
- The own-profile warmup (`start()` line ~316) is still a bare-homepage nav — if the CDP path
  is ever genuinely unavailable it will still trip the captcha; it should navigate to a note/
  share URL (with `xsec` params) or skip the warmup for cookie-authenticated sessions.
