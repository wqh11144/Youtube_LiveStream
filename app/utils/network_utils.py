import socket
import subprocess
import re
import urllib.parse
import logging

logger = logging.getLogger('youtube_live')

def validate_rtmp_url(rtmp_url: str) -> tuple[bool, str]:
    """
    验证RTMP URL的格式和基本连通性
    
    Args:
        rtmp_url: RTMP URL字符串
        
    Returns:
        tuple[bool, str]: (是否有效, 错误信息)
    """
    try:
        # 1. 基本格式验证
        if not rtmp_url.startswith(('rtmp://', 'rtmps://')):
            return False, "RTMP URL必须以 rtmp:// 或 rtmps:// 开头"
            
        # 2. URL格式解析
        parsed = urllib.parse.urlparse(rtmp_url)
        if not parsed.netloc:
            return False, "RTMP URL格式无效"
            
        # 3. 验证YouTube特定格式
        if 'youtube.com' in parsed.netloc or 'youtu.be' in parsed.netloc:
            # 验证流密钥格式
            path_parts = parsed.path.split('/')
            if len(path_parts) < 3 or not path_parts[-1]:
                return False, "YouTube流密钥格式无效，应为'live2/xxxx-xxxx-xxxx-xxxx-xxxx'"
                
            # 验证典型的YouTube流密钥格式 (四个或五个由'-'分隔的部分)
            stream_key = path_parts[-1]
            if not re.match(r'^[\w\-]{4,6}(-[\w\-]{4,6}){3,4}$', stream_key):
                return False, "YouTube流密钥格式可能有误，请确认是否完整复制"
            
        # 4. 提取主机名和端口
        host = parsed.hostname
        port = parsed.port or 1935  # 默认RTMP端口
        
        # 5. 测试TCP连接
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            return True, "连接成功"
        except socket.timeout:
            return False, f"连接超时: {host}:{port}"
        except socket.gaierror:
            return False, f"无法解析主机名: {host}"
        except ConnectionRefusedError:
            return False, f"连接被拒绝: {host}:{port}"
        except Exception as e:
            return False, f"连接测试失败: {str(e)}"
            
    except Exception as e:
        return False, f"URL验证失败: {str(e)}"

def test_rtmp_connection(rtmp_url, timeout=5):
    """测试RTMP连接
    Args:
        rtmp_url: RTMP URL
        timeout: 超时时间(秒)
    Returns:
        (bool, str): (是否连接成功, 详细信息)
    """
    try:
        # 首先检查URL格式
        if not rtmp_url.startswith(('rtmp://', 'rtmps://')):
            return False, "RTMP URL必须以 rtmp:// 或 rtmps:// 开头"
        
        logger.info(f"开始测试RTMP连接: {rtmp_url}, 超时设置: {timeout}秒")
        
        # 提取主机和路径部分进行日志记录
        host = extract_host_from_rtmp(rtmp_url)
        path = rtmp_url.split(host)[1] if host else ""
        logger.info(f"RTMP连接测试 - 主机: {host}, 路径: {path}")
        
        # 使用FFmpeg测试RTMP连接 - 确保每个参数都是单独的列表项
        test_cmd = [
            "ffmpeg",
            "-loglevel", "warning",  # 使用warning级别以获取更多有用的错误信息
            "-t", "1",               # 限制测试时间为1秒
            "-f", "lavfi",
            "-i", "testsrc=duration=1:size=320x240:rate=1",  # 1秒的测试视频
            "-c:v", "libx264",       # 使用x264编码器
            "-b:v", "360k",          # 设置低码率
            "-an",                   # 禁用音频
            "-f", "flv",
            rtmp_url
        ]
        
        # 记录完整命令
        logger.debug(f"执行测试命令: {' '.join(test_cmd)}")
        
        result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=timeout)
        
        if result.returncode == 0:
            logger.info(f"RTMP连接测试成功: {rtmp_url}")
            return True, "RTMP连接测试成功"
        else:
            error_msg = result.stderr.strip()
            stdout_msg = result.stdout.strip()
            
            if not error_msg and stdout_msg:
                error_msg = stdout_msg
            elif not error_msg:
                error_msg = f"返回码: {result.returncode}"
                
            # 分析错误信息
            if "Connection timed out" in error_msg or "timeout" in error_msg.lower():
                # 连接超时的更详细描述
                logger.warning(f"RTMP连接超时: {rtmp_url}, 错误: {error_msg}")
                return False, f"RTMP服务器连接超时: 请检查直播服务是否已开启或网络是否正常"
            elif "Connection refused" in error_msg:
                # 连接被拒绝的更详细描述
                logger.warning(f"RTMP连接被拒绝: {rtmp_url}, 错误: {error_msg}")
                return False, f"RTMP服务器拒绝连接: 请确认推流地址是否正确"
            elif "code=403" in error_msg or "forbidden" in error_msg.lower():
                # 403错误处理
                logger.warning(f"RTMP权限错误: {rtmp_url}, 错误: {error_msg}")
                return False, f"RTMP服务器返回403禁止访问: 直播密钥可能已失效或没有权限"
            elif "code=404" in error_msg or "not found" in error_msg.lower():
                # 404错误处理
                logger.warning(f"RTMP应用路径不存在: {rtmp_url}, 错误: {error_msg}")
                return False, f"RTMP应用路径不存在: 请检查直播密钥格式是否正确"
            
            logger.error(f"RTMP连接测试失败: {rtmp_url}, 错误: {error_msg}")
            return False, f"RTMP连接测试失败: {error_msg}"
    except subprocess.TimeoutExpired:
        logger.error(f"RTMP连接测试命令执行超时 ({timeout}秒): {rtmp_url}")
        return False, f"RTMP连接测试超时 ({timeout}秒): 网络可能较慢或服务器无响应"
    except Exception as e:
        logger.exception(f"RTMP连接测试异常: {rtmp_url}")
        return False, f"RTMP连接测试异常: {str(e)}"

def extract_host_from_rtmp(rtmp_url):
    """从RTMP URL提取主机名"""
    try:
        # 示例: rtmp://a.rtmp.youtube.com/live2/xxxx
        parts = rtmp_url.split('//')
        if len(parts) > 1:
            host_path = parts[1].split('/')
            return host_path[0]
    except:
        pass
    return None

# RTMP错误分类及其对应的重试策略
rtmp_error_strategies = {
    'default': {  # 统一策略
        'description': '网络连接错误',
        'retry_delay': 2,  # 2秒后重试
        'backoff_factor': 1,  # 固定延迟，不递增
        'max_retry': 20  # 最多重试20次
    }
} 