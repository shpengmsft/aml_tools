# Azure RBAC duplicate-role cleanup helper scripts

This toolkit helps reduce subscription role-assignment usage by finding redundant direct assignments for users, service principals, and managed identities. An assignment is a candidate only when the same role is already provided at the same scope or an ancestor scope through security-group membership (including nested groups) or a retained assignment to the same principal.

Files
- `generate_cleanup_candidates.py` — scans role assignments in a subscription and emits a reviewable CSV of redundant assignments for users, service principals, or managed identities.
- `remove_role_assignments.py` — reads the CSV produced by the generator and deletes the listed role assignments. Runs in dry-run mode by default.

Quick start

1) Create or reuse the repository virtual environment and install dependencies (Linux):

```bash
python3 -m venv ~/src/aml_tools/.venv  # only if it does not already exist
source ~/src/aml_tools/.venv/bin/activate
~/src/aml_tools/.venv/bin/python -m pip install azure-identity azure-mgmt-authorization requests
```

Run the following commands from the directory containing the **updated** scripts:

```bash
cd ~/src/aml_tools/azure_rbac_cleanup
```

If developing in a feature worktree, use that worktree's `azure_rbac_cleanup` directory instead. Activating the shared `.venv` does not select which copy of the script runs. Use `deactivate` to leave the virtual environment.

2) Authenticate
- Both scripts use `DefaultAzureCredential` from `azure-identity`. The easiest options are:
  - `az login` (Azure CLI) and use your logged-in user.
  - Use a service principal (client id/secret) or managed identity configured in your environment.

```bash
az --version
az login
```

Important: the identity used must have permissions to list role assignments in the subscription and must be allowed to call Microsoft Graph to read principal details and memberships. Authentication and Graph authorization failures abort the scan; an existing CSV is not a result of a failed run.

Generate candidates (scan)

```bash
# Users only; writes dup_roles.csv in the current directory.
python generate_cleanup_candidates.py --subscription-id "<SUBSCRIPTION_ID>"

# Managed identities only.
python generate_cleanup_candidates.py --subscription-id "<SUBSCRIPTION_ID>" --principal-type managed-identity

# Other service principals (excludes managed identities).
python generate_cleanup_candidates.py --subscription-id "<SUBSCRIPTION_ID>" --principal-type service-principal

# All supported principal categories, with a custom output file.
python generate_cleanup_candidates.py --subscription-id "<SUBSCRIPTION_ID>" --principal-type all --csv all_candidates.csv
```

Options
- `--subscription-id` (required): the target subscription GUID.
- `--csv` (optional): output CSV path; defaults to `dup_roles.csv` in the current working directory.
- `--debug`: enable debug logging for troubleshooting.
- `--principal-type`: `user` (default), `service-principal` (excludes managed identities), `managed-identity`, or `all`.

Use ordinary spaces in commands. Pass values such as `managed-identity` without braces: `{user,service-principal,managed-identity,all}` in `--help` lists the allowed choices, not literal syntax.

What the generator does
- Loads role definitions and role assignments scoped to `/subscriptions/<id>`.
- For the selected principal category, it checks whether the same role is already granted through security-group membership (including nested groups) or a retained assignment to the same principal.
- A covering assignment must be at the same scope or an ancestor scope. Sibling scopes and narrower scopes cannot justify removal.
- Conditional and delegated assignments are conservatively excluded as both candidates and covering grants.
- Managed identities have RBAC principal type `ServicePrincipal`; Graph's `servicePrincipalType` distinguishes them from other service principals.
- Membership is traversed through `users/{id}/memberOf`, `servicePrincipals/{id}/memberOf`, and parent groups, with pagination, cycle detection, and caching. This avoids the Graph v1.0 `groups/{id}/members` known omission of service principals.
- Every candidate lists covering assignments that are **not** themselves candidates. Equal-scope direct duplicates retain one deterministic assignment.
- Only assignments within the target subscription are candidates or covering grants. Management-group grants and equivalence across different role definitions are not analyzed.
- Graph authorization/network failures abort the scan rather than producing a success-shaped incomplete result. Deleted/unresolved objects are logged and skipped. Hidden memberships require appropriate Graph permissions and can otherwise be missed.
- Successful scans always write a CSV, even with zero candidates, replacing stale output. A failed scan does not replace an existing CSV; do not mistake that old file for a new result.

CSV format
The CSV contains these columns:
- `subscription_id`
- `role_name`
- `role_definition_id` (resource id)
- `assignment_id` (role assignment resource id; used for deletion)
- `assignment_name`
- `principal_type` (`User` or `ServicePrincipal`; managed identities use `ServicePrincipal`)
- `principal_id` (object id)
- `principal_displayName`
- `principal_userPrincipalName`
- `assignment_scope`
- `duplicated_via_groups` (semicolon-separated group ids that provide the role)
- `duplicated_via_groups_name` (semicolon-separated group display names, in the same order as `duplicated_via_groups`; empty for same-principal-only redundancy). Names reuse cached Graph lookups. Unavailable names are logged and retain an empty position.
- `service_principal_type` (`ManagedIdentity`, `Application`, etc.; empty for users)
- `redundancy_reason` (`group_membership` or `same_principal_assignment`)
- `covering_assignment_ids` and `covering_assignment_scopes` (semicolon-separated, in corresponding order)

The existing remover accepts the extended CSV without changes. Review the CSV carefully before deleting anything, and revalidate covering assignments and memberships before deletion: access can change after the scan.

Review the CSV in WSL

```bash
# Open the current directory's CSV in VS Code.
code dup_roles.csv

# Open the current directory in Windows Explorer, then open the CSV in Excel.
explorer.exe .

# Check the actual on-disk column names.
head -n 1 dup_roles.csv | tr ',' '\n'
```

`duplicated_via_groups_name` is column L (12), immediately after `duplicated_via_groups`. Scroll right to see it. After changing the generator, rerun the scan to update existing CSVs. If an editor or Excel still shows the old header, close the old view without saving and reopen the CSV from the directory where the scan ran.

Remove candidates (dry-run by default)

```bash
# Dry-run (default): prints actions
python remove_role_assignments.py --csv dup_roles.csv

# To actually delete (be careful):
python remove_role_assignments.py --csv dup_roles.csv --no-dry-run
```

Notes about the remover
- The script expects an `assignment_id` column in the CSV containing the full role assignment resource id (the SDK's `delete_by_id` is used).
- The default is a safe dry-run; `--no-dry-run` performs deletions.

Permissions and Graph caveats
- Reading memberships and principal details uses Microsoft Graph. `Directory.Read.All` covers these operations; narrower grants can use `User.Read.All`, `Application.Read.All`, and group-read permissions as appropriate. Hidden memberships additionally require `Member.Read.Hidden`. Azure role-assignment read permissions are also required.
- Some role assignment principals may reference objects that are not resolvable by Graph (deleted or cross-tenant/foreign objects). The generator logs 404s and skips unavailable membership information.

Safety checklist before deleting
1. Review `dup_roles.csv` and confirm the `duplicated_via_groups` value(s) — ensure the group indeed grants the role.
2. Check assignment `scope` — some assignments are scoped beyond subscription (resource groups / resources); ensure your deletion target is correct.
3. Run `remove_role_assignments.py` in dry-run and inspect logs.
4. When ready, run with `--no-dry-run` and consider running in a maintenance window.

Tests (from the repository/worktree root; no Azure calls):

```bash
~/src/aml_tools/.venv/bin/python -m unittest discover -s azure_rbac_cleanup -p 'test_*.py'
```
