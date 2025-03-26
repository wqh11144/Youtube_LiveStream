import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import logging
import pytz
from app.core.config import TASKS_DIR, get_app_root

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')
logger = logging.getLogger('youtube_live')

def ensure_tasks_dir():
    """确保任务存储目录存在"""
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f'确保任务存储目录存在: {TASKS_DIR} (绝对路径: {TASKS_DIR.resolve()})')
        return True
    except Exception as e:
        logger.error(f'创建任务存储目录失败: {str(e)}')
        return False

def get_daily_tasks_file(date: Optional[str] = None):
    """获取指定日期的任务文件路径"""
    if date:
        # 使用传入的日期
        date_obj = datetime.strptime(date, '%Y-%m-%d')
    else:
        # 默认使用今天的日期
        date_obj = datetime.now(beijing_tz)
    
    # 创建文件路径
    tasks_file = TASKS_DIR / f"tasks_{date_obj.strftime('%Y-%m-%d')}.json"
    logger.debug(f'任务文件路径: {tasks_file} (绝对路径: {tasks_file.resolve()})')
    return tasks_file

def load_tasks(limit: int = 10, date: Optional[str] = None):
    """加载任务记录
    
    Args:
        limit: 返回的最大任务数量，默认10条
        date: 可选的日期字符串，格式为 'YYYY-MM-DD'
    """
    try:
        logger.info(f'开始加载任务记录, 参数: limit={limit}, date={date}')
        logger.info(f'任务目录: {TASKS_DIR} (绝对路径: {TASKS_DIR.resolve()})')
        
        # 确保目录存在
        ensure_tasks_dir()
        
        all_tasks = []
        
        # 如果指定了日期，只加载该日期的任务
        if date:
            task_file = get_daily_tasks_file(date)
            logger.info(f'加载指定日期的任务文件: {task_file}')
            if task_file.exists():
                try:
                    with open(task_file, 'r', encoding='utf-8') as f:
                        tasks = json.load(f)
                        all_tasks.extend(tasks)
                        logger.info(f'成功从 {task_file} 加载了 {len(tasks)} 条任务')
                except Exception as e:
                    logger.error(f'读取任务文件失败 {task_file}: {str(e)}')
            else:
                logger.warning(f'任务文件不存在: {task_file}')
        else:
            # 获取所有任务文件
            if TASKS_DIR.exists():
                # 优先检查今天的任务文件
                today_date = datetime.now(beijing_tz).strftime('%Y-%m-%d')
                today_file = get_daily_tasks_file(today_date)
                
                # 如果今天的文件存在，先加载它
                if today_file.exists():
                    try:
                        logger.info(f'尝试加载今天的任务文件: {today_file}')
                        with open(today_file, 'r', encoding='utf-8') as f:
                            tasks = json.load(f)
                            all_tasks.extend(tasks)
                            logger.info(f'成功从今天的文件 {today_file} 加载了 {len(tasks)} 条任务')
                    except Exception as e:
                        logger.error(f'读取今天的任务文件失败 {today_file}: {str(e)}')
                
                # 加载其他所有任务文件
                task_files = sorted(TASKS_DIR.glob("tasks_*.json"), reverse=True)
                logger.info(f'找到 {len(task_files)} 个任务文件')
                
                # 遍历所有任务文件
                for file in task_files:
                    # 跳过已经加载的今天的文件
                    if file == today_file:
                        continue
                        
                    try:
                        logger.debug(f'正在读取任务文件: {file}')
                        with open(file, 'r', encoding='utf-8') as f:
                            tasks = json.load(f)
                            all_tasks.extend(tasks)
                            logger.debug(f'从 {file} 加载了 {len(tasks)} 条任务')
                    except Exception as e:
                        logger.error(f'读取任务文件失败 {file}: {str(e)}')
                        continue
                    
                    # 如果已经收集足够的任务，就停止
                    if len(all_tasks) >= limit:
                        logger.debug(f'已达到任务数量限制 {limit}，停止加载更多文件')
                        break
            else:
                logger.warning(f'任务目录不存在: {TASKS_DIR}')
        
        # 按开始时间排序并限制数量
        all_tasks.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        # 添加调试日志
        status_counts = {}
        for task in all_tasks[:limit]:
            status = task.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1
        
        logger.info(f'加载了 {len(all_tasks[:limit])} 个任务，状态分布: {status_counts}')
        
        return all_tasks[:limit]
        
    except Exception as e:
        logger.error(f'加载任务记录失败: {str(e)}', exc_info=True)
        return []
    
def save_tasks(tasks, date: Optional[str] = None):
    """保存任务记录
    
    Args:
        tasks: 要保存的任务列表
        date: 可选的日期字符串，格式为 'YYYY-MM-DD'
    """
    try:
        logger.info(f'开始保存任务记录，任务数量: {len(tasks)}, 日期: {date}')
        
        # 确保目录存在
        if not ensure_tasks_dir():
            logger.error('无法保存任务记录，目录创建失败')
            return
        
        # 如果没有提供日期，使用任务的开始时间来确定保存的日期
        if not date and tasks and len(tasks) > 0:
            # 获取第一个任务的开始时间
            first_task_time = datetime.fromisoformat(tasks[0]['start_time'])
            date = first_task_time.strftime('%Y-%m-%d')
            logger.debug(f'使用任务开始时间确定日期: {date}')
        
        # 如果还是没有日期，使用当前日期
        if not date:
            date = datetime.now(beijing_tz).strftime('%Y-%m-%d')
            logger.debug(f'使用当前日期: {date}')
            
        tasks_file = get_daily_tasks_file(date)
        logger.info(f'将保存任务到文件: {tasks_file} (绝对路径: {tasks_file.resolve()})')
        
        # 保存前再次确保父目录存在
        tasks_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 读取现有任务（如果有）
        existing_tasks = []
        if tasks_file.exists():
            try:
                with open(tasks_file, 'r', encoding='utf-8') as f:
                    existing_tasks = json.load(f)
                logger.info(f'从现有文件加载了 {len(existing_tasks)} 个任务')
            except Exception as e:
                logger.error(f'读取现有任务文件失败: {str(e)}')
                # 如果文件存在但读取失败，可能是格式错误，创建备份
                if tasks_file.exists():
                    backup_file = tasks_file.with_suffix('.json.bak')
                    try:
                        import shutil
                        shutil.copy2(tasks_file, backup_file)
                        logger.info(f'已创建任务文件备份: {backup_file}')
                    except Exception as backup_err:
                        logger.error(f'创建备份文件失败: {str(backup_err)}')
        
        # 创建任务ID映射，用于合并任务
        existing_task_map = {task['id']: task for task in existing_tasks if 'id' in task}
        
        # 合并新任务到现有任务 - 更新已存在的任务或添加新任务
        for task in tasks:
            if 'id' in task and task['id'] in existing_task_map:
                # 更新现有任务
                existing_task_map[task['id']].update(task)
                logger.debug(f'更新现有任务 ID: {task["id"]}')
            elif 'id' in task:
                # 添加新任务
                existing_task_map[task['id']] = task
                logger.debug(f'添加新任务 ID: {task["id"]}')
            else:
                # 没有ID的任务
                logger.warning(f'发现没有ID的任务，无法添加: {task}')
        
        # 转换回列表
        merged_tasks = list(existing_task_map.values())
        
        # 按开始时间排序
        merged_tasks.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        # 写入合并后的任务
        with open(tasks_file, 'w', encoding='utf-8') as f:
            json.dump(merged_tasks, f, ensure_ascii=False, indent=2)
        logger.info(f'保存任务记录成功: {tasks_file}，总任务数量: {len(merged_tasks)}')
    except Exception as e:
        logger.error(f'保存任务记录失败: {str(e)}', exc_info=True)

def update_task_status(task_id, status_update):
    """更新任务状态"""
    try:
        # 首先找到这个任务所在的日期
        task_date = None
        task_found = False
        
        # 获取所有任务
        all_tasks = load_tasks(limit=100)  # 加载更多任务以确保能找到目标任务
        
        # 按日期分组任务
        tasks_by_date = {}
        for task in all_tasks:
            if task['id'] == task_id:
                # 找到目标任务
                task_found = True
                # 获取任务日期
                start_time = datetime.fromisoformat(task['start_time'])
                task_date = start_time.strftime('%Y-%m-%d')
                
                # 确保日期键存在
                if task_date not in tasks_by_date:
                    tasks_by_date[task_date] = []
                
                # 更新任务状态
                task.update(status_update)
                tasks_by_date[task_date].append(task)
            else:
                # 其他任务按日期分组
                start_time = datetime.fromisoformat(task['start_time'])
                date_key = start_time.strftime('%Y-%m-%d')
                
                if date_key not in tasks_by_date:
                    tasks_by_date[date_key] = []
                tasks_by_date[date_key].append(task)
        
        # 保存更新后的任务
        if task_found and task_date:
            save_tasks(tasks_by_date[task_date], task_date)
            logger.info(f'更新任务状态成功 - task_id={task_id}, date={task_date}')
        else:
            logger.warning(f'未找到任务 - task_id={task_id}')
            
    except Exception as e:
        logger.error(f'更新任务状态失败 - task_id={task_id}: {str(e)}') 