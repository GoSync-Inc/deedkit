"""Bounded, request-scoped tool discovery from the effective catalog."""

from .catalog import CATALOG, public


def search_tools(visible, details, args):
    names = sorted(name for name in visible if CATALOG[name]["module"] != "gateway")
    modules = {}
    for name in names:
        module = CATALOG[name]["module"]
        modules[module] = modules.get(module, 0) + 1
    query = args["query"].strip().casefold()
    if query in CATALOG:
        matches = [name for name in names if name == query]
    else:
        words = query.split()
        matches = [name for name in names if all(word in (name + " " + CATALOG[name]["description"] + " "
                                                        + CATALOG[name].get("guidance", "")).casefold()
                                                for word in words)]
    start, end = args["offset"], args["offset"] + args["limit"]
    rows = []
    for name in matches[start:end]:
        tool = CATALOG[name]
        published = public(tool, details["actions"])
        row = {"name": name, "module": tool["module"], "description": published["description"],
               "read_only": tool.get("annotations", {}).get("readOnlyHint", False)}
        if args["detail"] == "full":
            row.update(published)
        rows.append(row)
    return {"modules": [{"id": module, "tool_count": count} for module, count in sorted(modules.items())],
            "tools": rows, "total": len(matches), "next_offset": end if end < len(matches) else None,
            "unavailable": details["unavailable_modules"], "selection_scope": "request"}
