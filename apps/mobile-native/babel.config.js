// Expo Babel 配置：jest-expo 依赖此配置转换 TS/JSX
module.exports = function (api) {
  api.cache(true)
  return {
    presets: ['babel-preset-expo'],
  }
}
