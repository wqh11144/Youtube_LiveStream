# YouTube Live 推流服务部署说明

## 文件说明
- `app/main.py`: 主程序文件
- `requirements.txt`: Python依赖文件
- `install.sh`: 安装脚本 

## 部署步骤

1. 将所有文件上传到服务器
```bash
# 在本地打包
tar -czf youtube-live.tar.gz main.py requirements.txt install.sh config.json

# 上传到服务器
scp youtube-live.tar.gz root@你的服务器IP:/youtube-tmp/
```

2. 在服务器上解压并安装
```bash
# 登录到服务器
ssh root@你的服务器IP

# 解压文件
cd /youtube-tmp
tar -xzf youtube-live.tar.gz

# 运行安装脚本
chmod +x install.sh
./install.sh
```

## 服务管理命令

```bash
# 启动服务
systemctl start youtube-live

# 停止服务
systemctl stop youtube-live

# 重启服务
systemctl restart youtube-live

# 查看服务状态
systemctl status youtube-live

# 查看日志
journalctl -u youtube-live -f
```

## 日志位置
- 应用日志: `/var/youtube_live/data/logs/youtube-live.log`
- 错误日志: `/var/youtube_live/data/logs/youtube-live.error.log`

## 注意事项
1. 确保服务器已安装 Python 3.8 或更高版本
2. 确保服务器防火墙允许 8000 端口访问
3. 确保服务器有足够的磁盘空间和内存

## 卸载服务
如果需要卸载服务，执行以下命令：
```bash
systemctl stop youtube-live
systemctl disable youtube-live
rm /etc/systemd/system/youtube-live.service
rm -rf /var/youtube_live
rm -rf /var/youtube_live/data/logs
``` 