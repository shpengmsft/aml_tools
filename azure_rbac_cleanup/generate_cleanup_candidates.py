#!/usr/bin/env python3
"""
generate_cleanup_candidates.py

Scans a subscription's role assignments and produces a CSV of "duplicated" user
role assignments: users who have a direct user assignment for a role while they
also receive the role via a group (including nested groups).

Usage:
  python generate_cleanup_candidates.py --subscription-id <sub-id> --csv dup_roles.csv

Notes:
 - Uses azure.mgmt.authorization for role assignments/definitions and
   azure.identity.DefaultAzureCredential for auth.
 - Uses Microsoft Graph (via REST) to enumerate group members (including nested groups).
 - Graph token is acquired via DefaultAzureCredential for scope https://graph.microsoft.com/.default

"""
from __future__ import annotations

import argparse
import csv
import logging
from collections import deque, defaultdict
from typing import Dict, Set, List

import requests
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient

LOG = logging.getLogger(__name__)


class GraphClient:
    def __init__(self, credential: DefaultAzureCredential, api_version: str = "v1.0"):
        self.credential = credential
        self.api_version = api_version
        token = credential.get_token("https://graph.microsoft.com/.default")
        self._headers = {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}
        self.base = f"https://graph.microsoft.com/{self.api_version}"

    def _get(self, url, params=None):
        res = requests.get(url, headers=self._headers, params=params or {})
        # Treat 404 as "not found" and return None so callers can handle absence gracefully
        if res.status_code == 404:
            LOG.debug("Graph GET %s returned 404: %s", url, res.text)
            return None
        if not res.ok:
            LOG.error("Graph GET %s failed: %s %s", url, res.status_code, res.text)
            res.raise_for_status()
        return res.json()

    def get_group_members_recursive(self, group_id: str) -> Set[str]:
        """Return set of user object ids that are members of group_id (expand nested groups)."""
        users: Set[str] = set()
        visited_groups: Set[str] = set()
        q = deque([group_id])

        while q:
            gid = q.popleft()
            if gid in visited_groups:
                continue
            visited_groups.add(gid)
            # members endpoint returns directoryObjects that can be users or groups or servicePrincipals
            url = f"{self.base}/groups/{gid}/members"
            params = {"$top": 999}
            while url:
                resp = self._get(url, params=params)
                if resp is None:
                    LOG.warning("Group %s not found when expanding members", gid)
                    break
                for member in resp.get("value", []):
                    odata_type = member.get("@odata.type", "")
                    if "user" in odata_type.lower() or member.get("userPrincipalName"):
                        users.add(member["id"])
                    elif "group" in odata_type.lower() or member.get("groupTypes") is not None:
                        q.append(member["id"])
                    else:
                        # ignore service principals and other types
                        continue
                url = resp.get("@odata.nextLink")
                params = None

        return users

    def get_user_info(self, user_id: str) -> Dict:
        url = f"{self.base}/users/{user_id}?$select=id,displayName,mail,userPrincipalName"
        return self._get(url) or {"id": user_id}

    def get_directory_object(self, obj_id: str) -> Dict | None:
        """Return directory object for id (could be user, group, servicePrincipal, etc.).
        Returns None if not found.
        """
        url = f"{self.base}/directoryObjects/{obj_id}"
        return self._get(url)

    def get_group_info(self, group_id: str) -> Dict:
        url = f"{self.base}/groups/{group_id}?$select=id,displayName,mail"
        return self._get(url)


def normalize_role_def_id(role_def_id: str) -> str:
    # Role definition id returned in assignments is the full resource id; this helper
    # extracts the trailing GUID to compare with roleDefinition.id which may be GUID or resource id.
    if role_def_id is None:
        return ""
    return role_def_id.rstrip().split("/")[-1]


def main():
    parser = argparse.ArgumentParser(description="Generate CSV of duplicated role assignments (user assigned directly and via group)")
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--csv", required=True, help="Output CSV file path")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    cred = DefaultAzureCredential()
    auth_client = AuthorizationManagementClient(cred, args.subscription_id)
    graph = GraphClient(cred)

    scope = f"/subscriptions/{args.subscription_id}"

    # load role definitions for the subscription
    LOG.info("Loading role definitions for subscription %s", args.subscription_id)
    role_defs = list(auth_client.role_definitions.list(scope, filter=""))
    LOG.info("Found %d role definitions", len(role_defs))

    # gather assignments for the subscription scope
    LOG.info("Loading role assignments for scope %s", scope)
    assignments = list(auth_client.role_assignments.list_for_scope(scope))
    LOG.info("Found %d role assignments", len(assignments))

    # Map role_def_guid -> group_member_user_ids (set) and user assignments list
    results = []  # rows to write

    # Pre-index assignments by roleDefinition id trailing GUID
    assignments_by_role: Dict[str, List] = defaultdict(list)
    for a in assignments:
        rid = normalize_role_def_id(getattr(a, "role_definition_id", "") or "")
        assignments_by_role[rid].append(a)

    for rd in role_defs:
        rd_guid = normalize_role_def_id(getattr(rd, "id", ""))
        rd_name = getattr(rd, "role_name", getattr(rd, "properties", {}).get("roleName", ""))
        rd_id_full = getattr(rd, "id", str(rd))

        role_assigns = assignments_by_role.get(rd_guid, [])
        if not role_assigns:
            continue

        # gather users that come via group assignments
        group_member_user_ids: Set[str] = set()
        # track which groups were the source
        group_sources: Dict[str, List[str]] = defaultdict(list)

        for a in role_assigns:
            principal_type = getattr(a, "principal_type", None)
            principal_id = getattr(a, "principal_id", None)
            if not principal_id:
                continue

            # If principal_type says Group or the principal id corresponds to a group, treat as group
            is_group = False
            if principal_type and principal_type.lower() == "group":
                is_group = True
            else:
                # Use directoryObjects lookup to determine object type (handles 404 gracefully)
                try:
                    dobj = graph.get_directory_object(principal_id)
                    if dobj is not None:
                        odata_type = dobj.get("@odata.type", "").lower()
                        # graph types look like "#microsoft.graph.group" or "#microsoft.graph.user"
                        if "group" in odata_type or dobj.get("groupTypes") is not None:
                            is_group = True
                except Exception:
                    LOG.exception("Failed to query directory object %s", principal_id)

            if is_group:
                members = graph.get_group_members_recursive(principal_id)
                if members:
                    LOG.debug("Group %s has %d user members", principal_id, len(members))
                    for u in members:
                        group_member_user_ids.add(u)
                        group_sources[u].append(principal_id)
                else:
                    LOG.debug("No members found or group not accessible for %s", principal_id)

        # find direct user assignments duplicated by group membership
        for a in role_assigns:
            principal_type = getattr(a, "principal_type", None)
            principal_id = getattr(a, "principal_id", None)
            if not principal_id:
                continue
            if principal_type and principal_type.lower() != "user":
                # skip non-user direct assignments
                continue

            if principal_id in group_member_user_ids:
                # duplicated candidate
                try:
                    info = graph.get_user_info(principal_id)
                except Exception:
                    info = {"id": principal_id}

                sources = ";".join(group_sources.get(principal_id, []))

                results.append({
                    "subscription_id": args.subscription_id,
                    "role_name": rd_name,
                    "role_definition_id": rd_id_full,
                    "assignment_id": getattr(a, "id", ""),
                    "assignment_name": getattr(a, "name", ""),
                    "principal_type": principal_type or "User",
                    "principal_id": principal_id,
                    "principal_displayName": info.get("displayName", ""),
                    "principal_userPrincipalName": info.get("userPrincipalName", info.get("mail", "")),
                    "assignment_scope": getattr(a, "scope", ""),
                    "duplicated_via_groups": sources,
                })

    # write CSV
    if results:
        fieldnames = [
            "subscription_id",
            "role_name",
            "role_definition_id",
            "assignment_id",
            "assignment_name",
            "principal_type",
            "principal_id",
            "principal_displayName",
            "principal_userPrincipalName",
            "assignment_scope",
            "duplicated_via_groups",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                writer.writerow(r)
        LOG.info("Wrote %d candidate rows to %s", len(results), args.csv)
    else:
        LOG.info("No duplicated role assignments found for subscription %s", args.subscription_id)


if __name__ == "__main__":
    main()
