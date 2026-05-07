"""API & mock data
https://requests.readthedocs.io/en/latest/user/quickstart/
https://developers.home-assistant.io/docs/integration_fetching_data
https://3.python-requests.org/user/advanced/
"""

import logging
import requests
import time
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse, parse_qs
from .const import HTTP_TIMEOUT
from datetime import datetime


_LOGGER = logging.getLogger(__name__)
    
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

class API:

    def _create_session(self):
        self.session = requests.Session()
        self.session.headers.update(defHeaders)
    

    def __init__(self, user: str, pwd: str) -> None:
        self.user = user
        self.pwd = pwd
        self._create_session()    
        self.isLoggedIn = False       


    def reset_session(self):
        self.session.close()
        self._create_session()
        self.isLoggedIn = False 


    def login(self):     
        if self.isLoggedIn:
            reset_session(self)

        while True:    
            _LOGGER.warning('Luminus Login called!')
            r = self.session.get(f"https://www.luminus.be/myluminus/nl/", timeout=30)
            u = urlparse(r.history[-1].headers['location'])
            q = parse_qs(u.query)
            s = q['state'][0]

            authUriQry = { 'state': s}
            idHeaders = { 
                'Origin': 'https://login.luminus.be', 
                'Referer': 'https://login.luminus.be/u/login/identifier?state=' + s
            }        
            idReqBody = { 
                'state': s, 
                'username': self.user, 
                'js-available': 'false', 
                'webauthn-available' : 'false', 
                'is-brave': 'false', 
                'webauthn-platform-available' : 'false', 
                'action': 'default' 
            }
            idReq = self.session.post('https://login.luminus.be/u/login/identifier', params=authUriQry, data=idReqBody, timeout=30, headers=idHeaders)
            
            if idReq.status_code != requests.codes.ok:
                _LOGGER.warning(f'Login 1 status code: {idReq.status_code}')
                time.sleep(15)
                continue
                
            authHeaders = { 
                'Origin': 'https://login.luminus.be', 
                'Referer': 'https://login.luminus.be/u/login/password?state=' + s
            }

            authReqBody = { 
                'state': s, 
                'username': self.user, 
                'password': self.pwd,
                'action': 'default' 
            }

            authReq = self.session.post('https://login.luminus.be/u/login/password', params=authUriQry, data=authReqBody, timeout=30, headers=authHeaders)       
            self.isLoggedIn = authReq.status_code == requests.codes.ok
            
            if authReq.status_code != requests.codes.ok:
                _LOGGER.warning(f'Login 2 status code: {authReq.status_code}')
                time.sleep(15)
                continue
            else:
                break
                
            _LOGGER.info('Luminus logged in!')


    def get_meters(self) -> list[dict[str, Any]]:
        return self.get_data('https://www.luminus.be/myluminus/api/meter-readings/available-sources')
        

    def get_meter(self, ean: str) -> dict[str, Any]:
        return self.get_data(f"https://www.luminus.be/myluminus/api/price-information/{ean}")
        

    def get_data(self, url: str) -> list[dict[str, Any]]:
        while True:
            try:
                if self.isLoggedIn:
                    r = self.session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=False)

                    if r.status_code != requests.codes.ok:
                        _LOGGER.warning("Luminus response error", r.url, r.status_code, r.text)
                        self.reset_session()
                        self.login()
                        continue
                    else:
                        return r.json()
                else:
                    self.reset_session()
                    self.login()

            except Exception as e:
                _LOGGER.warning("Error within get_data()")
                time.sleep(15)


    def get_current_consumption(self, ean:str):
        current_year = datetime.now().year
        current_month = datetime.now().month
        current_day = datetime.now().day
        url_year = current_year - 1 if (current_month < 5 or (current_month == 5 and current_day < 16)) else current_year
        date_from = f"{url_year}-04-30T23:59:59.999Z"
        periodicity = "TwelveMonths"

        return self.get_data(f"https://www.luminus.be/myluminus/api/meter-readings/for/{ean}?dateFrom={date_from}&periodicity={periodicity}")


    def get_advance_and_paid(self) -> list[dict[str, Any]]:        
        return self.get_data(f"https://www.luminus.be/myluminus/api/budget-billing")


    def logout(self):
        if not self.isLoggedIn:
            return

        try:
            _LOGGER.warning("Logout LUMINUS")
            r = self.session.get(f"https://www.luminus.be/myluminus/api/auth/logout", timeout=HTTP_TIMEOUT)
            self.isLoggedIn = False
            return r.json()
        except requests.exceptions.ConnectTimeout as e:
            _LOGGER.warning("Error logging out.")

