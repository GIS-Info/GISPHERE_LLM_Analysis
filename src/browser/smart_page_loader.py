#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能页面加载检测模块
提供多策略的页面加载完成检测，确保动态内容完全加载
"""
import logging
from typing import Optional, Dict, List
from playwright.sync_api import Page
import time

logger = logging.getLogger(__name__)

class SmartPageLoader:
    """智能页面加载器 - 检测页面是否真正加载完成"""
    
    def __init__(self, 
                 max_wait_time: int = 60,
                 initial_wait: float = 15.0,
                 stability_check_interval: float = 1.0,
                 stability_threshold: int = 3,
                 min_content_length: int = 500):
        """
        初始化智能页面加载器
        
        Args:
            max_wait_time: 最大等待时间（秒）
            initial_wait: 智能检测前的初始等待时间（秒），给页面足够时间加载
            stability_check_interval: 内容稳定性检查间隔（秒）
            stability_threshold: 内容稳定次数阈值
            min_content_length: 最小内容长度阈值
        """
        self.max_wait_time = max_wait_time
        self.initial_wait = initial_wait
        self.stability_check_interval = stability_check_interval
        self.stability_threshold = stability_threshold
        self.min_content_length = min_content_length
    
    def wait_for_page_load(self, page: Page, url: str, 
                          custom_selectors: Optional[List[str]] = None) -> Dict[str, any]:
        """
        智能等待页面加载完成
        
        使用多种策略检测页面是否真正加载完成：
        1. 基础网络空闲检测
        2. 关键元素出现检测
        3. DOM内容稳定性检测
        4. 页面高度稳定性检测
        
        Args:
            page: Playwright页面对象
            url: 页面URL
            custom_selectors: 自定义的关键元素选择器列表
            
        Returns:
            dict: {
                'success': bool,  # 是否成功加载
                'strategy': str,  # 使用的加载策略
                'wait_time': float,  # 实际等待时间
                'content_length': int,  # 内容长度
                'warnings': list  # 警告信息
            }
        """
        start_time = time.time()
        result = {
            'success': False,
            'strategy': 'unknown',
            'wait_time': 0,
            'content_length': 0,
            'warnings': []
        }
        
        try:
            logger.info(f"开始智能页面加载检测: {url}")
            
            # 初始等待：给页面足够的时间进行初始加载
            if self.initial_wait > 0:
                logger.info(f"初始等待 {self.initial_wait} 秒，让页面有足够时间加载...")
                page.wait_for_timeout(int(self.initial_wait * 1000))
                logger.info(f"✓ 初始等待完成")
            
            # 策略1: 基础网络空闲等待（作为初始等待）
            try:
                logger.info("策略1: 等待网络空闲...")
                remaining_timeout = self.max_wait_time - (time.time() - start_time)
                if remaining_timeout > 0:
                    page.wait_for_load_state('networkidle', timeout=min(30000, int(remaining_timeout * 1000)))
                    logger.info("✓ 网络空闲状态达到")
                    result['strategy'] = 'networkidle'
                else:
                    logger.warning("剩余时间不足，跳过网络空闲检测")
            except Exception as e:
                logger.warning(f"网络空闲等待超时（这是正常的）: {e}")
                result['warnings'].append("网络空闲等待超时")
            
            # 策略2: 等待关键元素出现
            key_element_found = self._wait_for_key_elements(page, custom_selectors, start_time)
            if key_element_found:
                result['strategy'] = 'key_element'
                logger.info(f"✓ 关键元素检测成功，策略: {result['strategy']}")
            
            # 策略3: DOM内容稳定性检测
            if self._wait_for_content_stability(page, start_time):
                result['strategy'] = 'content_stability'
                logger.info("✓ 内容稳定性检测通过")
            
            # 策略4: 页面高度稳定性检测
            if self._wait_for_height_stability(page, start_time):
                if result['strategy'] != 'content_stability':
                    result['strategy'] = 'height_stability'
                logger.info("✓ 页面高度稳定")
            
            # 最终内容检查
            content_length = self._get_content_length(page)
            result['content_length'] = content_length
            
            if content_length >= self.min_content_length:
                result['success'] = True
                logger.info(f"✅ 页面加载成功，内容长度: {content_length}")
            else:
                result['success'] = True  # 即使内容少，也认为加载成功
                result['warnings'].append(f"内容较少: {content_length} < {self.min_content_length}")
                logger.warning(f"⚠️  页面内容较少: {content_length} 字符")
            
        except Exception as e:
            logger.error(f"智能页面加载检测失败: {e}")
            result['warnings'].append(f"检测异常: {str(e)}")
            # 即使失败，也尝试获取已加载的内容
            try:
                result['content_length'] = self._get_content_length(page)
                result['success'] = result['content_length'] > 0
            except:
                pass
        
        finally:
            result['wait_time'] = time.time() - start_time
            logger.info(f"页面加载检测完成，耗时: {result['wait_time']:.2f}秒，策略: {result['strategy']}")
        
        return result
    
    def _wait_for_key_elements(self, page: Page, custom_selectors: Optional[List[str]], 
                               start_time: float) -> bool:
        """
        等待关键元素出现
        
        Args:
            page: Playwright页面对象
            custom_selectors: 自定义选择器列表
            start_time: 开始时间
            
        Returns:
            bool: 是否找到关键元素
        """
        # 常见的主要内容选择器
        default_selectors = [
            'main',
            'article',
            '[role="main"]',
            '#main',
            '#content',
            '.content',
            '.main-content',
            'body > div:first-child',  # 通常是主容器
        ]
        
        # 合并自定义选择器
        selectors = custom_selectors if custom_selectors else default_selectors
        
        logger.info(f"策略2: 等待关键元素出现（检测 {len(selectors)} 个选择器）...")
        
        for selector in selectors:
            try:
                remaining_time = self.max_wait_time - (time.time() - start_time)
                if remaining_time <= 0:
                    logger.warning("关键元素检测超时")
                    return False
                
                timeout = min(10000, int(remaining_time * 1000))  # 每个选择器最多等10秒
                page.wait_for_selector(selector, timeout=timeout, state='visible')
                logger.info(f"✓ 找到关键元素: {selector}")
                
                # 找到元素后，额外等待一小段时间让内容完全渲染
                page.wait_for_timeout(1000)
                return True
                
            except Exception as e:
                logger.debug(f"未找到元素 {selector}: {e}")
                continue
        
        logger.warning("未找到任何关键元素")
        return False
    
    def _wait_for_content_stability(self, page: Page, start_time: float) -> bool:
        """
        等待DOM内容稳定（不再变化）
        
        Args:
            page: Playwright页面对象
            start_time: 开始时间
            
        Returns:
            bool: 内容是否稳定
        """
        logger.info("策略3: 检测DOM内容稳定性...")
        
        stable_count = 0
        last_content_hash = None
        
        while stable_count < self.stability_threshold:
            elapsed = time.time() - start_time
            if elapsed >= self.max_wait_time:
                logger.warning("内容稳定性检测超时")
                return stable_count >= 2  # 至少稳定2次也算部分成功
            
            try:
                # 获取主要内容区域的文本内容哈希
                current_content_hash = page.evaluate("""
                    () => {
                        // 获取主要内容区域
                        const mainElements = [
                            document.querySelector('main'),
                            document.querySelector('article'),
                            document.querySelector('[role="main"]'),
                            document.querySelector('#content'),
                            document.body
                        ];
                        
                        const mainElement = mainElements.find(el => el !== null) || document.body;
                        const text = mainElement.innerText || '';
                        
                        // 简单哈希
                        let hash = 0;
                        for (let i = 0; i < text.length; i++) {
                            const char = text.charCodeAt(i);
                            hash = ((hash << 5) - hash) + char;
                            hash = hash & hash;
                        }
                        return {hash: hash, length: text.length};
                    }
                """)
                
                current_hash = current_content_hash.get('hash', 0)
                content_length = current_content_hash.get('length', 0)
                
                if last_content_hash is not None:
                    if current_hash == last_content_hash:
                        stable_count += 1
                        logger.debug(f"内容稳定次数: {stable_count}/{self.stability_threshold} (长度: {content_length})")
                    else:
                        stable_count = 0  # 内容变化，重置计数
                        logger.debug(f"内容仍在变化 (长度: {content_length})")
                
                last_content_hash = current_hash
                
                # 等待一段时间再检查
                page.wait_for_timeout(int(self.stability_check_interval * 1000))
                
            except Exception as e:
                logger.warning(f"内容稳定性检测出错: {e}")
                return False
        
        logger.info(f"✓ 内容已稳定（连续 {stable_count} 次检测无变化）")
        return True
    
    def _wait_for_height_stability(self, page: Page, start_time: float) -> bool:
        """
        等待页面高度稳定
        
        Args:
            page: Playwright页面对象
            start_time: 开始时间
            
        Returns:
            bool: 页面高度是否稳定
        """
        logger.info("策略4: 检测页面高度稳定性...")
        
        stable_count = 0
        last_height = None
        
        while stable_count < self.stability_threshold:
            elapsed = time.time() - start_time
            if elapsed >= self.max_wait_time:
                logger.warning("页面高度稳定性检测超时")
                return stable_count >= 2
            
            try:
                current_height = page.evaluate("document.body.scrollHeight")
                
                if last_height is not None:
                    if current_height == last_height:
                        stable_count += 1
                        logger.debug(f"高度稳定次数: {stable_count}/{self.stability_threshold} (高度: {current_height}px)")
                    else:
                        stable_count = 0
                        logger.debug(f"页面高度仍在变化: {last_height}px -> {current_height}px")
                
                last_height = current_height
                
                # 等待一段时间再检查
                page.wait_for_timeout(int(self.stability_check_interval * 1000))
                
            except Exception as e:
                logger.warning(f"页面高度检测出错: {e}")
                return False
        
        logger.info(f"✓ 页面高度已稳定: {last_height}px")
        return True
    
    def _get_content_length(self, page: Page) -> int:
        """
        获取页面主要内容长度
        
        Args:
            page: Playwright页面对象
            
        Returns:
            int: 内容长度
        """
        try:
            content_length = page.evaluate("""
                () => {
                    const mainElements = [
                        document.querySelector('main'),
                        document.querySelector('article'),
                        document.querySelector('[role="main"]'),
                        document.querySelector('#content'),
                        document.body
                    ];
                    
                    const mainElement = mainElements.find(el => el !== null) || document.body;
                    return (mainElement.innerText || '').length;
                }
            """)
            return content_length
        except Exception as e:
            logger.warning(f"获取内容长度失败: {e}")
            return 0
    
    def wait_for_page_with_retry(self, page: Page, url: str, 
                                 max_retries: int = 2,
                                 custom_selectors: Optional[List[str]] = None) -> Dict[str, any]:
        """
        带重试的智能页面加载等待
        
        如果第一次加载失败或内容不足，会自动重试
        
        Args:
            page: Playwright页面对象
            url: 页面URL
            max_retries: 最大重试次数
            custom_selectors: 自定义关键元素选择器
            
        Returns:
            dict: 加载结果
        """
        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.info(f"重试页面加载 (尝试 {attempt + 1}/{max_retries + 1})...")
                try:
                    page.reload(wait_until='domcontentloaded', timeout=30000)
                except Exception as e:
                    logger.warning(f"页面重载失败: {e}")
            
            result = self.wait_for_page_load(page, url, custom_selectors)
            
            if result['success'] and result['content_length'] >= self.min_content_length:
                if attempt > 0:
                    logger.info(f"✅ 重试成功 (尝试 {attempt + 1} 次)")
                return result
            
            if attempt < max_retries:
                logger.warning(f"页面加载不完整，准备重试...")
                time.sleep(2)  # 重试前等待2秒
        
        logger.warning("所有重试均失败，返回最后一次结果")
        return result


def create_smart_loader(config: Optional[Dict] = None) -> SmartPageLoader:
    """
    创建智能页面加载器实例
    
    Args:
        config: 配置字典，支持的键：
            - max_wait_time: 最大等待时间（秒）
            - initial_wait: 智能检测前的初始等待时间（秒）
            - stability_check_interval: 稳定性检查间隔（秒）
            - stability_threshold: 稳定性阈值（次）
            - min_content_length: 最小内容长度
            
    Returns:
        SmartPageLoader实例
    """
    config = config or {}
    return SmartPageLoader(
        max_wait_time=config.get('max_wait_time', 60),
        initial_wait=config.get('initial_wait', 15.0),
        stability_check_interval=config.get('stability_check_interval', 1.0),
        stability_threshold=config.get('stability_threshold', 3),
        min_content_length=config.get('min_content_length', 500)
    )




