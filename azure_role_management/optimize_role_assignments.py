import argparse
import logging
import csv
from collections import defaultdict

import requests
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_graph_token(credential):
    token = credential.get_token("https://graph.microsoft.com/.default")
    return token.token


def get_transitive_member_of(user_id, graph_token, log_context=None):
    """
    Fetches the groups that the user is a transitive member of.
    Returns a set of Group IDs.
    """
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Content-Type": "application/json",
    }
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/transitiveMemberOf"
    group_ids = set()

    while url:
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            for item in data.get("value", []):
                # We are only interested in groups, but transitiveMemberOf can return DirectoryRoles too
                if item.get("@odata.type") == "#microsoft.graph.group":
                    group_ids.add(item["id"])

            url = data.get("@odata.nextLink")
        except requests.exceptions.HTTPError as e:
            msg = f"Error fetching transitive members for user {user_id}: {e}"
            if log_context:
                msg += f" | Context: {log_context}"
            logger.error(msg)
            break
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            break

    return group_ids


def get_user_display_name(user_id, graph_token, log_context=None):
    """
    Fetches the user's display name from Microsoft Graph.
    """
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Content-Type": "application/json",
    }
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}?$select=displayName,userPrincipalName"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return f"{data.get('displayName')} ({data.get('userPrincipalName')})"
    except Exception as e:
        msg = f"Could not fetch name for user {user_id}: {e}"
        if log_context:
            msg += f" | Context: {log_context}"
        logger.warning(msg)
        return user_id


def get_group_display_name(group_id, graph_token):
    """
    Fetches the group's display name from Microsoft Graph.
    """
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Content-Type": "application/json",
    }
    url = f"https://graph.microsoft.com/v1.0/groups/{group_id}?$select=displayName"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return f"{data.get('displayName')} ({group_id})"
    except Exception as e:
        logger.warning(f"Could not fetch name for group {group_id}: {e}")
        return group_id


role_name_cache = {}

def get_role_name(role_def_id, auth_client):
    if role_def_id in role_name_cache:
        return role_name_cache[role_def_id]
    
    try:
        role_def = auth_client.role_definitions.get_by_id(role_def_id)
        role_name = role_def.role_name
        role_name_cache[role_def_id] = role_name
        return role_name
    except Exception as e:
        logger.warning(f"Could not fetch role name for {role_def_id}: {e}")
        return role_def_id


def get_parent_scopes(scope):
    """
    Generates all parent scopes for a given scope, including the scope itself.
    Example: /subscriptions/s/resourceGroups/r/providers/p/t/n
    Returns: [
        /subscriptions/s/resourceGroups/r/providers/p/t/n,
        /subscriptions/s/resourceGroups/r,
        /subscriptions/s
    ]
    """
    scopes = [scope]
    # Simple string manipulation to find parents
    # Scopes are hierarchical paths.
    # We can strip the last segment (which is usually /type/name or just /name depending on structure)
    # But Azure resource IDs are regular:
    # /subscriptions/{sub}
    # /subscriptions/{sub}/resourceGroups/{rg}
    # /subscriptions/{sub}/resourceGroups/{rg}/providers/{prov}/{type}/{name}

    # We can try to find the parent by removing the last resource segment.
    # A resource segment is usually 2 parts: /{type}/{name} or just /{name} for some?
    # Actually, standard resource ID structure is alternating key/value.
    # But simpler approach:
    # 1. /subscriptions/X/resourceGroups/Y/providers/P/T/N -> Parent: /subscriptions/X/resourceGroups/Y
    # 2. /subscriptions/X/resourceGroups/Y -> Parent: /subscriptions/X
    # 3. /subscriptions/X -> Parent: / (Root, but we usually stop at sub for this script)

    parts = scope.split("/")
    # parts[0] is empty because scope starts with /

    # Check for Resource Group level
    if "/resourceGroups/" in scope:
        # Find the index of resourceGroups
        try:
            rg_index = parts.index("resourceGroups")
            # parts[rg_index] is 'resourceGroups', parts[rg_index+1] is the RG name
            rg_scope = "/".join(parts[: rg_index + 2])
            if rg_scope != scope:
                scopes.append(rg_scope)
        except ValueError:
            pass

    # Check for Subscription level
    if "/subscriptions/" in scope:
        try:
            sub_index = parts.index("subscriptions")
            # parts[sub_index] is 'subscriptions', parts[sub_index+1] is the sub ID
            sub_scope = "/".join(parts[: sub_index + 2])
            if sub_scope != scope and sub_scope not in scopes:
                scopes.append(sub_scope)
        except ValueError:
            pass

    return scopes


def main():
    parser = argparse.ArgumentParser(
        description="Optimize Azure Role Assignments by identifying redundant user assignments covered by group memberships."
    )
    parser.add_argument(
        "--subscription-id",
        default="921496dc-987f-410f-bd57-426eb2611356",
        help="The Azure Subscription ID.",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Execute the deletions. Default is dry-run mode.",
    )
    parser.add_argument(
        "--output-csv",
        help="Path to output CSV file with redundant assignments.",
    )
    parser.set_defaults(dry_run=True)

    args = parser.parse_args()
    subscription_id = args.subscription_id
    dry_run = args.dry_run
    output_csv = args.output_csv

    credential = DefaultAzureCredential()

    logger.info(f"Authenticating and connecting to subscription {subscription_id}...")
    auth_client = AuthorizationManagementClient(credential, subscription_id)

    # 1. Fetch all Role Assignments
    logger.info("Fetching all role assignments...")
    role_assignments = list(auth_client.role_assignments.list_for_subscription())

    # Group assignments by (Scope, RoleDefinitionId)
    # Structure: { (scope, role_def_id): { 'users': [assignment], 'groups': [assignment] } }
    # We also need a quick lookup for Group Assignments by (Scope, Role)
    group_assignments_map = defaultdict(list)
    user_assignments = []

    count = 0
    for ra in role_assignments:
        count += 1
        if ra.principal_type == "User":
            user_assignments.append(ra)
        elif ra.principal_type == "Group":
            group_assignments_map[
                (ra.scope.lower(), ra.role_definition_id.lower())
            ].append(ra)

    logger.info(f"Total Role Assignments found: {count}")
    logger.info(f"User Assignments: {len(user_assignments)}")
    logger.info(
        f"Group Assignments: {sum(len(v) for v in group_assignments_map.values())}"
    )

    # 2. Identify Redundancies
    logger.info("Analyzing for redundancies...")

    graph_token = get_graph_token(credential)

    redundant_assignments = []

    # Cache for user group memberships to avoid repeated API calls for the same user
    user_groups_cache = {}
    user_name_cache = {}
    group_name_cache = {}

    for user_ra in user_assignments:
        user_id = user_ra.principal_id
        role_def_id = user_ra.role_definition_id.lower()
        scope = user_ra.scope.lower()

        # Get user's groups (with caching)
        if user_id not in user_groups_cache:
            role_name_for_log = get_role_name(user_ra.role_definition_id, auth_client)
            log_context = f"Role: '{role_name_for_log}', Scope: '{scope}'"
            
            user_groups_cache[user_id] = get_transitive_member_of(user_id, graph_token, log_context)
            user_name_cache[user_id] = get_user_display_name(user_id, graph_token, log_context)

        user_member_groups = user_groups_cache[user_id]
        user_name = user_name_cache[user_id]

        # Check this scope and all parent scopes for a matching group assignment
        parent_scopes = get_parent_scopes(scope)

        for check_scope in parent_scopes:
            check_scope = check_scope.lower()
            potential_group_assignments = group_assignments_map.get(
                (check_scope, role_def_id), []
            )

            assigned_group_ids = set(
                ra.principal_id for ra in potential_group_assignments
            )

            # Check intersection
            covering_groups = user_member_groups.intersection(assigned_group_ids)

            if covering_groups:
                role_name = get_role_name(user_ra.role_definition_id, auth_client)
                
                covering_group_names = []
                for gid in covering_groups:
                    if gid not in group_name_cache:
                        group_name_cache[gid] = get_group_display_name(gid, graph_token)
                    covering_group_names.append(group_name_cache[gid])

                redundant_assignments.append(
                    {
                        "user_assignment": user_ra,
                        "covering_group_ids": list(covering_groups),
                        "covering_scope": check_scope,
                        "user_name": user_name,
                        "role_name": role_name,
                    }
                )

                logger.info(
                    f"REDUNDANT: User {user_name} ({user_id}) has assignment '{role_name}' on {scope}. "
                    f"Covered by Group(s) {covering_group_names} on scope {check_scope}"
                )
                break  # Found a covering group, no need to check further up

    logger.info(f"Found {len(redundant_assignments)} redundant assignments.")

    # 3. Process Redundancies (Delete or Report)
    if not redundant_assignments:
        logger.info("No redundant assignments found.")
        return

    if output_csv:
        logger.info(f"Writing redundant assignments to {output_csv}...")
        try:
            with open(output_csv, mode='w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['RoleAssignmentName', 'Scope', 'PrincipalId', 'PrincipalName', 'RoleName', 'CoveringGroup', 'CoveringScope']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for item in redundant_assignments:
                    ra = item['user_assignment']
                    # covering_group_ids is a list, let's join them
                    covering_groups_str = ";".join(item.get('covering_group_ids', []))
                    
                    writer.writerow({
                        'RoleAssignmentName': ra.name,
                        'Scope': ra.scope,
                        'PrincipalId': ra.principal_id,
                        'PrincipalName': item.get('user_name', ''),
                        'RoleName': item.get('role_name', ''),
                        'CoveringGroup': covering_groups_str,
                        'CoveringScope': item.get('covering_scope', '')
                    })
            logger.info(f"Successfully wrote to {output_csv}")
        except Exception as e:
            logger.error(f"Failed to write CSV file: {e}")

    if dry_run:
        logger.info("Dry run enabled. No assignments will be deleted.")
    else:
        logger.info("Deleting redundant assignments...")
        for item in redundant_assignments:
            ra = item["user_assignment"]
            user_name = item.get("user_name", ra.principal_id)
            try:
                # role_assignments.delete_by_id is not available, usually delete(scope, name)
                # ra.id is the full resource ID of the assignment
                # We can use delete_by_id if available or parse scope and name
                # The SDK usually has delete_by_id on the operations object in newer versions,
                # or we use delete(scope, ra_name)

                logger.info(f"Deleting assignment {ra.name} for user {user_name}...")
                # Use delete(scope, name) which is standard for AuthorizationManagementClient
                auth_client.role_assignments.delete(
                    scope=ra.scope, role_assignment_name=ra.name
                )
                logger.info("Deleted.")
            except Exception as e:
                logger.error(f"Failed to delete assignment {ra.id}: {e}")


if __name__ == "__main__":
    main()
