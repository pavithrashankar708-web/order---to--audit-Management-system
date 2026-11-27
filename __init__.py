import os, secrets
from functools import wraps
from flask import Flask, g, session, request, redirect, url_for, abort, flash
from . import db as DB, services as S
from .core import fmt_money, fmt_qty, fmt_bp, fmt_gst, plain_money

READONLY_ROLES = ("auditor", "owner")
NAV = [  # (endpoint, label, roles, icon)
    ("main.home", "Dashboard", ("admin", "supervisor", "owner", "auditor"), "◧"),
    ("main.order_new", "New order", ("salesman", "admin"), "＋"),
    ("main.orders", "Orders", ("salesman", "admin", "owner", "auditor"), "▤"),
    ("main.invoices", "Invoices & payments", ("admin", "owner", "auditor"), "₹"),
    ("main.production", "Production", ("supervisor", "admin", "owner", "auditor"), "⚙"),
    ("main.bom_list", "BOM", ("supervisor", "admin", "owner", "auditor"), "☰"),
    ("main.purchases", "Purchases", ("supervisor", "admin", "owner", "auditor"), "⬇"),
    ("main.stock", "Stock", ("supervisor", "admin", "owner", "auditor"), "▦"),
    ("main.trace_index", "Traceability", ("supervisor", "admin", "owner", "auditor"), "⛓"),
    ("main.reports_index", "Reports", ("supervisor", "admin", "owner", "auditor"), "▥"),
    ("main.aura_page", "AURA assistant", ("supervisor", "admin", "owner", "auditor"), "✦"),
]
SETTINGS = [("products", "Products"), ("customers", "Customers"), ("suppliers", "Suppliers"), ("materials", "Materials")]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
                      DATABASE=os.path.join(os.path.dirname(app.root_path), "instance", "sri_murugan.db"), CSRF=True)
    if config:
        app.config.update(config)
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    if not os.path.exists(app.config["DATABASE"]):
        from .seed import reset_database
        reset_database(app.config["DATABASE"], benchmark=False)   # masters + opening stock (walkthrough mode)

    @app.before_request
    def gate():
        if request.endpoint in (None, "static"):
            return
        g.db = DB.connect(app.config["DATABASE"])
        g.user = None
        if session.get("uid"):
            g.user = g.db.execute("SELECT * FROM users WHERE id=? AND active=1", (session["uid"],)).fetchone()
        if request.method == "POST" and app.config["CSRF"]:
            tok = session.get("csrf")
            if not tok or not secrets.compare_digest(tok, request.form.get("_csrf", "")):
                abort(400, "Invalid or missing CSRF token")
        if request.endpoint == "main.login":
            return
        if not g.user:
            return redirect(url_for("main.login"))
        if request.method == "POST" and g.user["role"] in READONLY_ROLES and request.endpoint != "main.logout":
            abort(403)

    @app.teardown_request
    def close(_):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def ctx():
        role = g.user["role"] if getattr(g, "user", None) else None
        return dict(user=g.get("user"), role=role, company=S.COMPANY, today=__import__("datetime").date.today().isoformat(),
                    nav=[n for n in NAV if role in n[2]], settings=SETTINGS if role and role != "salesman" else [],
                    csrf_token=lambda: session.setdefault("csrf", secrets.token_hex(16)))

    app.jinja_env.filters.update(money=fmt_money, qty=fmt_qty, bp=fmt_bp, gst=fmt_gst, rupees=plain_money)
    from .views import bp
    app.register_blueprint(bp)

    @app.errorhandler(403)
    def forbidden(e):
        return __import__("flask").render_template("error.html", code=403, msg="Your role does not have access to this page."), 403

    @app.errorhandler(404)
    def missing(e):
        return __import__("flask").render_template("error.html", code=404, msg="Page not found."), 404

    @app.errorhandler(400)
    def bad(e):
        return __import__("flask").render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    return app
