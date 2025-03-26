import uuid
import logging
import subprocess
import datetime
import asyncio
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from datetime import timedelta
import time
import psutil

from app.models.schemas import StartStreamRequest, TaskInfo
from app.services.task_service import load_tasks, update_task_status, beijing_tz, save_tasks
from app.services.stream_service import active_processes, process_lock, video_executor, read_output, stop_stream, stop_stream_sync
from app.utils.video_utils import check_video_codec, get_ffmpeg_command, check_video_permissions
from app.utils.network_utils import test_rtmp_connection, validate_rtmp_url
from app.utils.file_utils import create_proxy_config, is_windows

router = APIRouter(prefix="/tasks", tags=["任务管理"])
logger = logging.getLogger('youtube_live')

# 用于调度自动停止的调度器
scheduler = BackgroundScheduler(
    timezone=pytz.timezone('Asia/Shanghai'),  # 确保使用北京时区
    job_defaults={'misfire_grace_time': 60}  # 添加一分钟的容错时间
)

# 确保调度器已启动
if not scheduler.running:
    scheduler.start()

@router.get("/list")
async def get_task_list(status: Optional[str] = None, sort_by: Optional[str] = None, limit: Optional[int] = 15):
    """获取任务列表，支持按状态筛选和排序"""
    try:
        # 记录请求参数
        logger.info(f"获取任务列表请求 - 参数: status={status}, sort_by={sort_by}, limit={limit}")
        
        # 加载指定数量的任务
        stored_tasks = load_tasks(limit=limit)
        logger.info(f"已加载任务数量: {len(stored_tasks)}")
        
        # 验证和修复任务数据
        for task in stored_tasks:
            validate_and_fix_task_times(task)
        
        # 记录加载的任务状态
        status_before = {task['id']: task.get('status', 'unknown') for task in stored_tasks}
        logger.info(f"加载的任务状态: {status_before}")
        
        # 更新活动进程的状态
        with process_lock:
            for task in stored_tasks:
                # 如果任务有结束时间，状态不应该是running
                if 'end_time' in task and task.get('status') == 'running':
                    task['status'] = 'error'
                    task['message'] = '任务异常退出'
                    logger.warning(f'检测到已结束的任务状态为running - task_id={task["id"]}')
                    continue
                
                # 第一次检查：如果任务状态为running但不在active_processes中
                if task.get('status') == 'running' and task['id'] not in active_processes:
                    logger.warning(f'检测到运行中的任务不在活动进程列表中 - task_id={task["id"]}')
                    
                    # 第二次检查：尝试在系统中查找相关的ffmpeg进程
                    try:
                        task_found = False
                        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                            if proc.info['name'] == 'ffmpeg':
                                cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                                if task['id'] in cmdline:
                                    task_found = True
                                    logger.info(f'在系统中找到对应的ffmpeg进程 - task_id={task["id"]}, pid={proc.info["pid"]}')
                                    break
                        
                        if not task_found:
                            # 如果在系统中也找不到相关进程，则将任务状态标记为error
                            task['status'] = 'error'
                            task['message'] = '任务异常退出 (无法找到进程)'
                            if 'end_time' not in task:
                                task['end_time'] = datetime.datetime.now(beijing_tz).isoformat()
                            logger.warning(f'未能在系统中找到对应的ffmpeg进程，标记任务为error - task_id={task["id"]}')
                            
                            # 将更新后的状态保存到任务文件
                            try:
                                update_task_status(task['id'], {
                                    'status': 'error',
                                    'message': '任务异常退出 (无法找到进程)',
                                    'end_time': task.get('end_time')
                                })
                                logger.info(f'已更新任务状态至磁盘 - task_id={task["id"]}')
                            except Exception as save_error:
                                logger.error(f'保存任务状态到磁盘失败 - task_id={task["id"]}: {str(save_error)}')
                    except Exception as e:
                        logger.error(f'尝试检查系统进程时发生错误 - task_id={task["id"]}: {str(e)}')
                        # 保守起见，仍然将状态标记为error
                        task['status'] = 'error'
                        task['message'] = '任务状态检查失败'
                        if 'end_time' not in task:
                            task['end_time'] = datetime.datetime.now(beijing_tz).isoformat()
                            
                        # 将更新后的状态保存到任务文件
                        try:
                            update_task_status(task['id'], {
                                'status': 'error',
                                'message': '任务状态检查失败',
                                'end_time': task.get('end_time')
                            })
                            logger.info(f'已更新异常任务状态至磁盘 - task_id={task["id"]}')
                        except Exception as save_error:
                            logger.error(f'保存异常任务状态到磁盘失败 - task_id={task["id"]}: {str(save_error)}')
                    continue
                    
                if task['id'] in active_processes:
                    process = active_processes[task['id']]['process']
                    is_running = process.poll() is None
                    
                    if is_running:
                        task['status'] = 'running'
                    elif 'stopped_by_user' in active_processes[task['id']] and active_processes[task['id']]['stopped_by_user']:
                        task['status'] = 'stopped'
                    elif 'auto_stopped' in active_processes[task['id']] and active_processes[task['id']]['auto_stopped']:
                        task['status'] = 'auto_stopped'
                    elif process.poll() != 0:
                        task['status'] = 'error'
                    else:
                        task['status'] = 'completed'
                    
                    # 更新结束时间
                    if not is_running and 'end_time' not in task:
                        task['end_time'] = datetime.datetime.now(beijing_tz).isoformat()
        
        # 记录更新后的任务状态
        status_after = {task['id']: task.get('status', 'unknown') for task in stored_tasks}
        logger.info(f"更新后的任务状态: {status_after}")
        
        # 按状态筛选
        if status:
            filtered_tasks = [task for task in stored_tasks if task.get('status') == status]
            logger.info(f"按状态 '{status}' 筛选后的任务数量: {len(filtered_tasks)}")
            stored_tasks = filtered_tasks
        
        # 排序
        if sort_by:
            reverse = True if sort_by == 'start_time' else False
            stored_tasks.sort(key=lambda x: x.get(sort_by, ''), reverse=reverse)
            logger.info(f"按 '{sort_by}' 排序完成")
        
        # 记录最终返回的任务状态分布
        final_status_counts = {}
        for task in stored_tasks:
            status = task.get('status', 'unknown')
            final_status_counts[status] = final_status_counts.get(status, 0) + 1
        
        logger.info(f"最终返回的任务状态分布: {final_status_counts}")
        
        return {
            'total_tasks': len(stored_tasks),
            'tasks': stored_tasks
        }
            
    except Exception as e:
        logger.error(f'获取任务列表失败: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

def validate_and_fix_task_times(task):
    """验证和修复任务的时间字段
    
    确保所有任务都有create_time和scheduled_start_time字段，如果没有则添加
    """
    try:
        task_id = task.get('id', 'unknown')
        
        # 1. 确保有start_time
        if 'start_time' not in task:
            logger.warning(f'任务缺少start_time字段 - task_id={task_id}')
            task['start_time'] = datetime.datetime.now(beijing_tz).isoformat()
            
        # 2. 确保有create_time
        if 'create_time' not in task:
            logger.warning(f'任务缺少create_time字段，使用start_time - task_id={task_id}')
            task['create_time'] = task['start_time']
            
        # 3. 确保有scheduled_start_time或使用create_time
        if 'scheduled_start_time' not in task or task['scheduled_start_time'] is None:
            logger.info(f'任务缺少scheduled_start_time字段，使用create_time - task_id={task_id}')
            task['scheduled_start_time'] = task['create_time']
            
        return task
    except Exception as e:
        logger.error(f'验证和修复任务时间字段时发生错误 - task_id={task.get("id", "unknown")}: {str(e)}')
        return task

@router.get("/status")
async def get_status():
    """获取所有活动任务的状态"""
    task_info = {}
    
    with process_lock:
        for task_id, info in active_processes.items():
            # 检查进程是否还在运行
            is_running = info['process'].poll() is None
            
            # 计算运行时间
            runtime = datetime.datetime.now(beijing_tz) - info['start_time']
            
            task_info[task_id] = {
                "running": is_running,
                "start_time": info['start_time'].isoformat(),
                "runtime_seconds": runtime.total_seconds(),
                "video_path": info.get('video_path', 'unknown'),
                "rtmp_url": info.get('rtmp_url', 'unknown')
            }
    
    return {
        "active_tasks": len(task_info),
        "tasks": task_info
    }

@router.get("/{task_id}/network")
async def get_task_network_status(task_id: str):
    """获取任务的网络状态"""
    try:
        with process_lock:
            if task_id not in active_processes:
                raise HTTPException(status_code=404, detail="任务不存在")
                
            network_warning = active_processes[task_id].get('network_warning', False)
            network_status = active_processes[task_id].get('network_status', '未知')
            retry_count = active_processes[task_id].get('retry_count', 0)
            
        return {
            "status": "success",
            "task_id": task_id,
            "network_warning": network_warning,
            "network_status": network_status,
            "retry_count": retry_count
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'获取任务网络状态失败 - task_id={task_id}: {str(e)}')
        raise HTTPException(status_code=500, detail=f"获取任务网络状态失败: {str(e)}")

@router.post("/start")
async def start_stream(request: Request):
    try:
        request_data = await request.json()
        
        # 验证必填字段
        if not request_data.get("rtmp_url") or not request_data.get("video_filename"):
            return {"status": "error", "message": "缺少必填字段"}
            
        # 获取计划开始时间，如果没有则为 None
        scheduled_start_time = request_data.get("scheduled_start_time")
        current_time = datetime.datetime.now(beijing_tz)
        
        if scheduled_start_time:
            try:
                # 先解析为 naive datetime
                scheduled_start_time = datetime.datetime.fromisoformat(scheduled_start_time.replace('Z', '+00:00'))
                # 如果没有时区信息，添加北京时区
                if scheduled_start_time.tzinfo is None:
                    scheduled_start_time = beijing_tz.localize(scheduled_start_time)
                    
                # 检查计划时间是否大于当前时间    
                if scheduled_start_time <= current_time:
                    return {"status": "error", "message": "计划开始时间必须大于当前时间"}
            except ValueError:
                return {"status": "error", "message": "计划开始时间格式无效"}
        
        # 构建任务数据
        task_data = {
            "rtmp_url": request_data["rtmp_url"],
            "video_filename": request_data["video_filename"],
            "task_name": request_data.get("task_name"),
            "auto_stop_minutes": request_data.get("auto_stop_minutes", 0),
            "transcode_enabled": request_data.get("transcode_enabled", False),
            "socks5_proxy": request_data.get("socks5_proxy"),
            "scheduled_start_time": scheduled_start_time,
            "current_time": current_time  # 添加当前时间供后续使用
        }
        
        # 启动任务
        if scheduled_start_time:
            task_id = await start_scheduled_stream(task_data)
            return {"status": "success", "message": f"任务已计划，将在 {scheduled_start_time.strftime('%Y-%m-%d %H:%M:%S')} 开始", "task_id": task_id}
        else:
            # 立即开始推流，直接返回 start_stream_task 的完整结果
            return await start_stream_task(task_data)
            
    except Exception as e:
        return {"status": "error", "message": f"启动推流失败: {str(e)}"}

async def start_scheduled_stream(task_data: dict):
    """启动计划任务"""
    try:
        task_id = str(uuid.uuid4())
        
        # 验证必填字段
        if not task_data.get("rtmp_url") or not task_data.get("video_filename"):
            raise ValueError("缺少必填字段")
            
        # 获取计划开始时间
        scheduled_start_time = task_data.get("scheduled_start_time")
        
        # 创建任务记录
        current_time = datetime.datetime.now(beijing_tz)
        create_time = current_time.isoformat()
        
        task_record = {
            "id": task_id,
            "rtmp_url": task_data["rtmp_url"],
            "video_filename": task_data["video_filename"],
            "task_name": task_data.get("task_name"),
            "start_time": current_time.isoformat(),
            "create_time": create_time,
            "status": "scheduled" if scheduled_start_time else "running",
            "auto_stop_minutes": task_data.get("auto_stop_minutes", 0),
            "transcode_enabled": task_data.get("transcode_enabled", False),
            "socks5_proxy": task_data.get("socks5_proxy"),
            "scheduled_start_time": scheduled_start_time.isoformat() if scheduled_start_time else create_time,
            "error_message": None,
            "network_status": "正常",
            "network_warning": False,
            "retry_count": 0
        }
        
        # 保存任务记录
        save_tasks([task_record])
        logger.info(f'任务记录已保存 - task_id={task_id}')
        
        # 如果有计划开始时间，添加调度任务
        if scheduled_start_time:
            # 深拷贝任务数据，防止后续修改影响调度任务
            import copy
            scheduled_task_data = copy.deepcopy(task_data)
            
            # 将任务ID添加到任务数据中，以便在调度器触发时能够找到对应的任务
            scheduled_task_data["id"] = task_id
            
            # 移除计划时间，避免无限调度
            scheduled_task_data["scheduled_start_time"] = None
            
            # 创建定时任务
            try:
                scheduler.add_job(
                    execute_scheduled_task,
                    'date',
                    run_date=scheduled_start_time,
                    args=[scheduled_task_data],
                    id=f'scheduled_stream_{task_id}',
                    replace_existing=True,
                    misfire_grace_time=300  # 允许5分钟的错过时间窗口
                )
                logger.info(f'成功添加计划任务到调度器 - task_id={task_id}, scheduled_time={scheduled_start_time.isoformat()}, job_id=scheduled_stream_{task_id}')
            except Exception as e:
                error_msg = f"向调度器添加任务失败: {str(e)}"
                logger.error(f'向调度器添加任务失败 - task_id={task_id}: {str(e)}')
                raise ValueError(error_msg)
                
            return task_id
        else:
            # 立即开始推流
            return await start_stream_task(task_data)
            
    except Exception as e:
        error_msg = f"启动计划任务失败: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)

def execute_scheduled_task(task_data: dict):
    """执行计划任务"""
    try:
        task_id = task_data['id']
        logger.info(f'开始执行计划任务 - task_id={task_id}')
        
        # 获取任务数据
        result = asyncio.run(start_stream_task(task_data))
        
        # 记录任务启动结果
        if result.get('status') == 'success':
            logger.info(f'计划任务执行成功 - task_id={task_id}')
        else:
            error_message = result.get('message', '未知错误')
            logger.error(f'计划任务执行失败 - task_id={task_id}: {error_message}')
            
        # 更新最后的执行时间
        _record_task_runtime(task_id, datetime.datetime.now(beijing_tz))
        
        return result
    except Exception as e:
        logger.exception(f'执行计划任务时发生异常 - task_id={task_data.get("id", "unknown")}: {str(e)}')
        
        # 更新任务状态为错误
        try:
            update_task_status(task_data.get('id'), {
                "status": "error",
                "message": f"计划任务执行失败: {str(e)}",
                "end_time": datetime.datetime.now(beijing_tz).isoformat()
            })
        except Exception as update_error:
            logger.error(f'更新计划任务状态失败: {str(update_error)}')
            
        return {
            "status": "error",
            "message": f"计划任务执行失败: {str(e)}"
        }

def _record_task_runtime(task_id, current_time):
    """记录任务运行时长的辅助函数"""
    try:
        stored_tasks = load_tasks()
        for task in stored_tasks:
            if task.get('id') == task_id and task.get('start_time'):
                start_time = datetime.datetime.fromisoformat(task['start_time'])
                if start_time.tzinfo is None:
                    start_time = beijing_tz.localize(start_time)
                runtime_minutes = (current_time - start_time).total_seconds() / 60
                update_task_status(task_id, {
                    "runtime_minutes": runtime_minutes
                })
                logger.info(f'任务运行时长: {runtime_minutes:.2f}分钟 - task_id={task_id}')
                break
    except Exception as runtime_error:
        logger.error(f'计算任务运行时长时发生错误 - task_id={task_id}: {str(runtime_error)}')

@router.get("/stop/{task_id}")
async def stop_task(task_id: str):
    """停止推流任务"""
    logger.info(f'收到请求: GET /api/tasks/stop/{task_id}')
    
    try:
        result = await stop_stream(task_id)
        # 无论任务是否存在或停止成功，都将状态更新为stopped
        update_task_status(task_id, {
            'status': 'stopped',
            'end_time': datetime.datetime.now(beijing_tz).isoformat(),
            'message': '任务已手动停止'
        })
        return {"status": "success", "message": "任务已停止"}
    except Exception as e:
        error_msg = f'停止任务失败: {str(e)}'
        logger.error(f'停止任务失败 - task_id={task_id}: {str(e)}')
        # 即使发生异常，也尝试更新任务状态
        update_task_status(task_id, {
            'status': 'stopped',  # 仍然设置为stopped而不是error
            'end_time': datetime.datetime.now(beijing_tz).isoformat(),
            'message': f'任务已停止，但过程中发生错误: {error_msg}'
        })
        return {"status": "success", "message": "任务已停止，但过程中发生错误"}

@router.delete("/delete")
async def delete_all_tasks():
    """停止并删除所有任务"""
    logger.info('收到删除所有任务的请求')
    
    # 第一步：获取所有任务ID
    task_ids = []
    with process_lock:
        task_ids = list(active_processes.keys())
        logger.info(f'找到 {len(task_ids)} 个活动任务')
    
    if not task_ids:
        logger.info('没有活动任务需要删除')
        return {"status": "success", "message": "没有活动任务需要删除"}
    
    # 第二步：停止每个任务
    success_count = 0
    for task_id in task_ids:
        logger.info(f'正在停止任务 - task_id={task_id}')
        try:
            result = await stop_stream(task_id)
            if result:
                success_count += 1
                logger.info(f'任务停止成功 - task_id={task_id}')
            else:
                logger.error(f'任务停止失败 - task_id={task_id}')
        except Exception as e:
            logger.error(f'停止任务时发生错误 - task_id={task_id}, error={str(e)}')
    
    # 第三步：清理活动任务列表
    with process_lock:
        active_processes.clear()
        logger.info(f'已清理活动任务列表，成功停止 {success_count}/{len(task_ids)} 个任务')
    
    return {
        "status": "success",
        "message": f"已停止 {success_count}/{len(task_ids)} 个任务",
        "total_tasks": len(task_ids),
        "successful_stops": success_count
    }

def diagnose_ffmpeg_failure(task_id, process, video_path, rtmp_url, ffmpeg_cmd):
    """诊断FFmpeg进程失败的原因"""
    diagnostic_info = []
    
    # 获取基本信息
    diagnostic_info.append(f"任务ID: {task_id}")
    diagnostic_info.append(f"视频文件: {video_path}")
    diagnostic_info.append(f"RTMP URL: {rtmp_url}")
    diagnostic_info.append(f"返回码: {process.poll()}")
    
    # 尝试获取stderr输出
    try:
        stderr_output = ""
        if process.stderr:
            stderr_output = process.stderr.read()
            if stderr_output:
                stderr_output = stderr_output.strip()
                diagnostic_info.append(f"\n错误输出:\n{stderr_output}")
        else:
            diagnostic_info.append("无法获取错误输出流")
    except Exception as e:
        diagnostic_info.append(f"读取错误输出失败: {str(e)}")
    
    # 检查文件是否存在
    try:
        import os
        file_exists = os.path.exists(video_path)
        file_size = os.path.getsize(video_path) if file_exists else 0
        diagnostic_info.append(f"文件存在: {file_exists}, 文件大小: {file_size} 字节")
    except Exception as e:
        diagnostic_info.append(f"检查文件状态失败: {str(e)}")
    
    # 检查网络连接
    try:
        from app.utils.network_utils import test_rtmp_connection
        connection_result = test_rtmp_connection(rtmp_url)
        diagnostic_info.append(f"RTMP连接测试: {connection_result}")
    except Exception as e:
        diagnostic_info.append(f"RTMP连接测试失败: {str(e)}")
    
    # 检查FFmpeg安装
    try:
        version_process = subprocess.run(['ffmpeg', '-version'], 
                                          stdout=subprocess.PIPE, 
                                          stderr=subprocess.PIPE,
                                          encoding='utf-8',
                                          timeout=5)
        if version_process.returncode == 0:
            ffmpeg_version = version_process.stdout.split('\n')[0]
            diagnostic_info.append(f"FFmpeg版本: {ffmpeg_version}")
        else:
            diagnostic_info.append(f"FFmpeg版本获取失败: {version_process.stderr}")
    except Exception as e:
        diagnostic_info.append(f"检查FFmpeg安装失败: {str(e)}")
    
    # 提供完整的命令行
    diagnostic_info.append(f"\n完整命令:\n{' '.join(ffmpeg_cmd)}")
    
    return "\n".join(diagnostic_info)

async def start_stream_task(task_data: dict):
    """启动推流任务"""
    try:
        # 检查是否有传入的任务ID，如果有则使用，没有则创建新ID
        task_id = task_data.get("id") if task_data.get("id") else str(uuid.uuid4())
        logger.info(f'开始执行推流任务 - task_id={task_id}, 是否为计划任务: {bool(task_data.get("id"))}')
        
        # 创建任务记录
        current_time = datetime.datetime.now(beijing_tz)
        create_time = current_time.isoformat()
        
        # 检查任务是否已存在
        is_existing_task = False
        if task_data.get("id"):
            stored_tasks = load_tasks()
            for task in stored_tasks:
                if task.get('id') == task_id:
                    is_existing_task = True
                    logger.info(f'使用现有计划任务 - task_id={task_id}')
                    break
        
        # 只有在任务不存在时才创建新任务记录
        if not is_existing_task:
            task_record = {
                "id": task_id,
                "rtmp_url": task_data["rtmp_url"],
                "video_filename": task_data["video_filename"],
                "task_name": task_data.get("task_name"),
                "start_time": current_time.isoformat(),
                "create_time": create_time,
                "status": "running",
                "auto_stop_minutes": task_data.get("auto_stop_minutes", 0),
                "transcode_enabled": task_data.get("transcode_enabled", False),
                "socks5_proxy": task_data.get("socks5_proxy"),
                "scheduled_start_time": task_data.get("scheduled_start_time").isoformat() if isinstance(task_data.get("scheduled_start_time"), datetime.datetime) else 
                                      task_data.get("scheduled_start_time") if task_data.get("scheduled_start_time") else create_time,
                "error_message": None,
                "network_status": "正常",
                "network_warning": False,
                "retry_count": 0
            }
            
            # 保存任务记录
            save_tasks([task_record])
            logger.info(f'即时任务记录已保存 - task_id={task_id}')
        
        # 获取视频文件路径
        video_dir = Path('public/video')
        video_path = video_dir.joinpath(task_data['video_filename'])

        # 检查文件权限
        file_ok, file_msg = check_video_permissions(video_path)
        if not file_ok:
            logger.error(f"视频文件权限检查失败 - task_id={task_id}: {file_msg}")
            update_task_status(task_id, {
                "status": "error",
                "error_message": file_msg,
                "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                "message": f"任务启动失败: 视频文件访问失败"
            })
            return {
                "status": "error",
                "message": f"视频文件访问失败",
                "error_detail": file_msg,
                "task_id": task_id
            }
        else:
            logger.info(f"视频文件权限检查通过 - task_id={task_id}: {file_msg}")

        # 检查视频编码
        try:
            video_codec, audio_codec = check_video_codec(video_path)
            task_message = [f"视频编码: {video_codec}, 音频编码: {audio_codec}"]
        except ValueError as codec_error:
            error_detail = str(codec_error)
            logger.error(f"视频编码检测失败 - task_id={task_id}: {error_detail}")
            # 更新任务状态
            update_task_status(task_id, {
                "status": "error",
                "error_message": error_detail,
                "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                "message": f"任务启动失败: 视频编码检测失败"
            })
            return {
                "status": "error",
                "message": f"视频编码检测失败",
                "error_detail": error_detail,
                "task_id": task_id
            }
        
        # 验证视频文件
        try:
            from app.utils.video_utils import validate_video_file
            is_valid, validation_msg = validate_video_file(video_path)
            if not is_valid:
                logger.error(f"视频文件验证失败 - task_id={task_id}: {validation_msg}")
                update_task_status(task_id, {
                    "status": "error",
                    "error_message": validation_msg,
                    "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                    "message": f"任务启动失败: 视频文件验证失败"
                })
                return {
                    "status": "error",
                    "message": f"视频文件验证失败",
                    "error_detail": validation_msg,
                    "task_id": task_id
                }
            else:
                logger.info(f"视频文件验证通过 - task_id={task_id}")
                task_message.append("视频文件验证通过")
        except Exception as e:
            logger.warning(f"视频文件验证功能调用失败 - task_id={task_id}: {str(e)}")
            # 继续执行，不阻止任务启动
        
        # 处理代理配置
        proxy_config = None
        proxy_config_file = None
        if task_data.get('socks5_proxy'):
            try:
                # 解析代理参数
                proxy_parts = task_data['socks5_proxy'].split(':')
                
                # 允许两种格式：ip:port 或 ip:port:username:password
                if len(proxy_parts) == 2:
                    proxy_ip, proxy_port = proxy_parts
                    proxy_user, proxy_pass = "", ""  # 明确设置为空字符串
                elif len(proxy_parts) == 4:
                    proxy_ip, proxy_port, proxy_user, proxy_pass = proxy_parts
                else:
                    # 考虑更灵活的格式，如 ip:port:::
                    if len(proxy_parts) > 2:
                        proxy_ip, proxy_port = proxy_parts[0], proxy_parts[1]
                        proxy_user = proxy_parts[2] if len(proxy_parts) > 2 and proxy_parts[2] else ""
                        proxy_pass = proxy_parts[3] if len(proxy_parts) > 3 and proxy_parts[3] else ""
                    else:
                        raise ValueError("代理参数格式错误,应为: ip:port 或 ip:port:username:password")
                
                # 创建代理配置文件
                proxy_config_file = create_proxy_config(task_id, proxy_ip, proxy_port, proxy_user, proxy_pass)
                
                # 创建代理配置字典 - 直接使用SOCKS5格式
                proxy_config = {}
                if proxy_user and proxy_pass:
                    socks5_url = f"socks5://{proxy_user}:{proxy_pass}@{proxy_ip}:{proxy_port}"
                else:
                    socks5_url = f"socks5://{proxy_ip}:{proxy_port}"
                
                # 专门为SOCKS5添加配置键
                proxy_config["socks5_proxy"] = socks5_url
                
                # 添加完整的日志
                logger.info(f"已创建SOCKS5代理配置 - task_id={task_id}: {socks5_url}")
                task_message.append(f"代理配置成功: SOCKS5 {proxy_ip}:{proxy_port}")
                logger.info(f"代理配置文件: {proxy_config_file}")
            except Exception as e:
                logger.error(f"配置代理失败: {str(e)}")
                raise
        
        # 添加RTMP URL验证和连接测试
        logger.info(f"开始验证RTMP URL并测试连接 - task_id={task_id}, url={task_data['rtmp_url']}")
        
        # 首先进行基本URL验证
        valid, error_msg = validate_rtmp_url(task_data['rtmp_url'])
        if not valid:
            logger.error(f"RTMP URL验证失败 - task_id={task_id}: {error_msg}")
            update_task_status(task_id, {
                "status": "error",
                "error_message": f"RTMP URL验证失败: {error_msg}",
                "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                "message": f"任务启动失败: RTMP URL无效"
            })
            return {
                "status": "error",
                "message": f"RTMP URL验证失败",
                "error_detail": error_msg,
                "task_id": task_id
            }
        
        # 如果使用代理，跳过实际的连接测试，因为普通连接测试无法通过代理
        if task_data.get('socks5_proxy'):
            logger.info(f"检测到使用代理配置，跳过RTMP连接测试 - task_id={task_id}")
            task_message.append(f"RTMP URL格式验证通过（使用代理时跳过连接测试）")
        else:
            # 然后进行实际的RTMP连接测试，增加重试逻辑
            rtmp_test_success = False
            rtmp_test_message = ""
            max_test_attempts = 3  # 最多尝试3次
            
            for attempt in range(1, max_test_attempts + 1):
                logger.info(f"RTMP连接测试 - 第{attempt}次尝试 - task_id={task_id}")
                success, connection_msg = test_rtmp_connection(task_data['rtmp_url'], timeout=5)
                if success:
                    rtmp_test_success = True
                    rtmp_test_message = connection_msg
                    logger.info(f"RTMP连接测试成功 - 第{attempt}次尝试 - task_id={task_id}")
                    break
                else:
                    rtmp_test_message = connection_msg
                    logger.warning(f"RTMP连接测试失败 - 第{attempt}次尝试 - task_id={task_id}: {connection_msg}")
                    if attempt < max_test_attempts:
                        logger.info(f"等待2秒后重试 - task_id={task_id}")
                        time.sleep(2)  # 等待2秒后重试
            
            if not rtmp_test_success:
                logger.error(f"RTMP连接测试失败(尝试{max_test_attempts}次) - task_id={task_id}: {rtmp_test_message}")
                update_task_status(task_id, {
                    "status": "error",
                    "error_message": f"RTMP连接测试失败({max_test_attempts}次尝试): {rtmp_test_message}",
                    "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                    "message": f"任务启动失败: RTMP服务器连接失败"
                })
                return {
                    "status": "error",
                    "message": f"RTMP连接测试失败({max_test_attempts}次尝试)",
                    "error_detail": rtmp_test_message,
                    "task_id": task_id
                }
            
            task_message.append(f"RTMP连接测试成功")
        
        # 获取FFmpeg命令 - 使用新的命令构建方式
        try:
            ffmpeg_cmd, env = get_ffmpeg_command(
                input_file=str(video_path),
                output_rtmp=task_data['rtmp_url'],
                proxy_config=proxy_config,
                transcode=task_data.get('transcode_enabled', False),
                task_id=task_id
            )
        except Exception as e:
            logger.error(f"构建FFmpeg命令失败 - task_id={task_id}: {str(e)}")
            update_task_status(task_id, {
                "status": "error",
                "error_message": f"构建FFmpeg命令失败: {str(e)}",
                "end_time": datetime.datetime.now(beijing_tz).isoformat(),
                "message": f"任务启动失败: 无法构建FFmpeg命令"
            })
            return {
                "status": "error",
                "message": f"构建FFmpeg命令失败",
                "error_detail": str(e),
                "task_id": task_id
            }
        
        # 确保不修改loglevel参数，我们已经在get_ffmpeg_command中设置了warning级别
        logger.info(f"保留FFmpeg日志级别为warning - task_id={task_id}")
        
        # 记录详细的命令信息
        cmd_str = ' '.join(ffmpeg_cmd)
        logger.info(f"将要执行的FFmpeg命令 - task_id={task_id}: {cmd_str}")
        
        # 启动FFmpeg进程
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,  # 行缓冲，确保错误能够及时读取
            shell=False,  # 不使用shell，避免命令注入风险
            env=env  # 使用包含代理配置的环境变量
        )
        
        # 记录进程信息
        logger.info(f"FFmpeg进程已启动 - task_id={task_id}, pid={process.pid}")
        
        # 立即检查进程是否已经退出
        if process.poll() is not None:
            returncode = process.poll()
            logger.error(f"FFmpeg进程启动后立即退出 - task_id={task_id}, returncode={returncode}")
            
            # 使用诊断工具获取详细信息
            diagnostic_info = diagnose_ffmpeg_failure(task_id, process, str(video_path), task_data['rtmp_url'], ffmpeg_cmd)
            
            # 更新任务状态
            update_task_status(task_id, {
                "status": "error",
                "message": f"进程启动后立即退出，返回码：{returncode}",
                "error_message": diagnostic_info,
                "end_time": datetime.datetime.now(beijing_tz).isoformat()
            })
            
            return {
                "status": "error",
                "message": f"推流进程启动后立即退出",
                "details": ["进程返回码：" + str(returncode)],
                "task_id": task_id,
                "diagnostic_info": diagnostic_info
            }
        
        # 记录进程信息及命令
        logger.info(f"启动FFmpeg进程 - task_id={task_id}, pid={process.pid}")
        logger.info(f"FFmpeg命令: {' '.join(ffmpeg_cmd)}")
        
        # 记录进程信息
        with process_lock:
            active_processes[task_id] = {
                'process': process,
                'stderr': process.stderr,  # 保存stderr以便在进程退出时读取
                'start_time': datetime.datetime.now(beijing_tz),
                'video_path': str(video_path),
                'rtmp_url': task_data['rtmp_url'],
                'auto_stop_minutes': task_data.get('auto_stop_minutes', 699),
                'stopped_by_user': False,
                'auto_stopped': False,
                'network_warning': False,
                'network_status': '正常',
                'retry_count': 0,
                'proxy_config_file': proxy_config_file,
                'use_proxy': bool(proxy_config_file),
                'video_codec': video_codec,
                'audio_codec': audio_codec,
                'ffmpeg_cmd': ' '.join(ffmpeg_cmd),  # 保存完整命令行
                'need_reconnect': True  # 允许外部重连机制工作
            }
        
        # 如果设置了自动停止时间，添加停止任务
        if task_data.get('auto_stop_minutes'):
            stop_time = datetime.datetime.now(beijing_tz) + timedelta(minutes=task_data['auto_stop_minutes'])
            scheduler.add_job(
                stop_stream_sync,
                'date',
                run_date=stop_time,
                args=[task_id],
                id=f'auto_stop_{task_id}',
                replace_existing=True
            )
            task_message.append(f"将在 {stop_time.strftime('%H:%M:%S')} 自动停止")
        
        # 启动输出读取任务 - 使用线程执行器来运行非异步函数
        video_executor.submit(read_output, process, task_id)
        
        return {
            "status": "success",
            "message": "推流已启动",
            "task_id": task_id,
            "details": task_message
        }
            
    except Exception as e:
        error_msg = f"启动推流任务失败: {str(e)}"
        logger.error(error_msg)
        # 添加结束时间
        update_task_status(task_id, {
            "status": "error",
            "error_message": error_msg,
            "end_time": datetime.datetime.now(beijing_tz).isoformat(),
            "message": f"任务启动失败: {str(e)}"
        })
        raise ValueError(error_msg)

@router.post("/test-rtmp")
async def test_rtmp_endpoint(request: Request):
    """测试RTMP URL的连接性"""
    try:
        request_data = await request.json()
        rtmp_url = request_data.get("rtmp_url")
        use_proxy = request_data.get("use_proxy", False)
        proxy_config = request_data.get("socks5_proxy") if use_proxy else None
        
        if not rtmp_url:
            return {"status": "error", "message": "缺少RTMP URL参数"}
            
        logger.info(f"开始测试RTMP URL连接: {rtmp_url}")
        
        # 先验证URL格式
        valid, error_msg = validate_rtmp_url(rtmp_url)
        if not valid:
            logger.warning(f"RTMP URL格式验证失败: {error_msg}")
            return {
                "status": "error", 
                "message": "RTMP URL格式无效",
                "details": error_msg
            }
        
        # 如果使用代理，则只验证格式不测试连接
        if use_proxy:
            logger.info(f"使用代理配置, 仅验证URL格式: {rtmp_url}")
            return {
                "status": "success",
                "message": "RTMP URL格式验证通过（使用代理配置，跳过实际连接测试）"
            }
        
        # 测试实际连接，增加重试逻辑
        max_attempts = 3
        timeout = 5
        success = False
        final_message = ""
        
        for attempt in range(1, max_attempts + 1):
            logger.info(f"RTMP连接测试 - 第{attempt}次尝试")
            test_success, test_message = test_rtmp_connection(rtmp_url, timeout=timeout)
            
            if test_success:
                success = True
                final_message = test_message
                logger.info(f"RTMP连接测试成功 - 第{attempt}次尝试")
                break
            else:
                final_message = test_message
                logger.warning(f"RTMP连接测试失败 - 第{attempt}次尝试: {test_message}")
                
                if attempt < max_attempts:
                    logger.info(f"等待2秒后重试RTMP连接")
                    time.sleep(2)
            
        if success:
            return {
                "status": "success",
                "message": "RTMP URL连接测试成功",
                "attempts": attempt
            }
        else:
            return {
                "status": "error",
                "message": f"RTMP URL连接测试失败 ({max_attempts}次尝试)",
                "details": final_message,
                "attempts": max_attempts
            }
            
    except Exception as e:
        logger.exception(f"测试RTMP URL连接时发生异常: {str(e)}")
        return {"status": "error", "message": f"测试RTMP URL连接失败: {str(e)}"} 