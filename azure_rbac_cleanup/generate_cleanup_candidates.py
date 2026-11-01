#!/usr/bin/env python3
"""Generate reviewable redundant user, service principal and managed identity RBAC assignments."""
from __future__ import annotations

import argparse
import csv
import logging
import time
from collections import defaultdict, deque

import requests
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignment, RoleDefinition

LOG = logging.getLogger(__name__)
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
REQUEST_TIMEOUT_SECONDS = 60
TOKEN_REFRESH_MARGIN_SECONDS = 60
PRINCIPAL_ENDPOINTS = {"User": "users", "ServicePrincipal": "servicePrincipals"}
PRINCIPAL_TYPES = {
    "#microsoft.graph.user": "User",
    "#microsoft.graph.serviceprincipal": "ServicePrincipal",
    "#microsoft.graph.group": "Group",
}
SCAN_TYPES = ("user", "service-principal", "managed-identity", "all")
DEFAULT_CSV_PATH = "dup_roles.csv"
CSV_FIELDS = [
    "subscription_id", "role_name", "role_definition_id", "assignment_id",
    "assignment_name", "principal_type", "principal_id", "principal_displayName",
    "principal_userPrincipalName", "assignment_scope", "duplicated_via_groups",
    "duplicated_via_groups_name",
    "service_principal_type", "redundancy_reason", "covering_assignment_ids",
    "covering_assignment_scopes",
]


class GraphClient:
    def __init__(self, credential: DefaultAzureCredential, api_version: str = "v1.0"):
        self.credential = credential
        self.base = f"https://graph.microsoft.com/{api_version}"
        self._token = None
        self._objects: dict[str, dict | None] = {}
        self._parents: dict[tuple[str, str], set[str]] = {}
        self._memberships: dict[tuple[str, str], set[str]] = {}
        self._groups: dict[str, dict | None] = {}
        self._principal_info: dict[tuple[str, str], dict] = {}

    def _get(self, url: str) -> dict | None:
        if self._token is None or self._token.expires_on <= time.time() + TOKEN_REFRESH_MARGIN_SECONDS:
            self._token = self.credential.get_token(GRAPH_SCOPE)
        headers = {"Authorization": f"Bearer {self._token.token}"}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 404:
            LOG.warning("Graph object not found: %s", url)
            return None
        if not response.ok:
            LOG.error("Graph GET %s failed: %s %s", url, response.status_code, response.text)
            response.raise_for_status()
        return response.json()

    def get_directory_object(self, principal_id: str) -> dict | None:
        if principal_id not in self._objects:
            self._objects[principal_id] = self._get(f"{self.base}/directoryObjects/{principal_id}")
        return self._objects[principal_id]

    def get_principal_info(self, principal_id: str, principal_type: str) -> dict:
        endpoint = PRINCIPAL_ENDPOINTS[principal_type]
        key = (principal_type, principal_id)
        if key not in self._principal_info:
            self._principal_info[key] = self._get(
                f"{self.base}/{endpoint}/{principal_id}"
            ) or {"id": principal_id}
        return self._principal_info[key]

    def get_group_info(self, group_id: str) -> dict | None:
        group_id = group_id.lower()
        if group_id not in self._groups:
            self._groups[group_id] = self._get(
                f"{self.base}/groups/{group_id}?$select=id,displayName,securityEnabled"
            )
        return self._groups[group_id]

    def _direct_groups(self, endpoint: str, object_id: str) -> set[str]:
        key = (endpoint, object_id)
        if key not in self._parents:
            groups: set[str] = set()
            url = f"{self.base}/{endpoint}/{object_id}/memberOf"
            while url:
                page = self._get(url)
                if page is None:
                    break
                for obj in page.get("value", []):
                    if obj.get("@odata.type", "").lower() == "#microsoft.graph.group":
                        group = self.get_group_info(obj["id"])
                        if group and group.get("securityEnabled") is True:
                            groups.add(obj["id"].lower())
                url = page.get("@odata.nextLink")
            self._parents[key] = groups
        return self._parents[key]

    def get_principal_groups(self, principal_id: str, principal_type: str) -> set[str]:
        """Walk security-group parents, including nested groups, for users and SPs.

        Querying SP memberOf avoids Graph v1.0's group-members omission of SPs.
        """
        key = (principal_type, principal_id)
        if key not in self._memberships:
            groups: set[str] = set()
            queue = deque(self._direct_groups(PRINCIPAL_ENDPOINTS[principal_type], principal_id))
            while queue:
                group_id = queue.popleft()
                if group_id in groups:
                    continue
                groups.add(group_id)
                queue.extend(self._direct_groups("groups", group_id) - groups)
            self._memberships[key] = groups
        return self._memberships[key]


def normalize_role_def_id(role_def_id: str | None) -> str:
    return (role_def_id or "").strip().rstrip("/").split("/")[-1].lower()


def normalize_scope(scope: str | None) -> str:
    return (scope or "").strip().rstrip("/").lower()


def scope_covers(covering_scope: str | None, target_scope: str | None) -> bool:
    covering = normalize_scope(covering_scope)
    target = normalize_scope(target_scope)
    return bool(covering and target) and (covering == target or target.startswith(covering + "/"))


def unrestricted(assignment: RoleAssignment) -> bool:
    # Conditional and delegated grants need a separate access-equivalence analysis.
    return not assignment.condition and not assignment.delegated_managed_identity_resource_id


def generate_candidates(
    subscription_id: str,
    assignments: list[RoleAssignment],
    role_defs: list[RoleDefinition],
    graph: GraphClient,
    scan_type: str = "user",
) -> list[dict]:
    if scan_type not in SCAN_TYPES:
        raise ValueError(f"Unsupported principal type: {scan_type}")
    scope = f"/subscriptions/{subscription_id}"
    role_names = {normalize_role_def_id(rd.id): rd.role_name for rd in role_defs}
    by_role: dict[str, list[RoleAssignment]] = defaultdict(list)
    principal_types: dict[str, str] = {}
    restricted_count = 0
    for assignment in assignments:
        if not assignment.id or not assignment.principal_id or not assignment.role_definition_id:
            LOG.warning("Skipping assignment with missing identifiers: %s", assignment.id)
            continue
        if not scope_covers(scope, assignment.scope):
            continue
        if not unrestricted(assignment):
            restricted_count += 1
            LOG.debug("Skipping conditional or delegated assignment %s", assignment.id)
            continue
        principal_id = assignment.principal_id.lower()
        principal_type = assignment.principal_type
        if not principal_type:
            obj = graph.get_directory_object(principal_id)
            principal_type = PRINCIPAL_TYPES.get((obj or {}).get("@odata.type", "").lower())
        if principal_type not in (*PRINCIPAL_ENDPOINTS, "Group"):
            LOG.warning("Skipping unsupported or unresolved principal %s (%s)", principal_id, principal_type)
            continue
        principal_types[principal_id] = principal_type
        by_role[normalize_role_def_id(assignment.role_definition_id)].append(assignment)
    LOG.info("Excluded %d conditional or delegated assignments", restricted_count)

    results = []
    for role_id, role_assignments in by_role.items():
        groups = [
            a for a in role_assignments if principal_types[a.principal_id.lower()] == "Group"
        ]
        retained: dict[str, list[RoleAssignment]] = defaultdict(list)
        # Ancestors precede descendants; equal-scope duplicates retain the lowest ID.
        direct = sorted(
            (a for a in role_assignments if principal_types[a.principal_id.lower()] in PRINCIPAL_ENDPOINTS),
            key=lambda a: (normalize_scope(a.scope).count("/"), normalize_scope(a.scope), a.id.lower()),
        )
        for assignment in direct:
            principal_id = assignment.principal_id.lower()
            principal_type = principal_types[principal_id]
            if scan_type == "user" and principal_type != "User":
                continue
            if scan_type in ("service-principal", "managed-identity"):
                if principal_type != "ServicePrincipal":
                    continue
                info = graph.get_principal_info(principal_id, principal_type)
                is_managed_identity = info.get("servicePrincipalType") == "ManagedIdentity"
                if scan_type == "managed-identity" and not is_managed_identity:
                    continue
                if scan_type == "service-principal":
                    if is_managed_identity:
                        continue
                    if not info.get("servicePrincipalType"):
                        LOG.warning("Skipping unclassified service principal %s", principal_id)
                        continue
            covering = [a for a in retained[principal_id] if scope_covers(a.scope, assignment.scope)]
            eligible_groups = [a for a in groups if scope_covers(a.scope, assignment.scope)]
            via_groups = []
            if eligible_groups:
                memberships = graph.get_principal_groups(principal_id, principal_type)
                via_groups = [a for a in eligible_groups if a.principal_id.lower() in memberships]
                covering.extend(via_groups)
            if not covering:
                retained[principal_id].append(assignment)
                continue

            info = graph.get_principal_info(principal_id, principal_type)
            covering.sort(key=lambda a: a.id.lower())
            group_ids = sorted({a.principal_id for a in via_groups})
            group_names = []
            for group_id in group_ids:
                group = graph.get_group_info(group_id)
                name = (group or {}).get("displayName") or ""
                if not name:
                    LOG.warning("Display name unavailable for covering group %s", group_id)
                group_names.append(name)
            results.append({
                "subscription_id": subscription_id,
                "role_name": role_names.get(role_id, role_id),
                "role_definition_id": assignment.role_definition_id,
                "assignment_id": assignment.id,
                "assignment_name": assignment.name,
                "principal_type": principal_type,
                "principal_id": assignment.principal_id,
                "principal_displayName": info.get("displayName", ""),
                "principal_userPrincipalName": info.get("userPrincipalName") or info.get("mail") or "",
                "assignment_scope": assignment.scope,
                "duplicated_via_groups": ";".join(group_ids),
                "duplicated_via_groups_name": ";".join(group_names),
                "service_principal_type": info.get("servicePrincipalType", ""),
                "redundancy_reason": "group_membership" if via_groups else "same_principal_assignment",
                "covering_assignment_ids": ";".join(a.id for a in covering),
                "covering_assignment_scopes": ";".join(a.scope for a in covering),
            })
    return results


def write_candidates(path: str, results: list[dict]) -> None:
    # Always replace the output, including empty scans, so stale candidates cannot survive.
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="Output CSV file path (default: %(default)s)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--principal-type", choices=SCAN_TYPES, default="user",
        help="Principal category to scan (default: user); service-principal excludes managed identities",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    if not args.debug:
        logging.getLogger("azure").setLevel(logging.WARNING)

    cred = DefaultAzureCredential()
    auth_client = AuthorizationManagementClient(cred, args.subscription_id)
    graph = GraphClient(cred)
    scope = f"/subscriptions/{args.subscription_id}"
    LOG.info("Loading role definitions for subscription %s", args.subscription_id)
    role_defs = list(auth_client.role_definitions.list(scope, filter=""))
    LOG.info("Found %d role definitions", len(role_defs))
    LOG.info("Loading role assignments for scope %s", scope)
    assignments = list(auth_client.role_assignments.list_for_scope(scope))
    LOG.info("Found %d role assignments", len(assignments))
    results = generate_candidates(
        args.subscription_id, assignments, role_defs, graph, args.principal_type
    )
    write_candidates(args.csv, results)
    LOG.info("Wrote %d candidate rows to %s; review before removing assignments", len(results), args.csv)


if __name__ == "__main__":
    main()
