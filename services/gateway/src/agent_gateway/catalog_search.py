"""Bounded, request-scoped tool discovery from the effective catalog."""

from .catalog import CATALOG, public, routed

# Words an agent uses for operations whose names do not say it (ANYM-982): the buttons are a bot's inline keyboard.
OPERATION_TERMS = {
    "inspect_message_buttons": "inline keyboard bot keyboard reply_markup buttons",
    "press_button": "inline keyboard bot keyboard reply_markup callback button",
}


def operation_texts(tool, actions):
    """`{operation: searchable text}` for the operations of a routed tool that this key may use, else `{}`."""
    if not tool.get("action_group"):
        return {}
    return {op: f"{op} {op.replace('_', ' ')} {OPERATION_TERMS.get(op, '')}".casefold() for op in routed(tool, actions)}


def search_tools(visible, details, args):
    names = sorted(name for name in visible if CATALOG[name]["module"] != "gateway")
    modules = {}
    for name in names:
        module = CATALOG[name]["module"]
        modules[module] = modules.get(module, 0) + 1
    query = args["query"].strip().casefold()
    operations = {}
    if query in CATALOG:
        matches = [name for name in names if name == query]
    else:
        words = query.split()
        matches = []
        for name in names:
            tool = CATALOG[name]
            text = (name + " " + tool["description"] + " " + tool.get("guidance", "")).casefold()
            # The words must all sit in one operation's text: words of two operations never add up to a match.
            found = [op for op, op_text in operation_texts(tool, details["actions"]).items()
                     if all(word in op_text for word in words)]
            if found:
                operations[name] = found
            if found or all(word in text for word in words):
                matches.append(name)
    start, end = args["offset"], args["offset"] + args["limit"]
    rows = []
    for name in matches[start:end]:
        tool = CATALOG[name]
        published = public(tool, details["actions"])
        row = {"name": name, "module": tool["module"], "description": published["description"],
               "read_only": tool.get("annotations", {}).get("readOnlyHint", False)}
        if name in operations:
            row["operations"] = operations[name]
        if args["detail"] == "full":
            row.update(published)
        rows.append(row)
    return {"modules": [{"id": module, "tool_count": count} for module, count in sorted(modules.items())],
            "tools": rows, "total": len(matches), "next_offset": end if end < len(matches) else None,
            "unavailable": details["unavailable_modules"], "selection_scope": "request"}
