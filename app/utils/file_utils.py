import os
import re
import unicodedata
import hashlib
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
import platform
from app.core.config import get_proxy_config_dir, get_temp_dir
from app.services.task_service import beijing_tz

def secure_filename(filename: str) -> str:
    """
    安全化文件名，保留中文字符
    :param filename: 原始文件名
    :return: 安全的文件名
    """
    # 获取文件扩展名
    ext = Path(filename).suffix.lower()
    # 获取文件名（不含扩展名）
    name = Path(filename).stem
    
    # 只过滤特殊字符，保留中文、字母、数字、下划线和连字符
    name = re.sub(r'[^\w\-\u4e00-\u9fff]+', '_', name)
    
    # 确保文件名不以点或空格开头
    name = name.strip('._')
    
    # 如果文件名为空，使用默认名称
    if not name:
        name = 'unnamed'
        
    return name + ext

def create_proxy_config(task_id: str, proxy_ip: str, proxy_port: str, proxy_user: str = "", proxy_pass: str = "") -> Path:
    """创建代理配置文件

    Args:
        task_id (str): 任务ID
        proxy_ip (str): 代理IP
        proxy_port (str): 代理端口
        proxy_user (str, optional): 代理用户名，可选
        proxy_pass (str, optional): 代理密码，可选

    Returns:
        Path: 配置文件路径
    """
    try:
        import json
        proxy_dir = get_proxy_config_dir()
        temp_dir = get_temp_dir()
        
        # 确保目录存在
        os.makedirs(proxy_dir, exist_ok=True)
        os.makedirs(temp_dir, exist_ok=True)
        
        # 使用任务ID生成唯一的文件名，确保每个任务使用独立配置
        config_hash = hashlib.md5(f"{task_id}:{proxy_ip}:{proxy_port}".encode()).hexdigest()[:8]
        config_file = proxy_dir / f"proxy_{task_id}_{config_hash}.json"
        
        # 创建代理配置内容（JSON格式）
        proxy_config = {}
        
        # 根据是否有用户名密码构建不同的代理URL
        if proxy_user and proxy_pass:
            socks5_url = f"socks5://{proxy_user}:{proxy_pass}@{proxy_ip}:{proxy_port}"
        else:
            socks5_url = f"socks5://{proxy_ip}:{proxy_port}"
        
        # 添加SOCKS5特定配置
        proxy_config["socks5_proxy"] = socks5_url
        
        # 添加元数据
        proxy_config["created_at"] = datetime.now(beijing_tz).isoformat()
        proxy_config["task_id"] = task_id
        
        # 使用应用专用的临时目录
        with tempfile.NamedTemporaryFile(mode='w', delete=False, dir=temp_dir, encoding='utf-8') as temp_file:
            json.dump(proxy_config, temp_file, indent=2)
            temp_path = Path(temp_file.name)
        
        # 安全地移动到目标位置
        shutil.move(str(temp_path), str(config_file))
        
        # 设置适当的权限
        try:
            os.chmod(config_file, 0o644)
        except Exception as e:
            print(f"设置文件权限失败: {str(e)}")
        
        return config_file
    
    except Exception as e:
        raise Exception(f"创建代理配置文件失败: {str(e)}")

def cleanup_proxy_config(config_file: Path) -> bool:
    """清理代理配置文件

    Args:
        config_file (Path): 配置文件路径

    Returns:
        bool: 是否成功清理
    """
    try:
        if config_file and config_file.exists():
            config_file.unlink()
            return True
        else:
            return False
    except Exception as e:
        raise Exception(f"删除代理配置文件失败 {config_file}: {str(e)}")

def is_windows():
    """检查是否是Windows系统"""
    return platform.system().lower() == 'windows' 