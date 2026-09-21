"""Guards for the failure modes that only show up inside GitLab or GitHub.

The motivating bug: an unquoted YAML scalar containing ``": "`` parses as a mapping, so
GitLab rejected the whole pipeline with *"script config should be a string or a nested
array of strings"* while the file looked perfectly fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_component import check_component, check_pipeline

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every CI file this repository ships. If one of them is broken, a push publishes
#: a red pipeline to the world before anyone notices.
CI_FILES = (
    ".gitlab-ci.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "templates/ownership-notify/template.yml",
)


def test_an_unquoted_colon_in_a_script_line_is_reported():
    text = """
    job:
      script:
        - echo "Consumers reference it as:"
        - echo "  include: component host/path/name@1.0.0"
    """

    problems = check_pipeline(text)

    assert len(problems) == 1
    assert "script[1]" in problems[0]
    assert "quote the whole line" in problems[0]


def test_quoting_the_whole_line_is_accepted():
    text = """
    job:
      script:
        - 'echo "  include: component host/path/name@1.0.0"'
    """

    assert check_pipeline(text) == []


def test_nested_arrays_of_strings_are_accepted():
    """GitLab allows a script entry to be a nested array (up to 10 levels)."""
    text = """
    job:
      script:
        - - echo one
          - echo two
        - echo three
    """

    assert check_pipeline(text) == []


def test_a_non_string_step_in_any_nested_position_is_reported():
    text = """
    job:
      script:
        - - [echo fine, {broken: value}]
    """

    problems = check_pipeline(text)

    assert len(problems) == 1
    assert "script[0][0][1]" in problems[0]


def test_before_script_and_after_script_are_checked_too():
    text = """
    job:
      before_script:
        - echo "step: broken"
      after_script:
        - echo fine
    """

    assert len(check_pipeline(text)) == 1


def test_github_run_steps_are_checked():
    text = """
    jobs:
      build:
        steps:
          - name: fine
            run: echo hello
          - name: broken
            run: echo "  note: this becomes a mapping"
    """

    problems = check_pipeline(text)

    assert len(problems) == 1
    assert "run" in problems[0]


def test_invalid_yaml_is_reported():
    problems = check_pipeline("job: [unclosed\n")

    assert len(problems) == 1
    assert problems[0].startswith("invalid YAML: ")


def test_a_pipeline_is_not_treated_as_a_component():
    """Only the component validator cares about ``spec.inputs``."""
    assert check_component("job:\n  script: echo hi\n") == []


@pytest.mark.parametrize("relative", CI_FILES)
def test_this_repository_ships_valid_ci_files(relative):
    """The regression guard: the real files must stay clean."""
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")

    assert check_pipeline(text) == []
    assert check_component(text) == []
