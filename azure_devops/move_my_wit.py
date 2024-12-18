import argparse
import datetime

from azure.devops.connection import Connection
from azure.devops.released.work import TeamContext
from msrest.authentication import BasicAuthentication

# Replace YOUR_PERSONAL_ACCESS_TOKEN with your personal access token which has work item read/write permissions
PERSONAL_ACCESS_TOKEN = "YOUR_PERSONAL_ACCESS_TOKEN"

# Define the user to move work items for
ASSIGNED_TO = "Shu Peng"

# Define the area path, exclude area path, and assigned to list
area_path = "Vienna\\Agents"

# Exclude the backlog area path
exclude_iteration_path = "Vienna\\Backlog"

# Define the work item types that should be moved
work_item_types = ["Task", "Bug"]

# Define the work item states that should NOT be changed
work_item_state = ["Done", "Resolved", "Removed"]


organization_url = "https://dev.azure.com/msdata"
project_name = "Vienna"
team_name = "Agents Runtime (RAGandOYDtoo)"
# Create a connection to the organization
credentials = BasicAuthentication("", PERSONAL_ACCESS_TOKEN)
connection = Connection(base_url=organization_url, creds=credentials)


# Get the project id
def get_project_id(project_name):
    core_client = connection.clients.get_core_client()
    for project in core_client.get_projects():
        if project.name == project_name:
            print(f"Found project {project_name}, with ID {project.id}")
            return project.id
    return None


# Get the team id
def get_team_id(project_name, team_name):
    project_id = get_project_id(project_name)
    if not project_id:
        raise Exception(f"Project {project_name} not found")

    core_client = connection.clients.get_core_client()
    continue_search = True
    skipped_teams = 0
    while continue_search:
        continue_search = False
        for team in core_client.get_teams(project_id, skip=skipped_teams):
            skipped_teams += 1
            continue_search = True
            if team.name == team_name:
                team_id = team.id
                print(f"Found team {team_name}, with ID {team_id}")
                return team_id
    return None


# Find the current sprint
def get_current_sprint(project_name, team_name):
    project_id = get_project_id(project_name)
    if not project_id:
        raise Exception(f"Project {project_name} not found")
    team_id = get_team_id(project_name, team_name)
    if not team_id:
        raise Exception(f"Team {team_name} not found")

    team_context = TeamContext(project_id=project_id, team_id=team_id)
    today = datetime.datetime.now(datetime.UTC)
    work_client = connection.clients.get_work_client()
    for iteration in work_client.get_team_iterations(team_context):
        if iteration.attributes.start_date <= today <= iteration.attributes.finish_date:
            return iteration.path
    return None


# Find future sprints
def get_future_sprints(project_name, team_name):
    project_id = get_project_id(project_name)
    if not project_id:
        raise Exception(f"Project {project_name} not found")
    team_id = get_team_id(project_name, team_name)
    if not team_id:
        raise Exception(f"Team {team_name} not found")

    team_context = TeamContext(project_id=project_id, team_id=team_id)
    today = datetime.datetime.now(datetime.UTC)
    work_client = connection.clients.get_work_client()
    future_sprints = []
    for iteration in work_client.get_team_iterations(team_context):
        if iteration.attributes.start_date > today:
            future_sprints.append(iteration.path)
    return future_sprints


def other_sprint_wits(excluded_sprints, assigned_to):
    wit_client = connection.clients.get_work_item_tracking_client()

    excluded_sprints_string = f"('{"', '".join(excluded_sprints)}')"
    assigned_to_string = f"('{"', '".join(assigned_to)}')"
    work_item_type_string = f"('{"', '".join(work_item_types)}')"
    work_item_state_string = f"('{"', '".join(work_item_state)}')"

    query = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.IterationPath] NOT IN {excluded_sprints_string}
    AND [System.AreaPath] UNDER '{area_path}'
    AND [System.AssignedTo] IN {assigned_to_string}
    AND [System.WorkItemType] IN {work_item_type_string}
    AND [System.State] NOT IN {work_item_state_string}
    """
    wiql = {"query": query}
    return wit_client.query_by_wiql(wiql).work_items


def move_wit_to_current_sprint(work_items, current_sprint):
    wit_client = connection.clients.get_work_item_tracking_client()
    for work_item in work_items:
        work_item_id = work_item.id
        document = [
            {
                "op": "add",
                "path": "/fields/System.IterationPath",
                "value": current_sprint,
            }
        ]
        print(f"Moving work item {work_item_id} to {current_sprint}")
        wit_client.update_work_item(document, work_item_id)


def get_wit_by_id(work_item_id):
    wit_client = connection.clients.get_work_item_tracking_client()
    return wit_client.get_work_item(work_item_id)


def main():
    # Initialize parser
    parser = argparse.ArgumentParser(
        description="Command line tool to move work items to the current sprint."
    )

    # Adding optional argument
    parser.add_argument(
        "--assigned", default=ASSIGNED_TO, help="User to move work items for."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the actions without making any changes.",
    )

    # Read arguments from command line
    args = parser.parse_args()

    # Define the list of users to move work items for
    assigned_to = [f"{args.assigned}", ""]

    current_sprint = get_current_sprint(project_name, team_name)
    if not current_sprint:
        raise Exception("Current sprint not found")
    excluded_sprints = get_future_sprints(project_name, team_name)
    excluded_sprints.append(current_sprint)
    excluded_sprints.append(exclude_iteration_path)
    work_items = other_sprint_wits(excluded_sprints, assigned_to)
    if args.dry_run:
        print(f"{len(work_items)} work items would be moved")
        for work_item in work_items:
            wit = get_wit_by_id(work_item.id)
            print(
                f"{wit.id}|{wit.fields['System.WorkItemType']}|Created by {wit.fields['System.CreatedBy']['displayName']}|{wit.fields['System.Title']}"
            )
    else:
        move_wit_to_current_sprint(work_items, current_sprint)
        print(f"{len(work_items)} work items moved successfully")


if __name__ == "__main__":
    main()
