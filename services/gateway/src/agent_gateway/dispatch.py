from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import asyncio
import logging
from platform_contracts import Capability, safe_error
from platform_contracts.agent import PLACEHOLDER_REQUEST_ID

from .catalog import arguments, CATALOG, InvalidArguments, routed
from .diagnostics import FAILURE_FIELDS, discovery, manifest_summary, stale_arguments_hint, unavailable, unknown_tool_denial
from .http_clients import WAIT_POOL, WAIT_RETRY_AFTER_S
from .policy import Denied, key_from_file
from .tiers import ADVANCED, default_list, REPLACED_BY
from .dispatch_shaping import (  # noqa: F401  (the moved names stay importable from here)
    CALENDAR_CONSTRAINTS, EXACT_TO, FILE_NAME, INLINE_UPLOAD_FIELDS, MIN_PAYLOAD, MIN_TOKEN, NAMED_WINDOWS, PHONE,
    REFERENCE_KEY, TIME_KEY, TIMESTAMP, WRITE_ONLY_ARGUMENTS, branches, caller_payload, candidate_query, carries,
    exposed, fixed, found, inline_upload_next, payload_strings, relayed_error, resolver_query, sensitive,
    shape_result)

PLAIN_PATH = "/mcp"
MODULE_PATH = "/mcp/modules/"
LIVE_SEARCH = "search_messages_live"
OPEN_CHAT_BY_PHONE = "open_chat_by_phone"
OPEN_CHAT_BY_USERNAME = "open_chat_by_username"
SEND_FILE = "send_file"
# One line each for an agent choosing what to load (ANYM-271); the ids are the deployable modules.
# No ", " or ": ": the MCP text block is compact JSON (ANYM-408).
MODULE_PURPOSES = {
    "recordings": "Wearable recording metadata plus transcripts and summaries from permitted accounts",
    "calendar": "Family calendar agenda and free slots with planned event changes",
    "calls": "Voice calls with briefs and preflight plus placed calls and their records",
    "messaging": "Messenger chats with search and the reply queue plus files and sending",
}


class Route:
    """An MCP connection path: plain `/mcp` (all modules) or `/mcp/modules/<m>[,<m>...]` (only those), where the
    `advanced` name lists the low-level tools as well (ANYM-550); `/mcp/modules/advanced` keeps every module."""

    def __init__(self, path, modules=None, advanced=False):
        self.path, self.modules, self.advanced = path, modules, advanced

    @classmethod
    def parse(cls, path):
        """The route of an MCP path, None for any other path; a malformed module path is `unknown_module`."""
        if path.rstrip("/") == PLAIN_PATH:
            return cls(PLAIN_PATH)
        if path.rstrip("/") != PLAIN_PATH + "/modules" and not path.startswith(MODULE_PATH):
            return None
        names = path[len(MODULE_PATH):]
        names = (names[:-1] if names.endswith("/") else names).split(",")
        if not names or len(set(names)) != len(names) or not set(names) <= MODULE_PURPOSES.keys() | {ADVANCED}:
            raise Denied("unknown_module", 404)
        modules = frozenset(names) - {ADVANCED}
        return cls(MODULE_PATH + ",".join(sorted(names)), modules or None, ADVANCED in names)

    def listed(self, names):
        """This route's tools/list out of the caller's effective tools: without `advanced`, the default list."""
        return names if self.advanced else default_list(names)


def tool_paths(usable, selections=None):
    """Every tool the key may use, the advanced ones included, with the connection path that lists it (ANYM-550).

    `selections` are the principal's in-session selections by path; a row whose path a selection narrows past the
    tool's module carries the `next` selection that lists it there again."""
    everyday = set(default_list(usable))
    rows = []
    for name in usable:
        module = CATALOG[name]["module"]
        names = ([] if module == "gateway" else [module]) + ([] if name in everyday else [ADVANCED])
        row = {"tool": name, "tier": "everyday" if name in everyday else ADVANCED,
               "path": MODULE_PATH + ",".join(sorted(names)) if names else PLAIN_PATH}
        if name not in everyday:
            row["replaced_by"] = REPLACED_BY[name]
        chosen = (selections or {}).get(row["path"])
        if chosen and module != "gateway" and module not in chosen:
            row["next"] = {"tool": "platform_select_modules", "arguments": {"modules": sorted(chosen | {module})}}
        rows.append(row)
    return rows


WARNING_WINDOW_DAYS = 7
# The holder's expiry from this request's latest authentication; transports read it after `call`.
admitted = ContextVar("deedkit_admitted_credential", default=None)


def utcnow():
    return datetime.now(timezone.utc)


def expiry_of(agent):
    return datetime.fromisoformat(agent["expires_at"].replace("Z", "+00:00")).astimezone(timezone.utc)


def credential_expiry(agent, now=None):
    """The holder's own expiry; never another principal's or any key material."""
    expiry = expiry_of(agent)
    remaining = expiry - (now or utcnow())
    warning = remaining < timedelta(days=WARNING_WINDOW_DAYS)
    return {
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "expires_in_days": max(remaining.days, 0),
        "expiry_warning": warning,
        "warning_window_days": WARNING_WINDOW_DAYS,
        "next_action": "ask_owner_to_extend_gateway_key" if warning else "none",
    }


def expiring(snapshot, now=None):
    """Operator view: principals in the warning window or expired, by id and date only."""
    now = now or utcnow()
    return [
        {"id": agent["id"], "expires_at": info["expires_at"], "expired": expiry_of(agent) <= now}
        for agent in snapshot["principals"]
        if (info := credential_expiry(agent, now))["expiry_warning"]
    ]


class Dispatch:
    def __init__(self, policy, clients, tickets=None):
        self.policy, self.clients, self.tickets = policy, clients, tickets
        self.warned = {}
        # In-session module selections of one policy revision: (principal id, route path) -> modules. Memory only.
        self.selections, self.selection_revision = {}, None

    def credential(self, agent):
        """Expiry for the authenticated holder; logs one warning per principal per UTC day."""
        now = utcnow()
        info = credential_expiry(agent, now)
        if info["expiry_warning"] and self.warned.get(agent["id"]) != now.date():
            self.warned[agent["id"]] = now.date()
            logging.getLogger("agent_gateway").warning(
                "gateway key for principal %s expires at %s", agent["id"], info["expires_at"]
            )
        return info

    def actions(self, snapshot, agent, caps, account=None):
        """Intersect reviewed grants with explicit current account implementation support.

        Counts/provider names are not evidence of individual implemented actions.
        Missing or malformed account contracts expose no actions for that account.
        """
        allowed = set(self.policy.actions(snapshot, agent, caps.get("permissions", [])))
        operations = caps.get("messenger_operations")
        accounts = operations.get("supported_by_account") if isinstance(operations, dict) else None
        if not isinstance(accounts, dict):
            return []
        rows = [accounts.get(account)] if account is not None else accounts.values()
        supported = set()
        for row in rows:
            values = row.get("operations") if isinstance(row, dict) else None
            if isinstance(values, list) and all(isinstance(value, str) for value in values):
                supported.update(values)
        return sorted(allowed & supported)

    def usable_actions(self, snapshot, agent, caps):
        """Advertise only actions usable now; the call ceiling stays `actions`.

        An action is listed for an account when it is granted, supported and
        reported by the backend as available (key permitted and account opt-in).
        Excluded ceiling actions are counted per reason, never listed. A backend
        without an availability report advertises none rather than guessing.
        """
        operations = caps.get("messenger_operations")
        accounts = operations.get("supported_by_account") if isinstance(operations, dict) else None
        usable, excluded = {}, {}
        for account, row in (accounts.items() if isinstance(accounts, dict) else ()):
            ceiling = self.actions(snapshot, agent, caps, account)
            available = row.get("available_operations") if isinstance(row, dict) else None
            reported = isinstance(available, list) and all(isinstance(value, str) for value in available)
            usable[account] = sorted(set(ceiling) & set(available)) if reported else []
            if len(ceiling) > len(usable[account]):
                reason = "not_available_for_account" if reported else "availability_not_reported"
                excluded[account] = {reason: len(ceiling) - len(usable[account])}
        return {
            "gateway_actions": sorted(set().union(*usable.values())),
            "gateway_actions_by_account": usable,
            "gateway_actions_excluded_by_account": excluded,
            "gateway_actions_basis": "granted_supported_and_available_for_account",
        }

    def next_call(self, value, snapshot, agent, caps):
        """ANYM-531: name the backend's suggested call by this gateway's own tool, or drop it where this
        gateway would refuse it (hidden tool, missing scope or action grant for the account)."""
        step = value.get("next_call") if isinstance(value, dict) else None
        if not isinstance(step, dict):
            return value
        value = {k: v for k, v in value.items() if k != "next_call"}
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        name = next((n for n in self.policy.visible(snapshot, agent)
                     if CATALOG[n]["module"] == "messaging" and CATALOG[n]["operation"] == step.get("tool")), None)
        usable = (name is not None and set(CATALOG[name]["scopes"]) <= set(caps.get("permissions", []))
                  and self.implemented(CATALOG[name], snapshot, agent, caps, arguments.get("account")))
        if usable and CATALOG[name].get("action_group"):
            allowed = (self.actions(snapshot, agent, caps, arguments["account"]) if "account" in arguments
                       else self.policy.actions(snapshot, agent, caps.get("permissions", [])))
            usable = bool(allowed) and ("operation" not in arguments or arguments["operation"] in allowed)
        if usable and arguments.get("phone"):
            # ANYM-532: a phone recipient is the open_chat_by_phone lookup, under its action grant.
            usable = ("send" in caps.get("permissions", [])
                      and OPEN_CHAT_BY_PHONE in self.actions(snapshot, agent, caps, arguments.get("account")))
        if usable:
            value["next_call"] = {**step, "tool": name}
        elif value.get("error") == "not_found" or arguments.get("operation") == "open_chat_by_phone":
            value["next_action"] = "key_cannot_open_new_chats"
        return value

    def implemented(self, tool, snapshot, agent, caps, account=None):
        if tool.get("required_manifest"):
            manifest = caps.get("tool_manifest")
            names = manifest.get("names") if isinstance(manifest, dict) else None
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                return False
            if tool["operation"] not in names:
                return False
        action = tool.get("required_action")
        return not action or action in self.actions(snapshot, agent, caps, account)

    async def backend_capabilities(self, snapshot, agent, module, timeout=2):
        target = snapshot["deployment"]["backends"].get(module)
        if target != self.clients.deployment["backends"].get(module):
            raise Denied("backend_configuration_requires_restart", 503)
        binding = agent["credentials"][module]
        credential = key_from_file(binding["key_file"], binding["key_sha256"])
        try:
            async with asyncio.timeout(timeout):
                result = await self.full_capabilities(module, credential)
        except TimeoutError:
            error = Denied("backend_unavailable", 503)
            error.detail = {"error": "backend_unavailable", "reason": "capabilities_timeout"}
            raise error from None
        permissions = result.get("permissions")
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            raise Denied("backend_capability_contract_unavailable", 503)
        return result

    async def full_capabilities(self, module, credential):
        """Messaging is asked for its full payload by name (ANYM-270); a backend from before
        `detail` rejects the argument, so it is asked once more with none, as before."""
        if module != "messaging":
            return await self.clients.call(module, "capabilities", credential, {})
        try:
            return await self.clients.call(module, "capabilities", credential, {"detail": "full"})
        except Denied as error:
            if error.code != "backend_denied" or (getattr(error, "detail", None) or {}).get("error") != "invalid_parameters":
                raise
        return await self.clients.call(module, "capabilities", credential, {})

    def selected(self, agent, revision, route):
        """The modules an MCP route may use: its path's modules narrowed by the principal's selection; None = all."""
        if revision != self.selection_revision:
            # Any request that observes a new policy revision clears every principal's selection.
            self.selections, self.selection_revision = {}, revision
        if route is None:
            return None
        modules = route.modules
        entry = self.selections.get((agent["id"], route.path))
        if entry:
            modules = entry if modules is None else modules & entry
        return modules

    async def discover(self, key, route=None, narrow=True):
        """Effective tools; on an MCP route with `narrow`, only the selected modules' (their backends alone are asked)."""
        snapshot, agent = self.authenticate(key)
        visible = self.policy.visible(snapshot, agent)
        selected = self.selected(agent, snapshot["revision"], route)
        if narrow and selected is not None:
            visible = [n for n in visible if CATALOG[n]["module"] in selected | {"gateway"}]
        modules = sorted({CATALOG[name]["module"] for name in visible} - {"gateway"})

        async def one(module):
            try:
                caps = await self.backend_capabilities(snapshot, agent, module)
                return module, caps, "ready"
            except Denied:
                return module, {"permissions": []}, "unavailable"

        results = await asyncio.gather(*(one(module) for module in modules))
        capabilities = {module: caps for module, caps, _ in results}
        scopes = {module: set(caps["permissions"]) for module, caps, _ in results}
        states = {module: state for module, _, state in results}
        # Recheck current local policy after discovery; a slow backend does not
        # let the old policy snapshot survive a revocation.
        current, principal = self.authenticate(key)
        if current["revision"] != snapshot["revision"]:
            raise Denied("policy_changed", 409)
        visible = [n for n in visible if n in self.policy.visible(current, principal)]
        actions = self.actions(current, principal, capabilities.get("messaging", {}))
        visible = [
            n
            for n in visible
            if CATALOG[n]["module"] == "gateway"
            or (
                states.get(CATALOG[n]["module"]) == "ready"
                and set(CATALOG[n]["scopes"]) <= scopes[CATALOG[n]["module"]]
                and self.implemented(CATALOG[n], current, principal, capabilities[CATALOG[n]["module"]])
                and (
                    not CATALOG[n].get("action_group")
                    or bool(routed(CATALOG[n], actions))
                    or (
                        CATALOG[n]["operation"] == "get_messenger_operation"
                        and bool(self.policy.actions(current, principal, scopes.get("messaging", [])))
                    )
                )
            )
        ]
        if narrow and route is not None:
            visible = route.listed(visible)
        manifests = {module: summary for module, caps, _ in results
                     if (summary := manifest_summary(caps)) is not None}
        return current, principal, visible, {
            "states": states, "scopes": scopes, "actions": actions,
            "discovery": discovery(visible, actions, current["revision"]),
            "backend_tool_manifests": manifests, "selected": selected,
        }

    async def select_modules(self, key, route, modules):
        """Record a narrowing selection for this principal and route; report the list it yields."""
        route = route or Route(PLAIN_PATH)
        snapshot, agent = self.authenticate(key)
        self.selected(agent, snapshot["revision"], None)
        self.selections[(agent["id"], route.path)] = frozenset(modules)
        _, _, visible, details = await self.discover(key, route)
        return {
            "selection_path": route.path,
            "effective_modules": sorted({CATALOG[n]["module"] for n in visible} - {"gateway"}),
            "tools_sha256": details["discovery"]["tools_sha256"],
            "takes_effect": "next_tools_list",
            "next_action": details["discovery"]["next_action"],
        }

    async def platform(self, key, route):
        """`platform_capabilities`: the route's effective list plus every module the key can use (REST: all tools)."""
        snapshot, agent, usable, states = await self.discover(key, route or Route(PLAIN_PATH), narrow=False)
        selected = states["selected"]
        visible = usable
        if route is not None:
            if selected is not None:
                visible = [n for n in usable if CATALOG[n]["module"] in selected | {"gateway"}]
            visible = route.listed(visible)
            states["discovery"] = discovery(visible, states["actions"], snapshot["revision"])
        counts = {}
        for name in usable:
            if CATALOG[name]["module"] != "gateway":
                counts[CATALOG[name]["module"]] = counts.get(CATALOG[name]["module"], 0) + 1
        chosen = sorted(m for m in counts if selected is None or m in selected)
        return snapshot, agent, visible, states, {
            "selection_path": (route or Route(PLAIN_PATH)).path,
            "selected_modules": chosen,
            "modules_available": [
                {"id": m, "purpose": MODULE_PURPOSES[m], "tool_count": counts[m], "selected": m in chosen,
                 "path": MODULE_PATH + m}
                for m in sorted(counts)
            ],
            "tool_paths": tool_paths(usable, {path: modules for (principal, path), modules in self.selections.items()
                                              if principal == agent["id"]}),
        }

    def authenticate(self, key):
        """`Policy.authenticate`, recording the holder's expiry as the request's latest."""
        snapshot, agent = self.policy.authenticate(key)
        admitted.set(self.credential(agent))
        return snapshot, agent

    def visible_to(self, key):
        """The caller's own tool names, or none when the key does not authenticate."""
        try:
            return self.policy.visible(*self.policy.authenticate(key))
        except Denied:
            return []

    def runnable(self, value, visible, module, snapshot, agent, caps):
        """A `next` this caller can run as given on this route: a tool of its effective list with arguments its
        schema accepts. Only the failing tool's module and the gateway's own tools can be checked without asking
        another backend, so a `next` into any other module is not runnable here. It passes the same argument checks
        as `call` (the placeholder request_id refusal and `argument_admission`)."""
        try:
            if not isinstance(value, dict) or set(value) != {"tool", "arguments"} or value["tool"] not in visible:
                return False
            target = CATALOG[value["tool"]]
            checked = arguments(target, value["arguments"])
            if checked.get("request_id") == PLACEHOLDER_REQUEST_ID:
                return False
            if target["module"] == "gateway":
                return True
            if target["module"] != module:
                return False
            self.argument_admission(target, checked, snapshot, agent, caps)
            return True
        except (Denied, TypeError, KeyError, ValueError):
            return False

    def argument_admission(self, tool, args, snapshot, agent, caps):
        """The admission of one module call that depends on its backend's capabilities and on the arguments: scopes,
        cursor-write for get_updates(start_from=now), the backend implementation, the calendar rules ceiling and the
        action group. Returns the gateway actions the call may use; raises Denied otherwise."""
        module = tool["module"]
        if not set(tool["scopes"]) <= set(caps["permissions"]):
            raise Denied()
        if (module == "messaging" and tool["operation"] == "get_updates"
                and args.get("start_from") == "now"
                and "cursor-write" not in (set(agent["grants"].get(module, [])) & set(caps["permissions"]))):
            raise Denied()
        if (module == "messaging" and tool["operation"] in ("create_subscription", "configure_subscription_delivery")
                and (args.get("delivery") is not None or args.get("endpoint_id") is not None)
                and "delivery-write" not in (set(agent["grants"].get(module, [])) & set(caps["permissions"]))):
            # ANYM-519: binding a self-service endpoint needs the local delivery-write grant too.
            raise Denied()
        if not self.implemented(tool, snapshot, agent, caps, args.get("account")):
            raise Denied()
        rules_ceiling = "assignment:read" in set(agent["grants"].get(module, [])) & set(caps["permissions"])
        if (module == "calendar" and tool["operation"] == "plan"
                and any(isinstance(a, dict) and (a.get("action") in ("project", "unproject")
                                                 or (a.get("action") == "create" and "activity" in a))
                        for a in args.get("actions", []))
                and not rules_ceiling):
            # A projection and its undo are planned from guardian rules: the assignment ceiling of rules in members.
            raise Denied()
        allowed_actions = self.actions(snapshot, agent, caps) if module == "messaging" else []
        if tool["operation"] == "get_messenger_operation":
            # Retained receipts are independent of current provider support.
            allowed_actions = self.policy.actions(snapshot, agent, caps["permissions"])
        if tool.get("action_group"):
            # ANYM-549: a risk-class tool carries only the granted actions it routes.
            allowed_actions = routed(tool, allowed_actions)
            if not allowed_actions or ("operation" in args and args["operation"] not in allowed_actions):
                raise Denied()
            if "account" in args:
                allowed_actions = routed(tool, self.actions(snapshot, agent, caps, args["account"]))
                if "operation" in args and args["operation"] not in allowed_actions:
                    raise Denied()
        if (module == "messaging" and tool["operation"] in ("preview_message", "send_message") and args.get("phone")
                and ("send" not in caps["permissions"]
                     or OPEN_CHAT_BY_PHONE not in self.actions(snapshot, agent, caps, args.get("account")))):
            # ANYM-532: a phone recipient needs the same action grant as the separate operation.
            raise Denied()
        if (module == "messaging" and tool["operation"] in ("preview_message", "send_message") and args.get("username")
                and ("send" not in caps["permissions"]
                     or OPEN_CHAT_BY_USERNAME not in self.actions(snapshot, agent, caps, args.get("account")))):
            # ANYM-643: likewise a Telegram username recipient, under the open_chat_by_username grant.
            raise Denied()
        if (module == "messaging" and tool["operation"] == "messages_send" and args.get("files")
                and SEND_FILE not in self.actions(snapshot, agent, caps, args.get("account") or None)):
            # ANYM-546: files go as send_file operations, under the same action grant.
            raise Denied()
        return allowed_actions

    @staticmethod
    def backend_call(tool, args, caps):
        """The backend operation and arguments of one admitted call. A risk-class tool (ANYM-549) is a preview on
        dry_run and an execute otherwise. Preview's request_id is accepted and never forwarded, and wait_s=0 is not
        sent, so a backend from before ANYM-549 keeps taking these calls; a real wait needs the backend's bound."""
        operation = tool["operation"]
        if tool.get("routes_actions"):
            operation = "preview_messenger_operation" if args.get("dry_run") else "execute_messenger_operation"
            args = {k: v for k, v in args.items() if k != "dry_run"}
        if operation == "preview_messenger_operation" and tool["module"] == "messaging":
            args = {k: v for k, v in args.items() if k not in ("request_id", "preview_digest", "wait_s")}
        if operation == "execute_messenger_operation" and tool["module"] == "messaging":
            if not args.get("wait_s"):
                args = {k: v for k, v in args.items() if k != "wait_s"}
            else:
                block = caps.get("messenger_operations")
                bound = block.get("execute_wait_s_max") if isinstance(block, dict) else None
                if type(bound) is not int or bound < args["wait_s"]:
                    denied = Denied("unsupported_by_backend", 409)
                    denied.detail = {"error": "unsupported_by_backend", "field": "wait_s",
                                     "next_action": "repeat_without_wait_s_then_poll_get_messenger_operation"}
                    raise denied
        return operation, args

    def admit(self, key, name):
        return self.policy.authorize_agent(*self.authenticate(key), name)

    def admit_call(self, key, name, values, route):
        try:
            snapshot, agent, tool = self.admit(key, name)
        except Denied as error:
            # One body for a nonexistent name, a tool outside the profile and one of an undeployed module.
            if error.code == "not_found_or_forbidden" and error.status == 403:
                error.detail = unknown_tool_denial("platform_capabilities" in self.visible_to(key))
            raise
        selected = self.selected(agent, snapshot["revision"], route)
        if selected is not None and tool["module"] not in selected | {"gateway"}:
            denied = Denied("module_not_selected", 403)
            denied.detail = {"error": "module_not_selected", "module": tool["module"],
                             "next_action": "select_module_or_use_its_connection_path"}
            # Only the in-session selection can widen again; a module path excludes the module for good.
            if ((route.modules is None or tool["module"] in route.modules)
                    and "platform_select_modules" in self.policy.visible(snapshot, agent)):
                chosen = self.selections.get((agent["id"], route.path)) or frozenset()
                denied.detail["next"] = {"tool": "platform_select_modules",
                                         "arguments": {"modules": sorted(chosen | {tool["module"]})}}
            raise denied
        try:
            args = arguments(tool, values)
            if args.get("request_id") == PLACEHOLDER_REQUEST_ID:
                # The placeholder of a `next` (ANYM-544) is never a key: refuse it before any backend sees it.
                raise InvalidArguments([{"field": "request_id", "reason": "format"}])
        except InvalidArguments as error:
            denied = Denied("invalid_arguments", 422)
            if error.field_errors:
                denied.detail = stale_arguments_hint(tool, {
                    "error": "invalid_arguments", "field_errors": error.field_errors,
                    "next_action": "check_parameters"})
                # ANYM-519: a signing secret or header values are never echoed back, even to their sender.
                if (error.field_errors == [{"field": "request_id", "reason": "required"}]
                        and "request_id" in tool["inputSchema"].get("properties", {})
                        and not WRITE_ONLY_ARGUMENTS.intersection(values)):
                    denied.detail["next"] = {"tool": name, "arguments": {**values, "request_id": PLACEHOLDER_REQUEST_ID}}
            raise denied from None
        except ValueError:
            raise Denied("invalid_arguments", 422) from None
        return snapshot, agent, tool, selected, args

    async def call(self, key, name, values, route=None):
        """One admitted tool call; `route` is the MCP connection path (REST passes None and ignores selection)."""
        snapshot, agent, tool, selected, args = self.admit_call(key, name, values, route)
        if tool["operation"] == "select_modules":
            return await self.select_modules(key, route, args["modules"])
        if tool["module"] == "gateway":
            snapshot, agent, visible, states, selection = await self.platform(key, route)
            credential = admitted.get()
            return {
                "service": "deedkit",
                "version": "0.1.0",
                "principal": agent["id"],
                "gateway_principal": agent["id"],
                "discovery": states["discovery"],
                "backend_tool_manifests": states["backend_tool_manifests"],
                "policy_revision": snapshot["revision"],
                "tools": visible,
                "modules": sorted({CATALOG[n]["module"] for n in visible} - {"gateway"}),
                "backend_states": states["states"],
                "coverage": "query selected module capabilities; unavailable/stale sources remain unknown",
                "subscriptions": "existing backend identity; passive events do not ACK",
                # The gateway's own local write (platform_select_modules) is not a provider mutation.
                "provider_mutations": any(
                    CATALOG[n]["module"] != "gateway"
                    and not CATALOG[n].get("annotations", {}).get("readOnlyHint", False) for n in visible
                ),
                "owner_authorization_verified": False,
                "credential": credential,
                "capability": Capability("gateway", "capabilities", snapshot["revision"]).to_dict(),
                **selection,
            }
        module = tool["module"]
        target = snapshot["deployment"]["backends"].get(module)
        if target != self.clients.deployment["backends"].get(module):
            raise Denied("backend_configuration_requires_restart", 503)
        binding = agent["credentials"][module]
        credential = key_from_file(binding["key_file"], binding["key_sha256"])
        read_only = tool.get("annotations", {}).get("readOnlyHint", False)
        try:
            # Discovery keeps a short bound; one call may wait for a busy pool.
            caps = await self.backend_capabilities(snapshot, agent, module, timeout=6)
        except Denied as error:
            if error.code == "backend_unavailable":
                error.detail = unavailable(tool["operation"], getattr(error, "detail", None),
                                           phase="capabilities", read_only=read_only)
            raise
        allowed_actions = self.argument_admission(tool, args, snapshot, agent, caps)
        operation, args = self.backend_call(tool, args, caps)
        # A preview has no effect, also when a risk-class tool asks for it.
        read_only = read_only or operation == "preview_messenger_operation"
        rules_ceiling = "assignment:read" in set(agent["grants"].get(module, [])) & set(caps["permissions"])
        if module == "messaging" and tool["operation"] in ("preview_message", "send_message"):
            # An empty recipient field is not sent, so a backend from before ANYM-532 keeps taking chat_id calls.
            args = {k: v for k, v in args.items() if k not in ("chat_id", "phone", "username") or v != ""}
        if module == "messaging" and tool["operation"] == "messages_send":
            # ANYM-546: whether a phone with no chat may be opened, and whether files may go, is this gateway's
            # grant on every call (a token-only or resume call carries no files), never the agent's word.
            actions = self.actions(snapshot, agent, caps, args.get("account") or None)
            args = {**args, "allow_open_chat": "send" in caps["permissions"] and OPEN_CHAT_BY_PHONE in actions,
                    "allow_files": "send" in caps["permissions"] and SEND_FILE in actions}
            # ANYM-643: likewise a Telegram username with no chat, only to a backend that lists the recipient (an
            # older one rejects the argument, and refuses such a username anyway); never the agent's own value.
            args.pop("allow_open_username", None)
            if "username" in (((caps.get("sending") or {}).get("recipients")) or {}):
                args["allow_open_username"] = "send" in caps["permissions"] and OPEN_CHAT_BY_USERNAME in actions
        if module == "calendar" and tool["operation"] == "plan":
            # Carried drives (and create with activity) are asked for only under this gateway's own rule-read
            # ceiling, and only from a backend that declares the field; an agent never sets it itself.
            args = {k: v for k, v in args.items() if k != "carry_projections"}
            if rules_ceiling and (caps.get("features") or {}).get("carry_projections") is True:
                args["carry_projections"] = True
        if module == "calendar" and tool["operation"] == "agenda":
            # Drive links in agenda are rule data: asked for only under the same ceiling (ANYM-436).
            args = {k: v for k, v in args.items() if k != "projection_status"}
            if rules_ceiling and (caps.get("features") or {}).get("projection_status") is True:
                args["projection_status"] = True
        # ANYM-478: search's live part is the search_messages_live action, so it needs that grant too;
        # without it the backend answers from the archive only and live says not_permitted.
        live_denied = (module == "messaging" and tool["operation"] == "search"
                       and LIVE_SEARCH not in self.policy.actions(snapshot, agent, caps["permissions"]))
        live_requested = live_denied and args.get("live", "off") != "off"
        if live_requested:
            args = {**args, "live": "off"}
        started = datetime.now(timezone.utc).timestamp()
        # Admission is rechecked after all local work; a cached MCP session is
        # never authority. ACK is not retried if its network outcome is unknown.
        current, principal, _ = self.admit(key, name)
        if current["revision"] != snapshot["revision"] or principal["id"] != agent["id"]:
            raise Denied("policy_changed", 409)
        try:
            result = (
                caps
                if tool["operation"] == "capabilities"
                else await self.clients.call(module, operation, credential, args)
            )
        except Denied as error:
            if error.code == "backend_busy" and module == "messaging" and tool["operation"] == "wait_for_subscription_events":
                # ANYM-518: a full wait pool is a wait outcome, not a bare 503.
                return {"wait_status": "busy", "retry_after_s": WAIT_RETRY_AFTER_S, "wait_pool": WAIT_POOL}
            if error.code in {"backend_unavailable", "backend_response_too_large"} and not read_only:
                diagnostics = getattr(error, "detail", {})
                error.detail = {
                    **safe_error("mutation_outcome_unknown", unknown_outcome=True),
                    **{key: diagnostics[key] for key in FAILURE_FIELDS if key in diagnostics},
                }
            elif error.code == "backend_unavailable":
                error.detail = unavailable(tool["operation"], getattr(error, "detail", None),
                                           phase="operation", read_only=True)
            elif error.code == "backend_denied" and isinstance(getattr(error, "detail", None), dict):
                if module == "messaging":
                    error.detail = self.next_call(error.detail, snapshot, agent, caps)
                visible = [n for n in self.policy.visible(current, principal)
                           if selected is None or CATALOG[n]["module"] in selected | {"gateway"}]
                upload = inline_upload_next(name, values, error.detail)
                error.detail = relayed_error(
                    error.detail, lambda value: self.runnable(value, visible, module, current, principal, caps),
                    {n for n in visible if CATALOG[n].get("annotations", {}).get("readOnlyHint", False)},
                    caller_payload(resolver_query(name, values), tool.get("inputSchema")),
                    lambda candidate: caller_payload(candidate_query(name, resolver_query(name, values), candidate),
                                                     tool.get("inputSchema")))
                # Runnable as soon as the caller picks its own request_id in place of the placeholder.
                if upload is not None and self.runnable(
                        {**upload, "arguments": {**upload["arguments"], "request_id": "inline-upload-check"}},
                        visible, module, current, principal, caps):
                    error.detail["next"] = upload
            raise
        return shape_result(
            self, result, tool=tool, module=module, snapshot=snapshot, agent=agent, caps=caps, current=current,
            principal=principal, allowed_actions=allowed_actions, rules_ceiling=rules_ceiling,
            live_denied=live_denied, live_requested=live_requested, key=key, credential=credential, args=args,
            started=started)
