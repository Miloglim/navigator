"""Pure name matching with explicit ambiguity and scope boundaries."""
import re
import unicodedata
from core.master_resolution_contracts import MASTER_RESOLVE_LIMIT, MASTER_NAME_MAX_LENGTH, MASTER_RESOLVE_TYPES


def fold_name(value):
    value = unicodedata.normalize("NFKD", str(value or "").strip())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[.,'’\-()]", " ", value.lower())).strip()


def alias_list(value):
    return [v.strip() for v in re.split(r"[,，、;；]", str(value or "")) if v.strip()]


class NameResolver:
    def __init__(self, ports, carriers, routes, groups, scopes):
        self.canonical, self.aliases, self.targets, self.scopes = {}, {}, {}, {}
        collections = (("port", ports, "name", ("name_cn", "name_en", "city")),
                       ("carrier", carriers, "code", ("name_cn", "name_en")),
                       ("route", routes, "name", ()), ("port_group", groups, "name", ("label",)))
        for entity, rows, primary, columns in collections:
            for row in rows:
                if not row.get("active", 1):
                    continue
                canonical = row[primary]
                sides = ("pol", "pod") if entity == "port" else (entity,)
                if entity == "port_group":
                    kind = row.get("kind") or "pod"
                    sides = ("pol", "pod") if kind == "both" else (kind,)
                for side in sides:
                    candidate = (entity, canonical)
                    self.targets.setdefault((side, canonical), set()).add(candidate)
                    self._add(self.canonical, side, canonical, candidate)
                    values = [row.get(col) for col in columns] + alias_list(row.get("aliases"))
                    if entity == "port_group":
                        values.append(re.split(r"[（(]", str(row.get("label") or ""))[0].strip())
                    for value in values:
                        self._add(self.aliases, side, value, candidate)
        for row in scopes:
            if row["kind"] not in MASTER_RESOLVE_TYPES:
                continue  # Container/space-type interpretation remains a business rule.
            key = (row["kind"], fold_name(row["alias"]), row.get("scope_group") or "*")
            self.scopes.setdefault(key, set()).add(row["target"])

    @staticmethod
    def _add(index, side, value, candidate):
        key = fold_name(value)
        if key:
            index.setdefault((side, key), set()).add(candidate)

    def resolve(self, kind, value, source_group=""):
        key = (kind, fold_name(value))
        result = {"input": value, "entity": None, "canonical": None, "status": "unknown"}
        # A canonical name cannot be hijacked by another entity's alias.
        candidates = self.canonical.get(key, set())
        if not candidates:
            scoped = self.scopes.get((*key, source_group), set()) if source_group else set()
            if not scoped:
                scoped = self.scopes.get((*key, "*"), set())
            if scoped:
                candidates = set()
                for target in scoped:
                    hits = self.targets.get((kind, target), set())
                    if not hits:
                        return dict(result, status="invalid_target")
                    candidates.update(hits)
            else:
                candidates = self.aliases.get(key, set())
        if len(candidates) == 1:
            entity, canonical = next(iter(candidates))
            return dict(result, entity=entity, canonical=canonical, status="resolved")
        if candidates:
            return dict(result, status="ambiguous", candidates=[
                {"entity": entity, "canonical": canonical} for entity, canonical in sorted(candidates)])
        return result

    def group_index(self):
        """Frontend consumes the server's decisions, not a second alias policy."""
        out = {"pol": {}, "pod": {}}
        keys = set(self.canonical) | set(self.aliases)
        keys.update((side, alias) for side, alias, group in self.scopes if group == "*")
        for side, alias in keys:
            if side not in out:
                continue
            result = self.resolve(side, alias)
            if result["entity"] == "port_group":
                out[side][alias] = result["canonical"]
        return out


def validate_group_names(proposed, groups, ports):
    """Reject group collisions per direction; inactive rows remain historical."""
    if not proposed.get("active", 1):
        return
    kind = proposed.get("kind") or "pod"
    if kind not in ("pol", "pod", "both"):
        raise ValueError("port group kind must be pol, pod or both")
    def directions(value):
        return {"pol", "pod"} if value == "both" else {value or "pod"}
    def names(row):
        label = str(row.get("label") or "")
        return {fold_name(value) for value in [row["name"], label, re.split(r"[（(]",label)[0]]
                + alias_list(row.get("aliases")) if fold_name(value)}
    mine = names(proposed)
    for row in groups:
        if row["name"] == proposed["name"] or not row.get("active", 1):
            continue
        if directions(kind) & directions(row.get("kind")) and mine & names(row):
            raise ValueError("港群识别名与在用港群冲突：" + row["name"])
    for port in ports:
        if port.get("active", 1) and fold_name(port["name"]) in mine:
            raise ValueError("港群识别名与港口规范名冲突：" + port["name"])


def validate_request(body):
    if not isinstance(body, dict):
        raise ValueError("request must be an object")
    group, items = body.get("source_group", ""), body.get("items")
    if not isinstance(group, str) or len(group) > MASTER_NAME_MAX_LENGTH:
        raise ValueError("invalid source_group")
    if not isinstance(items, list) or not items or len(items) > MASTER_RESOLVE_LIMIT:
        raise ValueError(f"items must contain 1..{MASTER_RESOLVE_LIMIT} names")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in MASTER_RESOLVE_TYPES:
            raise ValueError("name type must be pol, pod, carrier or route")
        value = item.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > MASTER_NAME_MAX_LENGTH:
            raise ValueError("invalid name value")
    return group.strip(), items
