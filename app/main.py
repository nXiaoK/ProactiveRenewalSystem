import csv
import io
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil import parser as date_parser
from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import (
    DEFAULT_PASSWORD,
    connect_db,
    get_data_dir,
    get_setting,
    init_db,
    now_iso,
    set_setting,
)
from .utils import (
    CYCLE_LABELS,
    CYCLE_OPTIONS,
    DEFAULT_CURRENCIES,
    add_cycle,
    as_bool,
    cycle_length_days,
    normalize_due_date,
    parse_date,
    remaining_days,
    remaining_value,
    safe_float,
    safe_int,
    today_date,
)

PUBLIC_ENDPOINTS = {"login", "static"}
FLOW_LABELS = {"expense": "支出", "income": "收益"}


def load_secret_key():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    secret_path = os.path.join(data_dir, "secret_key")
    if os.path.exists(secret_path):
        with open(secret_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    secret = secrets.token_hex(32)
    with open(secret_path, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


def create_app():
    init_db()
    app = Flask(__name__, static_url_path="/static")
    app.secret_key = load_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    @app.before_request
    def open_db():
        g.db = connect_db()

    @app.teardown_request
    def close_db(_exc):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        stored_hash = get_setting(g.db, "access_password_hash", "")
        show_default = False
        if stored_hash and check_password_hash(stored_hash, DEFAULT_PASSWORD):
            show_default = True
        if request.method == "POST":
            password = request.form.get("password", "")
            if stored_hash and check_password_hash(stored_hash, password):
                session["authed"] = True
                session.permanent = True
                return redirect(request.args.get("next") or url_for("index"))
            error = "密码错误，请重试"
        return render_template(
            "login.html",
            error=error,
            default_password=DEFAULT_PASSWORD if show_default else None,
        )

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def index():
        today = today_date()
        search = request.args.get("q", "").strip()
        category_filter = request.args.get("category", "all")
        status_filter = request.args.get("status", "all")
        sort = request.args.get("sort", "due")
        view = request.args.get("view", "card")
        allowed_views = {"card", "compact", "table"}
        if view not in allowed_views:
            view = "card"

        query = "SELECT * FROM subscriptions"
        params = []
        if search:
            query += " WHERE name LIKE ? OR category LIKE ?"
            like = f"%{search}%"
            params.extend([like, like])
        query += " ORDER BY due_date ASC"

        rows = g.db.execute(query, params).fetchall()
        fx_rates = load_fx_rates(g.db)
        default_reminder_days = safe_int(get_setting(g.db, "default_reminder_days", "7"), 7)

        items = []
        categories = set()
        stats = {
            "count": 0,
            "due_soon": 0,
            "expense_total_cny": 0.0,
            "income_total_cny": 0.0,
            "expense_monthly_cny": 0.0,
            "income_monthly_cny": 0.0,
            "expense_upcoming_30_cny": 0.0,
            "income_upcoming_30_cny": 0.0,
        }

        for row in rows:
            item = hydrate_subscription(row, fx_rates, today, default_reminder_days)
            categories.add(item["category"] or "未分类")

            if category_filter != "all" and item["category"] != category_filter:
                continue
            if status_filter == "soon" and not item["due_soon"]:
                continue
            if status_filter == "paused" and item["enabled"]:
                continue
            if status_filter == "active" and not item["enabled"]:
                continue

            items.append(item)
            stats["count"] += 1
            if item["enabled"]:
                if item["amount_cny"] is not None:
                    if item["is_income"]:
                        stats["income_total_cny"] += item["amount_cny"]
                        stats["income_monthly_cny"] += item["monthly_equiv_cny"]
                        if item["remaining_days"] <= 30:
                            stats["income_upcoming_30_cny"] += item["amount_cny"]
                    else:
                        stats["expense_total_cny"] += item["amount_cny"]
                        stats["expense_monthly_cny"] += item["monthly_equiv_cny"]
                        if item["remaining_days"] <= 30:
                            stats["expense_upcoming_30_cny"] += item["amount_cny"]
                if item["due_soon"]:
                    stats["due_soon"] += 1

        items = sort_items(items, sort)
        upcoming = [
            item for item in items if item["enabled"] and item["remaining_days"] <= 30
        ]
        upcoming = sorted(upcoming, key=lambda value: value["due_date"])[:5]

        category_totals = {}
        for item in items:
            if (
                not item["enabled"]
                or item["is_income"]
                or item["monthly_equiv_cny"] is None
            ):
                continue
            category_totals[item["category"]] = category_totals.get(
                item["category"], 0.0
            ) + item["monthly_equiv_cny"]

        total_monthly = sum(category_totals.values())
        category_breakdown = []
        for category, value in sorted(
            category_totals.items(), key=lambda entry: entry[1], reverse=True
        )[:5]:
            percent = value / total_monthly * 100 if total_monthly else 0
            category_breakdown.append(
                {"category": category, "monthly_cny": value, "percent": percent}
            )

        categories_sorted = sorted(categories)
        return render_template(
            "index.html",
            subscriptions=items,
            categories=categories_sorted,
            current_category=category_filter,
            status_filter=status_filter,
            sort=sort,
            view=view,
            search=search,
            stats=stats,
            upcoming=upcoming,
            category_breakdown=category_breakdown,
            cycle_labels=CYCLE_LABELS,
        )

    @app.route("/subscriptions/new", methods=["GET", "POST"])
    def create_subscription():
        default_reminder_days = safe_int(get_setting(g.db, "default_reminder_days", "7"), 7)
        if request.method == "POST":
            form = request.form
            name = form.get("name", "").strip()
            if not name:
                flash("服务名称不能为空", "error")
                return redirect(url_for("create_subscription"))
            amount = safe_float(form.get("amount"), 0.0)
            currency = form.get("currency", "CNY").strip().upper()
            billing_cycle = form.get("billing_cycle", "month")
            due_date = form.get("due_date", "")
            reminder_days = safe_int(form.get("reminder_days"), default_reminder_days)
            category = form.get("category", "").strip()
            renew_url = form.get("renew_url", "").strip()
            notes = form.get("notes", "").strip()
            enabled = 1 if form.get("enabled") == "on" else 0
            flow = form.get("flow", "expense").strip()

            if billing_cycle not in CYCLE_OPTIONS:
                flash("续费类型不合法", "error")
                return redirect(url_for("create_subscription"))
            if flow not in {"expense", "income"}:
                flash("类型不合法", "error")
                return redirect(url_for("create_subscription"))
            try:
                parse_date(due_date)
            except ValueError:
                flash("到期日期格式不正确", "error")
                return redirect(url_for("create_subscription"))

            now = now_iso()
            g.db.execute(
                """
                INSERT INTO subscriptions
                (name, category, amount, currency, billing_cycle, due_date, renew_url, flow, reminder_days, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    category,
                    amount,
                    currency,
                    billing_cycle,
                    due_date,
                    renew_url,
                    flow,
                    reminder_days,
                    enabled,
                    notes,
                    now,
                    now,
                ),
            )
            g.db.commit()
            flash("订阅已新增", "success")
            return redirect(url_for("index"))

        existing_categories = get_existing_categories(g.db)
        existing_currencies = get_existing_currencies(g.db)
        currency_options = merge_currency_options(existing_currencies)
        return render_template(
            "subscription_form.html",
            subscription=None,
            cycle_labels=CYCLE_LABELS,
            cycle_options=CYCLE_OPTIONS,
            currencies=currency_options,
            categories=existing_categories,
            default_reminder_days=default_reminder_days,
        )

    @app.route("/subscriptions/<int:sub_id>/edit", methods=["GET", "POST"])
    def edit_subscription(sub_id):
        row = g.db.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if row is None:
            flash("未找到订阅", "error")
            return redirect(url_for("index"))

        if request.method == "POST":
            form = request.form
            name = form.get("name", "").strip()
            if not name:
                flash("服务名称不能为空", "error")
                return redirect(url_for("edit_subscription", sub_id=sub_id))

            amount = safe_float(form.get("amount"), row["amount"])
            currency = form.get("currency", row["currency"]).strip().upper()
            billing_cycle = form.get("billing_cycle", row["billing_cycle"])
            due_date = form.get("due_date", row["due_date"])
            reminder_days = safe_int(form.get("reminder_days"), row["reminder_days"])
            category = form.get("category", row["category"] or "")
            renew_url = form.get("renew_url", row["renew_url"] or "")
            notes = form.get("notes", row["notes"] or "")
            enabled = 1 if form.get("enabled") == "on" else 0
            flow = form.get("flow", row["flow"] or "expense").strip()

            if billing_cycle not in CYCLE_OPTIONS:
                flash("续费类型不合法", "error")
                return redirect(url_for("edit_subscription", sub_id=sub_id))
            if flow not in {"expense", "income"}:
                flash("类型不合法", "error")
                return redirect(url_for("edit_subscription", sub_id=sub_id))
            try:
                parse_date(due_date)
            except ValueError:
                flash("到期日期格式不正确", "error")
                return redirect(url_for("edit_subscription", sub_id=sub_id))

            g.db.execute(
                """
                UPDATE subscriptions
                SET name = ?, category = ?, amount = ?, currency = ?, billing_cycle = ?, due_date = ?,
                    renew_url = ?, flow = ?, reminder_days = ?, enabled = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    category,
                    amount,
                    currency,
                    billing_cycle,
                    due_date,
                    renew_url,
                    flow,
                    reminder_days,
                    enabled,
                    notes,
                    now_iso(),
                    sub_id,
                ),
            )
            g.db.commit()
            flash("订阅已更新", "success")
            return redirect(url_for("index"))

        existing_categories = get_existing_categories(g.db)
        existing_currencies = get_existing_currencies(g.db)
        currency_options = merge_currency_options(existing_currencies)
        return render_template(
            "subscription_form.html",
            subscription=row,
            cycle_labels=CYCLE_LABELS,
            cycle_options=CYCLE_OPTIONS,
            currencies=currency_options,
            categories=existing_categories,
            default_reminder_days=row["reminder_days"],
        )

    @app.route("/subscriptions/<int:sub_id>/delete", methods=["POST"])
    def delete_subscription(sub_id):
        g.db.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        g.db.commit()
        flash("订阅已删除", "success")
        return redirect(url_for("index"))

    @app.route("/subscriptions/<int:sub_id>/toggle", methods=["POST"])
    def toggle_subscription(sub_id):
        row = g.db.execute("SELECT enabled FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if row is None:
            flash("未找到订阅", "error")
            return redirect(url_for("index"))
        enabled = 0 if row["enabled"] else 1
        g.db.execute(
            "UPDATE subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
            (enabled, now_iso(), sub_id),
        )
        g.db.commit()
        flash("订阅状态已更新", "success")
        return redirect(url_for("index"))

    @app.route("/subscriptions/<int:sub_id>/renew", methods=["POST"])
    def renew_subscription(sub_id):
        row = g.db.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if row is None:
            flash("未找到订阅", "error")
            return redirect(url_for("index"))

        due_date = parse_date(row["due_date"])
        due_date, _ = normalize_due_date(due_date, row["billing_cycle"], today_date())
        next_due = add_cycle(due_date, row["billing_cycle"])
        g.db.execute(
            "UPDATE subscriptions SET due_date = ?, updated_at = ? WHERE id = ?",
            (next_due.strftime("%Y-%m-%d"), now_iso(), sub_id),
        )
        g.db.commit()
        flash("已顺延一个周期", "success")
        return redirect(url_for("index"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "password":
                handle_password_update()
            elif action == "tg":
                handle_tg_update()
            elif action == "email":
                handle_email_update()
            elif action == "preferences":
                handle_preferences_update()
            elif action == "fx":
                handle_fx_update()
            elif action == "test":
                handle_test_send()
            return redirect(url_for("settings"))

        return render_template(
            "settings.html",
            settings=load_settings(g.db),
        )

    @app.route("/export")
    def export_csv():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "name",
                "category",
                "amount",
                "currency",
                "billing_cycle",
                "due_date",
                "renew_url",
                "flow",
                "reminder_days",
                "enabled",
                "notes",
            ]
        )
        rows = g.db.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        for row in rows:
            writer.writerow(
                [
                    row["name"],
                    row["category"],
                    row["amount"],
                    row["currency"],
                    row["billing_cycle"],
                    row["due_date"],
                    row["renew_url"],
                    row["flow"],
                    row["reminder_days"],
                    row["enabled"],
                    row["notes"],
                ]
            )
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=subscriptions.csv"},
        )

    @app.route("/export/ics")
    def export_ics():
        rows = g.db.execute(
            "SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY due_date ASC"
        ).fetchall()
        today = today_date()
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Renewal Pulse//EN",
            "CALSCALE:GREGORIAN",
        ]
        for row in rows:
            due_date = parse_date(row["due_date"])
            due_date, _ = normalize_due_date(due_date, row["billing_cycle"], today)
            dt_start = due_date.strftime("%Y%m%d")
            uid = f"subscription-{row['id']}@renewal"
            summary = f"{row['name']} 续费"
            description = f"续费金额：{row['amount']} {row['currency']}"
            if row["renew_url"]:
                description += f"\\n续费地址：{row['renew_url']}"
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTAMP:{ics_timestamp()}",
                    f"DTSTART;VALUE=DATE:{dt_start}",
                    f"SUMMARY:{escape_ics(summary)}",
                    f"DESCRIPTION:{escape_ics(description)}",
                ]
            )
            if row["renew_url"]:
                lines.append(f"URL:{escape_ics(row['renew_url'])}")
            rrule = rrule_for_cycle(row["billing_cycle"])
            if rrule:
                lines.append(f"RRULE:{rrule}")
            lines.append("END:VEVENT")

        lines.append("END:VCALENDAR")
        return Response(
            "\r\n".join(lines),
            mimetype="text/calendar; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=subscriptions.ics"},
        )

    @app.route("/import", methods=["GET", "POST"])
    def import_csv():
        if request.method == "POST":
            upload = request.files.get("file")
            if upload is None:
                flash("请选择要导入的 CSV", "error")
                return redirect(url_for("import_csv"))
            raw = upload.read()
            content, _encoding = decode_csv_bytes(raw)
            if content is None:
                flash("CSV 编码无法识别，请使用 UTF-8 或 GBK 保存", "error")
                return redirect(url_for("import_csv"))
            reader = csv.DictReader(io.StringIO(content))
            if not reader.fieldnames:
                flash("CSV 缺少表头，无法导入", "error")
                return redirect(url_for("import_csv"))

            header_map = build_header_map(reader.fieldnames)
            if not header_map:
                flash("CSV 表头无法识别，请使用导出模板或包含 name/amount/due_date 字段", "error")
                return redirect(url_for("import_csv"))

            required_headers = {"name", "amount", "due_date"}
            missing_headers = required_headers - set(header_map.values())
            if missing_headers:
                missing = ", ".join(sorted(missing_headers))
                flash(f"CSV 表头缺少必要字段：{missing}", "error")
                return redirect(url_for("import_csv"))

            count = 0
            skipped = 0
            errors = []
            default_reminder_days = safe_int(
                get_setting(g.db, "default_reminder_days", "7"), 7
            )
            now = now_iso()
            for line_no, raw_row in enumerate(reader, start=2):
                if is_row_empty(raw_row):
                    continue
                row = normalize_row(raw_row, header_map)
                if is_row_empty(row):
                    continue

                row_errors = []
                name = (row.get("name") or "").strip()
                if not name:
                    row_errors.append("缺少服务名称")

                amount = parse_amount(row.get("amount"))
                if amount is None:
                    row_errors.append("金额格式不正确")

                currency = normalize_currency(row.get("currency"))
                billing_cycle = normalize_cycle_value(row.get("billing_cycle"))
                if billing_cycle not in CYCLE_OPTIONS:
                    row_errors.append("续费类型不合法")

                flow = normalize_flow(row.get("flow"))
                if flow not in {"expense", "income"}:
                    row_errors.append("类型不合法")

                try:
                    due_date_obj = parse_date_flexible(row.get("due_date"))
                    due_date = due_date_obj.strftime("%Y-%m-%d")
                except ValueError as exc:
                    row_errors.append(str(exc))
                    due_date = None

                if row_errors:
                    skipped += 1
                    errors.append(f"第 {line_no} 行：{'; '.join(row_errors)}")
                    continue

                enabled_value = parse_enabled(row.get("enabled"))
                reminder_days = parse_reminder_days(
                    row.get("reminder_days"), default_reminder_days
                )

                g.db.execute(
                    """
                    INSERT INTO subscriptions
                    (name, category, amount, currency, billing_cycle, due_date, renew_url, flow, reminder_days, enabled, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        (row.get("category") or "").strip(),
                        amount,
                        currency,
                        billing_cycle,
                        due_date,
                        (row.get("renew_url") or "").strip(),
                        flow,
                        reminder_days,
                        enabled_value,
                        (row.get("notes") or "").strip(),
                        now,
                        now,
                    ),
                )
                count += 1
            g.db.commit()
            if count > 0:
                flash(f"导入完成：成功 {count} 条，跳过 {skipped} 条。", "success")
            else:
                flash("未导入任何记录，请检查 CSV 内容与字段格式。", "error")
            if errors:
                preview = " | ".join(errors[:5])
                if len(errors) > 5:
                    preview += f" ...共 {len(errors)} 条"
                flash(f"跳过原因：{preview}", "error")
            return redirect(url_for("index"))
        return render_template("import.html")

    def handle_password_update():
        current = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        stored_hash = get_setting(g.db, "access_password_hash", "")
        if not stored_hash or not check_password_hash(stored_hash, current):
            flash("当前密码不正确", "error")
            return
        if not new_password or new_password != confirm:
            flash("新密码两次输入不一致", "error")
            return
        set_setting(g.db, "access_password_hash", generate_password_hash(new_password))
        flash("访问密码已更新", "success")

    def handle_tg_update():
        tg_enabled = 1 if request.form.get("tg_enabled") == "on" else 0
        set_setting(g.db, "tg_enabled", tg_enabled)
        set_setting(g.db, "tg_bot_token", request.form.get("tg_bot_token", "").strip())
        set_setting(g.db, "tg_chat_id", request.form.get("tg_chat_id", "").strip())

        flash("TG 配置已保存", "success")

    def handle_email_update():
        email_enabled = 1 if request.form.get("email_enabled") == "on" else 0
        set_setting(g.db, "email_enabled", email_enabled)
        set_setting(g.db, "smtp_host", request.form.get("smtp_host", "").strip())
        set_setting(g.db, "smtp_port", request.form.get("smtp_port", "").strip())
        set_setting(g.db, "smtp_user", request.form.get("smtp_user", "").strip())
        set_setting(g.db, "smtp_password", request.form.get("smtp_password", "").strip())
        set_setting(g.db, "smtp_sender", request.form.get("smtp_sender", "").strip())
        set_setting(g.db, "smtp_tls", 1 if request.form.get("smtp_tls") == "on" else 0)

        flash("邮件配置已保存", "success")

    def handle_preferences_update():
        default_reminder = safe_int(request.form.get("default_reminder_days"), 7)
        set_setting(g.db, "default_reminder_days", default_reminder)
        flash("默认提醒天数已保存", "success")

    def handle_fx_update():
        fx_api_url = request.form.get("fx_api_url", "").strip()
        if fx_api_url:
            set_setting(g.db, "fx_api_url", fx_api_url)
        success, message = update_fx_rates(g.db)
        if success:
            flash("汇率已更新", "success")
        else:
            flash(f"汇率更新失败：{message}", "error")

    def handle_test_send():
        channel = request.form.get("channel")
        success, message = send_test_notification(g.db, channel)
        if success:
            flash("测试提醒已发送", "success")
        else:
            flash(f"测试提醒失败：{message}", "error")

    with app.app_context():
        db = connect_db()
        has_rate = db.execute("SELECT 1 FROM fx_rates LIMIT 1").fetchone()
        if not has_rate:
            update_fx_rates(db)
        db.close()

    scheduler = start_scheduler(app)
    return app


def hydrate_subscription(row, fx_rates, today, default_reminder_days):
    due_date = parse_date(row["due_date"])
    due_date, updated = normalize_due_date(due_date, row["billing_cycle"], today)
    if updated:
        g.db.execute(
            "UPDATE subscriptions SET due_date = ?, updated_at = ? WHERE id = ?",
            (due_date.strftime("%Y-%m-%d"), now_iso(), row["id"]),
        )
        g.db.commit()

    reminder_days = row["reminder_days"] if row["reminder_days"] is not None else default_reminder_days
    flow = row["flow"] or "expense"
    is_income = flow == "income"
    amount_cny = convert_to_cny(row["amount"], row["currency"], fx_rates)
    left_days = remaining_days(due_date, today)
    total_days = cycle_length_days(due_date, row["billing_cycle"])
    remaining_cny = remaining_value(amount_cny, due_date, row["billing_cycle"], today)
    monthly_equiv_cny = None
    yearly_equiv_cny = None
    if amount_cny is not None:
        monthly_equiv_cny = amount_cny / total_days * 30
        yearly_equiv_cny = amount_cny / total_days * 365
    progress_pct = (total_days - left_days) / total_days * 100 if total_days else 0
    progress_pct = max(0, min(progress_pct, 100))

    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"] or "未分类",
        "amount": row["amount"],
        "currency": row["currency"],
        "amount_cny": amount_cny,
        "billing_cycle": row["billing_cycle"],
        "due_date": due_date,
        "renew_url": row["renew_url"],
        "flow": flow,
        "flow_label": FLOW_LABELS.get(flow, "支出"),
        "is_income": is_income,
        "reminder_days": reminder_days,
        "remaining_days": left_days,
        "remaining_value": remaining_cny,
        "enabled": bool(row["enabled"]),
        "notes": row["notes"],
        "due_soon": left_days <= reminder_days,
        "total_days": total_days,
        "monthly_equiv_cny": monthly_equiv_cny,
        "yearly_equiv_cny": yearly_equiv_cny,
        "progress_pct": progress_pct,
        "rolled": updated,
    }


def load_settings(db):
    keys = [
        "default_reminder_days",
        "tg_enabled",
        "tg_bot_token",
        "tg_chat_id",
        "email_enabled",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_sender",
        "smtp_tls",
        "fx_api_url",
        "fx_last_updated",
    ]
    settings = {}
    for key in keys:
        settings[key] = get_setting(db, key, "")
    return settings


def get_existing_categories(db):
    rows = db.execute(
        "SELECT DISTINCT category FROM subscriptions "
        "WHERE category IS NOT NULL AND TRIM(category) != '' "
        "ORDER BY category"
    ).fetchall()
    return [row["category"] for row in rows]


def get_existing_currencies(db):
    rows = db.execute(
        "SELECT DISTINCT currency FROM subscriptions "
        "WHERE currency IS NOT NULL AND TRIM(currency) != '' "
        "ORDER BY currency"
    ).fetchall()
    return [row["currency"].upper() for row in rows]


def merge_currency_options(existing):
    options = []
    seen = set()
    for currency in DEFAULT_CURRENCIES + existing:
        if not currency:
            continue
        value = currency.upper()
        if value in seen:
            continue
        seen.add(value)
        options.append(value)
    return options


def load_fx_rates(db):
    rows = db.execute("SELECT currency, rate_to_cny FROM fx_rates").fetchall()
    return {row["currency"]: row["rate_to_cny"] for row in rows}


def convert_to_cny(amount, currency, fx_rates):
    if currency == "CNY":
        return amount
    rate = fx_rates.get(currency)
    if rate is None:
        return None
    return amount * rate


def update_fx_rates(db):
    url = get_setting(db, "fx_api_url", "")
    if not url:
        return False, "未配置汇率 API"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return False, str(exc)

    rates = data.get("rates") or data.get("conversion_rates")
    base = data.get("base_code") or data.get("base") or "CNY"
    if not rates:
        return False, "API 返回缺少汇率数据"
    if base != "CNY" and "CNY" not in rates:
        return False, "API 返回缺少 CNY 汇率"

    cny_per_base = 1.0
    if base != "CNY":
        cny_per_base = rates.get("CNY", 0)
        if not cny_per_base:
            return False, "无法解析 CNY 汇率"

    now = now_iso()
    for currency, value in rates.items():
        if not value:
            continue
        if currency == "CNY":
            rate_to_cny = 1.0
        elif base == "CNY":
            rate_to_cny = 1.0 / float(value)
        else:
            rate_to_cny = float(cny_per_base) / float(value)
        db.execute(
            "INSERT OR REPLACE INTO fx_rates (currency, rate_to_cny, updated_at) VALUES (?, ?, ?)",
            (currency, rate_to_cny, now),
        )
    db.commit()
    set_setting(db, "fx_last_updated", now)
    return True, None


def start_scheduler(app):
    scheduler = BackgroundScheduler(daemon=True)

    def daily_fx_job():
        with app.app_context():
            db = connect_db()
            update_fx_rates(db)
            db.close()

    def daily_reminder_job():
        with app.app_context():
            db = connect_db()
            send_reminders(db)
            db.close()

    scheduler.add_job(daily_fx_job, "cron", hour=3, minute=0)
    scheduler.add_job(daily_reminder_job, "cron", hour=9, minute=0)

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        scheduler.start()

    return scheduler


def send_reminders(db):
    today = today_date()
    fx_rates = load_fx_rates(db)
    default_reminder_days = safe_int(get_setting(db, "default_reminder_days", "7"), 7)

    tg_enabled = as_bool(get_setting(db, "tg_enabled", "0"))
    email_enabled = as_bool(get_setting(db, "email_enabled", "0"))

    rows = db.execute("SELECT * FROM subscriptions WHERE enabled = 1").fetchall()
    for row in rows:
        due_date = parse_date(row["due_date"])
        due_date, updated = normalize_due_date(due_date, row["billing_cycle"], today)
        if updated:
            db.execute(
                "UPDATE subscriptions SET due_date = ?, updated_at = ? WHERE id = ?",
                (due_date.strftime("%Y-%m-%d"), now_iso(), row["id"]),
            )
            db.commit()

        reminder_days = row["reminder_days"] if row["reminder_days"] is not None else default_reminder_days
        if reminder_days <= 0:
            continue

        left_days = remaining_days(due_date, today)
        if left_days > reminder_days:
            continue

        amount_cny = convert_to_cny(row["amount"], row["currency"], fx_rates)
        message = format_reminder_message(row, due_date, left_days, amount_cny)

        if tg_enabled:
            send_if_not_logged(db, row, due_date, "tg", lambda: send_telegram(db, message))
        if email_enabled:
            send_if_not_logged(db, row, due_date, "email", lambda: send_email(db, message))


def send_if_not_logged(db, row, due_date, channel, sender):
    exists = db.execute(
        "SELECT 1 FROM reminder_log WHERE subscription_id = ? AND due_date = ? AND channel = ?",
        (row["id"], due_date.strftime("%Y-%m-%d"), channel),
    ).fetchone()
    if exists:
        return
    success, _ = sender()
    if success:
        db.execute(
            "INSERT INTO reminder_log (subscription_id, due_date, channel, sent_at) VALUES (?, ?, ?, ?)",
            (row["id"], due_date.strftime("%Y-%m-%d"), channel, now_iso()),
        )
        db.commit()


def format_reminder_message(row, due_date, left_days, amount_cny):
    amount_display = f"{row['amount']} {row['currency']}"
    if amount_cny is not None:
        amount_display += f"（约 ¥{amount_cny:.2f}）"
    flow_label = FLOW_LABELS.get(row["flow"] or "expense", "支出")
    return (
        f"订阅续费提醒\n"
        f"服务：{row['name']}\n"
        f"类型：{flow_label}\n"
        f"到期：{due_date.strftime('%Y-%m-%d')}（剩余 {left_days} 天）\n"
        f"金额：{amount_display}\n"
        f"续费地址：{row['renew_url'] or '未填写'}"
    )


def send_telegram(db, message):
    token = get_setting(db, "tg_bot_token", "")
    chat_id = get_setting(db, "tg_chat_id", "")
    if not token or not chat_id:
        return False, "缺少 TG 配置"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        return True, None
    except Exception as exc:
        return False, str(exc)


def send_email(db, message):
    host = get_setting(db, "smtp_host", "")
    port = safe_int(get_setting(db, "smtp_port", "587"), 587)
    user = get_setting(db, "smtp_user", "")
    password = get_setting(db, "smtp_password", "")
    sender = get_setting(db, "smtp_sender", "") or user
    use_tls = as_bool(get_setting(db, "smtp_tls", "1"))

    if not host or not sender:
        return False, "缺少邮件配置"

    msg = MIMEText(message, _charset="utf-8")
    msg["Subject"] = "订阅续费提醒"
    msg["From"] = sender
    msg["To"] = sender

    try:
        server = smtplib.SMTP(host, port, timeout=10)
        if use_tls:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as exc:
        return False, str(exc)


def send_test_notification(db, channel):
    message = "订阅提醒测试：配置成功。"
    if channel == "tg":
        return send_telegram(db, message)
    if channel == "email":
        return send_email(db, message)
    return False, "未知通道"


def sort_items(items, sort_key):
    if sort_key == "remaining":
        return sorted(items, key=lambda item: item["remaining_days"])
    if sort_key == "amount":
        return sorted(
            items,
            key=lambda item: (item["amount_cny"] is None, -(item["amount_cny"] or 0)),
        )
    if sort_key == "monthly":
        return sorted(
            items,
            key=lambda item: (
                item["monthly_equiv_cny"] is None,
                -(item["monthly_equiv_cny"] or 0),
            ),
        )
    if sort_key == "yearly":
        return sorted(
            items,
            key=lambda item: (
                item["yearly_equiv_cny"] is None,
                -(item["yearly_equiv_cny"] or 0),
            ),
        )
    if sort_key == "name":
        return sorted(items, key=lambda item: item["name"].lower())
    return sorted(items, key=lambda item: item["due_date"])


def ics_timestamp():
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def escape_ics(value):
    if value is None:
        return ""
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def rrule_for_cycle(cycle):
    mapping = {
        "day": "FREQ=DAILY;INTERVAL=1",
        "week": "FREQ=WEEKLY;INTERVAL=1",
        "month": "FREQ=MONTHLY;INTERVAL=1",
        "quarter": "FREQ=MONTHLY;INTERVAL=3",
        "halfyear": "FREQ=MONTHLY;INTERVAL=6",
        "year": "FREQ=YEARLY;INTERVAL=1",
        "2year": "FREQ=YEARLY;INTERVAL=2",
        "3year": "FREQ=YEARLY;INTERVAL=3",
        "4year": "FREQ=YEARLY;INTERVAL=4",
        "5year": "FREQ=YEARLY;INTERVAL=5",
    }
    return mapping.get(cycle)


HEADER_ALIASES = {
    "name": "name",
    "service": "name",
    "服务名称": "name",
    "服务": "name",
    "category": "category",
    "分类": "category",
    "amount": "amount",
    "price": "amount",
    "金额": "amount",
    "currency": "currency",
    "币种": "currency",
    "billing_cycle": "billing_cycle",
    "cycle": "billing_cycle",
    "续费类型": "billing_cycle",
    "续费周期": "billing_cycle",
    "周期": "billing_cycle",
    "due_date": "due_date",
    "到期日期": "due_date",
    "到期时间": "due_date",
    "renew_date": "due_date",
    "renew_url": "renew_url",
    "续费地址": "renew_url",
    "续费链接": "renew_url",
    "flow": "flow",
    "type": "flow",
    "类型": "flow",
    "收支": "flow",
    "收益类型": "flow",
    "reminder_days": "reminder_days",
    "提醒提前天数": "reminder_days",
    "提醒天数": "reminder_days",
    "enabled": "enabled",
    "启用": "enabled",
    "是否启用": "enabled",
    "notes": "notes",
    "备注": "notes",
}

CURRENCY_ALIASES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "元": "CNY",
    "¥": "CNY",
    "￥": "CNY",
    "USD": "USD",
    "US$": "USD",
    "$": "USD",
    "美元": "USD",
    "HKD": "HKD",
    "HK$": "HKD",
    "港币": "HKD",
    "JPY": "JPY",
    "日元": "JPY",
    "EUR": "EUR",
    "欧元": "EUR",
    "GBP": "GBP",
    "英镑": "GBP",
    "AUD": "AUD",
    "澳元": "AUD",
    "SGD": "SGD",
    "新币": "SGD",
    "新加坡元": "SGD",
}

CYCLE_ALIASES = {
    "day": "day",
    "daily": "day",
    "天": "day",
    "日": "day",
    "week": "week",
    "weekly": "week",
    "周": "week",
    "month": "month",
    "monthly": "month",
    "月": "month",
    "quarter": "quarter",
    "季度": "quarter",
    "季": "quarter",
    "halfyear": "halfyear",
    "half-year": "halfyear",
    "半年": "halfyear",
    "year": "year",
    "annual": "year",
    "yearly": "year",
    "年": "year",
    "2year": "2year",
    "2-year": "2year",
    "两年": "2year",
    "2年": "2year",
    "3year": "3year",
    "3-year": "3year",
    "三年": "3year",
    "3年": "3year",
    "4year": "4year",
    "4-year": "4year",
    "四年": "4year",
    "4年": "4year",
    "5year": "5year",
    "5-year": "5year",
    "五年": "5year",
    "5年": "5year",
}

FLOW_ALIASES = {
    "expense": "expense",
    "cost": "expense",
    "支出": "expense",
    "支出类": "expense",
    "消费": "expense",
    "income": "income",
    "revenue": "income",
    "收益": "income",
    "收入": "income",
    "出租": "income",
}


def decode_csv_bytes(raw):
    if raw is None:
        return None, None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def normalize_header_key(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def build_header_map(fieldnames):
    header_map = {}
    for name in fieldnames:
        key = normalize_header_key(name)
        canonical = HEADER_ALIASES.get(key)
        if canonical:
            header_map[name] = canonical
    return header_map


def normalize_row(raw_row, header_map):
    normalized = {}
    for key, value in raw_row.items():
        if key is None:
            continue
        canonical = header_map.get(key)
        if canonical:
            normalized[canonical] = value
    return normalized


def is_row_empty(row):
    if not row:
        return True
    for value in row.values():
        if value is None:
            continue
        if str(value).strip():
            return False
    return True


def parse_date_flexible(value):
    if value is None or str(value).strip() == "":
        raise ValueError("到期日期为空")
    text = str(value).strip()
    text = text.replace("/", "-").replace(".", "-")
    try:
        return parse_date(text)
    except ValueError:
        try:
            return date_parser.parse(text, yearfirst=True, dayfirst=False).date()
        except Exception as exc:
            raise ValueError("到期日期格式不正确") from exc


def normalize_cycle_value(value):
    if value is None or str(value).strip() == "":
        return "month"
    key = str(value).strip().lower()
    return CYCLE_ALIASES.get(key, key)


def normalize_flow(value):
    if value is None or str(value).strip() == "":
        return "expense"
    key = str(value).strip().lower()
    return FLOW_ALIASES.get(key, key)


def parse_amount(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalize_currency(value):
    if value is None:
        return "CNY"
    text = str(value).strip()
    if not text:
        return "CNY"
    key = text.replace(" ", "").upper()
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    return key


def parse_enabled(value):
    if value is None or str(value).strip() == "":
        return 1
    text = str(value).strip().lower()
    truthy = {"1", "true", "yes", "on", "是", "启用", "开启"}
    falsy = {"0", "false", "no", "off", "否", "停用", "禁用", "关闭"}
    if text in truthy:
        return 1
    if text in falsy:
        return 0
    return 1 if as_bool(value) else 0


def parse_reminder_days(value, default_days):
    if value is None or str(value).strip() == "":
        return default_days
    days = safe_int(value, default_days)
    return days if days >= 0 else default_days


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
