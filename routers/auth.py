from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
import httpx, jwt, os
from datetime import datetime, timedelta

router = APIRouter(prefix="/auth", tags=["auth"])

class GoogleTokenRequest(BaseModel):
    token: str

@router.post("/google")
async def google_auth(request: GoogleTokenRequest):
    # אמת מול Google
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {request.token}"}
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    info      = resp.json()
    email     = info["email"]
    name      = info.get("name", "")
    google_id = info["sub"]
    avatar    = info.get("picture", "")

    db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    existing = db.table("users").select("*").eq("google_id", google_id).execute()

    if existing.data:
        db.table("users").update({"last_login": datetime.utcnow().isoformat()}).eq("google_id", google_id).execute()
        user_row = existing.data[0]
    else:
        result = db.table("users").insert({
            "email": email, "name": name,
            "google_id": google_id, "avatar": avatar,
            "last_login": datetime.utcnow().isoformat(),
        }).execute()
        user_row = result.data[0]

    access_token = jwt.encode({
        "sub": email,
        "uid": user_row["id"],
        "exp": datetime.utcnow() + timedelta(days=7)
    }, os.getenv("JWT_SECRET"), algorithm="HS256")

    return {
        "access_token": access_token,
        "user": {"id": user_row["id"], "email": email, "name": name, "avatar": avatar}
    }
