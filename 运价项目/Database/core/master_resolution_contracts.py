"""Read-only names contract; quote interpretation is out of scope."""
from typing import Literal, NotRequired, TypedDict

MASTER_RESOLVE_ROUTE = "/api/master/resolve"
MASTER_RESOLVE_LIMIT = 200
MASTER_NAME_MAX_LENGTH = 256
MASTER_RESOLUTION_STAGE = "shadow"
MASTER_RESOLVE_TYPES = frozenset({"pol", "pod", "carrier", "route"})
PORT_GROUP_INDEX_FIELD = "portGroupIndex"
PORT_GROUP_ALIAS_FIELD = "aliases"


class NameInput(TypedDict):
    type: Literal["pol", "pod", "carrier", "route"]
    value: str


class NameResolution(TypedDict):
    input: str
    entity: str | None
    canonical: str | None
    status: Literal["resolved", "unknown", "ambiguous", "invalid_target"]
    candidates: NotRequired[list[dict[str, str]]]
