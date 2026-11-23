# AGENTS.md

Project conventions for automated agents working in this repository (tg-dl).

## Release rule — every push ships a version

**Any push to GitHub must ship a new version, in the same push.** Do not push
plain `master` commits without a release. On every push:

1. Bump `__version__` in [`utils/__init__.py`](utils/__init__.py). It is the
   single source of truth for the image tag, the git tag and the GitHub Release.
   Follow the rules written in that file: MAJOR = breaking change or core
   rewrite, MINOR = new user-facing capability, PATCH = fixes, dependencies,
   docs or UI polish.
2. Add a matching `## vX.Y.Z` section at the top of [`CHANGELOG.md`](CHANGELOG.md).
   The section body becomes the GitHub Release notes, so it must not be empty.
3. Update the version shown at the top of [`README.md`](README.md) and, when the
   user-visible history matters, its `<details>` changelog block.
4. Commit the code changes together with the version bump.
5. Tag and push both:
   ```bash
   git tag vX.Y.Z
   git push origin master vX.Y.Z
   ```

The tag **must** equal `v${__version__}`. An existing tag or Release for that
version fails the workflow on purpose: versions are immutable, so bump instead
of force-repushing a tag.

A branch-only push (no tag) produces no versioned image and no Release — treat
it as an incomplete release.

## Verification before pushing

Run all of these and require zero failures:

```bash
python -m compileall -q media_downloader.py module utils scripts tests
ruff check --select E9,F media_downloader.py module utils scripts tests
pytest -q tests/
```

`preview/build.py` regenerates the static Web UI preview; run it after touching
`module/static/**`, `module/templates/**` or `config_form.FIELDS`, and keep
`preview/build.py::FORM_VALUES` covering every field in `config_form.FIELDS`
(there is a test for this).

## Toolchain notes

- Lint/format is `ruff` (`ruff check`, `ruff format`); types are `mypy`; tests
  are `pytest`. `make style_check` runs lint + mypy.
- Business logging uses `loguru`. Standard-library `logging` remains only for
  intercepting `werkzeug` / `pyrogram` output, so do not "migrate" those.
- Config keys that need a restart are listed in `Application.reload_hot_config`;
  when adding a key, update that method *and* the "配置热更新说明" section in
  `README.md`.
