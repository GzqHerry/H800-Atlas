# H800 Atlas · 个人训练工作台

H800 Atlas 把个人训练记录、文件、权重和 Slurm 队列放在同一个网页里。团队共用一套服务，每位同事用自己的 H800 SSH 账号登录。服务部署在 H800-node11，日常使用只需要浏览器和能到达节点的网络。

## 同事怎么登录

1. 连上能访问 H800 的内网或 VPN，打开 [团队登录页](http://100.64.0.71:18771/login)。使用已信任集群证书的设备，也可以打开 [HTTPS 入口](https://h800-node1.int.mintibot.com:18770/)。
2. 输入**自己的 H800 SSH 用户名和密码**。Atlas 通过节点本机的 SSH 服务验证身份，再以该 Linux 用户运行请求。浏览器到服务器的 HTTP 连接会明文传输登录信息；需要加密时使用 HTTPS 或下面的 SSH 隧道。
3. 登录后，右上角会显示当前账号。「文件管理」「训练记录」「资源目录」按该账号读取；「集群看板」展示 Slurm 允许查看的全局作业，并高亮自己的任务。训练剩余时间仅对作业本人显示。
4. 使用完毕，点击右上角「退出」。会话空闲约 30 分钟或建立满 8 小时后，需要重新登录。

**内网地址打不开，但可以 SSH 到 H800 时**，向维护者取得完整的 `client/` 目录。Windows 双击「打开工作台.cmd」，macOS 或 Linux 在该目录执行 `sh start.sh`。输入自己的 SSH 用户名，按终端提示认证，保持终端打开，再访问 [本机隧道入口](http://127.0.0.1:18769/) 并登录。脚本使用 OpenSSH 连接 `36.103.247.214:60011`，将本机 `18769` 转发到节点回环端口 `18768`，并用随包的 `known_hosts` 校验主机身份。隧道支持 SSH 密钥，网页登录仍使用账号密码。详见[客户端使用说明](client/使用说明.txt)。

## 可以做什么

| 页面 | 常用操作 |
| --- | --- |
| 工作台 | 查看作业总数、运行中和排队中的作业、checkpoint 数量，以及各存储位置的余量。`/home` 使用当前用户的 30 GiB 配额计算。 |
| 集群训练用量 | 在工作台切换近 7 天或 30 天，按用户比较分配的 GPU 卡时、累计训练时长、训练作业次数和成功率。排行来自当前账号可见的 Slurm 记账记录，按作业名或提交脚本识别训练；使用通用名称的训练可能漏计。每个实际运行的作业 ID 计一次，跨时段作业仅累计窗口内的用量。成功率为成功结束数除以已结束数。 |
| 文件管理 | 浏览、搜索自己的 Home、workspace、data、scratch 和已存在的 BodyPipeline 个人目录，查看文件信息并复制路径。 |
| 训练记录 | 查看 Slurm 作业、失败和重启记录、Step 进度和 epoch 分界。运行中的详情约每 10 秒同步。点击 loss、学习率、梯度范数或每步耗时等指标可放大曲线、查看采样点并调节平滑强度。 |
| 资源目录 | 按机器人和存储位置筛选数据集；在 Checkpoints 子页按训练任务展开权重，查看已有的 step、epoch、loss 和 H800 / NAS 副本，复制路径。 |
| 集群看板 | 查看各节点的 GPU、CPU、用户、运行时长、申请时限和排队情况。运行中的任务显示已分配资源，排队任务显示申请资源；自己的训练有近期步速时，还会显示预计剩余时间。 |

曲线来自当前作业可读的日志。Atlas 可以读取 Slurm 输出、W&B 本地历史和 SwanLab 本地标量记录；具体能画哪些指标取决于训练实际记录了什么。当前 W&B 读取器使用用户的 `~/Enactive/.venv/bin/python` 和其中的 W&B SDK。训练完成预估会随迭代速度变化，GPU 的实际释放时间还取决于作业脚本。

## 维护者首次部署

以下命令适用于当前 H800 环境，使用部署账号 `gzqherry` 在 **H800-node11** 上执行。源码示例目录是 `/workspace/gzqherry/src/H800-Atlas`，运行时发布目录是 `/workspace/gzqherry/h800-atlas-team`。更换部署账号或发布目录时，需要同步修改发布脚本和两个 service 文件中的绝对路径。

### 1. 检查节点环境

- 当前部署使用 Linux、Python 3.10、OpenSSH、`sshpass`、`curl`、`flock`、GNU coreutils 和用户级 systemd。前端静态文件随仓库提交，发布脚本直接组装页面。
- 节点需要能运行 `squeue`、`sacct`、`sinfo`、`scontrol` 等 Slurm 命令。登录用户需要能通过密码 SSH 到节点的 `127.0.0.1`，并能读取发布目录。
- 部署账号需要能写入发布目录及 `/scratch/gzqherry`。常驻运行依赖用户级 systemd；当前节点已为部署账号启用 linger，可用 `loginctl show-user "$USER" -p Linger` 核对。

### 2. 拉取私有仓库并发布

先为部署账号配置该私有仓库的 GitHub 读取权限。下例使用已授权的 GitHub SSH 密钥。

```bash
mkdir -p /workspace/gzqherry/src
cd /workspace/gzqherry/src
git clone git@github.com:GzqHerry/H800-Atlas.git
cd H800-Atlas
sh publish.sh
```

发布脚本会校验 Python 源码、组装网页、创建版本目录并切换 `current`，然后启动登录网关和每 5 分钟一次的节点历史采样。成功时终端会输出 `H800 Atlas release <版本号>`。详见[发布脚本](publish.sh)。

### 3. 配置团队直连入口

基础部署会监听 `127.0.0.1:18768`，可立即配合 SSH 隧道使用。团队内网 HTTP 入口由部署账号的 `~/.config/h800-atlas/http.json` 控制。当前节点使用以下配置。首次配置时先运行 `mkdir -p ~/.config/h800-atlas`，再将下列 JSON 保存到该文件。

```json
{"address": "100.64.0.71", "port": 18771}
```

HTTPS 入口读取同目录下的 `https.json`，字段为 `address`、`port`、`hostname`、`cert` 和 `key`。后两项填写节点上证书链和私钥的绝对路径。客户端需要信任该证书，证书需覆盖访问域名。配置监听地址或证书后执行 `systemctl --user restart h800-atlas.service`，再把对应网址发给同事。

### 4. 验证服务

```bash
systemctl --user is-active h800-atlas.service h800-atlas-sample.timer
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18768/
curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18768/api/overview
```

两个单元应当都是 `active`。首页应返回 `200`；未登录时访问个人接口应返回 `401`。最后用自己的账号从团队登录页进入，核对右上角用户名、个人目录和集群节点列表。

### 可选：启用 NAS 权重归档

NAS 使用登录用户在 H800 上的 `~/.ssh/id_ed25519` 和 `/scratch/<用户名>/nas-archive-20261112/known_hosts` 做密钥认证和主机校验。需要先在 NAS 为该公钥授权、核对主机指纹，并准备可写的个人目标目录；传输依赖两端的 `rsync`。当前 `gzqherry` 使用 `/mnt/orion/data/public/gzq`，其他账号按 `/mnt/orion/data/public/users/<用户名>` 映射。

本地 checkpoint 索引以 `~/nas-archive-20261112/inventory.json` 中已登记的训练目录为起点，再检查其中的新 checkpoint。完成配置后，在「资源目录 → Checkpoints」选择权重并点击「上传到 NAS」。出现已校验副本后，才会提供「删除 H800 本地副本」按钮；删除操作会再次比较两端内容。实现见 [NAS 模块](nas.py)。

## 后续怎么更新

**维护者在开发机提交并推送改动后，还要到 H800 上执行发布。** 当前流程由 `publish.sh` 切换线上版本。只更新 README 时，推送到 GitHub 即可。

```bash
# 在 H800-node11 的源码目录执行
cd /workspace/gzqherry/src/H800-Atlas
git switch main
git pull --ff-only origin main
sh publish.sh
readlink -f /workspace/gzqherry/h800-atlas-team/current
```

已打开的可见标签页每 10 秒检查一次版本，从后台切回时也会检查。发现新版本后页面自动刷新，同事继续使用原网址即可。更新登录网关或重启服务会结束现有登录会话，需要重新登录。

若发布或登录异常，可用 `journalctl --user -u h800-atlas.service -n 80 --no-pager` 查看网关日志。节点历史缺失时，查看 `systemctl --user status h800-atlas-sample.service`。需要恢复旧版程序时，在源码目录检出已验证的提交并再次运行 `sh publish.sh`；下次正常更新前切回 `main`。
