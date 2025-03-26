from pathlib import Path
import logging
from fastapi import APIRouter, HTTPException

from app.core.config import CONFIG_PATH, read_config, update_config

router = APIRouter(prefix="/config", tags=["配置管理"])
logger = logging.getLogger('youtube_live')

@router.get("")
async def get_config():
    """获取当前配置"""
    if not CONFIG_PATH.exists():
        return {"status": "error", "message": "配置文件不存在"}
    
    try:
        config = read_config()
        
        # 删除敏感信息
        if 'RTMP_URL' in config:
            config['RTMP_URL'] = '***隐藏***'
        
        return {"status": "success", "config": config}
    except Exception as e:
        logger.error(f"读取配置失败: {str(e)}")
        return {"status": "error", "message": str(e)}

@router.put("")
async def update_system_config(new_config: dict):
    """更新系统配置"""
    try:
        # 验证必要配置项
        required_fields = ['video_dir', 'watermark_path', 'auto_stop_minutes']
        missing_fields = [field for field in required_fields if field not in new_config]
        
        if missing_fields:
            return {
                "status": "error", 
                "message": f"缺少必要配置字段: {', '.join(missing_fields)}"
            }

        # 确保目录存在
        Path(new_config['video_dir']).mkdir(parents=True, exist_ok=True)
        Path(new_config['watermark_path']).parent.mkdir(parents=True, exist_ok=True)

        # 保存配置
        result = update_config(new_config)
        if result:
            logger.info(f"配置已更新: {', '.join(new_config.keys())}")
            return {"status": "success", "message": "配置已更新"}
        else:
            return {"status": "error", "message": "配置更新失败"}
    except Exception as e:
        logger.exception(f"更新配置失败: {str(e)}")
        return {"status": "error", "message": str(e)} 