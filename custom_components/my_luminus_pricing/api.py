"""API & mock data
https://requests.readthedocs.io/en/latest/user/quickstart/
https://developers.home-assistant.io/docs/integration_fetching_data
https://3.python-requests.org/user/advanced/

Authentication uses the cookies of a My Luminus session started in a browser.
The username/password login was dropped because Luminus' Auth0 login now shows
a CAPTCHA to automated clients. The session cookies (__session__0, __session__1)
are valid for 24 hours and are renewed by loading a My Luminus page (renew()).
"""

import http.cookiejar
import logging
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests

from .const import HTTP_TIMEOUT


_LOGGER = logging.getLogger(__name__)

BASE_URL = 'https://www.luminus.be'
RENEW_PATH = '/myluminus/nl'

defHeaders = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'nl,en-US;q=0.7,en;q=0.3',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests' : '1',
    'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0'
}


class AuthError(Exception):
    """The browser cookies no longer give access; fresh cookies are needed."""


class IncapsulaBlocked(AuthError):
    """Luminus' Incapsula bot protection blocked the request."""


class SessionExpired(AuthError):
    """The My Luminus session has expired or was logged out."""


def parse_cookies(raw: str | None) -> dict[str, str]:
    """Parse a 'name=value; name=value' cookie header into a dict.

    Accepts an optional 'www.luminus.be:' prefix per line. When a name occurs
    twice (same cookie on different paths), the last one wins: browsers list the
    most specific path first, so the last one is the site-wide ('/') cookie.
    """
    cookies = {}
    for line in (raw or '').splitlines():
        line = line.strip()
        if line.lower().startswith('www.luminus.be:'):
            line = line.split(':', 1)[1]
        elif line.lower().startswith('login.luminus.be:'):
            continue
        for part in line.split(';'):
            if '=' in part:
                name, value = part.split('=', 1)
                if name.strip():
                    cookies[name.strip()] = value.strip()
    return cookies


def _is_deletion(attrs: dict[str, str]) -> bool:
    """True when Set-Cookie attributes expire the cookie immediately."""
    max_age = attrs.get('max-age')
    if max_age is not None:
        try:
            return int(max_age) <= 0
        except ValueError:
            return False
    expires = attrs.get('expires')
    if expires:
        try:
            return parsedate_to_datetime(expires) <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False
    return False


class API:

    def __init__(self, cookies: dict[str, str] | str) -> None:
        self.cookies: dict[str, str] = parse_cookies(cookies) if isinstance(cookies, str) else dict(cookies)
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(defHeaders)
        # Cookies are managed by hand in self.cookies (see _apply_set_cookies), so
        # block the session's own cookie jar to avoid duplicate cookies.
        self.session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


    @property
    def account_id(self) -> str | None:
        return self.cookies.get('MyLuminus-AccountId')


    def _apply_set_cookies(self, r: requests.Response) -> None:
        """Store cookies the server sets or renews, like a browser would."""
        for header in r.raw.headers.getlist('Set-Cookie'):
            first, _, rest = header.partition(';')
            if '=' not in first:
                continue
            name, value = (x.strip() for x in first.split('=', 1))
            attrs = {}
            for a in rest.split(';'):
                k, _, v = a.partition('=')
                attrs[k.strip().lower()] = v.strip()
            path = attrs.get('path', '/')

            if _is_deletion(attrs):
                # Luminus also clears stale copies under /myluminus; only a
                # deletion of the site-wide cookie removes ours.
                if path == '/':
                    self.cookies.pop(name, None)
            elif path == '/':
                self.cookies[name] = value


    def _request(self, path: str) -> requests.Response:
        with self._lock:
            header = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
            r = self.session.get(
                BASE_URL + path,
                headers={'Cookie': header},
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
            )
            self._apply_set_cookies(r)

        if r.status_code == 403 and 'Incapsula' in r.text:
            raise IncapsulaBlocked('Blocked by Incapsula bot protection')
        if r.is_redirect or r.status_code in (401, 403):
            location = r.headers.get('location', '').split('?')[0]
            raise SessionExpired(f'Session not accepted: status {r.status_code} {location}'.strip())
        if r.status_code != requests.codes.ok:
            raise RuntimeError(f'Luminus returned status {r.status_code} for {path}')
        return r


    def renew(self) -> None:
        """Load a My Luminus page; the response renews the 24 h session cookies."""
        self._request(RENEW_PATH)
        _LOGGER.debug('Luminus session renewed')


    def get_meters(self) -> list[dict[str, Any]]:
        return self.get_data('/myluminus/api/meter-readings/available-sources')


    def get_meter(self, ean: str) -> dict[str, Any]:
        return self.get_data(f"/myluminus/api/price-information/{ean}")


    def get_data(self, path: str) -> list[dict[str, Any]]:
        r = self._request(path)
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(f'Luminus returned non-JSON content for {path}') from e


    def get_current_consumption(self, ean:str):
        current_year = datetime.now().year
        current_month = datetime.now().month
        current_day = datetime.now().day
        url_year = current_year - 1 if current_month < 5  else current_year  # (current_month < 5 or (current_month == 6 and current_day < 18)) else current_year
        date_from = f"{url_year}-04-30T23:59:59.999Z"
        periodicity = "TwelveMonths"

        return self.get_data(f"/myluminus/api/meter-readings/for/{ean}?dateFrom={date_from}&periodicity={periodicity}")


    def get_advance_and_paid(self) -> list[dict[str, Any]]:
        return self.get_data(f"/myluminus/api/budget-billing")


    def get_invoices(self) -> list[dict[str, Any]]:
        return self.get_data('/myluminus/api/invoices').get('invoices', [])


    def get_invoice_pdf(self, invoice_id: str) -> bytes:
        """Download the detailed PDF of an invoice (settlements list every tariff line)."""
        meta = self.get_data(f'/myluminus/api/invoice-document/{invoice_id}?format=pdf&detailed=true')
        url = urlparse(meta.get('contentUrl', ''))
        # Only follow links on www.luminus.be: the session cookies are sent with the request
        if url.scheme != 'https' or url.netloc != urlparse(BASE_URL).netloc:
            raise RuntimeError(f'Unexpected document location for invoice {invoice_id}: {url.netloc}')
        r = self._request(url.path + (f'?{url.query}' if url.query else ''))
        if not r.content.startswith(b'%PDF'):
            raise RuntimeError(f'Invoice {invoice_id} document is not a PDF')
        return r.content
