# Policy on the plan JSON (report section 8.1 Layer 5, "after plan"; IMP-25). TEMPLATE: read and run it with
#   conftest test --policy .claude/templates/infra/policy --namespace kit.infra.plan plan.json
# where plan.json is `terraform show -json tfplan`. It could not be run by the kit's offline tests (no conftest,
# no opa), so run `opa test .claude/templates/infra/policy` or `conftest verify` once yourself before you trust it.
#
# The source policy (.claude/scripts/infra/source_policy.py) already refused the dangerous constructs before the plan
# ran. This is the second layer, on what the plan would change. `deny` fails the run; `warn` is shown to the approver.
# It is a starting point, not a complete policy: extend the lists for the services you use.
package kit.infra.plan

import rego.v1

# Stateful resources: deleting or replacing one loses data. Changing them is a separate, human-reviewed change.
protected_types := {
	"aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance", "aws_dynamodb_table", "aws_s3_bucket",
	"aws_efs_file_system", "aws_kms_key", "aws_ebs_volume", "aws_elasticache_replication_group",
	"aws_secretsmanager_secret", "aws_opensearch_domain", "google_sql_database_instance", "google_storage_bucket",
	"azurerm_mssql_database", "azurerm_storage_account", "kubernetes_persistent_volume_claim",
}

iam_policy_types := {"aws_iam_policy", "aws_iam_role_policy", "aws_iam_user_policy", "aws_iam_group_policy"}

banned_data_types := {"external", "http"}

# --- deny ---------------------------------------------------------------------------------------------------------

deny contains msg if {
	some rc in input.resource_changes
	rc.type in protected_types
	"delete" in rc.change.actions
	msg := sprintf("%s: deleting or replacing a stateful resource (%s) needs its own human-approved change", [rc.address, rc.type])
}

deny contains msg if {
	some rc in input.resource_changes
	rc.type in iam_policy_types
	is_string(rc.change.after.policy)
	doc := json.unmarshal(rc.change.after.policy)
	some st in statements(doc)
	st.Effect == "Allow"
	has_star(st.Action)
	has_star(st.Resource)
	msg := sprintf("%s: IAM policy allows every action on every resource", [rc.address])
}

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_iam_role"
	is_string(rc.change.after.assume_role_policy)
	regex.match(`repo:[^"]*\*`, rc.change.after.assume_role_policy)
	msg := sprintf("%s: trust policy matches a wildcard repository subject (repo:*): pin it to an environment or a workflow", [rc.address])
}

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_iam_role"
	is_string(rc.change.after.assume_role_policy)
	regex.match(`"(AWS|Federated)"\s*:\s*"\*"`, rc.change.after.assume_role_policy)
	msg := sprintf("%s: trust policy allows any principal", [rc.address])
}

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_security_group_rule"
	rc.change.after.type == "ingress"
	"0.0.0.0/0" in rc.change.after.cidr_blocks
	not rc.change.after.from_port in {80, 443}
	msg := sprintf("%s: ingress from the whole internet on port %v", [rc.address, rc.change.after.from_port])
}

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_s3_bucket_acl"
	rc.change.after.acl in {"public-read", "public-read-write", "authenticated-read"}
	msg := sprintf("%s: public bucket ACL %s", [rc.address, rc.change.after.acl])
}

# A second layer behind the source policy: banned data sources and provisioners seen in the plan's configuration.
deny contains msg if {
	some res in config_resources
	res.mode == "data"
	res.type in banned_data_types
	msg := sprintf("%s: data source %s is not allowed", [res.address, res.type])
}

deny contains msg if {
	some res in config_resources
	count(object.get(res, "provisioners", [])) > 0
	msg := sprintf("%s: provisioners are not allowed", [res.address])
}

# --- warn ---------------------------------------------------------------------------------------------------------

warn contains msg if {
	some rc in input.resource_changes
	"delete" in rc.change.actions
	not rc.type in protected_types
	msg := sprintf("%s: will be deleted or replaced", [rc.address])
}

# --- helpers ------------------------------------------------------------------------------------------------------

statements(doc) := doc.Statement if is_array(doc.Statement)

statements(doc) := [doc.Statement] if is_object(doc.Statement)

has_star(x) if x == "*"

has_star(x) if {
	is_array(x)
	"*" in x
}

# Every resource in the root module and in every module call below it. (Rego has no recursion, so walk the tree.)
config_resources contains res if {
	walk(input.configuration, [_, node])
	is_object(node)
	some res in object.get(node, "resources", [])
	is_object(res)
}
