"""
Google Sheets API处理模块
"""
import os
import pickle
import pandas as pd
import logging
import warnings
import inflect
from typing import List, Dict, Optional, Tuple
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from config import (
    GOOGLE_CREDENTIALS_FILE, 
    GOOGLE_TOKEN_FILE, 
    GOOGLE_SPREADSHEET_ID, 
    GOOGLE_SCOPES,
    SHEET_NAME
)

# 配置pandas显示选项
pd.set_option('display.max_columns', None)

# 禁止显示警告
warnings.filterwarnings('ignore')

# 初始化用于数字到单词转换的变形引擎
p = inflect.engine()

logger = logging.getLogger(__name__)

class GoogleSheetsHandler:
    def __init__(self):
        self.spreadsheet_id = GOOGLE_SPREADSHEET_ID
        self.sheet_name = SHEET_NAME
        self.scopes = GOOGLE_SCOPES
        self.service = None
        self.df = None
        self.original_df = None
        self.sheet_id = None  # 用于批量操作
        
    def authorize_credentials(self):
        """授权Google API凭据"""
        creds = None
        
        # 检查是否存在token文件
        if GOOGLE_TOKEN_FILE.exists():
            try:
                with open(GOOGLE_TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
            except Exception as e:
                logger.warning(f"加载token文件失败: {e}")
        
        # 如果凭据无效或不存在，重新获取
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    logger.info("凭据刷新成功")
                except Exception as e:
                    logger.warning(f"凭据刷新失败: {e}")
                    creds = None
            
            if not creds:
                if not GOOGLE_CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(f"Google凭据文件不存在: {GOOGLE_CREDENTIALS_FILE}")
                
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(GOOGLE_CREDENTIALS_FILE), self.scopes)
                creds = flow.run_local_server(port=0)
                logger.info("获取新的授权凭据成功")
            
            # 保存凭据
            try:
                with open(GOOGLE_TOKEN_FILE, 'wb') as token:
                    pickle.dump(creds, token)
                logger.info("凭据保存成功")
            except Exception as e:
                logger.warning(f"凭据保存失败: {e}")
        
        return creds
    
    def _initialize_service(self):
        """初始化Google Sheets服务"""
        if self.service is None:
            creds = self.authorize_credentials()
            # 禁用文件缓存以避免警告（file_cache is only supported with oauth2client<4.0.0）
            self.service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
            logger.info("Google Sheets API服务初始化成功")
    
    def fetch_data(self, range_name: str) -> List[List]:
        """从Google表格获取数据"""
        self._initialize_service()
        
        try:
            sheet = self.service.spreadsheets()
            result = sheet.values().get(
                spreadsheetId=self.spreadsheet_id, 
                range=range_name
            ).execute()
            
            values = result.get('values', [])
            logger.info(f"从 {range_name} 获取到 {len(values)} 行数据")
            return values
            
        except Exception as e:
            logger.error(f"获取数据失败: {e}")
            return []
    
    def adjust_data_to_columns(self, data: List[List], column_headers: List[str]) -> List[List]:
        """调整列数对标标题行"""
        adjusted_data = []
        for row in data:
            # 为缺失的列添加None
            adjusted_row = row + [None] * (len(column_headers) - len(row))
            adjusted_data.append(adjusted_row)
        return adjusted_data
    
    def load_data(self) -> bool:
        """加载Google表格数据"""
        try:
            self._initialize_service()
            
            # 获取工作表信息
            spreadsheet = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()
            
            # 找到指定工作表的ID
            for sheet in spreadsheet['sheets']:
                if sheet['properties']['title'] == self.sheet_name:
                    self.sheet_id = sheet['properties']['sheetId']
                    break
            
            if self.sheet_id is None:
                logger.error(f"工作表 '{self.sheet_name}' 不存在")
                return False
            
            # 获取所有数据
            range_name = f"{self.sheet_name}"
            data = self.fetch_data(range_name)
            
            if not data:
                logger.error("未获取到任何数据")
                return False
            
            # 将数据转换为DataFrame
            headers = data[0] if data else []
            rows = data[1:] if len(data) > 1 else []
            
            # 调整数据以匹配列数
            adjusted_rows = self.adjust_data_to_columns(rows, headers)
            
            self.df = pd.DataFrame(adjusted_rows, columns=headers)
            self.original_df = self.df.copy()
            
            logger.info(f"成功加载Google表格数据")
            logger.info(f"数据行数: {len(self.df)}")
            logger.info(f"数据列数: {len(self.df.columns)}")
            
            # 验证必要列是否存在
            required_columns = ['Notes', 'Source', 'Verifier', 'Error']
            missing_columns = [col for col in required_columns if col not in self.df.columns]
            
            if missing_columns:
                logger.error(f"缺少必要列: {missing_columns}")
                return False
            
            # 预设可能需要字符串数据的列类型为object
            string_columns = [
                'Deadline', 'Direction', 'University_EN', 'Contact_Name', 'Contact_Email',
                'University_CN', 'Country_CN', 'WX_Label1', 'WX_Label2', 'WX_Label3',
                'WX_Label4', 'WX_Label5', 'Verifier', 'Error', 'Number_Places'
            ]
            
            for col in string_columns:
                if col in self.df.columns:
                    self.df[col] = self.df[col].astype('object')
            
            logger.info("已优化数据类型设置")
            return True
            
        except Exception as e:
            logger.error(f"加载Google表格数据失败: {e}")
            return False
    
    def get_unfilled_rows(self) -> List[int]:
        """获取需要处理的行索引（Verifier和Error都为空的行）"""
        if self.df is None:
            logger.error("数据未加载")
            return []
        
        # 筛选Verifier和Error都为空的行
        condition = (
            (self.df['Verifier'].isna() | (self.df['Verifier'] == '')) &
            (self.df['Error'].isna() | (self.df['Error'] == ''))
        )
        
        unfilled_indices = self.df[condition].index.tolist()
        logger.info(f"找到 {len(unfilled_indices)} 行需要处理")
        
        return unfilled_indices
    
    def get_row_data(self, row_index: int) -> Optional[Dict]:
        """获取指定行的数据"""
        if self.df is None or row_index not in self.df.index:
            return None
        
        row_data = self.df.loc[row_index].to_dict()
        
        # 清理NaN值
        for key, value in row_data.items():
            if pd.isna(value):
                row_data[key] = ""
        
        return row_data
    
    def extract_link_from_row(self, row_data: Dict) -> Tuple[Optional[str], bool]:
        """
        从行数据中提取链接
        优先级：Source > Notes
        
        Returns:
            tuple: (url, used_notes) - url为提取的链接，used_notes表示是否使用了Notes中的链接
        """
        from utils import extract_url_from_text, is_valid_url
        
        # 优先从Source中获取链接
        source = row_data.get('Source', '')
        if source:
            # Source可能直接是URL，也可能包含URL
            if is_valid_url(source.strip()):
                logger.info(f"从Source中获取到链接: {source.strip()}")
                return (source.strip(), False)
            else:
                # 尝试从Source中提取URL
                url = extract_url_from_text(source)
                if url:
                    logger.info(f"从Source中提取到链接: {url}")
                    return (url, False)
        
        # 如果Source中没有链接，尝试从Notes中提取
        notes = row_data.get('Notes', '')
        if notes:
            url = extract_url_from_text(notes)
            if url:
                logger.info(f"从Notes中提取到链接: {url}")
                return (url, True)
        
        logger.warning("未找到有效链接")
        return (None, False)
    
    def update_row_data(self, row_index: int, update_data: Dict, verifier: str = "LLM") -> bool:
        """更新指定行的数据"""
        try:
            if self.df is None or row_index not in self.df.index:
                logger.error(f"无效的行索引: {row_index}")
                return False
            
            # 更新本地DataFrame
            for column, value in update_data.items():
                if column in self.df.columns:
                    if pd.isna(self.df.loc[row_index, column]) or self.df.loc[row_index, column] == '':
                        if self.df[column].dtype in ['float64', 'int64'] and isinstance(value, str):
                            self.df[column] = self.df[column].astype('object')
                    
                    self.df.loc[row_index, column] = value
                else:
                    logger.warning(f"列不存在: {column}")
            
            # 设置验证人（只有verifier不为空时才设置）
            if 'Verifier' in self.df.columns and verifier:
                if self.df['Verifier'].dtype in ['float64', 'int64']:
                    self.df['Verifier'] = self.df['Verifier'].astype('object')
                self.df.loc[row_index, 'Verifier'] = verifier
            
            logger.info(f"成功更新行 {row_index} 的数据")
            return True
            
        except Exception as e:
            logger.error(f"更新行数据失败: {e}")
            return False
    
    def update_row_error(self, row_index: int, error_message: str) -> bool:
        """更新指定行的错误信息"""
        try:
            if self.df is None or row_index not in self.df.index:
                logger.error(f"无效的行索引: {row_index}")
                return False
            
            # 确保Error列可以存储字符串
            if 'Error' in self.df.columns:
                if self.df['Error'].dtype in ['float64', 'int64']:
                    self.df['Error'] = self.df['Error'].astype('object')
                self.df.loc[row_index, 'Error'] = error_message
            
            logger.info(f"更新行 {row_index} 错误信息: {error_message}")
            return True
            
        except Exception as e:
            logger.error(f"更新错误信息失败: {e}")
            return False
    
    def save_data(self) -> bool:
        """保存数据到Google表格"""
        try:
            if self.df is None:
                logger.error("没有数据需要保存")
                return False
            
            self._initialize_service()
            
            # 准备数据：包含标题行
            headers = self.df.columns.tolist()
            data_rows = self.df.fillna('').values.tolist()  # 将NaN替换为空字符串
            all_data = [headers] + data_rows
            
            # 更新整个工作表
            range_name = f"{self.sheet_name}"
            self.update_data_in_sheet(range_name, all_data)
            
            logger.info(f"数据已保存到Google表格: {self.spreadsheet_id}")
            return True
            
        except Exception as e:
            logger.error(f"保存数据失败: {e}")
            return False
    
    def update_data_in_sheet(self, range_name: str, data: List[List]):
        """更新指定范围内的数据"""
        self._initialize_service()
        
        try:
            sheet = self.service.spreadsheets()
            body = {'values': data}
            result = sheet.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            
            updated_rows = result.get('updatedRows', 0)
            logger.info(f"{updated_rows} 行已更新")
            
        except Exception as e:
            logger.error(f"更新数据失败: {e}")
            raise
    
    def append_data_to_sheet(self, range_name: str, data: List[List]):
        """向Google表格添加数据"""
        self._initialize_service()
        
        try:
            sheet = self.service.spreadsheets()
            body = {'values': data}
            result = sheet.values().append(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                valueInputOption='USER_ENTERED',
                body=body,
                insertDataOption='INSERT_ROWS'
            ).execute()
            
            updated_rows = result.get('updates', {}).get('updatedRows', 0)
            logger.info(f"{updated_rows} 行已添加")
            
        except Exception as e:
            logger.error(f"添加数据失败: {e}")
            raise
    
    def delete_rows_from_sheet(self, rows_to_delete: List[int]):
        """从Google表格中删除行"""
        if not rows_to_delete:
            return
        
        self._initialize_service()
        
        try:
            # 按降序排列，从后往前删除，避免索引变化
            rows_to_delete.sort(reverse=True)
            
            batch_requests = []
            for row_index in rows_to_delete:
                # Google Sheets API使用从0开始的索引，且需要+1因为有标题行
                start_index = row_index + 1
                batch_requests.append({
                    "deleteDimension": {
                        "range": {
                            "sheetId": self.sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_index,
                            "endIndex": start_index + 1
                        }
                    }
                })
            
            batch_update_body = {"requests": batch_requests}
            
            request = self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, 
                body=batch_update_body
            )
            response = request.execute()
            
            logger.info(f"{len(rows_to_delete)} 行已删除")
            
        except Exception as e:
            logger.error(f"删除行失败: {e}")
            raise
    
    def get_statistics(self) -> Dict:
        """获取处理统计信息"""
        if self.df is None:
            return {}
        
        total_rows = len(self.df)
        filled_rows = len(self.df[self.df['Verifier'].notna() & (self.df['Verifier'] != '')])
        # "N"表示"已审核"，单独统计
        reviewed_rows = len(self.df[self.df['Error'] == 'N'])
        # 真正的错误行（Error不为空且不为"N"）
        error_rows = len(self.df[self.df['Error'].notna() & (self.df['Error'] != '') & (self.df['Error'] != 'N')])
        # 待处理：Verifier和Error都为空的行
        pending_rows = len(self.df[
            (self.df['Verifier'].isna() | (self.df['Verifier'] == '')) &
            (self.df['Error'].isna() | (self.df['Error'] == ''))
        ])
        
        stats = {
            'total_rows': total_rows,
            'filled_rows': filled_rows,
            'reviewed_rows': reviewed_rows,
            'error_rows': error_rows,
            'pending_rows': pending_rows,
            'completion_rate': filled_rows / total_rows * 100 if total_rows > 0 else 0
        }
        
        return stats
    
    def print_statistics(self):
        """打印统计信息"""
        stats = self.get_statistics()
        if not stats:
            logger.info("暂无统计信息")
            return
        
        logger.info("=" * 50)
        logger.info("处理统计信息:")
        logger.info(f"总行数: {stats['total_rows']}")
        logger.info(f"已完成: {stats['filled_rows']}")
        logger.info(f"已审核: {stats['reviewed_rows']}")
        logger.info(f"错误: {stats['error_rows']}")
        logger.info(f"待处理: {stats['pending_rows']}")
        logger.info(f"完成率: {stats['completion_rate']:.1f}%")
        logger.info("=" * 50)
    
    def is_date(self, string: str) -> bool:
        """转换日期"""
        try:
            pd.to_datetime(string)
            return True
        except ValueError:
            return False

def validate_analysis_result(result: Dict, stage: str) -> bool:
    """验证分析结果的格式"""
    if not isinstance(result, dict):
        logger.error(f"{stage} 分析结果不是字典格式")
        return False
    
    # 定义每个阶段应该包含的字段
    stage_fields = {
        'stage1': ['Deadline', 'Number_Places', 'Direction', 'University_EN', 'Contact_Name', 'Contact_Email'],
        'stage2': ['Master Student', 'Doctoral Student', 'PostDoc', 'Research Assistant', 
                  'Competition', 'Summer School', 'Conference', 'Workshop',
                  'Physical_Geo', 'Human_Geo', 'Urban', 'GIS', 'RS', 'GNSS'],
        'stage3': ['University_CN', 'Country_CN', 'WX_Label1', 'WX_Label2', 'WX_Label3', 'WX_Label4', 'WX_Label5']
    }
    
    expected_fields = stage_fields.get(stage, [])
    
    # 检查必要字段是否存在
    missing_fields = [field for field in expected_fields if field not in result]
    if missing_fields:
        logger.error(f"{stage} 分析结果缺少字段: {missing_fields}")
        return False
    
    logger.info(f"{stage} 分析结果格式验证通过")
    return True 