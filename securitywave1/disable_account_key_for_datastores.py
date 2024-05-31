# Description:
#            This script will disable account key access to storage account
#            for all datastores in the specified workspace(s) in the specified resource group(s).

#            The script will then update the datastore to use the managed identity (MSI) of the workspace.
#            also grant MSI the 'Storage Blob Data Contributor' role to that storage account.

# Usage:
#       python storage_account_migrate.py  -s SUBSCRIPTION_ID [-r RESOURCE_GROUP] [-w WORKSPACE_NAME] [-log LOGLEVEL] [-h]

# version: 0.1.0

import argparse
import logging
import uuid
from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.storage.models import StorageAccountUpdateParameters
from azureml.core.datastore import Datastore
from azureml.core.workspace import Workspace

current_file_name = Path(__file__).name

logger = logging.getLogger(current_file_name)


def _turn_off_shared_key_access(
    subscription_id, resource_group_name, storage_account_name
):
    logger.info(
        f"Disabling shared key access for storage account {storage_account_name} in resource group {resource_group_name}"
    )

    storage_client = StorageManagementClient(DefaultAzureCredential(), subscription_id)

    storage_properties = storage_client.storage_accounts.get_properties(
        resource_group_name, storage_account_name
    )

    if storage_properties.allow_shared_key_access is False:
        logger.info(
            "Shared key access is already disabled for this storage account. No action needed."
        )
        return

    parameters = StorageAccountUpdateParameters(allow_shared_key_access=False)

    storage_client.storage_accounts.update(
        resource_group_name, storage_account_name, parameters
    )


def _get_workspace_system_assigned_principal_id(workspace):
    # Get the workspace details
    workspace_details = workspace.get_details()
    # The system-assigned managed identity is included in the details
    identity = workspace_details.get("identity", None)
    if identity and identity.get("type") == "SystemAssigned":
        principal_id = identity.get("principal_id")
        logger.info(f"Workspace {workspace.name}'s MSI: {principal_id}")
    else:
        raise ValueError(
            f"No system-assigned managed identity found for Workspace {workspace.name}."
        )
    return principal_id


def _grant_workspace_msi_access_to_storage(
    identity_principal_id, subscription_id, resource_group_name, storage_account_name
):
    storage_client = StorageManagementClient(DefaultAzureCredential(), subscription_id)

    storage_properties = storage_client.storage_accounts.get_properties(
        resource_group_name, storage_account_name
    )

    # Storage Blob Data Contributor role definition ID
    storage_blob_data_contributor_id = f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"

    # Create role assignment parameters
    role_params = RoleAssignmentCreateParameters(
        role_definition_id=storage_blob_data_contributor_id,
        principal_id=identity_principal_id,
        principal_type="ServicePrincipal",
    )

    # Assign the role to the managed identity
    scope = storage_properties.id

    # https://learn.microsoft.com/en-us/python/api/azure-mgmt-authorization/azure.mgmt.authorization.v2022_04_01.operations.roleassignmentsoperations?view=azure-python#azure-mgmt-authorization-v2022-04-01-operations-roleassignmentsoperations-create
    auth_client = AuthorizationManagementClient(
        DefaultAzureCredential(), subscription_id
    )
    try:
        # Initialize the AuthorizationManagement client
        role_assignment = auth_client.role_assignments.create(
            scope, uuid.uuid4(), parameters=role_params
        )
        logger.info(
            f"Granting workspace 'Storage Blob Data Contributor' access to storage account {role_assignment}"
        )
    except ResourceExistsError:
        logger.info(
            f"Role assignment already exists for workspace MSI on storage account {storage_account_name}"
        )
    except Exception as e:
        logger.error(
            f"Error granting workspace 'Storage Blob Data Contributor' access to storage account: {e}"
        )
        raise


def migrate_by_workspace(subscription_id, resource_group_name, workspace_name):
    try:
        ws = Workspace.get(
            name=workspace_name,
            subscription_id=subscription_id,
            resource_group=resource_group_name,
        )
    except Exception as e:
        logger.error(
            f"Error getting workspace {workspace_name} in resource group {resource_group_name}: {e}"
        )
        return

    logger.info(f"== ResourceGroup:'{resource_group_name}' Workspace '{ws.name}' ==")
    for datastore_name in ws.datastores:
        datastore = Datastore.get(ws, datastore_name)
        if datastore.datastore_type == "AzureBlob":
            if datastore.credential_type != "AccountKey":
                message = f"Datastore '{datastore_name}' in workspace '{ws.name}' is not using account key. Skipping."
                logger.info(message)
                print(message)
                continue

            # get storage account
            storage_client = StorageManagementClient(
                DefaultAzureCredential(), subscription_id
            )
            storage_accounts = storage_client.storage_accounts.list()

            storage_account = next(
                (sa for sa in storage_accounts if sa.name == datastore.account_name),
                None,
            )

            if storage_account is None:
                message = (
                    f"Storage account '{datastore.account_name}' not found. Skipping."
                )
                logger.error(message)
                print(message)
                continue
            # get resource group name from storage account id
            storage_account_resource_group = storage_account.id.split("/")[4]

            # disable the shared key
            _turn_off_shared_key_access(
                subscription_id, storage_account_resource_group, storage_account.name
            )

            try:
                # The workspace system-assigned managed identity is included in the details
                workspace_msi = _get_workspace_system_assigned_principal_id(ws)
                _grant_workspace_msi_access_to_storage(
                    workspace_msi,
                    subscription_id,
                    storage_account_resource_group,
                    storage_account.name,
                )
            except Exception as e:
                message = f"Fail to grant workspace the 'Storage Blob Data Contributor' access to storage account: {e}. Skipping."
                logger.error(message)
                print(message)
                continue

            # update the datastore to use the MSI
            Datastore.register_azure_blob_container(
                workspace=ws,
                datastore_name=datastore.name,
                container_name=datastore.container_name,
                account_name=datastore.account_name,
                sas_token=None,
                account_key=None,
                protocol=datastore.protocol,
                endpoint=datastore.endpoint,
                overwrite=True,
                create_if_not_exists=False,
                skip_validation=False,
                blob_cache_timeout=None,
                grant_workspace_access=True,
                subscription_id=subscription_id,
                resource_group=storage_account_resource_group,
            )

            # log the migration
            message = f"Datastore '{datastore_name}' migrated to MSI-based access."
            logger.info(message)
        else:
            message = f"Datastore '{datastore.name}' in workspace '{ws.name}' is {datastore.datastore_type}. Skipping."
            logger.info(message)
        print(message)


def migrate_by_resource_group(subscription_id, resource_group_name, workspace_name):
    # get all workspace in this resource group
    workspace_names = []
    if workspace_name is None:
        workspace_names = Workspace.list(
            subscription_id=subscription_id, resource_group=resource_group_name
        )
    else:
        workspace_names.append(workspace_name)

    for workspace_name in workspace_names:
        message = f"\n==== ResourceGroup:'{resource_group_name}' Workspace:'{workspace_name}' ===="
        logger.info(message)
        print(message)
        migrate_by_workspace(subscription_id, resource_group_name, workspace_name)


def main(args):
    # Acquire a credential object using default credentials
    credential = DefaultAzureCredential()

    # Obtain the management object for resources
    resource_groups = []
    if args.resource_group is None:
        resource_client = ResourceManagementClient(credential, args.subscription_id)
        # Retrieve the list of resource groups
        resource_groups = [rg.name for rg in resource_client.resource_groups.list()]
    else:
        resource_groups.append(args.resource_group)

    for resource_group_name in resource_groups:
        migrate_by_resource_group(
            args.subscription_id, resource_group_name, args.workspace_name
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--subscription_id", type=str, required=True)
    parser.add_argument("-r", "--resource_group", type=str, default=None)
    parser.add_argument("-w", "--workspace_name", type=str, default=None)
    parser.add_argument(
        "-log", "--loglevel", default="WARNING", help="Set the logging level"
    )
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel.upper()))

    logger.info("\n".join(f"{k}={v}" for k, v in vars(args).items()))

    if args.subscription_id is None:
        raise ValueError("Subscription ID must be provided.")

    main(args)
