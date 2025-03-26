#!/bin/bash

# 设置错误处理
set -e
trap 'last_command=$current_command; current_command=$BASH_COMMAND' DEBUG
trap 'echo "\"${last_command}\" 命令失败，退出代码 $?."' EXIT

# 定义颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查是否以root运行
if [ "$EUID" -ne 0 ]; then 
    log_error "请以root权限运行此脚本"
    exit 1
fi

# 检查系统是否为 Debian/Ubuntu
if ! command -v apt-get &> /dev/null; then
    log_error "此脚本仅支持 Debian/Ubuntu 系统"
    exit 1
fi

# 检查并安装依赖
log_info "正在安装系统依赖..."
apt update || {
    log_error "更新包列表失败"
    exit 1
}

# 安装所需包
PACKAGES="python3-venv python3-pip ffmpeg proxychains-ng git"
for package in $PACKAGES; do
    if ! dpkg -l | grep -q "^ii  $package"; then
        log_info "安装 $package..."
        apt install -y $package || {
            log_error "安装 $package 失败"
            exit 1
        }
    else
        log_info "$package 已安装"
    fi
done

# 创建临时目录用于存储克隆的仓库
TEMP_CLONE_DIR=$(mktemp -d)
log_info "创建临时目录用于克隆代码: $TEMP_CLONE_DIR"

# 从GitHub克隆仓库
log_info "从GitHub克隆代码..."
git clone https://github.com/wqh11144/youtube-live.git $TEMP_CLONE_DIR || {
    log_error "克隆GitHub仓库失败"
    rm -rf $TEMP_CLONE_DIR
    exit 1
}

# 创建应用目录
APP_DIR="/var/youtube_live"
log_info "创建应用目录: $APP_DIR"
mkdir -p $APP_DIR || {
    log_error "创建应用目录失败"
    exit 1
}

# 创建数据目录
DATA_DIR="$APP_DIR/data"
log_info "创建数据目录: $DATA_DIR"
mkdir -p $DATA_DIR || {
    log_error "创建数据目录失败"
    exit 1
}

# 创建临时目录
log_info "创建临时目录..."
TEMP_DIR="$DATA_DIR/tmp"
mkdir -p $TEMP_DIR || {
    log_error "创建临时目录失败"
    exit 1
}
chmod 755 $TEMP_DIR
chown -R root:root $TEMP_DIR

# 设置应用目录权限
chmod 755 $APP_DIR || {
    log_error "设置应用目录权限失败"
    exit 1
}
chown -R root:root $APP_DIR || {
    log_error "设置应用目录所有者失败"
    exit 1
}

# 从克隆的仓库复制文件到应用目录
log_info "复制文件到应用目录..."
cp -r $TEMP_CLONE_DIR/* $APP_DIR/ || {
    log_error "复制文件到 $APP_DIR 失败"
    exit 1
}

# 清理临时目录
log_info "清理临时克隆目录..."
rm -rf $TEMP_CLONE_DIR

# 创建视频目录
log_info "创建视频目录..."
mkdir -p $APP_DIR/public/video
chmod 755 $APP_DIR/public/video

# 确保 static 目录存在并设置权限
log_info "配置静态文件目录..."
mkdir -p $APP_DIR/public/static
chmod 755 $APP_DIR/public/static

# 验证必要的静态文件
if [ ! -f "$APP_DIR/public/static/script.js" ] || [ ! -f "$APP_DIR/public/static/style.css" ]; then
    log_error "静态文件未正确复制"
    exit 1
fi

# 验证 index.html
if [ ! -f "$APP_DIR/public/index.html" ]; then
    log_error "index.html 文件未找到"
    exit 1
fi

log_info "静态文件配置完成"

# 创建虚拟环境
log_info "创建Python虚拟环境..."
cd $APP_DIR
python3 -m venv venv || {
    log_error "创建虚拟环境失败"
    exit 1
}
source venv/bin/activate

# 安装Python依赖
log_info "安装Python依赖..."
pip install --upgrade pip
pip install -r requirements.txt || {
    log_error "安装Python依赖失败"
    exit 1
}

# 创建日志目录
log_info "创建日志目录..."
mkdir -p $DATA_DIR/logs
chown -R root:root $DATA_DIR/logs
chmod 755 $DATA_DIR/logs

# 配置代理
log_info "配置代理环境..."
# 创建代理配置目录
PROXY_CONFIG_DIR="$DATA_DIR/proxy_configs"
if ! mkdir -p $PROXY_CONFIG_DIR; then
    log_error "创建代理配置目录失败"
    exit 1
fi

# 设置权限
if ! chmod 755 $PROXY_CONFIG_DIR; then
    log_error "设置代理配置目录权限失败"
    exit 1
fi

if ! chown -R root:root $PROXY_CONFIG_DIR; then
    log_error "设置代理配置目录所有者失败"
    exit 1
fi

log_info "代理配置目录创建成功: $PROXY_CONFIG_DIR"

# 创建任务历史目录
log_info "创建任务历史目录..."
TASKS_DIR="$DATA_DIR/tasks_history"
mkdir -p $TASKS_DIR
chmod 755 $TASKS_DIR
chown -R root:root $TASKS_DIR

# 创建默认的 proxychains 配置文件
cat > /etc/proxychains4.conf << 'EOL'
# proxychains.conf  VER 4.x
#
# 默认代理配置文件
# 每个任务会在 /var/youtube_live/data/proxy_configs 目录下创建自己的配置文件

# 代理链类型
strict_chain

# DNS 配置
proxy_dns
remote_dns_subnet 224

# 超时设置（毫秒）
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
# 默认不配置代理
# 每个任务的代理配置将存储在独立的配置文件中
# 位置：/var/youtube_live/data/proxy_configs/proxychains_{task_id}.conf
EOL

chmod 644 /etc/proxychains4.conf

# 创建 systemd 服务文件
log_info "创建 systemd 服务文件..."
cat > /etc/systemd/system/youtube-live.service << EOL
[Unit]
Description=YouTube Live Streaming API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/var/youtube_live
Environment="PATH=/var/youtube_live/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PROXY_CONFIG_DIR=/var/youtube_live/data/proxy_configs"
Environment="TMPDIR=/var/youtube_live/data/tmp"
ExecStart=/var/youtube_live/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
StandardOutput=append:/var/youtube_live/data/logs/youtube-live.log
StandardError=append:/var/youtube_live/data/logs/youtube-live.error.log

[Install]
WantedBy=multi-user.target
EOL

# 创建卸载脚本
log_info "创建卸载脚本..."
cat > $APP_DIR/uninstall.sh << 'EOL'
#!/bin/bash

if [ "$EUID" -ne 0 ]; then 
    echo "请以root权限运行此脚本"
    exit 1
fi

# 定义颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 备份配置
echo -e "${YELLOW}[INFO]${NC} 备份代理配置..."
BACKUP_DIR="/root/youtube_live_backup_$(date +%Y%m%d_%H%M%S)"
if [ -d "/var/youtube_live/data/proxy_configs" ]; then
    mkdir -p $BACKUP_DIR
    cp -r /var/youtube_live/data/proxy_configs $BACKUP_DIR/
    echo -e "${GREEN}[INFO]${NC} 代理配置已备份到: $BACKUP_DIR"
fi

echo -e "${YELLOW}[INFO]${NC} 停止服务..."
systemctl stop youtube-live
systemctl disable youtube-live

echo -e "${YELLOW}[INFO]${NC} 删除服务文件..."
rm -f /etc/systemd/system/youtube-live.service
systemctl daemon-reload

echo -e "${YELLOW}[INFO]${NC} 删除应用文件..."
rm -rf /var/youtube_live

echo -e "${YELLOW}[INFO]${NC} 清理代理配置..."
if [ -f "/etc/proxychains4.conf" ]; then
    mv /etc/proxychains4.conf /etc/proxychains4.conf.bak
    echo -e "${GREEN}[INFO]${NC} 原代理配置已备份为: /etc/proxychains4.conf.bak"
fi

echo -e "${GREEN}[INFO]${NC} 卸载完成"
echo -e "${YELLOW}[INFO]${NC} 配置备份位置: $BACKUP_DIR"
EOL

chmod +x $APP_DIR/uninstall.sh

# 重新加载 systemd 配置
log_info "重新加载 systemd 配置..."
systemctl daemon-reload

# 启用并启动服务
log_info "启用并启动服务..."
systemctl enable youtube-live
systemctl start youtube-live || {
    log_error "启动服务失败，请检查日志"
    journalctl -u youtube-live -n 50
    exit 1
}

# 启用服务（无论是否已存在）
log_info "启用服务..."
systemctl enable youtube-live || {
    log_error "启用服务失败，请检查单元文件是否存在"
    exit 1
}

# 判断服务是否正在运行
if systemctl is-active --quiet youtube-live; then
    log_info "服务已在运行，执行重启..."
    systemctl restart youtube-live || {
        log_error "重启服务失败，请检查日志"
        journalctl -u youtube-live -n 50
        exit 1
    }
else
    log_info "启动服务..."
    systemctl start youtube-live || {
        log_error "启动服务失败，请检查日志"
        journalctl -u youtube-live -n 50
        exit 1
    }
fi

log_info "============安装完成！============"

# 查看服务状态
systemctl status youtube-live

echo -e "
服务管理命令:
${GREEN}启动${NC}: systemctl start youtube-live
${GREEN}停止${NC}: systemctl stop youtube-live
${GREEN}重启${NC}: systemctl restart youtube-live
${GREEN}状态${NC}: systemctl status youtube-live
${GREEN}查看日志${NC}: journalctl -u youtube-live -f

日志文件位置:
- 应用日志: /var/youtube_live/data/logs/youtube-live.log
- 错误日志: /var/youtube_live/data/logs/youtube-live.error.log


卸载命令:
${RED}卸载${NC}: /var/youtube_live/uninstall.sh
"

# 检查服务是否成功启动
if systemctl is-active --quiet youtube-live; then
    log_info "服务已成功启动!"
    
    # 显示当前 IP
    SERVER_IP=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -v '127.0.0.1' | head -n 1)
    if [ ! -z "$SERVER_IP" ]; then
        echo -e "你可以通过 ${GREEN}http://${SERVER_IP}:8000${NC} 访问服务"
    fi
else
    log_error "警告: 服务可能未正常启动，请检查日志"
    journalctl -u youtube-live -n 50
fi

# 清除错误处理
trap - EXIT

# 删除安装脚本自身
log_info "清除安装缓存..."
rm -f "$0"

exit 0


