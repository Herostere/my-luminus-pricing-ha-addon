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
from datetime import datetime


_LOGGER = logging.getLogger(__name__)
LOGGING_TRIES = 0
    
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
    

    def __init__(self) -> None:
        self.user = "luminus.rectify240@passmail.net"
        self.pwd = "Drool3-Cadmium8-Contend4-Wildfowl4-Pulverize8"
        self._create_session()    
        self.isLoggedIn = False       


    def reset_session(self):
        self.session.close()
        self._create_session()
        self.isLoggedIn = False 


    def login(self):   
        global LOGGING_TRIES  
        
        if self.isLoggedIn:
            self.reset_session()

        while True:    
            _LOGGER.warning('Luminus Login called!')
            r = self.session.get(f"https://www.luminus.be/myluminus/nl/", timeout=30)
            u = urlparse(r.history[-1].headers['location'])
            q = parse_qs(u.query)
            try:
                s = q['state'][0]
            except KeyError as e:
                continue

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
                LOGGING_TRIES += 1
                time.sleep(15 * LOGGING_TRIES)
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
            
            if authReq.status_code != requests.codes.ok and authReq.status_code != 500:
                _LOGGER.warning(f'Login 2 status code: {authReq.status_code}')
                _LOGGER.warning(authReq.text)
                LOGGING_TRIES += 1
                time.sleep(15 * LOGGING_TRIES)
                continue
            
            self.isLoggedIn = authReq.status_code == requests.codes.ok
            
            if self.isLoggedIn:
                LOGGING_TRIES = 0
                break

            _LOGGER.info('Luminus logged in!')


if __name__=="__main__":
    print("test")
    api = API()
    api.login()
