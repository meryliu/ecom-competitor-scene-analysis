"""Stable Python API shared by the standalone and embedded attribution tools."""

from .engine import AttributionError, run
from .identity import identity
from .query import query_operator
from .registry import get_operator, operator_matches, route_operator
from .semantics import derive_direction, normalize_direction, normalize_ranking, rank_rows

__all__ = [
    "AttributionError",
    "get_operator",
    "identity",
    "derive_direction",
    "normalize_direction",
    "normalize_ranking",
    "operator_matches",
    "query_operator",
    "route_operator",
    "rank_rows",
    "run",
]
