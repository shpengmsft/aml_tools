# Azure RBAC duplicate-role cleanup helper scripts

This small toolkit helps you find and remove "duplicated" role assignments in an Azure subscription — specifically, users who have a direct role assignment while also receiving the same role through membership in a group (including nested groups).

Files
- `generate_cleanup_candidates.py` — scans role assignments in a subscription, expands group membership (recursively via Microsoft Graph), and emits a CSV of candidate role assignments that appear duplicated and may be safe to remove.
- `remove_role_assignments.py` — reads the CSV produced by the generator and deletes the listed role assignments. Runs in dry-run mode by default.
- `requirements.txt` — Python package dependencies used by the scripts.

Quick start

1) Install dependencies (PowerShell):

```powershell
python -m pip install -r q:\github\aml_tools\azure_rbac\requirements.txt
```

2) Authenticate
- Both scripts use `DefaultAzureCredential` from `azure-identity`. The easiest options are:
  - `az login` (Azure CLI) and use your logged-in user.
  - Use a service principal (client id/secret) or managed identity configured in your environment.

Important: the identity used must have permissions to list role assignments in the subscription and must be allowed to call Microsoft Graph to read groups and members (Directory Reader or equivalent). If Graph calls are unauthorized you will see errors or incomplete results.

Generate candidates (scan)

```powershell
python q:\github\aml_tools\azure_rbac\generate_cleanup_candidates.py --subscription-id "<SUBSCRIPTION_ID>" --csv dup_roles.csv
```

Options
- `--subscription-id` (required): the target subscription GUID.
- `--csv` (required): path to write the output CSV file.
- `--debug`: enable debug logging for troubleshooting.

What the generator does
- Loads role definitions and role assignments scoped to `/subscriptions/<id>`.
- For each role, it finds group principals and expands their members recursively (including nested groups) using Microsoft Graph.
- If a user appears as a direct assignment principal for the same role and is also a member of one or more groups that have that role, the direct user assignment is flagged as a candidate for removal and written to the CSV.

CSV format
The CSV contains these columns:
- `subscription_id`
- `role_name`
- `role_definition_id` (resource id)
- `assignment_id` (role assignment resource id; used for deletion)
- `assignment_name`
- `principal_type` (User/Group)
- `principal_id` (object id)
- `principal_displayName`
- `principal_userPrincipalName`
- `assignment_scope`
- `duplicated_via_groups` (semicolon-separated group ids that provide the role)

Review the CSV carefully before deleting anything.

Remove candidates (dry-run by default)

```powershell
# Dry-run (default): prints actions
python q:\github\aml_tools\azure_rbac\remove_role_assignments.py --csv dup_roles.csv

# To actually delete (be careful):
python q:\github\aml_tools\azure_rbac\remove_role_assignments.py --csv dup_roles.csv --no-dry-run
```

Notes about the remover
- The script expects an `assignment_id` column in the CSV containing the full role assignment resource id (the SDK's `delete_by_id` is used).
- The default is a safe dry-run; `--no-dry-run` performs deletions.

Permissions and Graph caveats
- Enumerating group members uses Microsoft Graph. The scanning identity must have access to Graph (`Directory.Read.All` or be a Directory Reader / Global Reader) or be a user that can see group membership.
- Some role assignment principals may reference objects that are not resolvable by Graph (deleted objects, cross-tenant/foreign objects, service principals). The generator will handle 404s gracefully and will skip expansion for those objects.

Safety checklist before deleting
1. Review `dup_roles.csv` and confirm the `duplicated_via_groups` value(s) — ensure the group indeed grants the role.
2. Check assignment `scope` — some assignments are scoped beyond subscription (resource groups / resources); ensure your deletion target is correct.
3. Run `remove_role_assignments.py` in dry-run and inspect logs.
4. When ready, run with `--no-dry-run` and consider running in a maintenance window.

Recommended small improvements (optional)
- Add an extra safety `--confirm` flag to require the user to pass a confirmation string before actual deletion.
- Summarize counts per role before deleting (helpful preview).

If you want, I can add a README-based sample `dup_roles.csv` snippet or add the `--confirm` option to `remove_role_assignments.py` now.
