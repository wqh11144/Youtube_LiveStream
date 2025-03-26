from pathlib import Path
import logging
import platform
import os
import json

# 全局常量
SUPPORTED_VIDEO_FORMATS = ('.mp4', '.mov', '.avi', '.flv')

# 获取应用根目录
def get_app_root():
    if platform.system().lower() == 'windows':
        return Path(__file__).parent.parent.parent
    else:
        return Path('/var/youtube_live')

# 定义数据目录
DATA_DIR = get_app_root() / "data"
LOGS_DIR = DATA_DIR / "logs"
TASKS_DIR = DATA_DIR / "tasks_history"
PROXY_CONFIGS_DIR = DATA_DIR / "proxy_configs"
CONFIG_PATH = get_app_root() / "config.json"

# 获取数据根目录
def get_data_root():
    if platform.system().lower() == 'windows':
        return get_app_root() / 'data'
    else:
        return Path('/var/youtube_live/data')

# 获取代理配置目录
def get_proxy_config_dir():
    if platform.system().lower() == 'windows':
        proxy_dir = get_data_root() / 'proxy_configs'
    else:
        proxy_dir = Path('/var/youtube_live/data/proxy_configs')
    
    # 确保目录存在并设置正确的权限
    if not proxy_dir.exists():
        proxy_dir.mkdir(parents=True, exist_ok=True)
        if platform.system().lower() != 'windows':
            os.chmod(proxy_dir, 0o755)
    return proxy_dir

# 获取日志目录
def get_log_dir():
    if platform.system().lower() == 'windows':
        return get_data_root() / 'logs'
    else:
        return Path('/var/youtube_live/data/logs')

# 获取任务历史目录
def get_tasks_dir():
    if platform.system().lower() == 'windows':
        return get_data_root() / 'tasks_history'
    else:
        return Path('/var/youtube_live/data/tasks_history')

# 获取系统临时目录（替代自定义tmp目录）
def get_temp_dir():
    if platform.system().lower() == 'windows':
        # 使用系统临时目录
        import tempfile
        return Path(tempfile.gettempdir())
    else:
        # 根据install.sh脚本中的设置
        return Path('/var/youtube_live/data/tmp')

# 获取环境变量配置
def get_env_vars():
    env = os.environ.copy()
    
    # 设置临时目录
    env['TMPDIR'] = str(get_temp_dir())
    
    # 如果是Linux系统，设置代理配置目录
    if platform.system().lower() != 'windows':
        env['PROXY_CONFIG_DIR'] = str(get_proxy_config_dir())
    
    return env

# 读取配置文件
def read_config():
    """读取配置文件"""
    if not CONFIG_PATH.exists():
        # 如果配置文件不存在，创建默认配置
        update_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 确保所有必要的配置项都存在
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
    except Exception as e:
        logger.error(f"读取配置文件失败: {str(e)}")
        return DEFAULT_CONFIG

# 更新配置文件
def update_config(new_config):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(new_config, f)
        return True
    except Exception as e:
        print(f"更新配置文件失败: {str(e)}")
        return False

# 默认配置
DEFAULT_CONFIG = {
    'video_dir': 'public/video',
    'watermark_path': 'public/watermark.png',
    'auto_stop_minutes': 60,
    'max_file_size_mb': 100  # 添加文件大小限制配置，默认100MB
} 