#!/usr/bin/env python3
"""
remove_role_assignments.py

Reads a CSV produced by `generate_cleanup_candidates.py` and removes the listed
role assignments from Azure. By default runs in dry-run mode and only prints what
would be deleted.

Usage:
  python remove_role_assignments.py --csv dup_roles.csv --dry-run

Dry-run is True by default. To perform deletion pass --no-dry-run.
"""

from __future__ import annotations

import argparse
import csv
import logging
from typing import List

from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient

LOG = logging.getLogger(__name__)


def delete_assignment_by_id(client: AuthorizationManagementClient, assignment_id: str):
    # The SDK provides delete_by_id; assignment_id is expected to be the full resource id
    LOG.debug("Deleting assignment by id %s", assignment_id)
    return client.role_assignments.delete_by_id(assignment_id)


def main():
    parser = argparse.ArgumentParser(
        description="Remove role assignments listed in CSV"
    )
    parser.add_argument(
        "--csv", required=True, help="CSV file with assignment_id column"
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Only print actions (default)",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Actually perform deletions",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    # Read CSV
    rows: List[dict] = []
    with open(args.csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)

    if not rows:
        LOG.info("No rows found in %s", args.csv)
        return

    # We need a subscription id to build client, try to read subscription_id from first row
    sub_id = rows[0].get("subscription_id")
    if not sub_id:
        LOG.error("CSV missing subscription_id column or it's empty in first row")
        return

    cred = DefaultAzureCredential()
    client = AuthorizationManagementClient(cred, sub_id)

    for r in rows:
        assignment_id = r.get("assignment_id") or r.get("assignmentId") or r.get("id")
        role_name = r.get("role_name") or r.get("role")
        principal = r.get("principal_displayName") or r.get("principal_id")
        if not assignment_id:
            LOG.warning("Skipping row without assignment_id: %s", r)
            continue

        if args.dry_run:
            LOG.info(
                "DRY-RUN: Would remove assignment %s (role=%s principal=%s)",
                assignment_id,
                role_name,
                principal,
            )
        else:
            try:
                delete_assignment_by_id(client, assignment_id)
                LOG.info("Deleted assignment %s", assignment_id)
            except Exception as ex:
                LOG.exception("Failed to delete assignment %s: %s", assignment_id, ex)


if __name__ == "__main__":
    main()
