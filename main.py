import os
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from tokens import load_tokens, save_tokens

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "0"

app = FastAPI()


app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],
)


# ======================
# Google OAuth config
# ======================

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL")
REDIRECT_URI = f"{BASE_URL}/auth/google/callback"


def get_oauth_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


# ======================
# Helpers
# ======================

def get_user_id(payload: dict):
    return payload.get("meta", {}).get("user_id", "default")


def auth_error(id_, user_id):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": 401,
            "message": f"Google Calendar not connected for user '{user_id}'. Visit {BASE_URL}/auth/google?user_id={user_id}",
        },
    }


# ======================
# OAuth routes
# ======================

@app.get("/auth/google")
def google_auth(user_id: str = "default"):
    flow = get_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=user_id,
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        return {"status": "waiting for google authorization"}

    flow = get_oauth_flow()
    flow.fetch_token(authorization_response=request.url._url)

    user_id = request.query_params.get("state", "default")

    tokens = load_tokens()
    existing_refresh = tokens.get(user_id, {}).get("refresh_token")

    tokens[user_id] = {
        "token": flow.credentials.token,
        "refresh_token": flow.credentials.refresh_token or existing_refresh,
    }
    save_tokens(tokens)

    return {"status": "calendar connected successfully", "user": user_id}


# ======================
# Calendar helpers
# ======================

def get_calendar_service(user_id: str):
    tokens = load_tokens()
    if user_id not in tokens:
        return None

    creds = Credentials(
        token=tokens[user_id]["token"],
        refresh_token=tokens[user_id]["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    return build("calendar", "v3", credentials=creds)


def calendar_list_events(user_id: str, max_results=10):
    service = get_calendar_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    now = datetime.utcnow().isoformat() + "Z"

    events = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    return events.get("items", [])


def calendar_create_event(user_id: str, summary: str, start: str, end: str):
    service = get_calendar_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    event = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }

    created = (
        service.events()
        .insert(calendarId="primary", body=event)
        .execute()
    )

    return {
        "id": created["id"],
        "htmlLink": created["htmlLink"],
    }


def calendar_delete_event(user_id: str, event_id: str):
    service = get_calendar_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    service.events().delete(calendarId="primary", eventId=event_id).execute()
    return "DELETED"


# ======================
# MCP endpoints
# ======================

@app.post("/mcp")
async def mcp_handler(request: Request):
    payload = await request.json()
    method = payload.get("method")
    id_ = payload.get("id")
    user_id = get_user_id(payload)

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "serverInfo": {
                    "name": "Multi-User Google Calendar MCP",
                    "version": "0.1.0",
                }
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "tools": [
                    {
                        "name": "calendar.list_events",
                        "description": "List upcoming calendar events",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "max_results": {
                                    "type": "integer",
                                    "default": 10,
                                }
                            },
                        },
                    },
                    {
                        "name": "calendar.create_event",
                        "description": "Create a calendar event",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "start": {
                                    "type": "string",
                                    "description": "ISO datetime (2026-01-10T10:00:00)"
                                },
                                "end": {
                                    "type": "string",
                                    "description": "ISO datetime (2026-01-10T11:00:00)"
                                },
                            },
                            "required": ["summary", "start", "end"],
                        },
                    },
                    {
                        "name": "calendar.delete_event",
                        "description": "Delete a calendar event by ID",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "event_id": {"type": "string"},
                            },
                            "required": ["event_id"],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        tool = payload["params"]["name"]
        args = payload["params"].get("arguments", {})

        if tool == "calendar.list_events":
            res = calendar_list_events(user_id, args.get("max_results", 10))
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "json", "json": res}]}}

        if tool == "calendar.create_event":
            res = calendar_create_event(user_id, args["summary"], args["start"], args["end"])
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "json", "json": res}]}}

        if tool == "calendar.delete_event":
            res = calendar_delete_event(user_id, args["event_id"])
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": "🗑️ Event deleted"}]}}

    return JSONResponse(
        status_code=400,
        content={"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}},
    )


@app.get("/")
def health():
    return {"status": "Calendar MCP running"}
