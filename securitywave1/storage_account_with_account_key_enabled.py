# Description: This script lists all storage accounts in a subscription or a resource group that allow shared key access.
# Usage: python storage_account_with_account_key_enabled.py -s <subscription_id> [-r <resource_group>] [-a] [-l <loglevel>]

# version: 0.1.0

import argparse
import logging
from collections import namedtuple
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.storage import StorageManagementClient

StorageAccount = namedtuple(
    "StorageAccount",
    ["name", "resource_group", "subscription_id", "allow_shared_key_access"],
)

current_file_name = Path(__file__).name

logger = logging.getLogger(current_file_name)


def _is_shared_key_access_allowed(
    subscription_id, resource_group_name, storage_account_name
):
    logger.info(
        f"Getting shared key access setting for storage account {storage_account_name} in resource group {resource_group_name}"
    )

    storage_client = StorageManagementClient(DefaultAzureCredential(), subscription_id)

    storage_properties = storage_client.storage_accounts.get_properties(
        resource_group_name, storage_account_name
    )

    return storage_properties.allow_shared_key_access


def list_by_resource_group(subscription_id, resource_group_name):
    logger.info(f"Listing storage accounts in resource group {resource_group_name}")

    storage_accounts = []
    storage_client = StorageManagementClient(DefaultAzureCredential(), subscription_id)

    for storage_account in storage_client.storage_accounts.list_by_resource_group(
        resource_group_name
    ):
        allow_shared_key_access = _is_shared_key_access_allowed(
            subscription_id, resource_group_name, storage_account.name
        )
        storage_accounts.append(
            StorageAccount(
                storage_account.name,
                resource_group_name,
                subscription_id,
                allow_shared_key_access,
            )
        )
    return storage_accounts


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

    total_storage_accounts_with_shared_key_access = 0
    for resource_group_name in resource_groups:
        storage_accounts = list_by_resource_group(
            args.subscription_id, resource_group_name
        )
        if len(storage_accounts) != 0:
            count = len([s for s in storage_accounts if s.allow_shared_key_access])
            total_storage_accounts_with_shared_key_access += count
            print(
                f"=== {count} Storage account(s) in resource group '{resource_group_name}' allow shared key access ==="
            )
        for storage_account in storage_accounts:
            if args.all or storage_account.allow_shared_key_access:
                print(
                    f"  Storage account: {storage_account.name} \tAllow shared key access: {storage_account.allow_shared_key_access}"
                )
    # Print the total number of storage accounts with shared key access
    print("\n" + "=" * 80)
    print(f"Total number of storage accounts with shared key access: {total_storage_accounts_with_shared_key_access}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--subscription_id", type=str, required=True)
    parser.add_argument("-r", "--resource_group", type=str, default=None)
    parser.add_argument(
        "-a", "--all", action="store_true", help="List all storage accounts"
    )
    parser.add_argument(
        "-l", "--loglevel", default="WARNING", help="Set the logging level"
    )
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel.upper()))

    logger.info("\n".join(f"{k}={v}" for k, v in vars(args).items()))

    if args.subscription_id is None:
        raise ValueError("Subscription ID must be provided.")

    main(args)
