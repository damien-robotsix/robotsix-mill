# robotsix-yaml-config

Backend-agnostic primitives for a layered YAML configuration cascade:

1. Start from code-defined defaults.
2. Deep-merge one or more YAML config files in precedence order.
3. Overlay environment variables field-by-field (highest priority).
4. Flatten the nested dict into flat keyword arguments via a
   dotted-path alias map.

The package operates on plain dicts only — no pydantic, no
pydantic-settings — so it can be consumed by any configuration backend.

## Public API

| Symbol | Behaviour |
|---|---|
| `YamlConfigError(Exception)` | Raised for missing required files, YAML parse errors, non-dict top-level mappings. |
| `deep_merge(base, overlay) -> dict` | Recursively merge `overlay` into `base` (mutates `base`). Scalars overwrite; nested dicts recurse; lists/other are replaced wholesale via `deepcopy`. Returns `base`. |
| `read_yaml_file(path) -> dict` | Read & parse one YAML file. Missing file → `{}`. Parse error or non-dict top level → `YamlConfigError`. |
| `load_yaml_cascade(layers) -> dict` | Load & deep-merge `(path, required)` layers in order. A required-but-missing layer raises `YamlConfigError`. Later layers win. |
| `flatten_config(nested, alias_map) -> dict` | Walk a nested dict, map each dotted path through `alias_map`, return a flat `{alias: value}` dict. Unknown paths dropped; dict-valued aliases emitted as-is. |
| `overlay_env_vars(config, prefix, type_hints=None) -> dict` | Overlay `{PREFIX}_{KEY.upper()}` env vars onto existing keys with type coercion. Mutates and returns `config`. |

### `overlay_env_vars` coercion

`type_hints.get(key)` selects the coercion (default `str`):

- `str` → value unchanged
- `int` → `int(value)`
- `float` → `float(value)`
- `bool` → case-insensitive: `{"1","true","yes","on"}` → `True`,
  `{"0","false","no","off",""}` → `False`. (A raw `bool(value)` is
  wrong because `bool("false")` is `True`.)

## Development

```sh
pip install -e '.[dev]'
pytest
```
