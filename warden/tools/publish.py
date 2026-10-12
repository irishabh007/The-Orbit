"""Publish approved bundles into the generated repo as one commit per feature.

Called after a run succeeds (every gate, policy check and approval passed), never by
an agent: a discarded or rejected attempt can therefore never touch the repo.
Rolling a feature back is ``GitRepo.revert(commit)``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from warden.artifacts import ArtifactStore
from warden.tools.git import GitRepo
from warden.tools.workspace import collect_bundles, write_files


@dataclass(frozen=True)
class PublishResult:
    commit: str | None  # None when the approved content was already in the repo
    files: list[str]


def publish(store: ArtifactStore, bundles: Sequence[str], repo: GitRepo, message: str) -> PublishResult:
    files = collect_bundles(store, bundles)
    repo.init()
    written = sorted(write_files(repo.root, files))
    return PublishResult(repo.commit_all(message), written)
