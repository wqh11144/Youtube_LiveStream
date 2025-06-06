import subprocess
import threading
import time
import uuid
import asyncio
import logging
import pytz
from datetime import datetime, timedelta
from pathlib import Path
import platform
from typing import Dict, Any, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor
import re
import os
import json
from threading import Lock

from app.utils.file_utils import create_proxy_config, cleanup_proxy_config
from app.utils.video_utils import get_ffmpeg_command, check_video_codec, reconnect_keywords, ffmpeg_filter_patterns
from app.utils.network_utils import rtmp_error_strategies, test_rtmp_connection, validate_rtmp_url, extract_host_from_rtmp
from app.services.task_service import update_task_status
from app.core.logging import get_task_logger

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')
logger = logging.getLogger('youtube_live')

# 线程池
video_executor = ThreadPoolExecutor(max_workers=10)

# 活动任务存储
active_processes = {}
process_lock = Lock()  # 线程锁

# 配置化的网络参数
NETWORK_BUFFER_TIME = 10  # 默认缓冲期时间（秒）
NETWORK_MAX_BUFFER_TIME = 15  # 最大缓冲期时间（秒）

# 需要立即重连的严重错误关键词
immediate_reconnect_keywords = [
    'connection refused',
    'host not found',
    'no route to host',
    'server returned 403',
    'server returned 401',
    'access denied',
    'permission denied'
]

# 用于检测需要重连的网络相关错误关键词
reconnect_keywords = [
    'connection refused',
    'connection reset',
    'connection timed out',
    'timeout',
    'network is unreachable',
    'no route to host',
    'operation timed out',
    'broken pipe',
    'end of file',
    'server returned 4',  # 400系列错误
    'server returned 5',  # 500系列错误
    'network error',
    'socket closed',
    'i/o error',
    'could not connect',
    'failed to connect',
    'interrupt',
    'unexpected eof',
    'protocol error',
    'connection closed',
    'disconnected',
    'tcp timeout',
    'resolve failed',
    'network down',
    'network changed',
    'could not write'  # 写入失败
]

def get_error_description(line):
    """
    从FFmpeg输出行中提取错误描述
    
    Args:
        line: 包含错误信息的一行
        
    Returns:
        str: 格式化的错误描述
    """
    # 移除时间戳和日志级别等前缀
    cleaned_line = re.sub(r'^\[.*?\]', '', line).strip()
    
    # 常见错误关键词
    error_keywords = [
        'error', 'failed', 'unable', 'cannot', 'timeout', 'refused',
        'denied', 'not found', 'no such', 'permission', 'invalid'
    ]
    
    # 尝试定位错误的主要部分
    for keyword in error_keywords:
        pattern = re.compile(r'.*(' + keyword + r'[^.!:;]*)', re.IGNORECASE)
        match = pattern.search(cleaned_line)
        if match:
            return match.group(1).strip()
    
    # 如果没有找到明确的错误关键词，返回整行但限制长度
    if len(cleaned_line) > 100:
        return cleaned_line[:97] + '...'
    return cleaned_line

def read_output(process, task_id):
    """读取FFmpeg进程的输出流并检测错误"""
    try:
        logger.info(f'开始读取进程输出 - task_id={task_id}')
        
        # 创建任务特定的日志记录器
        task_logger = get_task_logger(task_id)
        task_logger.info(f"===== 开始记录任务 {task_id} 的输出 =====")
        
        # 记录进程启动相关信息
        pid = process.pid
        logger.info(f'进程已启动 - task_id={task_id}, pid={pid}')
        task_logger.info(f'进程已启动 - pid={pid}')
        
        # 获取任务信息
        with process_lock:
            if task_id not in active_processes:
                logger.error(f'无法找到对应的活动进程 - task_id={task_id}')
                task_logger.error(f'无法找到对应的活动进程')
                return
            
            # 初始化最后活动时间，用于检测卡住的进程
            active_processes[task_id]['last_activity_time'] = datetime.now(beijing_tz)
            
            task_info = active_processes[task_id]
            
            # 记录任务配置信息到专属日志
            task_logger.info(f"任务配置信息:")
            task_logger.info(f"- 视频文件: {task_info.get('video_path', '未知')}")
            task_logger.info(f"- RTMP地址: {task_info.get('rtmp_url', '未知')}")
            task_logger.info(f"- 转码设置: {'已启用' if task_info.get('transcode_enabled') else '未启用'}")
            task_logger.info(f"- 代理设置: {'已配置' if task_info.get('use_proxy') else '未配置'}")
            
            # 保存命令行信息(如果有)
            if 'ffmpeg_cmd' in task_info:
                task_logger.info(f"FFmpeg命令: {task_info['ffmpeg_cmd']}")
        
        # 添加任务ID用于日志关联
        log_prefix = f'[task_id={task_id}]'
        
        # 初始化错误收集变量
        error_output = []
        error_pattern = re.compile(r'(error|couldn\'t|failed|invalid|unable|no such|denied|not found|option\s+not\s+found)', re.IGNORECASE)
        reconnect_patterns = reconnect_keywords
        
        # 获取用于过滤的模式
        filter_patterns = ffmpeg_filter_patterns()
        
        # 标记是否发现需要重连的错误
        need_reconnect = False
        # 记录发现网络问题的时间
        network_issue_time = None
        network_issue_description = None
        
        # 关键调试信息关键词 - 这些总是要记录的
        important_keywords = ['rtmp', 'connect', 'error', 'fail', 'warning', 'unable', 'cannot', 'missing', 'invalid']
        
        # 输出每一行
        for line in iter(process.stderr.readline, ''):
            if not line:
                break
                
            # 去除末尾的空白字符    
            line = line.strip()
            if not line:
                continue
            
            # 更新最后活动时间 - 这是关键改进
            with process_lock:
                if task_id in active_processes:
                    active_processes[task_id]['last_activity_time'] = datetime.now(beijing_tz)
            
            # 检查这行是否包含重要关键词
            contains_important_keyword = any(keyword in line.lower() for keyword in important_keywords)
            
            # 如果包含重要关键词，不应该被过滤
            if contains_important_keyword:
                task_logger.info(line)
                logger.info(f'{log_prefix} {line}')
            else:
                # 检查这行是否匹配任何过滤模式
                should_filter = any(pattern.search(line) for pattern in filter_patterns)
                
                # 不匹配过滤模式的行和错误信息才记录到日志
                if not should_filter:
                    task_logger.info(line)
                    logger.debug(f'{log_prefix} {line}')
            
            # 判断是否包含错误相关信息
            if error_pattern.search(line):
                # 收集错误信息
                error_output.append(line)
                logger.warning(f'{log_prefix} 检测到可能的错误: {line}')
                task_logger.warning(f'检测到可能的错误: {line}')
                
                # 最多保留100行错误信息
                if len(error_output) > 100:
                    error_output.pop(0)
                    
                # 检查是否是重连相关的错误
                for pattern in reconnect_patterns:
                    if pattern.lower() in line.lower():
                        # 检查是否是需要立即重连的严重错误
                        if any(keyword in line.lower() for keyword in immediate_reconnect_keywords):
                            logger.warning(f'{log_prefix} 检测到严重错误，将跳过缓冲期直接重连: {line}')
                            task_logger.warning(f'检测到严重错误，将跳过缓冲期直接重连: {line}')
                            need_reconnect = True
                            # 记录问题描述以便记录统计
                            network_issue_description = get_error_description(line)
                            break

                        logger.warning(f'{log_prefix} 检测到网络异常，进入缓冲期监控: {line}')
                        task_logger.warning(f'检测到网络异常，进入缓冲期监控: {line}')
                        
                        # 记录问题描述
                        if network_issue_description is None:
                            network_issue_description = get_error_description(line)
                        
                        # 如果是首次检测到网络问题，记录时间
                        if network_issue_time is None:
                            network_issue_time = datetime.now(beijing_tz)
                            # 通知其他监控组件缓冲期已开始
                            with process_lock:
                                if task_id in active_processes:
                                    active_processes[task_id]['in_buffer_period'] = True
                                    active_processes[task_id]['buffer_period_start'] = network_issue_time
                                    active_processes[task_id]['network_issue_description'] = network_issue_description
                        else:
                            # 已经在缓冲期内，检查是否超过最大缓冲时间
                            buffer_seconds = (datetime.now(beijing_tz) - network_issue_time).total_seconds()
                            if buffer_seconds > NETWORK_BUFFER_TIME:
                                # 超过缓冲期，问题仍存在，标记需要重连
                                logger.warning(f'{log_prefix} 网络问题持续超过{NETWORK_BUFFER_TIME}秒，将执行重连')
                                task_logger.warning(f'网络问题持续超过{NETWORK_BUFFER_TIME}秒，将执行重连')
                                need_reconnect = True
                                # 重置问题时间，避免重复触发
                                network_issue_time = None
                                
                                # 添加统计
                                with process_lock:
                                    if task_id in active_processes:
                                        active_processes[task_id]['reconnect_reason'] = f"网络问题持续超过{NETWORK_BUFFER_TIME}秒: {network_issue_description}"
                                        active_processes[task_id]['buffer_timeout_count'] = active_processes[task_id].get('buffer_timeout_count', 0) + 1
                            else:
                                # 在缓冲期内，继续观察
                                task_logger.info(f'网络问题仍在缓冲期内 ({buffer_seconds:.1f}秒/{NETWORK_BUFFER_TIME}秒)')
                        break
        
        # 如果检测到正常的流媒体输出，可能表示问题已恢复
        if network_issue_time and is_network_recovered(line):
            # 检查是否在缓冲期内有正常输出
            buffer_seconds = (datetime.now(beijing_tz) - network_issue_time).total_seconds()
            logger.info(f'{log_prefix} 在缓冲期内({buffer_seconds:.1f}秒)检测到正常输出，网络已恢复')
            task_logger.info(f'在缓冲期内({buffer_seconds:.1f}秒)检测到正常输出，网络已恢复')
            
            # 统计缓冲期成功情况
            with process_lock:
                if task_id in active_processes:
                    active_processes[task_id]['in_buffer_period'] = False
                    # 记录恢复时间以便后续分析
                    if 'recovery_times' not in active_processes[task_id]:
                        active_processes[task_id]['recovery_times'] = []
                    active_processes[task_id]['recovery_times'].append(buffer_seconds)
                    # 增加成功计数
                    count_key = 'buffer_success_count'
                    active_processes[task_id][count_key] = active_processes[task_id].get(count_key, 0) + 1
                    logger.info(f'任务 {task_id} 缓冲期成功避免了不必要的重连，累计: {active_processes[task_id][count_key]}次')
            
            network_issue_time = None  # 重置问题时间
            network_issue_description = None
            
            # 在检测到网络已恢复并记录统计后添加
            if 'buffer_success_count' in active_processes[task_id]:
                success_count = active_processes[task_id]['buffer_success_count']
                task_logger.info(f"网络自动恢复统计: 累计成功恢复 {success_count} 次")
                
                # 如果有恢复时间记录，计算平均恢复时间
                if 'recovery_times' in active_processes[task_id] and active_processes[task_id]['recovery_times']:
                    recovery_times = active_processes[task_id]['recovery_times']
                    avg_recovery_time = sum(recovery_times) / len(recovery_times)
                    task_logger.info(f"平均网络恢复时间: {avg_recovery_time:.2f}秒")
        
        # 缓冲期防卡保护: 确保缓冲期不会无限期等待
        if network_issue_time:
            current_buffer_time = (datetime.now(beijing_tz) - network_issue_time).total_seconds()
            if current_buffer_time > NETWORK_MAX_BUFFER_TIME:
                logger.warning(f'{log_prefix} 缓冲期超过最大等待时间 {NETWORK_MAX_BUFFER_TIME}秒，强制结束缓冲')
                task_logger.warning(f'缓冲期超过最大等待时间 {NETWORK_MAX_BUFFER_TIME}秒，强制结束缓冲')
                need_reconnect = True
                network_issue_time = None
        
        # 缓冲期内进程状态监控: 检查进程是否在缓冲期内退出
        if network_issue_time and process.poll() is not None:
            logger.warning(f'{log_prefix} 缓冲期内进程已退出，将直接执行重连')
            task_logger.warning(f'缓冲期内进程已退出，将直接执行重连')
            need_reconnect = True
            network_issue_time = None
        
        # 进程结束后，检查返回值
        returncode = process.wait()
        task_logger.info(f"进程已结束 - 返回码: {returncode}")
        
        # 添加更详细的错误诊断
        if returncode != 0:
            task_logger.error(f"进程异常退出 - 返回码: {returncode}")
            
            # 汇总收集到的错误信息
            if error_output:
                error_summary = "\n".join(error_output[-10:]) # 最后10条错误信息
                task_logger.error(f"错误信息汇总:\n{error_summary}")
                logger.error(f"{log_prefix} 进程异常退出，错误信息:\n{error_summary}")
            else:
                task_logger.error("未捕获到明确的错误信息")
                
            # 尝试额外诊断
            try:
                # 检查视频文件是否存在
                video_path = task_info.get('video_path')
                if video_path:
                    if os.path.exists(video_path):
                        file_size = os.path.getsize(video_path)
                        task_logger.info(f"视频文件存在，大小: {file_size} 字节")
                    else:
                        task_logger.error(f"视频文件不存在: {video_path}")
            except Exception as e:
                task_logger.error(f"执行额外诊断时出错: {str(e)}")
        
        # 处理进程结束 - 尝试重连或更新状态
        with process_lock:
            if task_id in active_processes:
                # 如果是网络错误并标记了需要重连
                if (returncode != 0 and need_reconnect) or task_info.get('need_reconnect', False):
                    logger.warning(f'{log_prefix} 进程因网络问题结束，尝试外部重连')
                    task_logger.warning(f'进程因网络问题结束，尝试外部重连')
                    
                    # 记录重连详情
                    task_logger.info(f"===== 启动外部重连机制 =====")
                    task_logger.info(f"重连原因: {'网络错误检测' if need_reconnect else '任务标记需要重连'}")
                    task_logger.info(f"进程退出码: {returncode}")
                    if error_output:
                        error_summary = "\n".join(error_output[-5:]) # 最后5条错误信息
                        task_logger.info(f"导致重连的错误信息:\n{error_summary}")
                    
                    # 创建重连函数
                    from app.utils.video_utils import create_external_reconnect_function, monitor_and_reconnect
                    
                    # 记录当前任务状态
                    task_logger.info(f"当前任务状态:")
                    task_logger.info(f"- 已重启次数: {task_info.get('restart_count', 0)}")
                    task_logger.info(f"- 视频路径: {task_info.get('video_path')}")
                    task_logger.info(f"- RTMP地址: {task_info.get('rtmp_url')}")
                    
                    # 记录重连开始时间
                    reconnect_start_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    task_logger.info(f"开始创建重连函数时间: {reconnect_start_time}")
                    
                    reconnect_function = create_external_reconnect_function(
                        video_path=task_info.get('video_path'),
                        rtmp_url=task_info.get('rtmp_url'),
                        proxy_config_file=task_info.get('proxy_config_file'),
                        transcode_enabled=task_info.get('transcode_enabled', False),
                        task_id=task_id
                    )
                    
                    # 记录重连开始
                    logger.info(f'{log_prefix} 开始执行外部重连')
                    task_logger.info(f'开始执行外部重连')
                    
                    # # 记录延迟信息
                    # task_logger.info(f"延迟3秒后开始重连...")
                    
                    # # 延迟3秒后开始重连
                    # time.sleep(3)
                    
                    # 获取当前累计重连次数
                    total_reconnects = active_processes[task_id].get('total_reconnects', 0)
                    task_logger.info(f"当前累计重连次数: {total_reconnects}")
                    
                    # 执行重连
                    task_logger.info(f"开始monitor_and_reconnect重连过程")
                    new_process, updated_total_reconnects = monitor_and_reconnect(
                        process=None,  # 当前进程已结束
                        task_id=task_id,
                        reconnect_function=reconnect_function,
                        retry_delay=0,
                        max_retries=9999,
                        total_reconnects=total_reconnects
                    )
                    
                    if new_process:
                        # 重连成功，启动新的输出读取线程
                        logger.info(f'{log_prefix} 外部重连成功，启动新进程监控')
                        # task_logger.info(f'外部重连成功 ✓')
                        # task_logger.info(f'新进程PID: {new_process.pid}')
                        
                        # 记录重连成功的时间
                        reconnect_success_time = time.strftime("%Y-%m-%d %H:%M:%S")
                        # task_logger.info(f"重连成功时间: {reconnect_success_time}")
                        
                        # 获取当前重连信息
                        total_reconnects = updated_total_reconnects  # 累计总重连次数
                        
                        # 重置本次重启计数为1，因为这是重连成功后的第一次尝试
                        # 这样在日志中会显示为 "重连尝试 1/20"
                        restart_count = 1
                        
                        # # 更新日志信息
                        # task_logger.info(f"本次重启计数: {restart_count}")
                        # task_logger.info(f"累计重连次数: {total_reconnects}")
                        
                        # 更新进程信息
                        active_processes[task_id]['process'] = new_process
                        active_processes[task_id]['restart_count'] = restart_count  # 重置为1
                        active_processes[task_id]['total_reconnects'] = total_reconnects
                        active_processes[task_id]['last_reconnect_time'] = datetime.now(beijing_tz)
                        active_processes[task_id]['network_status'] = '已重连'
                        # 重置重试相关计数
                        active_processes[task_id]['retry_count'] = 0
                        
                        # 更新任务状态
                        update_task_status(task_id, {
                            "status": "running",
                            "message": "任务已通过机制重连",
                            "restart_count": restart_count,
                            "total_reconnects": total_reconnects
                        })
                        task_logger.info(f"已更新任务状态: running (已通过机制重连)")
                        
                        # 启动新的输出读取线程
                        video_executor.submit(read_output, new_process, task_id)
                        
                        return
                    else:
                        # 重连失败，更新任务状态为错误
                        logger.error(f'{log_prefix} 外部重连失败，任务终止')
                        task_logger.error(f'外部重连失败，任务终止 ✗')
                        
                        # 记录失败时间和详情
                        reconnect_fail_time = time.strftime("%Y-%m-%d %H:%M:%S")
                        task_logger.error(f"重连失败时间: {reconnect_fail_time}")
                        task_logger.error(f"最大重试次数已用尽，无法重新建立连接")
                        task_logger.error(f"===== 外部重连过程结束: 失败 =====")
                        
                        update_task_status(task_id, {
                            "status": "error",
                            "message": "重连失败",
                            "error_message": "网络问题导致流媒体中断，多次重连尝试失败",
                            "end_time": datetime.now(beijing_tz).isoformat()
                        })
                        task_logger.info(f"已更新任务状态: error (重连失败)")
                else:
                    # 不是重连错误，正常处理退出
                    if returncode != 0:
                        logger.error(f'{log_prefix} 进程异常退出，返回码：{returncode}')
                        task_logger.error(f'进程异常退出，返回码：{returncode}')
                        
                        # 如果有收集到错误信息，则记录
                        if error_output:
                            error_details = "\n".join(error_output)
                            logger.error(f'{log_prefix} 错误详情:\n{error_details}')
                            task_logger.error(f'错误详情:\n{error_details}')
                            
                            # 更新任务状态，包含错误详情
                            update_task_status(task_id, {
                                "status": "error",
                                "message": f"进程异常退出，返回码：{returncode}",
                                "error_message": error_details,
                                "end_time": datetime.now(beijing_tz).isoformat()
                            })
                            task_logger.info(f'已更新任务状态: error')
                        else:
                            logger.error(f'{log_prefix} 没有捕获到详细的错误输出')
                            task_logger.error(f'没有捕获到详细的错误输出')
                            
                            # 尝试从stderr获取所有内容
                            stderr_output = process.stderr.read()
                            if stderr_output:
                                logger.error(f'{log_prefix} stderr输出:\n{stderr_output}')
                                task_logger.error(f'stderr输出:\n{stderr_output}')
                                
                                # 更新任务状态，包含stderr输出
                                update_task_status(task_id, {
                                    "status": "error",
                                    "message": f"进程异常退出，返回码：{returncode}",
                                    "error_message": stderr_output,
                                    "end_time": datetime.now(beijing_tz).isoformat()
                                })
                                task_logger.info(f'已更新任务状态: error')
                            else:
                                # 如果没有stderr输出，则只更新基本状态
                                update_task_status(task_id, {
                                    "status": "error",
                                    "message": f"进程异常退出，返回码：{returncode}",
                                    "error_message": "没有捕获到详细的错误输出",
                                    "end_time": datetime.now(beijing_tz).isoformat()
                                })
                                task_logger.info(f'已更新任务状态: error')
                    else:
                        logger.info(f'{log_prefix} 进程正常退出，返回码：{returncode}')
                        task_logger.info(f'进程正常退出，返回码：{returncode}')
                        
                        # 更新任务状态为已完成
                        update_task_status(task_id, {
                            "status": "completed" if not active_processes[task_id].get('stopped_by_user') else "stopped",
                            "message": "任务已完成" if not active_processes[task_id].get('stopped_by_user') else "任务已手动停止",
                            "end_time": datetime.now(beijing_tz).isoformat()
                        })
                        
                        status = "completed" if not active_processes[task_id].get('stopped_by_user') else "stopped"
                        task_logger.info(f'已更新任务状态: {status}')
                
                # 如果不是重连成功，则从活动进程列表中移除
                if task_id in active_processes and not need_reconnect:
                    del active_processes[task_id]
                    logger.info(f'{log_prefix} 已从活动列表中移除任务')
        
        # 完成日志记录
        task_logger.info(f"===== 结束记录任务 {task_id} 的输出 =====")
        
        # 关闭任务日志处理器
        for handler in task_logger.handlers:
            handler.close()
        
    except Exception as e:
        # 获取完整的异常堆栈
        import traceback
        stack_trace = traceback.format_exc()
        
        logger.error(f'读取进程输出时发生错误 - task_id={task_id}: {str(e)}\n{stack_trace}')
        
        # 尝试记录到任务日志
        try:
            task_logger = get_task_logger(task_id)
            task_logger.error(f'读取进程输出时发生错误: {str(e)}\n{stack_trace}')
            
            # 关闭任务日志处理器
            for handler in task_logger.handlers:
                handler.close()
        except Exception as log_error:
            logger.error(f'无法写入任务日志 - task_id={task_id}: {str(log_error)}')
        
        # 确保任务状态被更新为错误
        try:
            update_task_status(task_id, {
                "status": "error",
                "message": f"监控进程输出时发生错误: {str(e)}",
                "error_message": stack_trace,
                "end_time": datetime.now(beijing_tz).isoformat()
            })
        except Exception as update_error:
            logger.error(f'更新任务状态时发生错误 - task_id={task_id}: {str(update_error)}')
            
        # 确保进程被终止
        try:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except:
                    process.kill()
                logger.info(f'已终止进程 - task_id={task_id}')
        except Exception as term_error:
            logger.error(f'终止进程时发生错误 - task_id={task_id}: {str(term_error)}')
            
        # 确保从活动进程列表中移除
        with process_lock:
            if task_id in active_processes:
                del active_processes[task_id]
                logger.info(f'已从活动列表中移除任务 - task_id={task_id}')

async def stop_stream(task_id: str, is_auto_stop: bool = False):
    """停止推流任务"""
    logger.info(f'开始停止推流任务 - task_id={task_id}, is_auto_stop={is_auto_stop}')
    
    with process_lock:
        if task_id not in active_processes:
            logger.warning(f'任务不存在 - task_id={task_id}')
            # 即使任务不在活动进程中，也将状态设置为stopped而不是error
            if not is_auto_stop:  # 只有手动停止时才设置为stopped
                update_task_status(task_id, {
                    'status': 'stopped',
                    'end_time': datetime.now(beijing_tz).isoformat(),
                    'message': '任务已手动停止（任务可能已结束或不存在）'
                })
            return False
            
        process_info = active_processes[task_id]
        process = process_info['process']
        
        # 标记停止原因
        if is_auto_stop:
            process_info['auto_stopped'] = True
        else:
            process_info['stopped_by_user'] = True
            
        # 检查进程是否已经结束
        if process.poll() is not None:
            logger.info(f'进程已经结束 - task_id={task_id}, 退出码={process.poll()}')
            # 从活动进程列表中移除
            del active_processes[task_id]
            return True
            
        # 尝试优雅地终止进程
        try:
            # 发送q命令给FFmpeg
            if platform.system() == 'Windows':
                process.communicate(input=b'q', timeout=5)
            else:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                
            # 等待进程结束
            try:
                process.wait(timeout=5)
                logger.info(f'进程已优雅终止 - task_id={task_id}')
            except subprocess.TimeoutExpired:
                # 如果超时，强制终止
                logger.warning(f'进程未能优雅终止，强制终止 - task_id={task_id}')
                process.kill()
                
            # 从活动进程列表中移除
            del active_processes[task_id]
            
            # 清理代理配置文件
            if 'proxy_config_file' in process_info and process_info['proxy_config_file']:
                cleanup_proxy_config(process_info['proxy_config_file'])
                
            return True
            
        except Exception as e:
            logger.error(f'停止进程时发生错误 - task_id={task_id}, error={str(e)}')
            # 尝试强制终止
            try:
                process.kill()
                logger.info(f'已强制终止进程 - task_id={task_id}')
                # 从活动进程列表中移除
                del active_processes[task_id]
                return True
            except Exception as e2:
                logger.error(f'强制终止进程失败 - task_id={task_id}, error={str(e2)}')
                return False

def stop_stream_sync(task_id: str):
    """同步版本的停止流函数，用于调度器"""
    current_time = datetime.now(beijing_tz)
    logger.info(f'调度器触发停止任务 - task_id={task_id}, 触发时间: {current_time.strftime("%Y-%m-%d %H:%M:%S %Z")}')
    
    # 获取任务的原始配置
    with process_lock:
        if task_id in active_processes:
            task_info = active_processes[task_id]
            start_time = task_info.get('start_time')
            auto_stop_minutes = task_info.get('auto_stop_minutes')
            if start_time and auto_stop_minutes:
                expected_stop_time = start_time + timedelta(minutes=auto_stop_minutes)
                actual_runtime = current_time - start_time
                logger.info(f'任务运行信息 - 开始时间: {start_time.strftime("%Y-%m-%d %H:%M:%S %Z")}, '
                          f'预期停止时间: {expected_stop_time.strftime("%Y-%m-%d %H:%M:%S %Z")}, '
                          f'实际运行时长: {actual_runtime.total_seconds() / 60:.2f}分钟')
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(stop_stream(task_id, is_auto_stop=True))
        loop.close()
        
        if result:
            logger.info(f'调度器成功停止任务 - task_id={task_id}')
            # 获取当前时间作为结束时间
            end_time = datetime.now(beijing_tz)
            
            # 更新任务状态
            update_task_status(task_id, {
                'status': 'auto_stopped',
                'end_time': end_time.isoformat(),
                'message': '任务已自动停止（达到预设时间）'
            })
        else:
            logger.error(f'调度器停止任务失败 - task_id={task_id}')
            update_task_status(task_id, {
                'status': 'error',
                'end_time': datetime.now(beijing_tz).isoformat(),
                'message': '自动停止失败'
            })
            
    except Exception as e:
        error_msg = f'调度器执行停止任务时发生错误: {str(e)}'
        logger.error(f'调度器执行停止任务时发生错误 - task_id={task_id}, error={str(e)}')
        update_task_status(task_id, {
            'status': 'error',
            'end_time': datetime.now(beijing_tz).isoformat(),
            'message': error_msg
        })
    finally:
        logger.info(f'调度器停止任务执行完成 - task_id={task_id}') 

def check_rtmp_connection(rtmp_url):
    """
    检查RTMP连接状态
    
    Args:
        rtmp_url: RTMP URL
        
    Returns:
        str: 连接状态 - 'connected', 'disconnected', 'error', 'timeout'
    """
    try:
        # 使用test_rtmp_connection函数测试连接
        success, message = test_rtmp_connection(rtmp_url, timeout=3)
        
        if success:
            return 'connected'
        elif 'timeout' in message.lower():
            return 'timeout'
        elif 'refused' in message.lower() or 'reset' in message.lower():
            return 'disconnected'
        else:
            return 'error'
    except Exception as e:
        logger.error(f"检查RTMP连接状态时发生错误: {str(e)}")
        return 'error'

def monitor_all_rtmp_connections():
    """监控所有RTMP连接状态"""
    with process_lock:
        for task_id, info in list(active_processes.items()):
            # 获取当前网络状态
            current_status = check_rtmp_connection(info['rtmp_url'])
            previous_status = info.get('network_status', 'unknown')
            
            # 更新当前状态
            active_processes[task_id]['network_status'] = current_status
            
            # 关键部分：检测网络是否从异常恢复为正常
            if previous_status in ['disconnected', 'error', 'timeout'] and current_status == 'connected':
                # 重置所有重连相关参数
                active_processes[task_id]['retry_count'] = 0
                active_processes[task_id]['network_warning'] = False
                
                # 重置退避时间和延迟相关参数
                active_processes[task_id]['last_retry_time'] = None
                active_processes[task_id]['current_delay'] = None
                active_processes[task_id]['last_error_type'] = None
                
                # 记录重置事件
                logger.info(f"任务 {task_id} 的网络连接已恢复，已重置所有重连参数")
                
                # 获取任务日志记录器并记录恢复事件
                task_logger = get_task_logger(task_id)
                task_logger.info(f"网络连接已恢复正常，重置重连参数")
            
            # 其他现有的网络监控逻辑... 

def restart_stream_task(task_id: str) -> bool:
    """
    重启一个流任务
    
    Args:
        task_id: 任务ID
        
    Returns:
        bool: 是否成功重启
    """
    try:
        with process_lock:
            # 检查任务是否存在
            if task_id not in active_processes:
                logger.warning(f"无法重启任务 {task_id}：任务不存在")
                return False
                
            task_info = active_processes[task_id]
            
            # 检查进程状态
            process = task_info.get('process')
            if process and process.poll() is None:
                logger.warning(f"任务 {task_id} 进程仍在运行，尝试停止")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                    except:
                        pass
            
            # 获取必要的任务信息
            rtmp_url = task_info.get('rtmp_url')
            video_path = task_info.get('video_path')
            proxy_config_file = task_info.get('proxy_config_file')
            transcode_enabled = task_info.get('transcode_enabled', False)
            task_logger = get_task_logger(task_id)
            
            if not rtmp_url or not video_path:
                logger.error(f"无法重启任务 {task_id}：缺少必要信息")
                return False
            
            # 读取代理配置文件内容
            proxy_config = None
            if proxy_config_file:
                try:
                    if os.path.exists(proxy_config_file):
                        with open(proxy_config_file, 'r') as f:
                            proxy_config = json.load(f)
                        logger.info(f"[task_id={task_id}] 已加载代理配置")
                        task_logger.info(f"已加载代理配置文件: {proxy_config_file}")
                        task_logger.info(f"代理配置内容: {proxy_config}")
                    else:
                        task_logger.warning(f"代理配置文件不存在: {proxy_config_file}")
                        # 创建默认代理配置，确保命令行有代理参数
                        proxy_config = {
                            "socks5_proxy": "socks5://127.0.0.1:1080",  # 默认本地代理
                            "created_at": datetime.datetime.now(beijing_tz).isoformat(),
                            "task_id": task_id,
                            "note": "此为默认配置，由于原配置文件不存在而创建"
                        }
                        task_logger.info(f"已创建默认代理配置: {proxy_config}")
                except Exception as e:
                    logger.error(f"[task_id={task_id}] 加载代理配置失败: {str(e)}")
                    task_logger.error(f"加载代理配置失败: {str(e)}")
                    # 创建默认代理配置，确保命令行有代理参数
                    proxy_config = {
                        "socks5_proxy": "socks5://127.0.0.1:1080",  # 默认本地代理
                        "created_at": datetime.datetime.now(beijing_tz).isoformat(),
                        "task_id": task_id,
                        "note": "此为默认配置，由于加载配置文件失败而创建"
                    }
                    task_logger.info(f"已创建默认代理配置: {proxy_config}")
            
            # 构建命令参数
            ffmpeg_cmd, env = get_ffmpeg_command(
                input_file=video_path,
                output_rtmp=rtmp_url,
                proxy_config=proxy_config,
                transcode=transcode_enabled,
                task_id=task_id
            )
            
            # 启动新进程
            logger.info(f"重启任务 {task_id}...")
            task_logger.info(f"重启流任务...")
            
            # 创建进程对象
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=env
            )
            
            if not process:
                logger.error(f"创建新进程失败: {task_id}")
                return False
            
            # 更新进程信息
            active_processes[task_id]['process'] = process
            active_processes[task_id]['restart_count'] = active_processes[task_id].get('restart_count', 0) + 1
            
            # 更新任务状态
            update_task_status(task_id, {
                "status": "running",
                "message": f"任务已重启",
                "restart_count": active_processes[task_id]['restart_count']
            })
            
            # 启动输出监控线程
            threading.Thread(
                target=read_output,
                args=(process, task_id),
                daemon=True
            ).start()
            
            logger.info(f"任务 {task_id} 重启成功")
            task_logger.info(f"任务重启成功")
            return True
            
    except Exception as e:
        logger.error(f"重启任务 {task_id} 时发生错误: {str(e)}")
        
    return False

def get_ffmpeg_command(video_path, rtmp_url, is_windows=False, transcode_enabled=False, proxy_config=None, task_id=None):
    """
    获取FFmpeg命令行
    
    在utils.get_ffmpeg_command基础上封装，以便兼容旧API
    
    参数:
        video_path (str): 视频文件路径
        rtmp_url (str): RTMP推流地址
        is_windows (bool): 是否Windows系统
        transcode_enabled (bool): 是否转码
        proxy_config (dict): 代理配置
        task_id (str): 任务ID
    
    返回:
        tuple: (命令行列表, 环境变量字典)
    """
    """直接使用utils中的get_ffmpeg_command，因为重连参数已经加强"""
    from app.utils.video_utils import get_ffmpeg_command as utils_get_ffmpeg_command
    
    # 调用原始函数获取命令，确保参数名称正确
    cmd, env = utils_get_ffmpeg_command(
        input_file=video_path,
        output_rtmp=rtmp_url,
        proxy_config=proxy_config,
        transcode=transcode_enabled,
        task_id=task_id
    )
    
    return cmd, env

async def start_stream(task_id: str, video_path: str, rtmp_url: str, proxy_config_file: str = None, transcode_enabled: bool = False) -> dict:
    """
    启动视频流任务
    
    Args:
        task_id: 任务ID
        video_path: 视频文件路径
        rtmp_url: RTMP推流地址
        proxy_config_file: 代理配置文件路径
        transcode_enabled: 是否启用转码
        
    Returns:
        dict: 任务信息
    """
    logger.info(f'开始启动视频流任务 - task_id={task_id}')
    
    # 检查任务是否已存在
    with process_lock:
        if task_id in active_processes:
            logger.warning(f'任务已存在 - task_id={task_id}')
            return {
                "status": "error",
                "message": "任务已存在",
                "task_id": task_id
            }
    
    # 检查视频文件
    if not os.path.exists(video_path):
        logger.error(f'视频文件不存在 - path={video_path}')
        return {
            "status": "error",
            "message": "视频文件不存在",
            "task_id": task_id
        }
        
    # 创建任务日志记录器
    task_logger = get_task_logger(task_id)
    
    # 加载代理配置（如果有）
    proxy_config = None
    if proxy_config_file and os.path.exists(proxy_config_file):
        try:
            with open(proxy_config_file, 'r') as f:
                proxy_config = json.load(f)
            task_logger.info(f"已加载代理配置：{proxy_config_file}")
        except Exception as e:
            task_logger.error(f"加载代理配置失败: {str(e)}")
            return {
                "status": "error",
                "message": f"加载代理配置失败: {str(e)}",
                "task_id": task_id
            }
    
    # 构建FFmpeg命令
    try:
        from app.utils.video_utils import get_ffmpeg_command
        ffmpeg_cmd, env = get_ffmpeg_command(
            input_file=video_path,
            output_rtmp=rtmp_url,
            proxy_config=proxy_config,
            transcode=transcode_enabled,
            task_id=task_id
        )
        
        # 记录命令
        cmd_str = ' '.join(ffmpeg_cmd)
        logger.info(f'FFmpeg命令: {cmd_str}')
        task_logger.info(f'FFmpeg命令: {cmd_str}')
        
        # 启动进程
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        pid = process.pid
        logger.info(f'进程已启动 - task_id={task_id}, pid={pid}')
        task_logger.info(f'进程已启动 - pid={pid}')
        
        # 保存进程信息
        start_time = datetime.now(beijing_tz).isoformat()
        
        task_info = {
            "task_id": task_id,
            "video_path": video_path,
            "rtmp_url": rtmp_url,
            "process": process,
            "pid": pid,
            "start_time": start_time,
            "status": "running",
            "message": "任务已启动",
            "transcode_enabled": transcode_enabled,
            "proxy_config_file": proxy_config_file,
            "use_proxy": proxy_config is not None,
            "ffmpeg_cmd": cmd_str,
            "restart_count": 0,
            "need_reconnect": True,  # 允许外部重连机制工作
            "stopped_by_user": False
        }
        
        # 添加到活动进程列表
        with process_lock:
            active_processes[task_id] = task_info
        
        # 启动线程监控进程输出
        threading.Thread(
            target=read_output,
            args=(process, task_id),
            daemon=True
        ).start()
        
        return {
            "status": "success",
            "message": "任务已启动",
            "task_id": task_id,
            "pid": pid,
            "start_time": start_time
        }
        
    except Exception as e:
        # 获取详细的异常堆栈
        import traceback
        error_details = traceback.format_exc()
        
        logger.error(f'启动任务失败 - task_id={task_id}: {str(e)}\n{error_details}')
        task_logger.error(f'启动任务失败: {str(e)}\n{error_details}')
        
        return {
            "status": "error",
            "message": f"启动任务失败: {str(e)}",
            "task_id": task_id,
            "error_details": error_details
        } 

def safe_trigger_reconnect(task_id, reason):
    """
    安全地触发重连，防止并发重连
    
    Args:
        task_id: 任务ID
        reason: 重连原因
        
    Returns:
        bool: 是否成功触发重连
    """
    logger.info(f'尝试安全触发任务 {task_id} 重连，原因: {reason}')
    
    with process_lock:
        if task_id not in active_processes:
            logger.warning(f'任务 {task_id} 不在活动列表中，无法重连')
            return False
            
        # 检查是否已经在重连中
        if active_processes[task_id].get('reconnecting'):
            logger.info(f'任务 {task_id} 已在重连过程中，忽略新的重连请求')
            return False
            
        # 检查是否在验证中
        if active_processes[task_id].get('verifying_reconnection'):
            logger.info(f'任务 {task_id} 正在验证重连状态，忽略新的重连请求')
            return False
            
        # 检查上次重连时间，避免频繁重连
        last_reconnect_time = active_processes[task_id].get('last_reconnect_time')
        if last_reconnect_time:
            now = datetime.now(beijing_tz)
            seconds_since_last_reconnect = (now - last_reconnect_time).total_seconds()
            if seconds_since_last_reconnect < 30:
                logger.warning(f'距离上次重连仅 {seconds_since_last_reconnect:.1f} 秒，忽略本次重连请求')
                return False
                
        # 标记为重连中
        active_processes[task_id]['reconnecting'] = True
        active_processes[task_id]['reconnect_initiated_by'] = 'safe_trigger_func'
        active_processes[task_id]['reconnect_reason'] = reason
        
        # 重置缓冲期状态
        active_processes[task_id]['in_buffer_period'] = False
        active_processes[task_id]['network_issue_start_time'] = None
    
    # 实际触发重连
    return trigger_reconnect(task_id, reason)

def trigger_task_reconnect(task_id: str, reason: str = "外部触发重连") -> bool:
    """
    主动触发任务重连
    
    Args:
        task_id: 任务ID
        reason: 重连原因
        
    Returns:
        bool: 是否成功触发重连
    """
    logger.info(f"主动触发任务重连 - task_id={task_id}, 原因: {reason}")
    
    with process_lock:
        if task_id not in active_processes:
            logger.warning(f"无法重连任务 {task_id}：任务不存在")
            return False
            
        task_info = active_processes[task_id]
        process = task_info.get('process')
        
        # 获取任务日志记录器
        task_logger = get_task_logger(task_id)
        task_logger.info(f"触发主动重连 - 原因: {reason}")
        
        # 检查是否正在重连或已经在短时间内重连过
        if task_info.get('reconnecting', False):
            task_logger.warning("已有重连操作正在进行中，忽略本次重连请求")
            return False
            
        last_reconnect_time = task_info.get('last_reconnect_time')
        if last_reconnect_time:
            now = datetime.now(beijing_tz)
            seconds_since_last_reconnect = (now - last_reconnect_time).total_seconds()
            # 避免在30秒内重复重连
            if seconds_since_last_reconnect < 30:
                task_logger.warning(f"距离上次重连仅 {seconds_since_last_reconnect:.1f} 秒，忽略本次重连请求")
                return False
        
        # 标记任务为正在重连状态
        task_info['reconnecting'] = True
        task_info['need_reconnect'] = True
        task_info['reconnect_reason'] = reason
        
        # 如果进程仍在运行，尝试优雅地终止它
        if process and process.poll() is None:
            task_logger.info("当前进程仍在运行，尝试终止它")
            
            try:
                # 先尝试发送q命令给FFmpeg
                if platform.system() == 'Windows':
                    process.communicate(input=b'q', timeout=3)
                else:
                    process.stdin.write(b'q\n')
                    process.stdin.flush()
                
                # 等待进程结束
                try:
                    process.wait(timeout=5)
                    task_logger.info("进程已优雅终止")
                except subprocess.TimeoutExpired:
                    # 如果超时，强制终止
                    task_logger.warning("进程未能优雅终止，强制终止")
                    process.kill()
                    
                task_logger.info("当前进程已终止，等待外部重连机制执行")
                return True
                
            except Exception as e:
                task_logger.error(f"终止进程时发生错误: {str(e)}")
                # 尝试强制终止
                try:
                    process.kill()
                    task_logger.info("进程已强制终止")
                    return True
                except Exception as e2:
                    task_logger.error(f"强制终止进程失败: {str(e2)}")
                    # 重置重连标志
                    task_info['reconnecting'] = False
                    return False
        else:
            # 进程已结束，可以直接重连
            task_logger.info("当前进程已结束，直接执行重连")
            
            # 创建重连函数
            from app.utils.video_utils import create_external_reconnect_function, monitor_and_reconnect
            
            reconnect_function = create_external_reconnect_function(
                video_path=task_info.get('video_path'),
                rtmp_url=task_info.get('rtmp_url'),
                proxy_config_file=task_info.get('proxy_config_file'),
                transcode_enabled=task_info.get('transcode_enabled', False),
                task_id=task_id
            )
            
            # 获取当前累计重连次数
            total_reconnects = task_info.get('total_reconnects', 0)
            
            # 执行重连
            threading.Thread(
                target=lambda: _execute_reconnect(task_id, reconnect_function, total_reconnects),
                daemon=True
            ).start()
            
            return True
    
    return False

def _execute_reconnect(task_id, reconnect_function, total_reconnects):
    """
    执行重连操作（在单独线程中调用）
    """
    try:
        # 获取任务记录器
        task_logger = get_task_logger(task_id)
        task_logger.info("开始执行重连操作")
        
        # 导入video_utils中的重连函数
        from app.utils.video_utils import monitor_and_reconnect
        
        # 执行重连
        new_process, updated_total_reconnects = monitor_and_reconnect(
            process=None,  # 当前进程已结束或正在结束
            task_id=task_id,
            reconnect_function=reconnect_function,
            retry_delay=0,  # 立即重连
            max_retries=9999,
            total_reconnects=total_reconnects
        )
        
        with process_lock:
            if task_id not in active_processes:
                task_logger.warning("重连时发现任务已不存在于活动任务列表")
                return
                
            if new_process:
                # 重连成功，但需要验证
                task_logger.info("重连初步成功，开始验证重连状态")
                
                # 更新进程信息，标记为验证中
                active_processes[task_id]['process'] = new_process
                active_processes[task_id]['verifying_reconnection'] = True
                active_processes[task_id]['last_activity_time'] = datetime.now(beijing_tz)
                
                # 启动输出读取线程（确保能捕获输出）
                video_executor.submit(read_output, new_process, task_id)
                
                # 给输出读取线程一点时间启动
                time.sleep(2)
                
                # 验证重连状态
                verification_result = verify_reconnection_status(task_id, new_process)
                
                # 根据验证结果更新状态
                with process_lock:
                    if task_id not in active_processes:
                        return
                        
                    if verification_result:
                        # 重连验证成功
                        task_logger.info("重连验证成功，更新任务状态")
                        
                        # 更新进程信息
                        active_processes[task_id]['restart_count'] = 1  # 重置为1
                        active_processes[task_id]['total_reconnects'] = updated_total_reconnects
                        active_processes[task_id]['last_reconnect_time'] = datetime.now(beijing_tz)
                        active_processes[task_id]['network_status'] = '已重连并验证'
                        active_processes[task_id]['reconnecting'] = False
                        active_processes[task_id]['need_reconnect'] = False
                        active_processes[task_id]['verifying_reconnection'] = False
                        # 重置网络相关状态
                        reset_task_network_status(task_id)
                        # 重置重试相关计数
                        active_processes[task_id]['retry_count'] = 0
                        
                        # 更新任务状态
                        update_task_status(task_id, {
                            "status": "running",
                            "message": f"任务已通过主动重连机制恢复并验证",
                            "restart_count": 1,
                            "total_reconnects": updated_total_reconnects
                        })
                    else:
                        # 重连验证失败，再次尝试重连或终止
                        task_logger.error("重连验证失败，任务状态不稳定")
                        
                        # 检查是否应该再次尝试重连
                        retry_count = active_processes[task_id].get('reconnect_verify_retries', 0)
                        if retry_count < 2:  # 最多尝试2次验证
                            # 增加重试计数
                            active_processes[task_id]['reconnect_verify_retries'] = retry_count + 1
                            task_logger.warning(f"重连验证失败，将再次尝试重连 (尝试 {retry_count + 1}/2)")
                            
                            # 终止当前进程
                            try:
                                if new_process and new_process.poll() is None:
                                    new_process.terminate()
                                    try:
                                        new_process.wait(timeout=5)
                                    except:
                                        new_process.kill()
                            except:
                                pass
                            
                            # 延迟5秒后再次尝试重连
                            time.sleep(5)
                            _execute_reconnect(task_id, reconnect_function, updated_total_reconnects)
                            return
                        else:
                            # 重连验证多次失败，终止任务
                            task_logger.error("重连验证多次失败，任务终止")
                            
                            # 更新任务状态
                            update_task_status(task_id, {
                                "status": "error",
                                "message": "主动重连失败：验证未通过",
                                "error_message": "任务重连后状态不稳定，多次验证失败",
                                "end_time": datetime.now(beijing_tz).isoformat()
                            })
                            
                            # 终止当前进程
                            try:
                                if new_process and new_process.poll() is None:
                                    new_process.terminate()
                                    try:
                                        new_process.wait(timeout=5)
                                    except:
                                        new_process.kill()
                            except:
                                pass
                            
                            # 从活动任务列表中移除
                            if task_id in active_processes:
                                del active_processes[task_id]
            else:
                # 重连失败
                task_logger.error("重连失败，任务终止")
                
                # 更新任务状态
                update_task_status(task_id, {
                    "status": "error",
                    "message": "主动重连失败",
                    "error_message": "尝试重新建立连接失败",
                    "end_time": datetime.now(beijing_tz).isoformat()
                })
                
                # 从活动任务列表中移除
                if task_id in active_processes:
                    del active_processes[task_id]
    
    except Exception as e:
        logger.error(f"执行重连操作时发生错误 - task_id={task_id}: {str(e)}")
        
        # 获取详细的异常堆栈
        import traceback
        stack_trace = traceback.format_exc()
        
        try:
            # 记录错误到任务日志
            task_logger = get_task_logger(task_id)
            task_logger.error(f"执行重连操作时发生错误: {str(e)}\n{stack_trace}")
            
            # 更新任务状态
            update_task_status(task_id, {
                "status": "error",
                "message": f"重连过程中发生错误: {str(e)}",
                "error_message": stack_trace,
                "end_time": datetime.now(beijing_tz).isoformat()
            })
            
            # 重置任务状态或从活动列表中移除
            with process_lock:
                if task_id in active_processes:
                    active_processes[task_id]['reconnecting'] = False
                    active_processes[task_id]['verifying_reconnection'] = False
                    # 如果没有活动进程，则从列表中移除
                    if not active_processes[task_id].get('process') or active_processes[task_id]['process'].poll() is not None:
                        del active_processes[task_id]
        except Exception as inner_e:
            logger.error(f"处理重连错误时又发生错误 - task_id={task_id}: {str(inner_e)}")

def verify_reconnection_status(task_id, new_process, verification_time=20):
    """
    验证重连后的任务是否真正恢复正常
    
    Args:
        task_id: 任务ID
        new_process: 新创建的进程
        verification_time: 验证时间（秒）
        
    Returns:
        bool: 是否验证通过
    """
    logger.info(f"开始验证重连状态 - task_id={task_id}, 验证时间={verification_time}秒")
    task_logger = get_task_logger(task_id)
    task_logger.info(f"开始验证重连状态, 验证时间={verification_time}秒")
    
    # 初始化验证状态
    verification_results = {
        'process_alive': False,     # 进程存活
        'has_output': False,        # 有输出产生
        'network_stable': False,    # 网络稳定
        'no_critical_errors': True  # 无严重错误
    }
    
    # 记录开始时间
    start_time = datetime.now(beijing_tz)
    end_time = start_time + timedelta(seconds=verification_time)
    
    # 初始化错误计数器
    error_count = 0
    critical_errors = []
    
    # 输出采集器（用于捕获这段时间的输出）
    captured_output = []
    
    # 监控循环
    while datetime.now(beijing_tz) < end_time:
        # 1. 检查进程是否仍在运行
        if new_process.poll() is not None:
            exitcode = new_process.poll()
            task_logger.error(f"验证失败: 进程已退出, 退出码={exitcode}")
            verification_results['process_alive'] = False
            return False
        else:
            verification_results['process_alive'] = True
        
        # 2. 检查是否有新输出
        with process_lock:
            if task_id in active_processes:
                last_activity = active_processes[task_id].get('last_activity_time')
                if last_activity:
                    seconds_since_activity = (datetime.now(beijing_tz) - last_activity).total_seconds()
                    if seconds_since_activity < 5:  # 5秒内有活动
                        verification_results['has_output'] = True
        
        # 3. 检查网络状态
        with process_lock:
            if task_id in active_processes:
                rtmp_url = active_processes[task_id].get('rtmp_url')
                if rtmp_url:
                    from app.utils.network_utils import extract_host_from_rtmp
                    host = extract_host_from_rtmp(rtmp_url)
                    
                    # 执行ping测试
                    try:
                        result = subprocess.run(['ping', '-c', '2', host], 
                                           capture_output=True, text=True, timeout=3)
                        if result.returncode == 0:
                            loss_match = re.search(r'(\d+)% packet loss', result.stdout)
                            if loss_match and int(loss_match.group(1)) < 20:
                                verification_results['network_stable'] = True
                    except Exception as e:
                        task_logger.warning(f"网络检测失败: {str(e)}")
        
        # 4. 检查关键错误
        try:
            stderr_output = new_process.stderr.readline()
            if stderr_output:
                captured_output.append(stderr_output.strip())
                
                # 检测严重错误
                error_keywords = ['Error', 'Failed', 'Cannot', 'Unable', 'Invalid']
                if any(keyword.lower() in stderr_output.lower() for keyword in error_keywords):
                    error_count += 1
                    critical_errors.append(stderr_output.strip())
                    
                # 如果有过多错误，标记验证失败
                if error_count >= 3:
                    verification_results['no_critical_errors'] = False
                    task_logger.error(f"验证失败: 检测到多个错误, 最后错误: {stderr_output.strip()}")
        except Exception:
            pass
            
        # 每次循环睡眠一段时间
        time.sleep(2)
    
    # 验证结束，分析结果
    passed_checks = sum(1 for result in verification_results.values() if result)
    total_checks = len(verification_results)
    
    # 记录详细的验证结果
    task_logger.info(f"重连验证结果:")
    task_logger.info(f"- 进程存活: {'是' if verification_results['process_alive'] else '否'}")
    task_logger.info(f"- 有输出产生: {'是' if verification_results['has_output'] else '否'}")
    task_logger.info(f"- 网络稳定: {'是' if verification_results['network_stable'] else '否'}")
    task_logger.info(f"- 无严重错误: {'是' if verification_results['no_critical_errors'] else '否'}")
    
    if passed_checks < total_checks - 1:  # 允许一项检查失败
        task_logger.warning(f"重连验证未完全通过: {passed_checks}/{total_checks} 项检查通过")
        if critical_errors:
            task_logger.error(f"验证期间的关键错误: {critical_errors}")
        return False
    
    task_logger.info(f"重连验证通过: {passed_checks}/{total_checks} 项检查通过")
    
    # 最后检查流媒体状态
    try:
        if captured_output:
            # 分析捕获的输出，查找关键信息
            fps_pattern = re.compile(r'fps=\s*(\d+)')
            speed_pattern = re.compile(r'speed=\s*([\d.]+)x')
            
            for line in captured_output[-10:]:  # 查看最后10行
                fps_match = fps_pattern.search(line)
                speed_match = speed_pattern.search(line)
                
                if fps_match:
                    fps = int(fps_match.group(1))
                    task_logger.info(f"检测到帧率: {fps} fps")
                    
                if speed_match:
                    speed = float(speed_match.group(1))
                    task_logger.info(f"检测到编码速度: {speed}x")
                    
                    # 如果编码速度过低，可能有问题
                    if speed < 0.5:
                        task_logger.warning(f"编码速度较低: {speed}x, 可能影响流媒体质量")
    except Exception as e:
        task_logger.warning(f"分析输出时出错: {str(e)}")
    
    # 所有检查通过，确认重连成功
    return True

def check_rtmp_connection_health(rtmp_url, timeout=3):
    """
    检查RTMP连接的健康状态，返回更详细的信息
    
    Args:
        rtmp_url: RTMP URL
        timeout: 超时时间（秒）
        
    Returns:
        dict: 连接健康报告
    """
    health_report = {
        'status': 'unknown',
        'latency_ms': None,
        'error_message': None,
        'timestamp': datetime.now(beijing_tz)
    }
    
    try:
        # 提取主机信息
        from app.utils.network_utils import extract_host_from_rtmp
        host = extract_host_from_rtmp(rtmp_url)
        
        if not host:
            health_report['status'] = 'error'
            health_report['error_message'] = '无法从RTMP URL提取主机地址'
            return health_report
        
        # 1. 先进行ping测试
        ping_start = time.time()
        ping_result = subprocess.run(['ping', '-c', '3', host], 
                                  capture_output=True, text=True, timeout=timeout)
        ping_duration = time.time() - ping_start
        
        # 处理ping结果
        if ping_result.returncode == 0:
            # 提取ping延迟
            avg_match = re.search(r'min/avg/max/mdev = [\d.]+/([\d.]+)', ping_result.stdout)
            if avg_match:
                health_report['latency_ms'] = float(avg_match.group(1))
            
            # 提取丢包率
            loss_match = re.search(r'(\d+)% packet loss', ping_result.stdout)
            if loss_match:
                packet_loss = int(loss_match.group(1))
                health_report['packet_loss'] = packet_loss
                
                # 根据丢包率判断状态
                if packet_loss == 0:
                    health_report['status'] = 'excellent'
                elif packet_loss < 5:
                    health_report['status'] = 'good'
                elif packet_loss < 20:
                    health_report['status'] = 'fair'
                else:
                    health_report['status'] = 'poor'
            else:
                health_report['status'] = 'unknown'
        else:
            health_report['status'] = 'error'
            health_report['error_message'] = 'Ping测试失败'
        
        # 2. 尝试TCP连接测试
        try:
            import socket
            port = 1935  # RTMP默认端口
            
            tcp_start = time.time()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            tcp_duration = time.time() - tcp_start
            s.close()
            
            health_report['tcp_connect_time'] = tcp_duration * 1000  # 转换为毫秒
            
            # 如果TCP连接成功，状态至少是fair
            if health_report['status'] in ['error', 'unknown', 'poor']:
                health_report['status'] = 'fair'
                
        except Exception as e:
            health_report['tcp_error'] = str(e)
            
            # 如果TCP连接失败，状态降为error
            health_report['status'] = 'error'
            health_report['error_message'] = f'TCP连接失败: {str(e)}'
        
        return health_report
    
    except Exception as e:
        health_report['status'] = 'error'
        health_report['error_message'] = f'检查连接健康时出错: {str(e)}'
        return health_report

def is_network_recovered(line):
    """
    判断一行输出是否表示网络已恢复
    
    Args:
        line: FFmpeg的一行输出
        
    Returns:
        bool: 是否表示网络已恢复
    """
    line = line.lower()
    
    # 成功发送数据的标志
    if 'speed=' in line and 'fps=' in line and 'frame=' in line:
        return True
        
    # RTMP连接成功的标志  
    if 'rtmp: connected' in line or 'connection successfully established' in line:
        return True
        
    # 成功打开输出的标志
    if 'output stream opened' in line:
        return True
        
    # 其他可能表示恢复的信息
    recovery_indicators = [
        'resuming',
        'reconnect successful',
        'connection restored'
    ]
    
    for indicator in recovery_indicators:
        if indicator in line:
            return True
            
    return False

def reset_task_network_status(task_id):
    """
    重置任务的网络状态相关参数
    
    Args:
        task_id: 任务ID
    """
    logger.info(f'重置任务 {task_id} 网络状态')
    
    with process_lock:
        if task_id in active_processes:
            # 重置网络监控相关状态
            active_processes[task_id]['network_issue_start_time'] = None
            active_processes[task_id]['in_buffer_period'] = False
            active_processes[task_id]['buffer_period_start'] = None
            active_processes[task_id]['previous_network_status'] = 'normal'
            active_processes[task_id]['network_recovered'] = False
            active_processes[task_id]['network_status'] = '正常'

def trigger_reconnect(task_id, reason=None):
    """触发任务重连流程"""
    logger.info(f'===== 启动外部重连机制 =====')
    # ...现有代码...
    
    # 检查任务是否在缓冲期内
    with process_lock:
        if task_id in active_processes:
            if active_processes[task_id].get('in_buffer_period', False):
                logger.info(f'任务 {task_id} 在缓冲期内，重连请求已记录但将延迟执行')
                # 仅记录请求，不立即执行
                active_processes[task_id]['pending_reconnect'] = True
                return True
    
    # 继续原有的重连逻辑...

# 其他函数保持不变... 