"""后端启动入口：python run.py （在 backend 目录下运行）。"""
import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
