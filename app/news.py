import re
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from notion_client import Client
from app.config import settings

notion = Client(auth=settings.notion_token)


def _tokenize(text: str) -> set[str]:
    """
    提取 ASCII 英文詞 + 中文字元 bigram。
    避免中文無空格導致整句被視為一個 token 的問題。
    """
    tl = text.lower()
    # 英文詞（2字元以上）
    ascii_words = set(re.findall(r'[a-z]{2,}', tl))
    # 中文字元 bigram（相鄰兩字元）
    cjk_chars = re.findall(r'[一-鿿]', tl)
    bigrams = {cjk_chars[i] + cjk_chars[i + 1] for i in range(len(cjk_chars) - 1)}
    return ascii_words | bigrams

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
    from app.agent import agent

    text       = title + " " + description
    text_lower = text.lower()
    words      = _tokenize(text)   # ASCII詞 + 中文bigram

    # ── 1. Agentic RAG：語意檢索 ─────────────────────────
    _STOPWORDS_CONN = {
        'the','and','for','with','this','that','from','into','have','been',
        '供應','零件','使用','提供','支援','產品','規格','包含','適用',
        '相關','影響','導致','可能','風險','情況','進行','造成',
    }

    # 風險事件類型 → 具體影響說明模板
    _RTYPE_CONN = {
        "出口管制": lambda countries: f"{'、'.join(countries[:2]) or '出口國'} 出口管制直接波及此類供應鏈",
        "供應短缺": lambda countries: "均面臨供應短缺壓力，備料需求上升",
        "價格波動": lambda countries: "原物料漲價將連帶推高此類採購成本",
        "地緣政治": lambda countries: f"{'、'.join(countries[:2]) or '相關地區'} 地緣緊張可能中斷此類物流與供貨",
        "自然災害": lambda countries: f"災害事件可能波及{'、'.join(countries[:2]) or '當地'}供應商生產能力",
        "產能問題": lambda countries: "產能受限加劇此類元件缺料風險",
    }

    def _conn_reason(desc: str, cat: str, sem_score: float, ent: dict) -> str:
        """
        嚴謹的關聯說明：優先顯示直接證據，其次推論，
        最後才是類別通用描述，並標明語意強度。
        """
        rw        = _tokenize(desc)
        common    = (words & rw) - _STOPWORDS_CONN
        comp_hit  = sorted(common & _COMPONENT_TERMS)
        other_hit = sorted(w for w in common - _COMPONENT_TERMS if len(w) >= 2)

        pct  = round(sem_score * 100)
        qual = "高度" if pct >= 30 else "中度" if pct >= 12 else "低度"

        reasons: list[str] = []

        # ① 直接元件重疊（最強證據）
        if comp_hit:
            reasons.append(f"共同涉及元件「{'、'.join(t.upper() for t in comp_hit[:3])}」")

        # ② 有意義關鍵詞重疊
        if other_hit:
            reasons.append(f"共同關鍵詞「{'、'.join(other_hit[:3])}」")

        # ③ 無直接重疊 → 用感知實體推論
        if not reasons:
            news_comps    = set(ent.get("components", []))
            news_rtypes   = ent.get("risk_types", [])
            news_countries= [c.split()[-1] for c in ent.get("countries", [])]

            # 檢查 QA 描述中的元件是否與新聞感知到的元件同類
            qa_comps = {t for t in _COMPONENT_TERMS if t in desc.lower()}
            shared   = {t.upper() for t in news_comps} & {t.upper() for t in qa_comps}
            if shared:
                reasons.append(f"元件類型「{'、'.join(sorted(shared)[:2])}」風險共振")
            elif news_rtypes:
                fn = _RTYPE_CONN.get(news_rtypes[0])
                if fn:
                    reasons.append(fn(news_countries))

        if not reasons:
            # 完全無法找到具體關聯 → 誠實回報
            if pct < 10:
                return f"語意相似度 {pct}%，直接關聯依據不足，僅供參考"
            cat_generic = {
                "供應": "供應鏈短缺情境具潛在間接關聯",
                "市場": "市場波動情境具潛在間接關聯",
                "政策": "政策管制情境具潛在間接關聯",
                "地緣": "地緣政治緊張情境具潛在間接關聯",
                "自然": "自然災害情境具潛在間接關聯",
                "RAGQA": "知識庫條目與當前新聞情境具潛在關聯",
            }
            return f"{cat_generic.get(cat, '間接相關')}（語意 {pct}%）"

        reason_str = "；".join(reasons)
        return f"{reason_str}（語意 {pct}%，{qual}相關）"

    all_risks    = [r for r in fetch_risks() if r.description]
    ragqa_pool   = [r for r in all_risks if r.category.upper() == 'RAGQA']
    regular_pool = [r for r in all_risks if r.category in _CAT_KEYWORDS]

    # Agent 語意分析：實體提取 + TF-IDF 語意排名
    agent_result  = agent.analyze(title, description, ragqa_pool, regular_pool)
    entities      = agent_result["entities"]           # 感知到的實體
    scored_matches = agent_result["matched"]           # [(semantic_score, RiskItem)]

    matched_risks  = [item for _, item in scored_matches]
    score_map      = {id(item): score for score, item in scored_matches}

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
            note_words = _tokenize(kp.vendor_note)
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

    # ── 5. 綜合評分（嚴謹版） ─────────────────────────────
    n_direct    = sum(1 for p in affected_kp if p["direct"])
    order_bonus = min(len(affected_orders) * 3, 15)

    # RAG 品質加分：有高語意分數的命中才加分
    rag_scores     = [score_map.get(id(r), 0.0) for r in matched_risks]
    avg_rag_score  = (sum(rag_scores) / len(rag_scores)) if rag_scores else 0.0
    rag_bonus      = round(avg_rag_score * 15)   # 最多 +15（平均語意 100% 時）
    has_ragqa_hit  = any(r.category.upper() == "RAGQA" for r in matched_risks)
    ragqa_bonus    = 5 if has_ragqa_hit else 0

    raw_total = min(base + n_direct * 10 + order_bonus + rag_bonus + ragqa_bonus, 99)

    # 無任何直接命中且 RAG 語意低 → 分數收斂至較低水準
    if n_direct == 0 and avg_rag_score < 0.10:
        raw_total = min(raw_total, base + 10)

    overall = round(raw_total / 100, 2)

    score_parts = [f"分類基礎（{category}）{base} 分"]
    if n_direct:
        score_parts.append(f"廠商直接命中 +{n_direct * 10}（{n_direct} 項）")
    if order_bonus:
        score_parts.append(f"工單衝擊 +{order_bonus}（{len(affected_orders)} 張）")
    if rag_bonus:
        score_parts.append(f"RAG 語意品質 +{rag_bonus}（平均 {round(avg_rag_score*100)}%）")
    if ragqa_bonus:
        score_parts.append(f"RAGQA 知識命中 +{ragqa_bonus}")

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
        "entities": entities,
        "matched_risks": [
            {"event_id":       r.event_id,
             "category":       r.category,
             "is_ragqa":       r.category.upper() == "RAGQA",
             "risk_score":     r.risk_score,
             "semantic_score": round(score_map.get(id(r), 0.0), 3),
             "description":    r.description[:160],
             "connection":     _conn_reason(r.description, r.category,
                                            score_map.get(id(r), 0.0), entities)}
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
