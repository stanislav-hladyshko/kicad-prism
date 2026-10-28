"""Pin a design query to a commit (ADR scripting-and-mcp §3.3).

Every design read takes a revision: ``None`` for the project's current head, a
full 40-hex commit id, or a branch or tag name. A full id is already immutable
and is returned lowercased without asking Git. Anything else goes through
``project_revision.resolve_commit``, the one place that turns a ref into a
commit in the project's repository, and an unknown name is a ``ValueError``
(the API answers 400 ``revision-unknown``).

The resolved commit is what a response states and what a caller cites; the
bundle's revision key travels beside it as ``designKey``. Neither is derived
here: the commit comes from Git, the design key from the bundle read.
"""

from __future__ import annotations

import re
from typing import Any

from app.services import project_revision

#: A full Git object id. Only this shape is immutable; a short id or a ref can
#: move, so both are resolved every time.
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def is_full_sha(revision: str | None) -> bool:
    """Whether ``revision`` is a full 40-hex commit id (case-insensitive)."""

    return bool(revision and _FULL_SHA.match(revision))


def resolve(project: Any, revision: str | None) -> str:
    """The full commit id a design query at ``revision`` reads.

    ``None`` is the checkout's ``HEAD`` (the same default the semantic-index
    reader uses). A full id is lowercased and returned as is. A branch or tag
    is resolved against the project's repository; an unknown one raises
    ``ValueError`` so the route can answer 400 ``revision-unknown``.
    """

    if revision is None:
        location = project_revision.locate(project)
        return project_revision.resolve_commit(location, "HEAD")
    text = revision.strip()
    if is_full_sha(text):
        return text.lower()
    location = project_revision.locate(project)
    try:
        return project_revision.resolve_commit(location, text)
    except project_revision.RevisionNotFound as error:
        raise ValueError(f"Unknown revision: {revision}") from error
