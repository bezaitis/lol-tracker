import requests
import time
from typing import Optional, Dict, Any
from urllib.parse import quote
import logging

logger = logging.getLogger(__name__)

class RiotClient:
    """
    Handles all communication with Riot API.
    Includes rate limiting and caching to respect API limits.
    """
    
    AMERICAS_BASE_URL = "https://americas.api.riotgames.com"
    NA1_BASE_URL = "https://na1.api.riotgames.com"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Riot-Token": api_key})
        
        # Rate limiting
        self.request_times = []
        self.rate_limit_requests = 20  # 20 requests per second
        self.rate_limit_window = 1
        
        # Cache (summoner_name -> {puuid, account_id, etc})
        self.summoner_cache = {}
        self.cache_expiry = {}
        self.cache_ttl = 3600  # 1 hour cache
    
    def _handle_rate_limit(self):
        """Implement rate limiting with backoff."""
        now = time.time()
        
        # Remove old timestamps outside window
        self.request_times = [t for t in self.request_times 
                             if now - t < self.rate_limit_window]
        
        # If at limit, wait
        if len(self.request_times) >= self.rate_limit_requests:
            sleep_time = self.rate_limit_window - (now - self.request_times[0])
            if sleep_time > 0:
                logger.warning(f"Rate limit hit, sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
                self.request_times = []
        
        self.request_times.append(time.time())
    
    def _make_request(self, endpoint: str, base_url: str = None) -> Optional[Dict[str, Any]]:
        """Make HTTP request with error handling."""
        self._handle_rate_limit()
        
        if base_url is None:
            base_url = self.NA1_BASE_URL
        
        try:
            response = self.session.get(f"{base_url}{endpoint}")
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"Not found: {endpoint}")
                return None
            elif response.status_code == 429:
                logger.error("Rate limited by Riot API")
                time.sleep(60)
                return self._make_request(endpoint, base_url)
            else:
                logger.error(f"API error {response.status_code}: {response.text}")
                return None
                
        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None
    
    def get_summoner_by_name(self, summoner_name: str, tag: str = "NA1") -> Optional[Dict[str, Any]]:
        """
        Get summoner info by name and tag using Account API v1.
        Properly URL-encodes special characters in names.
        Returns: {puuid, gameName, tagLine, ...}
        """
        cache_key = f"{summoner_name}#{tag}".lower()
        
        # Check cache
        if cache_key in self.summoner_cache:
            if time.time() - self.cache_expiry[cache_key] < self.cache_ttl:
                return self.summoner_cache[cache_key]
        
        # URL-encode the summoner name and tag to handle special characters
        encoded_name = quote(summoner_name, safe='')
        encoded_tag = quote(tag, safe='')
        
        # Use Account API v1 (correct endpoint)
        endpoint = f"/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        data = self._make_request(endpoint, self.AMERICAS_BASE_URL)
        
        if data:
            self.summoner_cache[cache_key] = data
            self.cache_expiry[cache_key] = time.time()
            return data
        return None
    
    def get_summoner_by_puuid(self, puuid: str) -> Optional[Dict[str, Any]]:
        """
        Get summoner data (including encrypted summoner_id) from puuid.
        Returns: {id, accountId, puuid, profileIconId, revisionDate, summonerLevel}
        """
        endpoint = f"/lol/summoner/v4/summoners/by-puuid/{puuid}"
        return self._make_request(endpoint, self.NA1_BASE_URL)

    def get_ranked_stats(self, summoner_id: str = None, puuid: str = None) -> Optional[Dict[str, Any]]:
        """
        Get current ranked stats for a summoner.
        Returns list of ranked queues (SOLO/DUO, FLEX, etc)
        """
        if not summoner_id and not puuid:
            logger.error("get_ranked_stats requires either summoner_id or puuid")
            return None

        if puuid:
            endpoint = f"/lol/league/v4/entries/by-puuid/{puuid}"
        else:
            endpoint = f"/lol/league/v4/entries/by-summoner/{summoner_id}"

        return self._make_request(endpoint, self.NA1_BASE_URL)
    
    def get_recent_matches(self, puuid: str, start: int = 0, count: int = 5, queue: int = 420) -> Optional[list]:
        """
        Get recent match IDs for a player.
        queue=420 is Ranked Solo/Duo (default). Pass queue=None to fetch all queues.
        Returns list of match IDs
        """
        params = f"start={start}&count={count}"
        if queue is not None:
            params += f"&queue={queue}"
        endpoint = f"/lol/match/v5/matches/by-puuid/{puuid}/ids?{params}"
        return self._make_request(endpoint, self.AMERICAS_BASE_URL)
    
    def get_match_details(self, match_id: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed match information.
        Returns full match data including participants, outcomes, stats, etc.
        """
        endpoint = f"/lol/match/v5/matches/{match_id}"
        return self._make_request(endpoint, self.AMERICAS_BASE_URL)
    
    def get_clash_tournaments(self) -> Optional[list]:
        """Get upcoming Clash tournaments for NA."""
        endpoint = "/lol/clash/v1/tournaments"
        return self._make_request(endpoint, self.NA1_BASE_URL)

    def get_player_in_match(self, match_data: Dict[str, Any], puuid: str) -> Optional[Dict[str, Any]]:
        """
        Extract a specific player's stats from match data.
        """
        if not match_data or "info" not in match_data:
            return None

        for participant in match_data["info"]["participants"]:
            if participant.get("puuid") == puuid:
                return participant

        return None
