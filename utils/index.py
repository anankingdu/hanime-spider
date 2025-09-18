import logging
import re
from typing import Optional, cast
from urllib.parse import urljoin

import requests
from fake_useragent import UserAgent

try:
    import cloudscraper
except ImportError:  # pragma: no cover - optional dependency
    cloudscraper = None


logger = logging.getLogger(__name__)

FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

try:
    _ua_provider = UserAgent()
except Exception as exc:  # pragma: no cover - rarely triggered
    logger.warning("Failed to initialize fake-useragent provider: %s", exc)
    _ua_provider = None


def _random_user_agent() -> str:
    """Return a realistic User-Agent string."""

    if _ua_provider is None:
        return FALLBACK_USER_AGENT

    try:
        return _ua_provider.random
    except Exception as exc:  # pragma: no cover - rarely triggered
        logger.debug("fake-useragent random selection failed: %s", exc)
        return FALLBACK_USER_AGENT


BASE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

BASE_URL = "https://hanime1.me"
MAIN_URL = f"{BASE_URL}/playlists"
SEARCH_URL = f"{BASE_URL}/search"


class CloudflareClient:
    """Minimal Cloudflare challenge bypass following CloudflareBypassForScraping."""

    def __init__(self) -> None:
        if cloudscraper is not None:
            self.session = cast(
                requests.Session,
                cloudscraper.create_scraper(
                    browser={
                        "browser": "chrome",
                        "platform": "windows",
                        "desktop": True,
                    }
                ),
            )
            logger.debug("Using cloudscraper backed session for Cloudflare bypass")
        else:  # pragma: no cover - optional dependency missing
            self.session = requests.Session()
            logger.debug("cloudscraper not installed; falling back to requests.Session")

        self._session_ready = False
        self._apply_base_headers()

    def _apply_base_headers(self) -> None:
        self.session.headers.clear()
        self.session.headers.update(BASE_HEADERS)
        self._rotate_user_agent()

    def _rotate_user_agent(self) -> None:
        self.session.headers["User-Agent"] = _random_user_agent()

    def invalidate(self) -> None:
        """Reset cookies and mark the session as needing revalidation."""

        self.session.cookies.clear()
        self._session_ready = False
        self._rotate_user_agent()

    def _solve_challenge_with_tokens(self) -> bool:
        """Attempt to fetch clearance tokens using cloudscraper's helper."""

        if cloudscraper is None:
            return False

        try:
            tokens, user_agent = cloudscraper.get_tokens(
                MAIN_URL,
                user_agent=self.session.headers.get("User-Agent"),
            )
        except Exception as exc:  # pragma: no cover - depends on remote service
            logger.warning("Failed to obtain Cloudflare tokens: %s", exc)
            return False

        self.session.cookies.update(tokens)
        if user_agent:
            self.session.headers["User-Agent"] = user_agent

        self._session_ready = True
        logger.debug("Successfully obtained Cloudflare clearance tokens")
        return True

    def ensure_ready(self) -> bool:
        """Make sure we hold valid Cloudflare cookies before scraping."""

        if self._session_ready:
            return True

        if self._solve_challenge_with_tokens():
            return True

        logger.debug("Falling back to direct request to prime Cloudflare session")
        try:
            response = self.session.get(MAIN_URL, timeout=20)
        except requests.RequestException as exc:
            logger.warning("Failed to initialize Cloudflare session: %s", exc)
            self.invalidate()
            return False

        if response.status_code != 200:
            logger.warning(
                "Cloudflare priming request returned status code %s", response.status_code
            )
            response.close()
            self.invalidate()
            return False

        response.close()
        self._session_ready = True
        logger.debug("Cloudflare session primed successfully with direct request")
        return True

    def request(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        context: str,
    ) -> Optional[requests.Response]:
        """Execute a GET request while automatically handling Cloudflare blocks."""

        for attempt in range(3):
            if not self.ensure_ready():
                logger.error(
                    "Cloudflare session could not be prepared before %s request to '%s'",
                    context,
                    url,
                )
                return None

            try:
                response = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                logger.error("%s request failed for '%s': %s", context, url, exc)
                self.invalidate()
                continue

            if response.status_code == 200:
                return response

            logger.warning(
                "%s request blocked for '%s' with status code %s",
                context,
                url,
                response.status_code,
            )
            response.close()

            if not self._handle_block(response.status_code, context):
                break

        logger.error("Giving up on %s request to '%s' after repeated failures", context, url)
        return None

    def _handle_block(self, status_code: int, context: str) -> bool:
        """React to Cloudflare errors by refreshing cookies and retrying."""

        recoverable_codes = {403, 429, 503, 520, 521}
        self.invalidate()

        if status_code not in recoverable_codes:
            logger.error(
                "Encountered unrecoverable status %s during %s request", status_code, context
            )
            return False

        if cloudscraper is not None:
            logger.info(
                "Attempting to refresh Cloudflare clearance after %s request hit status %s",
                context,
                status_code,
            )
            if self._solve_challenge_with_tokens():
                return True

        logger.debug("Retrying Cloudflare priming flow after status %s", status_code)
        return True


_client = CloudflareClient()


def _request(
    url: str,
    *,
    params: Optional[dict[str, str]] = None,
    context: str,
) -> Optional[requests.Response]:
    return _client.request(url, params=params, context=context)


def getSearchData(name: str) -> list[str]:
    params = {
        "query": name,
        "type": "",
        "genre": "",
        "sort": "",
        "year": "",
        "month": "",
    }

    search_resp = _request(SEARCH_URL, params=params, context="Search")
    if search_resp is None:
        return []

    search_regex = re.compile(
        r'overlay.*?href="(?P<href>.*?)"',
        re.S,
    )
    hrefs: list[str] = []
    seen: set[str] = set()
    for match in search_regex.finditer(search_resp.text):
        href = urljoin(BASE_URL, match.group("href"))
        if href not in seen:
            seen.add(href)
            hrefs.append(href)
    search_resp.close()

    if not hrefs:
        logger.info("Search for '%s' returned no results", name)

    return hrefs


def getFirstPageData(hrefs: Optional[list[str]]) -> list[str]:
    if not hrefs:
        logger.info("No search results available to fetch detail pages")
        return []

    download_page_hrefs: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        page_url = urljoin(BASE_URL, href)
        page_resp = _request(page_url, context="Detail page")
        if page_resp is None:
            continue

        download_regex = re.compile(r'儲存.*?<a href="(?P<href>.*?)".*?download</i>下載', re.S)
        page_result = download_regex.search(page_resp.text)
        if page_result:
            page_href = urljoin(BASE_URL, page_result.group("href"))
            if page_href not in seen:
                seen.add(page_href)
                download_page_hrefs.append(page_href)
        else:
            logger.warning("No download link found on detail page '%s'", page_url)
        page_resp.close()

    if not download_page_hrefs:
        logger.info("No download pages were discovered from the provided detail pages")

    return download_page_hrefs


def handleDownloadAudio(hrefs: Optional[list[str]]) -> tuple[list[dict[str, str]], list[str]]:
    if not hrefs:
        logger.info("No download pages available to resolve audio links")
        return [], []

    download_urls: list[str] = []
    infos: list[dict[str, str]] = []
    for href in hrefs:
        download_url = urljoin(BASE_URL, href)
        download_resp = _request(download_url, context="Download page")
        if download_resp is None:
            continue

        download_regex = re.compile(r'play_circle_filled.*?href="(?P<href>.*?)"', re.S)
        info_title_regex = re.compile(r'download="(?P<title>.*?)"')
        download_result = download_regex.search(download_resp.text)
        info_title_result = info_title_regex.search(download_resp.text)
        if download_result and info_title_result:
            download_href = urljoin(BASE_URL, download_result.group("href"))
            info_title = info_title_result.group("title")
            cleaned_href = download_href.replace("&amp;", "&")
            download_urls.append(cleaned_href)
            infos.append({"title": info_title, "url": cleaned_href})
        else:
            logger.warning("No download URL or title found on download page '%s'", download_url)
        download_resp.close()

    if not infos:
        logger.info("No downloadable audio information could be extracted")

    return infos, download_urls
