"""
MCP-сервер для Кодики/Фоксики
Подключается к S20 CRM и UIS телефонии.
"""

import os
import json
import httpx
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

S20_HOSTNAME = os.environ.get("S20_HOSTNAME", "kodiki.s20.online")
S20_EMAIL    = os.environ.get("S20_EMAIL", "")
S20_API_KEY  = os.environ.get("S20_API_KEY", "")
UIS_KEY      = os.environ.get("UIS_API_KEY", "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")
PORT         = int(os.environ.get("PORT", 8080))

S20_BASE = f"https://{S20_HOSTNAME}/v2api"
UIS_BASE = "https://dataapi.uiscom.ru/v2.0"

mcp = FastMCP("kodiki_mcp")


async def s20_auth() -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{S20_BASE}/auth/login",
            json={"email": S20_EMAIL, "api_key": S20_API_KEY}
        )
        r.raise_for_status()
        return r.json().get("token", "")


async def s20_get(path: str, payload: dict) -> dict:
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


@mcp.tool(name="s20_get_leads", annotations={"readOnlyHint": True})
async def s20_get_leads(params: LeadsInput) -> str:
    """Возвращает список лидов из S20 CRM за период."""
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")
    try:
        data  = await s20_get(f"{params.branch_id}/lead/index", {
            "page": params.page,
            "date_from": date_from,
            "date_to": date_to,
        })
        items = data.get("items", [])
        total = data.get("total", 0)
        if not items:
            return f"Лиды не найдены за период {date_from} — {date_to}."
        lines = [f"Лиды {date_from} — {date_to} | Всего: {total}\n"]
        for lead in items:
            lines.append(
                f"ID:{lead.get('id')} | {lead.get('name','—')} | "
                f"Статус: {lead.get('lead_status_id','—')} | "
                f"Источник: {lead.get('referer','—')} | "
                f"Создан: {lead.get('created_at','—')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


class FunnelInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")
    date_from: Optional[str] = Field(None, description="Дата от")
    date_to: Optional[str] = Field(None, description="Дата до")


@mcp.tool(name="s20_funnel_by_manager", annotations={"readOnlyHint": True})
async def s20_funnel_by_manager(params: FunnelInput) -> str:
    """Анализирует конверсию лидов по каждому менеджеру."""
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")
    try:
        all_leads = []
        page = 0
        while True:
            data = await s20_get(f"{params.branch_id}/lead/index", {
                "page": page,
                "date_from": date_from,
                "date_to": date_to,
            })
            items = data.get("items", [])
            all_leads.extend(items)
            if len(all_leads) >= data.get("total", 0) or not items:
                break
            page += 1

        from collections import Counter
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
            statuses_str = ", ".join(f"{k}:{v}" for k, v in d["statuses"].items())
            lines.append(f"{m}: всего={d['total']} | {statuses_str}")
        lines.append(f"\nИтого лидов: {len(all_leads)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


class StatusInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")


@mcp.tool(name="s20_get_lead_statuses", annotations={"readOnlyHint": True})
async def s20_get_lead_statuses(params: StatusInput) -> str:
    """Возвращает список статусов лидов с их ID."""
    try:
        data = await s20_get(f"{params.branch_id}/lead-status/index", {"page": 0})
        items = data.get("items", [])
        if not items:
            return "Статусы не найдены."
        lines = ["Статусы лидов:\n"]
        for s in items:
            lines.append(f"ID:{s.get('id')} — {s.get('name','—')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
