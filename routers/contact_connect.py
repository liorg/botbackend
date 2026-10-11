# routers/contact_connect.py
# Connect / ignore for contacts with tag=new from the calls screen + contact conversation.
#
#   connect:
#     no other contact on the phone with that number → updated in place: active + is_connect
#     a contact with that number exists              → it becomes active (target),
#                                                    the source becomes a draft with parent_contact_id = target
#   ignore: tag = ignore
#
#   conversation:
#     root     = the contact; a connected draft (is_connect + parent_contact_id) resolves to its parent
#     family   = root + drafts with parent_contact_id = root AND is_connect = true
#     messages = contact_id ∈ family
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client

from dependencies import get_supabase, get_current_user
from routers.contact_lang import normalize_lang, get_user_lang_by_phone
from routers.contacts import _is_valid_lid
from logging_config import get_logger

logger = get_logger("contact_connect")
router = APIRouter(tags=["contacts"])

CONNECTABLE_TAGS = {"new", "draft"}

MSG_COLS = (
    "id,sender,content,status,sent_at,direction,whatsapp_message_id,"
    "contact_id,phone_id,media_url,send_to,tool_tip,call_id"
)


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


# ── conversation helpers ─────────────────────────────────────────────

def _resolve_root(db: Client, c: dict) -> dict:
    pid = c.get("parent_contact_id")
    if pid:
        res = db.table("contacts").select("*").eq("id", pid).limit(1).execute()
        if res.data:
            return res.data[0]
    return c


def _family(db: Client, root: dict) -> tuple[list[str], list[str]]:
    ors = [f"parent_contact_id.eq.{root['id']}"]
    for ident in {_digits(root.get("number")), _digits(root.get("lid"))}:
        if len(ident) >= 7:
            ors += [f"number.eq.{ident}", f"lid.eq.{ident}"]

    res = (
        db.table("contacts").select("id,number,lid")
        .eq("phone_id", root["phone_id"])
        .or_(",".join(ors))
        .execute()
    )
    rows = {root["id"]: root}
    for r in res.data or []:
        rows[r["id"]] = r

    idents: set[str] = set()
    for r in rows.values():
        for v in (_digits(r.get("number")), _digits(r.get("lid"))):
            if len(v) >= 7:
                idents.add(v)

    ids = [root["id"]] + [i for i in rows if i != root["id"]]
    return ids, sorted(idents)


def _msg_filter(ids: list[str], idents: list[str]) -> str:
    parts = [f"contact_id.in.({','.join(ids)})"]
    if idents:
        jids = ",".join(f'"{i}@s.whatsapp.net","{i}@lid"' for i in idents)
        senders = ",".join(f'"{i}"' for i in idents)
        parts.append(f"send_to.in.({jids})")
        parts.append(f"sender.in.({senders})")
    return ",".join(parts)


def _parse_content(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"text": str(v), "type": "text"}
    except Exception:
        return {"text": str(raw), "type": "text"}


def _shape(m: dict, ids: set[str], idents: set[str]) -> dict:
    content = _parse_content(m.get("content"))
    sender = _digits(m.get("sender"))
    return {
        **m,
        "content":   content,
        "type":      content.get("type") or "text",
        "text":      content.get("text") or content.get("caption"),
        "media_url": m.get("media_url") or content.get("mediaUrl"),
        "incoming":  sender in idents if sender else bool(m.get("direction")),
        "orphan":    m.get("contact_id") not in ids,
    }


def _conversation_ctx(db: Client, contact_id: str):
    root = _resolve_root(db, _load(db, contact_id))
    ids, idents = _family(db, root)
    return root, ids, idents


# ── conversation ─────────────────────────────────────────────────────

@router.get(
    "/contacts/{contact_id}/conversation",
    summary="Contact conversation",
    description=(
        "Returns the contact's messages, oldest → newest. A draft resolves to its parent.\n\n"
        "Matches by `contact_id` of the whole family, and also by `send_to`/`sender` = the contact's number or LID — "
        "so messages saved on a different contact_id appear too (`orphan=true`).\n\n"
        "Paging: `before` = `sent_at` of the oldest message already loaded."
    ),
    responses={404: {"description": "Contact not found"}},
)
async def get_conversation(
    contact_id: str,
    limit: int = Query(50, ge=1, le=200),
    before: Optional[str] = Query(None, description="sent_at — return messages older than this"),
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        root, ids, idents = _conversation_ctx(db, contact_id)

        q = (
            db.table("messages").select(MSG_COLS)
            .eq("phone_id", root["phone_id"])
            .or_(_msg_filter(ids, idents))
        )
        if before:
            q = q.lt("sent_at", before)
        rows = q.order("sent_at", desc=True).limit(limit).execute().data or []
        rows.reverse()

        id_set, ident_set = set(ids), set(idents)
        msgs = [_shape(m, id_set, ident_set) for m in rows]

        return {
            "contact":     root,
            "contact_ids": ids,
            "identities":  idents,
            "messages":    msgs,
            "has_more":    len(rows) == limit,
            "orphans":     sum(1 for m in msgs if m["orphan"]),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[conversation] error contact={contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/contacts/{contact_id}/conversation/relink",
    summary="Relink conversation messages to the contact",
    description="Moves every message of the conversation (drafts + orphans) to `contact_id` of the root contact. Idempotent.",
    responses={404: {"description": "Contact not found"}},
)
async def relink_conversation(
    contact_id: str,
    user=Depends(get_current_user),
    db: Client = Depends(get_supabase),
):
    try:
        root, ids, idents = _conversation_ctx(db, contact_id)
        flt = _msg_filter(ids, idents)

        to_move: list[str] = []
        start = 0
        while True:
            chunk = (
                db.table("messages").select("id,contact_id")
                .eq("phone_id", root["phone_id"])
                .or_(flt)
                .order("id")
                .range(start, start + RELINK_PAGE - 1)
                .execute().data or []
            )
            to_move += [m["id"] for m in chunk if m.get("contact_id") != root["id"]]
            if len(chunk) < RELINK_PAGE:
                break
            start += RELINK_PAGE

        moved = 0
        for i in range(0, len(to_move), 200):
            res = db.table("messages").update({"contact_id": root["id"]}) \
                    .in_("id", to_move[i:i + 200]).execute()
            moved += len(res.data or [])

        logger.info(f"[conversation] relink root={root['id']} moved={moved}")
        return {"success": True, "contact_id": root["id"], "moved_messages": moved}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[conversation] relink error contact={contact_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── connect ──────────────────────────────────────────────────────────

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
