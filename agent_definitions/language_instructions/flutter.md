# Flutter / Dart language conventions

The authoritative Dart conventions live at the
[Dart style guide](https://dart.dev/guides/language/effective-dart/style).
This file covers only mill-specific operational content for Flutter repos.

## Dart SDK version alignment (critical)

Flutter dependency resolution is **SDK-sensitive** — picking the latest
package version without checking the SDK constraint will produce a build
that fails in CI.  The implement agent MUST detect and align with the
Dart SDK version the target repo's CI actually uses.

### How to detect the CI Dart SDK version

Check these sources, in priority order:

1. **GitHub Actions workflow** (`.github/workflows/`) — look for
   `flutter-version:` or `dart-version:` in the setup step, or a
   `subosito/flutter-action` with a pinned version.
2. **FVM config** (`.fvmrc` or `.fvm/fvm_config.json`) — Flutter
   Version Management pin file; the `flutterSdkVersion` field is the
   exact SDK the team uses.
3. **Dockerfile / docker-compose** — `FROM ghcr.io/cirruslabs/flutter:stable`
   or `FROM instrumentisto/flutter:3.x.y` pins the SDK.
4. **`pubspec.yaml` `environment.sdk` constraint** — the lower bound
   (e.g. `>=3.5.0 <4.0.0`) tells you the minimum SDK the project
   supports.  Use the lower bound as the target, not the upper.
5. **CI lockfile** (`pubspec.lock`) — the `dart` and `flutter` entries
   record the exact SDK that last resolved successfully.

If none of these resolve, ask the operator — do NOT guess.

### Constraining dependency resolution

When adding or updating dependencies in `pubspec.yaml`:

1. Always check the package's `environment.sdk` constraint against the
   detected CI SDK version.  A package that requires `sdk: '>=3.10.0'`
   will NOT build on CI running Dart 3.5.x.
2. Use `flutter pub add <pkg>` when available — it resolves
   version constraints automatically.  When editing `pubspec.yaml`
   manually, pin to a version known to support the CI SDK.
3. If a dependency requires a newer SDK than CI provides, pin to an
   older compatible version.  Do NOT widen the repo's SDK constraint
   to accommodate one dependency — that is a team decision.
4. After editing `pubspec.yaml`, run `flutter pub get` to regenerate
   `pubspec.lock`.  Commit both files together.

## Local verification

Before committing, run:

```bash
flutter analyze
flutter test
```

These are the same commands CI runs.  Fix all analyzer warnings — CI
may treat warnings as errors depending on `analysis_options.yaml`.

If the sandbox does not have the Flutter SDK installed, skip local
verification and rely on the CI-fix agent's iteration budget to catch
remaining issues.  Note this in your summary.

## Common CI failure patterns

- **SDK version mismatch** — `pubspec.yaml` `environment.sdk` is wider
  than what CI provides.  Tighten the constraint to match CI.
- **Dependency resolution failure** — package requires newer SDK.  Pin
  to an older version compatible with the CI SDK.
- **Analyzer errors** — unused imports, missing types, deprecated API.
  Run `flutter analyze` and fix every reported issue.
- **Test failures** — widget tests need `testWidgets`, unit tests use
  `test`.  Ensure `WidgetsFlutterBinding.ensureInitialized()` is called
  in widget test setup when needed.
- **Build failures** — missing platform-specific configuration
  (`android/app/build.gradle`, `ios/Runner.xcodeproj`).  Check that
  generated files are committed if the repo requires them.

## Sandbox constraints

The Flutter SDK is typically NOT available in the mill sandbox.  The
implement agent cannot run `flutter analyze` or `flutter test` locally.
Instead:

1. Write code that follows the patterns already present in the target
   repo (imports, test structure, widget patterns).
2. Rely on the CI-fix agent's iteration budget to catch and fix any
   remaining analyze/test failures after CI runs.
3. When the CI-fix agent reports failures, read the CI logs carefully
   and make minimal, targeted fixes — do not rewrite large sections.
