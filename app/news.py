import re
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from notion_client import Client
from app.config import settings

notion = Client(auth=settings.notion_token)

FEEDS = [
    # ── 國際供應鏈 ──────────────────────────────────────
    ("半導體供應鏈", "https://news.google.com/rss/search?q=semiconductor+supply+chain+shortage+risk&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("原物料漲價",   "https://news.google.com/rss/search?q=原物料+漲價+供應鏈+2026&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ("地緣政治風險", "https://news.google.com/rss/search?q=geopolitical+risk+chip+semiconductor+tariff&hl=en-US&gl=US&ceid=US:en"),
    ("Reuters Tech", "https://feeds.reuters.com/reuters/technologyNews"),
    ("Yahoo 半導體", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SOXX&region=US&lang=en-US"),
    ("Yahoo 供應鏈", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TSM&region=US&lang=en-US"),
    # ── 台灣綜合新聞 ─────────────────────────────────────
    ("聯合新聞網",    "https://udn.com/rssfeed/news/2/6638?ch=news"),
    ("中時新聞網",    "https://www.chinatimes.com/feeds?cat=finance"),
    ("Yahoo奇摩新聞", "https://tw.news.yahoo.com/rss/finance"),
    # ── 台灣財經新聞 ─────────────────────────────────────
    ("自由財經",      "https://ec.ltn.com.tw/rss/"),
    ("經濟日報",      "https://money.udn.com/rssfeed/news/1001/5588?ch=money"),
    ("工商時報",      "https://ctee.com.tw/feed"),
    ("鉅亨網 Anue",   "https://news.cnyes.com/rss/category/important_stock"),
    # ── 即時 / 新媒體 ────────────────────────────────────
    ("ETtoday財經",   "https://www.ettoday.net/news/rss/news.rss"),
    ("關鍵評論網",    "https://www.thenewslens.com/rss"),
    # ── Google 新聞聚合（台灣財經） ──────────────────────
    ("Google 台灣財經", "https://news.google.com/rss/search?q=台灣+供應鏈+半導體+財經&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
]

# 元件/產品型號關鍵字 — 命中時在 RAG 比對中大幅加分
_COMPONENT_TERMS = {
    # 儲存元件
    "ssd", "hdd", "nand", "dram", "nor", "eeprom", "flash", "hbm", "emmc",
    # 半導體 / 晶片
    "cpu", "gpu", "mcu", "fpga", "asic", "soc", "apu", "pmic",
    # 被動元件
    "mlcc", "capacitor", "resistor", "inductor", "mosfet",
    # 連接 / 板材
    "pcb", "connector", "ic",
    # 台灣慣用中文詞
    "快閃", "記憶體", "晶片", "晶圓", "半導體", "固態",
    # 規格型號慣用
    "ddr", "lpddr", "gddr",
}

CATEGORY_KEYWORDS = {
    "政策": ["tariff", "sanction", "ban", "regulation", "政策", "關稅", "制裁", "禁令", "出口管制", "法規", "監管", "合規", "貿易壁壘"],
    "市場": ["price", "demand", "market", "漲價", "需求", "市場", "供需", "成本", "原物料", "物價", "漲幅", "跌價"],
    "地緣": ["geopolit", "war", "conflict", "china", "taiwan", "地緣", "戰爭", "衝突", "中美", "貿易戰", "俄烏", "台海"],
    "自然": ["earthquake", "typhoon", "flood", "disaster", "地震", "颱風", "洪水", "暴雨", "災害", "停工", "天災"],
    "供應": ["shortage", "capacity", "lead time", "缺貨", "產能", "交期", "缺料", "備料", "斷鏈", "庫存", "斷供", "缺芯"],
}

def _guess_category(text: str) -> str:
    text_lower = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return cat
    return "新聞"

def _guess_score(text: str) -> float:
    text_lower = text.lower()
    high = ["critical", "severe", "major", "crisis", "緊急", "嚴重", "危機", "崩潰", "暴跌", "大幅下滑", "斷供", "停擺"]
    med  = ["risk", "concern", "warning", "alert", "風險", "警示", "注意", "警戒", "波動", "影響", "衝擊", "調漲"]
    if any(k in text_lower for k in high): return 0.85
    if any(k in text_lower for k in med):  return 0.60
    return 0.40

async def fetch_news(max_per_feed: int = 8, feed_idx: int = -1) -> list[dict]:
    """Fetch RSS news. feed_idx=-1 fetches all feeds; 0-N fetches a specific feed."""
    feeds = [FEEDS[feed_idx]] if 0 <= feed_idx < len(FEEDS) else FEEDS
    fetch_all = feed_idx == -1
    now = datetime.now(timezone.utc)
    cutoff_14 = now - timedelta(days=14)
    cutoff_21 = now - timedelta(days=21)
    fresh: list[dict] = []
    overflow_ts: list[tuple] = []  # (timestamp, entry) for items 14-21 days old
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        for source, url in feeds:
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
                    if not title:
                        continue
                    desc = re.sub(r"<[^>]+>", "", desc)[:300]
                    combined = title + " " + desc
                    entry = {
                        "title":              title,
                        "description":        desc,
                        "source":             source,
                        "link":               link,
                        "pub_date":           pub,
                        "suggested_category": _guess_category(combined),
                        "suggested_score":    _guess_score(combined),
                    }
                    if pub:
                        pub_dt = _parse_pub_date(pub)
                        if pub_dt:
                            aware = pub_dt if pub_dt.tzinfo else pub_dt.replace(tzinfo=timezone.utc)
                            if aware >= cutoff_14:
                                fresh.append(entry)
                            elif aware >= cutoff_21:
                                overflow_ts.append((aware.timestamp(), entry))
                        else:
                            fresh.append(entry)
                    else:
                        fresh.append(entry)
            except Exception:
                continue
    # Guarantee minimum 4 items when fetching all feeds
    if fetch_all and len(fresh) < 4 and overflow_ts:
        overflow_ts.sort(key=lambda x: x[0], reverse=True)
        needed = 4 - len(fresh)
        fresh.extend(entry for _, entry in overflow_ts[:needed])
    return fresh

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

def _parse_pub_date(pub_str: str):
    """Parse RSS pubDate string to timezone-aware datetime, or None on failure."""
    try:
        return parsedate_to_datetime(pub_str)
    except Exception:
        return None

def cleanup_old_news() -> int:
    """Archive Notion '新聞' category entries older than 14 days. Returns deleted count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
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
    from app.notion import fetch_risks, fetch_keyparts, fetch_enriched

    text       = title + " " + description
    text_lower = text.lower()
    words      = set(re.findall(r'[\w一-鿿]{2,}', text_lower))

    # ── 1. RAG 知識庫：關鍵字重疊比對 ───────────────────
    _STOPWORDS_CONN = {
        'the','and','for','with','this','that','from','into','have','been',
        '供應','零件','使用','提供','支援','產品','規格','包含','適用',
    }
    _CAT_CONN = {
        "供應": "均屬供應鏈短缺/交期延遲類風險",
        "市場": "均涉及市場價格波動或採購成本上升",
        "政策": "均涉及政策法規或關稅管制影響",
        "地緣": "均屬地緣政治緊張相關風險",
        "自然": "均涉及自然災害或不可抗力事件",
    }

    def _conn_reason(desc: str, cat: str) -> str:
        rw = set(re.findall(r'[\w一-鿿]{2,}', desc.lower()))
        common = (words & rw) - _STOPWORDS_CONN
        comp_hit = common & _COMPONENT_TERMS
        other_hit = common - _COMPONENT_TERMS
        parts = []
        if comp_hit:
            parts.append(f"零件品項「{'、'.join(t.upper() for t in list(comp_hit)[:3])}」")
        if other_hit:
            parts.append(f"關鍵詞「{'、'.join(list(other_hit)[:3])}」")
        if parts:
            return "新聞與此事件均涉及" + "與".join(parts)
        return _CAT_CONN.get(cat, "風險類別與新聞主題相符")

    def _overlap(r):
        rw = set(re.findall(r'[\w一-鿿]{2,}', r.description.lower()))
        base = len(words & rw)
        comp = len((words & rw) & _COMPONENT_TERMS)
        return base + comp * 2

    all_risks    = [r for r in fetch_risks() if r.description]
    # RAGQA = 大腦核心知識（大小寫不敏感），保留獨立 2 個位置確保一定出現
    ragqa_pool   = [r for r in all_risks if r.category.upper() == 'RAGQA']
    regular_pool = [r for r in all_risks if r.category in _CAT_KEYWORDS]

    ragqa_top   = sorted(ragqa_pool,   key=_overlap, reverse=True)[:2]
    regular_top = sorted(regular_pool, key=_overlap, reverse=True)[:3]

    # 合併：RAGQA 優先，去除重複，上限 5 筆
    seen = {r.event_id for r in ragqa_top}
    matched_risks = ragqa_top + [r for r in regular_top if r.event_id not in seen]
    matched_risks = matched_risks[:5]

    # ── 2. 分類基礎分 ────────────────────────────────────
    CAT_BASE = {"自然": 75, "供應": 70, "政策": 65, "地緣": 60, "市場": 55, "新聞": 35}
    base = CAT_BASE.get(category, 40)

    # ── 3. 零件衝擊分析（含明確原因） ────────────────────
    kp_list = fetch_keyparts()
    _STOPWORDS = {
        'the','and','for','with','this','that','from','into','have','been',
        '供應','零件','使用','提供','支援','產品','規格','包含','適用',
    }
    _CAT_REASON = {
        "供應": "此類供應鏈短缺風險預計波及同類零件備料",
        "市場": "原物料漲價將直接推高此零件採購成本",
        "政策": "關稅或出口管制措施波及供應商所在地區",
        "地緣": "地緣政治緊張威脅供應商正常出貨能力",
        "自然": "自然災害可能導致供應商廠區停工或物流中斷",
    }

    affected_kp = []
    for kp in kp_list:
        reasons, bonus = [], 0

        # 廠商名稱直接命中（最強訊號）
        if kp.vendor and kp.vendor.lower() in text_lower:
            reasons.append(f"供應商「{kp.vendor}」名稱直接見於新聞")
            bonus += 30

        # 料號直接命中
        if kp.gbt_pn and kp.gbt_pn.lower() in text_lower:
            reasons.append(f"料號 {kp.gbt_pn} 直接出現於新聞")
            bonus += 40

        # 零件描述關鍵詞重疊（至少 2 個有意義詞）
        if kp.vendor_note:
            note_words = set(re.findall(r'[\w一-鿿]{2,}', kp.vendor_note.lower()))
            overlap = (words & note_words) - _STOPWORDS
            if len(overlap) >= 2:
                kw_str = '、'.join(list(overlap)[:3])
                reasons.append(f"零件用料「{kw_str}」與新聞主題高度關聯")
                bonus += min(len(overlap) * 5, 20)

        # 分類通用影響原因
        if category in _CAT_REASON:
            reasons.append(_CAT_REASON[category])
            bonus += 10

        if reasons:
            affected_kp.append({
                "page_id":      kp.page_id,
                "gbt_pn":       kp.gbt_pn,
                "vendor":       kp.vendor,
                "unit_price":   kp.unit_price,
                "vendor_note":  kp.vendor_note[:80],
                "reasons":      reasons,
                "impact_score": min(base + bonus, 99),
                "direct":       bonus >= 30,
            })

    affected_kp.sort(key=lambda x: (x["direct"], x["impact_score"]), reverse=True)

    # 若完全無直接命中，補上高單價零件（間接風險示警）
    if not any(x["direct"] for x in affected_kp) and kp_list:
        for kp in sorted(kp_list, key=lambda k: k.unit_price, reverse=True)[:2]:
            if not any(x["gbt_pn"] == kp.gbt_pn for x in affected_kp):
                cat_r = _CAT_REASON.get(category, "整體供應鏈波動間接影響此高價值零件")
                affected_kp.append({
                    "page_id":      kp.page_id,
                    "gbt_pn":       kp.gbt_pn,
                    "vendor":       kp.vendor,
                    "unit_price":   kp.unit_price,
                    "vendor_note":  kp.vendor_note[:80],
                    "reasons":      [cat_r],
                    "impact_score": base,
                    "direct":       False,
                })
    affected_kp = affected_kp[:5]

    # ── 4. BOM → SKU → 工單追蹤 ──────────────────────────
    orders_list, bom_items, _ = fetch_enriched()
    affected_pns  = {p["gbt_pn"] for p in affected_kp}
    affected_skus = list({b.sku_pn for b in bom_items if b.gbt_pn in affected_pns and b.sku_pn})
    affected_orders = [o for o in orders_list if o.sku_pn in affected_skus]
    total_qty = sum(o.quantity for o in affected_orders)

    # ── 5. 綜合評分（有據可查） ───────────────────────────
    n_direct    = sum(1 for p in affected_kp if p["direct"])
    order_bonus = min(len(affected_orders) * 3, 15)
    raw_total   = min(base + n_direct * 10 + order_bonus, 99)
    overall     = round(raw_total / 100, 2)

    score_parts = [f"分類基礎（{category}）{base} 分"]
    if n_direct:
        score_parts.append(f"廠商直接命中 +{n_direct * 10}（{n_direct} 項）")
    if order_bonus:
        score_parts.append(f"工單衝擊加分 +{order_bonus}（{len(affected_orders)} 張）")

    rec = (
        f"綜合評分 {raw_total} 分（{'、'.join(score_parts)}）。"
        f"影響 {len(affected_kp)} 項關鍵零件、{len(affected_orders)} 張工單"
        f"（總需求量 {total_qty:,} pcs）。{_CAT_ADVICE.get(category, '')}"
    )

    # ── 6. 建議採購行動 ───────────────────────────────────
    top = next((p for p in affected_kp if p["direct"]), affected_kp[0] if affected_kp else None)
    suggested_action = None
    if top and overall >= 0.4:
        kp_obj = next((k for k in kp_list if k.gbt_pn == top["gbt_pn"]), None)
        if kp_obj:
            qty = max(50, min(total_qty // 5, 5000)) if total_qty else 200
            suggested_action = {
                "gbt_pn":          kp_obj.gbt_pn,
                "vendor":          kp_obj.vendor,
                "unit_price":      kp_obj.unit_price,
                "keypart_page_id": kp_obj.page_id,
                "suggested_qty":   qty,
                "estimated_cost":  round(kp_obj.unit_price * qty, 2),
            }

    return {
        "ragqa_total": len(ragqa_pool),
        "matched_risks": [
            {"event_id":    r.event_id,
             "category":    r.category,
             "is_ragqa":    r.category.upper() == "RAGQA",
             "risk_score":  r.risk_score,
             "description": r.description[:160],
             "connection":  _conn_reason(r.description, r.category)}
            for r in matched_risks
        ],
        "affected_parts":   affected_kp,
        "affected_skus":    affected_skus,
        "affected_orders":  [
            {"order_id": o.order_id, "sku_pn": o.sku_pn,
             "quantity": o.quantity, "country": o.country}
            for o in affected_orders[:6]
        ],
        "total_impact_qty": total_qty,
        "recommendation":   rec,
        "score_breakdown":  score_parts,
        "overall_score":    overall,
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
    if trigger_title:
        props["Trigger"] = {"rich_text": [{"text": {"content": trigger_title[:200]}}]}
    try:
        page = notion.pages.create(
            parent={"database_id": settings.action_db_id},
            properties=props,
        )
    except Exception:
        props.pop("Trigger", None)
        page = notion.pages.create(
            parent={"database_id": settings.action_db_id},
            properties=props,
        )
    return {"action_id": action_id, "page_id": page["id"], "trigger": trigger_title}
