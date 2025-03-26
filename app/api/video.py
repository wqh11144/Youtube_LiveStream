import os
import shutil
import tempfile
import platform
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, File, UploadFile, HTTPException
from app.utils.file_utils import secure_filename
from app.core.config import SUPPORTED_VIDEO_FORMATS, read_config
import logging
import pytz

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')

router = APIRouter(prefix="/video", tags=["视频管理"])
logger = logging.getLogger('youtube_live')

@router.get("/list")
async def list_videos():
    """获取视频列表"""
    try:
        video_dir = Path('public/video')
        if not video_dir.exists():
            video_dir.mkdir(parents=True, exist_ok=True)
        files = [f.name for f in video_dir.glob('*') if f.suffix.lower() in SUPPORTED_VIDEO_FORMATS]
        return {"status": "success", "files": files}
    except Exception as e:
        logger.error(f"获取视频列表失败: {str(e)}")
        return {"status": "error", "message": f"获取视频列表失败: {str(e)}"}

@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    """上传视频文件"""
    temp_file = None
    try:
        # 读取配置
        config = read_config()
        max_file_size = config.get('max_file_size_mb', 100) * 1024 * 1024  # 转换为字节
        
        # 验证文件类型
        filename = file.filename.lower()
        if not any(filename.endswith(ext) for ext in SUPPORTED_VIDEO_FORMATS):
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件格式。支持的格式：{', '.join(SUPPORTED_VIDEO_FORMATS)}"
            )
        
        # 验证文件大小
        file_size = 0
        
        # 创建临时文件
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        try:
            # 分块读取并计算文件大小
            while chunk := await file.read(8192):  # 使用更小的块大小
                file_size += len(chunk)
                if file_size > max_file_size:
                    temp_file.close()
                    os.unlink(temp_file.name)
                    raise HTTPException(
                        status_code=400, 
                        detail=f"文件大小超过限制（最大{config.get('max_file_size_mb')}MB）"
                    )
                temp_file.write(chunk)
            
            # 确保所有数据都写入磁盘
            temp_file.flush()
            temp_file.close()

            # 确保目标目录存在
            video_dir = Path('public/video')
            video_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成安全的文件名
            safe_filename = secure_filename(file.filename)
            target_path = video_dir / safe_filename
            
            # 如果文件已存在，添加时间戳
            if target_path.exists():
                name, ext = os.path.splitext(safe_filename)
                timestamp = datetime.now(beijing_tz).strftime('%Y%m%d_%H%M%S')
                safe_filename = f"{name}_{timestamp}{ext}"
                target_path = video_dir / safe_filename
                
            # 移动临时文件到目标位置
            shutil.move(temp_file.name, target_path)
            
            # 设置文件权限
            if not platform.system() == 'Windows':
                os.chmod(target_path, 0o644)
                
            logger.info(f'文件上传成功: {safe_filename}')
            return {
                "status": "success",
                "filename": safe_filename,
                "size": file_size,
                "message": "文件上传成功"
            }
            
        except Exception as e:
            # 如果处理过程中出现错误，确保清理临时文件
            if os.path.exists(temp_file.name):
                try:
                    temp_file.close()
                    os.unlink(temp_file.name)
                except:
                    pass
            raise e
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")
    finally:
        # 确保在所有情况下都清理临时文件
        if temp_file and os.path.exists(temp_file.name):
            try:
                temp_file.close()
                os.unlink(temp_file.name)
            except:
                pass

@router.delete("/clear")
async def clear_videos():
    """清空所有视频文件"""
    try:
        from app.services.stream_service import active_processes, process_lock
        
        video_dir = Path('public/video')
        if not video_dir.exists():
            return {"status": "success", "message": "视频目录不存在"}
            
        # 获取所有视频文件
        video_files = [f for f in video_dir.glob('*') if f.suffix.lower() in SUPPORTED_VIDEO_FORMATS]
        
        # 检查是否有正在运行的任务使用这些视频
        videos_in_use = set()
        with process_lock:
            for process_info in active_processes.values():
                if 'video_path' in process_info:
                    videos_in_use.add(str(process_info['video_path']))
        
        # 删除未被使用的视频文件
        deleted_count = 0
        skipped_count = 0
        for video_file in video_files:
            try:
                if str(video_file) in videos_in_use:
                    logger.warning(f'视频文件正在使用中，跳过删除: {video_file.name}')
                    skipped_count += 1
                    continue
                    
                video_file.unlink()
                deleted_count += 1
                logger.info(f'已删除视频文件: {video_file.name}')
            except Exception as e:
                logger.error(f'删除视频文件失败 {video_file.name}: {str(e)}')
                
        message = f'已删除 {deleted_count} 个视频文件'
        if skipped_count > 0:
            message += f'，{skipped_count} 个文件因正在使用而跳过'
            
        return {
            "status": "success",
            "message": message,
            "deleted_count": deleted_count,
            "skipped_count": skipped_count
        }
        
    except Exception as e:
        error_msg = f"清空视频失败: {str(e)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg) 