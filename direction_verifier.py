"""
方向验证模块
用于在阶段二（专业方向识别）无法匹配时，通过网络搜索进行二次验证
"""
import logging
import json
import re
import time
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

class DirectionVerifier:
    def __init__(self, llm_agent, mcp_client=None):
        """
        初始化方向验证器
        
        Args:
            llm_agent: LLM 代理示例，用于提取方向、分析结果
            mcp_client: MCPClientWrapper 实例（可选）。提供时使用 Playwright MCP 进行搜索
        """
        self.llm_agent = llm_agent
        self.mcp_client = mcp_client
        self._cleaned = False
        
        if self.mcp_client:
            logger.info("✅ DirectionVerifier: 使用 Playwright MCP 进行方向网络验证")
        else:
            logger.info("⚠️  DirectionVerifier: 未提供 MCP 客户端，将仅使用 LLM 自身知识进行推断")
            
        self.geo_fields = ['Physical_Geo', 'Human_Geo', 'Urban', 'GIS', 'RS', 'GNSS']
        
    def _parse_json_obj(self, text: str) -> dict:
        """从 LLM 返回文本中提取 JSON 对象"""
        text = text.strip()
        text = re.sub(r'^```[a-z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return {}
        
    def _extract_actual_directions(self, text: str) -> List[str]:
        """步骤A：从原文中提取真实的 1-3 个研究方向/工作内容"""
        prompt = f"""Please analyze the following academic or job posting text and extract the TOP 1-3 actual research directions, project topics, or core work responsibilities mentioned.

Text to analyze:
{text}

RULES:
1. Extract the specific, actual noun phrases (e.g., "deep reinforcement learning for power systems", "climate change adaptation", "satellite image segmentation").
2. Do not attempt to generalize them into broad categories yet. Use the specific terminology from the text.
3. Return ONLY a JSON object with a list of strings under the key "directions". Maximum 3 items.

Expected JSON Format:
{{
    "directions": ["direction 1", "direction 2"]
}}
"""
        try:
            response = self.llm_agent.call_llm(prompt)
            if response:
                data = self._parse_json_obj(response)
                directions = data.get("directions", [])
                if isinstance(directions, list):
                    return [str(d) for d in directions if d][:3]
        except Exception as e:
            logger.warning(f"方向提取失败: {e}")
            
        return []
        
    def _mcp_search_knowledge(self, query: str) -> str:
        """步骤B：通过 Playwright MCP 在 DuckDuckGo 搜索知识库，返回快照作为参考内容"""
        if not self.mcp_client:
            return ""
            
        search_url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}&kp=-2&kl=us-en&ia=web"
        logger.info(f"方向验证检索: {query}")
        
        MAX_RETRIES = 2
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.mcp_client.call_tool("mcp_playwright_browser_navigate", {"url": search_url})
                time.sleep(3.0) # 等待加载
                self.mcp_client.call_tool("mcp_playwright_browser_snapshot", {}) # 触发渲染
                time.sleep(1.0)
                snap = self.mcp_client.call_tool("mcp_playwright_browser_snapshot", {})
                
                if snap and len(snap.strip()) > 100:
                    # 截取前 4000 个字符的搜索结果文本摘要足以让 LLM 判断
                    logger.debug(f"成功获取方向验证搜索结果，长度 {len(snap)}")
                    return snap[:4000]
                    
                logger.warning(f"第{attempt}次搜索快照为空或过短，重试...")
                time.sleep(2.0)
            except Exception as e:
                logger.warning(f"第{attempt}次网络搜索异常: {e}")
                time.sleep(2.0)
            
        return ""
        
    def _evaluate_directions(self, original_text: str, directions: List[str], search_context: str) -> Dict[str, str]:
        """步骤C：结合搜索到的上下文，让 LLM 做最终定夺"""
        directions_str = ", ".join(directions)
        
        prompt = f"""You are an expert at evaluating whether specific academic research directions fall into the broad disciplines of Geography, Geographic Information Systems (GIS), or Remote Sensing.

We are trying to categorize a position with the following extracted directions:
[{directions_str}]

Original text excerpt:
{original_text[:1500]}

Here is some web search context about whether these directions relate to Geography/GIS/RS (if any):
{search_context if search_context else "No active web context available. Please rely on your extensive internal knowledge."}

Based on this information, can this position be reasonably classified into ONE OR MORE of the following 6 core geographic fields?
- "Physical_Geo": Physical Geography, Environmental Sciences, Ecology, Earth Sciences, Hydrology, Climate Change, etc.
- "Human_Geo": Human Geography, Economic Geography, Demography, Social Geography, Urban/Regional Studies, etc.
- "Urban": Urban Planning, Smart City, Land Use, Architecture, Urban Analytics, etc.
- "GIS": Geographic Information Systems, Spatial Analysis, Spatial Data Science, Location Intelligence, etc.
- "RS": Remote Sensing, Satellite Imagery, Earth Observation, Photogrammetry, Drone Mapping, etc.
- "GNSS": Global Navigation Satellite Systems, GPS, Geodesy, Positioning Systems, etc.

RULES:
1. If the position is strongly related to computers/AI/Data Science but explicitly applies these techniques to geography/environment/spatial data, you SHOULD map it to GIS, RS, or Physical/Human Geo.
2. If the position is PURELY computer science, pure physics, or completely unrelated to earth/spatial/environmental sciences, it DOES NOT belong to any of these.
3. Return ONLY a JSON object mapping the matched fields to "1". If NONE match, return an empty JSON object {{}}.

Expected JSON Format (Example if matches GIS and RS):
{{
    "GIS": "1",
    "RS": "1"
}}

Expected JSON Format (Example if unrelated):
{{}}
"""
        try:
            response = self.llm_agent.call_llm(prompt)
            if response:
                data = self._parse_json_obj(response)
                
                # Verify the mapped fields
                result = {}
                for key in self.geo_fields:
                    if str(data.get(key, "")) == "1":
                        result[key] = "1"
                
                return result
        except Exception as e:
            logger.warning(f"方向最终验证失败: {e}")
            
        return {}

    def verify_and_map_direction(self, text: str) -> Optional[Dict[str, str]]:
        """执行完整的方向二次验证流程
        
        Returns:
            Dict: 映射成功的地理方向（如 {'GIS': '1'}），如果验证失败认为不属于GIS，则返回 None
        """
        logger.info("进入方向二次验证流程（因为常规判定未能选出任何方向）")
        
        # 步骤A: 提取实际方向
        actual_directions = self._extract_actual_directions(text)
        if not actual_directions:
            logger.warning("无法提取出具体的专业方向词汇，验证失败")
            return None
            
        logger.info(f"提取出具体研究方向: {actual_directions}")
        
        # 步骤B: 网络搜索（可选）
        search_context = ""
        if self.mcp_client:
            # 构造搜索词，例如："Is deep reinforcement learning for power systems related to Geographic Information Systems, Geography, or Remote Sensing?"
            query_parts = " and ".join([f'"{d}"' for d in actual_directions])
            query = f"{query_parts} relation to Geography GIS Remote Sensing"
            search_context = self._mcp_search_knowledge(query)
            if search_context:
                logger.info("已获取辅助验证的网络搜索结果")
            else:
                logger.info("辅助验证搜索未能返回有效内容，将仅依赖大模型本地知识验证")
                
        # 步骤C: 综合研判
        mapped_fields = self._evaluate_directions(text, actual_directions, search_context)
        
        if mapped_fields:
            logger.info(f"经过二次验证，成功归类为: {list(mapped_fields.keys())}")
            return mapped_fields
        else:
            logger.info("经过二次验证，判定该方向确实不属于上述6大地理类领域")
            return None
            
    def cleanup(self):
        """清理验证器资源"""
        if getattr(self, '_cleaned', False):
            return
        self._cleaned = True
        logger.info("DirectionVerifier 资源已清理")

    def __del__(self):
        self.cleanup()
