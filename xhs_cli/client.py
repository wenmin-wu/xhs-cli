"""Xiaohongshu browser-based client using Playwright Chromium + DOM scraping.

Ported from the proven RedNote-MCP approach: launch a real (headed by default)
Chromium, navigate to pages, and scrape the *rendered DOM* with stable CSS
selectors. Modern Xiaohongshu no longer populates ``window.__INITIAL_STATE__``
with the search/feed feeds, so DOM scraping is the reliable path.

Each data method keeps its original signature and *return shape* so that
``cli.py`` continues to format output unchanged: scraped flat values are
re-packed into the nested ``note_card`` / ``interactInfo`` dicts that the CLI
already knows how to read.
"""

from __future__ import annotations

import logging
import os
import pathlib
import random
import re
import time

from .exceptions import DataFetchError, LoginError

logger = logging.getLogger(__name__)

# Persistent browser profile dir — a fixed user-data-dir gives a stable
# fingerprint plus persistent session/history/localStorage, so the browser
# looks like a returning real user (and the login naturally persists here).
PROFILE_DIR = pathlib.Path.home() / ".xhs-cli" / "profile"
# Cross-process rate guard: each CLI run is a separate process, so we serialize
# launches via a timestamp file rather than an in-process lock.
LAST_LAUNCH_FILE = pathlib.Path.home() / ".xhs-cli" / ".last_launch"

# Chromium launch args that strip the most obvious automation markers.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]

# Init script (runs before any page script) to hide residual webdriver markers.
STEALTH_INIT_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});\n"
    "window.chrome = window.chrome || { runtime: {} };"
)


def _apply_stealth(context) -> None:
    """Best-effort: install the anti-automation init script on a context.

    Wrapped so a failure here never blocks the browser launch.
    """
    try:
        context.add_init_script(STEALTH_INIT_JS)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to install stealth init script: %s", exc)


def _enforce_launch_gap() -> None:
    """Enforce a minimum gap between browser launches (cross-command).

    Reads/writes a timestamp file; if the previous launch was < XHS_MIN_GAP
    seconds ago, sleeps the remainder. Best-effort — never crashes on file
    errors.
    """
    try:
        min_gap = float(os.environ.get("XHS_MIN_GAP", "8").strip() or "8")
    except (TypeError, ValueError):
        min_gap = 8.0
    if min_gap <= 0:
        return
    now = time.time()
    try:
        last = float(LAST_LAUNCH_FILE.read_text().strip())
        elapsed = now - last
        if 0 <= elapsed < min_gap:
            wait = min_gap - elapsed
            logger.info("Rate guard: waiting %.1fs before launch (min gap %.0fs)",
                        wait, min_gap)
            time.sleep(wait)
    except Exception:
        # No prior timestamp / unreadable / unparsable — proceed without waiting.
        pass
    try:
        LAST_LAUNCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_LAUNCH_FILE.write_text(str(time.time()))
    except Exception as exc:
        logger.debug("Could not write launch timestamp: %s", exc)

# Login-status probe used by RedNote-MCP: the sidebar "我" (Me) channel link is
# only rendered when logged in.
LOGIN_CHECK_JS = """() => {
    const el = document.querySelector('.user.side-bar-component .channel');
    // Any non-empty channel label ('我' / 'Me' / other locale) = logged in;
    // the element only renders when authenticated.
    return !!(el && el.textContent && el.textContent.trim());
}"""

# Default data/selector timeout in ms (RedNote-MCP uses 30000). Overridable via
# XHS_TIMEOUT (seconds).
DEFAULT_TIMEOUT_S = 30.0


def _xhs_host() -> str:
    """Base web host for XHS. International accounts authenticate on
    rednote.com (the cn site xiaohongshu.com stays guest for them). Resolution
    order: $XHS_DOMAIN → the host auto-saved at login (~/.xhs-cli/domain) →
    the cn default.
    """
    import pathlib

    env = os.environ.get("XHS_DOMAIN", "").strip()
    if env:
        return env.replace("https://", "").replace("http://", "").strip("/")
    try:
        saved = (pathlib.Path.home() / ".xhs-cli" / "domain").read_text().strip()
        if saved:
            return saved
    except Exception:
        pass
    return "www.xiaohongshu.com"


def _chinese_unit_to_number(text: str) -> int:
    """Convert a count string like '1.2万' / '3,456' / '赞' into an int.

    Mirrors RedNote-MCP's ChineseUnitStrToNumber: '万' → ×10000. Non-numeric
    text (e.g. a bare label) returns 0.
    """
    if text is None:
        return 0
    s = str(text).strip()
    if not s:
        return 0
    try:
        if "万" in s:
            return int(float(s.replace("万", "").replace(",", "").strip()) * 10000)
        if "亿" in s:
            return int(float(s.replace("亿", "").replace(",", "").strip()) * 100000000)
        s = s.replace(",", "")
        digits = re.sub(r"[^\d.]", "", s)
        if not digits:
            return 0
        return int(float(digits))
    except (ValueError, TypeError):
        return 0


class XhsClient:
    """Playwright-Chromium-based Xiaohongshu client.

    Navigates to real pages and scrapes the rendered DOM, indistinguishable
    from a real user browsing.

    Can be used as a context manager::

        with XhsClient(cookie_dict) as client:
            client.search_notes("咖啡")
    """

    def __init__(self, cookie_dict: dict):
        self._cookie_dict = cookie_dict
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    @staticmethod
    def _is_publish_success(page_text: str, current_url: str, note_id: str = "") -> bool:
        """Heuristic to determine whether publish action succeeded."""
        success_indicators = [
            "发布成功",
            "已发布",
            "publish-success",
            "published successfully",
        ]
        normalized = (page_text or "").lower()
        url = (current_url or "").lower()
        if "creator.xiaohongshu.com/login" in url or "website-login/captcha" in url:
            return False
        on_publish_page = "publish/publish" in url
        if any(indicator.lower() in normalized for indicator in success_indicators):
            return True
        if on_publish_page:
            return False
        if note_id and note_id.lower() in url:
            return True
        if re.search(r"/(explore|notes?)/([a-zA-Z0-9]+)", url):
            return True
        return False

    @staticmethod
    def _extract_note_id_from_url(url: str) -> str:
        """Extract note_id from common URL patterns."""
        if not url:
            return ""
        patterns = [
            r"/explore/([a-zA-Z0-9]+)",
            r"[?&](?:note_id|noteId|id)=([a-zA-Z0-9]+)",
            r"/notes?/([a-zA-Z0-9]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return ""

    def _extract_note_id_from_page(self) -> str:
        """Best-effort note_id extraction from current page links."""
        try:
            note_id = self._page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll('a[href*="/explore/"]'));
                    const hrefs = [window.location.href, ...links.map(a => a.href || '')];
                    for (const href of hrefs) {
                        const m = href.match(/\\/explore\\/([a-zA-Z0-9]+)/);
                        if (m && m[1]) return m[1];
                    }
                    return "";
                }"""
            )
        except Exception:
            return ""
        return str(note_id or "")

    def start(self):
        """Launch a persistent Chromium profile, inject cookies, establish session.

        Uses ``launch_persistent_context`` against a fixed user-data-dir so the
        browser keeps a stable fingerprint plus session/history/localStorage
        across runs (looks like a returning real user).
        """
        from playwright.sync_api import sync_playwright

        # Cross-process rate guard before we spin up a browser.
        _enforce_launch_gap()

        self._cdp_mode = False
        # --- CDP mode (default): attach to a persistent chrome-dev browser so the
        # session is already logged in AND the user can solve any security
        # verification BY HAND in that same window. Disable with XHS_CDP="" / "off".
        # See stocks/scripts/start-chrome-dev.sh (CDP on :19327). ---
        cdp_url = os.environ.get("XHS_CDP", "http://127.0.0.1:19327").strip()
        if cdp_url.lower() in ("", "off", "none", "0", "false"):
            cdp_url = ""
        if cdp_url:
            try:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url, timeout=5000)
                self._cdp_mode = True
                self._context = (self._browser.contexts[0] if self._browser.contexts
                                 else self._browser.new_context())
                self._page = self._context.new_page()
                logger.info("Connected to chrome-dev via CDP %s — using its logged-in "
                            "session; solve any verification in that window.", cdp_url)
                return
            except Exception as exc:
                logger.info("CDP connect to %s failed (%s) — launching own profile.",
                            cdp_url, exc)
                try:
                    if self._playwright:
                        self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
                self._browser = None
                self._cdp_mode = False

        # Headed by DEFAULT: XHS serves a limited/guest page to headless
        # automation; only a visible browser gets the real feed. Set
        # XHS_HEADLESS=1 to force headless (XHS will likely wall it).
        headless = os.environ.get("XHS_HEADLESS", "").strip().lower() in ("1", "true", "yes")
        launch_opts: dict = {"headless": headless, "args": list(STEALTH_ARGS)}
        if not headless:
            logger.info("Running Chromium headed (visible window)")
        proxy = os.environ.get("XHS_PROXY", "").strip()
        if proxy:
            # Optional: route the browser through a proxy (e.g. a SOCKS5 tunnel).
            launch_opts["proxy"] = {"server": proxy}
            logger.info("Using proxy %s", proxy)

        logger.info("Starting Chromium browser (persistent profile)...")
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("Could not create profile dir %s: %s", PROFILE_DIR, exc)
        self._playwright = sync_playwright().start()
        # launch_persistent_context returns a BrowserContext directly (no Browser).
        self._context = self._playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR), **launch_opts
        )
        # Best-effort: keep a Browser handle for symmetry; persistent contexts
        # may or may not expose one depending on the launch.
        self._browser = getattr(self._context, "browser", None)
        _apply_stealth(self._context)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # Inject cookies before navigation so the session is authenticated.
        # Inject for BOTH domains — international (rednote.com) accounts share
        # the session with the cn site (xiaohongshu.com), which redirects there.
        # The persistent profile keeps these warm across runs too.
        cookies = [
            {"name": k, "value": v, "domain": dom, "path": "/"}
            for dom in (".xiaohongshu.com", ".rednote.com")
            for k, v in self._cookie_dict.items()
        ]
        if cookies:
            self._context.add_cookies(cookies)

        # Navigate to homepage to establish session.
        self._goto(
            f"https://{_xhs_host()}",
            timeout=20000,
            wait_min=1,
            wait_max=2,
            context="establishing browser session",
        )
        logger.info("Browser ready.")

    def close(self):
        """Shut down the browser session and Playwright."""
        if getattr(self, "_cdp_mode", False):
            # Connected to the user's chrome-dev — close ONLY our page, leave the
            # browser + the user's other tabs running; just disconnect Playwright.
            try:
                if self._page:
                    self._page.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._page = self._context = self._browser = self._playwright = None
            logger.info("Disconnected from chrome-dev (browser left running).")
            return
        # With a persistent context there's no separate Browser to close, and
        # closing the context tears down its pages — so just close the context
        # then stop Playwright.
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser closed.")

    # ===== Login status =====

    def is_logged_in(self) -> bool:
        """Lightweight login check via the '我' sidebar channel link.

        Navigates to the homepage (if not already there) and runs the
        RedNote-MCP login probe. No feed/search load required.
        """
        if "xiaohongshu.com" not in (self._page.url or ""):
            self._goto(
                f"https://{_xhs_host()}",
                timeout=20000,
                wait_min=1,
                wait_max=2,
                context="loading homepage for login check",
            )
        # Give the sidebar a moment to render after cookies take effect.
        try:
            self._page.wait_for_selector(
                ".user.side-bar-component .channel",
                timeout=int(self._effective_timeout(8.0) * 1000),
            )
        except Exception:
            pass
        try:
            return bool(self._page.evaluate(LOGIN_CHECK_JS))
        except Exception:
            return False

    # ===== Search =====

    def search_notes(self, keyword: str, limit: int = 10, sort: str = "general") -> list[dict]:
        """Search notes by keyword, scraping the rendered DOM.

        Opens each result card to read its detail, then re-packs the scraped
        values into the ``{"id", "xsec_token", "note_card": {...}}`` shape that
        ``cli.py`` expects.
        """
        import urllib.parse

        # XHS honors a `sort` query param on the web search page (verified live):
        # general (综合, default) / time_descending (最新) / popularity_descending (最热).
        sort_map = {"general": "", "time": "time_descending",
                    "popular": "popularity_descending"}
        url = (
            f"https://{_xhs_host()}/search_result?keyword="
            + urllib.parse.quote(keyword)
        )
        sort_val = sort_map.get(sort, "")
        if sort_val:
            url += "&sort=" + sort_val

        logger.info("Searching: %s (limit=%d, sort=%s)", keyword, limit, sort)
        self._goto(
            url,
            timeout=20000,
            wait_min=1,
            wait_max=2,
            context="loading search page",
        )

        self._human_browse()
        self._wait_for_selector(".feeds-container", desc="search feeds container")

        note_items = self._page.query_selector_all(".feeds-container .note-item")
        logger.info("Found %d note items", len(note_items))

        notes: list[dict] = []
        count = min(len(note_items), max(0, limit))
        for i in range(count):
            logger.info("Processing note %d/%d", i + 1, count)
            try:
                # Re-query each iteration: opening/closing the detail dialog can
                # invalidate previously captured element handles.
                items = self._page.query_selector_all(".feeds-container .note-item")
                if i >= len(items):
                    break
                item = items[i]

                cover = item.query_selector("a.cover.mask.ld")
                if not cover:
                    logger.debug("note %d has no cover link, skipping", i + 1)
                    continue

                # Try to capture the note id + xsec_token from the cover href
                # before we navigate (the detail URL may not expose the token).
                href = cover.get_attribute("href") or ""
                pre_note_id = self._extract_note_id_from_url(href)
                m = re.search(r"xsec_token=([^&]+)", href)
                pre_token = m.group(1) if m else ""

                cover.click()
                self._wait_for_selector("#noteContainer", desc="note detail container")
                self._human_wait(0.5, 1.5)

                raw = self._page.evaluate(_SEARCH_DETAIL_JS)
                if raw:
                    note_id = pre_note_id or self._extract_note_id_from_url(
                        raw.get("url", "")
                    )
                    notes.append(
                        self._pack_search_card(raw, note_id=note_id, xsec_token=pre_token)
                    )
                    logger.info("Extracted note: %s", (raw.get("title") or "")[:30])

                self._human_wait(0.5, 1.0)
                self._close_note_dialog()
            except Exception as exc:  # keep going on a single bad card
                logger.error("Error processing note %d: %s", i + 1, exc)
                self._close_note_dialog()
            finally:
                self._human_wait(0.5, 1.5)

        logger.info("Successfully processed %d notes", len(notes))
        return notes

    @staticmethod
    def _pack_search_card(raw: dict, note_id: str, xsec_token: str) -> dict:
        """Re-pack scraped flat detail into the CLI's search-item shape."""
        likes = _chinese_unit_to_number(raw.get("likes", ""))
        collects = _chinese_unit_to_number(raw.get("collects", ""))
        comments = _chinese_unit_to_number(raw.get("comments", ""))
        return {
            "id": note_id,
            "xsec_token": xsec_token,
            "xsecToken": xsec_token,
            "note_card": {
                "display_title": raw.get("title", "") or "",
                "displayTitle": raw.get("title", "") or "",
                "type": raw.get("type", "") or "",
                "user": {"nickname": raw.get("author", "") or ""},
                "interact_info": {
                    "liked_count": likes,
                    "likedCount": likes,
                    "collected_count": collects,
                    "comment_count": comments,
                },
                "interactInfo": {
                    "likedCount": likes,
                    "collectedCount": collects,
                    "commentCount": comments,
                },
            },
            "url": raw.get("url", "") or "",
        }

    def _close_note_dialog(self):
        """Close an open note-detail dialog and wait for it to detach."""
        try:
            close_btn = self._page.query_selector(".close-circle")
            if close_btn:
                close_btn.click()
                self._page.wait_for_selector(
                    "#noteContainer",
                    state="detached",
                    timeout=int(self._effective_timeout(DEFAULT_TIMEOUT_S) * 1000),
                )
        except Exception:
            pass

    # ===== Note Detail =====

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> dict:
        """Get note detail by navigating to the explore page and scraping DOM.

        Returns a dict shaped ``{"note": {...}}`` so ``cli.py``'s
        ``detail.get("note", detail)`` and downstream key reads keep working.
        """
        url = f"https://{_xhs_host()}/explore/{note_id}"
        if xsec_token:
            url += f"?xsec_token={xsec_token}&xsec_source=pc_feed"

        logger.info("Loading note: %s", note_id)
        self._goto(
            url,
            timeout=20000,
            wait_min=1.5,
            wait_max=3,
            context=f"loading note {note_id}",
        )

        self._human_browse()
        self._wait_for_selector(".note-container", desc="note container")
        try:
            self._wait_for_selector(".media-container", desc="media container")
        except DataFetchError:
            # Text-only notes may lack a media container; don't hard-fail.
            logger.debug("media-container not found for %s (text-only note?)", note_id)

        raw = self._page.evaluate(_NOTE_DETAIL_JS)
        if not raw:
            raise DataFetchError(f"Failed to extract note detail for {note_id}")

        likes = _chinese_unit_to_number(raw.get("likes", ""))
        collects = _chinese_unit_to_number(raw.get("collects", ""))
        comments = _chinese_unit_to_number(raw.get("comments", ""))

        note = {
            "noteId": note_id,
            "title": raw.get("title", "") or "",
            "desc": raw.get("content", "") or "",
            "type": raw.get("type", "") or "",
            "ipLocation": raw.get("ipLocation", "") or "",
            "ip_location": raw.get("ipLocation", "") or "",
            "tagList": [{"name": t} for t in (raw.get("tags") or [])],
            "imageList": [{"urlDefault": u} for u in (raw.get("imgs") or [])],
            "video": {"url": (raw.get("videos") or [""])[0]} if raw.get("videos") else {},
            "user": {"nickname": raw.get("author", "") or ""},
            "interactInfo": {
                "likedCount": likes,
                "collectedCount": collects,
                "commentCount": comments,
                "shareCount": 0,
                "liked": bool(raw.get("liked", False)),
                "collected": bool(raw.get("collected", False)),
            },
            "interact_info": {
                "liked_count": likes,
                "collected_count": collects,
                "comment_count": comments,
                "share_count": 0,
            },
            "url": url,
        }
        return {"note": note}

    # ===== User Profile =====

    def get_user_info(self, user_id: str) -> dict:
        """Get user profile by scraping their profile page.

        Returns a dict with ``userPageData.basicInfo`` / ``interactions`` so the
        existing CLI/whoami extraction logic keeps working.
        """
        url = f"https://{_xhs_host()}/user/profile/{user_id}"

        logger.info("Loading user profile: %s", user_id)
        self._goto(
            url,
            timeout=20000,
            wait_min=1.5,
            wait_max=3,
            context=f"loading user profile {user_id}",
        )

        try:
            self._wait_for_selector(
                ".user-info, .user-page, .info-part, .basic-info",
                desc="user profile info",
            )
        except DataFetchError:
            logger.warning("user profile selectors not found for %s", user_id)

        raw = self._page.evaluate(_USER_PROFILE_JS)
        if not raw:
            logger.warning(
                "Failed to scrape user profile for %s; returning minimal fallback",
                user_id,
            )
            return {"userInfo": {"userId": user_id}}

        basic = {
            "userId": user_id,
            "user_id": user_id,
            "nickname": raw.get("nickname", "") or "",
            "redId": raw.get("redId", "") or "",
            "red_id": raw.get("redId", "") or "",
            "desc": raw.get("desc", "") or "",
            "ipLocation": raw.get("ipLocation", "") or "",
            "gender": raw.get("gender", "") or "",
        }
        interactions = raw.get("interactions") or []
        return {
            "userPageData": {"basicInfo": basic, "interactions": interactions},
            "basicInfo": basic,
            "interactions": interactions,
            "userInfo": {"userId": user_id, "nickname": basic["nickname"]},
        }

    # ===== Followers / Following =====

    def _get_follow_list(self, user_id: str, tab: str) -> list[dict]:
        """Get a user's followers or following list by scraping the DOM.

        Args:
            user_id: The user ID to fetch for.
            tab: 'fans' for followers, 'follows' for following.
        """
        url = f"https://{_xhs_host()}/user/profile/{user_id}?tab={tab}"
        logger.info("Loading %s list for user %s", tab, user_id)
        self._goto(
            url,
            timeout=20000,
            wait_min=2,
            wait_max=3,
            context=f"loading {tab} list for user {user_id}",
        )
        try:
            self._wait_for_selector(
                ".user-list, .follow-list, [class*='user-item']",
                desc=f"{tab} list",
            )
        except DataFetchError:
            logger.warning("%s list selectors not found for %s", tab, user_id)
            return []

        result = self._page.evaluate(_FOLLOW_LIST_JS)
        return result if isinstance(result, list) else []

    def get_followers(self, user_id: str) -> list[dict]:
        """Get a user's followers list."""
        return self._get_follow_list(user_id, "fans")

    def get_following(self, user_id: str) -> list[dict]:
        """Get a user's following list."""
        return self._get_follow_list(user_id, "follows")

    # ===== User Posts =====

    def get_user_posts(self, user_id: str) -> list[dict]:
        """Get a user's published notes by scraping their profile feed.

        Returns a list of ``{"id", "xsec_token", "note_card": {...}}`` items.
        """
        url = f"https://{_xhs_host()}/user/profile/{user_id}"

        logger.info("Loading user posts: %s", user_id)
        self._goto(
            url,
            timeout=20000,
            wait_min=1.5,
            wait_max=3,
            context=f"loading user posts for {user_id}",
        )

        try:
            self._wait_for_selector(
                ".feeds-container, .note-item, .user-posts",
                desc="user posts feed",
            )
        except DataFetchError:
            logger.warning("user posts feed not found for %s", user_id)
            return []

        result = self._page.evaluate(_NOTE_CARDS_JS)
        return result if isinstance(result, list) else []

    # ===== Feed (Explore/Recommend) =====

    def get_feed(self) -> list[dict]:
        """Get recommended feed from the explore page via DOM scraping.

        Returns a list of ``{"id", "xsec_token", "note_card": {...}}`` items.
        """
        logger.info("Loading explore feed...")
        self._goto(
            f"https://{_xhs_host()}/explore",
            timeout=20000,
            wait_min=2,
            wait_max=4,
            context="loading explore feed",
        )
        self._human_browse()
        try:
            self._wait_for_selector(
                ".feeds-container, #exploreFeeds, .note-item",
                desc="feed container",
            )
        except DataFetchError:
            logger.warning("feed container not found")
            return []

        result = self._page.evaluate(_NOTE_CARDS_JS)
        return result if isinstance(result, list) else []

    # ===== Topics / Hashtags =====

    def search_topics(self, keyword: str) -> list[dict]:
        """Search for topic/hashtag pages by scraping the search results.

        Topic DOM structure on XHS is less stable than note cards; this scrapes
        any visible channel/topic chips and falls back to an empty list.
        """
        import urllib.parse

        url = (
            f"https://{_xhs_host()}/search_result?keyword="
            + urllib.parse.quote(keyword)
            + "&type=51"  # topic/channel tab
        )

        logger.info("Searching topics: %s", keyword)
        self._goto(
            url,
            timeout=20000,
            wait_min=1.5,
            wait_max=3,
            context="loading topics search page",
        )

        try:
            self._wait_for_selector(
                ".feeds-container, .channel-list, [class*='topic']",
                desc="topics container",
            )
        except DataFetchError:
            logger.warning("topics container not found")
            return []

        result = self._page.evaluate(_TOPICS_JS)
        return result if isinstance(result, list) else []

    # ===== Favorites =====

    def get_favorites(self, max_count: int = 50) -> list[dict]:
        """Get current user's favorite (collected) notes by scraping the DOM.

        Navigates to the user's profile collect tab and scrapes note cards,
        scrolling to load more up to ``max_count``.
        """
        user_id = self._resolve_self_user_id()
        if not user_id:
            raise LoginError("Cannot determine user_id. Make sure you are logged in.")

        url = f"https://{_xhs_host()}/user/profile/{user_id}?tab=collect"
        logger.info("Loading favorites: %s", url)
        self._goto(
            url,
            timeout=20000,
            wait_min=2,
            wait_max=3,
            context="loading favorites page",
        )
        try:
            self._wait_for_selector(
                ".feeds-container, .note-item",
                desc="favorites feed",
            )
        except DataFetchError:
            logger.warning("favorites feed not found")
            return []

        all_notes: list[dict] = []
        seen_ids: set[str] = set()
        page_limit = max(1, (max_count + 9) // 10)
        for _scroll in range(page_limit):
            cards = self._page.evaluate(_FAVORITES_JS)
            if isinstance(cards, list):
                for note in cards:
                    if not isinstance(note, dict):
                        continue
                    nid = note.get("noteId", note.get("note_id", note.get("id", "")))
                    if nid and nid not in seen_ids:
                        seen_ids.add(nid)
                        all_notes.append(note)
            if len(all_notes) >= max_count:
                break
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._human_wait(1.5, 2.5)

        return all_notes[:max_count]

    def _resolve_self_user_id(self) -> str:
        """Best-effort: read the logged-in user's id from the sidebar '我' link."""
        try:
            uid = self._page.evaluate(
                """() => {
                    const a = document.querySelector('.user.side-bar-component .channel');
                    const link = a && a.closest('a');
                    const href = (link && link.getAttribute('href')) || '';
                    const m = href.match(/\\/user\\/profile\\/([a-zA-Z0-9]+)/);
                    if (m) return m[1];
                    // Fallback: any profile link on the page
                    const any = document.querySelector('a[href*="/user/profile/"]');
                    const h2 = (any && any.getAttribute('href')) || '';
                    const m2 = h2.match(/\\/user\\/profile\\/([a-zA-Z0-9]+)/);
                    return m2 ? m2[1] : '';
                }"""
            )
            return str(uid or "")
        except Exception:
            return ""

    # ===== Self Info =====

    def get_self_info(self) -> dict:
        """Get current user's profile info.

        Uses the lightweight login check, resolves the self user_id from the
        sidebar, then scrapes the full profile page.
        """
        self._goto(
            f"https://{_xhs_host()}",
            timeout=15000,
            wait_min=1,
            wait_max=2,
            context="loading homepage for self info",
        )
        # Wait briefly for the sidebar to render.
        try:
            self._page.wait_for_selector(
                ".user.side-bar-component .channel",
                timeout=int(self._effective_timeout(10.0) * 1000),
            )
        except Exception:
            pass

        logged_in = False
        try:
            logged_in = bool(self._page.evaluate(LOGIN_CHECK_JS))
        except Exception:
            logged_in = False

        user_id = self._resolve_self_user_id()
        if user_id:
            try:
                full = self.get_user_info(user_id)
                if isinstance(full, dict) and full:
                    return full
            except Exception:
                pass

        if logged_in:
            # Logged in but couldn't resolve full profile — return a minimal
            # truthy payload so verification/whoami don't treat this as a guest.
            return {"userInfo": {"userId": user_id, "guest": False}}
        return {}

    # ===== Comments =====

    def get_note_comments(self, note_id: str, xsec_token: str = "",
                          max_comments: int = 50) -> list[dict]:
        """Scrape comments from the note page.

        Returns a list of ``{"userInfo": {"nickname": ...}, "content": ...}``
        items so ``cli.py``'s comment formatting keeps working.
        """
        expected_path = f"/explore/{note_id}"
        if expected_path not in (self._page.url or ""):
            self._navigate_to_note(note_id, xsec_token)

        # RedNote-MCP waits on the dialog list; the inline note page uses a
        # comments list too. Try both, don't hard-fail if absent.
        for selector in (
            '[role="dialog"] [role="list"]',
            ".comments-container",
            ".comments-el",
        ):
            try:
                self._page.wait_for_selector(
                    selector,
                    timeout=int(self._effective_timeout(8.0) * 1000),
                )
                break
            except Exception:
                continue

        result = self._page.evaluate(_COMMENTS_JS)
        if not isinstance(result, list):
            return []
        if max_comments and max_comments > 0:
            return result[:max_comments]
        return result

    # ===== Like / Unlike =====

    def like_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Like a note by clicking the like button."""
        return self._toggle_interact(note_id, xsec_token, "like", True)

    def unlike_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Unlike a note by clicking the like button."""
        return self._toggle_interact(note_id, xsec_token, "like", False)

    # ===== Favorite / Unfavorite =====

    def favorite_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Favorite a note by clicking the collect button."""
        return self._toggle_interact(note_id, xsec_token, "favorite", True)

    def unfavorite_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Unfavorite a note by clicking the collect button."""
        return self._toggle_interact(note_id, xsec_token, "favorite", False)

    # ===== Comment (post) =====

    def post_comment(self, note_id: str, content: str, xsec_token: str = "") -> bool:
        """Post a comment on a note by typing into the comment input."""
        self._navigate_to_note(note_id, xsec_token)

        try:
            input_el = (
                self._page.query_selector("#content-textarea")
                or self._page.query_selector('[contenteditable="true"]')
            )
            if not input_el:
                raise RuntimeError("Comment input not found")

            input_el.click()
            self._human_wait(0.3, 0.8)
            input_el.type(content, delay=random.randint(50, 150))
            self._human_wait(0.5, 1.0)

            submit = (
                self._page.query_selector(".submit.active")
                or self._page.query_selector("button.submit")
            )
            if submit:
                submit.click()
                self._human_wait(1, 2)
                if self._verify_comment_submitted(content):
                    logger.info("Comment posted on %s", note_id)
                    return True

            self._page.keyboard.press("Enter")
            self._human_wait(1, 2)
            if self._verify_comment_submitted(content):
                logger.info("Comment posted (Enter) on %s", note_id)
                return True
            logger.warning("Comment submit attempted but no success signal for %s", note_id)
            return False
        except Exception as e:
            logger.error("Failed to post comment: %s", e)
            return False

    def _verify_comment_submitted(self, content: str) -> bool:
        """Check whether comment submit succeeded (toast or comment appeared)."""
        try:
            body_text = (self._page.text_content("body") or "").strip()
        except Exception:
            body_text = ""
        success_tokens = ("评论成功", "发布成功", "发送成功", "success")
        lowered = body_text.lower()
        if body_text and any(token.lower() in lowered for token in success_tokens):
            return True
        # The freshly posted comment text usually appears in the comment list.
        if content and content in body_text:
            return True
        return False

    # ===== Publish Note =====

    def publish_note(
        self,
        title: str,
        image_paths: list[str],
        content: str = "",
        return_detail: bool = False,
    ) -> bool | dict[str, str | bool]:
        """Publish a new image note on Xiaohongshu (creator platform).

        NOTE: Mutation flow not fully re-ported to DOM-only scraping; selector
        logic is preserved from the original implementation and runs under the
        new Chromium launch. May need live tweaking.
        """
        # Validate image paths exist
        for path in image_paths:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Image not found: {path}")

        publish_url = "https://creator.xiaohongshu.com/publish/publish"
        logger.info("Navigating to publish page: %s", publish_url)
        self._goto(
            publish_url,
            timeout=30000,
            wait_min=3,
            wait_max=5,
            context="loading creator publish page",
        )

        for frame in self._page.frames:
            frame_url = (frame.url or "").lower()
            if "creator.xiaohongshu.com/login" in frame_url:
                raise LoginError(
                    "Creator platform login required for publishing. "
                    "Please log in at https://creator.xiaohongshu.com first."
                )

        file_input_selectors = [
            'input[type="file"]',
            '[type="file"]',
            'input[accept*="image"]',
            'input[accept*="image/*"]',
            ".upload-input",
            "#upload-input",
        ]

        def _find_file_input():
            for sel in file_input_selectors:
                el = self._page.query_selector(sel)
                if el:
                    return el
            for frame in self._page.frames:
                for sel in file_input_selectors:
                    try:
                        el = frame.query_selector(sel)
                    except Exception:
                        el = None
                    if el:
                        return el
            return None

        file_input = None
        for _ in range(6):
            try:
                self._page.wait_for_selector(
                    'input[type="file"]', state="attached", timeout=2500
                )
            except Exception:
                pass
            file_input = _find_file_input()
            if file_input:
                break
            self._human_wait(0.5, 1.2)

        if not file_input:
            upload_area_selectors = [
                ".upload-wrapper",
                '[class*="upload"]',
                ".drag-over",
                ".creator-upload-entry",
            ]
            for sel in upload_area_selectors:
                area = self._page.query_selector(sel)
                if area:
                    area.click()
                    self._human_wait(1, 2)
                    break
            for _ in range(4):
                file_input = _find_file_input()
                if file_input:
                    break
                self._human_wait(0.5, 1.2)

        if not file_input:
            raise RuntimeError(
                "Cannot find file upload input on the publish page. "
                "The page structure may have changed."
            )

        logger.info("Uploading %d images...", len(image_paths))
        file_input.set_input_files(image_paths)
        self._human_wait(3, 5)

        for _ in range(10):
            thumbnails = self._page.query_selector_all(
                'img[class*="thumbnail"], img[class*="preview"], '
                '.image-item, [class*="upload-item"]'
            )
            if len(thumbnails) >= len(image_paths):
                break
            self._human_wait(1, 2)

        logger.info("Images uploaded, filling in details...")

        title_selectors = [
            "#title-textarea",
            '[placeholder*="标题"]',
            'input[class*="title"]',
            'textarea[class*="title"]',
            ".title-input textarea",
            ".title-input input",
        ]
        for sel in title_selectors:
            title_el = self._page.query_selector(sel)
            if title_el:
                title_el.click()
                self._human_wait(0.3, 0.5)
                title_el.fill(title)
                logger.info("Title filled: %s", title)
                break
        else:
            logger.warning("Title input not found, trying keyboard input")
            self._page.keyboard.type(title)

        self._human_wait(0.5, 1)

        if content:
            content_selectors = [
                "#post-textarea",
                '[placeholder*="正文"]',
                '[placeholder*="描述"]',
                '[placeholder*="添加描述"]',
                'textarea[class*="content"]',
                'textarea[class*="desc"]',
                ".ql-editor",
                '[contenteditable="true"]',
            ]
            for sel in content_selectors:
                content_el = self._page.query_selector(sel)
                if content_el:
                    content_el.click()
                    self._human_wait(0.3, 0.5)
                    tag = content_el.evaluate("el => el.tagName.toLowerCase()")
                    if tag in ("textarea", "input"):
                        content_el.fill(content)
                    else:
                        self._page.keyboard.type(content)
                    logger.info("Content filled (%d chars)", len(content))
                    break
            else:
                logger.warning("Content input not found")

        self._human_wait(1, 2)

        publish_selectors = [
            'button:has-text("发布")',
            ".publishBtn",
            '[class*="publish-btn"]',
            'button[class*="submit"]',
            "button.css-k01sra",
        ]
        for sel in publish_selectors:
            publish_btn = self._page.query_selector(sel)
            if publish_btn:
                logger.info("Clicking publish button...")
                publish_btn.click()
                self._human_wait(3, 5)

                page_text = self._page.text_content("body") or ""
                current_url = self._page.url
                note_id = (
                    self._extract_note_id_from_url(current_url)
                    or self._extract_note_id_from_page()
                )
                if self._is_publish_success(page_text, current_url, note_id):
                    logger.info("Note published successfully. URL: %s", current_url)
                    if return_detail:
                        return {"success": True, "note_id": note_id, "url": current_url}
                    return True

                logger.warning(
                    "Publish clicked but no success signal. URL: %s", current_url
                )
                if return_detail:
                    return {"success": False, "note_id": note_id, "url": current_url}
                return False

        raise RuntimeError(
            "Cannot find publish button on the page. "
            "The page structure may have changed."
        )

    # ===== Delete Note =====

    def delete_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Delete a note by opening menu actions on the note page.

        NOTE: Mutation flow preserved from the original implementation; runs
        under the new Chromium launch. May need live tweaking.
        """
        self._navigate_to_note(note_id, xsec_token)

        more_selectors = [
            'button:has-text("...")',
            '[aria-label*="更多"]',
            '[class*="more"]',
            ".more",
            ".reds-icon.more",
        ]
        menu_opened = False
        for sel in more_selectors:
            el = self._page.query_selector(sel)
            if not el:
                continue
            try:
                el.click()
                self._human_wait(0.8, 1.5)
                menu_opened = True
                break
            except Exception:
                continue

        if not menu_opened:
            logger.error("Failed to open note menu for delete action")
            return False

        delete_selectors = [
            'button:has-text("删除")',
            '[role="menuitem"]:has-text("删除")',
            "text=删除",
            '[class*="delete"]',
        ]
        delete_clicked = False
        for sel in delete_selectors:
            el = self._page.query_selector(sel)
            if not el:
                continue
            try:
                el.click()
                self._human_wait(0.8, 1.5)
                delete_clicked = True
                break
            except Exception:
                continue

        if not delete_clicked:
            logger.error("Delete menu item not found/clickable for note %s", note_id)
            return False

        confirm_selectors = [
            'button:has-text("确定")',
            'button:has-text("确认")',
            'button:has-text("删除")',
            ".reds-button-primary",
        ]
        for sel in confirm_selectors:
            el = self._page.query_selector(sel)
            if not el:
                continue
            try:
                el.click()
                self._human_wait(2, 3)
                break
            except Exception:
                continue

        page_text = (self._page.text_content("body") or "").strip()
        if "删除成功" in page_text or "已删除" in page_text:
            return True
        if "删除失败" in page_text or "操作失败" in page_text:
            return False
        if self._verify_note_deleted(note_id, xsec_token):
            return True
        return False

    def _verify_note_deleted(self, note_id: str, xsec_token: str = "") -> bool:
        """Re-open the note page and verify the note is no longer available."""
        url = f"https://{_xhs_host()}/explore/{note_id}"
        if xsec_token:
            url += f"?xsec_token={xsec_token}&xsec_source=pc_feed"
        try:
            self._goto(
                url,
                timeout=20000,
                wait_min=1,
                wait_max=2,
                context=f"verifying deletion for note {note_id}",
            )
            # If the note container renders, the note still exists.
            container = self._page.query_selector(".note-container, #noteContainer")
            if container:
                return False
            body_text = (self._page.text_content("body") or "").strip().lower()
            unavailable_tokens = ("内容不存在", "已删除", "not found", "removed")
            return any(token in body_text for token in unavailable_tokens)
        except Exception:
            return False

    # ===== Internal: Interaction helpers =====

    def _navigate_to_note(self, note_id: str, xsec_token: str = ""):
        """Navigate to note detail page and wait for it to render."""
        url = f"https://{_xhs_host()}/explore/{note_id}"
        if xsec_token:
            url += f"?xsec_token={xsec_token}&xsec_source=pc_feed"
        self._goto(
            url,
            timeout=20000,
            wait_min=1.5,
            wait_max=3,
            context=f"loading note {note_id}",
        )
        self._wait_for_selector(
            ".note-container, #noteContainer", desc="note container"
        )

    def _get_interact_state(self, note_id: str) -> dict:
        """Read like/favorite state from the rendered interact bar."""
        try:
            state = self._page.evaluate(
                """() => {
                    const c = document.querySelector('.interact-container');
                    if (!c) return null;
                    const like = c.querySelector('.like-wrapper, .like-lottie');
                    const collect = c.querySelector('.collect-wrapper, .collect-icon');
                    const isActive = (el) => {
                        if (!el) return false;
                        const cls = (el.className || '') + '';
                        return /active|liked|collected|selected/i.test(cls);
                    };
                    return {
                        liked: isActive(like),
                        collected: isActive(collect),
                    };
                }"""
            )
            return state or {}
        except Exception:
            return {}

    def _toggle_interact(self, note_id: str, xsec_token: str,
                         action: str, target_state: bool) -> bool:
        """Toggle like/favorite by clicking the button and verifying state.

        NOTE: Like/favorite state detection on the rendered DOM is heuristic
        (class-name based); may need live tweaking.
        """
        SELECTORS = {
            "like": ".interact-container .like-wrapper, "
                    ".interact-container .left .like-lottie",
            "favorite": ".interact-container .collect-wrapper, "
                        ".interact-container .left .collect-icon",
        }
        STATE_KEYS = {"like": "liked", "favorite": "collected"}

        self._navigate_to_note(note_id, xsec_token)

        state = self._get_interact_state(note_id)
        current = state.get(STATE_KEYS[action], False)
        if current == target_state:
            action_name = action if target_state else f"un{action}"
            logger.info("Note %s already %sd, skipping", note_id, action_name)
            return True

        selector = SELECTORS[action]
        el = self._page.query_selector(selector)
        if not el:
            logger.error("%s button not found: %s", action, selector)
            return False

        el.click()
        self._human_wait(2, 3)

        state = self._get_interact_state(note_id)
        if state.get(STATE_KEYS[action], False) == target_state:
            logger.info("Note %s %s success", note_id, action)
            return True

        logger.warning("State didn't change, retrying click...")
        el = self._page.query_selector(selector)
        if el:
            el.click()
            self._human_wait(2, 3)

        state = self._get_interact_state(note_id)
        if state.get(STATE_KEYS[action], False) == target_state:
            logger.info("Note %s %s success after retry", note_id, action)
            return True

        logger.error("Failed to %s note %s after retry", action, note_id)
        return False

    # ===== Internal helpers =====

    def _goto(
        self,
        url: str,
        *,
        timeout: int = 20000,
        wait_until: str = "domcontentloaded",
        wait_min: float = 1.0,
        wait_max: float = 2.0,
        context: str = "loading page",
    ):
        """Navigate to URL and fail fast if redirected to risk-control pages."""
        self._page.goto(url, wait_until=wait_until, timeout=timeout)
        self._human_wait(wait_min, wait_max)
        self._raise_if_blocked(context, include_body=True)

    def _detect_block_reason(self, include_body: bool = False) -> str:
        """Detect whether current page is a security verification/risk-control page."""
        if not self._page:
            return ""

        page_url = getattr(self._page, "url", "") or ""
        url = page_url.lower()
        url_markers = (
            "website-login/captcha",
            "verifyuuid=",
            "verifytype=",
        )
        if any(marker in url for marker in url_markers):
            return f"redirected to verification URL: {page_url}"

        if not include_body:
            return ""

        try:
            body_text = (self._page.text_content("body") or "").lower()
        except Exception:
            return ""

        body_markers = (
            "security verification",
            "scan with logged-in",
            "qr code expires",
            "requests too frequent",
            "try again later",
            "请求过于频繁",
            "请求太频繁",
            "安全验证",
            "扫码验证",
        )
        for marker in body_markers:
            if marker in body_text:
                return f"verification page content detected ({marker})"
        return ""

    def _raise_if_blocked(self, context: str, include_body: bool = False):
        """Raise LoginError when the page is blocked by risk control."""
        reason = self._detect_block_reason(include_body=include_body)
        if reason:
            raise LoginError(f"Blocked by security verification while {context}: {reason}")

    def _effective_timeout(self, base: float) -> float:
        """Scale data-wait timeouts; XHS_TIMEOUT (seconds) overrides the base.

        Otherwise a configured XHS_PROXY triples the budget (tunnelled
        connections load slower).
        """
        env_t = os.environ.get("XHS_TIMEOUT", "").strip()
        if env_t:
            try:
                return float(env_t)
            except ValueError:
                pass
        if os.environ.get("XHS_PROXY", "").strip():
            return base * 3.0
        return base

    def _wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        desc: str = "element",
    ):
        """Wait for a CSS selector to appear; raise DataFetchError on timeout.

        Checks for risk-control redirects on timeout so a block surfaces as a
        clear LoginError rather than a generic data error.
        """
        timeout = self._effective_timeout(timeout)
        try:
            self._page.wait_for_selector(selector, timeout=int(timeout * 1000))
            logger.debug("%s ready", desc)
            return
        except Exception:
            self._raise_if_blocked(f"waiting for {desc}", include_body=True)
            logger.warning("%s (%s) not found after %.1fs", desc, selector, timeout)
            raise DataFetchError(f"{desc} not ready after {timeout:.1f}s")

    def _human_wait(self, min_sec: float = 1.0, max_sec: float = 3.5):
        """Wait a random human-like interval (widened default jitter)."""
        time.sleep(random.uniform(min_sec, max_sec))

    def _human_browse(self):
        """Mimic a human briefly scrolling the page before scraping.

        Does a few randomized small scrolls with short human pauses between.
        Best-effort — wrapped so a failure never blocks the scrape.
        """
        try:
            steps = random.randint(2, 4)
            for _ in range(steps):
                delta = random.randint(120, 520)
                try:
                    self._page.mouse.wheel(0, delta)
                except Exception:
                    # Fall back to JS scroll if mouse.wheel is unavailable.
                    self._page.evaluate("(d) => window.scrollBy(0, d)", delta)
                self._human_wait(0.4, 1.2)
        except Exception as exc:
            logger.debug("human-browse step skipped: %s", exc)


# ===========================================================================
# In-page scraping scripts (run via page.evaluate). Selectors ported from
# RedNote-MCP (rednoteTools.ts / noteDetail.ts). Kept as module-level strings
# for readability and reuse.
# ===========================================================================

# Read an opened note-detail dialog (search flow). Mirrors rednoteTools.ts.
_SEARCH_DETAIL_JS = r"""() => {
    const article = document.querySelector('#noteContainer');
    if (!article) return null;
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');

    const title = txt(article.querySelector('#detail-title'));
    const content = txt(article.querySelector('#detail-desc .note-text'));
    const author = txt(article.querySelector('.author-wrapper .username'));

    const engageBar = document.querySelector('.engage-bar-style');
    const likes = engageBar ? txt(engageBar.querySelector('.like-wrapper .count')) : '';
    const collects = engageBar ? txt(engageBar.querySelector('.collect-wrapper .count')) : '';
    const comments = engageBar ? txt(engageBar.querySelector('.chat-wrapper .count')) : '';

    const isVideo = !!document.querySelector('.media-container video');
    return {
        title, content, author,
        likes, collects, comments,
        type: isVideo ? 'video' : 'normal',
        url: window.location.href,
    };
}"""

# Full note-detail page. Mirrors noteDetail.ts (GetNoteDetail).
_NOTE_DETAIL_JS = r"""() => {
    const article = document.querySelector('.note-container');
    if (!article) return null;
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');

    const title =
        txt(article.querySelector('#detail-title')) ||
        txt(article.querySelector('.title'));

    const scroller = article.querySelector('.note-scroller') || article;
    const content = txt(scroller.querySelector('.note-content .note-text span'));

    const tags = Array.from(
        scroller.querySelectorAll('.note-content .note-text a')
    ).map((a) => (a.textContent || '').trim().replace('#', '')).filter(Boolean);

    const authorEl = article.querySelector('.author-container .info');
    const author = authorEl ? txt(authorEl.querySelector('.username')) : '';

    const interact = document.querySelector('.interact-container');
    const likes = interact ? txt(interact.querySelector('.like-wrapper .count')) : '';
    const collects = interact ? txt(interact.querySelector('.collect-wrapper .count')) : '';
    const comments = interact ? txt(interact.querySelector('.chat-wrapper .count')) : '';

    const imgs = Array.from(document.querySelectorAll('.media-container img'))
        .map((img) => img.getAttribute('src') || '').filter(Boolean);
    const videos = Array.from(document.querySelectorAll('.media-container video'))
        .map((v) => v.getAttribute('src') || '').filter(Boolean);

    let ipLocation = '';
    const ipEl = document.querySelector('.bottom-container .date, .date');
    if (ipEl) {
        const t = (ipEl.textContent || '').trim();
        const parts = t.split(/\s+/);
        if (parts.length > 1) ipLocation = parts[parts.length - 1];
    }

    return {
        title, content, tags, author,
        likes, collects, comments,
        imgs, videos, ipLocation,
        type: videos.length ? 'video' : 'normal',
    };
}"""

# Scrape note cards from a feed/search/profile grid into the CLI's item shape.
_NOTE_CARDS_JS = r"""() => {
    const out = [];
    const seen = new Set();
    const cards = document.querySelectorAll('.feeds-container .note-item, section.note-item');
    cards.forEach((card) => {
        const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
        const link =
            card.querySelector('a.cover.mask.ld') ||
            card.querySelector('a[href*="/explore/"]') ||
            card.querySelector('a');
        const href = (link && (link.getAttribute('href') || link.href)) || '';
        const idm = href.match(/\/(?:explore|search_result)\/([a-zA-Z0-9]+)/) ||
                    href.match(/\/explore\/([a-zA-Z0-9]+)/);
        const noteId = idm ? idm[1] : '';
        if (!noteId || seen.has(noteId)) return;
        seen.add(noteId);
        const tokenM = href.match(/xsec_token=([^&]+)/);
        const xsecToken = tokenM ? tokenM[1] : '';

        const title =
            txt(card.querySelector('.footer .title')) ||
            txt(card.querySelector('[class*="title"]'));
        const author =
            txt(card.querySelector('.author .name')) ||
            txt(card.querySelector('.card-bottom-wrapper .name')) ||
            txt(card.querySelector('[class*="author"] [class*="name"]')) ||
            txt(card.querySelector('[class*="author"]'));
        const likes =
            txt(card.querySelector('.like-wrapper .count')) ||
            txt(card.querySelector('[class*="like"] [class*="count"]')) ||
            txt(card.querySelector('[class*="like"]'));
        const isVideo = !!card.querySelector('.play-icon, [class*="video"]');

        out.push({
            id: noteId,
            xsec_token: xsecToken,
            xsecToken: xsecToken,
            note_card: {
                display_title: title,
                displayTitle: title,
                type: isVideo ? 'video' : 'normal',
                user: { nickname: author },
                interact_info: { liked_count: likes, likedCount: likes },
                interactInfo: { likedCount: likes },
            },
        });
    });
    return out;
}"""

# Favorites grid → flat note dicts (cli.py reads noteId/displayTitle/user/interactInfo).
_FAVORITES_JS = r"""() => {
    const out = [];
    const seen = new Set();
    const cards = document.querySelectorAll('.feeds-container .note-item, section.note-item');
    cards.forEach((card) => {
        const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
        const link =
            card.querySelector('a.cover.mask.ld') ||
            card.querySelector('a[href*="/explore/"]') ||
            card.querySelector('a');
        const href = (link && (link.getAttribute('href') || link.href)) || '';
        const idm = href.match(/\/explore\/([a-zA-Z0-9]+)/);
        const noteId = idm ? idm[1] : '';
        if (!noteId || seen.has(noteId)) return;
        seen.add(noteId);
        const tokenM = href.match(/xsec_token=([^&]+)/);
        const title =
            txt(card.querySelector('.footer .title')) ||
            txt(card.querySelector('[class*="title"]'));
        const author =
            txt(card.querySelector('.author .name')) ||
            txt(card.querySelector('[class*="author"]'));
        const likes = txt(card.querySelector('[class*="like"]'));
        const isVideo = !!card.querySelector('.play-icon, [class*="video"]');
        out.push({
            noteId: noteId,
            note_id: noteId,
            id: noteId,
            xsec_token: tokenM ? tokenM[1] : '',
            xsecToken: tokenM ? tokenM[1] : '',
            displayTitle: title,
            display_title: title,
            title: title,
            type: isVideo ? 'video' : 'normal',
            user: { nickname: author },
            interactInfo: { likedCount: likes },
            interact_info: { liked_count: likes },
        });
    });
    return out;
}"""

# Comments → list of {userInfo:{nickname}, content, likeCount, time}.
_COMMENTS_JS = r"""() => {
    const out = [];
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
    // RedNote-MCP dialog structure first, then inline comment list fallbacks.
    let items = document.querySelectorAll('[role="dialog"] [role="list"] [role="listitem"]');
    if (!items.length) {
        items = document.querySelectorAll('.comments-container .comment-item, .comment-item, .parent-comment');
    }
    items.forEach((item) => {
        const author =
            txt(item.querySelector('[data-testid="user-name"]')) ||
            txt(item.querySelector('.author .name')) ||
            txt(item.querySelector('.name'));
        const content =
            txt(item.querySelector('[data-testid="comment-content"]')) ||
            txt(item.querySelector('.content')) ||
            txt(item.querySelector('.note-text'));
        const likes =
            txt(item.querySelector('[data-testid="likes-count"]')) ||
            txt(item.querySelector('[class*="like"] .count')) ||
            txt(item.querySelector('[class*="like"]'));
        const time =
            txt(item.querySelector('time')) ||
            txt(item.querySelector('.date'));
        if (!content && !author) return;
        out.push({
            userInfo: { nickname: author },
            user_info: { nickname: author },
            content: content,
            likeCount: likes,
            time: time,
        });
    });
    return out;
}"""

# User profile header → flat fields (cli.py wraps into basicInfo/interactions).
_USER_PROFILE_JS = r"""() => {
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
    const nickname =
        txt(document.querySelector('.user-info .user-name')) ||
        txt(document.querySelector('.user-name')) ||
        txt(document.querySelector('[class*="nickname"]'));
    let redId = '';
    const redEl = document.querySelector('.user-redId, [class*="red-id"], .user-content .user-redId');
    if (redEl) {
        const t = (redEl.textContent || '').trim();
        const m = t.match(/[:：]?\s*([0-9A-Za-z_]+)\s*$/);
        redId = m ? m[1] : t.replace(/[^0-9A-Za-z_]/g, '');
    }
    const desc = txt(document.querySelector('.user-desc, [class*="description"]'));
    let ipLocation = '';
    const ipEl = document.querySelector('.user-IP, [class*="ip-location"], [class*="ipLocation"]');
    if (ipEl) {
        const t = (ipEl.textContent || '').trim();
        const m = t.match(/[:：]\s*(.+)$/);
        ipLocation = m ? m[1].trim() : t;
    }

    const interactions = [];
    const counts = document.querySelectorAll('.user-interactions .count, .interaction-list .count, [class*="interaction"] .count');
    const wraps = document.querySelectorAll('.user-interactions > div, .interaction-list > div, [class*="interaction-item"]');
    wraps.forEach((w) => {
        const num = txt(w.querySelector('.count'));
        const label = txt(w.querySelector('.shows, .desc, .label'));
        if (label) {
            let key = label;
            if (/关注/.test(label)) key = 'follows';
            else if (/粉丝/.test(label)) key = 'fans';
            else if (/获赞|收藏/.test(label)) key = 'interaction';
            interactions.push({ type: key, name: key, count: num });
        }
    });

    return { nickname, redId, desc, ipLocation, gender: '', interactions };
}"""

# Follower/following list → list of {nickname, redId, userId}.
_FOLLOW_LIST_JS = r"""() => {
    const out = [];
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
    const items = document.querySelectorAll(
        '.user-list .user-item, .follow-list .user-item, [class*="user-item"]'
    );
    items.forEach((item) => {
        const link = item.querySelector('a[href*="/user/profile/"]') || item.querySelector('a');
        const href = (link && (link.getAttribute('href') || link.href)) || '';
        const m = href.match(/\/user\/profile\/([a-zA-Z0-9]+)/);
        const userId = m ? m[1] : '';
        const nickname =
            txt(item.querySelector('.name')) ||
            txt(item.querySelector('[class*="name"]'));
        if (!nickname && !userId) return;
        out.push({
            nickname: nickname,
            nick_name: nickname,
            userId: userId,
            user_id: userId,
            id: userId,
            redId: '',
            red_id: '',
        });
    });
    return out;
}"""

# Topic/channel chips → list of {name, id, ...}. XHS topic DOM is unstable.
_TOPICS_JS = r"""() => {
    const out = [];
    const txt = (el) => (el && el.textContent ? el.textContent.trim() : '');
    const items = document.querySelectorAll(
        '.channel-list a, [class*="topic"] a, .feeds-container .note-item a[href*="page_id"]'
    );
    items.forEach((a) => {
        const name = txt(a);
        const href = (a.getAttribute('href') || a.href || '');
        const m = href.match(/page_id=([a-zA-Z0-9]+)/);
        const id = m ? m[1] : '';
        if (!name) return;
        out.push({ name: name, title: name, id: id, topicId: id });
    });
    return out;
}"""
