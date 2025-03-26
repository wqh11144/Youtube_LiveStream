// 全局变量
const API_BASE_URL = '/api';  // 不要使用 http://0.0.0.0:8000/api
let toast = null;
let confirmModal = null;
let taskDetailModal = null;
let streamSettings = null; // 存储推流设置
let currentTaskDetail = null; // 存储当前查看的任务详情
let tasksData = [];
let maxFileSize = null; // 文件大小限制（MB）

// 获取并显示应用版本
async function getAndDisplayVersion() {
    try {
        const response = await fetch('/version');
        if (response.ok) {
            const data = await response.json();
            // 更新版本号显示
            const versionElement = document.getElementById('appVersion');
            if (versionElement) {
                versionElement.textContent = data.version;
            }
            // 更新文件大小限制
            if (data.max_file_size_mb) {
                maxFileSize = data.max_file_size_mb;
                console.log('已更新文件大小限制:', maxFileSize, 'MB');
                updateUploadSizeLimit();
            }
        }
    } catch (error) {
        console.error('获取版本信息失败', error);
        showToast('错误', '获取系统配置失败');
    }
}

// 更新上传大小限制提示
function updateUploadSizeLimit() {
    const uploadHint = document.getElementById('uploadSizeLimit');
    if (uploadHint && maxFileSize) {
        uploadHint.textContent = `只支持视频编码: h264；支持的视频格式：mp4, flv（最大${maxFileSize}MB）`;
    }
}

// 显示提示信息
function showToast(title, message) {
    if (!toast) {
        console.warn('Toast 组件未初始化');
        return;
    }
    const toastTitle = document.getElementById('toastTitle');
    const toastMessage = document.getElementById('toastMessage');
    
    if (toastTitle) toastTitle.textContent = title;
    if (toastMessage) toastMessage.textContent = message;
    
    toast.show();
}

// 任务状态枚举
const TaskStatus = {
    RUNNING: 'running',
    STOPPED: 'stopped',
    AUTO_STOPPED: 'auto_stopped',
    ERROR: 'error',
    COMPLETED: 'completed',
    SCHEDULED: 'scheduled'
};

// 任务状态显示配置
const TaskStatusConfig = {
    [TaskStatus.RUNNING]: { label: '运行中', class: 'bg-success' },
    [TaskStatus.STOPPED]: { label: '已停止', class: 'bg-secondary' },
    [TaskStatus.AUTO_STOPPED]: { label: '已完成', class: 'bg-info' },
    [TaskStatus.ERROR]: { label: '发生异常', class: 'bg-danger' },
    [TaskStatus.COMPLETED]: { label: '已结束', class: 'bg-primary' },
    [TaskStatus.SCHEDULED]: { label: '计划中', class: 'bg-warning' }
};

// 添加排序状态变量
let currentSortField = 'start_time';
let currentSortDirection = 'desc';

// 获取系统配置
async function getSystemConfig() {
    try {
        const response = await fetch('/version');
        if (response.ok) {
            const data = await response.json();
            if (data.max_file_size_mb) {
                maxFileSize = data.max_file_size_mb;
                console.log('已更新文件大小限制:', maxFileSize, 'MB');
                updateUploadSizeLimit();
            }
        }
    } catch (error) {
        console.error('获取系统配置失败:', error);
        showToast('错误', '获取系统配置失败');
    }
}

// 页面加载完成后执行
document.addEventListener('DOMContentLoaded', async function() {
    console.log('页面加载完成');
    
    // 初始化Toast组件
    const toastEl = document.getElementById('toast');
    if (toastEl) {
        toast = new bootstrap.Toast(toastEl, {
            delay: 3000
        });
    }
    
    // 初始化确认弹窗组件
    const confirmEl = document.getElementById('confirmModal');
    if (confirmEl) {
        confirmModal = new bootstrap.Modal(confirmEl);
    }
    
    // 初始化任务详情弹窗组件
    const taskDetailEl = document.getElementById('taskDetailModal');
    if (taskDetailEl) {
        taskDetailModal = new bootstrap.Modal(taskDetailEl);
    }
    
    // 初始化加载中弹窗组件并确保其可用
    initializeLoadingModal();
    
    // 添加详情按钮的事件监听
    document.addEventListener('click', function(e) {
        if (e.target.closest('.task-detail-btn')) {
            const taskId = e.target.closest('.task-detail-btn').getAttribute('data-task-id');
            if (taskId) {
                showTaskDetail(taskId);
            }
        }
    });
    
    // 创建操作确认弹窗
    if (!document.getElementById('confirmActionModal')) {
        // 创建模态弹窗HTML
        const modalHTML = `
            <div class="modal fade" id="confirmActionModal" tabindex="-1" aria-labelledby="confirmActionModalLabel" aria-hidden="true">
                <div class="modal-dialog modal-dialog-centered">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title" id="confirmActionModalLabel">确认操作</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                        <div class="modal-body">
                            <p id="confirmActionMessage">确定要执行此操作吗？</p>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button>
                            <button type="button" class="btn btn-danger" id="confirmActionButton">确认</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        // 将弹窗HTML添加到body
        document.body.insertAdjacentHTML('beforeend', modalHTML);
        
        // 初始化操作确认弹窗
        const confirmActionEl = document.getElementById('confirmActionModal');
        if (confirmActionEl) {
            confirmActionModal = new bootstrap.Modal(confirmActionEl);
            
            // 添加确认按钮事件监听
            document.getElementById('confirmActionButton').addEventListener('click', function() {
                // 隐藏弹窗
                confirmActionModal.hide();
                
                // 如果有回调函数，执行它
                if (confirmActionCallback && typeof confirmActionCallback === 'function') {
                    confirmActionCallback();
                }
                
                // 重置回调
                confirmActionCallback = null;
            });
        }
    }
    
    // 获取并显示应用版本
    await getAndDisplayVersion();
    
    // 获取系统配置（包含文件大小限制）
    await getSystemConfig();
    
    // 获取视频列表
    refreshVideoList();
    
    // 刷新任务状态
    refreshTaskStatus();
    
    // 设置定时器，每10秒刷新一次任务状态
    setInterval(refreshTaskStatus, 10000);
    
    // 初始化表单提交事件
    const streamForm = document.getElementById('streamForm');
    if (streamForm) {
        streamForm.addEventListener('submit', function(e) {
            e.preventDefault();
            showConfirmDialog();
        });
    }
    
    // 初始化确认开始按钮事件
    const confirmStartStream = document.getElementById('confirmStartStream');
    if (confirmStartStream) {
        confirmStartStream.addEventListener('click', function() {
            confirmModal.hide();
            startStream();
        });
    }
    
    // 设置计划时间的最小值（确保至少为当前时间）
    const scheduledStartTimeInput = document.getElementById('scheduledStartTime');
    if (scheduledStartTimeInput) {
        // 获取当前时间并格式化为datetime-local格式（YYYY-MM-DDThh:mm）
        const now = new Date();
        const year = now.getFullYear();
        const month = String(now.getMonth() + 1).padStart(2, '0');
        const day = String(now.getDate()).padStart(2, '0');
        const hours = String(now.getHours()).padStart(2, '0');
        const minutes = String(now.getMinutes()).padStart(2, '0');
        const formattedDateTime = `${year}-${month}-${day}T${hours}:${minutes}`;
        
        scheduledStartTimeInput.min = formattedDateTime;
    }
});

// 强化的加载中弹窗初始化函数
function initializeLoadingModal() {
    console.log('初始化加载中弹窗...');
    // 先调用原始的初始化函数
    loadingModalInit();
    
    // 检查是否成功初始化
    if (!loadingModal) {
        console.warn('loadingModal初始化失败，尝试重新初始化...');
        const loadingEl = document.getElementById('loadingModal');
        
        if (loadingEl) {
            try {
                // 尝试使用不同的方式初始化Modal
                loadingModal = new bootstrap.Modal(loadingEl);
                console.log('使用替代方法成功初始化loadingModal');
            } catch (error) {
                console.error('初始化loadingModal失败:', error);
            }
        } else {
            console.error('找不到loadingModal元素');
        }
    }
    
    // 确保任何存在的modal-backdrop被移除
    const existingBackdrops = document.querySelectorAll('.modal-backdrop');
    if (existingBackdrops.length > 0) {
        console.warn(`发现${existingBackdrops.length}个残留的modal-backdrop，移除中...`);
        existingBackdrops.forEach(backdrop => {
            backdrop.remove();
        });
        
        // 清理body上的modal相关样式
        document.body.classList.remove('modal-open');
        document.body.style.removeProperty('overflow');
        document.body.style.removeProperty('padding-right');
    }
    
    console.log('加载中弹窗初始化完成');
}

// 显示确认弹窗
function showConfirmDialog() {
    // 安全获取表单值
    const videoSelect = document.getElementById('videoSelect');
    const rtmpUrl = document.getElementById('rtmpUrl');
    const taskName = document.getElementById('taskName');
    const autoStopMinutes = document.getElementById('autoStopMinutes');
    const transcodeEnabled = document.getElementById('transcodeEnabled');
    const socks5Proxy = document.getElementById('socks5Proxy');
    
    // 确保所有元素都存在
    if (!videoSelect || !rtmpUrl) {
        showToast('错误', '无法获取表单元素，请刷新页面重试');
        return;
    }
    
    const videoFilename = videoSelect.value;
    let rtmpUrlValue = rtmpUrl.value;
    const taskNameValue = taskName ? taskName.value : '';
    const autoStopMinutesValue = autoStopMinutes ? autoStopMinutes.value : '699';
    const transcodeEnabledValue = transcodeEnabled ? transcodeEnabled.checked : false;
    const socks5ProxyValue = socks5Proxy ? socks5Proxy.value : '';

    if (!videoFilename) {
        showToast('错误', '请选择视频文件');
        return;
    }
    
    if (!rtmpUrlValue) {
        showToast('错误', '请输入RTMP地址或直播码');
        return;
    }

    // 处理RTMP地址 - 如果用户只输入了直播码，自动添加前缀
    const RTMP_PREFIX = 'rtmp://a.rtmp.youtube.com/live2/';
    if (!rtmpUrlValue.startsWith('rtmp://')) {
        // 用户可能只输入了直播码，自动添加前缀
        rtmpUrlValue = RTMP_PREFIX + rtmpUrlValue.trim();
    }

    // 存储设置以供后续使用
    streamSettings = {
        rtmp_url: rtmpUrlValue,
        video_filename: videoFilename,
        task_name: taskNameValue || '',
        auto_stop_minutes: parseInt(autoStopMinutesValue),
        transcode_enabled: transcodeEnabledValue,
        socks5_proxy: socks5ProxyValue || undefined
    };

    // 更新确认弹窗内容
    const confirmVideo = document.getElementById('confirmVideo');
    const confirmRtmp = document.getElementById('confirmRtmp');
    const confirmTaskName = document.getElementById('confirmTaskName');
    const confirmTime = document.getElementById('confirmTime');
    const confirmTranscode = document.getElementById('confirmTranscode');
    const confirmProxy = document.getElementById('confirmProxy');
    
    if (confirmVideo) confirmVideo.textContent = videoFilename;
    
    // 对RTMP地址的显示进行特殊处理
    if (confirmRtmp) {
        // 如果用户输入的是直播码（系统自动添加了前缀）
        if (rtmpUrlValue !== rtmpUrl.value && rtmpUrlValue.includes(rtmpUrl.value)) {
            confirmRtmp.innerHTML = `<span title="${rtmpUrlValue}">${rtmpUrl.value} <small class="text-muted">(系统将自动添加前缀)</small></span>`;
        } else {
            confirmRtmp.textContent = rtmpUrlValue;
        }
    }
    
    if (confirmTaskName) confirmTaskName.textContent = taskNameValue || '未设置';
    if (confirmTime) confirmTime.textContent = `${autoStopMinutesValue} 分钟`;
    if (confirmTranscode) confirmTranscode.textContent = transcodeEnabledValue ? '已启用' : '未启用';
    if (confirmProxy) confirmProxy.textContent = socks5ProxyValue ? '已配置' : '未配置';

    // 显示确认弹窗
    if (confirmModal) {
        confirmModal.show();
    } else {
        showToast('错误', '无法显示确认对话框，请刷新页面重试');
    }
}

// 文件上传相关
function initializeUpload() {
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const uploadProgress = document.getElementById('uploadProgress');
    
    // 如果元素不存在，直接返回
    if (!dropZone || !fileInput || !uploadProgress) {
        console.warn('上传相关元素未找到，跳过初始化上传功能');
        return;
    }
    
    const progressBar = uploadProgress.querySelector('.progress-bar');
    
    // 拖放处理
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('upload-zone-active');
    });
    
    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('upload-zone-active');
    });
    
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('upload-zone-active');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFileUpload(files[0]);
        }
    });
    
    // 文件选择处理
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFileUpload(e.target.files[0]);
        }
    });
    
    async function handleFileUpload(file) {
        // 验证文件类型
        const validTypes = ['.mp4', '.mov', '.avi', '.flv'];
        const fileExt = '.' + file.name.split('.').pop().toLowerCase();
        if (!validTypes.includes(fileExt)) {
            showToast('错误', `不支持的文件格式。支持的格式：${validTypes.join(', ')}`);
            return;
        }
        
        // 验证文件大小
        const maxFileSizeBytes = maxFileSize * 1024 * 1024; // 转换为字节
        if (file.size > maxFileSizeBytes) {
            showToast('错误', `文件大小超过限制（最大${maxFileSize}MB）`);
            return;
        }
        
        // 准备上传
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            // 显示进度条
            uploadProgress.classList.remove('d-none');
            progressBar.style.width = '0%';
            progressBar.textContent = '0%';
            
            const response = await fetch(`${API_BASE_URL}/video/upload`, {
                method: 'POST',
                body: formData,
                onUploadProgress: (progressEvent) => {
                    const percentCompleted = Math.round((progressEvent.loaded * 100) / progressEvent.total);
                    progressBar.style.width = `${percentCompleted}%`;
                    progressBar.textContent = `${percentCompleted}%`;
                }
            });
            
            const result = await response.json();
            
            if (result.status === 'success') {
                showToast('成功', '文件上传成功');
                // 刷新视频列表
                refreshVideoList();
            } else {
                throw new Error(result.message);
            }
            
        } catch (error) {
            showToast('错误', `上传失败: ${error.message}`);
        } finally {
            // 隐藏进度条
            setTimeout(() => {
                uploadProgress.classList.add('d-none');
                progressBar.style.width = '0%';
                progressBar.textContent = '';
            }, 2000);
        }
    }
}

// 刷新视频列表
async function refreshVideoList() {
    try {
        const response = await fetch(`${API_BASE_URL}/video/list`);
        const data = await response.json();
        
        if (data.status === 'success') {
            const select = document.getElementById('videoSelect');
            select.innerHTML = '<option value="">请选择视频文件...</option>';
            
            data.files.forEach(file => {
                const option = document.createElement('option');
                option.value = file;
                // 处理 Windows 路径
                option.textContent = file.split('\\').pop().split('/').pop();
                select.appendChild(option);
            });
            
            showToast('成功', '视频列表已更新');
        } else {
            throw new Error(data.message || '获取视频列表失败');
        }
    } catch (error) {
        showToast('错误', error.message);
    }
}

// 获取状态显示HTML
function getStatusBadgeHtml(status) {
    const config = TaskStatusConfig[status] || { label: '未知状态', class: 'bg-secondary' };
    return `<span class="badge ${config.class} rounded-pill">${config.label}</span>`;
}

// 格式化时间函数
function formatDateTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    });
}

// 排序任务列表
function sortTasks(field) {
    const startTimeIcon = document.getElementById('sort-start-time');
    const endTimeIcon = document.getElementById('sort-end-time');
    
    // 重置所有排序图标
    startTimeIcon.className = 'bx bx-sort';
    endTimeIcon.className = 'bx bx-sort';
    
    // 如果点击的是当前排序字段，切换排序方向
    if (field === currentSortField) {
        currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        currentSortField = field;
        currentSortDirection = 'desc'; // 默认降序
    }
    
    // 更新排序图标
    const currentIcon = document.getElementById(`sort-${field}`);
    currentIcon.className = `bx bx-sort-${currentSortDirection === 'asc' ? 'up' : 'down'}`;
    
    // 重新渲染任务列表
    renderTaskList();
}

// 渲染任务列表
function renderTaskList() {
    const taskList = document.getElementById('taskList');
    console.log('开始渲染任务列表，数据条数:', tasksData.length);
    
    taskList.innerHTML = '';
    
    if (tasksData.length === 0) {
        console.log('没有任务数据，显示空状态');
        taskList.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted py-4">
                    <i class="bx bx-info-circle me-2"></i>暂无任务记录
                </td>
            </tr>
        `;
        return;
    }
    
    // 对任务数据进行排序
    const sortedTasks = [...tasksData].sort((a, b) => {
        const aValue = a[currentSortField] ? new Date(a[currentSortField]).getTime() : 0;
        const bValue = b[currentSortField] ? new Date(b[currentSortField]).getTime() : 0;
        return currentSortDirection === 'asc' ? aValue - bValue : bValue - aValue;
    });
    
    console.log('排序后的任务数据:', sortedTasks);
    
    sortedTasks.forEach((task, index) => {
        console.log(`渲染第 ${index + 1} 个任务:`, task);
        const row = document.createElement('tr');
        row.setAttribute('data-task-id', task.id);
        
        // 格式化计划时间
        let scheduledTimeDisplay = '-';
        if (task.scheduled_start_time) {
            // 不再显示自动设置标记
            scheduledTimeDisplay = formatDateTime(task.scheduled_start_time);
        }
        const endTime = task.end_time ? formatDateTime(task.end_time) : '-';
        
        row.innerHTML = `
            <td><small>${task.id.substring(0, 8)}</small></td>
            <td>${task.rtmp_url.match(/[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$/)?.[0] || task.rtmp_url}</td>
            <td>${getStatusBadgeHtml(task.status)}</td>
            
            <td><small>${scheduledTimeDisplay}</small></td>
            <td><small>${endTime}</small></td>
            <td class="text-truncate" style="max-width: 200px;">
                <span title="${task.task_name || '-'}" class="d-inline-block text-truncate" style="max-width: 100%;">
                    ${task.task_name || '-'}
                </span>
            </td>
            <td>
                <div class="btn-group">
                    ${task.status === TaskStatus.RUNNING ? 
                        `<button class="btn btn-sm btn-danger" onclick="stopStream('${task.id}')">
                            <i class="bx bx-stop-circle"></i>
                        </button>` : 
                        ''
                    }
                    ${task.status === 'scheduled' ? 
                        `<button class="btn btn-sm btn-warning" onclick="cancelScheduledTask('${task.id}')">
                            <i class="bx bx-x-circle"></i>
                        </button>` : 
                        ''
                    }
                    <button class="btn btn-sm btn-primary task-detail-btn" data-task-id="${task.id}">
                        <i class="bx bx-detail"></i>
                    </button>
                </div>
            </td>
        `;
        
        taskList.appendChild(row);
    });
    
    console.log('任务列表渲染完成');
}

// 修改刷新任务状态函数
async function refreshTaskStatus() {
    try {
        console.log('正在刷新任务状态...');
        const taskLimit = parseInt(document.getElementById('taskLimit').value) || 15;
        console.log('当前设置的任务显示数量:', taskLimit);
        
        const response = await fetch(`${API_BASE_URL}/tasks/list?limit=${taskLimit}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        
        if (!data) {
            throw new Error('服务器返回数据为空');
        }
        
        // 存储任务数据
        tasksData = data.tasks || [];
        console.log(`已加载 ${tasksData.length} 条任务数据`);
        
        // 渲染任务列表
        renderTaskList();
    } catch (error) {
        console.error('刷新任务状态失败:', error);
        showToast('错误', '刷新任务状态失败');
    }
}

// 显示任务详情
function showTaskDetail(taskId) {
    // 查找任务
    const task = tasksData.find(t => t.id === taskId);
    if (!task) {
        showToast('错误', '找不到任务详情');
        return;
    }
    
    // 存储当前查看的任务详情
    currentTaskDetail = task;
    
    // 设置任务详情
    function setElementText(id, value) {
        const element = document.getElementById(id);
        if (element) {
            element.textContent = value || '-';
        }
    }
    
    function setElementHTML(id, value) {
        const element = document.getElementById(id);
        if (element) {
            element.innerHTML = value || '-';
        }
    }
    
    setElementText('detailTaskId', task.id);
    setElementText('detailTaskName', task.task_name || '-');
    setElementHTML('detailStatus', getStatusBadgeHtml(task.status));
    setElementText('detailCreateTime', formatDateTime(task.create_time) || '-');
    
    // 计划时间
    setElementText('detailScheduledTime', task.scheduled_start_time ? formatDateTime(task.scheduled_start_time) : '-');
    
    setElementText('detailEndTime', formatDateTime(task.end_time) || '-');
    
    // 安全处理文件名显示
    if (task.video_filename) {
        setElementText('detailVideoFile', task.video_filename.split('\\').pop().split('/').pop());
    } else {
        setElementText('detailVideoFile', '-');
    }
    
    setElementText('detailRtmpUrl', task.rtmp_url);
    setElementText('detailAutoStop', task.auto_stop_minutes);
    setElementText('detailTranscode', task.transcode_enabled ? '启用' : '禁用');
    setElementText('detailProxy', task.socks5_proxy || '未设置');
    
    // 添加显示任务消息的内容
    setElementText('detailMessage', task.message || '无消息');
    
    // 添加显示错误详细信息的内容
    const errorMessageElement = document.getElementById('detailErrorMessage');
    if (errorMessageElement) {
        if (task.error_message) {
            errorMessageElement.textContent = task.error_message;
            errorMessageElement.style.display = 'block';
        } else {
            errorMessageElement.textContent = '无详细信息';
            errorMessageElement.style.color = '#6c757d'; // 使用灰色显示无详细信息
        }
    }
    
    // 如果任务有运行时长，显示；否则计算或显示未知
    let runtimeText = '未知';
    if (task.runtime_minutes) {
        runtimeText = `${task.runtime_minutes.toFixed(2)} 分钟`;
    } else if (task.start_time && task.end_time) {
        // 计算运行时长
        const start = new Date(task.start_time);
        const end = new Date(task.end_time);
        const diffMinutes = (end - start) / (1000 * 60);
        runtimeText = `${diffMinutes.toFixed(2)} 分钟`;
    }
    setElementText('detailRuntime', runtimeText);
    
    // 使用全局定义的 Modal 实例
    taskDetailModal.show();
}

// 重用任务设置并立即推流
function reusePreviousSettings() {
    if (!currentTaskDetail) {
        showToast('错误', '无法获取任务详情，请重试');
        return;
    }
    
    // 重用之前的设置
    streamSettings = {
        rtmp_url: currentTaskDetail.rtmp_url || '',
        video_filename: currentTaskDetail.video_filename || '',
        task_name: currentTaskDetail.task_name || '',
        auto_stop_minutes: currentTaskDetail.auto_stop_minutes || 699, // 默认值
        transcode_enabled: !!currentTaskDetail.transcode_enabled, // 确保是布尔值
        socks5_proxy: currentTaskDetail.socks5_proxy || undefined
    };
    
    // 关闭详情弹窗
    taskDetailModal.hide();
    
    // 开始推流
    startStream();
}

// 编辑设置后重新开播
function editAndRestart() {
    if (!currentTaskDetail) {
        showToast('错误', '无法获取任务详情，请重试');
        return;
    }
    
    // 更新表单，安全地设置每个字段的值
    // 首先检查每个元素是否存在
    const videoSelect = document.getElementById('videoSelect');
    if (videoSelect) videoSelect.value = currentTaskDetail.video_filename || '';
    
    const rtmpUrl = document.getElementById('rtmpUrl');
    if (rtmpUrl) rtmpUrl.value = currentTaskDetail.rtmp_url || '';
    
    const taskName = document.getElementById('taskName');
    if (taskName) taskName.value = currentTaskDetail.task_name || '';
    
    const autoStopMinutes = document.getElementById('autoStopMinutes');
    if (autoStopMinutes) autoStopMinutes.value = currentTaskDetail.auto_stop_minutes || 699;
    
    const transcodeEnabled = document.getElementById('transcodeEnabled');
    if (transcodeEnabled) transcodeEnabled.checked = !!currentTaskDetail.transcode_enabled;
    
    const socks5Proxy = document.getElementById('socks5Proxy');
    if (socks5Proxy) socks5Proxy.value = currentTaskDetail.socks5_proxy || '';
    
    // 清空计划时间字段，让用户重新选择
    const scheduledStartTime = document.getElementById('scheduledStartTime');
    if (scheduledStartTime) scheduledStartTime.value = '';
    
    // 关闭详情弹窗
    taskDetailModal.hide();
}

// 选择新视频重新开播
function reuseWithNewVideo() {
    if (!currentTaskDetail) {
        showToast('错误', '无法获取任务详情，请重试');
        return;
    }
    
    // 安全填充表单
    const rtmpUrl = document.getElementById('rtmpUrl');
    if (rtmpUrl) rtmpUrl.value = currentTaskDetail.rtmp_url || '';
    
    const autoStopMinutes = document.getElementById('autoStopMinutes');
    if (autoStopMinutes) autoStopMinutes.value = currentTaskDetail.auto_stop_minutes || 699;
    
    const transcodeEnabled = document.getElementById('transcodeEnabled');
    if (transcodeEnabled) transcodeEnabled.checked = !!currentTaskDetail.transcode_enabled;
    
    const socks5Proxy = document.getElementById('socks5Proxy');
    if (socks5Proxy) socks5Proxy.value = currentTaskDetail.socks5_proxy || '';
    
    // 关闭详情弹窗
    taskDetailModal.hide();
    
    // 安全滚动到视频选择区域
    const videoSelect = document.getElementById('videoSelect');
    if (videoSelect) {
        videoSelect.scrollIntoView({ behavior: 'smooth' });
    }
}

// 开始推流
async function startStream() {
    // 确保在函数结束时关闭加载弹窗的标志
    let loadingModalShown = false;
    
    // 安全定时器ID，用于清除
    let safetyTimerId = null;
    
    try {
        // 检查streamSettings是否存在
        if (!streamSettings) {
            showToast('错误', '推流设置不完整，请重新选择视频和RTMP地址');
            return;
        }

        // 安全获取scheduledStartTime值
        let scheduledStartTime = null;
        const scheduledStartTimeElement = document.getElementById('scheduledStartTime');
        if (scheduledStartTimeElement) {
            scheduledStartTime = scheduledStartTimeElement.value || null;
        }
        
        // 构建请求数据
        const requestData = {
            ...streamSettings,
            scheduled_start_time: scheduledStartTime
        };
        
        // 确保task_name字段存在且长度不超过限制
        if (!requestData.task_name) {
            requestData.task_name = '';
        } else if (requestData.task_name.length > 25) {
            // 截断过长的任务描述
            requestData.task_name = requestData.task_name.substring(0, 25);
            showToast('提示', '任务描述过长，已自动截断为25个字符');
        }

        // 显示加载中弹窗
        try {
            showLoadingModal('任务创建中，请稍候...', '正在与服务器通信');
            loadingModalShown = true;
            console.log('加载弹窗已显示，准备发送请求...');
        } catch (modalError) {
            console.error('显示加载弹窗失败:', modalError);
        }

        // 记录请求开始时间和设置超时处理
        const requestStartTime = new Date();
        const requestTimeout = 15000; // 15秒超时
        
        // 设置一个安全定时器，确保即使发生意外，加载弹窗也会在一定时间后关闭
        safetyTimerId = setTimeout(() => {
            if (loadingModalShown) {
                console.warn('安全定时器触发: 强制关闭加载弹窗');
                hideLoadingModal();
                loadingModalShown = false;
                showToast('警告', '操作超时，但请求可能仍在后台处理中');
            }
        }, requestTimeout);
        
        let response;
        try {
            console.log('发送API请求...');
            response = await fetch(`${API_BASE_URL}/tasks/start`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestData)
            });
            
            console.log('收到响应，状态码:', response.status);
            
            // 重要: 收到响应就立即关闭弹窗，不等待JSON解析
            if (loadingModalShown) {
                hideLoadingModal();
                loadingModalShown = false;
                console.log('在解析响应前已关闭加载弹窗');
            }
            
            // 清除安全定时器
            if (safetyTimerId) {
                clearTimeout(safetyTimerId);
                safetyTimerId = null;
            }
        } catch (fetchError) {
            console.error('请求发送失败:', fetchError);
            throw new Error(`网络请求失败: ${fetchError.message}`);
        }

        // 确保响应成功
        if (!response.ok) {
            throw new Error(`服务器返回错误状态码: ${response.status}`);
        }

        let data;
        try {
            console.log('开始解析响应JSON...');
            data = await response.json();
            console.log('解析JSON成功:', data);
        } catch (jsonError) {
            console.error('JSON解析失败:', jsonError);
            throw new Error('服务器响应格式错误');
        }

        if (data.status === 'success') {
            showToast('成功', data.message);
            refreshTaskStatus();
            // 安全清空表单
            const streamForm = document.getElementById('streamForm');
            if (streamForm) {
                streamForm.reset();
            }
        } else {
            showToast('错误', data.message || '创建任务失败');
        }
    } catch (error) {
        console.error('创建任务失败:', error);
        showToast('错误', `启动推流失败: ${error.message}`);
    } finally {
        // 清除安全定时器
        if (safetyTimerId) {
            clearTimeout(safetyTimerId);
        }
        
        // 确保无论如何都关闭加载弹窗
        if (loadingModalShown) {
            console.log('通过finally块关闭加载弹窗');
            try {
                hideLoadingModal();
                // 以防万一再次尝试直接移除modal-backdrop
                const backdrops = document.querySelectorAll('.modal-backdrop');
                if (backdrops.length > 0) {
                    backdrops.forEach(backdrop => {
                        backdrop.remove();
                        console.log('finally中移除了modal背景');
                    });
                    
                    // 恢复body样式
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('overflow');
                    document.body.style.removeProperty('padding-right');
                }
            } catch (finallyError) {
                console.error('finally块中关闭弹窗失败:', finallyError);
            }
        }
    }
}

// 停止推流
async function stopStream(taskId) {
    // 显示确认弹窗
    showConfirmActionModal('停止直播', '确定要停止此直播任务吗？', async () => {
        try {
            // 显示加载中弹窗
            showLoadingModal('正在停止任务...', '请稍候');
            
            const response = await fetch(`${API_BASE_URL}/tasks/stop/${taskId}`);
            const data = await response.json();
            
            // 隐藏加载中弹窗
            hideLoadingModal();
            
            if (data.status !== 'error') {
                showToast('成功', '推流任务已停止，任务记录将在刷新后更新');
                
                // 立即更新当前任务的状态显示
                const taskRow = document.querySelector(`tr[data-task-id="${taskId}"]`);
                if (taskRow) {
                    const statusCell = taskRow.querySelector('td:nth-child(3)');
                    const operationCell = taskRow.querySelector('td:last-child');
                    
                    if (statusCell) {
                        statusCell.innerHTML = getStatusBadgeHtml(TaskStatus.STOPPED);
                    }
                    
                    if (operationCell) {
                        operationCell.innerHTML = `
                            <div class="btn-group">
                                <button class="btn btn-sm btn-primary task-detail-btn" data-task-id="${taskId}">
                                    <i class="bx bx-detail"></i>
                                </button>
                            </div>
                        `;
                    }
                }
                
                // 刷新任务状态
                refreshTaskStatus();
            } else {
                throw new Error(data.message || '停止推流失败');
            }
        } catch (error) {
            // 隐藏加载中弹窗
            hideLoadingModal();
            showToast('错误', error.message);
        }
    });
}

// 上传视频文件
async function uploadVideo(input) {
    if (!input.files || input.files.length === 0) {
        return;
    }

    const file = input.files[0];
    
    // 验证文件类型
    const validTypes = ['.mp4', '.mov', '.avi', '.flv'];
    const fileExt = '.' + file.name.split('.').pop().toLowerCase();
    if (!validTypes.includes(fileExt)) {
        showToast('错误', `不支持的文件格式。支持的格式：${validTypes.join(', ')}`);
        input.value = '';
        return;
    }
    
    // 验证文件大小
    if (!maxFileSize) {
        showToast('错误', '系统配置未加载，请刷新页面重试');
        input.value = '';
        return;
    }
    
    const maxFileSizeBytes = maxFileSize * 1024 * 1024; // 转换为字节
    if (file.size > maxFileSizeBytes) {
        showToast('错误', `文件大小超过限制（最大${maxFileSize}MB）`);
        input.value = '';
        return;
    }

    // 显示上传进度
    const progressBar = document.getElementById('uploadProgress');
    const progressContainer = document.getElementById('uploadProgressContainer');
    progressContainer.style.display = 'block';
    progressBar.style.width = '0%';
    progressBar.textContent = '0%';

    try {
        const formData = new FormData();
        formData.append('file', file);

        await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            
            xhr.upload.addEventListener('progress', (event) => {
                if (event.lengthComputable) {
                    const percentComplete = Math.round((event.loaded / event.total) * 100);
                    progressBar.style.width = percentComplete + '%';
                    progressBar.textContent = percentComplete + '%';
                }
            });

            xhr.addEventListener('load', () => {
                if (xhr.status === 200) {
                    const result = JSON.parse(xhr.responseText);
                    if (result.status === 'success') {
                        showToast('成功', '文件上传成功');
                        refreshVideoList();
                        resolve(result);
                    } else {
                        reject(new Error(result.message || '上传失败'));
                    }
                } else {
                    reject(new Error('上传失败'));
                }
                // 在上传完成后重置进度条
                setTimeout(() => {
                    progressContainer.style.display = 'none';
                    progressBar.style.width = '0%';
                    progressBar.textContent = '0%';
                }, 1000);
            });

            xhr.addEventListener('error', () => {
                reject(new Error('上传失败'));
                // 在上传失败后重置进度条
                progressContainer.style.display = 'none';
                progressBar.style.width = '0%';
                progressBar.textContent = '0%';
            });

            xhr.open('POST', `${API_BASE_URL}/video/upload`);
            xhr.send(formData);
        });
    } catch (error) {
        console.error('上传失败:', error);
        showToast('错误', '上传失败: ' + error.message);
    } finally {
        // 清空文件输入
        input.value = '';
    }
}

// 清空视频列表
async function clearVideos() {
    // 显示确认弹窗
    showConfirmActionModal('清空视频', '确定要删除所有视频文件吗？此操作不可恢复！', async () => {
        try {
            // 显示加载中弹窗
            showLoadingModal('正在清空视频...', '请稍候');
            
            const response = await fetch(`${API_BASE_URL}/video/clear`, {
                method: 'DELETE'
            });
            
            const data = await response.json();
            
            // 隐藏加载中弹窗
            hideLoadingModal();
            
            if (data.status === 'success') {
                showToast('成功', '所有视频已清空');
                // 刷新视频列表
                await refreshVideoList();
            } else {
                throw new Error(data.message || '清空视频失败');
            }
        } catch (error) {
            // 隐藏加载中弹窗
            hideLoadingModal();
            showToast('错误', error.message);
        }
    });
}

// 取消计划任务
async function cancelScheduledTask(taskId) {
    // 显示确认弹窗
    showConfirmActionModal('取消计划任务', '确定要取消此计划任务吗？此操作不可撤销。', async () => {
        try {
            // 显示加载中弹窗
            showLoadingModal('正在取消计划任务...', '请稍候');
            
            // 发送取消计划任务请求
            const response = await fetch(`${API_BASE_URL}/tasks/stop/${taskId}`, {
                method: 'GET',
            });
            
            const data = await response.json();
            
            // 隐藏加载中弹窗
            hideLoadingModal();
            
            if (data.status === 'success') {
                showToast('成功', data.message);
                refreshTaskStatus(); // 刷新任务列表
            } else {
                showToast('错误', data.message);
            }
        } catch (error) {
            // 隐藏加载中弹窗
            hideLoadingModal();
            showToast('错误', `取消计划任务失败: ${error.message}`);
        }
    });
}

// 加载中弹窗相关函数
let loadingModal = null;

// 初始化加载中弹窗
function loadingModalInit() {
    const loadingEl = document.getElementById('loadingModal');
    if (loadingEl) {
        loadingModal = new bootstrap.Modal(loadingEl, {
            backdrop: 'static',
            keyboard: false
        });
    }
}

// 显示加载中弹窗
function showLoadingModal(text, subtext) {
    if (!loadingModal) {
        console.warn('加载中弹窗未初始化');
        return;
    }
    
    // 设置加载文本
    const loadingText = document.getElementById('loadingText');
    if (loadingText && text) {
        loadingText.textContent = text;
    }
    
    // 设置加载子文本
    const loadingSubtext = document.getElementById('loadingSubtext');
    if (loadingSubtext && subtext) {
        loadingSubtext.textContent = subtext;
    }
    
    // 显示弹窗
    loadingModal.show();
}

// 隐藏加载中弹窗
function hideLoadingModal() {
    try {
        console.log('尝试关闭加载弹窗 - 彻底重写版');
        
        // 1. 直接使用jQuery关闭modal（如果jQuery可用）
        if (typeof $ !== 'undefined') {
            try {
                $('#loadingModal').modal('hide');
                console.log('使用jQuery关闭modal');
            } catch (error) {
                console.error('jQuery方法失败:', error);
            }
        }
        
        // 2. 使用Bootstrap实例方法
        if (loadingModal) {
            try {
                loadingModal.hide();
                console.log('Bootstrap实例方法尝试关闭');
            } catch (error) {
                console.error('Bootstrap实例方法失败:', error);
            }
        }
        
        // 3. 通过DOM操作强制清理
        try {
            const modalElement = document.getElementById('loadingModal');
            if (modalElement) {
                // 隐藏模态框
                modalElement.classList.remove('show');
                modalElement.style.display = 'none';
                modalElement.setAttribute('aria-hidden', 'true');
                modalElement.removeAttribute('aria-modal');
                modalElement.removeAttribute('role');
            }
            
            // 清除所有backdrop
            const backdrops = document.querySelectorAll('.modal-backdrop');
            backdrops.forEach(backdrop => {
                backdrop.remove();
            });
            
            // 恢复body样式
            document.body.classList.remove('modal-open');
            document.body.style.removeProperty('overflow');
            document.body.style.removeProperty('padding-right');
            
            console.log('直接DOM操作完成');
        } catch (domError) {
            console.error('DOM操作失败:', domError);
        }
        
        // 4. 最后的尝试 - 使用timeout延迟执行上述操作
        setTimeout(() => {
            try {
                const modalElement = document.getElementById('loadingModal');
                if (modalElement) {
                    modalElement.classList.remove('show');
                    modalElement.style.display = 'none';
                }
                
                const backdrops = document.querySelectorAll('.modal-backdrop');
                backdrops.forEach(backdrop => {
                    backdrop.remove();
                });
                
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
                
                console.log('延迟清理完成');
            } catch (error) {
                console.error('延迟清理失败:', error);
            }
        }, 300);
    } catch (error) {
        console.error('关闭加载弹窗过程中发生错误:', error);
    }
}

// 显示操作确认弹窗
let confirmActionModal = null;
let confirmActionCallback = null;

function showConfirmActionModal(title, message, callback) {
    // 如果弹窗元素不存在，则创建
    let modalElement = document.getElementById('confirmActionModal');
    
    if (!modalElement) {
        console.warn('确认操作弹窗未初始化');
        // 使用浏览器原生confirm作为后备方案
        if (confirm(message)) {
            callback();
        }
        return;
    }
    
    // 设置标题和消息
    document.getElementById('confirmActionModalLabel').textContent = title || '确认操作';
    document.getElementById('confirmActionMessage').textContent = message || '确定要执行此操作吗？';
    
    // 存储回调函数
    confirmActionCallback = callback;
    
    // 显示弹窗
    confirmActionModal.show();
}