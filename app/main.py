import importlib
import pkgutil
from fastapi import FastAPI
from app.config import settings
from app.database import db
from app.plugins.base import LifeOSPlugin

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        docs_url="/docs",
        redoc_url=None
    )

    # Initialize core database tables
    db.init_db()

    # Discover and load plugins dynamically
    load_plugins(app)

    @app.get("/")
    def root():
        return {
            "system": settings.PROJECT_NAME,
            "status": "active",
            "plugins_loaded": list(app.state.plugins.keys())
        }

    return app

def load_plugins(app: FastAPI):
    app.state.plugins = {}
    plugins_dir = settings.PLUGINS_DIR

    # Ensure plugins directory exists
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Iterate over packages in app.plugins
    for _, module_name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if module_name == "base":
            continue
        try:
            module = importlib.import_module(f"app.plugins.{module_name}")
            for attribute_name in dir(module):
                attribute = getattr(module, attribute_name)
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, LifeOSPlugin)
                    and attribute is not LifeOSPlugin
                ):
                    plugin_instance = attribute()
                    # Init plugin database tables
                    with db.get_connection() as conn:
                        plugin_instance.init_tables(conn)
                    
                    # Register routes
                    router = plugin_instance.register_routes()
                    app.include_router(router, prefix=f"/api/{plugin_instance.name}", tags=[plugin_instance.name])
                    
                    app.state.plugins[plugin_instance.name] = plugin_instance
        except Exception as e:
            print(f"Failed to load plugin {module_name}: {e}")
