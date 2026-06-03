# robotsix-modules

Shared JSON-Schema-driven module-taxonomy validation for robotsix projects.

A *module taxonomy* is a structured inventory of the logical modules in a
repository — their descriptions, the file globs they own, and the
dependency relationships between them. It lives in a `docs/modules.yaml`
file. This package bundles the canonical JSON Schema (draft 2020-12) for
that file and ships a small CLI + Python API to validate it.

## Install

```console
pip install "robotsix-modules @ git+https://github.com/damien-robotsix/robotsix-modules@v0.1.0"
```

## CLI

```console
$ robotsix-modules validate docs/modules.yaml   # exit 0 when valid, empty output
$ robotsix-modules validate broken.yaml          # prints "<pointer>: <message>" lines to stderr; exit 1
$ robotsix-modules validate missing.yaml         # "robotsix-modules: error: file not found: ..."; exit 2
$ robotsix-modules --version                     # robotsix-modules 0.1.0
```

Exit codes: `0` = valid, `1` = validation errors, `2` = file/parse
errors. All diagnostics go to stderr; stdout stays empty on success.

Pass `--schema <path>` to `validate` to override the bundled schema.

For pre-commit, use the wrapper entry point, which accepts one or more
positional paths (pre-commit passes each matched file separately):

```yaml
  - repo: local
    hooks:
      - id: validate-module-taxonomy
        name: Validate module taxonomy
        entry: robotsix-modules-validate
        language: system
        files: ^docs/modules\.ya?ml$
        types_or: [yaml]
```

## Python API

```python
from robotsix_modules import validate, validate_file, load_taxonomy, SCHEMA_PATH

errors = validate({"modules": [{"id": "foo", "description": "x", "paths": ["src/foo.py"]}]})
assert errors == []

errors = validate({})
assert errors  # ["modules: 'modules' is a required property"]

errors = validate_file("docs/modules.yaml")
```

- `load_taxonomy(path)` — load a `modules.yaml` and return a dict.
- `validate(taxonomy, *, schema=None)` — validate a dict; returns a list
  of human-readable error strings (empty = valid).
- `validate_file(path, *, schema_path=None)` — `load_taxonomy` then
  `validate`.
- `SCHEMA_PATH` — `pathlib.Path` to the bundled schema.

## License

MIT
