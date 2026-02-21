"""Abstract base class for sport data feeds."""

from abc import ABC, abstractmethod
from ..models import LiveEvent


class BaseSportFeed(ABC):
    @abstractmethod
    def poll(self) -> list:
        """Fetch current live events. Returns only NEW events since last poll."""
        ...

    @abstractmethod
    def sport_name(self) -> str:
        """Return a string identifying the sport (e.g. 'soccer')."""
        ...
