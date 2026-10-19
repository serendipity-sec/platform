"""Deterministic local card retrieval and registered normal planning."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = ["compose_plan", "load_cards", "make_plan", "retrieve", "write_plan"]

if TYPE_CHECKING:
    from flowdelta.knowledge.planner import (
        compose_plan,
        load_cards,
        make_plan,
        retrieve,
        write_plan,
    )


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    planner = import_module(".planner", __name__)
    return getattr(planner, name)
