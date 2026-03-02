from fastapi import APIRouter, Depends
from dependencies import get_supabase
from supabase import Client

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

@router.get("/phone/{phone_id}")
async def list_scenarios(phone_id: str, db: Client = Depends(get_supabase)):
    result = db.table("scenarios").select("*").eq("phone_id", phone_id).execute()
    return result.data

@router.get("/{scenario_id}")
async def get_scenario(scenario_id: str, db: Client = Depends(get_supabase)):
    result = db.table("scenarios").select("*").eq("id", scenario_id).single().execute()
    return result.data

@router.post("/")
async def create_scenario(body: dict, db: Client = Depends(get_supabase)):
    result = db.table("scenarios").insert(body).execute()
    return result.data[0]

@router.patch("/{scenario_id}")
async def update_scenario(scenario_id: str, body: dict, db: Client = Depends(get_supabase)):
    result = db.table("scenarios").update(body).eq("id", scenario_id).execute()
    return result.data[0]

@router.delete("/{scenario_id}")
async def delete_scenario(scenario_id: str, db: Client = Depends(get_supabase)):
    db.table("scenarios").delete().eq("id", scenario_id).execute()
    return {"ok": True}
