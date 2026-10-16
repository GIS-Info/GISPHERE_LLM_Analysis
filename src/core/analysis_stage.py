"""
分析阶段封装模块
"""
import os
import logging
from typing import Dict, Optional, Tuple
from .llm_agent import LLMAgent
from .observability import traced
from .result_schema import (validation_errors, valid_deadline, STAGE_FIELDS,
                            discard_overlong_labels)
from ..ingestion.excel_handler import validate_analysis_result
from .contact_verifier import ContactVerifier
from .direction_verifier import DirectionVerifier
from .utils import clean_email_format
from .config import (
    ENABLE_WEB_SEARCH, PLAYWRIGHT_MCP_HEADLESS,
    STAGE1_FIELDS, STAGE2_FIELDS, STAGE3_FIELDS, GEO_FIELDS,
)

logger = logging.getLogger(__name__)

class AnalysisStageManager:
    def __init__(self):
        self.llm_agent = LLMAgent()
        self._cleaned = False  # 添加清理标志
        
        # 初始化 Playwright MCP 客户端（用于联系人网络搜索）
        self.mcp_client = None
        if ENABLE_WEB_SEARCH:
            try:
                from ..integrations.mcp_client import MCPClientWrapper
                playwright_args = ["@playwright/mcp@latest", "--isolated"]
                if PLAYWRIGHT_MCP_HEADLESS:
                    playwright_args.append("--headless")
                mcp_configs = [{
                    "name": "playwright",
                    "command": "npx",
                    "args": playwright_args
                }]
                self.mcp_client = MCPClientWrapper()
                self.mcp_client.initialize(mcp_configs)
                logger.info("✅ Playwright MCP 客户端初始化成功")
            except Exception as e:
                logger.warning(f"Playwright MCP 初始化失败，联系人搜索将被跳过: {e}")
                self.mcp_client = None
        else:
            logger.info("网络搜索已禁用 (ENABLE_WEB_SEARCH=0)，跳过 MCP 初始化")
        
        self.contact_verifier = ContactVerifier(self.llm_agent, mcp_client=self.mcp_client)
        self.direction_verifier = DirectionVerifier(self.llm_agent, mcp_client=self.mcp_client)
        self.current_row_index = None
        
    @traced("three-stage-analysis")
    def analyze_text_complete(self, text: str, row_index: int, *,
                              completed_stages=None, on_stage_complete=None,
                              completed_evidence=None, completed_warnings=None) -> Tuple[bool, Dict, str]:
        """Run three stages; reuse only validated checkpoints supplied by the caller."""
        self.current_row_index = row_index
        self.llm_agent.conversation_saved = False
        summary = {"executed": [], "reused": [], "successful": [], "failed": {}}
        self.last_run_summary = summary
        self.review_evidence = {}
        self.field_warnings = []
        summary['warnings'] = self.field_warnings
        final_result = {}
        cached = completed_stages or {}
        try:
            for stage, execute in (("stage1", self._execute_stage1),
                                   ("stage2", self._execute_stage2),
                                   ("stage3", self._execute_stage3)):
                cached_warnings = (completed_warnings or {}).get(stage, [])
                if stage in cached and not validation_errors(cached[stage], stage,
                        allow_empty_label1=stage == 'stage3'):
                    success, result, error = True, dict(cached[stage]), ""
                    summary["reused"].append(stage)
                    self.review_evidence[stage] = (completed_evidence or {}).get(stage, [])
                    self.field_warnings.extend(cached_warnings)
                    logger.info("行 %s 复用 %s 成功检查点", row_index, stage)
                else:
                    summary["executed"].append(stage)
                    try:
                        success, result, error = execute(text)
                        if success:
                            errors = validation_errors(result, stage,
                                allow_empty_label1=stage == 'stage3')
                            if errors:
                                success, error = False, "; ".join(errors)
                    except Exception as exc:
                        success, result, error = False, {}, str(exc)
                    verifier = {"stage1": self.contact_verifier,
                                "stage2": self.direction_verifier}.get(stage)
                    evidence = getattr(verifier, 'last_review_evidence', [])
                    self.review_evidence[stage] = evidence if isinstance(evidence, list) else []
                    if on_stage_complete is not None:
                        try:
                            on_stage_complete(stage, success, result, error)
                        except Exception as exc:
                            logger.warning("检查点保存失败，继续分析: %s", exc)
                if success:
                    summary["successful"].append(stage)
                    final_result.update(result)
                else:
                    summary["failed"][stage] = error or "阶段返回失败"
            if final_result:
                final_result = self._post_process_results(final_result)
            errors = [f"阶段{stage[-1]}失败: {error}" for stage, error in summary["failed"].items()]
            logger.info("行 %s 分析摘要: %s", row_index, summary)
            return bool(summary["successful"]), final_result, "; ".join(errors)
        except Exception as exc:
            logger.exception("分析编排异常")
            return False, {}, f"分析过程发生异常: {exc}"
        finally:
            try:
                self.llm_agent.save_conversation_log(row_index)
            except Exception as exc:
                logger.warning("保存对话记录失败: %s", exc)
    
    @traced("stage1-english-and-contact")
    def _execute_stage1(self, text: str) -> Tuple[bool, Dict, str]:
        """执行阶段1分析（包含联系人验证）"""
        logger.info("执行英文分析阶段1（含联系人验证）")
        if self.contact_verifier is not None:
            self.contact_verifier.last_review_evidence = []
        
        try:
            # 重置上下文
            self.llm_agent.reset_context()
            
            # 步骤1：基本信息提取
            logger.info("步骤1: 基本信息提取")
            result = self.llm_agent.analyze_text_stage1(text)
            
            if not result:
                return False, {}, "LLM返回空结果"
            
            # 验证结果格式
            errors = validation_errors(result, "stage1")
            if errors:
                return False, {}, "; ".join(errors)
            
            logger.info("基本信息提取完成")
            
            # 步骤2：联系人验证流程
            logger.info("步骤2: 联系人验证流程")
            verification_result = self._execute_contact_verification(
                result, text
            )
            
            # 更新联系人信息
            if verification_result:
                result.update(verification_result)
                logger.info("联系人验证完成")
            else:
                logger.info("联系人验证跳过或失败")
            
            logger.info("阶段1分析成功")
            return True, result, ""
            
        except Exception as e:
            error_msg = f"阶段1分析异常: {str(e)}"
            logger.error(error_msg)
            return False, {}, error_msg
    
    def _execute_contact_verification(self, stage1_result: Dict, original_text: str) -> Optional[Dict]:
        """执行联系人验证流程"""
        try:
            university_en = stage1_result.get('University_EN', '')
            contact_name = stage1_result.get('Contact_Name', '')
            contact_email = stage1_result.get('Contact_Email', '')
            
            # 场景3：缺失联系人，无需验证
            if not contact_name or contact_name.strip() in ['-', '', 'N/A']:
                logger.info("场景3: 缺失联系人，跳过验证")
                return None
            
            # 执行联系人验证
            verification_result = self.contact_verifier.verify_and_update_contact(
                university_en, contact_name, contact_email, original_text
            )
            
            # 构建返回结果
            updated_fields = {}
            
            # 记录验证详情
            verification_performed = verification_result.get('verification_performed', False)
            if verification_performed:
                logger.info(f"验证原因: {verification_result.get('verification_reason', '')}")
                logger.info(f"验证详情: {verification_result.get('verification_details', '')}")
            else:
                logger.info(f"跳过验证: {verification_result.get('verification_reason', '')}")
            
            # 更新联系人姓名（只有在验证后有变化时才更新）
            if verification_result.get('Contact_Name') != contact_name:
                updated_fields['Contact_Name'] = verification_result['Contact_Name']
                logger.info(f"联系人姓名已更新: {contact_name} -> {verification_result['Contact_Name']}")
            # 如果验证被跳过（第一阶段已有Dr.前缀），保持原样，不做任何格式化
            
            # 更新联系邮箱（如果验证后有变化）
            if verification_result.get('Contact_Email') != contact_email:
                updated_fields['Contact_Email'] = verification_result['Contact_Email']
                logger.info(f"联系邮箱已更新: {contact_email} -> {verification_result['Contact_Email']}")
            
            return updated_fields if updated_fields else None
            
        except Exception as e:
            logger.error(f"联系人验证失败: {e}")
            return None
    
    def cleanup(self):
        """清理资源"""
        # 检查是否已经清理，避免重复清理
        if getattr(self, '_cleaned', False):
            return
        
        if hasattr(self, 'contact_verifier') and self.contact_verifier:
            try:
                self.contact_verifier.cleanup()
            except Exception as e:
                logger.warning(f"清理联系人验证器资源失败: {e}")
            finally:
                self.contact_verifier = None
                
        if hasattr(self, 'direction_verifier') and self.direction_verifier:
            try:
                self.direction_verifier.cleanup()
            except Exception as e:
                logger.warning(f"清理方向验证器资源失败: {e}")
            finally:
                self.direction_verifier = None
        
        # 关闭 Playwright MCP 进程
        if hasattr(self, 'mcp_client') and self.mcp_client:
            try:
                self.mcp_client.cleanup()
                logger.info("Playwright MCP 客户端已关闭")
            except Exception as e:
                logger.warning(f"关闭 MCP 客户端失败: {e}")
            finally:
                self.mcp_client = None
        
        self._cleaned = True  # 标记为已清理
    
    def __del__(self):
        """析构函数，确保资源被清理"""
        self.cleanup()
    
    @traced("stage2-type-and-direction")
    def _execute_stage2(self, text: str) -> Tuple[bool, Dict, str]:
        """执行阶段2分析"""
        logger.info("执行英文分析阶段2")
        if self.direction_verifier is not None:
            self.direction_verifier.last_review_evidence = []
        
        try:
            # 重置上下文
            self.llm_agent.reset_context()
            
            # 调用LLM分析
            result = self.llm_agent.analyze_text_stage2(text)
            
            if not result:
                return False, {}, "LLM返回空结果"
            
            errors = validation_errors(result, "stage2", require_geo=False)
            if errors:
                return False, {}, "; ".join(errors)
            
            # 验证专业方向字段数量（最多3个，最少1个）
            marked_fields = [field for field in GEO_FIELDS if field in result and result[field] == "1"]
            
            if len(marked_fields) == 0:
                logger.info("第一遍分析未标记任何专业方向，尝试二次验证...")
                
                # 进入方向网络搜索和判定二次验证
                if hasattr(self, 'direction_verifier') and self.direction_verifier:
                    verified_fields = self.direction_verifier.verify_and_map_direction(text)
                    if verified_fields:
                        result.update(verified_fields)
                        errors = validation_errors(result, "stage2")
                        if errors:
                            return False, {}, "; ".join(errors)
                        logger.info(f"阶段2通过二次验证，补充字段: {list(verified_fields.keys())}")
                        return True, result, ""
                
                return False, {}, "可能不属于GIS领域"
            elif len(marked_fields) > 3:
                return False, {}, f"标记了{len(marked_fields)}个专业方向，超过最大限制3个"
            
            if not validate_analysis_result(result, "stage2"):
                return False, {}, "; ".join(validation_errors(result, "stage2"))
            logger.info(f"阶段2分析成功，返回 {len(result)} 个字段: {list(result.keys())}")
            return True, result, ""
            
        except Exception as e:
            error_msg = f"阶段2分析异常: {str(e)}"
            logger.error(error_msg)
            return False, {}, error_msg
    
    @traced("stage3-chinese-fields")
    def _execute_stage3(self, text: str) -> Tuple[bool, Dict, str]:
        """执行阶段3分析"""
        logger.info("执行中文分析阶段")
        
        try:
            # 重置上下文
            self.llm_agent.reset_context()
            
            # 调用LLM分析
            result = self.llm_agent.analyze_text_stage3(text)
            
            if not result:
                return False, {}, "LLM返回空结果"
            
            # Keep every other field when rejecting an overlong label value.
            result, warnings = discard_overlong_labels(result)
            if not hasattr(self, 'field_warnings'):
                self.field_warnings = []
            self.field_warnings.extend(warnings)
            for warning in warnings:
                logger.warning('%s 超过六字，仅舍弃该标签: %s', warning['field'], warning['value'])
            # Missing Label1 is a manual-review finding, not a reason to lose
            # University_CN/Country_CN and the other extracted labels.
            errors = validation_errors(result, "stage3", allow_empty_label1=True)
            if errors:
                return False, {}, "; ".join(errors)
            
            if not result['WX_Label1'] and not warnings:
                logger.warning('WX_Label1 为空，保留阶段三其他字段，交给审核及人工补充')
            logger.info("阶段3提取完成，结果将进入审核")
            return True, result, ""
            
        except Exception as e:
            error_msg = f"阶段3分析异常: {str(e)}"
            logger.error(error_msg)
            return False, {}, error_msg
    
    def _post_process_results(self, results: Dict) -> Dict:
        """后处理分析结果，现在支持部分结果处理"""
        logger.info("开始后处理分析结果")
        
        # 只为阶段1和阶段3的字段添加空值（如果不存在）；阶段2字段只保留 LLM 返回的，不补空值
        for field in STAGE1_FIELDS + STAGE3_FIELDS:
            if field not in results:
                results[field] = ""
        
        # 注意：不为阶段2字段添加空值，只处理LLM返回的字段
        
        # 数据类型转换和清理（只处理已存在的字段）
        results = self._clean_and_convert_data(results)
        
        # 业务规则验证和修正（对部分结果进行适当的规则应用）
        try:
            results = self._apply_business_rules(results)
        except Exception as e:
            logger.warning(f"业务规则应用失败（部分结果可能导致）: {e}")
            # 对于部分结果，业务规则失败不应该阻止处理
        
        logger.info("结果后处理完成")
        return results
    
    def _clean_and_convert_data(self, results: Dict) -> Dict:
        """清理和转换数据"""
        # 清理字符串字段（只处理已存在的字段）
        string_fields = ['Deadline', 'Direction', 'University_EN', 'Contact_Name', 'Contact_Email',
                        'University_CN', 'Country_CN', 'WX_Label1', 'WX_Label2', 'WX_Label3']
        
        for field in string_fields:
            if field in results:
                value = str(results[field]).strip()
                results[field] = value if value else ""
        
        # 特殊处理：清理邮箱格式（将[at]、(at)等转换为@）
        if 'Contact_Email' in results and results['Contact_Email']:
            original_email = results['Contact_Email']
            cleaned_email = clean_email_format(original_email)
            if cleaned_email != original_email:
                results['Contact_Email'] = cleaned_email
                logger.info(f"邮箱格式已清理: {original_email} -> {cleaned_email}")
        
        # 处理Number_Places字段
        if 'Number_Places' in results:
            try:
                # 尝试提取数字
                import re
                number_str = str(results['Number_Places']).strip()
                if number_str:
                    # 提取第一个数字
                    match = re.search(r'\d+', number_str)
                    if match:
                        results['Number_Places'] = match.group()
                    else:
                        results['Number_Places'] = ""
                else:
                    results['Number_Places'] = ""
            except:
                results['Number_Places'] = ""
        
        # 确保阶段2的二进制字段只有"1"（只处理已存在的字段，不添加空字符串）
        for field in STAGE2_FIELDS:
            if field in results:
                value = str(results[field]).strip()
                # 如果值不是"1"，则从结果中删除该字段（而不是设为空字符串）
                if value == "1":
                    results[field] = "1"
                else:
                    del results[field]
        
        return results
    
    def _apply_business_rules(self, results: Dict) -> Dict:
        """应用业务规则"""
        # 规则1：事件类型（Competition, Summer School, Conference, Workshop）不填Number_Places
        event_types = ['Competition', 'Summer School', 'Conference', 'Workshop']
        if any(results.get(event_type) == "1" for event_type in event_types):
            results['Number_Places'] = ""
        
        # 规则2：Deadline格式验证
        deadline = results.get('Deadline', '')
        if deadline and deadline != "Soon":
            import re
            # 简单的日期格式验证
            if not valid_deadline(deadline):
                raise ValueError(f"Deadline格式不正确: {deadline}，应为YYYY-MM-DD格式或'Soon'")
        
        # 规则3：联系人信息一致性
        if results.get('Contact_Name') == "-":
            results['Contact_Email'] = "-"
        elif results.get('Contact_Email') == "-" and results.get('Contact_Name') != "-":
            logger.warning("有联系人姓名但无邮箱")
        
        return results
    
    def get_model_info(self) -> Dict:
        """获取当前使用的模型信息"""
        return self.llm_agent.get_model_info()

def test_analysis_stage():
    """测试分析阶段管理器"""
    try:
        manager = AnalysisStageManager()
        
        # 测试文本
        test_text = """
        PhD Position in Remote Sensing and Machine Learning
        University of Cambridge, United Kingdom
        
        We are seeking a highly motivated PhD student to work on satellite-based forest monitoring using deep learning techniques. The project involves developing novel algorithms for analyzing time-series satellite imagery to detect deforestation patterns.
        
        Requirements:
        - Master's degree in Computer Science, Geography, or related field
        - Experience with machine learning and remote sensing
        - Programming skills in Python
        
        Application deadline: April 30, 2024
        Duration: 3.5 years
        Positions available: 1
        
        Contact: Prof. Sarah Johnson
        Email: s.johnson@cam.ac.uk
        """
        
        logger.info("测试完整分析流程...")
        
        success, results, error = manager.analyze_text_complete(test_text, 1)
        
        if success:
            logger.info("分析成功！")
            for key, value in results.items():
                logger.info(f"{key}: {value}")
        else:
            logger.error(f"分析失败: {error}")
        
    except Exception as e:
        logger.error(f"测试失败: {e}")

if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(level=logging.INFO)
    test_analysis_stage() 



