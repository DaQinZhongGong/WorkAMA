# sandbox-code 镜像
# 基于 sandbox-agentd 镜像，额外安装多语言工具链（Python 常用包 / Node.js 20.x LTS / Git / 编译工具）。
# 用于支持通用代码执行类沙箱任务（数据分析、脚本编写、Node.js 工具链、本地编译等）。
# 对齐《520-Agent引擎与运行时设计》§4.2 三镜像设计：
#   - sandbox-base（Ubuntu + Python/Node/Git/常用 CLI）
#   - sandbox-browser（+Chromium/Playwright）
#   - sandbox-code（+多语言工具链）—— 本镜像
# 网络策略：与 sandbox-base 一致，默认 network_mode="none"（见 netpolicy.build_egress_rules）。
FROM workama-sandbox-agentd:local

# 切换到 root 安装多语言工具链，完成后切回 sandbox 用户
USER root

# 安装系统级依赖与 Node.js 20.x LTS
# - ca-certificates / curl：NodeSource 安装脚本依赖
# - git / jq / make：常用 CLI 工具
# - gcc / g++：本地编译 C/C++ 及 Python native 扩展所需
# - nodejs：通过 NodeSource 安装 20.x LTS（含 npm）
# --no-install-recommends 避免安装不必要的依赖，减小镜像体积
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        gcc \
        g++ && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 常用包（基础镜像已含 Python 3.12 + pip）
# 涵盖 HTTP 客户端、科学计算、符号计算等代码沙箱常用领域
RUN pip install --no-cache-dir \
        requests \
        httpx \
        numpy \
        pandas \
        matplotlib \
        scipy \
        sympy

# 验证多语言工具链可用
RUN python --version && \
    node --version && \
    npm --version && \
    git --version

# 切回 sandbox 用户，对齐 sandbox-agentd 的运行身份
USER sandbox

WORKDIR /workspace

# ENTRYPOINT/CMD 与 sandbox-agentd 一致，由 fleet 启动时通过 entrypoint/command 覆盖
ENTRYPOINT ["/usr/local/bin/sandbox-agentd"]
CMD ["serve"]
