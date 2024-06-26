# Azure Machine Learning Tools

## disable_account_key_for_datastores.py v 0.2

### Description

This script will disable account key access to storage account for all datastores in the specified workspace(s) in the specified resource group(s).

The script will then update the datastore to use the managed identity (MSI) of the workspace, also grant MSI the 'Storage Blob Data Contributor' role to that storage account.

### Install required packages

```cmd
pip install azure-ai-ml azure-core azure-identity azure-mgmt-authorization azure-mgmt-resource azure-mgmt-storage azureml-core setuptools
```

### Usage

```cmd
python disable_account_key_for_datastores.py  -s SUBSCRIPTION_ID [-r RESOURCE_GROUP] [-w WORKSPACE_NAME] [-log LOGLEVEL] [-h]
```

### Known issue

Datastore client will show following WARNING message when try to enable MSI access to the storage account, it is expected.

"WARNING:azureml.data.datastore_client:You do not have permissions to check whether the Workspace Managed Identity has access to the Storage Account....We will try to grant Reader and Storage Blob Data Reader role to the Workspace Managed Identity for the storage account."

### Update History

v 0.2 - set workspace's "systemDatastoresAuthMode" to "identity".

## storage_account_with_account_key_enabled.py v 0.1

### Description:

This script lists all storage accounts in a subscription or a resource group that allow shared key access.

### Usage

```cmd
python storage_account_with_account_key_enabled.py -s <subscription_id> [-r <resource_group>] [-a] [-l <loglevel>]
```
