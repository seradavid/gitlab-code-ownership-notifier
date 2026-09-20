"""Ownership notification engine (see docs/design.md).

The design this implements:

* Ownership lives in each repo's ``.gitlab/CODEOWNERS`` (read from the MR's target branch).
* Only metadata that CODEOWNERS cannot express lives in the central ``teams.yml``
  (channel, dashboard label, target-branch scope, rosters, knobs, ignore lists).
* Two jobs drive everything, both riding pipelines that already exist:
  ``ownership-mr-check`` (last stage of the MR pipeline) and
  ``ownership-merge-audit`` (first, non-blocking stage of the post-merge pipeline).
"""

__version__ = "0.1.0"
