## What this changes

<!-- One or two sentences. If it changes behaviour, name the design-notes section it touches. -->

## Why

<!-- The problem, not the patch. -->

## Checklist

- [ ] `pytest` is green (and new behaviour has a test that fails without the change)
- [ ] `python scripts/check_component.py` is green if the component changed
- [ ] Nothing new can fail a pipeline, block a merge or delay a deployment
- [ ] `docs/design.md` updated if behaviour changed
- [ ] `CHANGELOG.md` updated under `Unreleased` if the change is user-visible
