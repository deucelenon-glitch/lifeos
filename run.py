from fastapi import FastAPI
from app.main import create_app
import uvicorn

app = create_app()

if __name__ == "__main__":
    uvicorn.run("run:app", host="0.0.0.0", port=8080, reload=True)
