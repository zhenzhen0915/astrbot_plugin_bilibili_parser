"""QQ group allow/deny policy, shared by text and share-card parsing."""

from __future__ import annotations

import re
from collections.abc import Mapping


def group_allowed(group_id: str, config: Mapping) -> bool:
    """Apply the selected mode; an empty whitelist enables no groups."""
    if not group_id.isdecimal() or int(group_id) <= 0:
        return False
    mode = config.get("group_filter_mode", "whitelist")
    if mode == "all":
        return True
    if mode not in {"whitelist", "blacklist"}:
        return False
    field = "allowed_group_ids" if mode == "whitelist" else "blocked_group_ids"
    raw = config.get(field, "")
    raw = " ".join(map(str, raw)) if isinstance(raw, list) else str(raw)
    ids = {str(int(item)) for item in re.split(r"[,，;；\s]+", raw) if item.isdecimal()}
    present = str(int(group_id)) in ids
    return present if mode == "whitelist" else not present
