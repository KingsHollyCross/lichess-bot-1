import math
from urllib.parse import urljoin
import logging
from timer import Timer
from config import Configuration
from typing import Dict, Any, Optional, Tuple, List, DefaultDict

logger = logging.getLogger(__name__)


class Challenge:
    def __init__(self, c_info: Dict[str, Any], user_profile: Dict[str, Any]) -> None:
        self.id = c_info["id"]
        self.rated = c_info["rated"]
        self.variant = c_info["variant"]["key"]
        self.perf_name = c_info["perf"]["name"]
        self.speed = c_info["speed"]
        self.increment = c_info.get("timeControl", {}).get("increment")
        self.base = c_info.get("timeControl", {}).get("limit")
        self.days = c_info.get("timeControl", {}).get("daysPerTurn")
        self.challenger = c_info.get("challenger") or {}
        self.challenger_title = self.challenger.get("title")
        self.challenger_is_bot = self.challenger_title == "BOT"
        self.challenger_master_title = self.challenger_title if not self.challenger_is_bot else None
        self.challenger_name = self.challenger.get("name", "Anonymous")
        self.challenger_rating_int = self.challenger.get("rating", 0)
        self.challenger_rating = self.challenger_rating_int or "?"
        self.from_self = self.challenger_name == user_profile["username"]

    def is_supported_variant(self, challenge_cfg: Configuration) -> bool:
        return self.variant in challenge_cfg.variants

    def is_supported_time_control(self, challenge_cfg: Configuration) -> bool:
        speeds = challenge_cfg.time_controls
        increment_max = challenge_cfg.max_increment
        increment_min = challenge_cfg.min_increment
        base_max = challenge_cfg.max_base
        base_min = challenge_cfg.min_base
        days_max = challenge_cfg.max_days
        days_min = challenge_cfg.min_days

        if self.speed not in speeds:
            return False

        if self.base is not None and self.increment is not None:
            # Normal clock game
            return (increment_min <= self.increment <= increment_max
                    and base_min <= self.base <= base_max)
        elif self.days is not None:
            # Correspondence game
            return days_min <= self.days <= days_max
        else:
            # Unlimited game
            return days_max == math.inf

    def is_supported_mode(self, challenge_cfg: Configuration) -> bool:
        return ("rated" if self.rated else "casual") in challenge_cfg.modes

    def is_supported_recent(self, config: Configuration, recent_bot_challenges: DefaultDict[str, List[Timer]]) -> bool:
        # Filter out old challenges
        recent_bot_challenges[self.challenger_name] = [timer for timer
                                                       in recent_bot_challenges[self.challenger_name]
                                                       if not timer.is_expired()]
        max_recent_challenges = config.max_recent_bot_challenges
        return (not self.challenger_is_bot
                or max_recent_challenges is None
                or len(recent_bot_challenges[self.challenger_name]) < max_recent_challenges)

    def decline_due_to(self, requirement_met: bool, decline_reason: str) -> Optional[str]:
        return None if requirement_met else decline_reason

    def is_supported(self, config: Configuration,
                     recent_bot_challenges: DefaultDict[str, List[Timer]]) -> Tuple[bool, Optional[str]]:
        try:
            if self.from_self:
                return True, None

            decline_reason = (self.decline_due_to(config.accept_bot or not self.challenger_is_bot, "noBot")
                              or self.decline_due_to(not config.only_bot or self.challenger_is_bot, "onlyBot")
                              or self.decline_due_to(self.is_supported_time_control(config), "timeControl")
                              or self.decline_due_to(self.is_supported_variant(config), "variant")
                              or self.decline_due_to(self.is_supported_mode(config), "casual" if self.rated else "rated")
                              or self.decline_due_to(self.challenger_name not in config.block_list, "generic")
                              or self.decline_due_to(self.is_supported_recent(config, recent_bot_challenges), "later"))

            return decline_reason is None, decline_reason

        except Exception:
            logger.exception(f"Error while checking challenge {self.id}:")
            return False, "generic"

    def score(self) -> int:
        rated_bonus = 200 if self.rated else 0
        titled_bonus = 200 if self.challenger_master_title else 0
        return self.challenger_rating_int + rated_bonus + titled_bonus

    def mode(self) -> str:
        return "rated" if self.rated else "casual"

    def challenger_full_name(self) -> str:
        return f'{self.challenger_title or ""} {self.challenger_name}'.strip()

    def __str__(self) -> str:
        return (f"{self.perf_name} {self.mode()} challenge from {self.challenger_full_name()}({self.challenger_rating})"
                f" ({self.id})")

    def __repr__(self) -> str:
        return self.__str__()


class Game:
    def __init__(self, json: Dict[str, Any], username: str, base_url: str, abort_time: int) -> None:
        self.username = username
        self.id = json.get("id")
        self.speed = json.get("speed")
        clock = json.get("clock") or {}
        ten_years_in_ms = 1000 * 3600 * 24 * 365 * 10
        self.clock_initial = clock.get("initial", ten_years_in_ms)
        self.clock_increment = clock.get("increment", 0)
        self.perf_name = (json.get("perf") or {}).get("name", "{perf?}")
        self.variant_name = json.get("variant")["name"]
        self.white = Player(json.get("white"))
        self.black = Player(json.get("black"))
        self.initial_fen = json.get("initialFen")
        self.state = json.get("state")
        self.is_white = (self.white.name or "").lower() == username.lower()
        self.my_color = "white" if self.is_white else "black"
        self.opponent_color = "black" if self.is_white else "white"
        self.me = self.white if self.is_white else self.black
        self.opponent = self.black if self.is_white else self.white
        self.base_url = base_url
        self.abort_time = Timer(abort_time)
        self.terminate_time = Timer((self.clock_initial + self.clock_increment) / 1000 + abort_time + 60)
        self.disconnect_time = Timer(0)

    def url(self) -> str:
        return urljoin(self.base_url, f"{self.id}/{self.my_color}")

    def is_abortable(self) -> bool:
        return len(self.state["moves"]) < 6

    def ping(self, abort_in: int, terminate_in: int, disconnect_in: int) -> None:
        if self.is_abortable():
            self.abort_time = Timer(abort_in)
        self.terminate_time = Timer(terminate_in)
        self.disconnect_time = Timer(disconnect_in)

    def should_abort_now(self) -> bool:
        return self.is_abortable() and self.abort_time.is_expired()

    def should_terminate_now(self) -> bool:
        return self.terminate_time.is_expired()

    def should_disconnect_now(self) -> bool:
        return self.disconnect_time.is_expired()

    def my_remaining_seconds(self) -> float:
        return (self.state["wtime"] if self.is_white else self.state["btime"]) / 1000

    def __str__(self) -> str:
        return f"{self.url()} {self.perf_name} vs {self.opponent.__str__()} ({self.id})"

    def __repr__(self) -> str:
        return self.__str__()


class Player:
    def __init__(self, json: Dict[str, Any]) -> None:
        self.name = json.get("name")
        self.title = json.get("title")
        self.rating = json.get("rating")
        self.provisional = json.get("provisional")
        self.aiLevel = json.get("aiLevel")

    def __str__(self) -> str:
        if self.aiLevel:
            return f"AI level {self.aiLevel}"
        else:
            rating = f'{self.rating}{"?" if self.provisional else ""}'
            return f'{self.title or ""} {self.name}({rating})'.strip()

    def __repr__(self) -> str:
        return self.__str__()
