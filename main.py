import os
import requests
import time
import asyncio
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from supabase import create_client
from typing import Any

# -----------------------------------------------
# 1) ENVIRONMENT VARIABLES (Render stores secrets)
# -----------------------------------------------
CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI")
SCOPE = "read,activity:read_all"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()


# -----------------------------------------------
# 2) Exchange auth code → permanent tokens
# -----------------------------------------------
@app.get("/exchange_token")
def exchange_tokens(code: str, scope: str):
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }
    )

    if response.status_code != 200:
        return {"error": response.text}

    token = response.json()
    athlete = token["athlete"]

    supabase.table("USERS").upsert({
        "id": athlete["id"],
        "username": athlete["username"],
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": token["expires_at"]
    }, on_conflict="id").execute()

    return {"message": "success", "athlete_id": athlete["id"]}


# -------------------------------------------------------
# 3) Validate & refresh access tokens when expired
# -------------------------------------------------------
def ensure_accessToken_valid(user_id: str):
    response = supabase.table("USERS").select("*").eq("id", user_id).execute()
    if not response.data:
        return None

    user = response.data[0]

    if time.time() >= user["expires_at"]:
        if not regenerate_token(user_id, user["refresh_token"]):
            return None
        access = supabase.table("USERS").select("access_token").eq("id", user_id).execute().data[0]["access_token"]
        return access

    return user["access_token"]


def regenerate_token(user_id: str, refresh_token: str):
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
    )

    if response.status_code != 200:
        return False

    new = response.json()

    supabase.table("USERS").upsert({
        "id": user_id,
        "access_token": new["access_token"],
        "refresh_token": new["refresh_token"],
        "expires_at": new["expires_at"]
    }, on_conflict="id").execute()

    return True



# -------------------------------------------------------
# 4) Background worker to fetch daily stats
# -------------------------------------------------------
@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic())


async def periodic():
    while True:
        users = supabase.table("USERS").select("id").execute().data
        for u in users:
            asyncio.create_task(fetch_stats(u["id"]))
        await asyncio.sleep(24 * 60 * 60)



# -------------------------------------------------------
# 5) Fetch ride/run/swim stats for each user
# -------------------------------------------------------
async def fetch_stats(user_id: str):
    token = ensure_accessToken_valid(user_id)
    if not token:
        return

    url = f"https://www.strava.com/api/v3/athletes/{user_id}/stats"
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"})

    if response.status_code != 200:
        return

    data = response.json()

    # ride data
    supabase.table("RIDES").upsert({
        "id": user_id,
        "month_dist": data["recent_ride_totals"]["distance"],
        "month_elevation": data["recent_ride_totals"]["elevation_gain"],
        "year_dist": data["ytd_ride_totals"]["distance"],
        "year_elevation": data["ytd_ride_totals"]["elevation_gain"],
        "all_dist": data["all_ride_totals"]["distance"],
        "all_elevation": data["all_ride_totals"]["elevation_gain"],
    }, on_conflict="id").execute()

    # run data
    supabase.table("RUNS").upsert({
        "id": user_id,
        "month_dist": data["recent_run_totals"]["distance"],
        "month_elevation": data["recent_run_totals"]["elevation_gain"],
        "year_dist": data["ytd_run_totals"]["distance"],
        "year_elevation": data["ytd_run_totals"]["elevation_gain"],
        "all_dist": data["all_run_totals"]["distance"],
        "all_elevation": data["all_run_totals"]["elevation_gain"],
    }, on_conflict="id").execute()

    # swim data
    supabase.table("SWIMS").upsert({
        "id": user_id,
        "month_dist": data["recent_swim_totals"]["distance"],
        "year_dist": data["ytd_swim_totals"]["distance"],
        "all_dist": data["all_swim_totals"]["distance"],
    }, on_conflict="id").execute()


# -------------------------------------------------------
# 6) OAuth login redirect
# -------------------------------------------------------
@app.get("/login")
def login():
    auth = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPE}"
    )
    return RedirectResponse(url=auth)


# -------------------------------------------------------
# 7) Leaderboard endpoint
# -------------------------------------------------------
@app.get("/leaderboard")
async def leaderboard():
    users = supabase.table("USERS").select("id, username").execute().data
    lb = []

    for u in users:
        uid = u["id"]
        run = supabase.table("RUNS").select("*").eq("id", uid).execute().data[0]
        ride = supabase.table("RIDES").select("*").eq("id", uid).execute().data[0]
        swim = supabase.table("SWIMS").select("*").eq("id", uid).execute().data[0]

        score = (
            run["month_dist"] + run["month_elevation"] / 0.1 +
            ride["month_dist"] / 4 + ride["month_elevation"] / 0.3 +
            swim["month_dist"] / 0.25
        )

        lb.append({"id": uid, "username": u["username"], "score": score})

    lb.sort(key=lambda x: x["score"], reverse=True)
    return {"LeaderBoard": lb[:10]}


@app.get("/run")
async def run(user_id: str):
    return supabase.table("RUNS").select("*").eq("id", user_id).execute().data[0]


@app.get("/ride")
async def ride(user_id: str):
    return supabase.table("RIDES").select("*").eq("id", user_id).execute().data[0]


@app.get("/swim")
async def swim(user_id: str):
    return supabase.table("SWIMS").select("*").eq("id", user_id).execute().data[0]

