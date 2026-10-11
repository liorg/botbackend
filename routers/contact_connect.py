# routers/contact_connect.py
# Connect / ignore for contacts with tag=new from the calls screen.
#
#   connect:
#     no other contact on the phone with that number → updated in place: active + is_connect
#     a contact with that number exists              → it becomes active (target),
#                                                    the source becomes a draft with parent_contact_id = target
#   ignore: tag = ignore
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_supabase, get_current_user
from routers.contact_lang import normalize_lang, get_user_lang_by_phone
from routers.contacts import _is_valid_lid
from logging_config import get_logger

logger = get_logger("contact_connect")
router = APIRouter(tags=["contacts"])

CONNECTABLE_TAGS = {"new", "draft"}


class ConnectContactRequest(BaseModel):
    name: str = Field(..., description="Contact name. Required. A leading ~ is stripped.")
    number: Optional[str] = Field(None, description="Phone number (7-15 digits). Required when the contact has no valid number.")
    lang: Optional[str] = Field(None, description="Messaging language (he/en/ru/ar). If omitted: the contact's language, then the phone owner's.")
    email: Optional[str] = Field(None, description="Email. An empty string clears it.")
    move_messages: bool = Field(True, description="On merge: move the source contact's messages to the target contact.")
    force: bool = Field(False, description="Merge even when the target already has a different LID (bypasses lid_conflict).")


def _digits(v: Optional[str]) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real_lid(c: dict) -> Optional[str]:
    lid = (c.get("lid") or "").strip()
    return lid if _is_valid_lid(lid) and lid != _digits(c.get("number")) else None


def _complete_pings(db: Client, contact_ids: list[str]) -> None:
    for cid in contact_ids:
        try:
            db.table("ping_sender").update({"status": "completed"}) \
              .eq("contact_id", cid).in_("status", ["pending", "waiting_reply"]).execute()
        except Exception as e:
            logger.warning(f"[connect] ping_sender complete failed contact={cid}: {e}")


def _load(db: Client, contact_id: str) -> dict:
    res = db.table("contacts").select("*").eq("id", contact_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Contact not found")
    return res.data[0]


@router.post(
    "/contacts/{contact_id}/connect",
    summary="Connect unidentified contact",
    description=(
        "Completes the details (name, number, language, email) of a `new`/`draft` contact and makes it active.\n\n"
        "**`in_place` mode** - no other contact on the phone has that number: the contact is updated in place, "
        "`tag=active`, `is_connect=true`. If it has no real LID, the LID is set to the number.\n\n"
        "**`merged` mode** - a contact with that number already exists: the existing one (target) becomes `active` "
        "and receives the name/language/email and the source's LID. The source becomes a `draft` with "
        "`parent_contact_id` pointing at the target. If `move_messages=true`, messages are moved to the target. "
        "Drafts that pointed at the source are relinked to the target.\n\n"
        "**`lid_conflict` (409)** - source and target have different LIDs. Resend with `force=true` to merge anyway.\n\n"
        "In all cases, open `ping_sender` rows of the contacts involved are marked `completed`."
    ),
    responses={
        400: {"description": "`name_required` - name missing, or `number_required` - invalid number (7-15 digits)"},
        404: {"description": "Contact not found"},
        409: {"description": "Tag cannot be connected, or `lid_conflict` - target is linked to a different LID"},
    },
)
async def connect_contact(
    contact_id: str,
    body: ConnectContactRequest,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        src = _load(db, contact_id)
        if src.get("tag") not in CONNECTABLE_TAGS:
            raise HTTPException(status_code=409, detail=f"Contact tag '{src.get('tag')}' cannot be connected")

        name = (body.name or "").strip().lstrip("~").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name_required: Name is required")

        number = _digits(body.number) or _digits(src.get("number"))
        if not (7 <= len(number) <= 15):
            raise HTTPException(status_code=400, detail="number_required: Phone number must be 7-15 digits")

        if body.lang:
            lang = normalize_lang(body.lang)
        elif src.get("lang"):
            lang = normalize_lang(src["lang"])
        else:
            lang = get_user_lang_by_phone(db, src["phone_id"])

        src_lid = _real_lid(src)

        common = {
            "name":       name,
            "lang":       lang,
            "tag":        "active",
            "is_connect": True,
            "updated_at": _now(),
        }
        if body.email is not None:
            common["email"] = body.email.strip() or None

        tgt_res = (
            db.table("contacts").select("*")
            .eq("phone_id", src["phone_id"])
            .eq("number", number)
            .neq("id", contact_id)
            .limit(1)
            .execute()
        )
        target = tgt_res.data[0] if tgt_res.data else None

        # ── in place ─────────────────────────────────────────────────
        if target is None:
            upd = {
                **common,
                "number":            number,
                "lid":               src_lid or number,
                "parent_contact_id": None,
            }
            res = db.table("contacts").update(upd).eq("id", contact_id).execute()
            if not res.data:
                raise HTTPException(status_code=404, detail="Contact not found")
            _complete_pings(db, [contact_id])
            logger.info(f"[connect] in_place contact={contact_id} number={number} lid={upd['lid']}")
            return {
                "success":        True,
                "mode":           "in_place",
                "contact":        res.data[0],
                "draft_id":       None,
                "moved_messages": 0,
            }

        # ── merge into an existing contact ─────────────────────────
        tgt_lid = _real_lid(target)
        if src_lid and tgt_lid and src_lid != tgt_lid and not body.force:
            raise HTTPException(
                status_code=409,
                detail=f"lid_conflict: contact {target.get('name') or number} already linked to LID {tgt_lid}",
            )

        # 1. release the LID from the source first — avoids a unique conflict on (phone_id, lid)
        src_backup = {
            "lid":               src.get("lid"),
            "tag":               src.get("tag"),
            "is_connect":        src.get("is_connect"),
            "parent_contact_id": src.get("parent_contact_id"),
        }
        db.table("contacts").update({
            "lid":               None,
            "tag":               "draft",
            "is_connect":        True,
            "parent_contact_id": target["id"],
            "updated_at":        _now(),
        }).eq("id", contact_id).execute()

        # 2. target → active
        upd = dict(common)
        if src_lid:
            upd["lid"] = src_lid
        elif not target.get("lid"):
            upd["lid"] = number
        if src.get("whatsapp_name") and not target.get("whatsapp_name"):
            upd["whatsapp_name"] = src["whatsapp_name"]

        try:
            res = db.table("contacts").update(upd).eq("id", target["id"]).execute()
            if not res.data:
                raise RuntimeError("target update returned no rows")
        except Exception:
            db.table("contacts").update(src_backup).eq("id", contact_id).execute()
            logger.error(f"[connect] merge failed, source restored contact={contact_id}")
            raise

        # 3. messages + drafts that pointed at the source
        moved = 0
        if body.move_messages:
            try:
                mv = db.table("messages").update({"contact_id": target["id"]}) \
                       .eq("contact_id", contact_id).execute()
                moved = len(mv.data or [])
            except Exception as e:
                logger.warning(f"[connect] move messages failed: {e}")

        try:
            db.table("contacts").update({"parent_contact_id": target["id"]}) \
              .eq("parent_contact_id", contact_id).execute()
        except Exception as e:
            logger.warning(f"[connect] relink child drafts failed: {e}")

        _complete_pings(db, [contact_id, target["id"]])
        logger.info(f"[connect] merged source={contact_id} → target={target['id']} moved={moved}")

        return {
            "success":        True,
            "mode":           "merged",
            "contact":        res.data[0],
            "draft_id":       contact_id,
            "moved_messages": moved,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[connect] error contact={contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/contacts/{contact_id}/ignore",
    summary="Ignore contact",
    description=(
        "Sets `tag=ignore`. The contact is hidden from the calls screen and the contacts list "
        "(which shows only `new`/`active`). Reversible via `/unignore`."
    ),
    responses={404: {"description": "Contact not found"}},
)
async def ignore_contact(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        res = db.table("contacts").update({"tag": "ignore", "updated_at": _now()}) \
                .eq("id", contact_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Contact not found")
        return {"success": True, "contact": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ignore] error contact={contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/contacts/{contact_id}/unignore",
    summary="Restore ignored contact",
    description="Returns a contact with `tag=ignore` to `tag=new`, so it appears again in the unidentified list on the calls screen.",
    responses={404: {"description": "No ignored contact found with this ID"}},
)
async def unignore_contact(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        res = db.table("contacts").update({"tag": "new", "updated_at": _now()}) \
                .eq("id", contact_id).eq("tag", "ignore").execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Ignored contact not found")
        return {"success": True, "contact": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[unignore] error contact={contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
