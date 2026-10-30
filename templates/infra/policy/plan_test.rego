# Tests for plan.rego. Run: opa test .claude/templates/infra/policy   (or: conftest verify --policy .claude/templates/infra/policy)
# Not run by the kit's offline suite (no opa in it). Written for OPA 0.59 or newer.
package kit.infra.plan

import rego.v1

test_deleting_a_database_is_denied if {
	count(deny) == 1 with input as {"resource_changes": [{"address": "aws_db_instance.main", "type": "aws_db_instance", "change": {"actions": ["delete", "create"], "after": {}}}]}
}

test_creating_a_database_is_allowed if {
	count(deny) == 0 with input as {"resource_changes": [{"address": "aws_db_instance.main", "type": "aws_db_instance", "change": {"actions": ["create"], "after": {}}}]}
}

test_star_star_policy_is_denied if {
	count(deny) == 1 with input as {"resource_changes": [{
		"address": "aws_iam_policy.admin", "type": "aws_iam_policy",
		"change": {"actions": ["create"], "after": {"policy": "{\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"*\",\"Resource\":\"*\"}]}"}},
	}]}
}

test_scoped_policy_is_allowed if {
	count(deny) == 0 with input as {"resource_changes": [{
		"address": "aws_iam_policy.read", "type": "aws_iam_policy",
		"change": {"actions": ["create"], "after": {"policy": "{\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::b/*\"}]}"}},
	}]}
}

test_wildcard_repo_subject_is_denied if {
	count(deny) >= 1 with input as {"resource_changes": [{
		"address": "aws_iam_role.ci", "type": "aws_iam_role",
		"change": {"actions": ["create"], "after": {"assume_role_policy": "{\"Condition\":{\"StringLike\":{\"sub\":\"repo:acme/widgets:*\"}}}"}},
	}]}
}

test_open_ssh_is_denied if {
	count(deny) == 1 with input as {"resource_changes": [{
		"address": "aws_security_group_rule.ssh", "type": "aws_security_group_rule",
		"change": {"actions": ["create"], "after": {"type": "ingress", "cidr_blocks": ["0.0.0.0/0"], "from_port": 22}},
	}]}
}

test_open_https_is_allowed if {
	count(deny) == 0 with input as {"resource_changes": [{
		"address": "aws_security_group_rule.web", "type": "aws_security_group_rule",
		"change": {"actions": ["create"], "after": {"type": "ingress", "cidr_blocks": ["0.0.0.0/0"], "from_port": 443}},
	}]}
}

test_external_data_in_configuration_is_denied if {
	count(deny) == 1 with input as {
		"resource_changes": [],
		"configuration": {"root_module": {"resources": [{"address": "data.external.x", "mode": "data", "type": "external"}]}},
	}
}

test_provisioner_in_configuration_is_denied if {
	count(deny) == 1 with input as {
		"resource_changes": [],
		"configuration": {"root_module": {"resources": [{"address": "aws_instance.x", "mode": "managed", "type": "aws_instance", "provisioners": [{"type": "local-exec"}]}]}},
	}
}
