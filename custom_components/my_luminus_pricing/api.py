"""My Luminus API client.
https://requests.readthedocs.io/en/latest/user/quickstart/
https://developers.home-assistant.io/docs/integration_fetching_data

Authentication uses the Auth0 client of the Luminus mobile app (Android package
be.luminus.bsp). An email/password login (OAuth2 password grant) returns an
access token (valid 24 h) and a refresh token. The access token is renewed with
the refresh token; when Luminus no longer accepts the refresh token, the saved
email and password are used to log in again. Data comes from the endpoints the
app uses:

- https://myluminus-api.luminus.be/  (app backend, same data as the website API)
- https://mobileapi.luminus.be/api/v11/  (mobile API, invoices)

The previous authentication (browser session cookies on www.luminus.be) was
removed: the website session expired within a day. The website endpoints it
used are kept below as comments, next to the app endpoint that replaces them.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

from .const import HTTP_TIMEOUT


_LOGGER = logging.getLogger(__name__)

# Auth0 settings of the Luminus mobile app (public client, no secret)
AUTH_URL = 'https://login.luminus.be'
CLIENT_ID = 'IKPSr122AreYvrlNgRVE8o3vuscGETbe'
SCOPE = 'openid profile offline_access email email_verified'

APP_API_URL = 'https://myluminus-api.luminus.be/'
MOBILE_API_URL = 'https://mobileapi.luminus.be/api/v11/'

# Settlement PDFs are served from Azure blob storage through signed links
DOCUMENT_HOST_SUFFIX = '.blob.core.windows.net'

# Renew the access token this many seconds before it expires
TOKEN_MARGIN = 300

# Auth0 errors that need the user to act (wrong password, account blocked, MFA, ...)
AUTH_ERRORS = {'invalid_grant', 'unauthorized_client', 'access_denied', 'mfa_required', 'too_many_attempts'}

defHeaders = {
    'Accept': 'application/json',
    'Accept-Language': 'nl',
    'User-Agent': 'okhttp/4.12.0',
}

# Previous authentication, kept for reference (website, session cookies):
# BASE_URL = 'https://www.luminus.be'
# RENEW_PATH = '/myluminus/nl'


class AuthError(Exception):
    """Luminus rejected the email and password; the user must log in again."""


class VerificationRequired(AuthError):
    """Luminus' bot detection wants a CAPTCHA for this login; retrying does not help."""


class API:

    def __init__(self, username: str, password: str, refresh_token: str | None = None) -> None:
        self.username = username
        self._password = password
        # Persisted by the coordinator; may be replaced by Luminus at any refresh
        self.refresh_token = refresh_token
        self._access_token: str | None = None
        self._access_expires = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(defHeaders)


    # ----------------------------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------------------------

    def _post_token(self, fields: dict[str, str]) -> dict[str, Any]:
        """Call the Auth0 token endpoint; raise AuthError when the user must act."""
        r = self.session.post(
            f'{AUTH_URL}/oauth/token',
            data={'client_id': CLIENT_ID, **fields},
            timeout=HTTP_TIMEOUT,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == requests.codes.ok and body.get('access_token'):
            return body
        error = body.get('error', '')
        description = body.get('error_description', '')
        if error == 'requires_verification':
            raise VerificationRequired(f'{error}: {description}'.strip(': '))
        if r.status_code in (400, 401, 403) and error in AUTH_ERRORS:
            raise AuthError(f'{error}: {description}'.strip(': '))
        raise RuntimeError(f'Luminus login failed: status {r.status_code} {error} {description}'.strip())


    def _store_tokens(self, body: dict[str, Any]) -> None:
        self._access_token = body['access_token']
        self._access_expires = time.monotonic() + float(body.get('expires_in', 3600))
        # Luminus does not rotate refresh tokens today, but keep any new one it sends
        if body.get('refresh_token') and body['refresh_token'] != self.refresh_token:
            if self.refresh_token:
                _LOGGER.info('Luminus issued a new refresh token')
            self.refresh_token = body['refresh_token']


    def login(self) -> None:
        """Log in with email and password; gives a new access and refresh token."""
        with self._lock:
            self._login()


    def _login(self) -> None:
        body = self._post_token({
            'grant_type': 'password',
            'username': self.username,
            'password': self._password,
            'scope': SCOPE,
        })
        self._store_tokens(body)
        _LOGGER.info('Logged in to Luminus')


    def _ensure_token(self, force: bool = False) -> str:
        """Return a valid access token, refreshing or logging in when needed."""
        with self._lock:
            if not force and self._access_token and time.monotonic() < self._access_expires - TOKEN_MARGIN:
                return self._access_token
            if self.refresh_token:
                try:
                    self._store_tokens(self._post_token({
                        'grant_type': 'refresh_token',
                        'refresh_token': self.refresh_token,
                    }))
                    _LOGGER.debug('Luminus access token refreshed')
                    return self._access_token
                except VerificationRequired:
                    raise
                except AuthError as err:
                    # Refresh token expired or revoked: log in again with the saved credentials
                    _LOGGER.info('Luminus refresh token no longer accepted (%s); logging in again', err)
                    self.refresh_token = None
            self._login()
            return self._access_token


    # ----------------------------------------------------------------------------
    # Requests
    # ----------------------------------------------------------------------------

    def get_data(self, url: str) -> Any:
        """GET JSON with the access token; on 401 renew the token once and retry."""
        for attempt in (1, 2):
            token = self._ensure_token(force=attempt == 2)
            r = self.session.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=HTTP_TIMEOUT)
            _LOGGER.debug('GET %s: status %s', url, r.status_code)
            if r.status_code != 401:
                break
        if r.status_code == 401:
            raise AuthError(f'Luminus refused the access token for {url}')
        if r.status_code != requests.codes.ok:
            raise RuntimeError(f'Luminus returned status {r.status_code} for {url}')
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(f'Luminus returned non-JSON content for {url}') from e


    def get_meters(self) -> dict[str, Any]:
        # Website: self.get_data('/myluminus/api/meter-readings/available-sources')
        return self.get_data(APP_API_URL + 'meter-readings/available-sources')


    def get_meter(self, ean: str) -> dict[str, Any]:
        # Website: self.get_data(f"/myluminus/api/price-information/{ean}")
        return self.get_data(APP_API_URL + f'manage-contract/pricing/{ean}')


    def get_current_consumption(self, ean: str) -> dict[str, Any]:
        current_year = datetime.now().year
        current_month = datetime.now().month
        url_year = current_year - 1 if current_month < 5 else current_year
        date_from = f"{url_year}-04-30T23:59:59.999Z"
        periodicity = "TwelveMonths"

        # Website: self.get_data(f"/myluminus/api/meter-readings/for/{ean}?dateFrom={date_from}&periodicity={periodicity}")
        return self.get_data(APP_API_URL + f'meter-readings/for/{ean}?dateFrom={date_from}&periodicity={periodicity}')


    def get_advance_and_paid(self) -> list[dict[str, Any]]:
        # Website: self.get_data("/myluminus/api/budget-billing")
        # Same fields per EAN (ean, advanceAmount, simulation, ...)
        return self.get_data(APP_API_URL + 'budget-billing/plans')


    def get_invoices(self) -> list[dict[str, Any]]:
        """Invoices of the account (mobile API).

        Fields used: Category ("Settlement", "Advance"), EnergyType ("Electricity",
        "Gas", "ElectricityAndGas"), DocumentNumber, Date, DownloadDetailAvailable.
        """
        # Website: self.get_data('/myluminus/api/invoices').get('invoices', [])
        # (fields type, id, date, documents, contractInfo[].ean/energyType)
        return self.get_data(MOBILE_API_URL + 'GetAccountStatement').get('Invoices') or []


    def get_invoice_pdf(self, document_number: str) -> bytes:
        """Download the detailed PDF of an invoice (settlements list every tariff line)."""
        # Website:
        # meta = self.get_data(f'/myluminus/api/invoice-document/{invoice_id}?format=pdf&detailed=true')
        # url = urlparse(meta.get('contentUrl', ''))
        # if url.scheme != 'https' or url.netloc != urlparse(BASE_URL).netloc:
        #     raise RuntimeError(f'Unexpected document location for invoice {invoice_id}: {url.netloc}')
        # r = self._request(url.path + (f'?{url.query}' if url.query else ''))
        meta = self.get_data(APP_API_URL + f'documents/commodity/invoice/{document_number}?detailed=true')
        url = urlparse(meta.get('contentUrl', ''))
        if url.scheme != 'https' or not url.netloc.endswith(DOCUMENT_HOST_SUFFIX):
            raise RuntimeError(f'Unexpected document location for invoice {document_number}: {url.netloc}')
        # Signed link: download without the access token (the storage refuses it anyway)
        r = requests.get(meta['contentUrl'], headers={'User-Agent': defHeaders['User-Agent']}, timeout=HTTP_TIMEOUT)
        if r.status_code != requests.codes.ok:
            raise RuntimeError(f'Invoice {document_number} download returned status {r.status_code}')
        if not r.content.startswith(b'%PDF'):
            raise RuntimeError(f'Invoice {document_number} document is not a PDF')
        return r.content
