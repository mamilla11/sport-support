#!/usr/bin/env python3
"""
sync_calendar_to_sheets.py
──────────────────────────
Переносит события из Google Календаря в Google Таблицу.

Используется для первичной синхронизации: если данные раньше вносились
вручную только в Calendar, этот скрипт перенесёт их все в Sheets.

Что делает:
  • Читает ВСЕ однодневные события из Google Календаря (или за указанный период).
  • Для каждого события берёт: дату и название (имя участника).
  • Пропускает дубликаты — строки, которые уже есть в таблице (та же дата + имя).
  • Добавляет новые строки в Google Таблицу.

Запуск:
  python sync_calendar_to_sheets.py

  # Только за определённый период:
  python sync_calendar_to_sheets.py --from 2025-01-01 --to 2025-12-31

  # Посмотреть что будет синхронизировано, без записи:
  python sync_calendar_to_sheets.py --dry-run
"""

from dotenv import load_dotenv
import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ─── Конфигурация ─────────────────────────────────────────────────────────────
# Читается из переменных окружения (те же, что и в боте)

load_dotenv()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CALENDAR_ID    = os.environ.get("CALENDAR_ID", "")

# ─── Таблица нормализации имён ─────────────────────────────────────────────────
# Ключ: summary из Google Calendar (точное совпадение, регистр игнорируется)
# Значение: нормализованное имя в формате "Имя (@username)"
#
# Формат "Имя (@username)" позволяет автоматически извлечь username
# и записать его в отдельную колонку таблицы.

NAME_MAP: dict[str, str] = {
    "fira"              : "Fira (@FiraSakhapova)",
    "y jin"             : "Y Jin (@YJinLi)",
    "anya"              : "Anya (@anyakholina)",
    "anastasiia"        : "Anastasiia (@chaosresolver)",
    "katya"             : "Katya (@katyazarukina)",
    "yulia"             : "Yulia (@Julialatygan)",
    "jenya"             : "Y Jin (@YJinLi)",       # Jenya → Y Jin
    "julia"             : "Yulia (@juliapliasunova)",
    "dasha"             : "Dasha (@dashaseed)",
    "yulia p"           : "Yulia (@juliapliasunova)",
    "yulia p."          : "Yulia (@juliapliasunova)",
    "sergey"            : "Sergey (@sergeykurakov)",
    "yana"              : "Yana (@yana_mikhnovets)",
    "alyona"            : "Alyona (@alyonabe)",
    "lena"              : "Lena (@ohhlena)",
    "sergey !!! 30km"   : "Sergey (@sergeykurakov)",
    "sergey 42km !!!"   : "Sergey (@sergeykurakov)",
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]


# ─── Google API ───────────────────────────────────────────────────────────────

def get_creds() -> Credentials:
    env_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_json:
        return Credentials.from_service_account_info(json.loads(env_json), scopes=SCOPES)
    if os.path.exists("credentials.json"):
        return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    print("❌ Не найден credentials.json и не задана переменная GOOGLE_CREDENTIALS_JSON")
    sys.exit(1)
 
 
def get_sheets_client():
    return gspread.authorize(get_creds())
 
 
def get_calendar_service():
    return build("calendar", "v3", credentials=get_creds())
 
 
# ─── Нормализация имён ────────────────────────────────────────────────────────
 
def extract_username(normalized_name: str) -> str:
    """
    Из строки вида "Anya (@anyakholina)" возвращает "@anyakholina".
    Если формат не соответствует — возвращает пустую строку.
    """
    if "(@" in normalized_name and normalized_name.endswith(")"):
        start = normalized_name.index("(@") + 1   # позиция '@'
        return normalized_name[start:-1]           # "@anyakholina"
    return ""
 
 
def normalize_events(events: list[dict]) -> list[dict]:
    """
    Нормализует поле 'name' в каждом событии согласно NAME_MAP.
    Добавляет поле 'username', извлечённое из нормализованного имени.
    Выводит предупреждение для имён, которых нет в таблице маппинга.
    """
    unmapped: set[str] = set()
 
    for ev in events:
        raw      = ev["name"]
        key      = raw.strip().lower()
        mapped   = NAME_MAP.get(key)
 
        ev["original_name"] = raw   # сохраняем для сравнения при патче Calendar
 
        if mapped:
            ev["name"]     = mapped
            ev["username"] = extract_username(mapped)
        else:
            # Имя не найдено в маппинге — оставляем как есть
            ev["username"] = ""
            unmapped.add(raw)
 
    if unmapped:
        print(f"\n⚠️  Не найдено в таблице нормализации ({len(unmapped)} уникальных):")
        for name in sorted(unmapped):
            print(f"   «{name}»")
        print("   Эти записи будут добавлены с оригинальным именем.\n")
    else:
        print("   Все имена успешно нормализованы.")
 
    return events
 
 
# ─── Загрузка событий из Google Calendar ──────────────────────────────────────
 
def fetch_calendar_events(
    calendar_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    """
    Возвращает список однодневных событий из Google Calendar.
    Каждый элемент: {"date": date, "name": str}
    """
    svc = get_calendar_service()
    events = []
    page_token = None
 
    # Параметры запроса
    kwargs: dict = {
        "calendarId": calendar_id,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 2500,
    }
 
    # Фильтрация по дате (RFC3339 с полночью UTC)
    if date_from:
        kwargs["timeMin"] = datetime(
            date_from.year, date_from.month, date_from.day,
            tzinfo=timezone.utc
        ).isoformat()
    if date_to:
        # +1 день, чтобы включить последний день включительно
        next_day = date_to + timedelta(days=1)
        kwargs["timeMax"] = datetime(
            next_day.year, next_day.month, next_day.day,
            tzinfo=timezone.utc
        ).isoformat()
 
    print(f"📅 Загружаю события из Google Calendar...")
 
    while True:
        if page_token:
            kwargs["pageToken"] = page_token
 
        result = svc.events().list(**kwargs).execute()
        items  = result.get("items", [])
 
        for item in items:
            # Берём только однодневные события (без времени)
            start = item.get("start", {})
            event_date_str = start.get("date")   # "2025-03-10"
 
            if not event_date_str:
                # Событие со временем (не весь день) — пропускаем
                continue
 
            summary = (item.get("summary") or "").strip()
            if not summary:
                # Без названия — пропускаем
                continue
 
            try:
                event_date = date.fromisoformat(event_date_str)
            except ValueError:
                continue
 
            events.append({
                "id":   item["id"],
                "date": event_date,
                "name": summary,
            })
 
        page_token = result.get("nextPageToken")
        if not page_token:
            break
 
    print(f"   Найдено событий в Calendar: {len(events)}")
    return events
 
 
# ─── Обновление событий в Google Calendar ─────────────────────────────────────
 
def update_calendar_events(
    calendar_id: str,
    events: list[dict],
    dry_run: bool = False,
) -> None:
    """
    Для каждого события, у которого 'name' отличается от 'original_name',
    патчит поле summary в Google Calendar.
 
    events: список {"id": str, "date": date, "name": str, "original_name": str, ...}
    """
    to_update = [ev for ev in events if ev["name"] != ev["original_name"]]
 
    if not to_update:
        print("✅ Все названия событий в Calendar уже нормализованы — ничего не меняем.")
        return
 
    print(f"\n📝 Нужно обновить {len(to_update)} событий в Google Calendar:")
    for ev in to_update:
        print(f"   {ev['date']}  «{ev['original_name']}»  →  «{ev['name']}»")
 
    if dry_run:
        print("🔍 DRY RUN — изменения в Calendar НЕ записаны.")
        return
 
    svc     = get_calendar_service()
    updated = 0
    errors  = 0
 
    for ev in to_update:
        try:
            svc.events().patch(
                calendarId=calendar_id,
                eventId=ev["id"],
                body={"summary": ev["name"]},
            ).execute()
            updated += 1
        except Exception as exc:
            print(f"   ❌ Ошибка при обновлении {ev['id']} ({ev['original_name']}): {exc}")
            errors += 1
 
    print(f"✅ Обновлено в Calendar: {updated}  |  Ошибок: {errors}")
 
 
# ─── Загрузка существующих данных из Google Sheets ────────────────────────────
 
def _dedup_key(date_str: str, username: str, name: str) -> tuple[str, str]:
    """
    Ключ для определения дубликатов.
    Если есть username — используем (date, @username), иначе (date, name).
    """
    return (date_str, username if username else name)
 
 
def fetch_existing_rows(spreadsheet_id: str) -> set[tuple[str, str]]:
    """
    Возвращает множество dedup-ключей для строк, уже имеющихся в таблице.
    Ключ: (date_str, @username) если username заполнен, иначе (date_str, name).
    """
    gc    = get_sheets_client()
    sheet = gc.open_by_key(spreadsheet_id).sheet1
 
    # Убеждаемся, что заголовки есть
    header = sheet.row_values(1)
    if not header or header[0] != "Date":
        print("ℹ️  Таблица пустая — заголовки будут добавлены автоматически.")
        return set()
 
    records = sheet.get_all_records()
    existing = set()
    for row in records:
        d        = str(row.get("Date",     "")).strip()
        name     = str(row.get("Name",     "")).strip()
        username = str(row.get("Username", "")).strip()
        if d and name:
            existing.add(_dedup_key(d, username, name))
 
    print(f"   Уже есть строк в таблице: {len(existing)}")
    return existing
 
 
# ─── Запись новых строк в Google Sheets ───────────────────────────────────────
 
def write_rows_to_sheet(
    spreadsheet_id: str,
    rows: list[dict],
    dry_run: bool = False,
) -> None:
    """
    Добавляет строки в конец Google Таблицы.
    rows: список {"date": date, "name": str}
    """
    if not rows:
        print("✅ Нечего добавлять — все события уже есть в таблице.")
        return
 
    gc    = get_sheets_client()
    sheet = gc.open_by_key(spreadsheet_id).sheet1
 
    # Создаём заголовки, если таблица пустая
    if not sheet.row_values(1):
        if not dry_run:
            sheet.insert_row(["Date", "Name", "Username", "UserID"], 1)
        print("   Добавлены заголовки: Date | Name | Username | UserID")
 
    # Формируем строки для вставки
    # Username берём из нормализованного имени; UserID недоступен из Calendar
    data_to_write = [
        [str(r["date"]), r["name"], r.get("username", ""), ""]
        for r in rows
    ]
 
    if dry_run:
        print(f"\n🔍 DRY RUN — будет добавлено {len(data_to_write)} строк (без записи):")
        for row in data_to_write[:20]:
            print(f"   {row[0]}  |  {row[2] or row[1]}")   # username если есть, иначе name
        if len(data_to_write) > 20:
            print(f"   ... и ещё {len(data_to_write) - 20} строк")
        return
 
    # Batch-запись для скорости (один API-запрос вместо N)
    sheet.append_rows(data_to_write, value_input_option="RAW")
    print(f"✅ Добавлено {len(data_to_write)} строк в Google Таблицу.")
 
 
# ─── Основная логика ──────────────────────────────────────────────────────────
 
def main():
    parser = argparse.ArgumentParser(
        description="Синхронизация Google Calendar → Google Sheets"
    )
    parser.add_argument(
        "--from", dest="date_from", metavar="YYYY-MM-DD",
        help="Начальная дата (включительно). По умолчанию — всё время.",
    )
    parser.add_argument(
        "--to", dest="date_to", metavar="YYYY-MM-DD",
        help="Конечная дата (включительно). По умолчанию — сегодня.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Показать что будет добавлено, но ничего не записывать.",
    )
    parser.add_argument(
        "--patch-calendar-id", dest="patch_calendar_id", metavar="CALENDAR_ID",
        help=(
            "Calendar ID, в который будут записаны нормализованные события. "
            "По умолчанию — тот же, из которого читаем (CALENDAR_ID из env). "
            "Укажите ID тестового календаря, чтобы проверить патч без риска."
        ),
    )
    args = parser.parse_args()
 
    # Проверка обязательных переменных
    if not SPREADSHEET_ID:
        print("❌ Не задана переменная SPREADSHEET_ID")
        sys.exit(1)
    if not CALENDAR_ID:
        print("❌ Не задана переменная CALENDAR_ID")
        sys.exit(1)
 
    # Парсинг дат
    date_from = None
    date_to   = None
    if args.date_from:
        try:
            date_from = date.fromisoformat(args.date_from)
        except ValueError:
            print(f"❌ Неверный формат даты --from: {args.date_from} (нужен YYYY-MM-DD)")
            sys.exit(1)
    if args.date_to:
        try:
            date_to = date.fromisoformat(args.date_to)
        except ValueError:
            print(f"❌ Неверный формат даты --to: {args.date_to} (нужен YYYY-MM-DD)")
            sys.exit(1)
 
    # ── Шаг 1: загрузить события из Calendar
    calendar_events = fetch_calendar_events(CALENDAR_ID, date_from, date_to)
 
    if not calendar_events:
        print("ℹ️  В указанном периоде событий не найдено.")
        return
 
    # ── Шаг 1б: нормализовать имена (в памяти)
    print("\n🔄 Нормализую имена...")
    calendar_events = normalize_events(calendar_events)
 
    # ── Шаг 1в: обновить summary в Google Calendar
    patch_cal_id = args.patch_calendar_id or CALENDAR_ID
    if args.patch_calendar_id:
        print(f"\n📅 Обновляю события в тестовом Calendar ({patch_cal_id})...")
    else:
        print("\n📅 Обновляю события в Google Calendar...")
    update_calendar_events(patch_cal_id, calendar_events, dry_run=args.dry_run)
 
    # ── Шаг 2: загрузить существующие строки из Sheets
    existing = fetch_existing_rows(SPREADSHEET_ID)
    # print(calendar_events)
    print(existing)
 
    # ── Шаг 3: найти новые (не дубликаты)
    # Ключ дедупликации — (date, @username); если username нет — (date, name)
    # new_rows = []
    # skipped  = 0
    # for ev in calendar_events:
    #     key = _dedup_key(str(ev["date"]), ev.get("username", ""), ev["name"])
    #     if key in existing:
    #         skipped += 1
    #     else:
    #         new_rows.append(ev)
    #         existing.add(key)   # избегаем дублей внутри одного запуска
 
    # print(f"   Пропущено дубликатов: {skipped}")
    # print(f"   Новых строк для добавления: {len(new_rows)}")
 
    # # ── Шаг 4: записать в Sheets
    # write_rows_to_sheet(SPREADSHEET_ID, new_rows, dry_run=args.dry_run)
 
    # if not args.dry_run:
    #     print("\n🎉 Синхронизация завершена!")
    #     print(f"   Открыть таблицу: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


if __name__ == "__main__":
    main()
