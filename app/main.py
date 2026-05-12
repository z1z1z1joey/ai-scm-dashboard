from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.models import RiskResponse, KeyPartResponse, OrderResponse, BomResponse, ActionResponse
from app.notion import fetch_risks, fetch_keyparts, fetch_enriched, update_action
import asyncio
from app.news import fetch_news, add_risk_to_notion, cleanup_old_news, evaluate_news_impact, create_action_from_news

app = FastAPI(title="AI智慧供應平台")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
async def index():
    return FileResponse("frontend/index.html")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/risks", response_model=RiskResponse)
async def get_risks():
    try:
        data = fetch_risks()
        return RiskResponse(total=len(data), results=data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/keyparts", response_model=KeyPartResponse)
async def get_keyparts():
    try:
        data = fetch_keyparts()
        return KeyPartResponse(total=len(data), results=data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders", response_model=OrderResponse)
async def get_orders():
    try:
        orders, _, _ = fetch_enriched()
        return OrderResponse(total=len(orders), results=orders)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bom", response_model=BomResponse)
async def get_bom():
    try:
        _, bom, _ = fetch_enriched()
        return BomResponse(total=len(bom), results=bom)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/actions", response_model=ActionResponse)
async def get_actions():
    try:
        _, _, actions = fetch_enriched()
        return ActionResponse(total=len(actions), results=actions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/news")
async def get_news(feed_idx: Optional[int] = Query(default=None)):
    try:
        loop = asyncio.get_event_loop()
        cleaned = 0
        if feed_idx is None:
            cleaned = await loop.run_in_executor(None, cleanup_old_news)
        idx = feed_idx if feed_idx is not None else -1
        data = await fetch_news(feed_idx=idx)
        return {"total": len(data), "results": data, "feed_idx": idx, "cleaned": cleaned}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class AddRiskRequest(BaseModel):
    title: str
    description: str
    category: str = "新聞"
    risk_score: float = 0.5
    source: str = ""

@app.post("/api/news/add-risk")
async def add_risk(body: AddRiskRequest):
    try:
        result = add_risk_to_notion(body.title, body.description, body.category, body.risk_score, body.source)
        return {"status": "ok", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class EvaluateRequest(BaseModel):
    title: str
    description: str = ""
    suggested_category: str = "新聞"
    suggested_score: float = 0.5

@app.post("/api/news/evaluate")
async def evaluate_news(body: EvaluateRequest):
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: evaluate_news_impact(
            body.title, body.description, body.suggested_category, body.suggested_score
        ))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CreateActionRequest(BaseModel):
    trigger_title: str = ""
    gbt_pn: str = ""
    keypart_page_id: str = ""
    suggested_qty: int = 100

@app.post("/api/news/create-action")
async def create_action(body: CreateActionRequest):
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: create_action_from_news(
            body.gbt_pn, body.keypart_page_id, body.suggested_qty, body.trigger_title
        ))
        return {"status": "ok", **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ReviewRequest(BaseModel):
    reviewer: str = "PM Team"
    reason: str = ""

@app.patch("/api/actions/{page_id}/approve")
async def approve_action(page_id: str, body: ReviewRequest):
    try:
        update_action(page_id, "Approved")
        return {"status": "ok", "message": "核准成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/actions/{page_id}/reject")
async def reject_action(page_id: str, body: ReviewRequest):
    if not body.reason:
        raise HTTPException(status_code=400, detail="駁回原因為必填")
    try:
        update_action(page_id, "Rejected")
        return {"status": "ok", "message": "已駁回"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
