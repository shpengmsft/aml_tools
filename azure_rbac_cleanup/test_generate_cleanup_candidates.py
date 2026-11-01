import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from azure.mgmt.authorization.models import RoleAssignment, RoleDefinition

from generate_cleanup_candidates import GraphClient, generate_candidates, main, write_candidates

SUBSCRIPTION_ID = "test-subscription"
SUBSCRIPTION_SCOPE = f"/subscriptions/{SUBSCRIPTION_ID}"
ROLE_ID = f"{SUBSCRIPTION_SCOPE}/providers/Microsoft.Authorization/roleDefinitions/test-role"
RESOURCE_GROUP_SCOPE = f"{SUBSCRIPTION_SCOPE}/resourceGroups/rg"
RESOURCE_SCOPE = f"{RESOURCE_GROUP_SCOPE}/providers/Microsoft.Storage/storageAccounts/account"


def assignment(name, principal="user", principal_type="User", scope=SUBSCRIPTION_SCOPE, **kwargs):
    result = RoleAssignment(
        principal_id=principal, principal_type=principal_type, role_definition_id=ROLE_ID, **kwargs
    )
    # The SDK treats these server-returned properties as read-only constructor arguments.
    result.id = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
    result.name = name
    result.scope = scope
    return result


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.graph = Mock(spec=GraphClient)
        self.graph.get_principal_groups.return_value = {"group"}
        self.graph.get_group_info.side_effect = lambda group_id: {"displayName": f"Name of {group_id}"}
        self.graph.get_principal_info.side_effect = lambda principal_id, principal_type: {
            "displayName": principal_id,
            "servicePrincipalType": "ManagedIdentity" if principal_id == "mi" else "Application",
        }
        role = RoleDefinition(role_name="Test role")
        role.id = ROLE_ID
        self.roles = [role]

    def scan(self, assignments, scan_type="user"):
        return generate_candidates(SUBSCRIPTION_ID, assignments, self.roles, self.graph, scan_type)

    def test_default_is_user_only_and_selectors_separate_service_principals_and_identities(self):
        assignments = [
            assignment("group", "group", "Group"),
            assignment("user"),
            assignment("app", "app", "ServicePrincipal"),
            assignment("mi", "mi", "ServicePrincipal"),
        ]
        for selector, expected in [
            ("user", {"user"}), ("service-principal", {"app"}),
            ("managed-identity", {"mi"}), ("all", {"user", "app", "mi"}),
        ]:
            with self.subTest(selector=selector):
                rows = self.scan(assignments, selector)
                self.assertEqual({r["principal_id"] for r in rows}, expected)
        self.assertEqual({r["principal_id"] for r in self.scan(assignments)}, {"user"})

    def test_group_same_or_ancestor_scope_covers_but_descendant_and_sibling_do_not(self):
        target = assignment("direct", scope=RESOURCE_GROUP_SCOPE)
        for group_scope, expected in [
            (SUBSCRIPTION_SCOPE, 1), (RESOURCE_GROUP_SCOPE.upper() + "/", 1),
            (RESOURCE_SCOPE, 0), (RESOURCE_GROUP_SCOPE + "2", 0),
        ]:
            with self.subTest(group_scope=group_scope):
                rows = self.scan([target, assignment("group", "group", "Group", group_scope)])
                self.assertEqual(len(rows), expected)
                if rows:
                    self.assertEqual(rows[0]["duplicated_via_groups"], "group")
                    self.assertEqual(rows[0]["duplicated_via_groups_name"], "Name of group")
                    self.assertEqual(rows[0]["covering_assignment_scopes"], group_scope)

    def test_group_names_align_with_sorted_deduplicated_ids_and_survive_csv(self):
        self.graph.get_principal_groups.return_value = {"group-a", "group-b"}
        rows = self.scan([
            assignment("b", "group-b", "Group"),
            assignment("a", "group-a", "Group"),
            assignment("a-child", "group-a", "Group", RESOURCE_GROUP_SCOPE),
            assignment("direct", scope=RESOURCE_SCOPE),
        ])
        self.assertEqual(rows[0]["duplicated_via_groups"], "group-a;group-b")
        self.assertEqual(rows[0]["duplicated_via_groups_name"], "Name of group-a;Name of group-b")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            write_candidates(str(path), rows)
            with path.open() as fh:
                saved = list(csv.DictReader(fh))
            self.assertEqual(saved[0]["duplicated_via_groups_name"], rows[0]["duplicated_via_groups_name"])

    def test_missing_group_name_is_logged_and_keeps_its_position(self):
        self.graph.get_principal_groups.return_value = {"group-a", "group-b"}
        self.graph.get_group_info.side_effect = lambda group_id: (
            None if group_id == "group-a" else {"displayName": "Group B"}
        )
        with self.assertLogs("generate_cleanup_candidates", level="WARNING") as logs:
            rows = self.scan([
                assignment("a", "group-a", "Group"),
                assignment("b", "group-b", "Group"),
                assignment("direct"),
            ])
        self.assertEqual(rows[0]["duplicated_via_groups_name"], ";Group B")
        self.assertIn("group-a", logs.output[0])

    def test_same_principal_ancestor_and_equal_scope_keep_a_covering_assignment(self):
        for principal_type, principal in [("User", "user"), ("ServicePrincipal", "mi")]:
            with self.subTest(principal_type=principal_type):
                assignments = [
                    assignment("child", principal, principal_type, RESOURCE_SCOPE),
                    assignment("b", principal, principal_type),
                    assignment("a", principal, principal_type),
                ]
                rows = self.scan(assignments, "all")
                self.assertEqual({r["assignment_name"] for r in rows}, {"b", "child"})
                self.assertEqual(
                    {r["covering_assignment_ids"] for r in rows}, {assignments[2].id}
                )
                self.assertTrue(all(r["redundancy_reason"] == "same_principal_assignment" for r in rows))
                self.assertTrue(all(r["duplicated_via_groups_name"] == "" for r in rows))
                self.graph.get_principal_groups.assert_not_called()
                self.graph.get_group_info.assert_not_called()

    def test_same_principal_sibling_scopes_are_not_redundant(self):
        self.assertEqual(self.scan([
            assignment("first", scope=RESOURCE_GROUP_SCOPE),
            assignment("second", scope=RESOURCE_GROUP_SCOPE + "2"),
        ]), [])

    def test_group_covered_ancestor_is_not_used_as_a_retained_direct_cover(self):
        rows = self.scan([
            assignment("parent"),
            assignment("child", scope=RESOURCE_SCOPE),
            assignment("group", "group", "Group"),
        ])
        deleted_ids = {r["assignment_id"] for r in rows}
        covering_ids = {cid for r in rows for cid in r["covering_assignment_ids"].split(";")}
        self.assertTrue(deleted_ids.isdisjoint(covering_ids))
        self.assertEqual(len(rows), 2)

    def test_other_principals_roles_and_nonmembers_are_not_duplicates(self):
        other_role = assignment("other-role", "group", "Group")
        other_role.role_definition_id = ROLE_ID + "-other"
        self.graph.get_principal_groups.return_value = set()
        self.assertEqual(self.scan([
            assignment("user"), assignment("another", "another"),
            assignment("group", "group", "Group"), other_role,
        ]), [])

    def test_conditional_and_delegated_assignments_are_not_targets_or_covers(self):
        for kwargs in [
            {"condition": "restricted", "condition_version": "2.0"},
            {"delegated_managed_identity_resource_id": RESOURCE_SCOPE},
        ]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.scan([
                    assignment("group", "group", "Group", **kwargs), assignment("user"),
                ]), [])
                self.assertEqual(self.scan([
                    assignment("group", "group", "Group"), assignment("user", **kwargs),
                ]), [])

    def test_unknown_principal_is_resolved_without_treating_deleted_object_as_user(self):
        self.graph.get_directory_object.return_value = None
        self.assertEqual(self.scan([
            assignment("unknown", principal_type=None),
            assignment("group", "group", "Group"),
        ]), [])
        self.graph.get_directory_object.return_value = {"@odata.type": "#microsoft.graph.servicePrincipal"}
        self.assertEqual(len(self.scan([
            assignment("mi", "mi", None), assignment("group", "group", "Group"),
        ], "managed-identity")), 1)

    def test_graph_permission_failure_aborts_scan(self):
        self.graph.get_principal_groups.side_effect = requests.HTTPError("403 Forbidden")
        with self.assertRaises(requests.HTTPError):
            self.scan([assignment("group", "group", "Group"), assignment("user")])

    def test_empty_scan_replaces_stale_csv_with_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            path.write_text("stale candidate")
            write_candidates(str(path), [])
            with path.open() as fh:
                reader = csv.DictReader(fh)
                self.assertIn("assignment_id", reader.fieldnames)
                self.assertEqual(list(reader), [])


class GraphTests(unittest.TestCase):
    def test_service_principal_parent_pagination_nested_cycles_and_cache(self):
        graph = GraphClient(Mock())
        base = graph.base
        group_type = "#microsoft.graph.group"
        pages = {
            f"{base}/servicePrincipals/mi/memberOf": {
                "value": [{"id": "child", "@odata.type": group_type}],
                "@odata.nextLink": f"{base}/next",
            },
            f"{base}/next": {"value": [
                {"id": "mail", "@odata.type": group_type},
                {"id": "directory-role", "@odata.type": "#microsoft.graph.directoryRole"},
            ]},
            f"{base}/groups/child?$select=id,displayName,securityEnabled": {"securityEnabled": True},
            f"{base}/groups/parent?$select=id,displayName,securityEnabled": {"securityEnabled": True},
            f"{base}/groups/mail?$select=id,displayName,securityEnabled": {"securityEnabled": False},
            f"{base}/groups/child/memberOf": {"value": [{"id": "parent", "@odata.type": group_type}]},
            f"{base}/groups/parent/memberOf": {"value": [{"id": "child", "@odata.type": group_type}]},
        }
        with patch.object(graph, "_get", side_effect=lambda url: pages[url]) as get:
            self.assertEqual(graph.get_principal_groups("mi", "ServicePrincipal"), {"child", "parent"})
            count = get.call_count
            self.assertEqual(graph.get_principal_groups("mi", "ServicePrincipal"), {"child", "parent"})
            self.assertEqual(get.call_count, count)

    def test_http_failure_propagates_and_token_refreshes(self):
        credential = Mock()
        credential.get_token.return_value = Mock(token="test-token", expires_on=0)
        graph = GraphClient(credential)
        response = Mock(status_code=403, ok=False, text="Forbidden")
        response.raise_for_status.side_effect = requests.HTTPError("403")
        with patch("generate_cleanup_candidates.requests.get", return_value=response) as get:
            for _ in range(2):
                with self.assertRaises(requests.HTTPError):
                    graph._get(f"{graph.base}/users/user")
            self.assertEqual(credential.get_token.call_count, 2)
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")
            self.assertGreater(get.call_args.kwargs["timeout"], 0)


class CliTests(unittest.TestCase):
    def test_csv_defaults_to_dup_roles_and_accepts_override(self):
        for options, expected in [([], "dup_roles.csv"), (["--csv", "custom.csv"], "custom.csv")]:
            with self.subTest(options=options):
                client = Mock()
                client.role_definitions.list.return_value = []
                client.role_assignments.list_for_scope.return_value = []
                with (
                    patch("sys.argv", ["generate_cleanup_candidates.py", "--subscription-id", SUBSCRIPTION_ID, *options]),
                    patch("generate_cleanup_candidates.DefaultAzureCredential"),
                    patch("generate_cleanup_candidates.AuthorizationManagementClient", return_value=client),
                    patch("generate_cleanup_candidates.GraphClient"),
                    patch("generate_cleanup_candidates.write_candidates") as write,
                ):
                    main()
                write.assert_called_once_with(expected, [])


if __name__ == "__main__":
    unittest.main()
