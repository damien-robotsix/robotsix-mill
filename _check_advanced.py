import sys

sys.path.insert(0, "/repo")

# Simulate the schema generation
from robotsix_mill.config.repos import ReposRegistry
from robotsix_mill.config.secrets import Secrets
from robotsix_mill.config.settings import Settings

settings_schema = Settings.model_json_schema()
# Check a few fields have "advanced": true
props = settings_schema.get("properties", {})
advanced_fields = sorted([k for k, v in props.items() if v.get("advanced") is True])
print(f"Settings advanced fields count: {len(advanced_fields)}")
print(f"All advanced fields: {advanced_fields}")

# Check a field that should NOT be advanced
non_advanced = [
    "branch_prefix",
    "api_host",
    "api_port",
    "api_url",
    "data_dir",
    "forge_kind",
    "forge_remote_url",
    "forge_target_branch",
    "github_api_url",
    "gitlab_api_url",
    "sandbox_image",
    "sandbox_proxy_url",
    "test_command",
    "smoke_command",
    "skills_dir",
    "language_instructions_dir",
]
for f in non_advanced:
    if f in props:
        assert props[f].get("advanced") is not True, f"{f} should NOT be advanced"  # noqa: S101
print("Non-advanced check passed")

# Check repos schema for advanced fields
repos_schema = ReposRegistry.model_json_schema()
repo_props = repos_schema.get("$defs", {}).get("RepoConfig", {}).get("properties", {})
repo_advanced = sorted([k for k, v in repo_props.items() if v.get("advanced") is True])
print(f"RepoConfig advanced fields: {repo_advanced}")

# Verify secrets have NO advanced flag
secrets_schema = Secrets.model_json_schema()
secrets_props = secrets_schema.get("properties", {})
secrets_advanced = [k for k, v in secrets_props.items() if v.get("advanced") is True]
print(f"Secrets advanced fields: {secrets_advanced}")
assert len(secrets_advanced) == 0, "Secrets should never be advanced!"  # noqa: S101

print("\nAll checks passed!")
