"""
LLM调用模块，支持OpenAI API和Ollama本地模型
"""
import json
import logging
import requests
from typing import Optional, Dict, Any, List
import time
from datetime import datetime

from .config import (
    OPENAI_MODEL,
    OPENAI_PRIMARY_MODEL,
    OPENAI_BASE_URL,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    check_openai_key,
)
from .api_client import NewAPIClient
from .utils import validate_json_response, save_llm_conversation, check_ollama_availability

logger = logging.getLogger(__name__)

class LLMAgent:
    def __init__(self):
        self.use_openai = False
        self.openai_client = None
        self.conversation_history = []
        self.conversation_saved = False  # 添加标志位，避免重复保存
        self.last_used_model = None
        
        # 检查LLM可用性
        self._initialize_llm()
    
    def _initialize_llm(self):
        """初始化LLM"""
        # 优先尝试 New API（OpenAI-compatible gateway）
        if check_openai_key():
            try:
                self.openai_client = NewAPIClient()
                self.use_openai = True
                self.last_used_model = OPENAI_PRIMARY_MODEL
                logger.info(f"✅ New API 初始化成功 (Base URL: {OPENAI_BASE_URL})")
                return
            except Exception as e:
                logger.warning(f"New API 初始化失败: {e}")
        
        # 尝试Ollama
        if check_ollama_availability():
            self.use_openai = False
            logger.info("✅ Ollama 本地模型初始化成功")
        else:
            logger.error("❌ 无可用的LLM服务")
            raise RuntimeError("无可用的LLM服务，请检查OpenAI API Key或Ollama服务")
    
    def reset_context(self):
        """重置对话上下文（但保留conversation_history用于日志记录）"""
        if self.use_openai:
            # OpenAI API每次调用都是独立的，无需特殊处理
            pass
        else:
            # Ollama需要清理上下文
            try:
                self._reset_ollama_context()
            except Exception as e:
                logger.warning(f"重置Ollama上下文失败: {e}")
        
        # 注意：不清空conversation_history，保留用于最终的日志记录
        # 但重置保存标志，允许新的对话记录保存
        self.conversation_saved = False
        logger.info("LLM对话上下文已重置")
    
    def _reset_ollama_context(self):
        """重置Ollama模型上下文"""
        try:
            # 通过发送一个特殊的重置请求来清空上下文
            reset_url = f"{OLLAMA_BASE_URL}/api/generate"
            reset_data = {
                "model": OLLAMA_MODEL,
                "prompt": "[RESET]",
                "stream": False,
                "options": {
                    "num_ctx": 4096,
                    "temperature": 0.1,
                    "top_p": 0.001,
                    "repetition_penalty": 1.05
                }
            }
            
            response = requests.post(reset_url, json=reset_data, timeout=10)
            if response.status_code == 200:
                logger.info("Ollama上下文重置成功")
            else:
                logger.warning(f"Ollama上下文重置失败: {response.status_code}")
                
        except Exception as e:
            logger.warning(f"Ollama上下文重置异常: {e}")
    
    def call_llm(self, prompt: str, system_prompt: str = None) -> Optional[str]:
        """调用LLM获取响应"""
        try:
            if self.use_openai:
                return self._call_openai(prompt, system_prompt)
            else:
                return self._call_ollama(prompt, system_prompt)
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return None
    
    def _call_openai(self, prompt: str, system_prompt: str = None) -> Optional[str]:
        """调用 New API /chat/completions，按 TEXT_MODEL_CHAIN 价格升序回退。"""
        combined_prompt = f"{system_prompt or ''}\n{prompt}".lower()
        try:
            result = self.openai_client.complete_text(
                prompt=prompt,
                instructions=system_prompt,
                max_output_tokens=3000,
                temperature=0.1,
                json_mode="json" in combined_prompt,
            )
        except Exception as e:
            logger.error(f"New API 文本调用异常: {e}")
            return None

        if result is None:
            logger.error("New API 模型链全部调用失败")
            return None

        # 记录实际成功使用的模型
        self.last_used_model = getattr(self.openai_client, "last_model", None) or OPENAI_PRIMARY_MODEL
        return result
    
    def _call_ollama(self, prompt: str, system_prompt: str = None) -> Optional[str]:
        """调用Ollama本地模型"""
        try:
            # 为qwen3模型特别优化prompt格式
            if system_prompt:
                full_prompt = f"<|im_start|>system\n{system_prompt}<|im_end|>\n<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
            else:
                full_prompt = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
            
            url = f"{OLLAMA_BASE_URL}/api/generate"
            data = {
                "model": OLLAMA_MODEL,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "num_ctx": 8192,  # 增加上下文长度
                    "temperature": 0.1,
                    "top_p": 0.1,  # 稍微增加一点创造性
                    "repetition_penalty": 1.05,
                    "stop": ["<|im_end|>", "<|im_start|>"]  # 设置停止词
                }
            }
            
            logger.info("调用Ollama API...")
            
            response = requests.post(url, json=data, timeout=180)  # 增加超时时间
            response.raise_for_status()
            
            result_data = response.json()
            result = result_data.get('response', '')
            
            logger.info("Ollama API调用成功")
            return result
            
        except Exception as e:
            logger.error(f"Ollama API调用失败: {e}")
            return None
    
    def analyze_text_stage1(self, text: str) -> Optional[Dict]:
        """英文分析阶段1：基本信息提取"""
        logger.info("开始英文分析阶段1")
        
        # 获取当前日期
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        
        system_prompt = """You are an expert at extracting academic and research position information from text. You need to analyze the provided text and extract specific information in JSON format. 

IMPORTANT: Respond ONLY with the JSON object. Do not include any explanation, reasoning, or additional text. Just return the pure JSON response."""
        
        prompt = f"""
TODAY'S DATE: {current_date}

TEXT TO ANALYZE:
{text}

EXTRACTION INSTRUCTIONS:
CRITICAL RULE: 
- You MUST ONLY extract information that is EXPLICITLY stated in the provided text. 
- If information is not found, use the specified default values.
- You MUST return a COMPLETE JSON object with ALL required fields.

1. "Deadline": Extract ONLY the "application deadline" that is explicitly mentioned in the text. Convert to YYYY-MM-DD format. If deadline is not mentioned, use "Soon".
   - IMPORTANT: If the text only provides month and day without a year, infer the year based on today's date ({current_date}):
     * If the month/day has already passed this year, assume it refers to next year ({current_year + 1})
     * If the month/day has not yet passed this year, assume it refers to this year ({current_year})
2. "Number_Places": Extract ONLY the number of positions that is explicitly stated. For academic positions (Master, PhD, PostDoc, Research Assistant), specify the exact number if mentioned. If there are multiple positions, the number should be added up. If not specified, should fill in "1". For events (Competition, Summer School, Conference, Workshop), leave empty.
3. "Direction": Extract ONLY the research direction or project topic that is stated in the text. If not explicitly stated, summarize ONLY from the actual content provided. Please make sure that the first letter of the first word and special nouns are capitalized, and the others are lowercase.For example, “PhD Position: Using AI for Pandemic Preparedness and Building Resilient Healthcare Systems (PARAATHEID)” should be converted to “Using AI for pandemic preparedness and building resilient healthcare systems”.
4. "University_EN": Extract the top-level full official English name of the university/institution. Prefer the parent university over any school, college, faculty, department, center, institute, laboratory, hospital, or other sub-unit. If the source text uses a non-English local-language institution name, convert it to the institution's standard official English name when that mapping is clear and unambiguous. Do NOT append any abbreviation, acronym, short form, or alias in parentheses, after commas, or anywhere else. If the text provides both a full name and an abbreviation, return only the full name without the abbreviation. If the text provides only an abbreviation/acronym and the full name is not explicitly given, leave empty.
   - If a posting is issued by a sub-unit of a university, return the university name, not the sub-unit name.
   - If the page mentions a school/college/faculty/department/center/lab that clearly belongs to a university, normalize it to the university's full official name.
   - Correct: "Helmholtz Centre for Environmental Research"
   - Wrong: "Helmholtz Centre for Environmental Research (UFZ)"
   - Wrong: "Helmholtz Centre for Environmental Research, UFZ"
   - Correct: "Harvard University"
   - Wrong: "Harvard Chan School of Public Health"
   - Correct: "Paris-Saclay University"
   - Wrong: "Université Paris-Saclay"
5. "Contact_Name": Extract ONLY the contact person's name that is EXPLICITLY provided in the text (usually the project leader, proposer, or professor, etc.). If multiple contacts, choose the first one. If NO contact is provided, use "-".
   CRITICAL FORMATTING RULES:
   - ONLY use these exact prefixes: "Dr. ", "Mr. ", "Ms. " or NO prefix at all
   - NEVER use: "Prof.", "Professor", "Assistant Professor", "Associate Professor", etc.
   - NEVER include suffixes like "Ph.D.", "PhD", "Professor" after the name
   - NEVER include nicknames in quotes like "Jerry"
   - NEVER include academic degrees or titles after the name
   - IMPORTANT: If the text mentions the person is a Professor, Assistant Professor, Associate Professor, Dr., Doctor, or has a PhD/Ph.D. degree, you MUST add "Dr. " prefix
   - Examples of applying "Dr." prefix:
     * "Professor John Smith" → "Dr. John Smith"
     * "Prof. John Smith" → "Dr. John Smith"
     * "Assistant Professor Jane Doe" → "Dr. Jane Doe"
     * "Associate Professor Michael Brown" → "Dr. Michael Brown"
     * "John Smith, Ph.D." → "Dr. John Smith"
     * "John Smith, PhD" → "Dr. John Smith"
     * "Dr. Sarah Johnson" → "Dr. Sarah Johnson"
     * "Doctor Tom Wilson" → "Dr. Tom Wilson"
   - If uncertain about title and no doctorate/professor information is mentioned, extract only the clean name: "John Smith"
   - Clean format examples: "Dr. John Smith", "Mr. John Smith", "Ms. Sarah Johnson", "John Smith"
6. "Contact_Email": Extract ONLY the email address that is EXPLICITLY provided in the text. DO NOT create, guess, or construct email addresses. If NO email is provided, use "-".
   CRITICAL EMAIL FORMATTING RULES:
   - If the text uses "[at]", "(at)", " at ", or similar variations instead of "@", you MUST convert them to "@"
   - If the text uses "[dot]", "(dot)", or similar variations instead of ".", you MUST convert them to "."
   - Examples of email format conversion:
     * "yichun.fan[at]duke.edu" → "yichun.fan@duke.edu"
     * "john.smith(at)university.edu" → "john.smith@university.edu"
     * "contact at example.com" → "contact@example.com"
     * "user[at]domain[dot]com" → "user@domain.com"
     * "name (at) school (dot) edu" → "name@school.edu"
   - Always output the email in standard format with "@" and "." symbols

REQUIRED JSON FORMAT (EXAMPLE ONLY - DO NOT COPY THESE VALUES):
{{
  "Deadline": "2024-03-15",
  "Number_Places": "3",
  "Direction": "Machine learning for environmental monitoring",
  "University_EN": "University of Cambridge",
  "Contact_Name": "Dr. John Smith",
  "Contact_Email": "j.smith@cam.ac.uk"
}}

IMPORTANT: Extract actual information from the text above, not the example values.
"""
        
        response = self.call_llm(prompt, system_prompt)
        if not response:
            logger.error("LLM返回空响应")
            return None
        
        # 记录对话到历史
        self._add_to_conversation_history("stage1", prompt, response, text)
        
        result = validate_json_response(response)
        if result:
            logger.info("英文分析阶段1完成")
        else:
            logger.error("英文分析阶段1结果格式错误")
        
        return result
    
    def analyze_text_stage2(self, text: str) -> Optional[Dict]:
        """英文分析阶段2：类型和方向分析"""
        logger.info("开始英文分析阶段2")
        
        system_prompt = """You are an expert at categorizing academic positions and research fields. Analyze the text and identify relevant categories.

IMPORTANT: Respond ONLY with the JSON object. Do not include any explanation, reasoning, or additional text. Just return the pure JSON response."""
        
        prompt = f"""
TEXT TO ANALYZE:
{text}

CATEGORIZATION INSTRUCTIONS:
CRITICAL RULE: 
- You MUST ONLY categorize based on information that is stated in the provided text. 
- IMPORTANT: ONLY include fields with value "1" in your response. DO NOT include fields that don't apply.
- You must mark at least 1 and at most 3 Research Fields.

Position Types (mark "1" if mentioned, DO NOT include in response if not applicable):
- "Master Student": Master's degree students
- "Doctoral Student": Doctoral's degree students (PhD students)  
- "PostDoc": Postdoctoral researchers (If a research assistant position also requires the candidate to have a PhD degree, then this category counts as a postdoctoral)
- "Research Assistant": Research assistants
- "Competition": Competitions
- "Summer School": Summer schools
- "Conference": Academic conferences
- "Workshop": Workshops

Research Fields (STRICT LIMIT: Select MINIMUM 1, MAXIMUM 3 fields. NEVER exceed 3 fields. If content relates to more than 3 fields, you MUST choose only the TOP 3 most relevant. Any response with more than 3 Research Fields is INVALID. Mark "1" only for selected fields, DO NOT include unselected fields.):
- "Physical_Geo": Physical Geography, Agriculture, Environmental Sciences, Climatology, Ecology, Geology, Earth Sciences, Hydrology, Biodiversity, Landscape Ecology, Climate Change, Soil Science, Natural Hazards, Geomorphology, Oceanography, Atmospheric Sciences, etc.
- "Human_Geo": Human Geography, Health Geography, Economic Geography, Demography, Medical Geography, Social Geography, Cultural Geography, Political Geography, Population Studies, Migration Studies, Tourism Geography, Behavioral Geography, Development Studies, Regional Studies, etc.
- "Urban": Urban Planning, Smart City, Land Use, Architecture, Sustainable Cities, Urban Design, Urban Development, City Planning, Metropolitan Studies, Urban Transportation, Urban Environment, Urban Policy, Housing Studies, Infrastructure Planning, Urban Analytics, Urban Modeling, etc.
- "GIS": Geographic Information Systems/Science, Spatial Analysis, Spatial Data Science, Geospatial Technology, Cartography, Spatial Statistics, Location Intelligence, Spatial Modeling, Geodatabases, Web GIS, Spatial Data Mining, Geovisualization, Spatial Decision Support Systems, Geoinformatics, Location Analytics, Epidemiology (when involving spatial analysis), Disease Mapping, etc.
- "RS": Remote Sensing, Satellite Imagery, Unmanned Aerial Vehicle (Drone), Earth Observation, Image Processing, Multispectral Analysis, Hyperspectral Analysis, Radar Imaging, LiDAR, Aerial Photography, Satellite Data Analysis, Change Detection, Land Cover Classification, Digital Image Processing, etc.
- "GNSS": Global Navigation Satellite Systems, GPS, Surveying and Mapping, Geodesy, Precision Positioning, Navigation Systems, Satellite Navigation, Location Services, Geolocation Technology, Positioning Systems, etc.

REQUIRED JSON FORMAT (ALL fields shown below for reference - but ONLY include fields with value "1" in your actual response):
{{
  "Master Student": "1",
  "Doctoral Student": "1",
  "PostDoc": "1",
  "Research Assistant": "1",
  "Competition": "1",
  "Summer School": "1",
  "Conference": "1",
  "Workshop": "1",
  "Physical_Geo": "1",
  "Human_Geo": "1",
  "Urban": "1",
  "GIS": "1",
  "RS": "1",
  "GNSS": "1"
}}

EXAMPLE RESPONSE (only fields that apply):
{{
  "Master Student": "1",
  "Doctoral Student": "1",
  "Physical_Geo": "1",
  "GIS": "1"
}}

IMPORTANT: Analyze the actual text content to determine which categories apply. ONLY return the fields that have value "1".
"""
        
        response = self.call_llm(prompt, system_prompt)
        if not response:
            logger.error("LLM返回空响应")
            return None
        
        # 记录对话到历史
        self._add_to_conversation_history("stage2", prompt, response, text)
        
        result = validate_json_response(response)
        if result:
            logger.info("英文分析阶段2完成")
        else:
            logger.error("英文分析阶段2结果格式错误")
        
        return result
    
    def analyze_text_stage3(self, text: str) -> Optional[Dict]:
        """中文分析阶段：中文字段提取"""
        logger.info("开始中文分析阶段")
        
        system_prompt = """你是一个专业的学术信息提取专家。请分析提供的文本，并用标准简体中文提取所需信息。

重要说明：只需要返回JSON对象，不要包含任何解释、推理过程或其他文本。只返回纯JSON格式的回答。"""
        
        prompt = f"""
要分析的文本：
{text}

提取指令：
关键规则：
- 您只能提取文本中提到的信息。如果找不到信息或不能确定，请留空。
- 任何输出内容都必须使用标准的简体中文汉字填写，不得包含繁体字、英文字母、数字或特殊符号。
- 您必须返回一个包含所有字段的完整JSON对象。

1. "University_CN": 仅提取文本中提到该项目所属的大学/机构的中文全称(如有多个大写/机构，则仅选择最主要的)。
2. "Country_CN": 仅提取文本中提到的大学/机构所在国家的中文名称。
3. "WX_Label1": 从文本中提到的相关专业学科中选择最符合的。
4. "WX_Label2": 其他相关专业学科或研究方向。
5. "WX_Label3": 其他相关研究方向。
6. "WX_Label4": 留空。
7. "WX_Label5": 留空。

注意：
- WX_Label1必须填写
- WX_Label3可以填写但不是必需的
- 不能填写"自然地理学"、"人文地理学"、"地理信息科学"、"地理信息系统"、"城市规划"、"遥感"、"卫星导航系统"
- WX_Label1应该填写具体的专业学科名称，比如："生态学"、"地质学"、"环境科学"等
- WX_Label2、WX_Label3应该填写具体的研究方向名称，比如："风力发电"、"空间分析"、"深度学习"等
- 在WX_Label1-5中所有填写的单个内容不得超过6个字

要求的JSON格式（仅为示例，请勿复制示例数值）：
{{
  "University_CN": "剑桥大学",
  "Country_CN": "英国",
  "WX_Label1": "数据科学",
  "WX_Label2": "环境监测",
  "WX_Label3": "机器学习",
  "WX_Label4": "统计分析",
  "WX_Label5": "算法优化"
}}

重要提醒：请从上述文本中提取真实信息，而不是使用示例中的值。
"""
        
        response = self.call_llm(prompt, system_prompt)
        if not response:
            logger.error("LLM返回空响应")
            return None
        
        # 记录对话到历史
        self._add_to_conversation_history("stage3", prompt, response, text)
        
        result = validate_json_response(response)
        if result:
            logger.info("中文分析阶段完成")
        else:
            logger.error("中文分析阶段结果格式错误")
        
        return result
    
    def _add_to_conversation_history(self, stage: str, prompt: str, response: str, original_text: str = None):
        """添加对话到历史记录"""
        conversation = {
            "stage": stage,
            "timestamp": time.time(),
            "prompt": prompt,
            "response": response,
            "original_text": original_text,  # 添加原始文本内容
            "model": self.last_used_model if self.use_openai else OLLAMA_MODEL
        }
        self.conversation_history.append(conversation)
        logger.info(f"已记录{stage}阶段对话")
    
    def save_conversation_log(self, row_index: int):
        """保存对话记录"""
        # 检查是否已经保存过
        if self.conversation_saved:
            logger.info(f"行 {row_index} 的对话记录已经保存过，跳过重复保存")
            return
        
        if self.conversation_history:
            save_llm_conversation(row_index, self.conversation_history)
            logger.info(f"已保存行{row_index}的{len(self.conversation_history)}条对话记录")
            # 标记为已保存
            self.conversation_saved = True
            # 清空对话历史，为下一行处理准备
            self.conversation_history = []
        else:
            logger.warning(f"行 {row_index} 没有对话记录可保存")
    
    def get_model_info(self) -> Dict[str, Any]:
        """获取当前使用的模型信息"""
        return {
            "use_openai": self.use_openai,
            "model": self.last_used_model if self.use_openai else OLLAMA_MODEL,
            "api_url": OPENAI_BASE_URL if self.use_openai else OLLAMA_BASE_URL
        }

def test_llm_agent():
    """测试LLM代理功能"""
    try:
        agent = LLMAgent()
        
        # 测试文本
        test_text = """
        PhD Position in Remote Sensing and Environmental Monitoring
        University of Technology, Netherlands
        
        We are seeking a highly motivated PhD student to join our research team working on satellite-based environmental monitoring. The position involves developing machine learning algorithms for analyzing remote sensing data to monitor forest changes and carbon sequestration.
        
        Application deadline: March 15, 2024
        Duration: 4 years
        Contact: Dr. John Smith (j.smith@uni.nl)
        """
        
        logger.info("测试LLM分析功能...")
        
        # 测试三个分析阶段
        logger.info("\n=== 测试阶段1 ===")
        result1 = agent.analyze_text_stage1(test_text)
        if result1:
            logger.info(f"阶段1结果: {json.dumps(result1, ensure_ascii=False, indent=2)}")
        
        agent.reset_context()
        
        logger.info("\n=== 测试阶段2 ===")
        result2 = agent.analyze_text_stage2(test_text)
        if result2:
            logger.info(f"阶段2结果: {json.dumps(result2, ensure_ascii=False, indent=2)}")
        
        agent.reset_context()
        
        logger.info("\n=== 测试阶段3 ===")
        result3 = agent.analyze_text_stage3(test_text)
        if result3:
            logger.info(f"阶段3结果: {json.dumps(result3, ensure_ascii=False, indent=2)}")
        
        # 保存测试对话记录
        agent.save_conversation_log(9999)  # 测试行号
        
        logger.info("\nLLM代理测试完成")
        
    except Exception as e:
        logger.error(f"LLM代理测试失败: {e}")

if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(level=logging.INFO)
    test_llm_agent() 



