"""Knock adapter: the only place that talks to Knock (WP-568 F17 item 5).

PROVISIONAL placement: the working folder name and the repository are not
confirmed by the pilot yet. The package depends on the standard library only,
so moving it is a plain `git mv`. Add `tzdata` to the image requirements: slim
images may have no time zone database.
"""
from .client import KnockAdapter
from .models import BatchResult, Outcome, Person, Result, RuleSpec
from .timecheck import TimeCheckError
from .transport import AdapterConfig

__all__ = [
    "AdapterConfig",
    "BatchResult",
    "KnockAdapter",
    "Outcome",
    "Person",
    "Result",
    "RuleSpec",
    "TimeCheckError",
]
