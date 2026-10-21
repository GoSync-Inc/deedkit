from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import asyncio
import logging
from platform_contracts import Capability, safe_error
from .adapters.calls import receipt_reference

from .catalog import arguments, CATALOG, InvalidArguments
from .diagnostics import FAILURE_FIELDS, discovery, manifest_summary, stale_arguments_hint, unavailable, unknown_tool_denial
from .policy import Denied, key_from_file

CALENDAR_CONSTRAINTS = {
    "event_write",
    "time_format",
    "occurrence_write",
    "all_day_write",
    "provider_write",
    "recurrence_write",
    "future_split",
    "invitations",
    "refresh",
    "window_limit_days",
    "calendar_limit",
    "event_limit",
    "cache_max_age_seconds",
    "refresh_wait_seconds",
    "plan_action_limit",
    "plan_step_limit",
    "plan_ttl_seconds",
    "atomic_cross_provider",
    "availability_sources",
}


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
                result = await self.clients.call(module, "capabilities", credential, {})
        except TimeoutError:
            error = Denied("backend_unavailable", 503)
            error.detail = {"error": "backend_unavailable", "reason": "capabilities_timeout"}
            raise error from None
        permissions = result.get("permissions")
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            raise Denied("backend_capability_contract_unavailable", 503)
        return result

    async def discover(self, key):
        snapshot, agent = self.authenticate(key)
        visible = self.policy.visible(snapshot, agent)
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
                    or bool(actions)
                    or (
                        CATALOG[n]["operation"] == "get_messenger_operation"
                        and bool(self.policy.actions(current, principal, scopes.get("messaging", [])))
                    )
                )
            )
        ]
        manifests = {module: summary for module, caps, _ in results
                     if (summary := manifest_summary(caps)) is not None}
        return current, principal, visible, {
            "states": states, "scopes": scopes, "actions": actions,
            "discovery": discovery(visible, actions, current["revision"]),
            "backend_tool_manifests": manifests,
        }

    def authenticate(self, key):
        """`Policy.authenticate`, recording the holder's expiry as the request's latest."""
        snapshot, agent = self.policy.authenticate(key)
        admitted.set(self.credential(agent))
        return snapshot, agent

    def admit(self, key, name):
        return self.policy.authorize_agent(*self.authenticate(key), name)

    async def call(self, key, name, values):
        try:
            snapshot, agent, tool = self.admit(key, name)
        except Denied as error:
            # One body for a nonexistent name, a tool outside the profile and one of an undeployed module.
            if error.code == "not_found_or_forbidden" and error.status == 403:
                error.detail = unknown_tool_denial()
            raise
        try:
            args = arguments(tool, values)
        except InvalidArguments as error:
            denied = Denied("invalid_arguments", 422)
            if error.field_errors:
                denied.detail = stale_arguments_hint(tool, {
                    "error": "invalid_arguments", "field_errors": error.field_errors,
                    "next_action": "check_parameters"})
            raise denied from None
        except ValueError:
            raise Denied("invalid_arguments", 422) from None
        if tool["module"] == "gateway":
            snapshot, agent, visible, states = await self.discover(key)
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
                "provider_mutations": any(
                    not CATALOG[n].get("annotations", {}).get("readOnlyHint", False) for n in visible
                ),
                "owner_authorization_verified": False,
                "credential": credential,
                "capability": Capability("gateway", "capabilities", snapshot["revision"]).to_dict(),
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
        if not set(tool["scopes"]) <= set(caps["permissions"]):
            raise Denied()
        if (module == "messaging" and tool["operation"] == "get_updates"
                and args.get("start_from") == "now"
                and "cursor-write" not in (set(agent["grants"].get(module, [])) & set(caps["permissions"]))):
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
            if not allowed_actions or ("operation" in args and args["operation"] not in allowed_actions):
                raise Denied()
            if "account" in args:
                allowed_actions = self.actions(snapshot, agent, caps, args["account"])
                if "operation" in args and args["operation"] not in allowed_actions:
                    raise Denied()
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
                else await self.clients.call(module, tool["operation"], credential, args)
            )
        except Denied as error:
            if error.code in {"backend_unavailable", "backend_response_too_large"} and not read_only:
                diagnostics = getattr(error, "detail", {})
                error.detail = {
                    **safe_error("mutation_outcome_unknown", unknown_outcome=True),
                    **{key: diagnostics[key] for key in FAILURE_FIELDS if key in diagnostics},
                }
            elif error.code == "backend_unavailable":
                error.detail = unavailable(tool["operation"], getattr(error, "detail", None),
                                           phase="operation", read_only=True)
            raise
        if module == "calls":
            receipt_reference(tool["operation"], result)
        if (module == "calendar" and tool["operation"] == "plan" and not rules_ceiling and isinstance(result, dict)
                and ("projections_skipped" in result
                     or any(isinstance(st, dict) and "link" in st for st in result.get("steps") or []))):
            # A lesson change the backend carried to its drives shows rule IDs past this gateway's ceiling;
            # the stored plan is never applied through it and expires.
            raise Denied()
        if module == "calendar" and tool["operation"] in {"calendars", "capabilities"}:
            result = dict(result)
            if "constraints" in result:
                constraints = result["constraints"]
                result["constraints"] = (
                    {field: value for field, value in constraints.items() if field in CALENDAR_CONSTRAINTS}
                    if isinstance(constraints, dict)
                    else {}
                )
            selected = set(self.policy.visible(current, principal))
            scopes = set(principal["grants"].get("calendar", [])) & set(caps["permissions"])
            result["calendars"] = [
                dict(
                    row,
                    allowed_event_actions=[
                        action
                        for action in row.get("allowed_event_actions", [])
                        if action in {"create", "update", "delete"}
                        and {"calendar_plan", "calendar_apply"} <= selected
                        and {"plan:create", "plan:execute", "event:" + action} <= scopes
                    ],
                )
                for row in result.get("calendars", [])
            ]
        if module == "calendar" and tool["operation"] == "members":
            # Guardian rules follow the gateway's assignment ceiling, not only the backend key's.
            if "assignment:read" not in set(principal["grants"].get("calendar", [])) & set(caps["permissions"]):
                result = dict(result)
                result["members"] = [
                    {k: v for k, v in row.items() if k != "guardian_rules"} if isinstance(row, dict) else row
                    for row in result.get("members", [])
                ]
        if tool.get("action_group"):
            if tool["operation"] == "list_messenger_operations":
                result = {
                    **result,
                    "items": [
                        row
                        for row in result.get("items", [])
                        if row.get("operation") in allowed_actions and row.get("supported") is True
                    ],
                }
            elif result.get("operation") not in allowed_actions:
                raise Denied()
        if tool["operation"] == "get_attachment" and result.get("download_url"):
            result = self.tickets.issue(
                result, key, agent, snapshot, credential, args["attachment_id"], started
            )
        if tool["operation"] == "capabilities":
            # Preserve scoped coverage/account data, while avoiding the upstream
            # catalog advertising methods unavailable through this connection.
            visible = [
                n
                for n in self.policy.visible(snapshot, agent)
                if CATALOG[n]["module"] == module
                and set(CATALOG[n]["scopes"]) <= set(caps["permissions"])
                and self.implemented(CATALOG[n], snapshot, agent, caps)
                and (
                    not CATALOG[n].get("action_group")
                    or bool(allowed_actions)
                    or (
                        CATALOG[n]["operation"] == "get_messenger_operation"
                        and bool(self.policy.actions(snapshot, agent, caps["permissions"]))
                    )
                )
            ]
            result = dict(result)
            for field in (
                "tool_manifest",
                "messenger_operations",
                "available",
                "features",
                "key_permission_ceiling",
                "permissions",
            ):
                result.pop(field, None)
            result["gateway_tools"] = visible
            result["gateway_principal"] = agent["id"]
            result["backend_principal"] = result.get("principal")
            result["gateway_actions"] = allowed_actions
            result["gateway_actions_scope"] = "messenger_operations"
            manifest = manifest_summary(caps)
            if manifest is not None:
                result["backend_tool_manifest"] = manifest
            if module == "messaging":
                result.update(self.usable_actions(snapshot, agent, caps))
            result["gateway_policy_revision"] = snapshot["revision"]
        return result
