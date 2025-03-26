import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import platform
import re
import os
import sys
import time
import pytz

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')

# 自定义时间格式化器，使用北京时区
class BeijingTimeFormatter(logging.Formatter):
    """使用北京时区的日志格式化器"""
    
    def formatTime(self, record, datefmt=None):
        """重写时间格式化方法，使用北京时区"""
        # 将时间戳转换为datetime对象，然后应用北京时区
        dt = datetime.fromtimestamp(record.created).astimezone(beijing_tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]

# 配置日志路径
def get_logs_dir():
    """获取日志目录路径"""
    if platform.system().lower() == 'windows':
        data_dir = Path("data")
        return data_dir / "logs"
    else:
        # Linux环境使用/var/youtube_live/data/logs
        return Path('/var/youtube_live/data/logs')

LOGS_DIR = get_logs_dir()

def ensure_logs_dir():
    """确保日志存储目录存在"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    print(f'确保日志存储目录存在: {LOGS_DIR}')

def cleanup_old_logs():
    """清理7天以前的日志文件"""
    try:
        current_time = datetime.now(beijing_tz)
        threshold = current_time - timedelta(days=7)
        
        # 检查所有任务日志
        count = 0
        for log_file in LOGS_DIR.glob('ffmpeg_task_*.log'):
            try:
                # 获取文件修改时间
                mod_time = datetime.fromtimestamp(log_file.stat().st_mtime)
                if mod_time < threshold:
                    log_file.unlink()  # 删除旧文件
                    count += 1
            except Exception as e:
                print(f'删除旧日志文件失败: {log_file}, 错误: {str(e)}')
                logging.error(f'删除旧日志文件失败: {log_file}, 错误: {str(e)}')
        
        if count > 0:
            print(f'已清理 {count} 个超过7天的任务日志文件')
            logging.info(f'已清理 {count} 个超过7天的任务日志文件')
            
    except Exception as e:
        print(f'日志清理失败: {str(e)}')
        logging.error(f'日志清理失败: {str(e)}')

def setup_logging():
    """设置日志系统"""
    ensure_logs_dir()  
    
    # 主应用日志配置
    main_log_file = LOGS_DIR / 'app.log'
    
    # 使用RotatingFileHandler进行日志轮转
    file_handler = RotatingFileHandler(
        main_log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,          # 保留5个备份文件
        encoding='utf-8'
    )
    
    # 控制台日志处理器
    console_handler = logging.StreamHandler()
    
    # 设置日志格式，使用北京时区格式化器
    log_formatter = BeijingTimeFormatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    )
    file_handler.setFormatter(log_formatter)
    console_handler.setFormatter(log_formatter)
    
    # 配置根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # 返回主日志器
    return logging.getLogger('youtube_live')

# FFmpeg日志过滤器
class FFmpegLogFilter(logging.Filter):
    """FFmpeg任务日志过滤器
    
    用于过滤FFmpeg日志中的重复和非关键信息，减少日志冗余
    """
    def __init__(self):
        super().__init__()
        # 上一条记录的消息内容
        self.last_message = ""
        # 重复消息计数
        self.repeat_count = 0
        # 进度信息计数
        self.progress_count = 0
        
        # 进度信息匹配模式
        self.progress_patterns = [
            'frame=', 'fps=', 'size=', 'time=', 
            'bitrate=', 'speed=', 'dup=', 'drop=',
            'progress='
        ]
        
        # 冗余信息匹配模式
        self.redundant_patterns = [
            '开始创建重连函数时间',
            '已加载代理配置',
            '重连信息:',
            '视频文件:',
            'RTMP地址:',
            '转码设置:',
            '代理设置:',
            '执行重连函数...',
            '-------- 新重连进程 --------',
            '-------'
        ]
    
    def filter(self, record):
        # 获取当前日志消息
        message = record.getMessage()
        
        # 检查是否与上一条消息完全相同
        if message == self.last_message:
            self.repeat_count += 1
            # 每10条重复消息只记录1条，并标记重复次数
            if self.repeat_count >= 10:
                record.msg = f"{record.msg} (重复 {self.repeat_count} 次)"
                self.repeat_count = 0
                self.last_message = message
                return True
            return False
        
        # 当有新消息时，重置重复计数
        if self.repeat_count > 0:
            # 如果之前有重复消息，在新消息前记录一下重复次数
            if self.repeat_count > 1:
                self.repeat_count = 0
        
        # 保存当前消息作为上一条消息
        self.last_message = message
        
        # 检查是否为进度信息
        for pattern in self.progress_patterns:
            if pattern in message:
                self.progress_count += 1
                # 只记录每100条进度信息中的1条
                return self.progress_count % 100 == 0
        
        # 检查是否为冗余信息
        for pattern in self.redundant_patterns:
            if pattern in message:
                # 对于冗余信息，减少记录频率
                return len(message) < 50  # 只记录较短的消息，避免冗长输出
        
        # 对于错误和警告信息，始终记录
        if record.levelno >= logging.WARNING:
            return True
            
        # 其他消息正常记录
        return True

def get_task_logger(task_id):
    """获取特定任务的日志记录器"""
    logger_name = f'task_{task_id}'
    logger = logging.getLogger(logger_name)
    
    # 检查是否已经配置了处理器
    if not logger.handlers:
        # 获取应用根目录的绝对路径
        app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        
        # 使用绝对路径创建日志目录
        log_dir = os.path.join(app_root, 'logs', 'tasks')
        os.makedirs(log_dir, exist_ok=True)
        
        # 创建日志文件路径
        log_file = os.path.join(log_dir, f"{task_id}.log")
        
        try:
            # 尝试创建文件处理器
            file_handler = logging.FileHandler(log_file)
            
            # 设置格式器
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', 
                                          datefmt='%Y-%m-%d %H:%M:%S')
            file_handler.setFormatter(formatter)
            
            # 添加处理器并设置日志级别
            logger.addHandler(file_handler)
            logger.setLevel(logging.INFO)
            
            # 记录日志系统初始化成功
            logger.info(f"任务日志初始化成功 - 日志文件: {log_file}")
        except Exception as e:
            # 记录日志初始化失败
            sys_logger = logging.getLogger('youtube_live')
            sys_logger.error(f"无法为任务 {task_id} 创建日志文件: {str(e)}")
    
    return logger

def get_task_log_path(task_id):
    """获取任务日志文件路径"""
    return LOGS_DIR / f'ffmpeg_task_{task_id}.log' 