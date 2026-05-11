import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from notion_client import Client
from app.config import settings

notion = Client(auth=settings.notion_token)

FEEDS = [
    ("半導體供應鏈", "https://news.google.com/rss/search?q=semiconductor+supply+chain+shortage+risk&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("原物料漲價",   "https://news.google.com/rss/search?q=原物料+漲價+供應鏈+2026&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("地緣政治風險", "https://news.google.com/rss/search?q=geopolitical+risk+chip+semiconductor+tariff&hl=en-US&gl=US&ceid=US:en"),
    ("Reuters Tech", "https://feeds.reuters.com/reuters/technologyNews"),
]

CATEGORY_KEYWORDS = {
    "政策": ["tariff", "sanction", "ban", "regulation", "政策", "關稅", "制裁", "禁令"],
    "市場": ["price", "demand", "market", "漲價", "需求", "市場", "供需"],
    "地緣": ["geopolit", "war", "conflict", "china", "taiwan", "地緣", "戰爭", "衝突"],
    "自然": ["earthquake", "typhoon", "flood", "disaster", "地震", "颱風", "洪水"],
    "供應": ["shortage", "capacity", "lead time", "shortage", "缺貨", "產能", "交期"],
}

def _guess_category(text: str) -> str:
    text_lower = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return "新聞"

def _guess_score(text: str) -> float:
    text_lower = text.lower()
    high = ["critical", "severe", "major", "crisis", "緊急", "嚴重", "危機"]
    med  = ["risk", "concern", "warning", "alert", "風險", "警示", "注意"]
    if any(k in text_lower for k in high): return 0.85
    if any(k in text_lower for k in med):  return 0.60
    return 0.40

async def fetch_news(max_per_feed: int = 5) -> list[dict]:
    results = []
    async with httpx.AsyncClient(verify=False, timeout=12) as client:
        for source, url in FEEDS:
            try:
                resp = await client.get(url)
                root = ET.fromstring(resp.text)
                channel = root.find("channel")
                if channel is None:
                    continue
                for item in list(channel.findall("item"))[:max_per_feed]:
                    title = (item.findtext("title") or "").strip()
                    desc  = (item.findtext("description") or "").strip()
                    link  = (item.findtext("link") or "").strip()
                    pub   = (item.findtext("pubDate") or "").strip()
                    # strip HTML tags from description
                    import re
                    desc = re.sub(r"<[^>]+>", "", desc)[:300]
                    if not title:
                        continue
                    results.append({
                        "title": title,
                        "description": desc,
                        "source": source,
                        "link": link,
                        "pub_date": pub,
                        "suggested_category": _guess_category(title + " " + desc),
                        "suggested_score": _guess_score(title + " " + desc),
                    })
            except Exception:
                continue
    return results

def _query_all_risk() -> list[dict]:
    pages, cursor = [], None
    while True:
        kwargs: dict = {"database_id": settings.risk_db_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages

def cleanup_old_news() -> int:
    """Archive Notion '新聞' category entries older than 7 days. Returns deleted count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    deleted = 0
    for page in _query_all_risk():
        p = page.get("properties", {})
        sel = p.get("Category", {}).get("select")
        if not sel or sel.get("name") != "新聞":
            continue
        created_str = page.get("created_time", "")
        if not created_str:
            continue
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created < cutoff:
            notion.pages.update(page_id=page["id"], archived=True)
            deleted += 1
    return deleted

def add_risk_to_notion(title: str, description: str, category: str, risk_score: float, source: str) -> dict:
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
    event_id = f"NEWS-{ts}"
    body = f"來源：{source}\n\n{description}"
    page = notion.pages.create(
        parent={"database_id": settings.risk_db_id},
        properties={
            "Event_ID":   {"title":     [{"text": {"content": event_id}}]},
            "Risk_Score": {"number":    risk_score},
            "Category":   {"select":    {"name": category}},
            "Description":{"rich_text": [{"text": {"content": body[:2000]}}]},
        },
    )
    return {"event_id": event_id, "page_id": page["id"]}


# ── 關鍵字與分類的風險對映 ────────────────────────────
_CAT_KEYWORDS = {
    "供應": {"shortage", "缺貨", "缺料", "供應鏈", "lead time", "交期", "產能", "capacity"},
    "市場": {"漲價", "price", "cost", "成本", "漲", "漲幅", "原物料", "材料"},
    "政策": {"tariff", "ban", "關稅", "禁令", "制裁", "sanction", "regulation", "合規"},
    "地緣": {"china", "taiwan", "中國", "台灣", "conflict", "戰爭", "war", "geopolit"},
    "自然": {"earthquake", "typhoon", "flood", "地震", "颱風", "洪水", "disaster", "災害"},
}

_CAT_ADVICE = {
    "供應": "供應中斷風險明確，建議立即評估緊急備料，優先針對高價值、長交期零件執行採購。",
    "市場": "原物料與市場價格波動預計推高製造成本，建議鎖定現有合約報價或向替代供應商詢價。",
    "政策": "政策/關稅風險可能導致供應商合規問題或進出口限制，建議評估替代供應鏈路徑。",
    "地緣": "地緣政治局勢惡化可能衝擊供應商正常運作，建議提前拉高安全庫存水位。",
    "自然": "自然災害風險可能造成工廠停工或物流中斷，建議啟動備援供應商機制。",
    "新聞": "建議持續追蹤此事件後續發展，評估對供應鏈的潛在衝擊。",
}


def evaluate_news_impact(title: str, description: str, category: str, risk_score: float) -> dict:
    """
    以外部風險 DB 作為 RAG 知識庫，比對新聞影響的零件與工單，並產生 AI 採購建議。
    """
    import re
    from app.notion import fetch_risks, fetch_keyparts, fetch_enriched

    text = (title + " " + description).lower()
    words = set(re.findall(r'[\w一-鿿]{2,}', text))

    # ── 1. RAG：從風險知識庫找最相關條目 ────────────────
    kb_risks = [r for r in fetch_risks()
                if r.category in _CAT_KEYWORDS and r.description]

    def _overlap(r):
        r_words = set(re.findall(r'[\w一-鿿]{2,}',
                                 (r.event_id + " " + r.description).lower()))
        return len(words & r_words)

    matched_risks = sorted(kb_risks, key=_overlap, reverse=True)[:3]

    # ── 2. 受影響零件 ─────────────────────────────────
    kp_list = fetch_keyparts()

    def _kp_score(kp):
        kp_text = (kp.vendor + " " + kp.vendor_note).lower()
        kp_words = set(re.findall(r'[\w一-鿿]{2,}', kp_text))
        overlap = len(words & kp_words)
        cat_bonus = 1 if any(kw in text for kw in _CAT_KEYWORDS.get(category, set())) else 0
        return overlap + cat_bonus

    n_show = 5 if risk_score >= 0.7 else 3
    affected_kp = sorted(kp_list, key=_kp_score, reverse=True)[:n_show]
    if not affected_kp:
        affected_kp = kp_list[:2]

    # ── 3. BOM → SKU → 工單 追蹤鏈 ────────────────────
    orders_list, bom_items, _ = fetch_enriched()
    affected_pns  = {kp.gbt_pn for kp in affected_kp}
    affected_skus = list({b.sku_pn for b in bom_items
                          if b.gbt_pn in affected_pns and b.sku_pn})
    affected_orders = [o for o in orders_list if o.sku_pn in affected_skus]
    total_qty = sum(o.quantity for o in affected_orders)

    # ── 4. 建議採購動作 ────────────────────────────────
    top_kp = max(affected_kp, key=lambda k: k.unit_price) if affected_kp else None
    suggested_action = None
    if top_kp and risk_score >= 0.4:
        qty = max(50, min(total_qty // 5, 5000)) if total_qty else 200
        suggested_action = {
            "gbt_pn":          top_kp.gbt_pn,
            "vendor":          top_kp.vendor,
            "unit_price":      top_kp.unit_price,
            "keypart_page_id": top_kp.page_id,
            "suggested_qty":   qty,
            "estimated_cost":  round(top_kp.unit_price * qty, 2),
        }

    sc = round(risk_score * 100)
    rec = (f"風險評分 {sc} 分，影響 {len(affected_kp)} 項關鍵零件、"
           f"{len(affected_orders)} 張工單（總需求量 {total_qty:,} pcs）。"
           f" {_CAT_ADVICE.get(category, _CAT_ADVICE['新聞'])}")

    return {
        "matched_risks": [
            {"event_id": r.event_id, "category": r.category,
             "risk_score": r.risk_score, "description": r.description[:160]}
            for r in matched_risks
        ],
        "affected_parts": [
            {"page_id": k.page_id, "gbt_pn": k.gbt_pn, "vendor": k.vendor,
             "unit_price": k.unit_price, "vendor_note": k.vendor_note[:80]}
            for k in affected_kp
        ],
        "affected_skus":   affected_skus,
        "affected_orders": [
            {"order_id": o.order_id, "sku_pn": o.sku_pn,
             "quantity": o.quantity, "country": o.country}
            for o in affected_orders[:6]
        ],
        "total_impact_qty": total_qty,
        "recommendation":   rec,
        "suggested_action": suggested_action,
    }


def create_action_from_news(gbt_pn: str, keypart_page_id: str,
                             suggested_qty: int, trigger_title: str) -> dict:
    ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
    action_id = f"ACT-N{ts}"
    props: dict = {
        "Action_ID":     {"title":  [{"text": {"content": action_id}}]},
        "Suggested_Qty": {"number": suggested_qty},
        "Status":        {"select": {"name": "Pending"}},
    }
    if keypart_page_id:
        props["Keyparts"] = {"relation": [{"id": keypart_page_id}]}
    page = notion.pages.create(
        parent={"database_id": settings.action_db_id},
        properties=props,
    )
    return {"action_id": action_id, "page_id": page["id"]}
