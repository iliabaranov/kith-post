"""Address-book routes: manage the reusable contact list (G-AB)."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from kith.config import get_settings
from kith.services import contacts as book
from kith.web.deps import get_db, load_user, templates

router = APIRouter()


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(
    request: Request, db: Session = Depends(get_db), added: int = 0, skipped: int = 0
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    ctx = {
        "settings": get_settings(), "user": user,
        "contacts": book.list_contacts(db, user.id),
        "all_groups": book.all_groups(db, user.id),
        "added": added, "skipped": skipped,
    }
    return templates.TemplateResponse(request, "contacts.html", ctx)


@router.post("/contacts/add")
def add_one(
    request: Request, db: Session = Depends(get_db),
    name: str = Form(""), email: str = Form(""), groups: str = Form(""),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    contact, created = book.add_contact(
        db, user.id, email, name.strip() or None, groups=book.parse_groups(groups)
    )
    added = 1 if created else 0
    skipped = 1 if (contact is not None and not created) else 0
    return RedirectResponse(f"/contacts?added={added}&skipped={skipped}", status_code=303)


@router.post("/contacts/import")
def import_bulk(request: Request, db: Session = Depends(get_db), people: str = Form("")):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    added, skipped, _invalid = book.import_text(db, user.id, people)
    return RedirectResponse(f"/contacts?added={added}&skipped={skipped}", status_code=303)


@router.post("/contacts/import-csv")
async def import_csv_route(
    request: Request, db: Session = Depends(get_db), file: UploadFile = File(...)
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    text = (await file.read()).decode("utf-8-sig", errors="replace")  # strip BOM from Excel
    added, skipped, _invalid = book.import_csv(db, user.id, text)
    return RedirectResponse(f"/contacts?added={added}&skipped={skipped}", status_code=303)


@router.get("/contacts/template.csv")
def csv_template():
    return Response(
        book.CSV_TEMPLATE,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kith-contacts-template.csv"},
    )


@router.post("/contacts/{contact_id}/edit")
def edit_one(
    contact_id: str, request: Request, db: Session = Depends(get_db),
    name: str = Form(""), email: str = Form(""), groups: str = Form(""),
):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    book.update_contact(
        db, user.id, contact_id, email, name.strip() or None, groups=book.parse_groups(groups)
    )
    return RedirectResponse("/contacts", status_code=303)


@router.post("/contacts/{contact_id}/delete")
def delete_one(contact_id: str, request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    book.delete_contact(db, user.id, contact_id)
    return RedirectResponse("/contacts", status_code=303)


@router.get("/contacts/export")
def export_csv(request: Request, db: Session = Depends(get_db)):
    user = load_user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "email", "groups"])
    for c in book.list_contacts(db, user.id):
        writer.writerow([c.name or "", c.email, ", ".join(c.groups or [])])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kith-contacts.csv"},
    )
