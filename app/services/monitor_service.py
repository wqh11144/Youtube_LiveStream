import re
import subprocess
import threading
import logging
import time
import psutil
from app.utils.network_utils import extract_host_from_rtmp, validate_rtmp_url
import pytz
from typing import Tuple, Dict, Any, Optional
from datetime import datetime
import platform
import os

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')

logger = logging.getLogger('youtube_live')

# 定义事件类型
NETWORK_CRITICAL = "network_critical"  # 网络严重问题，需要立即重连
NETWORK_WARNING = "network_warning"    # 网络警告，需要观察
NETWORK_RECOVERY = "network_recovery"  # 网络恢复正常

class MonitorResult:
    """监控结果数据类"""
    def __init__(self, host: str, status: str, ping_ms: float = 0, 
                 packet_loss: int = 0, event_type: Optional[str] = None,
                 affected_tasks: list = None):
        self.host = host
        self.status = status  # 'normal', 'warning', 'critical', 'error'
        self.ping_ms = ping_ms
        self.packet_loss = packet_loss
        self.event_type = event_type
        self.affected_tasks = affected_tasks or []
        self.timestamp = datetime.now(beijing_tz)
        
    def __str__(self):
        return (f"MonitorResult(host={self.host}, status={self.status}, "
                f"ping={self.ping_ms}ms, loss={self.packet_loss}%, "
                f"event={self.event_type}, tasks={len(self.affected_tasks)})")

# 导入延迟到函数内部，避免循环依赖问题
def get_active_processes():
    """获取活动进程列表，解决循环导入问题"""
    from app.services.stream_service import active_processes, process_lock
    return active_processes, process_lock

def trigger_reconnect(task_id: str, reason: str) -> bool:
    """触发任务重连，解决循环导入问题"""
    from app.services.stream_service import trigger_task_reconnect
    return trigger_task_reconnect(task_id, reason)

def monitor_rtmp_connection(host: str, task_ids: list) -> MonitorResult:
    """监控单个RTMP服务器的连接质量并返回监控结果"""
    try:
        # 执行ping测试
        result = subprocess.run(['ping', '-c', '3', host], 
                               capture_output=True, text=True, timeout=5)
        ping_output = result.stdout
        
        # 默认值
        avg_ping = 0
        packet_loss = 0
        status = "normal"
        event_type = None
        
        if result.returncode != 0:
            status = 'critical'
            event_type = NETWORK_CRITICAL
            logger.warning(f'RTMP服务器连接不稳定 - host={host}, 影响 {len(task_ids)} 个任务')
        else:
            # 分析ping结果
            try:
                # 提取丢包率
                loss_match = re.search(r'(\d+)% packet loss', ping_output)
                if loss_match:
                    packet_loss = int(loss_match.group(1))
                
                # 提取平均延迟
                avg_match = re.search(r'min/avg/max/mdev = [\d.]+/([\d.]+)', ping_output)
                if avg_match:
                    avg_ping = float(avg_match.group(1))
            except Exception as e:
                logger.error(f"分析ping结果失败: {str(e)}")
            
            # 根据延迟和丢包率评估连接质量
            if packet_loss > 20 or avg_ping > 300:
                status = 'critical'
                event_type = NETWORK_CRITICAL
                logger.warning(f'RTMP连接质量严重不佳 - host={host}, 平均延迟={avg_ping}ms, '
                             f'丢包率={packet_loss}%, 影响 {len(task_ids)} 个任务')
            elif packet_loss > 10 or avg_ping > 200:
                status = 'warning'
                event_type = NETWORK_WARNING
                logger.warning(f'RTMP连接质量不佳 - host={host}, 平均延迟={avg_ping}ms, '
                             f'丢包率={packet_loss}%, 影响 {len(task_ids)} 个任务')
            else:
                status = 'normal'
                logger.info(f'RTMP连接质量良好 - host={host}, 平均延迟={avg_ping}ms, 丢包率={packet_loss}%')
        
        return MonitorResult(
            host=host,
            status=status,
            ping_ms=avg_ping,
            packet_loss=packet_loss,
            event_type=event_type,
            affected_tasks=task_ids
        )
        
    except Exception as e:
        logger.error(f'检查主机 {host} 的连接质量失败: {str(e)}')
        return MonitorResult(
            host=host,
            status='error',
            event_type=NETWORK_CRITICAL,
            affected_tasks=task_ids
        )

def monitor_all_rtmp_connections():
    """监控所有活动任务的RTMP连接质量并根据严重程度触发主动重连"""
    try:
        # 获取活动进程
        active_processes, process_lock = get_active_processes()
        
        # 定义用于收集任务组的字典
        hosts_to_check = {}  # {host: [task_ids]}
    
        # 获取所有活动任务
        with process_lock:
            if not active_processes:
                return  # 没有活动任务，直接返回
            
            # 检查每个任务的连接状态和异常情况
            for task_id, info in list(active_processes.items()):
                process = info['process']
                # 检查进程是否还在运行
                is_running = process.poll() is None
                
                if not is_running:
                    # 检查是否在缓冲期中
                    if info.get('in_buffer_period', False):
                        logger.info(f'任务 {task_id} 在缓冲期内，暂不处理')
                        continue
                        
                    # 检查进程是否僵尸状态（无法响应但仍占用PID）
                    try:
                        import psutil
                        try:
                            proc = psutil.Process(process.pid)
                            proc_status = proc.status()
                            if proc_status == psutil.STATUS_ZOMBIE:
                                logger.warning(f'检测到僵尸进程 - task_id={task_id}, pid={process.pid}')
                                # 先尝试发送SIGTERM信号
                                proc.terminate()
                                try:
                                    proc.wait(timeout=5)
                                except:
                                    # 如果SIGTERM无效，使用SIGKILL强制结束
                                    proc.kill()
                                logger.info(f'已终止僵尸进程 - task_id={task_id}, pid={process.pid}')
                                
                                # 更新任务状态
                                from app.services.task_service import update_task_status
                                update_task_status(task_id, {
                                    'status': 'error',
                                    'message': '任务已自动终止（僵尸进程）',
                                    'end_time': datetime.now(beijing_tz).isoformat()
                                })
                                
                                # 从活动任务列表中移除
                                del active_processes[task_id]
                                continue
                        except psutil.NoSuchProcess:
                            logger.warning(f'进程不存在但仍在活动列表中 - task_id={task_id}, pid={process.pid}')
                            # 从活动任务列表中移除
                            del active_processes[task_id]
                            continue
                    except Exception as e:
                        logger.error(f'检查进程状态失败 - task_id={task_id}: {str(e)}')
                    
                    # 检查卡住的进程 - 如果超过1分钟没有输出，且进程仍在运行，直接终止进程
                    last_activity = info.get('last_activity_time')
                    if last_activity:
                        now = datetime.now(beijing_tz)
                        inactive_seconds = (now - last_activity).total_seconds()
                        if inactive_seconds > 60:  # 1分钟无活动
                            logger.warning(f'任务超过1分钟无活动 - task_id={task_id}, 直接终止进程')
                            
                            # 获取任务日志记录器
                            from app.core.logging import get_task_logger
                            task_logger = get_task_logger(task_id)
                            task_logger.warning(f'监控检测到进程超过1分钟无活动，主动终止')
                            
                            try:
                                # 尝试优雅终止
                                if platform.system() == 'Windows':
                                    process.communicate(input=b'q', timeout=3)
                                else:
                                    import signal
                                    os.kill(process.pid, signal.SIGTERM)
                                
                                # 等待进程结束
                                try:
                                    process.wait(timeout=5)
                                    logger.info(f'已优雅终止卡住的进程 - task_id={task_id}')
                                except:
                                    # 如果优雅终止失败，强制终止
                                    process.kill()
                                    logger.warning(f'强制终止卡住的进程 - task_id={task_id}')
                            except Exception as term_err:
                                logger.error(f'终止卡住进程失败 - task_id={task_id}: {str(term_err)}')
                                try:
                                    # 最后尝试使用psutil强制终止
                                    psutil.Process(process.pid).kill()
                                except:
                                    pass
                            
                            # 更新任务状态
                            from app.services.task_service import update_task_status
                            update_task_status(task_id, {
                                'status': 'error',
                                'message': '任务已自动终止（进程卡住超过1分钟无输出）',
                                'end_time': datetime.now(beijing_tz).isoformat()
                            })
                            
                            # 从活动任务列表中移除
                            del active_processes[task_id]
                            continue
                    
                    # 检查是否正在验证重连或已在缓冲期内
                    if info.get('verifying_reconnection', False) or info.get('in_buffer_period', False):
                        continue  # 跳过验证中的任务，避免干扰
                        
                    # 如果任务已经在缓冲期内，则跳过网络监控触发的重连
                    if info.get('in_buffer_period', False):
                        # 检查缓冲期是否刚开始(不到5秒)
                        buffer_start = info.get('buffer_period_start')
                        if buffer_start:
                            now = datetime.now(beijing_tz)
                            buffer_seconds = (now - buffer_start).total_seconds()
                            # 如果缓冲期刚开始不久，完全跳过
                            if buffer_seconds < 5:
                                logger.info(f'任务 {task_id} 正在缓冲期初期({buffer_seconds:.1f}秒)，暂不进行网络监控')
                                continue
                    
                    # 按照主机名分组任务
                    rtmp_url = info.get('rtmp_url', '')
                    host = extract_host_from_rtmp(rtmp_url)
                    
                    if not host:
                        continue
                        
                    if host not in hosts_to_check:
                        hosts_to_check[host] = []
                        
                    hosts_to_check[host].append(task_id)
        
        # 没有需要检查的主机
        if not hosts_to_check:
            return
            
        logger.debug(f'开始检查 {len(hosts_to_check)} 个RTMP服务器的连接质量')
        
        # 检查每个主机并处理监控结果
        for host, task_ids in hosts_to_check.items():
            monitor_result = monitor_rtmp_connection(host, task_ids)
            
            # 更新所有受影响任务的网络状态
            with process_lock:
                for task_id in task_ids:
                    if task_id in active_processes:
                        # 构建网络状态描述
                        if monitor_result.status == 'normal':
                            network_status = f'良好 (延迟:{monitor_result.ping_ms:.1f}ms, 丢包:{monitor_result.packet_loss}%)'
                        elif monitor_result.status == 'warning':
                            network_status = f'不佳 (延迟:{monitor_result.ping_ms:.1f}ms, 丢包:{monitor_result.packet_loss}%)'
                        elif monitor_result.status == 'critical':
                            network_status = f'严重不佳 (延迟:{monitor_result.ping_ms:.1f}ms, 丢包:{monitor_result.packet_loss}%)'
                        else:
                            network_status = f'错误 (无法连接)'
                        
                        # 更新网络状态
                        active_processes[task_id]['network_status'] = network_status
                        
                        # 如果之前有网络警告状态，现在变为正常，标记为恢复
                        previous_status = active_processes[task_id].get('previous_network_status')
                        if previous_status in ['warning', 'critical'] and monitor_result.status == 'normal':
                            active_processes[task_id]['network_recovered'] = True
                            logger.info(f'任务 {task_id} 的网络连接已恢复正常')
                        
                        # 保存当前状态为之前状态，用于下次比较
                        active_processes[task_id]['previous_network_status'] = monitor_result.status
                        
                        # 对于严重网络问题，添加缓冲期逻辑
                        if monitor_result.event_type == NETWORK_CRITICAL:
                            # 检查是否已经在缓冲期或正在重连
                            if active_processes[task_id].get('in_buffer_period') or active_processes[task_id].get('reconnecting'):
                                logger.info(f'任务 {task_id} 已在缓冲期或重连中，跳过网络监控触发')
                                continue
                                
                            # 检查是否已经标记了网络问题开始时间
                            if not active_processes[task_id].get('network_issue_start_time'):
                                # 首次发现问题，记录开始时间
                                active_processes[task_id]['network_issue_start_time'] = datetime.now(beijing_tz)
                                logger.warning(f'任务 {task_id} 发现严重网络问题，开始缓冲期监控')
                            else:
                                # 已经在缓冲期，检查是否超过最大缓冲时间(10秒)
                                issue_start = active_processes[task_id]['network_issue_start_time']
                                now = datetime.now(beijing_tz)
                                buffer_seconds = (now - issue_start).total_seconds()
                                
                                if buffer_seconds > 10:
                                    # 超过缓冲期，问题仍存在，触发重连
                                    logger.warning(f'任务 {task_id} 网络问题持续超过10秒 ({buffer_seconds:.1f}秒)，触发重连')
                                    
                                    # 重置问题计时器
                                    active_processes[task_id]['network_issue_start_time'] = None
                                    
                                    # 使用线程执行重连，避免阻塞监控循环
                                    threading.Thread(
                                        target=trigger_reconnect,
                                        args=(task_id, f"监控检测到持续10秒以上的严重网络问题 - 丢包率:{monitor_result.packet_loss}%, 延迟:{monitor_result.ping_ms}ms"),
                                        daemon=True
                                    ).start()
                                else:
                                    # 在缓冲期内，继续观察
                                    logger.info(f'任务 {task_id} 网络问题仍在缓冲期内 ({buffer_seconds:.1f}秒/10秒)')
                        elif active_processes[task_id].get('network_issue_start_time'):
                            # 网络已恢复，清除问题开始时间
                            logger.info(f'任务 {task_id} 网络问题已自行恢复，取消重连')
                            active_processes[task_id]['network_issue_start_time'] = None
            
    except Exception as e:
        logger.error(f'全局网络质量监控失败: {str(e)}')

class ResourceMonitor:
    """系统资源监控类"""
    
    def __init__(self):
        self.running = False
        self.monitoring_interval = 30  # 监控间隔（秒）
        self.system_monitor_thread = None
        self.network_check_count = 0  # 网络检查计数器
    
    def start_monitoring(self):
        """启动监控服务"""
        if self.running:
            logger.warning("监控服务已在运行")
            return
            
        self.running = True
        
        # 启动监控线程
        self.system_monitor_thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        self.system_monitor_thread.start()
        
        logger.info("启动资源监控服务")
    
    def stop_monitoring(self):
        """停止监控服务"""
        if not self.running:
            logger.warning("监控服务未在运行")
            return
            
        self.running = False
        logger.info("停止资源监控服务")
        
        # 停止系统监控线程
        if self.system_monitor_thread and self.system_monitor_thread.is_alive():
            self.system_monitor_thread.join(timeout=5)
            logger.info("系统监控线程已停止")
    
    def run(self):
        """资源监控主循环"""
        try:
            logger.info("资源监控线程已启动")
            
            while self.running:
                try:
                    # 监控CPU和内存使用率
                    cpu_percent = psutil.cpu_percent(interval=1)
                    memory_info = psutil.virtual_memory()
                    
                    # 当CPU或内存使用率过高时记录警告
                    if cpu_percent > 80:
                        logger.warning(f"CPU使用率过高: {cpu_percent}%")
                        
                    if memory_info.percent > 85:
                        logger.warning(f"内存使用率过高: {memory_info.percent}%")
                        
                    # 仅在日志级别为DEBUG时才记录常规资源信息
                    if logger.level <= logging.DEBUG:
                        logger.debug(f"系统资源使用情况 - CPU: {cpu_percent}%, 内存: {memory_info.percent}%")
                    
                    # 检查并清理可能存在的僵尸进程
                    for proc in psutil.process_iter(['pid', 'name', 'status']):
                        try:
                            if proc.info['status'] == psutil.STATUS_ZOMBIE and proc.info['name'] == 'ffmpeg':
                                logger.warning(f"检测到僵尸进程: PID={proc.info['pid']}, 名称={proc.info['name']}")
                                # 不主动终止僵尸进程，只记录日志
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    
                    # 网络检查计数器增加
                    self.network_check_count += 1
                    
                    # 每一次循环都检查网络状态
                    monitor_all_rtmp_connections()
                    
                except Exception as e:
                    logger.error(f"资源监控过程中发生错误: {str(e)}")
                
                # 监控间隔
                time.sleep(self.monitoring_interval)
                
        except Exception as e:
            logger.error(f"资源监控线程异常退出: {str(e)}")
        finally:
            logger.info("资源监控线程已结束") 