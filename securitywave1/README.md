# Azure Machine Learning Tools

## disable_account_key_for_datastores.py v 0.1

Description:

This script will disable account key access to storage account for all datastores in the specified workspace(s) in the specified resource group(s).

The script will then update the datastore to use the managed identity (MSI) of the workspace, also grant MSI the 'Storage Blob Data Contributor' role to that storage account.

Install required packages:

```cmd
pip install azure-core azure-identity azure-mgmt-authorization azure-mgmt-resource azure-mgmt-storage azureml-core
```

Usage:

```cmd
python disable_account_key_for_datastores.py.py  -s SUBSCRIPTION_ID [-r RESOURCE_GROUP] [-w WORKSPACE_NAME] [-log LOGLEVEL] [-h]
```

Known issue:

Datastore client will show following WARNING message when try to enable MSI access to the storage account, it is expected.

"WARNING:azureml.data.datastore_client:You do not have permissions to check whether the Workspace Managed Identity has access to the Storage Account....We will try to grant Reader and Storage Blob Data Reader role to the Workspace Managed Identity for the storage account."
