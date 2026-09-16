import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.importers.mediacrawler import (
    MAX_BYTES,
    ImportProblem,
    ImportRequest,
    ParsedImport,
    parse_export,
)
from backend.services.import_service import commit_import, preview

router = APIRouter(prefix="/api/imports/mediacrawler", tags=["imports"])


async def read_import(request: Request) -> ParsedImport:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2 * MAX_BYTES:
            raise HTTPException(413, "Dữ liệu gửi lên quá lớn; file tối đa 1 MB.")
    try:
        return parse_export(ImportRequest.model_validate(json.loads(body)))
    except (ImportProblem, ValidationError, ValueError, RecursionError):
        raise HTTPException(
            422, "File không hợp lệ. Chọn JSON/JSONL content, tối đa 1 MB và 200 bản ghi."
        ) from None


@router.post("/preview")
async def preview_import(request: Request, db: Session = Depends(get_db)):
    parsed = await read_import(request)
    return preview(parsed, db)


@router.post("/commit", status_code=201)
async def import_export(request: Request, db: Session = Depends(get_db)):
    parsed = await read_import(request)
    if not parsed.items:
        raise HTTPException(
            422, "Không có video hợp lệ để nhập; hãy xem danh sách bản ghi bị bỏ qua."
        )
    try:
        return commit_import(parsed, db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Dữ liệu vừa thay đổi. Hãy xem trước lại rồi nhập.") from None
