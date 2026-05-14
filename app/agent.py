"""
Agentic RAG — RiskAssessmentAgent
  Step 1  Sensing Agent   : 從新聞文本提取實體（元件、國家、風險類型）
  Step 2  Semantic Retrieval: TF-IDF 字元 n-gram 語意相似度排名
  Step 3  Orchestrator    : 協調上游結果，回傳結構化分析
"""
from __future__ import annotations
import re
from typing import Any, Callable

# ── 元件 / 材料關鍵詞 ────────────────────────────────────────────────────────
_COMPONENT_TERMS = {
    "ssd", "hdd", "nand", "dram", "nor", "flash", "hbm", "emmc", "eeprom",
    "cpu", "gpu", "mcu", "fpga", "asic", "soc", "apu", "pmic",
    "mlcc", "capacitor", "resistor", "inductor", "mosfet", "pcb", "ic",
    "快閃", "記憶體", "晶片", "晶圓", "半導體", "固態",
    "ddr", "lpddr", "gddr",
    "鍺", "germanium", "鎵", "gallium", "銦", "indium",
    "稀土", "rare earth", "矽", "silicon", "wafer",
}

# ── 風險事件類型模式 ─────────────────────────────────────────────────────────
_RISK_EVENT_PATTERNS: dict[str, list[str]] = {
    "出口管制": ["出口限制", "禁令", "管制", "export ban", "restriction", "制裁", "sanction"],
    "供應短缺": ["短缺", "供應中斷", "shortage", "disruption", "斷料", "缺貨"],
    "價格波動": ["漲價", "價格上漲", "price surge", "inflation", "通膨", "成本上升"],
    "地緣政治": ["貿易戰", "地緣政治", "geopolitical", "關稅", "tariff", "衝突"],
    "自然災害": ["地震", "颱風", "洪水", "火災", "earthquake", "flood", "disaster"],
    "產能問題": ["產能", "良率", "停工", "capacity", "yield", "shutdown"],
}

# ── 國家 / 地區辨識 ──────────────────────────────────────────────────────────
_COUNTRY_MAP: dict[str, str] = {
    "中國": "🇨🇳 中國", "china": "🇨🇳 中國",
    "美國": "🇺🇸 美國", "united states": "🇺🇸 美國",
    "台灣": "🇹🇼 台灣", "taiwan": "🇹🇼 台灣",
    "日本": "🇯🇵 日本", "japan": "🇯🇵 日本",
    "韓國": "🇰🇷 韓國", "korea": "🇰🇷 韓國",
    "荷蘭": "🇳🇱 荷蘭", "netherlands": "🇳🇱 荷蘭",
    "歐洲": "🇪🇺 歐洲", "europe": "🇪🇺 歐洲",
    "俄羅斯": "🇷🇺 俄羅斯", "russia": "🇷🇺 俄羅斯",
}


class RiskAssessmentAgent:
    """
    Agentic RAG 三段式管線：
      extract_entities → semantic_rank → analyze
    """

    # ── Step 1: Sensing Agent ─────────────────────────────────────────────────

    def extract_entities(self, text: str) -> dict:
        """從新聞文本提取結構化實體。"""
        tl = text.lower()

        components = sorted({t for t in _COMPONENT_TERMS if t in tl})

        countries: list[str] = []
        seen: set[str] = set()
        for kw, label in _COUNTRY_MAP.items():
            if kw in tl and label not in seen:
                countries.append(label)
                seen.add(label)

        risk_types = [
            rt for rt, pats in _RISK_EVENT_PATTERNS.items()
            if any(p in tl for p in pats)
        ]

        return {
            "components": components[:6],
            "countries":  countries[:4],
            "risk_types": risk_types[:3],
        }

    # ── Step 2: Semantic Retrieval ────────────────────────────────────────────

    def semantic_rank(
        self,
        query: str,
        candidates: list[Any],
        text_fn: Callable[[Any], str],
    ) -> list[tuple[float, Any]]:
        """
        TF-IDF 字元 n-gram 語意排名（支援中英混合文本）。
        若 scikit-learn 不可用，自動降回關鍵詞重疊率。
        """
        if not candidates:
            return []

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            corpus = [text_fn(c) for c in candidates]
            vec = TfidfVectorizer(
                analyzer="char_wb",   # 字元 n-gram，對中文友好
                ngram_range=(2, 4),
                max_features=10000,
                sublinear_tf=True,
            )
            matrix = vec.fit_transform([query] + corpus)
            sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
            return sorted(zip(sims.tolist(), candidates), key=lambda x: x[0], reverse=True)

        except ImportError:
            # 降回方案：關鍵詞重疊率
            q_words = set(re.findall(r"[\w一-鿿]{2,}", query.lower()))
            results = []
            for item in candidates:
                c_words = set(re.findall(r"[\w一-鿿]{2,}", text_fn(item).lower()))
                score = len(q_words & c_words) / max(len(q_words), 1)
                results.append((score, item))
            return sorted(results, key=lambda x: x[0], reverse=True)

    # ── Step 3: Orchestrator ──────────────────────────────────────────────────

    def analyze(
        self,
        title: str,
        description: str,
        ragqa_pool: list,
        regular_pool: list,
    ) -> dict:
        """
        完整 Agentic 管線：
          1. 感知實體
          2. 分別對 RAGQA 知識庫 & 一般風險事件進行語意檢索
          3. 設最低門檻過濾（不合格不出現）
          4. RAGQA 最多 2 席（需過門檻），一般最多 3 席
        回傳 {entities, matched: list[(score, RiskItem)]}
        """
        news_text = f"{title} {description}"
        entities  = self.extract_entities(news_text)

        def item_text(r) -> str:
            return f"{getattr(r, 'title', '')} {getattr(r, 'description', '')}"

        ragqa_ranked   = self.semantic_rank(news_text, ragqa_pool,   item_text)
        regular_ranked = self.semantic_rank(news_text, regular_pool, item_text)

        # 語意門檻：低於此分數不列入（避免湊數）
        # TF-IDF char n-gram：0.06 等同「有實質字元重疊」
        RAGQA_THRESHOLD   = 0.06
        REGULAR_THRESHOLD = 0.04

        ragqa_top = [(s, item) for s, item in ragqa_ranked
                     if s >= RAGQA_THRESHOLD][:2]
        seen_ids  = {id(item) for _, item in ragqa_top}
        regular_top = [(s, item) for s, item in regular_ranked
                       if s >= REGULAR_THRESHOLD and id(item) not in seen_ids][:3]

        return {
            "entities": entities,
            "matched":  ragqa_top + regular_top,
        }


# 模組級單例
agent = RiskAssessmentAgent()
