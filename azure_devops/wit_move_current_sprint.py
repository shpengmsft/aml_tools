import datetime

from azure.devops.connection import Connection
from azure.devops.released.work import TeamContext
from msrest.authentication import BasicAuthentication

# Replace YOUR_PERSONAL_ACCESS_TOKEN with your personal access token which has work item read/write permissions
personal_access_token = "YOUR_PERSONAL_ACCESS_TOKEN"

# Define the area path, exclude area path, and assigned to list
area_path = "Vienna\\Agents"

# Exclude the backlog area path
exclude_area_path = "Vienna\\Backlog"

# Define the list of users to move work items for
assigned_to = ["User A", "User B", "User C"]

# Define the work item types that should be moved
work_item_types = ["Task", "Bug"]

# Define the work item states that should NOT be changed
work_item_state = ["Done", "Resolved", "Removed"]


organization_url = "https://dev.azure.com/msdata"
project_name = "Vienna"
team_name = "Agents Runtime (RAGandOYDtoo)"
# Create a connection to the organization
credentials = BasicAuthentication("", personal_access_token)
connection = Connection(base_url=organization_url, creds=credentials)

# Get the work item tracking client and core client
wit_client = connection.clients.get_work_item_tracking_client()
core_client = connection.clients.get_core_client()

# Get the project id
project_id = None
for project in core_client.get_projects():
    if project.name == project_name:
        project_id = project.id
        print(f"Found project {project_name}, with ID {project_id}")
        break

# Get the team id
team_id = None
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
            break
    if team_id:
        continue_search = False
if not team_id:
    raise Exception(f"Team {team_name} not found")

# Find the current sprint
current_sprint = None
team_context = TeamContext = TeamContext(project_id=project_id, team_id=team_id)
today = datetime.datetime.now(datetime.UTC)
# Get the work client
work_client = connection.clients.get_work_client()
for iteration in work_client.get_team_iterations(team_context):
    if iteration.attributes.start_date <= today <= iteration.attributes.finish_date:
        current_sprint = iteration.path
        break

if not current_sprint:
    raise Exception("Current sprint not found")

# Query to get all work items that need to be moved to the current sprint
assigned_to_string = f"('{"', '".join(assigned_to)}')"
work_item_type_string = f"('{"', '".join(work_item_types)}')"
work_item_state_string = f"('{"', '".join(work_item_state)}')"
query = f"""
SELECT [System.Id]
FROM WorkItems
WHERE [System.IterationPath] != '{current_sprint}'
AND [System.IterationPath] NOT UNDER '{exclude_area_path}'
AND [System.AreaPath] UNDER '{area_path}'
AND [System.AssignedTo] IN {assigned_to_string}
AND [System.WorkItemType] IN {work_item_type_string}
AND [System.State] NOT IN {work_item_state_string}
"""

wiql = {"query": query}
work_items = wit_client.query_by_wiql(wiql).work_items

# Move each work item to the current sprint
for work_item in work_items:
    work_item_id = work_item.id
    document = [
        {"op": "add", "path": "/fields/System.IterationPath", "value": current_sprint}
    ]
    print(f"Moving work item {work_item_id} to {current_sprint}")
    wit_client.update_work_item(document, work_item_id)

print(f"Moved {len(work_items)} work items to {current_sprint}")
