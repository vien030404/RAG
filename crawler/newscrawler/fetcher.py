import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HEADERS, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_RETRIES


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


_session = _build_session()


def fetch(url: str) -> tuple[str | None, int]:
    """Returns (html, http_status_code). html is None on error."""
    try:
        response = _session.get(url, timeout=REQUEST_TIMEOUT)
        status = response.status_code
        response.raise_for_status()
        response.encoding = "utf-8"
        time.sleep(REQUEST_DELAY)
        return response.text, status
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        print(f"[fetcher] HTTP {status} for {url}")
        return None, status
    except requests.RequestException as e:
        print(f"[fetcher] Error fetching {url}: {e}")
        return None, 0
