from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class StartStreamRequest(BaseModel):
    rtmp_url: str
    video_filename: str
    task_name: Optional[str] = None
    auto_stop_minutes: int = 699
    transcode_enabled: bool = False
    socks5_proxy: Optional[str] = None
    scheduled_start_time: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "rtmp_url": "rtmp://a.rtmp.youtube.com/live2/xxxx-yyyy-zzzz",
                "video_filename": "video.mp4",
                "task_name": "我的直播任务",
                "auto_stop_minutes": 699,
                "transcode_enabled": False,
                "socks5_proxy": None,
                "scheduled_start_time": "2024-03-20T14:30:00"
            }
        }

class TaskResponse(BaseModel):
    status: str
    task_id: Optional[str] = None
    rtmp_url: Optional[str] = None
    video_filename: Optional[str] = None
    stream_mode: Optional[str] = None
    auto_stop_minutes: Optional[int] = None
    stop_time: Optional[str] = None
    use_proxy: Optional[bool] = None
    command: Optional[str] = None
    message: Optional[str] = None

class TaskInfo(BaseModel):
    id: str
    rtmp_url: str
    video_filename: str
    task_name: Optional[str] = None
    start_time: str
    create_time: str
    status: str
    auto_stop_minutes: Optional[int] = None
    transcode_enabled: bool = False
    socks5_proxy: Optional[str] = None
    scheduled_start_time: Optional[str] = None
    end_time: Optional[str] = None
    error_message: Optional[str] = None
    network_status: Optional[str] = None
    network_warning: bool = False
    retry_count: int = 0

class TaskListResponse(BaseModel):
    total_tasks: int
    tasks: List[TaskInfo]

class ConfigResponse(BaseModel):
    status: str
    config: Optional[Dict[str, Any]] = None
    message: Optional[str] = None 