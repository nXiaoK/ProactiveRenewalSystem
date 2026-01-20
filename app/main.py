import csv
import io
import os
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import requests
from apscheduler.schedulers.background import BackgroundScheduler
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
            "total_cny": 0.0,
            "monthly_cny": 0.0,
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
                    stats["total_cny"] += item["amount_cny"]
                    stats["monthly_cny"] += item["monthly_equiv_cny"]
                if item["due_soon"]:
                    stats["due_soon"] += 1

        categories_sorted = sorted(categories)
        return render_template(
            "index.html",
            subscriptions=items,
            categories=categories_sorted,
            current_category=category_filter,
            status_filter=status_filter,
            search=search,
            stats=stats,
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

            if billing_cycle not in CYCLE_OPTIONS:
                flash("续费类型不合法", "error")
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
                (name, category, amount, currency, billing_cycle, due_date, renew_url, reminder_days, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    category,
                    amount,
                    currency,
                    billing_cycle,
                    due_date,
                    renew_url,
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

        return render_template(
            "subscription_form.html",
            subscription=None,
            cycle_labels=CYCLE_LABELS,
            cycle_options=CYCLE_OPTIONS,
            currencies=DEFAULT_CURRENCIES,
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

            if billing_cycle not in CYCLE_OPTIONS:
                flash("续费类型不合法", "error")
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
                    renew_url = ?, reminder_days = ?, enabled = ?, notes = ?, updated_at = ?
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

        return render_template(
            "subscription_form.html",
            subscription=row,
            cycle_labels=CYCLE_LABELS,
            cycle_options=CYCLE_OPTIONS,
            currencies=DEFAULT_CURRENCIES,
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

    @app.route("/import", methods=["GET", "POST"])
    def import_csv():
        if request.method == "POST":
            upload = request.files.get("file")
            if upload is None:
                flash("请选择要导入的 CSV", "error")
                return redirect(url_for("import_csv"))
            content = upload.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(content))
            count = 0
            now = now_iso()
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                due_date = (row.get("due_date") or "").strip()
                try:
                    parse_date(due_date)
                except ValueError:
                    continue
                billing_cycle = (row.get("billing_cycle") or "month").strip()
                if billing_cycle not in CYCLE_OPTIONS:
                    continue
                enabled_raw = row.get("enabled")
                if enabled_raw is None or str(enabled_raw).strip() == "":
                    enabled_value = 1
                else:
                    enabled_value = 1 if as_bool(enabled_raw) else 0

                g.db.execute(
                    """
                    INSERT INTO subscriptions
                    (name, category, amount, currency, billing_cycle, due_date, renew_url, reminder_days, enabled, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        (row.get("category") or "").strip(),
                        safe_float(row.get("amount"), 0.0),
                        (row.get("currency") or "CNY").strip().upper(),
                        billing_cycle,
                        due_date,
                        (row.get("renew_url") or "").strip(),
                        safe_int(row.get("reminder_days"), 7),
                        enabled_value,
                        (row.get("notes") or "").strip(),
                        now,
                        now,
                    ),
                )
                count += 1
            g.db.commit()
            flash(f"已导入 {count} 条订阅", "success")
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
    amount_cny = convert_to_cny(row["amount"], row["currency"], fx_rates)
    left_days = remaining_days(due_date, today)
    total_days = cycle_length_days(due_date, row["billing_cycle"])
    remaining_cny = remaining_value(amount_cny, due_date, row["billing_cycle"], today)
    monthly_equiv_cny = None
    if amount_cny is not None:
        monthly_equiv_cny = amount_cny / total_days * 30

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
        "reminder_days": reminder_days,
        "remaining_days": left_days,
        "remaining_value": remaining_cny,
        "enabled": bool(row["enabled"]),
        "notes": row["notes"],
        "due_soon": left_days <= reminder_days,
        "total_days": total_days,
        "monthly_equiv_cny": monthly_equiv_cny,
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
    return (
        f"订阅续费提醒\n"
        f"服务：{row['name']}\n"
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


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
