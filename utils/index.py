import logging
import re
from typing import Optional

import requests
from fake_useragent import UserAgent


logger = logging.getLogger(__name__)

ua = UserAgent()
headers = {
    'User-Agent': ua.random
}

session = requests.Session()

MAIN_URL = 'https://hanime1.me/playlists'
SEARCH_URL = 'https://hanime1.me/search'

_session_ready = False


def _ensure_session_ready() -> None:
    """Prime the shared session so Cloudflare issues the required cookies."""
    global _session_ready
    if _session_ready:
        return

    try:
        response = session.get(MAIN_URL, headers=headers)
    except requests.RequestException as exc:
        logger.warning("Failed to initialize Cloudflare session: %s", exc)
        return

    if response.status_code != 200:
        logger.warning(
            "Cloudflare challenge priming failed with status code %s", response.status_code
        )
        response.close()
        return

    _session_ready = True
    response.close()


def getSearchData(name: str) -> list[str]:
    _ensure_session_ready()

    params = {
        "query": name,
        "type": "",
        "genre": "",
        "sort": "",
        "year": "",
        "month": "",
    }

    try:
        search_resp = session.get(url=SEARCH_URL, headers=headers, params=params)
    except requests.RequestException as exc:
        logger.error("Search request failed for '%s': %s", name, exc)
        return []

    if search_resp.status_code != 200:
        logger.warning(
            "Search request blocked for '%s' with status code %s", name, search_resp.status_code
        )
        search_resp.close()
        return []

    search_regex = re.compile(
        r'overlay.*?href="(?P<href>.*?)"',
        re.S,
    )
    hrefs: list[str] = []
    seen: set[str] = set()
    for match in search_regex.finditer(search_resp.text):
        href = match.group('href')
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

    _ensure_session_ready()

    download_page_hrefs: list[str] = []
    for href in hrefs:
        try:
            page_resp = session.get(url=href, headers=headers)
        except requests.RequestException as exc:
            logger.error("Failed to fetch detail page '%s': %s", href, exc)
            continue

        if page_resp.status_code != 200:
            logger.warning(
                "Detail page request blocked for '%s' with status code %s", href, page_resp.status_code
            )
            page_resp.close()
            continue

        download_regex = re.compile(r'儲存.*?<a href="(?P<href>.*?)".*?download</i>下載', re.S)
        page_result = download_regex.search(page_resp.text)
        if page_result:
            page_href = page_result.group('href')
            download_page_hrefs.append(page_href)
        else:
            logger.warning("No download link found on detail page '%s'", href)
        page_resp.close()

    if not download_page_hrefs:
        logger.info("No download pages were discovered from the provided detail pages")

    return download_page_hrefs


def handleDownloadAudio(hrefs: Optional[list[str]]) -> tuple[list[dict[str, str]], list[str]]:
    if not hrefs:
        logger.info("No download pages available to resolve audio links")
        return [], []

    _ensure_session_ready()

    download_urls: list[str] = []
    infos: list[dict[str, str]] = []
    for href in hrefs:
        try:
            download_resp = session.get(url=href, headers=headers)
        except requests.RequestException as exc:
            logger.error("Failed to fetch download page '%s': %s", href, exc)
            continue

        if download_resp.status_code != 200:
            logger.warning(
                "Download page request blocked for '%s' with status code %s", href, download_resp.status_code
            )
            download_resp.close()
            continue

        download_regex = re.compile(r'play_circle_filled.*?href="(?P<href>.*?)"', re.S)
        info_title_regex = re.compile(r'download="(?P<title>.*?)"')
        download_result = download_regex.search(download_resp.text)
        info_title_result = info_title_regex.search(download_resp.text)
        if download_result and info_title_result:
            download_href = download_result.group('href')
            info_title = info_title_result.group('title')
            cleaned_href = download_href.replace('&amp;', '&')
            download_urls.append(cleaned_href)
            infos.append({'title': info_title, 'url': cleaned_href})
        else:
            logger.warning("No download URL or title found on download page '%s'", href)
        download_resp.close()

    if not infos:
        logger.info("No downloadable audio information could be extracted")

    return infos, download_urls





