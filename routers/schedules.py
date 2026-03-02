from fastapi import APIRouter, Depends
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/schedules", tags=["schedules"])

@router.get("/phone/{phone_id}")
async def list_schedules(phone_id: str, db: Client = Depends(get_supabase)):
    result = db.table("schedules")\
        .select("*, scenarios(name), contacts(name, number)")\
        .eq("phone_id", phone_id)\
        .execute()
    return result.data

@router.post("/")
async def create_schedule(body: dict, db: Client = Depends(get_supabase)):
    result = db.table("schedules").insert(body).execute()
    return result.data[0]

@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("schedules").update(body).eq("id", schedule_id).execute()
    return result.data[0]

@router.patch("/{schedule_id}/status")
async def update_status(schedule_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("schedules").update({"status": body["status"]}).eq("id", schedule_id).execute()
    return result.data[0]

@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str, db: Client = Depends(get_supabase)):
    db.table("schedules").delete().eq("id", schedule_id).execute()
    return {"ok": True}
