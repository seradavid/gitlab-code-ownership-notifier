"""Module identity resolution for the optional ``module_overlay`` (§7.1).

The same walk-up logic serves a local checkout and the ``GIT_STRATEGY: none``
component, where identity files are fetched through the API instead.
"""

from __future__ import annotations

from ownership_bot import identity

POM_WITH_PROJECT_GROUP = """
<project>
  <groupId>com.acme</groupId>
  <artifactId>payment-core</artifactId>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>not-this-one</artifactId>
    </dependency>
  </dependencies>
</project>
"""

POM_WITH_PARENT_GROUP = """
<project>
  <parent>
    <groupId>com.acme</groupId>
    <artifactId>parent</artifactId>
  </parent>
  <artifactId>child</artifactId>
</project>
"""


def test_pom_identity_uses_the_project_group_id_and_artifact_id():
    assert identity.pom_identity(POM_WITH_PROJECT_GROUP) == "com.acme:payment-core"


def test_pom_identity_falls_back_to_the_parent_group_id():
    assert identity.pom_identity(POM_WITH_PARENT_GROUP) == "com.acme:child"


def test_pom_identity_ignores_group_ids_inside_dependency_blocks():
    # The only <groupId> in this pom is inside <dependencies>, so there is no identity.
    text = """
    <project>
      <artifactId>lonely</artifactId>
      <dependencies>
        <dependency><groupId>org.example</groupId><artifactId>x</artifactId></dependency>
      </dependencies>
    </project>
    """
    assert identity.pom_identity(text) is None


def test_package_identity_reads_the_name():
    assert identity.package_identity('{"name": "@acme/payment-core"}') == "@acme/payment-core"
    assert identity.package_identity('{"private": true}') is None
    assert identity.package_identity("not json") is None


def test_find_identity_walks_up_to_the_nearest_manifest(tmp_path):
    module = tmp_path / "libs" / "payment-core"
    (module / "src").mkdir(parents=True)
    (module / "pom.xml").write_text(POM_WITH_PROJECT_GROUP, encoding="utf-8")

    assert identity.find_identity(tmp_path, "libs/payment-core/src/A.java") == ("com.acme:payment-core")


def test_find_identity_rejects_paths_that_escape_the_root(tmp_path):
    assert identity.find_identity(tmp_path, "../outside/A.java") is None


def test_resolver_prefers_a_local_checkout_before_the_api(tmp_path):
    local = tmp_path / "libs" / "thing"
    local.mkdir(parents=True)
    (local / "package.json").write_text('{"name": "local-name"}', encoding="utf-8")

    calls: list[str] = []

    def remote(path: str) -> str | None:
        calls.append(path)
        return '{"name": "remote-name"}'

    resolve = identity.identity_resolver(tmp_path, remote=remote)
    assert resolve is not None
    assert resolve("libs/thing/src/A.java") == "local-name"
    assert calls == []  # the local hit ends the search before the API


def test_resolver_falls_back_to_the_api_without_a_checkout(tmp_path):
    """The component has ``GIT_STRATEGY: none``: no files exist locally."""

    def remote(path: str) -> str | None:
        if path == "libs/payment-core/package.json":
            return '{"name": "payment-core"}'
        return None

    resolve = identity.identity_resolver(tmp_path, remote=remote)
    assert resolve is not None
    assert resolve("libs/payment-core/src/A.java") == "payment-core"


def test_resolver_caches_and_tolerates_a_failing_reader(tmp_path):
    calls: list[str] = []

    def remote(path: str) -> str | None:
        calls.append(path)
        if path.endswith("pom.xml"):
            raise RuntimeError("boom")
        return None

    resolve = identity.identity_resolver(tmp_path, remote=remote)
    assert resolve is not None
    assert resolve("libs/a/A.java") is None
    assert resolve("libs/a/B.java") is None

    # Every probed path was read at most once, despite two files sharing ancestors.
    assert len(calls) == len(set(calls))


def test_resolver_is_none_when_there_is_nothing_to_read():
    assert identity.identity_resolver(None) is None


def test_filesystem_reader_treats_a_directory_as_a_miss(tmp_path):
    (tmp_path / "pkg" / "pom.xml").mkdir(parents=True)  # a directory, not a file
    assert identity.find_identity(tmp_path, "pkg/A.java") is None
