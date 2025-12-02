import argparse
import logging
import csv
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_subscription_id_from_scope(scope):
    if not scope:
        return None
    parts = scope.split('/')
    # Scope usually starts with /subscriptions/{id}/...
    # parts = ['', 'subscriptions', '{id}', ...]
    if len(parts) > 2 and parts[1].lower() == 'subscriptions':
        return parts[2]
    return None

def main():
    parser = argparse.ArgumentParser(description="Delete Azure Role Assignments listed in a CSV file.")
    parser.add_argument("--input-csv", required=True, help="Path to input CSV file containing role assignments to delete.")
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="Execute the deletions. Default is dry-run mode.")
    parser.set_defaults(dry_run=True)
    
    args = parser.parse_args()
    input_csv = args.input_csv
    dry_run = args.dry_run

    assignments_to_delete = []
    
    try:
        with open(input_csv, mode='r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                assignments_to_delete.append(row)
    except Exception as e:
        logger.error(f"Failed to read CSV file {input_csv}: {e}")
        return

    logger.info(f"Found {len(assignments_to_delete)} assignments to process from CSV.")
    
    if not assignments_to_delete:
        logger.info("No assignments to process.")
        return

    # Extract subscription ID from the first assignment
    first_scope = assignments_to_delete[0].get('Scope')
    subscription_id = get_subscription_id_from_scope(first_scope)
    
    if not subscription_id:
        logger.error("Could not determine Subscription ID from the first record in CSV. Please ensure 'Scope' column exists and contains a valid Azure scope starting with /subscriptions/{id}.")
        return

    logger.info(f"Detected Subscription ID: {subscription_id}")

    credential = DefaultAzureCredential()
    
    logger.info(f"Authenticating and connecting to subscription {subscription_id}...")
    auth_client = AuthorizationManagementClient(credential, subscription_id)
    
    if dry_run:
        logger.info("Dry run enabled. No assignments will be deleted.")
        for row in assignments_to_delete:
            logger.info(f"[Dry Run] Would delete assignment {row.get('RoleAssignmentName')} ('{row.get('RoleName')}') on scope {row.get('Scope')} for user {row.get('PrincipalName')} ({row.get('PrincipalId')})")
    else:
        logger.info("Deleting assignments...")
        for row in assignments_to_delete:
            ra_name = row.get('RoleAssignmentName')
            scope = row.get('Scope')
            principal_name = row.get('PrincipalName')
            principal_id = row.get('PrincipalId')
            role_name = row.get('RoleName')
            
            if not ra_name or not scope:
                logger.warning(f"Skipping row due to missing RoleAssignmentName or Scope: {row}")
                continue
                
            try:
                logger.info(f"Deleting assignment {ra_name} ('{role_name}') on scope {scope} for user {principal_name} ({principal_id})...")
                auth_client.role_assignments.delete(scope=scope, role_assignment_name=ra_name)
                logger.info("Deleted.")
            except Exception as e:
                logger.error(f"Failed to delete assignment {ra_name}: {e}")

if __name__ == "__main__":
    main()
