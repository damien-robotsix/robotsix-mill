# Python module shadowing: subdirectory alongside single-file module

## Hazard

Python's import system resolves `import foo.bar` by looking for a
`foo/` directory containing an `__init__.py`. If your repo already has a
single-file module `foo.py` at the same level, creating a `foo/`
directory **shadows** it — `import foo` then finds the directory
instead of `foo.py`, and the original module becomes unreachable.

This is a hard break: Python will not fall back to `foo.py` once a
`foo/` directory exists on `sys.path`.

## Rule

Before proposing a new file under a directory that matches an existing
single-file module name, check whether that directory is already a
**package** (contains `__init__.py`).

- **If the directory IS already a package** (has `__init__.py`):
  the new file can safely live inside it (e.g. `foo/_helper.py`).

- **If the directory does NOT exist** and would shadow a single-file
  module (`foo.py`): place the new file **alongside** the module
  instead, using an underscored prefix to namespace it:
  `_foo_helper.py` rather than `foo/_helper.py`.

## Example

In `src/robotsix_mill/forge/`:

- `gitlab/` **is** a package (contains `__init__.py`), so
  `gitlab/_pagination.py` is safe.

- `github.py` is a single-file module (the main `GitHubForge` class
  lives there). `github/` does **not** exist. Creating
  `github/_pagination.py` would shadow `github.py`. The correct path
  is `_github_pagination.py` — placed alongside `github.py` rather
  than inside a new `github/` directory.

## Converting a single-file module to a package

If the new file genuinely belongs under the module's namespace and
the scope justifies it, you can convert `foo.py` to `foo/__init__.py`
(moving the content of `foo.py` into `foo/__init__.py`). This is a
larger change that touches every import of `foo` and should be its own
ticket — do not bundle it with the addition of the new helper file.
