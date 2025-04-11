import signal
import sys
import threading
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import uvicorn
import atexit
from contextlib import asynccontextmanager
import time
import uuid
import datetime
import asyncio
import psutil
import os

from app.api import api_router
from app.core.logging import setup_logging, cleanup_old_logs, get_task_log_path
from app.core.config import get_app_root, get_log_dir, get_proxy_config_dir, get_temp_dir, get_tasks_dir, DATA_DIR, read_config
from app.services.monitor_service import ResourceMonitor, monitor_all_rtmp_connections
from app.services.stream_service import video_executor, active_processes, process_lock

# 全局常量 
APP_VERSION = "1.2.3.4-2"

# 初始化日志记录器
logger = setup_logging()

# 1. 首先创建应用实例
@asynccontextmanager
async def lifespan(app):
    """应用生命周期管理"""
    try:
        logger.info('应用启动初始化开始')
        
        # 确保所需目录存在
        dirs_to_check = [
            get_app_root(),
            DATA_DIR,
            get_proxy_config_dir(),
            get_log_dir(),
            get_temp_dir(),
            get_tasks_dir()
        ]
        
        for dir_path in dirs_to_check:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.info(f'确保目录存在: {dir_path}')
        
        # 添加任务状态检查调度任务
        from apscheduler.schedulers.background import BackgroundScheduler
        import pytz
        
        scheduler = BackgroundScheduler(
            timezone=pytz.timezone('Asia/Shanghai'),
            job_defaults={'misfire_grace_time': 60}
        )
        
        # 添加定期检查任务状态
        from app.services.stream_service import active_processes, process_lock
        
        def check_active_tasks():
            """定期检查活动任务的状态"""
            try:
                # 在函数开始就导入所需的模块和变量
                from app.services.task_service import load_tasks, update_task_status, beijing_tz
                from app.api.tasks import execute_scheduled_task
                from datetime import datetime
                import pytz
            
                logger.info('开始检查活动任务状态')
                with process_lock:
                    # 检查活动进程列表中的任务
                    for task_id, info in list(active_processes.items()):
                        process = info['process']
                        # 检查进程是否还在运行
                        is_running = process.poll() is None
                        if not is_running:
                            return_code = process.poll()
                            # 读取进程的错误输出（如果有的话）
                            stderr_content = ''
                            if 'stderr' in info and info['stderr']:
                                try:
                                    stderr_content = info['stderr'].read()
                                    if stderr_content:
                                        stderr_content = stderr_content.decode('utf-8', errors='replace')
                                        logger.error(f'任务异常退出的错误输出 - task_id={task_id}:\n{stderr_content}')
                                except Exception as stderr_err:
                                    logger.error(f'读取进程错误输出失败 - task_id={task_id}: {str(stderr_err)}')
                            
                            logger.warning(f'检测到任务已停止但未从活动列表中移除 - task_id={task_id}, 返回码={return_code}')
                            
                            # 构建详细的错误消息
                            error_details = f'进程已停止 (返回码: {return_code})'
                            if stderr_content:
                                # 只取最后500个字符作为错误消息，避免消息过长
                                error_msg = stderr_content[-500:] if len(stderr_content) > 500 else stderr_content
                                error_details += f'\n错误输出: {error_msg}'
                            
                            # 获取任务日志文件路径
                            log_path = get_task_log_path(task_id)
                            if log_path and log_path.exists():
                                error_details += f'\n任务日志文件: {log_path}'
                                
                                # 尝试读取日志文件的最后几行
                                try:
                                    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                                        # 读取最后20行
                                        last_lines = list(f.readlines())[-20:]
                                        if last_lines:
                                            log_excerpt = ''.join(last_lines)
                                            logger.info(f'任务{task_id}日志文件最后几行:\n{log_excerpt}')
                                except Exception as log_err:
                                    logger.error(f'读取日志文件失败 - task_id={task_id}: {str(log_err)}')
                            
                            # 从活动进程列表中移除
                            del active_processes[task_id]
                            logger.info(f'已从活动列表中移除已停止的任务 - task_id={task_id}')
                            
                            # 更新任务状态
                            try:
                                update_task_status(task_id, {
                                    'status': 'error',
                                    'message': '任务异常退出',
                                    'error_message': error_details,
                                    'end_time': datetime.now(beijing_tz).isoformat()
                                })
                                logger.info(f'已更新任务状态为error - task_id={task_id}')
                            except Exception as update_error:
                                logger.error(f'更新已停止任务状态失败 - task_id={task_id}: {str(update_error)}')
                    
                    # 查找系统中可能存在的ffmpeg进程但不在活动列表中的任务
                    try:
                        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                            # 检查是否是ffmpeg进程
                            if proc.info['name'] == 'ffmpeg':
                                # 获取命令行参数
                                cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                                # 检查是否是视频推流任务
                                if 'rtmp://' in cmdline and '-i' in cmdline:
                                    # 检查是否在活动进程列表中
                                    found = False
                                    for _, info in active_processes.items():
                                        process = info['process']
                                        if process.pid == proc.info['pid']:
                                            found = True
                                            break
                                    
                                    if not found:
                                        logger.warning(f'检测到系统中存在未由应用控制的ffmpeg进程 - pid={proc.info["pid"]}')
                                        logger.info(f'正在终止未控制的ffmpeg进程 - pid={proc.info["pid"]}')
                                        try:
                                            proc.terminate()
                                            try:
                                                proc.wait(timeout=5)
                                            except:
                                                proc.kill()
                                            logger.info(f'已终止未控制的ffmpeg进程 - pid={proc.info["pid"]}')
                                        except Exception as term_error:
                                            logger.error(f'终止未控制的ffmpeg进程失败 - pid={proc.info["pid"]}: {str(term_error)}')
                    except Exception as psutil_error:
                        logger.error(f'检查系统中的ffmpeg进程失败: {str(psutil_error)}')
                
                # 检查计划中的任务 - 以防调度器错过任务
                all_tasks = load_tasks(limit=100)
                current_time = datetime.now(beijing_tz)
                
                # 检查数据库中标记为running但实际进程不存在的任务
                for task in all_tasks:
                    if task.get('status') == 'running' and task.get('id') not in active_processes:
                        logger.warning(f'发现数据库中标记为running但不在活动进程列表中的任务 - task_id={task.get("id")}')
                        
                        # 获取任务ID
                        task_id = task.get('id')
                        if not task_id:
                            continue
                            
                        # 检查是否有对应的进程在系统中
                        system_process_found = False
                        try:
                            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                                if proc.info['name'] == 'ffmpeg':
                                    cmdline = ' '.join(proc.info['cmdline'] if proc.info['cmdline'] else [])
                                    if task_id in cmdline:
                                        system_process_found = True
                                        logger.info(f'在系统中找到对应的ffmpeg进程 - task_id={task_id}, pid={proc.info["pid"]}')
                                        break
                        except Exception as proc_err:
                            logger.error(f'检查系统进程时发生错误 - task_id={task_id}: {str(proc_err)}')
                            
                        # 如果系统中也没有相关进程，更新任务状态
                        if not system_process_found:
                            try:
                                update_task_status(task_id, {
                                    'status': 'error',
                                    'message': '任务异常退出 (定期检查)',
                                    'error_message': '任务在活动列表和系统进程中均未找到',
                                    'end_time': datetime.now(beijing_tz).isoformat()
                                })
                                logger.info(f'已将任务状态从running更新为error - task_id={task_id}')
                            except Exception as update_err:
                                logger.error(f'更新任务状态失败 - task_id={task_id}: {str(update_err)}')
                
                # 获取当前调度器中的所有作业ID
                scheduler_job_ids = [job.id for job in scheduler.get_jobs()]
                
                for task in all_tasks:
                    if task.get('status') == 'scheduled':
                        try:
                            task_id = task.get('id')
                            job_id = f'scheduled_stream_{task_id}'
                            
                            # 优先使用scheduled_start_time，如果不存在则使用create_time
                            if task.get('scheduled_start_time'):
                                scheduled_time = datetime.fromisoformat(task['scheduled_start_time'])
                            elif task.get('create_time'):
                                scheduled_time = datetime.fromisoformat(task['create_time'])
                                logger.info(f'任务未提供计划时间，使用创建时间作为计划时间 - task_id={task_id}')
                            else:
                                # 如果既没有计划时间也没有创建时间，则跳过此任务
                                logger.warning(f'任务既没有计划时间也没有创建时间，无法处理 - task_id={task_id}')
                                continue
                                
                            if scheduled_time.tzinfo is None:
                                scheduled_time = beijing_tz.localize(scheduled_time)
                            
                            # 如果计划时间已到但作业不在调度器中，可能是调度器错过了
                            time_diff = (scheduled_time - current_time).total_seconds()
                            
                            if time_diff <= 0 and job_id not in scheduler_job_ids:
                                logger.warning(f'计划任务可能被调度器错过 - task_id={task_id}, scheduled_time={scheduled_time.isoformat()}')
                                
                                # 构建任务数据
                                task_data = {
                                    "id": task_id,
                                    "rtmp_url": task["rtmp_url"],
                                    "video_filename": task["video_filename"],
                                    "auto_stop_minutes": task.get("auto_stop_minutes", 0),
                                    "transcode_enabled": task.get("transcode_enabled", False),
                                    "socks5_proxy": task.get("socks5_proxy"),
                                    "scheduled_start_time": None
                                }
                                
                                # 直接执行任务
                                logger.info(f'立即执行错过的计划任务 - task_id={task_id}')
                                execute_scheduled_task(task_data)
                            
                            # 如果计划时间即将到来但作业不在调度器中，添加到调度器
                            elif 0 < time_diff <= 600 and job_id not in scheduler_job_ids:  # 10分钟内即将执行的任务
                                logger.warning(f'计划任务未在调度器中找到，重新添加 - task_id={task_id}, scheduled_time={scheduled_time.isoformat()}')
                                
                                # 构建任务数据
                                task_data = {
                                    "id": task_id,
                                    "rtmp_url": task["rtmp_url"],
                                    "video_filename": task["video_filename"],
                                    "auto_stop_minutes": task.get("auto_stop_minutes", 0),
                                    "transcode_enabled": task.get("transcode_enabled", False),
                                    "socks5_proxy": task.get("socks5_proxy"),
                                    "scheduled_start_time": None
                                }
                                
                                # 添加到调度器
                                scheduler.add_job(
                                    execute_scheduled_task,
                                    'date',
                                    run_date=scheduled_time,
                                    args=[task_data],
                                    id=job_id,
                                    replace_existing=True,
                                    misfire_grace_time=300  # 允许5分钟的错过时间窗口
                                )
                                logger.info(f'已重新添加计划任务到调度器 - task_id={task_id}, scheduled_time={scheduled_time.isoformat()}')
                                
                        except Exception as e:
                            logger.error(f'检查计划任务状态时发生错误 - task_id={task.get("id", "unknown")}: {str(e)}')
                    
            except Exception as e:
                logger.error(f'检查活动任务状态时发生错误: {str(e)}')
                
        # 确保将任务状态检查添加到调度器
        scheduler.add_job(
            check_active_tasks,
            'interval',
            seconds=30,  # 每30秒执行一次
            id='check_active_tasks',
            replace_existing=True,
            max_instances=1  # 确保不会有多个实例同时运行
        )
        
        # 添加网络监控任务
        scheduler.add_job(
            monitor_all_rtmp_connections,
            'interval',
            seconds=30,
            id="global_network_monitor",
            replace_existing=True
        )
        logger.info('已添加全局网络质量监控任务')
        
        # 添加每天凌晨2点执行日志清理的任务
        scheduler.add_job(
            cleanup_old_logs,
            'cron',
            hour=2,
            minute=0,
            id="log_cleanup",
            replace_existing=True
        )
        logger.info('已添加日志清理调度任务')
        
        # 创建资源监控实例 - 保持不变
        resource_monitor = ResourceMonitor()
        
        # 将resource_monitor添加到应用状态中，以便在事件处理器中访问
        app.state.resource_monitor = resource_monitor
        
        # 应用程序运行阶段
        yield
        
        # 关闭阶段：清理资源
        logger.info('开始执行清理工作')
        
        # 停止调度器
        if scheduler.running:
            scheduler.shutdown()
            logger.info('调度器已关闭')
        
        # 清理所有活动进程
        with process_lock:
            for task_id, process_info in active_processes.items():
                try:
                    process = process_info['process']
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except:
                            process.kill()
                    logger.info(f'已终止进程 - task_id={task_id}')
                except Exception as e:
                    logger.error(f'终止进程失败 - task_id={task_id}: {str(e)}')
            
            active_processes.clear()
            logger.info('已清理所有活动进程')
                    
    except Exception as e:
        logger.error(f'应用生命周期管理发生错误: {str(e)}')
        if 'yield' not in locals():
            # 如果错误发生在yield之前，需要让异常继续传播
            raise
    finally:
        if 'yield' in locals():
            # 只有当yield已执行过，才是关闭阶段
            logger.info('清理工作完成')

app = FastAPI(
    title="YouTube直播助手API",
    description="用于管理自动化YouTube直播的API服务",
    version="1.0.0",
    lifespan=lifespan
)

# 2. 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 错误处理中间件
@app.middleware("http")
async def log_requests_and_handle_exceptions(request: Request, call_next):
    """请求日志和错误处理中间件"""
    start_time = time.time()
    method = request.method
    url = str(request.url)
    
    # 不记录静态文件请求
    if 'static' in url:
        return await call_next(request)
    
    # 不记录视频文件流请求
    if '/video/' in url and 'range' in request.headers:
        return await call_next(request)
    
    logger.info(f"{method} {url}")
    
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        
        # 记录响应状态
        status_code = response.status_code
        logger.info(f"{status_code} {method} {url} 处理时间: {process_time:.3f}s")
        
        return response
    except Exception as e:
        # 记录错误但不暴露详细信息
        error_id = str(uuid.uuid4())
        logger.exception(f"请求异常 [{error_id}] {method} {url}: {str(e)}")
        
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "服务器内部错误",
                "error_id": error_id
            }
        )

# 添加版本API端点
@app.get("/version")
async def get_version():
    """返回应用版本信息"""
    config = read_config()
    max_file_size = config.get('max_file_size_mb', 100)  # 默认100MB
    return {
        "version": APP_VERSION,
        "max_file_size_mb": max_file_size
    }

# 4. 挂载API路由
app.include_router(api_router, prefix="/api")

# 5. 挂载静态文件路由
app.mount("/video", StaticFiles(directory="public/video"), name="video")
app.mount("/static", StaticFiles(directory="public/static"), name="static")
app.mount("/", StaticFiles(directory="public", html=True), name="public")

# 6. 根路由
@app.get("/")
async def read_root():
    """返回前端页面"""
    return FileResponse("public/index.html")

# 添加线程池关闭钩子
atexit.register(lambda: video_executor.shutdown(wait=True))

# 添加信号处理函数
def handle_sigterm(signum, frame):
    logger.info('接收到SIGTERM信号，开始优雅关闭')
    sys.exit(0)

# 注册信号处理
signal.signal(signal.SIGTERM, handle_sigterm)

# 在应用启动时启动监控服务
@app.on_event("startup")
async def startup_event():
    # 从应用状态中获取监控服务
    resource_monitor = app.state.resource_monitor
    
    # 启动监控服务
    if not resource_monitor.running:
        resource_monitor.start_monitoring()
        logger.info("应用启动完成，监控服务已启动")
    else:
        logger.info("监控服务已在运行中")

# 在应用关闭时停止监控服务
@app.on_event("shutdown")
async def shutdown_event():
    # 从应用状态中获取监控服务
    resource_monitor = app.state.resource_monitor
    
    # 停止监控服务
    if resource_monitor.running:
        resource_monitor.stop_monitoring()
        logger.info("应用关闭，监控服务已停止")
    else:
        logger.info("监控服务未在运行中")

def verify_logging_system():
    """验证日志系统路径和权限"""
    try:
        # 获取应用根目录
        app_root = os.path.abspath(os.path.dirname(__file__))
        log_root = os.path.join(app_root, '..', 'logs')
        
        # 确保主日志目录存在
        os.makedirs(log_root, exist_ok=True)
        
        # 确保任务日志目录存在
        tasks_log_dir = os.path.join(log_root, 'tasks')
        os.makedirs(tasks_log_dir, exist_ok=True)
        
        # 测试写入权限
        test_file = os.path.join(tasks_log_dir, 'test_write.log')
        with open(test_file, 'w') as f:
            f.write('Logging system check: OK\n')
        
        # 清理测试文件
        os.remove(test_file)
        
        print(f"日志系统验证成功 - 路径: {log_root}")
        return True
    except Exception as e:
        print(f"日志系统验证失败: {str(e)}")
        return False

# 主入口点
if __name__ == '__main__':
    logger.info('启动应用程序')
    
    try:
        # 验证日志系统
        verify_logging_system()
        
        # 启动FastAPI应用
        uvicorn.run(
            app,
            host='0.0.0.0',
            port=8000,
            log_level="debug",
            access_log=False,
            timeout_keep_alive=300
        )
    except Exception as e:
        logger.error(f'应用程序启动失败: {str(e)}')
        raise
    finally:
        logger.info('应用程序已关闭') 
