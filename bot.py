#!/usr/bin/env python3
import io
import json
import logging
import os
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from dotenv import load_dotenv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pytz
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from telegram import (
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

# ─── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CALENDAR_ID    = os.environ["CALENDAR_ID"]
GROUP_CHAT_ID  = int(os.environ["GROUP_CHAT_ID"])
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Moscow")

# ── Configurable auto-stats schedule ──────────────────────────────────────────
# STATS_DAY: 0=Понедельник … 6=Воскресенье (default 0)
# STATS_HOUR / STATS_MINUTE: время отправки (default 09:00)
STATS_DAY    = int(os.environ.get("STATS_DAY",    "0"))
STATS_HOUR   = int(os.environ.get("STATS_HOUR",   "9"))
STATS_MINUTE = int(os.environ.get("STATS_MINUTE", "0"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Chart palette (dark-mode, looks great in Telegram) ───────────────────────

BG    = "#0F172A"   # dark navy
FG    = "#F1F5F9"   # off-white text
DIM   = "#64748B"   # dimmed labels
GOLD  = "#F59E0B"
SILV  = "#94A3B8"
BRNZ  = "#CD7F32"
BLUE  = "#4F8EF7"
GREEN = "#34D399"
COLORS = ["#4F8EF7", "#34D399", "#F59E0B", "#F87171", "#A78BFA",
          "#FB923C", "#60A5FA", "#4ADE80", "#FBBF24", "#F472B6"]

# Russian month abbreviations
MONTHS_RU = ["янв","фев","мар","апр","май","июн",
              "июл","авг","сен","окт","ноя","дек"]
DAYS_RU   = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]


def fmt_date(d: date) -> str:
    """'5 апр' — cross-platform, Russian."""
    return f"{d.day} {MONTHS_RU[d.month - 1]}"


def fmt_month(d: date) -> str:
    months_full = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                   "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    return f"{months_full[d.month - 1]} {d.year}"


# ─── Google API helpers ────────────────────────────────────────────────────────

def _get_creds() -> Credentials:
    """Supports credentials from env var (cloud) or local file."""
    env_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if env_json:
        return Credentials.from_service_account_info(
            json.loads(env_json), scopes=SCOPES
        )
    return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)


def _sheets_client():
    return gspread.authorize(_get_creds())


def _calendar_service():
    return build("calendar", "v3", credentials=_get_creds())


def _ensure_headers(sheet) -> None:
    try:
        if not sheet.row_values(1):
            sheet.insert_row(["Date", "Name", "Username", "UserID"], 1)
    except Exception:
        pass


# ─── Data helpers ──────────────────────────────────────────────────────────────

def load_records() -> list[dict]:
    gc = _sheets_client()
    return gc.open_by_key(SPREADSHEET_ID).sheet1.get_all_records()


def parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def in_range(row: dict, start: date, end: date) -> bool:
    d = parse_date(row.get("Date", ""))
    return d is not None and start <= d <= end


def filter_records(records: list[dict], start: date, end: date) -> list[dict]:
    return [r for r in records if in_range(r, start, end)]


def week_bounds(ref: date) -> tuple[date, date]:
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=6)


def month_bounds(ref: date) -> tuple[date, date]:
    first = ref.replace(day=1)
    last  = ref.replace(day=monthrange(ref.year, ref.month)[1])
    return first, last


def counts_by_name(records: list[dict]) -> Counter:
    return Counter(r.get("Name", "?") for r in records)


def counts_by_week(records: list[dict]) -> dict[date, int]:
    agg: dict[date, int] = defaultdict(int)
    for r in records:
        d = parse_date(r.get("Date", ""))
        if d:
            monday = d - timedelta(days=d.weekday())
            agg[monday] += 1
    return agg


def counts_by_day(records: list[dict]) -> dict[date, int]:
    agg: dict[date, int] = defaultdict(int)
    for r in records:
        d = parse_date(r.get("Date", ""))
        if d:
            agg[d] += 1
    return agg


def period_bounds(period: str, today: date) -> tuple[date, date]:
    if period == "week":
        return week_bounds(today)
    if period == "month":
        return month_bounds(today)
    return date(2000, 1, 1), today   # "all"


def period_label(period: str, today: date) -> str:
    if period == "week":
        s, e = week_bounds(today)
        return f"за эту неделю ({fmt_date(s)} – {fmt_date(e)})"
    if period == "month":
        return f"за {fmt_month(today)}"
    return "за всё время"


# ─── Chart generators ──────────────────────────────────────────────────────────

def _to_buf(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf


def _apply_dark(ax):
    ax.set_facecolor(BG)
    ax.spines[:].set_visible(False)
    ax.tick_params(colors=FG)


def chart_my_week(name: str, records: list[dict], today: date) -> io.BytesIO:
    """Bar chart Mon→Sun for the current week."""
    monday = today - timedelta(days=today.weekday())
    days   = [monday + timedelta(days=i) for i in range(7)]

    personal  = [r for r in records if r.get("Name") == name]
    day_counts = counts_by_day(personal)
    cnts = [day_counts.get(d, 0) for d in days]

    fig, ax = plt.subplots(figsize=(9, 4.2), facecolor=BG)
    _apply_dark(ax)

    bar_colors = []
    for d, c in zip(days, cnts):
        if d == today:        bar_colors.append(GOLD)
        elif c > 0:           bar_colors.append(BLUE)
        elif d > today:       bar_colors.append("#111827")   # future
        else:                 bar_colors.append("#1E293B")   # past empty

    # Plot invisible 0.05 bar for empty days so bars render at all
    plot_cnts = [max(c, 0.05) for c in cnts]
    bars = ax.bar(range(7), plot_cnts, color=bar_colors, width=0.6, edgecolor="none", zorder=3)

    for i, (bar, cnt) in enumerate(zip(bars, cnts)):
        if cnt > 0:
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.04,
                    "✓" if cnt == 1 else f"×{cnt}",
                    ha="center", va="bottom", color=FG, fontsize=12, fontweight="bold")

    ax.set_xticks(range(7))
    xlabels = [f"{DAYS_RU[i]}\n{days[i].day}" for i in range(7)]
    ax.set_xticklabels(xlabels, color=FG, fontsize=11)
    ax.set_yticks([])

    total = sum(cnts)
    ax.set_title(
        f"  {name}  ·  эта неделя  ·  {fmt_date(monday)} – {fmt_date(days[-1])}",
        color=FG, fontsize=13, fontweight="bold", pad=14, loc="left"
    )
    ax.text(0.99, 1.03, f"Итого: {total}", transform=ax.transAxes,
            ha="right", va="bottom", color=GOLD, fontsize=13, fontweight="bold")
    ax.grid(axis="y", color="#1E293B", linewidth=0.8, zorder=0)

    fig.tight_layout(pad=1.5)
    return _to_buf(fig)


def chart_my_month(name: str, records: list[dict], today: date) -> io.BytesIO:
    """Daily bar chart for current month with week separators."""
    first, last = month_bounds(today)
    days_count  = last.day
    days        = [first + timedelta(days=i) for i in range(days_count)]

    personal   = [r for r in records if r.get("Name") == name]
    day_counts = counts_by_day(personal)
    cnts = [day_counts.get(d, 0) for d in days]

    fig, ax = plt.subplots(figsize=(12, 4.2), facecolor=BG)
    _apply_dark(ax)

    bar_colors = []
    for d, c in zip(days, cnts):
        if d == today:   bar_colors.append(GOLD)
        elif c > 0:      bar_colors.append(BLUE)
        elif d > today:  bar_colors.append("#111827")
        else:            bar_colors.append("#1E293B")

    ax.bar(range(days_count), [max(c, 0.04) for c in cnts],
           color=bar_colors, width=0.75, edgecolor="none", zorder=3)

    # Value labels (only for days with >1 to avoid clutter)
    for i, cnt in enumerate(cnts):
        if cnt > 1:
            ax.text(i, cnt + 0.05, str(cnt), ha="center", va="bottom",
                    color=FG, fontsize=8, fontweight="bold")

    # X-axis: show day numbers on Mon and every 5th day
    x_labels = []
    for i, d in enumerate(days):
        if d.weekday() == 0 or d.day in (1, 10, 20):
            x_labels.append(str(d.day))
        else:
            x_labels.append("")
    ax.set_xticks(range(days_count))
    ax.set_xticklabels(x_labels, color=DIM, fontsize=9)
    ax.set_yticks([])

    # Week separator lines
    for i, d in enumerate(days):
        if d.weekday() == 0 and i > 0:
            ax.axvline(i - 0.5, color="#1E293B", linewidth=1, zorder=2)

    total = sum(c for c in cnts if c > 0)
    ax.set_title(f"  {name}  ·  {fmt_month(today)}", color=FG,
                 fontsize=13, fontweight="bold", pad=14, loc="left")
    ax.text(0.99, 1.03, f"Итого: {total}", transform=ax.transAxes,
            ha="right", va="bottom", color=GOLD, fontsize=13, fontweight="bold")
    ax.grid(axis="y", color="#1E293B", linewidth=0.8, zorder=0)

    fig.tight_layout(pad=1.5)
    return _to_buf(fig)


def chart_my_alltime(name: str, records: list[dict], today: date) -> io.BytesIO:
    """12-week trend (bar) + stats panel on the right."""
    current_monday = today - timedelta(days=today.weekday())
    weeks = [current_monday - timedelta(weeks=i) for i in range(11, -1, -1)]

    personal = [r for r in records if r.get("Name") == name]
    by_week  = counts_by_week(personal)
    cnts     = [by_week.get(w, 0) for w in weeks]

    w_s, w_e = week_bounds(today)
    m_s, m_e = month_bounds(today)
    week_total  = sum(1 for r in personal if in_range(r, w_s, w_e))
    month_total = sum(1 for r in personal if in_range(r, m_s, m_e))
    all_total   = len(personal)

    fig, (ax_bar, ax_stat) = plt.subplots(
        1, 2, figsize=(12, 4.5), facecolor=BG,
        gridspec_kw={"width_ratios": [3, 1]},
    )

    # ── Bar chart ─────────────────────────────────────────────────────────────
    _apply_dark(ax_bar)
    bar_colors = [BLUE] * 11 + [GOLD]   # current week = gold
    bars = ax_bar.bar(range(12), cnts, color=bar_colors, width=0.65,
                      edgecolor="none", zorder=3)
    for bar, cnt in zip(bars, cnts):
        if cnt > 0:
            ax_bar.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + 0.08,
                        str(cnt), ha="center", va="bottom",
                        color=FG, fontsize=10, fontweight="bold")

    # Show label every other week
    xlabels = [fmt_date(w) if i % 2 == 0 else "" for i, w in enumerate(weeks)]
    ax_bar.set_xticks(range(12))
    ax_bar.set_xticklabels(xlabels, rotation=35, ha="right", color=DIM, fontsize=9)
    ax_bar.set_yticks([])
    ax_bar.set_title(f"  {name}  ·  последние 12 недель",
                     color=FG, fontsize=13, fontweight="bold", pad=14, loc="left")
    ax_bar.grid(axis="y", color="#1E293B", linewidth=0.8, zorder=0)

    # ── Stats panel ───────────────────────────────────────────────────────────
    ax_stat.set_facecolor(BG)
    ax_stat.axis("off")
    for i, (lbl, val, col) in enumerate([
        ("эта\nнеделя",  week_total,  GOLD),
        ("этот\nмесяц",  month_total, BLUE),
        ("всё\nвремя",   all_total,   GREEN),
    ]):
        y = 0.83 - i * 0.30
        ax_stat.text(0.5, y,       str(val), transform=ax_stat.transAxes,
                     ha="center", va="center", color=col,
                     fontsize=32, fontweight="bold")
        ax_stat.text(0.5, y - 0.10, lbl, transform=ax_stat.transAxes,
                     ha="center", va="center", color=DIM, fontsize=10)

    fig.tight_layout(pad=1.5)
    return _to_buf(fig)


def chart_rating(counts: Counter, title: str) -> io.BytesIO:
    """Horizontal bar chart for group leaderboard."""
    if not counts:
        fig, ax = plt.subplots(figsize=(9, 2.2), facecolor=BG)
        ax.set_facecolor(BG)
        ax.axis("off")
        ax.text(0.5, 0.5, "Нет данных за этот период",
                ha="center", va="center", color=FG, fontsize=14,
                transform=ax.transAxes)
        return _to_buf(fig)

    ranked = counts.most_common()
    names  = [n for n, _ in ranked]
    values = [v for _, v in ranked]
    n      = len(names)
    height = max(3.0, n * 0.62 + 1.4)

    fig, ax = plt.subplots(figsize=(9, height), facecolor=BG)
    _apply_dark(ax)

    bar_colors = []
    rank_labels = []
    for i in range(n):
        if   i == 0: bar_colors.append(GOLD);  rank_labels.append("#1")
        elif i == 1: bar_colors.append(SILV);  rank_labels.append("#2")
        elif i == 2: bar_colors.append(BRNZ);  rank_labels.append("#3")
        else:        bar_colors.append(COLORS[i % len(COLORS)]); rank_labels.append(f"{i+1}.")

    # Reverse so rank 1 is at top
    y_pos   = list(range(n - 1, -1, -1))
    bars    = ax.barh(y_pos, values, color=bar_colors, height=0.55,
                      edgecolor="none", zorder=3)

    max_val = max(values)
    for bar, val in zip(bars, values):
        ax.text(bar.get_width() + max_val * 0.015,
                bar.get_y() + bar.get_height() / 2,
                str(val), va="center", color=FG, fontsize=12, fontweight="bold")

    y_labels = [f"{rank_labels[i]}  {names[i]}" for i in range(n)]
    ax.set_yticks(y_pos)
    ax.set_yticklabels(y_labels[::-1], color=FG, fontsize=11)
    ax.set_xticks([])
    ax.set_xlim(0, max_val * 1.20)
    ax.set_title(f"  Рейтинг группы  ·  {title}",
                 color=FG, fontsize=13, fontweight="bold", pad=14, loc="left")
    ax.grid(axis="x", color="#1E293B", linewidth=0.8, zorder=0)

    fig.tight_layout(pad=1.5)
    return _to_buf(fig)


# ─── Keyboard builders ─────────────────────────────────────────────────────────

def my_stats_kb(user_id: int) -> InlineKeyboardMarkup:
    prefix = f"my|{user_id}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Неделя",    callback_data=f"{prefix}|week"),
        InlineKeyboardButton("📆 Месяц",     callback_data=f"{prefix}|month"),
        InlineKeyboardButton("🏆 Всё время", callback_data=f"{prefix}|all"),
    ]])


def rating_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Неделя",    callback_data="rating|week"),
        InlineKeyboardButton("📆 Месяц",     callback_data="rating|month"),
        InlineKeyboardButton("🏆 Всё время", callback_data="rating|all"),
    ]])


# ─── Shared render functions ───────────────────────────────────────────────────

async def _render_my_stats(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    name: str,
    period: str,
    edit_msg_id: int | None = None,
) -> None:
    tz    = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()

    try:
        records = load_records()
    except Exception as exc:
        logger.error("load_records error: %s", exc)
        return

    # Generate period-specific chart
    if period == "week":
        buf = chart_my_week(name, records, today)
    elif period == "month":
        buf = chart_my_month(name, records, today)
    else:
        buf = chart_my_alltime(name, records, today)

    # Count for caption
    s, e = period_bounds(period, today)
    personal = [r for r in records if r.get("Name") == name]
    cnt = sum(1 for r in personal if in_range(r, s, e))
    lbl = period_label(period, today)
    caption = f"*{name}* — {cnt} тренировок {lbl}"

    kb = my_stats_kb(user_id)

    if edit_msg_id:
        await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=edit_msg_id,
            media=InputMediaPhoto(media=buf, caption=caption, parse_mode="Markdown"),
            reply_markup=kb,
        )
    else:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb,
        )


async def _render_rating(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    period: str,
    edit_msg_id: int | None = None,
    add_text_caption: bool = False,
) -> None:
    tz    = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()

    try:
        records = load_records()
    except Exception as exc:
        logger.error("load_records error: %s", exc)
        return

    s, e     = period_bounds(period, today)
    filtered = filter_records(records, s, e)
    counts   = counts_by_name(filtered)
    title    = period_label(period, today)
    buf      = chart_rating(counts, title)

    # Caption: text leaderboard
    lines = [f"*Рейтинг группы {title}*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (n, c) in enumerate(counts.most_common()):
        m = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{m} {n} — {c} тренировок")
    caption = "\n".join(lines) if len(lines) > 1 else f"Рейтинг группы {title}\n\nНет данных 😴"

    kb = rating_kb()

    if edit_msg_id:
        await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=edit_msg_id,
            media=InputMediaPhoto(media=buf, caption=caption, parse_mode="Markdown"),
            reply_markup=kb,
        )
    else:
        msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=buf,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb,
        )


# ─── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Команды:\n"
        "  /i\\_did\\_it — зафиксировать тренировку 💪\n"
        "  /my\\_stats  — моя статистика 📊\n"
        "  /rating     — рейтинг группы 🏅\n\n"
        f"Авто-отчёт: каждую неделю в {STATS_HOUR:02d}:{STATS_MINUTE:02d}",
        parse_mode="Markdown",
    )


async def cmd_i_did_it(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user       = update.effective_user
    tz         = pytz.timezone(TIMEZONE)
    today      = datetime.now(tz).date()
    first_name = user.first_name or "Аноним"
    tg_username    = user.username or ""
    calendar_title = f"{first_name} (@{tg_username})" if tg_username else first_name

    # ── Google Sheets ──────────────────────────────────────────────────────────
    try:
        gc    = _sheets_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        _ensure_headers(sheet)
        sheet.append_row([str(today), calendar_title, tg_username, str(user.id)])
        logger.info("Sheets ✓  %s  %s", calendar_title, today)
    except Exception as exc:
        logger.error("Sheets error: %s", exc)
        await update.message.reply_text("⚠️ Не удалось записать в таблицу.")
        return

    # ── Google Calendar ────────────────────────────────────────────────────────
    try:
        svc = _calendar_service()
        svc.events().insert(
            calendarId=CALENDAR_ID,
            body={"summary": calendar_title,
                  "start": {"date": str(today)},
                  "end":   {"date": str(today)}},
        ).execute()
        logger.info("Calendar ✓  %s  %s", calendar_title, today)
    except Exception as exc:
        logger.error("Calendar error: %s", exc)
        await update.message.reply_text(
            f"✅ {first_name}, записано в таблицу!\n"
            "⚠️ Но в Календарь добавить не получилось."
        )
        return

    await update.message.reply_text(f"✅ {first_name}, зафиксировано! Так держать 💪")


async def cmd_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name or "Аноним"
    # Send photo immediately (default: all-time view)
    await _render_my_stats(
        context,
        chat_id=update.effective_chat.id,
        user_id=user.id,
        name=name,
        period="all",
    )


async def cmd_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Default: current week
    await _render_rating(
        context,
        chat_id=update.effective_chat.id,
        period="week",
    )


# ─── Callback handlers ─────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")

    chat_id    = query.message.chat_id
    msg_id     = query.message.message_id

    if parts[0] == "my" and len(parts) == 3:
        _, user_id_str, period = parts
        user_id = int(user_id_str)
        # Look up name from message caption or fallback to sender
        name = query.from_user.first_name or "Аноним"
        await _render_my_stats(context, chat_id, user_id, name, period, edit_msg_id=msg_id)

    elif parts[0] == "rating" and len(parts) == 2:
        _, period = parts
        await _render_rating(context, chat_id, period, edit_msg_id=msg_id)


# ─── Weekly auto-job ───────────────────────────────────────────────────────────

async def job_weekly_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_rating(
        context,
        chat_id=GROUP_CHAT_ID,
        period="week",
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("i_did_it", cmd_i_did_it))
    app.add_handler(CommandHandler("my_stats", cmd_my_stats))
    app.add_handler(CommandHandler("rating",   cmd_rating))
    app.add_handler(CallbackQueryHandler(callback_handler))

    tz: pytz.BaseTzInfo = pytz.timezone(TIMEZONE)
    job_queue: JobQueue  = app.job_queue
    job_queue.run_daily(
        callback=job_weekly_stats,
        time=time(hour=STATS_HOUR, minute=STATS_MINUTE, tzinfo=tz),
        days=(STATS_DAY,),
        name="weekly_stats",
    )

    logger.info(
        "Бот запущен. Авто-отчёт: день %s в %02d:%02d (%s)",
        STATS_DAY, STATS_HOUR, STATS_MINUTE, TIMEZONE,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
