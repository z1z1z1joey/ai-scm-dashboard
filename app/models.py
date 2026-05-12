from pydantic import BaseModel

class RiskItem(BaseModel):
    page_id: str = ""
    event_id: str = ""
    title: str = ""
    risk_score: float = 0
    category: str = ""
    source: str = ""
    description: str = ""
    impacted_parts: list[str] = []
    impacted_orders: list[str] = []

class KeyPart(BaseModel):
    page_id: str = ""
    gbt_pn: str = ""
    vendor: str = ""
    unit_price: float = 0
    vendor_note: str = ""

class Order(BaseModel):
    page_id: str = ""
    order_id: str = ""
    sku_pn: str = ""
    quantity: int = 0
    country: str = ""
    ship_date: str = ""
    status: str = ""
    bom_count: int = 0          # 該 SKU 的 BOM 零件數（後端計算）

class BomItem(BaseModel):
    page_id: str = ""
    sku_pn: str = ""
    gbt_pn: str = ""
    vendor: str = ""            # 從 Keyparts join
    unit_price: float = 0       # 從 Keyparts join
    vendor_note: str = ""       # 從 Keyparts join

class ActionLog(BaseModel):
    page_id: str = ""
    action_id: str = ""
    gbt_pn: str = ""
    vendor: str = ""            # 從 Keyparts join
    unit_price: float = 0       # 從 Keyparts join
    suggested_qty: int = 0
    total_cost: float = 0       # unit_price × qty
    status: str = "Pending"
    review_time: str = ""       # last_edited_time from Notion (ISO 8601)
    trigger: str = ""           # 觸發此決策的新聞標題
    affected_skus: list[str] = []    # 從 BOM 追蹤
    affected_orders: list[str] = []  # 從 Orders 追蹤
    affected_qty: int = 0            # 受影響工單總量

class RiskResponse(BaseModel):
    total: int
    results: list[RiskItem]

class KeyPartResponse(BaseModel):
    total: int
    results: list[KeyPart]

class OrderResponse(BaseModel):
    total: int
    results: list[Order]

class BomResponse(BaseModel):
    total: int
    results: list[BomItem]

class ActionResponse(BaseModel):
    total: int
    results: list[ActionLog]
