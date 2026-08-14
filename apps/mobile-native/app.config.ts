// 动态 Expo 配置：注入环境变量（API 地址等）
interface ExpoConfigLike {
  [key: string]: unknown
}

// 根据环境变量动态生成 Expo 配置，暴露 extra.apiUrl 供运行时读取
export default ({ config }: { config: ExpoConfigLike }): ExpoConfigLike => {
  return {
    ...config,
    name: 'WorkAMA',
    slug: 'workama',
    scheme: 'workama',
    extra: {
      apiUrl: process.env.EXPO_PUBLIC_API_URL || 'http://localhost:20200',
    },
  }
}
