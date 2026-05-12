from notion_client import Client
from app.config import settings
from app.models import RiskItem, KeyPart, Order, BomItem, ActionLog

notion = Client(auth=settings.notion_token)

def _get_prop(prop: dict):
    if not prop:
        return ""
    t = prop.get("type", "")
    if t in ("title", "rich_text"):
        data = prop.get(t, [])
        return data[0].get("plain_text", "").strip() if data else ""
    if t == "number":
        return prop.get("number") or 0
    if t == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if t == "status":
        sel = prop.get("status")
        return sel.get("name", "") if sel else ""
    if t == "multi_select":
        return [i.get("name", "") for i in prop.get("multi_select", [])]
    if t == "relation":
        return [i.get("id", "") for i in prop.get("relation", [])]
    if t == "date":
        d = prop.get("date")
        return d.get("start", "") if d else ""
    if t == "checkbox":
        return prop.get("checkbox", False)
    if t == "formula":
        f = prop.get("formula", {})
        ft = f.get("type", "")
        return f.get(ft, 0)
    if t == "unique_id":
        uid = prop.get("unique_id", {})
        prefix = uid.get("prefix") or ""
        return f"{prefix}-{uid.get('number','')}" if prefix else str(uid.get("number", ""))
    return ""

def _query_all(db_id: str) -> list[dict]:
    pages, cursor = [], None
    while True:
        kwargs: dict = {"database_id": db_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages

# ── 基礎 fetch（無關聯） ────────────────────────────────

def fetch_risks() -> list[RiskItem]:
    items = []
    for page in _query_all(settings.risk_db_id):
        p = page.get("properties", {})
        event_id = str(_get_prop(p.get("Event_ID")) or _get_prop(p.get("Name")) or "")
        items.append(RiskItem(
            page_id=page.get("id", ""),
            event_id=event_id,
            title=str(_get_prop(p.get("Title")) or _get_prop(p.get("Name")) or ""),
            risk_score=float(_get_prop(p.get("Risk_Score")) or 0),
            category=str(_get_prop(p.get("Category"))),
            source=str(_get_prop(p.get("Source"))),
            description=str(_get_prop(p.get("Description")) or _get_prop(p.get("Content")) or ""),
            impacted_parts=_get_prop(p.get("Impacted_Parts")) or [],
            impacted_orders=_get_prop(p.get("Impacted_Orders")) or [],
        ))
    return items

def fetch_keyparts() -> list[KeyPart]:
    items = []
    for page in _query_all(settings.keypart_db_id):
        p = page.get("properties", {})
        items.append(KeyPart(
            page_id=page.get("id", ""),
            gbt_pn=str(_get_prop(p.get("GBT_PN"))),
            vendor=str(_get_prop(p.get("Vendor"))),
            unit_price=float(_get_prop(p.get("Unit_Price")) or 0),
            vendor_note=str(_get_prop(p.get("Vendor_Note"))),
        ))
    return items

# ── 串聯版 fetch（含 join 邏輯） ───────────────────────

def fetch_enriched():
    """
    一次載入全部資料，在記憶體內完成所有 join：
      Actions  →  Keyparts (relation)
      BOM.gbt_pn  →  Keyparts (text match)
      Orders.sku  →  BOM.parent_sku (text match)
    回傳 (orders, bom_items, actions) 已完成關聯的版本
    """
    kp_list = fetch_keyparts()
    kp_by_id  = {kp.page_id: kp for kp in kp_list}   # page_id → KeyPart
    kp_by_pn  = {kp.gbt_pn: kp for kp in kp_list}     # GBT_PN  → KeyPart

    # ── BOM (join Keyparts by GBT_PN) ──
    raw_bom: list[BomItem] = []
    for page in _query_all(settings.bom_db_id):
        p = page.get("properties", {})
        gbt_pn = str(_get_prop(p.get("Child_PN")))
        sku_pn = str(_get_prop(p.get("Parent_SKU")))
        kp = kp_by_pn.get(gbt_pn)
        raw_bom.append(BomItem(
            page_id=page.get("id", ""),
            sku_pn=sku_pn,
            gbt_pn=gbt_pn,
            vendor=kp.vendor if kp else "",
            unit_price=kp.unit_price if kp else 0,
            vendor_note=kp.vendor_note if kp else "",
        ))

    # sku → [gbt_pn, ...] 查詢表
    sku_to_parts: dict[str, list[str]] = {}
    for b in raw_bom:
        sku_to_parts.setdefault(b.sku_pn, []).append(b.gbt_pn)

    # gbt_pn → [sku_pn, ...] 查詢表
    pn_to_skus: dict[str, list[str]] = {}
    for b in raw_bom:
        pn_to_skus.setdefault(b.gbt_pn, []).append(b.sku_pn)

    # ── Orders (join BOM part count) ──
    raw_orders: list[Order] = []
    for page in _query_all(settings.order_db_id):
        p = page.get("properties", {})
        sku_pn = str(_get_prop(p.get("SKU")))
        raw_orders.append(Order(
            page_id=page.get("id", ""),
            order_id=str(_get_prop(p.get("Order_ID"))),
            sku_pn=sku_pn,
            quantity=int(_get_prop(p.get("Qty")) or 0),
            country=str(_get_prop(p.get("Country"))),
            bom_count=len(sku_to_parts.get(sku_pn, [])),
        ))

    # sku → [Order, ...] 查詢表
    sku_to_orders: dict[str, list[Order]] = {}
    for o in raw_orders:
        sku_to_orders.setdefault(o.sku_pn, []).append(o)

    # ── Actions (join Keyparts via relation, then trace BOM → Orders) ──
    raw_actions: list[ActionLog] = []
    for page in _query_all(settings.action_db_id):
        p = page.get("properties", {})
        kp_ids = _get_prop(p.get("Keyparts")) or []

        gbt_pn = vendor = ""
        unit_price = 0.0
        if kp_ids:
            kp = kp_by_id.get(kp_ids[0])
            if kp:
                gbt_pn, vendor, unit_price = kp.gbt_pn, kp.vendor, kp.unit_price

        qty = int(_get_prop(p.get("Suggested_Qty")) or 0)

        # 追蹤衝擊鏈
        affected_skus = list(set(pn_to_skus.get(gbt_pn, [])))
        affected_orders_obj = []
        for sku in affected_skus:
            affected_orders_obj.extend(sku_to_orders.get(sku, []))
        affected_order_ids = [o.order_id for o in affected_orders_obj]
        affected_qty = sum(o.quantity for o in affected_orders_obj)

        raw_actions.append(ActionLog(
            page_id=page.get("id", ""),
            action_id=str(_get_prop(p.get("Action_ID"))),
            gbt_pn=gbt_pn,
            vendor=vendor,
            unit_price=unit_price,
            suggested_qty=qty,
            total_cost=round(unit_price * qty, 2),
            status=str(_get_prop(p.get("Status"))),
            review_time=page.get("last_edited_time", ""),
            trigger=str(_get_prop(p.get("Trigger")) or ""),
            affected_skus=affected_skus,
            affected_orders=affected_order_ids,
            affected_qty=affected_qty,
        ))

    return raw_orders, raw_bom, raw_actions

def update_action(page_id: str, status: str) -> None:
    notion.pages.update(page_id=page_id, properties={
        "Status": {"select": {"name": status}},
    })
