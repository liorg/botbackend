from fastapi import APIRouter, Depends
from dependencies import get_supabase, get_current_user
from supabase import Client

router = APIRouter(prefix="/phones", tags=["phones"])

@router.get("/")
async def list_phones(user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    result = db.table("phones").select("*").eq("user_id", user["uid"]).execute()
    return result.data

@router.post("/")
async def create_phone(body: dict, user=Depends(get_current_user), db: Client = Depends(get_supabase)):
    result = db.table("phones").insert({**body, "user_id": user["uid"]}).execute()
    return result.data[0]

@router.patch("/{phone_id}")
async def update_phone(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update(body).eq("id", phone_id).execute()
    return result.data[0]

@router.delete("/{phone_id}")
async def delete_phone(phone_id: str, db: Client = Depends(get_supabase)):
    db.table("phones").delete().eq("id", phone_id).execute()
    return {"ok": True}

# נקרא מה-Oracle VM לעדכן סטטוס docker
@router.patch("/{phone_id}/docker-status")
async def update_docker_status(phone_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("phones").update({
        "docker_status": body["status"],
        "docker_url": body.get("url"),
    }).eq("id", phone_id).execute()
    return result.data[0]
