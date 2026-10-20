"""
GME API login (api.mercatoelettrico.org /Auth), shared by the get_token of
fetch_gme_units.py, fetch_pun.py and fetch_zonal.py.

GME's API is unreachable now and then for a few minutes (a connect timeout
at login failed the market offers job on 19 October 2026, the jobs either
side of it ran), so the login is tried up to LOGIN_ATTEMPTS times: again
after a timeout, a dropped connection, HTTP 429 or a server error (5xx),
waiting LOGIN_WAITS_SECONDS in between.  Wrong credentials (another 4xx, or
GME answering success: false) fail at once.  Worst case, with every attempt
timing out, about 8 minutes, within the shortest GME job's limit (15).

The login and password are never printed.
"""

import time

import requests

AUTH_URL = "https://api.mercatoelettrico.org/request/api/v1/Auth"

LOGIN_ATTEMPTS = 4
LOGIN_WAITS_SECONDS = (15, 45, 90)
# (connect, read) seconds.
LOGIN_TIMEOUT = (20, 60)


def _retryable(error):
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    return response is not None and (response.status_code == 429 or response.status_code >= 500)


def login(login, password, url=AUTH_URL, sleep=time.sleep):
    """The JWT token GME returns for the login."""
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        try:
            response = requests.post(url, json={"Login": login, "Password": password}, timeout=LOGIN_TIMEOUT)
            response.raise_for_status()
            break
        except requests.RequestException as error:
            if attempt == LOGIN_ATTEMPTS or not _retryable(error):
                raise
            wait = LOGIN_WAITS_SECONDS[attempt - 1]
            print(f"  GME login, attempt {attempt} of {LOGIN_ATTEMPTS}: {type(error).__name__}; trying again in {wait}s")
            sleep(wait)

    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"GME authentication failed: {payload.get('reason', 'Unknown authentication error')}")
    token = payload.get("token")
    if not token:
        raise RuntimeError("GME authentication succeeded but no token was returned.")
    return token
