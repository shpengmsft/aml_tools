import argparse
import json
import os

from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication

# Retrieve the PAT from the environment variable
personal_access_token = os.getenv("AZURE_DEVOPS_PAT")
if personal_access_token is None:
    print("Please set AZURE_DEVOPS_PAT in your environment variable")
    exit()

# Azure DevOps org URL and Asset Release/Build definition ID
organization_url = "https://dev.azure.com/msdata"
Project_Name = "Vienna"
AzureML_Assets_Release_Definition_ID = 2346
AzureML_Assets_Build_Definition_ID = 25490

Last_N_Releases = 1

# Create a connection to the org
credentials = BasicAuthentication("", personal_access_token)
connection = Connection(base_url=organization_url, creds=credentials)


def get_last_n_releases(args):
    asset_releases = []

    # Get a release client
    release_client = connection.clients.get_release_client()
    max_created_time = None

    # Get a build client
    build_client = connection.clients.get_build_client()

    while True:
        print(">", end="", flush=True)

        releases = release_client.get_releases(
            project=Project_Name,
            definition_id=AzureML_Assets_Release_Definition_ID,
            top=10,
            max_created_time=max_created_time,
        )

        if releases is not None:
            for release in releases:
                rls_item = {}
                rls = release_client.get_release(
                    project=Project_Name, release_id=release.id
                )
                rls_item["release_name"] = rls.name
                rls_item["release_time"] = rls.created_on
                max_created_time = (
                    rls.created_on
                    if max_created_time is None or rls.created_on < max_created_time
                    else max_created_time
                )
                rls_item["release_status"] = rls.status
                rls_item["release_created_by"] = rls.created_by.display_name
                rls_item["release_description"] = rls.description
                rls_item["release_url"] = (
                    f"https://msdata.visualstudio.com/Vienna/_releaseProgress?_a=release-pipeline-progress&releaseId={release.id}"
                )
                artifact = rls.artifacts[0]
                rls_build_id = artifact.definition_reference["version"].id
                build = build_client.get_build(
                    project=Project_Name, build_id=rls_build_id
                )
                queue_time_variables = json.loads(build.parameters)
                rls_item["build_version"] = build.build_number
                rls_item["build_pattern"] = queue_time_variables["pattern"]
                if (
                    args.pattern is None
                    or args.pattern.lower() in rls_item["build_pattern"].lower()
                ):
                    asset_releases.append(rls_item)
                if len(asset_releases) >= args.number:
                    print("")
                    return asset_releases[: args.number]
        else:
            break
    print("")
    return asset_releases[: args.number]


def main():
    global Last_N_Releases
    # Initialize parser
    parser = argparse.ArgumentParser(
        description="This is quick script to get the latest release of AzureML Assets."
    )

    # Adding optional argument
    parser.add_argument(
        "-v", "--verbose", help="Show verbose output", action="store_true"
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        default=Last_N_Releases,
        help=f"Last n releases, default is {Last_N_Releases}",
    )

    parser.add_argument(
        "-p",
        "--pattern",
        type=str,
        help="Filter by build pattern, default is None.",
    )

    # Read arguments from command line
    args = parser.parse_args()

    if args.verbose:
        print("Verbose output is turned on")

    if args.number:
        message = f"Searching for the last {args.number} asset releases"
        if args.pattern:
            message += f" with build pattern '{args.pattern}':"
        print(message)

    asset_releases = get_last_n_releases(args)

    for rls in asset_releases:
        print(f"Release Name: {rls['release_name']}")
        print(f"Build Pattern: {rls['build_pattern']}")
        print(f"Create Time: {rls['release_time'].strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Created By: {rls['release_created_by']}")
        if args.verbose:
            print(f"Release Description: {rls['release_description']}")
            print(f"Release Status: {rls['release_status']}")
            print(f"Release URL: {rls['release_url']}")
        print("")


if __name__ == "__main__":
    main()
