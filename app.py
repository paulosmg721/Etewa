import click
from flask import Flask, render_template
from sqlalchemy import event

from auth import auth, LoginLimiter
from config import Config
from delivery import delivery_api
from models import db
from trips import COUNTRY_CODES, trip_api


def enable_sqlite_foreign_keys(connection, _connection_record):
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def create_app(test_config=None, *, instance_path=None):
    app = Flask(__name__, instance_path=instance_path)
    app.config.from_object(Config)
    if test_config is not None:
        app.config.update(test_config)

    db.init_app(app)
    app.extensions["etewa_login_limiter"] = LoginLimiter(
        app.config["LOGIN_RATE_LIMIT"], app.config["LOGIN_RATE_WINDOW_SECONDS"]
    )
    app.register_blueprint(auth)
    app.register_blueprint(trip_api)
    app.register_blueprint(delivery_api)
    with app.app_context():
        if db.engine.dialect.name == "sqlite":
            event.listen(db.engine, "connect", enable_sqlite_foreign_keys)

    @app.get("/")
    def homepage():
        return render_template("index.html", country_codes=" ".join(COUNTRY_CODES))

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.cli.command("init-db")
    def init_db():
        """Create missing tables while retaining existing records."""
        db.create_all()
        click.echo("Database tables are ready.")

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
