"""The contract every agent adapter implements, and the errors it may raise.

The runner maps each ``AdapterError`` subclass to a ``FailureType`` via its ``failure_type``
attribute. ``AuthError`` is deliberately *not* an ``AdapterError``: a rejected key would fail
every case, so it aborts the whole run instead of being recorded per case.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import TracebackType
from typing import Any, ClassVar, Self

from evalkit.schemas import AgentResponse, FailureType, TestCase


@dataclass(frozen=True)
class AgentReply:
    """A parsed agent response plus how many HTTP attempts it took."""

    response: AgentResponse
    attempts: int


class AdapterError(Exception):
    """A per-case failure. ``attempts`` is how many requests were made (0 = none)."""

    failure_type: ClassVar[FailureType]

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class AgentTimeout(AdapterError):
    failure_type = FailureType.TIMEOUT


class AgentConnectionError(AdapterError):
    failure_type = FailureType.CONNECTION


class AgentHTTPError(AdapterError):
    failure_type = FailureType.HTTP_ERROR

    def __init__(self, message: str, *, status: int, attempts: int = 1) -> None:
        super().__init__(message, attempts=attempts)
        self.status = status


class AgentParseError(AdapterError):
    failure_type = FailureType.PARSE_ERROR


class CaseSkipped(AdapterError):
    failure_type = FailureType.SKIPPED

    def __init__(self, message: str) -> None:
        super().__init__(message, attempts=0)


class AuthError(Exception):
    """The agent rejected the API key (HTTP 401/403). Aborts the run."""


class ConfigError(Exception):
    """The adapter is missing required configuration (e.g. an env var)."""


class AgentAdapter(ABC):
    """An agent under evaluation."""

    name: ClassVar[str]

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Return the agent's health payload. Raises AdapterError if unreachable."""

    @abstractmethod
    async def answer(self, case: TestCase) -> AgentReply:
        """Send one case. Raises AdapterError (per-case failure) or AuthError (abort)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release network resources."""

    def settings(self) -> dict[str, Any]:
        """Settings that affect results; part of the run's config_hash. No secrets."""
        return {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
