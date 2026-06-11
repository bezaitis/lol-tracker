import requests
import threading
import time
from typing import Optional, Dict, Any
from urllib.parse import quote
import logging

logger = logging.getLogger(__name__)

class RiotClient:
    AMERICAS_BASE_URL = "https://americas.api.riotgames.com"
    NA1_BASE_URL = "https://na1.api.riotgames.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key})

        self._lock = threading.Lock()
        self.request_times = []
        self.rate_limit_requests = 20
        self.rate_limit_window = 1

        self.summoner_cache: dict = {}
        self.cache_expiry: dict = {}
        self.cache_ttl = 3600

    def _handle_rate_limit(self):
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < self.rate_limit_window]
        if len(self.request_times) >= self.rate_limit_requests:
            sleep_time = self.rate_limit_window - (now - self.request_times[0])
            if sleep_time > 0:
                logger.warning(f"Rate limit hit, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
                self.request_times = []
        self.request_times.append(time.time())

    def _make_request(self, endpoint: str, base_url: str = None,
                      _attempt: int = 0) -> Optional[Dict[str, Any]]:
        """Make HTTP request with error handling and bounded 429 retry."""
        if base_url is None:
            base_url = self.NA1_BASE_URL

        with self._lock:
            self._handle_rate_limit()

        try:
            response = self.session.get(f"{base_url}{endpoint}")

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"Not found: {endpoint}")
                return None
            elif response.status_code == 429:
                if _attempt >= 3:
                    logger.error(f"429 after 3 retries, giving up: {endpoint}")
                    return None
                retry_after = int(response.headers.get("Retry-After", 10))
                logger.warning(f"Rate limited by Riot API, retrying in {retry_after}s (attempt {_attempt + 1})")
                time.sleep(retry_after)
                return self._make_request(endpoint, base_url, _attempt + 1)
            else:
                logger.error(f"API error {response.status_code}: {response.text}")
                return None

        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

    def get_ddragon_patch(self) -> str:
        """Fetch the current game patch version from Data Dragon."""
        try:
            resp = self.session.get(
                "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
            )
            if resp.status_code == 200:
                return resp.json()[0]
        except Exception as e:
            logger.warning(f"Could not fetch Data Dragon version: {e}")
        return "15.1.1"

    def get_summoner_by_name(self, summoner_name: str, tag: str = "NA1") -> Optional[Dict[str, Any]]:
        cache_key = f"{summoner_name}#{tag}".lower()
        with self._lock:
            if cache_key in self.summoner_cache:
                if time.time() - self.cache_expiry.get(cache_key, 0) < self.cache_ttl:
                    return self.summoner_cache[cache_key]

        encoded_name = quote(summoner_name, safe='')
        encoded_tag = quote(tag, safe='')
        endpoint = f"/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        data = self._make_request(endpoint, self.AMERICAS_BASE_URL)

        if data:
            with self._lock:
                self.summoner_cache[cache_key] = data
                self.cache_expiry[cache_key] = time.time()
        return data

    def get_summoner_by_puuid(self, puuid: str) -> Optional[Dict[str, Any]]:
        endpoint = f"/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return self._make_request(endpoint, self.NA1_BASE_URL)

    def get_ranked_stats(self, summoner_id: str = None, puuid: str = None) -> Optional[Dict[str, Any]]:
        if not summoner_id and not puuid:
            logger.error("get_ranked_stats requires either summoner_id or puuid")
            return None
        if puuid:
            endpoint = f"/lol/league/v4/entries/by-puuid/{puuid}"
        else:
            endpoint = f"/lol/league/v4/entries/by-summoner/{summoner_id}"
        return self._make_request(endpoint, self.NA1_BASE_URL)

    def get_recent_matches(self, puuid: str, start: int = 0, count: int = 5,
                           queue: int = 420) -> Optional[list]:
        params = f"start={start}&count={count}"
        if queue is not None:
            params += f"&queue={queue}"
        endpoint = f"/lol/match/v5/matches/by-puuid/{puuid}/ids?{params}"
        return self._make_request(endpoint, self.AMERICAS_BASE_URL)

    def get_match_details(self, match_id: str) -> Optional[Dict[str, Any]]:
        endpoint = f"/lol/match/v5/matches/{match_id}"
        return self._make_request(endpoint, self.AMERICAS_BASE_URL)

    def get_clash_tournaments(self) -> Optional[list]:
        endpoint = "/lol/clash/v1/tournaments"
        return self._make_request(endpoint, self.NA1_BASE_URL)

    def get_player_in_match(self, match_data: Dict[str, Any], puuid: str) -> Optional[Dict[str, Any]]:
        if not match_data or "info" not in match_data:
            return None
        for participant in match_data["info"]["participants"]:
            if participant.get("puuid") == puuid:
                return participant
        return None
