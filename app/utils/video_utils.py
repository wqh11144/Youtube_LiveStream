import subprocess
from pathlib import Path
import logging
from app.utils.file_utils import is_windows
import os
import time
import json
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import re
import platform
from app.core.logging import get_task_logger
from datetime import datetime
from app.services.task_service import beijing_tz
import pytz
from typing import Tuple, Callable, Dict, Any, Optional

logger = logging.getLogger('youtube_live')

def check_video_codec(video_path: Path) -> tuple[str, str]:
    """
    检查视频编码格式
    
    Args:
        video_path (Path): 视频文件路径
        
    Returns:
        tuple[str, str]: (视频编码, 音频编码)
        
    Raises:
        ValueError: 如果视频检测失败，提供详细的错误信息
    """
    # 首先确认文件是否存在
    if not Path(video_path).exists():
        error_msg = f"文件不存在: {video_path}"
        logger.error(error_msg)
        raise ValueError(error_msg)
        
    try:
        # 检查视频编码
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(video_path)
        ]
        video_codec = subprocess.check_output(cmd, text=True).strip()
        
        if not video_codec:
            # 如果没有返回视频编码，尝试获取更多信息
            logger.warning(f"未检测到视频流，尝试获取详细信息: {video_path}")
            probe_cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_format',
                '-show_streams',
                str(video_path)
            ]
            try:
                probe_output = subprocess.check_output(probe_cmd, text=True).strip()
                logger.info(f"视频文件详细信息:\n{probe_output}")
            except Exception as probe_error:
                logger.error(f"获取视频详细信息失败: {str(probe_error)}")
                
            raise ValueError(f"未检测到视频流，文件可能损坏或格式不支持: {video_path}")
        
        # 检查音频编码
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(video_path)
        ]
        audio_codec = subprocess.check_output(cmd, text=True).strip()
        
        # 检查结果
        if not audio_codec:
            logger.warning(f"未检测到音频流: {video_path}")
            audio_codec = "no_audio"
        
        logger.info(f"视频编码检测结果 - 文件: {video_path}, 视频编码: {video_codec}, 音频编码: {audio_codec}")
        return video_codec, audio_codec
        
    except subprocess.CalledProcessError as e:
        error_output = e.output.decode('utf-8', errors='replace') if hasattr(e, 'output') else "未知错误"
        error_msg = f"FFprobe检测视频编码失败: {error_output}, 返回码: {e.returncode}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    except Exception as e:
        error_msg = f"检查视频编码失败: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)

def is_rtmp_url(url):
    """判断是否是RTMP URL"""
    return url.lower().startswith(('rtmp://', 'rtmps://'))

def append_rtmp_params(rtmp_url):
    """为RTMP URL添加必要的参数以提高稳定性"""
    if not is_rtmp_url(rtmp_url):
        return rtmp_url
    
    # 解析URL
    parsed_url = urlparse(rtmp_url)
    
    # 提取查询参数
    query_params = parse_qs(parsed_url.query)
    
    # 添加或更新参数
    params_to_add = {
        'live': '1',
        'timeout': '10',
        'conn': ['S:OK', 'O:1'],  # 多个相同键的参数
        'bufferTime': '15'
    }
    
    # 合并参数，保留原有参数
    for key, value in params_to_add.items():
        if isinstance(value, list):
            # 如果是列表，表示同一个键可能有多个值
            if key not in query_params:
                query_params[key] = []
            query_params[key].extend(value)
        else:
            query_params[key] = [value]
    
    # 重构查询字符串
    # urlencode默认会将列表值转换为key=value1&key=value2格式
    query_string = urlencode(query_params, doseq=True)
    
    # 构建新的URL
    new_url = urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        parsed_url.params,
        query_string,
        parsed_url.fragment
    ))
    
    return new_url

def get_ffmpeg_command(input_file, output_rtmp, proxy_config=None, transcode=False, task_id=None):
    """
    构建FFmpeg命令行
    
    Args:
        input_file: 输入文件路径
        output_rtmp: 输出RTMP URL
        proxy_config: 代理配置信息 (dict 或 文件路径)
        transcode: 是否进行转码
        task_id: 任务ID，用于proxychains配置

    Returns:
        tuple: (命令列表, 环境变量字典)
    """
    # 设置基本命令
    command = []
    
    # 设置环境变量
    env = os.environ.copy()
    
    # 处理代理配置，确保每个任务使用独立的proxychains配置
    if proxy_config and task_id:
        # 将文件路径转换为代理配置字典
        if isinstance(proxy_config, str):
            try:
                with open(proxy_config, 'r') as f:
                    proxy_config = json.load(f)
                    logger.info(f"从文件加载代理配置: {proxy_config}")
            except Exception as e:
                logger.error(f"读取代理配置文件失败: {str(e)}")
                proxy_config = None
        
        if proxy_config and isinstance(proxy_config, dict):
            # 获取SOCKS5代理URL
            socks5_proxy = None
            if 'socks5_proxy' in proxy_config:
                socks5_proxy = proxy_config['socks5_proxy']
            elif 'socks5' in proxy_config:
                socks5_proxy = proxy_config['socks5']
            elif 'proxy' in proxy_config:
                proxy = proxy_config['proxy']
                if proxy and isinstance(proxy, str) and ('socks5://' in proxy.lower() or 'socks5h://' in proxy.lower()):
                    socks5_proxy = proxy
            
            if socks5_proxy:
                # 确保代理URL格式正确
                if not socks5_proxy.lower().startswith(('socks5://', 'socks5h://')):
                    if '@' in socks5_proxy:
                        socks5_proxy = f"socks5://{socks5_proxy}"
                    else:
                        socks5_proxy = f"socks5://{socks5_proxy}"
                
                logger.info(f"使用SOCKS5代理: {socks5_proxy}")
                
                # 为当前任务创建专用的proxychains配置文件
                from app.core.config import get_proxy_config_dir
                proxy_dir = get_proxy_config_dir()
                proxychains_config = proxy_dir / f"proxychains_{task_id}.conf"
                
                if not os.path.exists(proxychains_config):
                    # 如果任务特定的配置文件不存在，动态创建
                    proxychains_config = create_proxychains_config(proxy_config, task_id)
                    logger.info(f"为任务 {task_id} 创建专用proxychains配置: {proxychains_config}")
                else:
                    proxychains_config = str(proxychains_config)
                    logger.info(f"使用已存在的proxychains配置: {proxychains_config}")
                
                # 使用proxychains执行FFmpeg
                if proxychains_config:
                    command.extend(['proxychains4', '-f', proxychains_config])
                else:
                    # 如果无法创建配置文件，使用系统默认配置
                    logger.warning(f"无法创建任务专用配置，使用系统默认proxychains配置")
                    command.append('proxychains4')
                
                # 设置环境变量
                proxy_parts = socks5_proxy.replace('socks5://', '').split(':')
                proxy_host = proxy_parts[0].split('@')[-1]  # 获取主机部分
                proxy_port = proxy_parts[-1] if len(proxy_parts) > 1 else "1080"
                
                env['SOCKS5_SERVER'] = proxy_host
                env['SOCKS5_PORT'] = proxy_port
                logger.info(f"代理服务器: {proxy_host}:{proxy_port}")
            else:
                logger.warning(f"代理配置中未找到SOCKS5代理，将不使用代理")
    
    # 添加ffmpeg命令
    command.append('ffmpeg')
    command.extend(['-loglevel', 'warning', '-hide_banner', '-stream_loop', '-1', '-re'])
    
    # 设置输入文件
    command.extend(['-i', input_file])
    
    # 设置输出参数
    if transcode:
        # 使用转码设置
        command.extend([
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '1000k', 
            '-maxrate', '1000k', '-bufsize', '2000k',
            '-c:a', 'aac', '-b:a', '128k', 
            '-f', 'flv', output_rtmp
        ])
    else:
        # 不转码，直接复制流
        command.extend(['-c', 'copy', '-f', 'flv', output_rtmp])
    
    return command, env

def create_external_reconnect_function(video_path, rtmp_url, proxy_config_file=None, transcode_enabled=False, task_id=None):
    """
    创建一个可用于外部重连的函数
    
    参数:
        video_path (str): 视频文件路径
        rtmp_url (str): RTMP推流地址
        proxy_config_file (str, 可选): 代理配置文件路径
        transcode_enabled (bool, 可选): 是否启用转码
        task_id (str, 可选): 任务ID
        
    返回:
        callable: 重连函数
    """
    # 获取任务特定的日志记录器
    task_logger = get_task_logger(task_id or "unknown")
    
    # 记录重连信息 - 使用更简洁的格式
    task_logger.info(f"初始化重连功能 - 视频:{os.path.basename(video_path)}, RTMP目标:{rtmp_url.split('/')[-1]}")
    
    # 检查文件是否存在
    if not os.path.exists(video_path):
        task_logger.error(f"视频文件不存在: {video_path}")
        return lambda: None
    
    def reconnect_function():
        try:
            # 检查文件是否仍然存在
            if not os.path.exists(video_path):
                task_logger.error(f"视频文件不存在，无法重连: {video_path}")
                return None
            
            # 日志记录开始创建新重连进程 - 更简洁的格式
            task_logger.info(f"创建新推流 - 转码:{transcode_enabled}, 代理:{bool(proxy_config_file)}")
            
            # 检查文件大小
            try:
                file_size = os.path.getsize(video_path)
                file_size_mb = file_size / 1024 / 1024
                task_logger.info(f"视频大小: {file_size_mb:.2f}MB")
            except Exception as e:
                task_logger.warning(f"无法获取视频文件大小: {str(e)}")
            
            # 加载代理配置 - 针对当前任务
            proxy_config = None
            if proxy_config_file and task_id:
                try:
                    if os.path.exists(proxy_config_file):
                        with open(proxy_config_file, 'r') as f:
                            proxy_config = json.load(f)
                        # 仅记录代理的URL而不是完整配置
                        if proxy_config and "socks5_proxy" in proxy_config:
                            task_logger.info(f"使用代理: {proxy_config['socks5_proxy']}")
                            
                            # 为当前任务创建专用的proxychains配置文件
                            proxychains_config = create_proxychains_config(proxy_config, task_id)
                            task_logger.info(f"已为任务 {task_id} 创建专用proxychains配置: {proxychains_config}")
                    else:
                        task_logger.warning(f"代理配置文件不存在，使用默认代理配置")
                        # 创建任务默认代理配置
                        proxy_config = {
                            "socks5_proxy": "socks5://127.0.0.1:1080",  # 默认本地代理
                            "created_at": datetime.now(beijing_tz).isoformat(),
                            "task_id": task_id,
                            "note": f"任务 {task_id} 的默认代理配置"
                        }
                        # 为当前任务创建专用proxychains配置
                        proxychains_config = create_proxychains_config(proxy_config, task_id)
                        task_logger.info(f"已为任务 {task_id} 创建默认proxychains配置: {proxychains_config}")
                except Exception as e:
                    task_logger.error(f"为任务 {task_id} 加载代理配置失败: {str(e)}")
                    # 创建默认代理配置
                    proxy_config = {
                        "socks5_proxy": "socks5://127.0.0.1:1080",  # 默认本地代理
                        "created_at": datetime.now(beijing_tz).isoformat(),
                        "task_id": task_id,
                        "note": f"任务 {task_id} 的故障恢复代理配置"
                    }
            
            # 获取FFmpeg命令
            ffmpeg_cmd, env = get_ffmpeg_command(
                input_file=video_path,
                output_rtmp=rtmp_url,
                proxy_config=proxy_config,
                transcode=transcode_enabled,
                task_id=task_id
            )
            
            # 简化命令记录，只记录必要参数
            task_logger.info(f"FFmpeg输入:{os.path.basename(video_path)}, 输出:RTMP")
            task_logger.info(f"完整命令: {' '.join(ffmpeg_cmd)}")
            
            # 启动前记录时间
            start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            task_logger.info(f"开始时间: {start_time}")
            
            # 启动进程
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                errors='replace'
            )
            
            if process:
                pid = process.pid
                task_logger.info(f"推流进程已启动, PID: {pid}")
                
                # 检查进程是否立即退出
                if process.poll() is not None:
                    returncode = process.poll()
                    task_logger.error(f"推流进程立即退出，返回码: {returncode}")
                    
                    # 获取错误输出
                    try:
                        stderr_output = process.stderr.read() if process.stderr else ""
                        
                        # 过滤输出，只记录重要错误信息
                        filter_patterns = ffmpeg_filter_patterns()
                        important_keywords = ['error', 'fail', 'warning', 'unable', 'cannot', 'missing', 'invalid', 'rtmp', 'connect']
                        
                        important_lines = []
                        for line in stderr_output.splitlines():
                            # 检查是否包含重要关键词
                            contains_important = any(keyword in line.lower() for keyword in important_keywords)
                            
                            # 检查是否匹配任何过滤模式
                            should_filter = not contains_important and any(pattern.search(line) for pattern in filter_patterns)
                            
                            # 不应被过滤的行才保留
                            if not should_filter:
                                important_lines.append(line)
                        
                        # 只记录重要的错误信息
                        if important_lines:
                            filtered_output = "\n".join(important_lines[:15]) # 最多显示15行
                            task_logger.error(f"进程错误输出 (已过滤):\n{filtered_output}")
                            
                            # 分析错误原因
                            stderr_text = " ".join(important_lines).lower()
                            if 'connection refused' in stderr_text:
                                task_logger.error("错误原因分析: 目标服务器拒绝连接")
                            elif 'timeout' in stderr_text:
                                task_logger.error("错误原因分析: 连接超时")
                            elif 'no such file or directory' in stderr_text:
                                task_logger.error("错误原因分析: 找不到文件或目录")
                            elif 'permission denied' in stderr_text:
                                task_logger.error("错误原因分析: 权限被拒绝")
                            elif 'invalid data found' in stderr_text:
                                task_logger.error("错误原因分析: 视频文件可能损坏")
                        else:
                            # 如果没有重要行，记录最后的10行作为上下文
                            fallback_lines = stderr_output.splitlines()[-10:]
                            task_logger.error("未找到重要错误，最后10行输出:\n" + "\n".join(fallback_lines))
                    except Exception as e:
                        task_logger.error(f"无法读取错误输出: {str(e)}")
                    
                    return None
                else:
                    return process
            else:
                task_logger.error(f"创建重连进程失败")
                return None
                
        except Exception as e:
            error_msg = f"启动重连进程失败: {str(e)}"
            task_logger.error(error_msg)
            
            # 记录堆栈跟踪信息
            import traceback
            stack_trace = traceback.format_exc()
            task_logger.error(f"异常堆栈:\n{stack_trace}")
            return None
            
    return reconnect_function

def monitor_and_reconnect(process, task_id, reconnect_function, retry_delay=3, max_retries=5, total_reconnects=0):
    """监控进程并在断线时自动重连"""
    logger = logging.getLogger('youtube_live')
    
    # 获取任务特定的日志记录器
    task_logger = get_task_logger(task_id)
    
    # 记录重连开始
    task_logger.info(f"===== 开始重连过程 =====")
    task_logger.info(f"重连函数: {reconnect_function.__name__ if hasattr(reconnect_function, '__name__') else '匿名函数'}")
    task_logger.info(f"最大重试次数: {max_retries}")
    task_logger.info(f"当前累计重连次数: {total_reconnects}")
    
    # 记录重连尝试
    retry_count = 0
    new_process = None
    
    while retry_count < max_retries:
        # 增加重试次数
        retry_count += 1
        total_reconnects += 1
        
        # 记录重试信息
        task_logger.info(f"重连尝试 {retry_count}/{max_retries} (累计重连次数: {total_reconnects})")
        
        # 如果需要延迟，先等待
        if retry_delay > 0:
            task_logger.info(f"等待 {retry_delay} 秒后尝试重连...")
            time.sleep(retry_delay)
        
        # 尝试重连
        try:
            task_logger.info(f"执行重连函数...")
            # 调用重连函数创建新进程
            new_process = reconnect_function()
            
            if new_process:
                pid = new_process.pid
                task_logger.info(f"重连成功 ✓ - 新进程PID: {pid}")
                logger.info(f"[task_id={task_id}] 重连成功 - 新进程PID: {pid}")
                break
            else:
                task_logger.error(f"重连失败 - 重连函数未能创建有效进程 ✗")
                
                # 增加重试延迟 - 指数退避策略
                retry_delay = min(30, retry_delay * 2 if retry_delay > 0 else 3)
                task_logger.info(f"下次重试将在 {retry_delay} 秒后进行")
                
        except Exception as e:
            # 记录异常信息
            import traceback
            stack_trace = traceback.format_exc()
            task_logger.error(f"重连过程中发生异常: {str(e)}\n{stack_trace}")
            logger.error(f"[task_id={task_id}] 重连过程中发生异常: {str(e)}")
            
            # 增加重试延迟 - 指数退避策略
            retry_delay = min(30, retry_delay * 2 if retry_delay > 0 else 3)
            task_logger.info(f"下次重试将在 {retry_delay} 秒后进行")
    
    # 判断重连结果
    if not new_process:
        task_logger.error(f"达到最大重试次数，重连失败 ✗")
        logger.error(f"[task_id={task_id}] 达到最大重试次数 {max_retries}，重连失败")
        return None, total_reconnects
    
    # 记录重连结束信息
    task_logger.info(f"===== 重连过程成功完成 =====")
    
    # 返回新进程和累计重连次数
    return new_process, total_reconnects

# 增强重连关键词，包含更多RTMP特定错误
reconnect_keywords = [
    'broken pipe', 
    'connection reset', 
    'timeout', 
    'timed out', 
    'refused',
    'av_interleaved_write_frame()',  # RTMP写入失败
    'error writing trailer',  # RTMP流关闭时出错
    'error closing file',  # 结束推流时出错
    'end of file',  # 文件结束（可能是网络断开）
    'server disconnect',  # 服务器主动断开
    'failed to connect',  # 连接失败
    'could not write'  # 写入失败
]

def check_video_permissions(video_path: Path) -> tuple[bool, str]:
    """
    检查视频文件权限问题
    
    Args:
        video_path (Path): 视频文件路径
        
    Returns:
        tuple[bool, str]: (权限是否正常, 错误信息)
    """
    # 首先确认文件是否存在
    if not Path(video_path).exists():
        return False, f"文件不存在: {video_path}"
    
    # 尝试获取文件大小
    try:
        file_size = os.path.getsize(video_path)
        if file_size == 0:
            return False, f"文件大小为0，可能是空文件: {video_path}"
            
        # 尝试读取文件开头一小部分
        with open(video_path, 'rb') as f:
            try:
                header = f.read(1024)  # 读取前1KB
                if not header:
                    return False, f"无法读取文件内容，文件可能损坏: {video_path}"
            except Exception as e:
                return False, f"读取文件内容时出错: {str(e)}"
                
        return True, f"文件权限正常，大小: {file_size} 字节"
    except PermissionError:
        return False, f"没有足够权限访问文件: {video_path}"
    except Exception as e:
        return False, f"检查文件权限时出错: {str(e)}"

def ffmpeg_filter_patterns():
    """
    返回用于过滤FFmpeg输出的正则表达式模式列表
    
    这些模式用于识别并过滤FFmpeg输出中的不重要信息，如进度条、内部调试等
    
    返回:
        list: 正则表达式模式列表
    """
    # 定义重要关键词，这些关键词出现的消息不会被过滤掉
    important_keywords = ['error', 'fail', 'warn', 'cannot', 'invalid', 'unable', 'timeout']
    
    return [
        # 进度信息
        re.compile(r'^\s*frame=\s*\d+\s+fps=\s*\d+.*?'),
        # 打开文件或URL的详细信息
        re.compile(r'^\s*Opening.*?for (?:reading|writing)'),
        # 延迟统计信息
        re.compile(r'^\s*Last message repeated \d+ times'),
        # 内部音频参数
        re.compile(r'^\s*tb:(?:\d+)/(?:\d+) '),
        # 详细调试信息，但不包含错误
        re.compile(r'^\s*debug(?!.*?error).*?:'),
        # 媒体格式详情
        re.compile(r'^\s*Metadata:'),
        # FFmpeg版本信息
        re.compile(r'^\s*configuration:'),
        # 库版本信息
        re.compile(r'^\s*lib(?:av|sw|x).*?:'),
        # 隐私信息
        re.compile(r'^\s*built with '),
        # 频道和布局信息
        re.compile(r'^\s*channel layout:'),
        # 通知无错误消息
        re.compile(r'^\s*Skipping '),
        # 音频重采样信息
        re.compile(r'^\s*audio:.*?Hz'),
        # 常见的时钟和时间戳信息
        re.compile(r'^\s*clock:'),
        # 常见的FFmpeg调试前缀，但不包含错误
        re.compile(r'^\s*\[(info|debug)\](?!.*?error)'),
        # fps信息
        re.compile(r'^\s*fps='),
        # 进度指示
        re.compile(r'^\s*size='),
        # 无实际内容的行
        re.compile(r'^\s*$'),
        # Stream映射信息
        re.compile(r'^\s*Stream mapping:'),
        # Stream #信息，但不包含错误或警告
        re.compile(r'^\s*Stream #\d+:(?!.*?(error|warning))'),
        # 缓冲区信息
        re.compile(r'^\s*buffer:'),
        # 常规输入/输出信息
        re.compile(r'^\s*Input #\d+,'),
        re.compile(r'^\s*Output #\d+,'),
        # Timestamp相关
        re.compile(r'^\s*timestamp:'),
        # 内部编码器信息
        re.compile(r'^\s*encoder:'),
        # 解码器信息
        re.compile(r'^\s*decoder:'),
        # I/P/B帧统计
        re.compile(r'^\s*[ipb]?frames:'),
        
        # 过滤地址和端口信息
        re.compile(r'^\s*\[\w+ @ 0x[0-9a-f]+\] Address .* port \d+'),
        # ct_type和pic_struct信息
        re.compile(r'^\s*\[\w+ @ 0x[0-9a-f]+\] ct_type:\d+ pic_struct:\d+'),
        # 重复消息提示
        re.compile(r'^\s*Last message repeated \d+ times'),
        
        # 新增：过滤带 [component @ address] 格式的所有消息，除非包含重要关键词
        # 先匹配标准格式
        re.compile(r'^\s*\[\w+ @ 0x[0-9a-f]+\](?!.*?(' + '|'.join(important_keywords) + '))'),
        
        # 新增：特别过滤 flv 相关的但不含错误的消息
        re.compile(r'^\s*\[flv @ 0x[0-9a-f]+\](?!.*?(' + '|'.join(important_keywords) + '))'),
        
        # avio操作信息
        re.compile(r'^\s*avio_.*:'),
        # 解码参数信息
        re.compile(r'^\s*(?:color|pixel|bits|aspect|duration).*:'),
        # TCP/UDP网络详细信息，但不包括RTMP
        re.compile(r'^\s*\[(?:tcp|udp)(?!.*?rtmp).*\].*(?!error|fail|timeout)'),
        # 一般分析/处理信息
        re.compile(r'^\s*(?:analyzing|analyzing input|this may result)'),
        # 解析信息
        re.compile(r'^\s*(?:parsed)'),
        # flv格式特定信息，但不含rtmp和错误
        re.compile(r'^\s*(?:flv|hls)(?!.*?(rtmp|' + '|'.join(important_keywords) + ')):'),
        # 队列和缓冲区操作
        re.compile(r'^\s*(?:queue|buffer|dropping)'),
        # AVOptions信息
        re.compile(r'^\s*\[AVOption'),
        # 日期相关信息
        re.compile(r'^\s*date:'),
        # 注释信息
        re.compile(r'^\s*comment:'),
        # 版权信息
        re.compile(r'^\s*copyright:'),
        # 时区信息
        re.compile(r'^\s*(?:timezone|creation_time|time):'),
        # 处理信息
        re.compile(r'^\s*(?:Processing|Processed):'),
        # 协议详情，但不包含RTMP和错误
        re.compile(r'^\s*(?:Protocol)(?!.*?(rtmp|' + '|'.join(important_keywords) + ')):'),
        # 进度通知
        re.compile(r'^\s*(?:progress):'),
        # 通用常量信息
        re.compile(r'^\s*(?:constant):'),
        # 调试堆栈信息
        re.compile(r'^\s*(?:0x[0-9a-f]+\b)'),
        # 通用日志前缀但不含错误信息和RTMP相关信息
        re.compile(r'^\s*\[[^\]]+\](?!.*?(error|fail|unable|warn|cannot|invalid|rtmp|connect))')
    ]

def validate_video_file(file_path):
    """
    简化版的视频文件验证，只检查文件是否存在以及必须是h264编码
    
    参数:
        file_path (str): 视频文件路径
        
    返回:
        tuple: (is_valid, error_message)
    """
    # 检查文件是否存在
    if not os.path.exists(file_path):
        return False, f"文件不存在: {file_path}"
        
    # 检查文件大小
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return False, f"文件大小为0字节: {file_path}"
        
        # 记录文件大小，便于调试
        logger.info(f"视频文件大小: {file_size} 字节")
    except Exception as e:
        return False, f"获取文件大小失败: {str(e)}"
    
    # 使用FFprobe检查视频编码（比FFmpeg更轻量）
    try:
        # 检查视频编码命令
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(file_path)
        ]
        
        # 执行命令
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        # 检查是否成功运行
        if result.returncode != 0:
            error_output = result.stderr.strip()
            if error_output:
                return False, f"视频文件检查失败: {error_output}"
            else:
                return False, "视频文件检查失败: FFprobe返回非零状态码"
        
        # 获取编码信息
        codec = result.stdout.strip()
        
        # 如果没有获取到编码，可能没有视频流
        if not codec:
            return False, "文件中没有检测到视频流"
        
        # 记录编码信息
        logger.info(f"视频文件 {file_path} 编码: {codec}")
        
        # 强制要求必须是h264编码
        if codec.lower() != 'h264':
            logger.error(f"视频编码必须是h264，当前编码: {codec}")
            return False, f"视频编码必须是h264，当前编码: {codec}"
        
        # 视频文件验证通过
        return True, "视频文件验证通过"
        
    except subprocess.TimeoutExpired:
        return False, "验证视频文件超时"
    except Exception as e:
        return False, f"验证视频文件时出错: {str(e)}"

def is_windows():
    """判断当前系统是否是Windows"""
    return platform.system().lower() == 'windows' 

def create_proxychains_config(proxy_config, task_id):
    """
    为task动态创建proxychains配置文件
    
    Args:
        proxy_config (dict): 代理配置信息
        task_id (str): 任务ID
    
    Returns:
        str: proxychains配置文件路径
    """
    try:
        from app.core.config import get_proxy_config_dir
        
        # 确保代理配置目录存在
        proxy_dir = get_proxy_config_dir()
        os.makedirs(proxy_dir, exist_ok=True)
        
        # 为特定任务创建专用配置文件
        config_file = proxy_dir / f"proxychains_{task_id}.conf"
        
        # 从代理URL中提取信息
        socks5_url = proxy_config.get("socks5_proxy", "socks5://127.0.0.1:1080")
        socks5_url = socks5_url.replace("socks5://", "").replace("socks5h://", "")
        
        # 分离用户名密码和主机端口
        if "@" in socks5_url:
            auth, hostport = socks5_url.split("@", 1)
            if ":" in auth:
                username, password = auth.split(":", 1)
            else:
                username, password = auth, ""
        else:
            username, password = "", ""
            hostport = socks5_url
        
        # 分离主机和端口
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "1080"
        
        # 创建配置文件内容
        config_content = f"""# proxychains.conf 为任务 {task_id} 专用配置
# 创建时间: {datetime.now(beijing_tz).isoformat()}

# 基本设置 - 独立任务配置
strict_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
# 任务 {task_id} 的专用代理配置
"""
        
        # 根据是否有用户名密码添加不同的配置行
        if username and password:
            config_content += f"socks5 {host} {port} {username} {password}\n"
        else:
            config_content += f"socks5 {host} {port}\n"
        
        # 写入配置文件
        with open(config_file, "w") as f:
            f.write(config_content)
        
        # 设置权限
        try:
            os.chmod(config_file, 0o644)
        except Exception as e:
            logger.warning(f"设置文件权限失败: {str(e)}")
        
        logger.info(f"已为任务 {task_id} 创建专用proxychains配置: {config_file}")
        return str(config_file)
        
    except Exception as e:
        logger.error(f"为任务 {task_id} 创建proxychains配置文件失败: {str(e)}")
        return None 