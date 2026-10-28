"""Git Smart HTTP, served on the port Prism already listens on.

    git clone https://prism.example.com/git/prj_abc.git

Why here rather than sshd or a separate git server: a client reaching a repo then goes
through the *same* auth as everything else in Prism. A parallel SSH key system is one
nobody remembers to revoke, and it would put access control somewhere Prism cannot see.

The implementation shells out to `git http-backend`, git's own CGI program. We are not
reimplementing the pack protocol; we are speaking CGI to the thing that already
implements it, correctly, including all the negotiation subtleties that make hand-rolled
git servers subtly wrong.

Two things git clients do that shape this file:

  * They authenticate with **HTTP Basic**, not Bearer. Git has no notion of a bearer
    token, so we accept a Prism API token as the Basic *password* and bridge it.
  * They expect a **401 with WWW-Authenticate** to know they should prompt for
    credentials. A 403 makes git give up without asking.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import subprocess

from fastapi import APIRouter, HTTPException, Request, Response

from app.api._helpers import get_project_for_role_or_404
from app.core.config import settings
from app.core import policy
from app.core.security import AuthenticatedUser, _resolve_bearer_user, guest_user
from app.services import git_host_service

# Note: the whole pack is buffered in memory rather than streamed. A board repo is tens
# of megabytes, which is fine; a repo in the gigabytes would want streaming, and that
# has its own CGI subtleties worth doing separately rather than half-doing here.

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

# The only paths git actually asks for. Anything else is not a git client and has no
# business being routed into http-backend.
_GIT_PATHS = ("/info/refs", "/git-upload-pack", "/git-receive-pack")

# Reading = clone/fetch. Writing = push. Split so a viewer can clone but not push.
_WRITE_PATHS = ("/git-receive-pack",)


def _unauthorised() -> HTTPException:
    """A 401 that makes git PROMPT rather than give up.

    Without WWW-Authenticate, git treats the failure as final and never offers
    credentials, which looks to the user like "the repo doesn't exist".
    """
    return HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Prism"'},
    )


def _authenticate(request: Request) -> AuthenticatedUser:
    """Who is this git client?

    Git speaks Basic auth, so a Prism API token arrives as the password. Username is
    ignored: tokens identify themselves, and demanding a matching username would just
    be a second thing to get wrong.
    """
    if not settings.AUTH_ENABLED:
        return guest_user()

    header = request.headers.get("authorization") or ""
    scheme, _, encoded = header.partition(" ")

    if scheme.casefold() != "basic" or not encoded.strip():
        raise _unauthorised()

    try:
        decoded = base64.b64decode(encoded.strip()).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _unauthorised() from exc

    _, _, token = decoded.partition(":")
    if not token:
        raise _unauthorised()

    try:
        return _resolve_bearer_user(token)
    except HTTPException as exc:
        # 401 so git prompts again rather than treating a bad token as final.
        raise _unauthorised() from exc


def _authorise(user: AuthenticatedUser, project_id: str, path: str) -> None:
    """Reading needs viewer. Writing needs designer. Either needs the project visible.

    Visibility is the same folder check every /api/projects/{id} route applies, so
    hiding a folder from a role also stops that role cloning and pushing its repos.
    A hidden or unknown project is a 404, as it is in the API.
    """
    writing = any(path.endswith(p) for p in _WRITE_PATHS)
    # Tokens carry scopes as well as the user's role: a push needs design:write, a clone api:read.
    policy.authorize(user, "design" if writing else "read")
    get_project_for_role_or_404(project_id, user.role)


def _split(full_path: str) -> tuple[str, str]:
    """Split `prj_abc.git/info/refs` into the project id and the git sub-path."""
    for suffix in _GIT_PATHS:
        if full_path.endswith(suffix):
            repo = full_path[: -len(suffix)]
            break
    else:
        raise HTTPException(status_code=404, detail="Not found")

    repo = repo.strip("/")
    if not repo.endswith(".git"):
        raise HTTPException(status_code=404, detail="Not found")
    return repo[: -len(".git")], suffix


@router.api_route("/{full_path:path}", methods=["GET", "POST"])
async def git_http(full_path: str, request: Request):
    """Hand the request to `git http-backend` and stream its answer back."""
    project_id, sub_path = _split(full_path)

    user = _authenticate(request)
    await asyncio.to_thread(_authorise, user, project_id, sub_path)

    if not git_host_service.exists(project_id):
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.body()

    # CGI. http-backend reads the request out of the environment and stdin, and writes
    # a CGI response (headers, blank line, body) to stdout.
    env = git_host_service.http_backend_env(
        project_id,
        {
            "REQUEST_METHOD": request.method,
            "PATH_INFO": f"/{project_id}.git{sub_path}",
            "QUERY_STRING": request.url.query,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": user.email,
            "REMOTE_ADDR": request.client.host if request.client else "",
            # git sends packs gzipped; http-backend needs to be told so it inflates.
            "HTTP_CONTENT_ENCODING": request.headers.get("content-encoding", ""),
            # Without this, http-backend serves the dumb protocol, which is far slower
            # and does not support push at all.
            "GIT_PROTOCOL": request.headers.get("git-protocol", ""),
        },
    )

    stdout, stderr, code = await asyncio.to_thread(_run_backend, env, body)
    if code != 0:
        logger.error(
            "git http-backend failed for %s%s: %s",
            project_id,
            sub_path,
            (stderr or b"").decode("utf-8", "replace")[:500],
        )
        raise HTTPException(status_code=500, detail="git failed")

    headers, payload = _parse_cgi(stdout)
    status = int(headers.pop("Status", "200 OK").split()[0])

    return Response(
        content=payload,
        status_code=status,
        headers=headers,
        media_type=headers.pop("Content-Type", "application/octet-stream"),
    )


def _run_backend(env: dict, body: bytes) -> tuple[bytes, bytes, int]:
    proc = subprocess.run(
        ["git", "http-backend"],
        input=body,
        capture_output=True,
        env=env,
        timeout=git_host_service.GIT_TIMEOUT,
        check=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _parse_cgi(raw: bytes) -> tuple[dict, bytes]:
    """Split a CGI response into headers and body.

    The separator is a blank line. Splitting on the FIRST one matters: a pack body is
    binary and will contain \\r\\n\\r\\n sequences of its own, and splitting on a later
    one would truncate the pack and hand git a corrupt response.
    """
    marker = b"\r\n\r\n"
    index = raw.find(marker)
    if index == -1:
        marker = b"\n\n"
        index = raw.find(marker)
    if index == -1:
        return {}, raw

    head = raw[:index].decode("latin-1")
    body = raw[index + len(marker) :]

    headers: dict[str, str] = {}
    for line in head.splitlines():
        name, _, value = line.partition(":")
        if value:
            headers[name.strip()] = value.strip()
    return headers, body
