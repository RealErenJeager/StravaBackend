import os
import requests
import time
import asyncio
from fastapi import FastAPI, Response, Cookie
from fastapi.responses import RedirectResponse
from supabase import create_client
from typing import Any, Optional

# -----------------------------
# 1) ENVIRONMENT VARIABLES
# -----------------------------
CLIENT_ID = 184811 #os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = "f211a3bf3d878f3e9096cb90f6d3d78c75ed2477" #os.getenv("STRAVA_CLIENT_SECRET")
REDIRECT_URI = "https://stravabackend.onrender.com/exchange_token" #os.getenv("STRAVA_REDIRECT_URI")
SCOPE = "read,activity:read_all"

SUPABASE_URL = "https://hicsdiuldmcpolnvyapv.supabase.co" #os.getenv("SUPABASE_URL")
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhpY3NkaXVsZG1jcG9sbnZ5YXB2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTkyOTM3NTAsImV4cCI6MjA3NDg2OTc1MH0.UumpymuCYtykPsp0f3EWY_UduwuhNizzFupT4LeaKEs" #os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()


# -----------------------------
# 2) Exchange code → tokens
# -----------------------------
@app.get("/exchange_token")
def exchange_tokens(code: str, scope: str):
    if not code:
        return {"error": "Missing authorization code"}

    res = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code"
        }
    )
    if res.status_code != 200:
        return {"error": res.text}

    token = res.json()
    athlete = token["athlete"]

    # store tokens in Supabase
    supabase.table("USERS").upsert({
        "id": athlete["id"],
        "username": athlete["username"],
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": token["expires_at"]
    }, on_conflict="id").execute()

    response = Response(content="Token exchange successful")
    response.set_cookie(key="FTC_Token", value=str(athlete["id"]), httponly=True, samesite="lax", secure=True)
    return response


# -----------------------------
# 3) Token management
# -----------------------------
def ensure_access_token(user_id: str) -> Optional[str]:
    res = supabase.table("USERS").select("*").eq("id", user_id).execute()
    if not res.data:
        return None
    user = res.data[0]

    if time.time() >= user["expires_at"]:
        if not refresh_token(user_id, user["refresh_token"]):
            return None
        user = supabase.table("USERS").select("*").eq("id", user_id).execute().data[0]

    return user["access_token"]


def refresh_token(user_id: str, refresh_token: str) -> bool:
    res = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
    )
    if res.status_code != 200:
        return False
    token = res.json()
    supabase.table("USERS").upsert({
        "id": user_id,
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": token["expires_at"]
    }, on_conflict="id").execute()
    return True


# -----------------------------
# 4) Background periodic fetch
# -----------------------------
@app.on_event("startup")
async def startup():
    asyncio.create_task(periodic_fetch())


async def periodic_fetch():
    while True:
        users = supabase.table("USERS").select("id").execute().data
        for u in users:
            asyncio.create_task(fetch_stats(u["id"]))
        await asyncio.sleep(24 * 60 * 60)


# -----------------------------
# 5) Fetch ride/run/swim stats
# -----------------------------
async def fetch_stats(user_id: str):
    token = ensure_access_token(user_id)
    if not token:
        return

    url = f"https://www.strava.com/api/v3/athletes/{user_id}/stats"
    res = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if res.status_code != 200:
        return

    data = res.json()

    supabase.table("RIDES").upsert({
        "id": user_id,
        "month_dist": data["recent_ride_totals"]["distance"],
        "month_elevation": data["recent_ride_totals"]["elevation_gain"],
        "year_dist": data["ytd_ride_totals"]["distance"],
        "year_elevation": data["ytd_ride_totals"]["elevation_gain"],
        "all_dist": data["all_ride_totals"]["distance"],
        "all_elevation": data["all_ride_totals"]["elevation_gain"]
    }, on_conflict="id").execute()

    supabase.table("RUNS").upsert({
        "id": user_id,
        "month_dist": data["recent_run_totals"]["distance"],
        "month_elevation": data["recent_run_totals"]["elevation_gain"],
        "year_dist": data["ytd_run_totals"]["distance"],
        "year_elevation": data["ytd_run_totals"]["elevation_gain"],
        "all_dist": data["all_run_totals"]["distance"],
        "all_elevation": data["all_run_totals"]["elevation_gain"]
    }, on_conflict="id").execute()

    supabase.table("SWIMS").upsert({
        "id": user_id,
        "month_dist": data["recent_swim_totals"]["distance"],
        "year_dist": data["ytd_swim_totals"]["distance"],
        "all_dist": data["all_swim_totals"]["distance"]
    }, on_conflict="id").execute()


# -----------------------------
# 6) Login redirect
# -----------------------------
@app.get("/login")
def login():
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPE}"
    )
    return RedirectResponse(url=auth_url)


# -----------------------------
# 7) Leaderboard
# -----------------------------
@app.get("/leaderboard")
async def leaderboard():
    users = supabase.table("USERS").select("id,username").execute().data
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


# -----------------------------
# 8) User stats endpoints
# -----------------------------
@app.get("/run")
async def run(FTC_Token: str):
    if not FTC_Token:
        return {"error": "No cookie found"}
    return supabase.table("RUNS").select("*").eq("id", FTC_Token).execute().data[0]


@app.get("/ride")
async def ride(FTC_Token: str):
    if not FTC_Token:
        return {"error": "No cookie found"}
    return supabase.table("RIDES").select("*").eq("id", FTC_Token).execute().data[0]


@app.get("/swim")
async def swim(FTC_Token: str):
    if not FTC_Token:
        return {"error": "No cookie found"}
    return supabase.table("SWIMS").select("*").eq("id", FTC_Token).execute().data[0]


