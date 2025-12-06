import requests
import time
import asyncio
from fastapi import FastAPI, Response, Cookie
from fastapi.responses import RedirectResponse, JSONResponse
from supabase import create_client
from typing import Any, Annotated


CLIENT_ID = 184811
REDIRECT_URI = "https://stravabackend.onrender.com/exchange_token"
SCOPE = "read,activity:read_all"
CLIENT_SECRET= "f211a3bf3d878f3e9096cb90f6d3d78c75ed2477"

SUPABASE_URL = "https://nujprkwzitxyknezdgfw.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im51anBya3d6aXR4eWtuZXpkZ2Z3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1OTUxNDIxOSwiZXhwIjoyMDc1MDkwMjE5fQ.9UloBpTL51uGDLUfngsUWf2P4kE-IaS3H1PlhlhZzHg"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()



# ---------------------------------------------------------
# EXCHANGE TOKEN
# ---------------------------------------------------------
@app.api_route("/exchange_token", methods=["GET", "HEAD"])
def exchange_tokens(code: str, scope: str):
    if not code:
        return JSONResponse({"error": "Missing or invalid authorization code"})

    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": f"{CLIENT_ID}",
            "client_secret": f"{CLIENT_SECRET}",
            "code": f"{code}",
            "grant_type": "authorization_code"
        }
    )

    if response.status_code != 200:
        return JSONResponse({"error": "Token exchange failed", "details": response.text})

    token = response.json()
    athlete = token["athlete"]

    fastapi_response = JSONResponse({"message": "Token exchange successful"})

    fastapi_response.set_cookie(
        key="FTC_Token",
        value=athlete["id"],
        httponly=True,
        samesite="lax",
        secure=False
    )

    user_data = {
        "id": athlete["id"],
        "username": athlete["username"],
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": token["expires_at"]
    }

    supabase.table("USERS").upsert(user_data, on_conflict="id").execute()

    return fastapi_response



# ---------------------------------------------------------
# TOKEN REFRESH HELPERS
# ---------------------------------------------------------
def ensure_accessToken_valid(user_id: str) -> str | None:
    response: Any = supabase.table("USERS").select("*").eq("id", user_id).execute()
    if not response.data:
        return None
    user_data = response.data[0]
    expire_time = user_data["expires_at"]

    if (time.time() >= expire_time):
        print(f"Access code for {user_id} is being refreshed")
        if not regenerate_token(user_data["id"], user_data["refresh_token"]):
            return None

        response = supabase.table("USERS").select("access_token").eq("id", user_id).execute()
        return response.data[0]["access_token"]

    return user_data["access_token"]


def regenerate_token(user_id: str, token: str) -> bool:
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": token
        }
    )

    if response.status_code != 200:
        print("Failed to regenerate the token")
        return False

    new_token = response.json()
    supabase.table("USERS").upsert(
        {
            "id": user_id,
            "access_token": new_token["access_token"],
            "refresh_token": new_token["refresh_token"],
            "expires_at": new_token["expires_at"]
        }, on_conflict="id"
    ).execute()

    return True



# ---------------------------------------------------------
# STARTUP BACKGROUND JOB
# ---------------------------------------------------------
@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic_activities())


async def periodic_activities():
    while True:
        print("Grabbing all user data from strava")
        Users: list[Any] = supabase.table("USERS").select("id").execute().data
        for user in Users:
            id = user["id"]
            asyncio.create_task(getActivites(id))

        print("All data on supabase has been updated")
        await asyncio.sleep(24 * 60 * 60)



async def getActivites(user_id: str):
    ID = user_id
    if not ensure_accessToken_valid(ID):
        return {"error": "Access Token Invalid"}
    Strava_Token: Any = supabase.table("USERS").select("access_token").eq("id", user_id).execute()
    Strava_Token = Strava_Token.data[0]["access_token"]
    print("This is your token::", Strava_Token)

    headers = {"Authorization": f"Bearer {Strava_Token}"}
    activityUrl = f"https://www.strava.com/api/v3/athletes/{ID}/stats"

    response = requests.get(activityUrl, headers=headers)

    if response.status_code != 200:
        return {"error": "Failed to fetch data", "status": response.status_code, "details": response.text}

    data = response.json()

    # Rides
    supabase.table("RIDES").upsert(
        {
            "id": ID,
            "month_dist": data["recent_ride_totals"]["distance"],
            "month_elevation": data["recent_ride_totals"]["elevation_gain"],
            "year_dist": data["ytd_ride_totals"]["distance"],
            "year_elevation": data["ytd_ride_totals"]["elevation_gain"],
            "all_dist": data["all_ride_totals"]["distance"],
            "all_elevation": data["all_ride_totals"]["elevation_gain"]
        }, on_conflict="id"
    ).execute()

    # Runs
    supabase.table("RUNS").upsert(
        {
            "id": ID,
            "month_dist": data["recent_run_totals"]["distance"],
            "month_elevation": data["recent_run_totals"]["elevation_gain"],
            "year_dist": data["ytd_run_totals"]["distance"],
            "year_elevation": data["ytd_run_totals"]["elevation_gain"],
            "all_dist": data["all_run_totals"]["distance"],
            "all_elevation": data["all_run_totals"]["elevation_gain"]
        }, on_conflict="id"
    ).execute()

    # Swims
    supabase.table("SWIMS").upsert(
        {
            "id": ID,
            "month_dist": data["recent_swim_totals"]["distance"],
            "year_dist": data["ytd_swim_totals"]["distance"],
            "all_dist": data["all_swim_totals"]["distance"]
        }, on_conflict="id"
    ).execute()

    return data



# ---------------------------------------------------------
# LOGIN (HEAD + GET)
# ---------------------------------------------------------
@app.api_route("/login", methods=["GET", "HEAD"])
def login():
    print("login called")
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPE}"
    )

    return RedirectResponse(url=auth_url)



# ---------------------------------------------------------
# LEADERBOARD (HEAD + GET)
# ---------------------------------------------------------
@app.api_route("/leaderboard", methods=["GET", "HEAD"])
async def generate_leaderboard():
    Users: list[Any] = supabase.table("USERS").select("id, username").execute().data
    Leaderboard = []

    for user in Users:
        user_id = user["id"]
        username = user["username"]
        run_data = helper_run(user_id)
        ride_data = helper_ride(user_id)
        swim_data = helper_swim(user_id)
        
        score = (
            run_data["month_dist"] + run_data["month_elevation"] / 0.1 +
            ride_data["month_dist"] / 4 + ride_data["month_elevation"] / 0.3 +
            swim_data["month_dist"] / 0.25
        )

        Leaderboard.append({
            "id": user_id,
            "username": username,
            "score": score
        })

    Leaderboard.sort(key=lambda x: x["score"], reverse=True)
    return {"LeaderBoard": Leaderboard[0:10]}



# ---------------------------------------------------------
# RUN, RIDE, SWIM (HEAD + GET)
# ---------------------------------------------------------
@app.api_route("/run", methods=["GET", "HEAD"])
async def run_data(FTC_Token: Annotated[str | None, Cookie()] = None):
    if not FTC_Token:
        return {"Error": "No Cookie Found"}
    return helper_run(FTC_Token)


@app.api_route("/ride", methods=["GET", "HEAD"])
async def ride_data(FTC_Token: Annotated[str | None, Cookie()] = None):
    if not FTC_Token:
        return {"Error": "No Cookie Found"}
    return helper_ride(FTC_Token)


@app.api_route("/swim", methods=["GET", "HEAD"])
async def swim_data(FTC_Token: Annotated[str | None, Cookie()] = None):
    if not FTC_Token:
        return {"Error": "No Cookie Found"}
    return helper_swim(FTC_Token)



# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------
def helper_run(user_id: str):
    data: Any = supabase.table("RUNS").select("*").eq("id", user_id).execute()
    return data.data[0]


def helper_ride(user_id: str):
    data: Any = supabase.table("RIDES").select("*").eq("id", user_id).execute()
    return data.data[0]


def helper_swim(user_id: str):
    data: Any = supabase.table("SWIMS").select("*").eq("id", user_id).execute()
    return data.data[0]
