"""Signed, session-less access to one comment attachment.

Forges that cannot host uploads (GitHub) render issue images from these URLs,
fetched by the forge's image proxy with no Prism session. The HMAC binds the
project, the attachment and an expiry, so a URL opens exactly one file until
it expires; there is nothing to enumerate and no project lookup to leak.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services import comment_attachments
from app.services.comments_store_service import comments_store
from app.services.trackers import attachment_links

router = APIRouter()


@router.get(
    attachment_links.PUBLIC_PATH + "/{project_id}/{attachment_id}",
    responses={
        200: {
            "description": "The attachment file, in the media type it was stored with.",
            "content": {"*/*": {}},
        }
    },
    summary="Open one comment attachment through its signed link",
)
async def get_signed_comment_attachment(project_id: str, attachment_id: str, exp: int = 0, sig: str = ""):
    if not attachment_links.verify_signature(project_id, attachment_id, exp, sig):
        raise HTTPException(status_code=404, detail="Not found")
    row = await asyncio.to_thread(comments_store.get_attachment, project_id, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    path = comment_attachments.blob_path(row["sha256"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = str(row["media_type"])
    return FileResponse(
        path,
        media_type=media_type,
        filename=row["filename"],
        content_disposition_type="inline" if media_type.startswith("image/") else "attachment",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Referrer-Policy": "no-referrer",
        },
    )
