# sandbox-browser 镜像
# 基于 sandbox-agentd 镜像，额外安装 Chromium 浏览器。
# 用于支持 BrowserOp RPC（导航/点击/输入/截图/eval/wait_for）。
# 对齐《520》§4.2 三镜像设计（sandbox-base/sandbox-browser/sandbox-code）。
FROM workama-sandbox-agentd:local

# 切换到 root 安装 Chromium，然后切回 sandbox 用户
USER root

# 安装 Chromium（Debian slim 版本需要先更新包索引）
# --no-install-recommends 避免安装不必要的依赖，减小镜像体积
RUN apt-get update && \
    apt-get install -y --no-install-recommends chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    # 验证 chromium 可执行
    which chromium && \
    chromium --version

# 创建 Chrome 用户数据目录（/workspace 下的子目录，由 sandbox-agentd 管理）
RUN mkdir -p /workspace/.chrome && chown 10001:10001 /workspace/.chrome

# 切回 sandbox 用户
USER sandbox

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/sandbox-agentd"]
CMD ["serve"]
