// Jest 配置：基于 jest-expo，针对 pnpm 嵌套 .pnpm 目录调整 transformIgnorePatterns
// pnpm 把真实包放在 node_modules/.pnpm/<pkg>/node_modules/<pkg>，
// 路径中存在两层 node_modules，需把 .pnpm/<pkg>/node_modules/ 前缀设为可选，
// 使否定前瞻在两层位置都能命中，确保 RN/Expo 相关包被 babel 转译（剥离 Flow/TS 语法）。
module.exports = {
  preset: 'jest-expo',
  testPathIgnorePatterns: ['/node_modules/', '/android/', '/ios/'],
  transformIgnorePatterns: [
    'node_modules/(?!(?:\\.pnpm/[^/]+/node_modules/)?(?:(jest-)?react-native|@react-native(-community)?|expo(nent)?|@expo(nent)?|@react-navigation|@unimodules|unimodules|native-base|@sentry|sentry-expo|@babel/runtime|@callstack))',
  ],
}
