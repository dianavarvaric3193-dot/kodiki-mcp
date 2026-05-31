"""
MCP-сервер для Кодики/Фоксики
Подключается к S20 CRM и UIS телефонии.
Позволяет Claude анализировать лиды и транскрибировать звонки.
"""

import os
import json
import httpx
import asyncio
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

# ─── Конфигурация ────────────────────────────────────────────────────────────

S20_HOSTNAME  = os.environ.get("S20_HOSTNAME", "kodiki.s20.online")
S20_EMAIL     = os.environ.get("S20_EMAIL", "")
S20_API_KEY   = os.environ.get("S20_API_KEY", "")

UIS_KEY       = os.environ.get("UIS_API_KEY", "")
UIS_LOGIN     = os.environ.get("UIS_LOGIN", "")
UIS_PASSWORD  = os.environ.get("UIS_PASSWORD", "")

OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")   # для Whisper (транскрипция)

S20_BASE      = f"https://{S20_HOSTNAME}/v2api"
UIS_BASE      = "https://dataapi.uiscom.ru/v2.0"

# ─── S20 helpers ─────────────────────────────────────────────────────────────

async def s20_auth() -> str:
    """Получаем токен S20."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{S20_BASE}/auth/login",
            json={"email": S20_EMAIL, "api_key": S20_API_KEY}
        )
        r.raise_for_status()
        data = r.json()
        return data.get("token", "")


async def s20_get(path: str, payload: dict) -> dict:
    """POST-запрос к S20 API (все методы — POST)."""
    token = await s20_auth()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{S20_BASE}/{path}",
            json=payload,
            headers={"X-ALFACRM-TOKEN": token}
        )
        r.raise_for_status()
        return r.json()


# ─── UIS helpers ─────────────────────────────────────────────────────────────

async def uis_request(method: str, params: dict) -> dict:
    """Запрос к UIS Data API."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "access_token": UIS_KEY,
            **params
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(UIS_BASE, json=payload)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ValueError(f"UIS ошибка: {data['error']}")
        return data.get("result", {})


async def transcribe_audio(audio_url: str) -> str:
    """Скачиваем аудио и транскрибируем через Whisper API."""
    if not OPENAI_KEY:
        return "⚠️ OPENAI_API_KEY не задан — транскрипция недоступна."

    async with httpx.AsyncClient(timeout=60) as client:
        # Скачиваем аудио
        r = await client.get(audio_url)
        r.raise_for_status()
        audio_bytes = r.content

    # Сохраняем во временный файл
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    # Отправляем в Whisper
    async with httpx.AsyncClient(timeout=120) as client:
        with open(tmp_path, "rb") as audio_file:
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                files={"file": ("call.mp3", audio_file, "audio/mpeg")},
                data={"model": "whisper-1", "language": "ru"}
            )
            r.raise_for_status()
            result = r.json()

    os.unlink(tmp_path)
    return result.get("text", "Транскрипция пуста")


# ─── MCP сервер ──────────────────────────────────────────────────────────────

mcp = FastMCP("kodiki_mcp")


# ── 1. Лиды ──────────────────────────────────────────────────────────────────

class LeadsInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала (обычно 1)")
    date_from: Optional[str] = Field(None, description="Дата от YYYY-MM-DD (по умолчанию 30 дней назад)")
    date_to: Optional[str]   = Field(None, description="Дата до YYYY-MM-DD (по умолчанию сегодня)")
    page: int = Field(0, description="Страница (0 = первая)")


@mcp.tool(
    name="s20_get_leads",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def s20_get_leads(params: LeadsInput) -> str:
    """
    Возвращает список лидов из S20 CRM за период.
    Включает: имя, статус, источник, менеджера, дату создания.
    """
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")

    data = await s20_get(f"{params.branch_id}/lead/index", {
        "page": params.page,
        "date_from": date_from,
        "date_to": date_to,
    })

    items = data.get("items", [])
    total = data.get("total", 0)

    if not items:
        return f"Лиды не найдены за период {date_from} — {date_to}."

    lines = [f"📋 Лиды {date_from} — {date_to} | Всего: {total}\n"]
    for lead in items:
        lines.append(
            f"ID:{lead.get('id')} | {lead.get('name','—')} | "
            f"Статус: {lead.get('lead_status_id','—')} | "
            f"Источник: {lead.get('referer','—')} | "
            f"Менеджер: {lead.get('assigned_id','—')} | "
            f"Создан: {lead.get('created_at','—')}"
        )
    return "\n".join(lines)


# ── 2. Клиенты ───────────────────────────────────────────────────────────────

class CustomerInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")
    query: Optional[str] = Field(None, description="Поиск по имени или телефону")
    page: int = Field(0, description="Страница")


@mcp.tool(
    name="s20_get_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def s20_get_customers(params: CustomerInput) -> str:
    """
    Поиск клиентов (учеников) в S20 CRM.
    Возвращает имя, телефон, баланс, статус.
    """
    payload: dict = {"page": params.page}
    if params.query:
        payload["name"] = params.query

    data = await s20_get(f"{params.branch_id}/customer/index", payload)
    items = data.get("items", [])
    total = data.get("total", 0)

    if not items:
        return "Клиенты не найдены."

    lines = [f"👥 Клиенты | Всего: {total}\n"]
    for c in items:
        phones = ", ".join(p.get("value","") for p in c.get("phone",[]))
        lines.append(
            f"ID:{c.get('id')} | {c.get('name','—')} | "
            f"Тел: {phones} | "
            f"Баланс: {c.get('balance','—')} | "
            f"Статус: {c.get('customer_status_id','—')}"
        )
    return "\n".join(lines)


# ── 3. Конверсия по менеджерам ───────────────────────────────────────────────

class FunnelInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")
    date_from: Optional[str] = Field(None, description="Дата от YYYY-MM-DD")
    date_to: Optional[str]   = Field(None, description="Дата до YYYY-MM-DD")


@mcp.tool(
    name="s20_funnel_by_manager",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def s20_funnel_by_manager(params: FunnelInput) -> str:
    """
    Анализирует конверсию лидов по каждому менеджеру:
    сколько лидов, сколько дошли до счёта, % конверсии.
    """
    date_from = params.date_from or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")

    # Забираем все страницы
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

    # Группируем по менеджеру
    mgr: dict = {}
    for lead in all_leads:
        m = str(lead.get("assigned_id") or "Не назначен")
        if m not in mgr:
            mgr[m] = {"total": 0, "paid": 0, "statuses": {}}
        mgr[m]["total"] += 1
        status = str(lead.get("lead_status_id", "—"))
        mgr[m]["statuses"][status] = mgr[m]["statuses"].get(status, 0) + 1

    lines = [f"📊 Конверсия по менеджерам {date_from} — {date_to}\n"]
    lines.append(f"{'Менеджер':<12} | {'Лидов':>6} | {'Статусы'}");
    lines.append("-" * 60)
    for m, d in sorted(mgr.items(), key=lambda x: -x[1]["total"]):
        statuses_str = ", ".join(f"{k}:{v}" for k, v in d["statuses"].items())
        lines.append(f"{m:<12} | {d['total']:>6} | {statuses_str}")

    lines.append(f"\nИтого лидов: {len(all_leads)}")
    return "\n".join(lines)


# ── 4. Звонки из UIS ─────────────────────────────────────────────────────────

class CallsInput(BaseModel):
    date_from: Optional[str] = Field(None, description="Дата от YYYY-MM-DD")
    date_to: Optional[str]   = Field(None, description="Дата до YYYY-MM-DD")
    employee_id: Optional[str] = Field(None, description="ID сотрудника для фильтра")
    limit: int = Field(20, description="Количество звонков", ge=1, le=100)


@mcp.tool(
    name="uis_get_calls",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def uis_get_calls(params: CallsInput) -> str:
    """
    Возвращает список звонков из UIS за период.
    Включает: дату, номер клиента, сотрудника, длительность, ссылку на запись.
    """
    date_from = params.date_from or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to   = params.date_to   or datetime.now().strftime("%Y-%m-%d")

    req_params: dict = {
        "date_from": f"{date_from} 00:00:00",
        "date_till": f"{date_to} 23:59:59",
        "limit": params.limit,
        "offset": 0,
        "fields": [
            "id", "start_time", "finish_time", "duration",
            "client_phone", "employee_id", "direction",
            "record", "talk_duration", "virtual_phone_number"
        ]
    }
    if params.employee_id:
        req_params["employee_id"] = params.employee_id

    data = await uis_request("get.calls.report", req_params)
    calls = data.get("data", {}).get("calls", [])

    if not calls:
        return f"Звонки за {date_from} — {date_to} не найдены."

    lines = [f"📞 Звонки {date_from} — {date_to} | Найдено: {len(calls)}\n"]
    for c in calls:
        record_info = "🎙 есть запись" if c.get("record") else "нет записи"
        lines.append(
            f"ID:{c.get('id')} | {c.get('start_time','—')} | "
            f"Клиент: {c.get('client_phone','—')} | "
            f"Сотрудник: {c.get('employee_id','—')} | "
            f"Длит: {c.get('talk_duration','0')}с | {record_info}"
        )
    return "\n".join(lines)


# ── 5. Транскрипция звонка ───────────────────────────────────────────────────

class TranscribeInput(BaseModel):
    call_id: str = Field(..., description="ID звонка из uis_get_calls")


@mcp.tool(
    name="uis_transcribe_call",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def uis_transcribe_call(params: TranscribeInput) -> str:
    """
    Скачивает запись звонка по ID и транскрибирует её в текст (Whisper AI).
    Затем анализирует качество разговора менеджера:
    - выявление потребности, работа с возражениями, приглашение на пробный урок.
    """
    # Получаем ссылку на запись
    data = await uis_request("get.calls.report", {
        "filter": {"id": params.call_id},
        "fields": ["id", "record", "client_phone", "employee_id", "start_time", "talk_duration"]
    })
    calls = data.get("data", {}).get("calls", [])
    if not calls:
        return f"Звонок {params.call_id} не найден."

    call = calls[0]
    record_url = call.get("record")
    if not record_url:
        return (
            f"У звонка {params.call_id} нет записи.\n"
            f"Дата: {call.get('start_time')} | "
            f"Клиент: {call.get('client_phone')} | "
            f"Сотрудник: {call.get('employee_id')}"
        )

    # Транскрибируем
    transcript = await transcribe_audio(record_url)

    # Возвращаем транскрипт + метаданные
    return (
        f"📞 Звонок ID: {params.call_id}\n"
        f"Дата: {call.get('start_time')} | "
        f"Клиент: {call.get('client_phone')} | "
        f"Сотрудник: {call.get('employee_id')} | "
        f"Длит: {call.get('talk_duration')}с\n\n"
        f"📝 ТРАНСКРИПТ:\n{transcript}"
    )


# ── 6. Анализ звонка (транскрипт → оценка) ───────────────────────────────────

class AnalyzeCallInput(BaseModel):
    call_id: str = Field(..., description="ID звонка для анализа")


@mcp.tool(
    name="uis_analyze_call",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def uis_analyze_call(params: AnalyzeCallInput) -> str:
    """
    Транскрибирует звонок и возвращает полный текст разговора.
    Claude затем самостоятельно анализирует:
    - выявил ли менеджер потребность родителя
    - работал ли с возражениями
    - пригласил ли на пробный урок
    - ошибки и точки роста
    """
    transcript_result = await uis_transcribe_call(TranscribeInput(call_id=params.call_id))
    return transcript_result


# ── 7. Статусы лидов (справочник) ────────────────────────────────────────────

class StatusInput(BaseModel):
    branch_id: int = Field(1, description="ID филиала")


@mcp.tool(
    name="s20_get_lead_statuses",
    annotations={"readOnlyHint": True, "destructiveHint": False}
)
async def s20_get_lead_statuses(params: StatusInput) -> str:
    """Возвращает список статусов лидов с их ID — нужно для расшифровки воронки."""
    data = await s20_get(f"{params.branch_id}/lead-status/index", {"page": 0})
    items = data.get("items", [])
    if not items:
        return "Статусы не найдены."
    lines = ["📌 Статусы лидов:\n"]
    for s in items:
        lines.append(f"ID:{s.get('id')} — {s.get('name','—')}")
    return "\n".join(lines)


# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable_http", port=8080)
