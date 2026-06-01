"""
MCP-сервер для Кодики/Фоксики — S20 CRM
"""
import os
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.requests import Request

S20_HOSTNAME = os.environ.get("S20_HOSTNAME", "kodiki.s20.online")
S20_EMAIL    = os.environ.get("S20_EMAIL", "")
S20_API_KEY  = os.environ.get("S20_API_KEY", "")
PORT         = int(os.environ.get("PORT", 8080))
S20_BASE     = f"https://{S20_HOSTNAME}/v2api"

mcp = FastMCP("kodiki_mcp")


async def s20_auth() -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{S20_BASE}/auth/login",
            json={"email": S20_EMAIL, "api_key": S20_API_KEY}
        )
        r.raise_for_status()
        return r.json().get("token", "")


async def s20_post(path: str, payload: dict) -> dict:
    token = await s20_auth()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{S20_BASE}/{path}",
            json=payload,
            headers={"X-ALFACRM-TOKEN": token}
        )
        r.raise_for_status()
        return r.json()


class LeadsInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")
    date_from: Optional[str] = Field(None, description="Дата от YYYY-MM-DD")
    date_to: Optional[str] = Field(None, description="Дата до YYYY-MM-DD")
    page: int = Field(0, description="Страница")


@mcp.tool(name="s20_get_leads")
async def s20_get_leads(params: LeadsInput) -> str:
    """Возвращает список лидов из S20 CRM за период."""
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")
    try:
        data  = await s20_post(f"{params.branch_id}/lead/index", {
            "page": params.page, "date_from": date_from, "date_to": date_to,
        })
        items = data.get("items", [])
        total = data.get("total", 0)
        if not items:
            return f"Лиды не найдены за {date_from} — {date_to}."
        lines = [f"Лиды {date_from} — {date_to} | Всего: {total}\n"]
        for lead in items:
            lines.append(
                f"ID:{lead.get('id')} | {lead.get('name','—')} | "
                f"Статус: {lead.get('lead_status_id','—')} | "
                f"Источник: {lead.get('referer','—')} | "
                f"Менеджер: {lead.get('assigned_id','—')} | "
                f"Создан: {lead.get('created_at','—')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


class FunnelInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")
    date_from: Optional[str] = Field(None, description="Дата от")
    date_to: Optional[str] = Field(None, description="Дата до")


@mcp.tool(name="s20_funnel_by_manager")
async def s20_funnel_by_manager(params: FunnelInput) -> str:
    """Анализирует конверсию лидов по каждому менеджеру."""
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")
    try:
        all_leads = []
        page = 0
        while True:
            data = await s20_post(f"{params.branch_id}/lead/index", {
                "page": page, "date_from": date_from, "date_to": date_to,
            })
            items = data.get("items", [])
            all_leads.extend(items)
            if len(all_leads) >= data.get("total", 0) or not items:
                break
            page += 1
        mgr: dict = {}
        for lead in all_leads:
            m = str(lead.get("assigned_id") or "Не назначен")
            if m not in mgr:
                mgr[m] = {"total": 0, "statuses": {}}
            mgr[m]["total"] += 1
            status = str(lead.get("lead_status_id", "—"))
            mgr[m]["statuses"][status] = mgr[m]["statuses"].get(status, 0) + 1
        lines = [f"Конверсия по менеджерам {date_from} — {date_to}\n"]
        for m, d in sorted(mgr.items(), key=lambda x: -x[1]["total"]):
            st = ", ".join(f"{k}:{v}" for k, v in d["statuses"].items())
            lines.append(f"{m}: всего={d['total']} | {st}")
        lines.append(f"\nИтого: {len(all_leads)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


class StatusInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")


@mcp.tool(name="s20_get_lead_statuses")
async def s20_get_lead_statuses(params: StatusInput) -> str:
    """Возвращает список статусов лидов с их ID."""
    try:
        data = await s20_post(f"{params.branch_id}/lead-status/index", {"page": 0})
        items = data.get("items", [])
        if not items:
            return "Статусы не найдены."
        return "\n".join([f"ID:{s.get('id')} — {s.get('name','—')}" for s in items])
    except Exception as e:
        return f"Ошибка: {e}"


# OAuth endpoints — нужны для подключения к Claude
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    })


async def oauth_authorize(request: Request):
    params = dict(request.query_params)
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code = "kodiki_auth_code"
    return Response(
        status_code=302,
        headers={"location": f"{redirect_uri}?code={code}&state={state}"}
    )


async def oauth_token(request: Request):
    return JSONResponse({
        "access_token": "kodiki_token",
        "token_type": "bearer",
        "expires_in": 86400,
    })


async def homepage(request: Request):
    return JSONResponse({"status": "ok", "service": "kodiki_mcp"})


# Собираем приложение
mcp_app = mcp.streamable_http_app()

app = Starlette(routes=[
    Route("/", homepage),
    Route("/.well-known/oauth-authorization-server", oauth_metadata),
    Route("/.well-known/openid-configuration", oauth_metadata),
    Route("/oauth/authorize", oauth_authorize),
    Route("/oauth/token", oauth_token, methods=["POST"]),
    Mount("/mcp", app=mcp_app),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
