const app = getApp()

Page({
  data: {
    account: '',
    password: '',
    loading: false,
    errorMsg: ''
  },

  onAccountInput(e) {
    this.setData({ account: e.detail.value })
  },

  onPasswordInput(e) {
    this.setData({ password: e.detail.value })
  },

  // 主登录路径：wx.login 拿 code，调用后端 /api/v1/wechat/miniapp/login 换 token
  async onWxLogin() {
    if (this.data.loading) return
    this.setData({ loading: true, errorMsg: '' })
    try {
      const { code } = await this._wxLogin()
      if (!code) {
        this.setData({ errorMsg: '微信登录凭证获取失败' })
        return
      }
      const data = await app.request('/api/v1/wechat/miniapp/login', 'POST', {
        js_code: code
      })
      this._applyLoginResult(data)
    } catch (err) {
      this.setData({ errorMsg: err.message || '微信登录失败，可改用账号密码登录' })
    } finally {
      this.setData({ loading: false })
    }
  },

  // 兜底登录路径：邮箱密码登录（保留原有行为）
  async onLogin() {
    const { account, password } = this.data
    if (!account || !password) {
      this.setData({ errorMsg: '请输入账号和密码' })
      return
    }

    this.setData({ loading: true, errorMsg: '' })

    try {
      const data = await app.request('/api/v1/auth/login', 'POST', {
        username: account,
        password
      })
      this._applyLoginResult(data)
    } catch (err) {
      this.setData({ errorMsg: err.message || '登录失败' })
    } finally {
      this.setData({ loading: false })
    }
  },

  // 统一处理登录结果：持久化 token（access_token / session_token / refresh_token）并跳转
  _applyLoginResult(data) {
    const accessToken = data.access_token
    if (accessToken) {
      app.setToken(accessToken)
      // 小程序专用 token（session/refresh）单独存储，供订阅消息等场景使用
      if (data.session_token) wx.setStorageSync('session_token', data.session_token)
      if (data.refresh_token) wx.setStorageSync('refresh_token', data.refresh_token)
      app.globalData.userInfo = data.user || null
      wx.switchTab({ url: '/pages/chat/chat' })
    } else {
      this.setData({ errorMsg: '登录失败，请重试' })
    }
  },

  // Promise 化 wx.login
  _wxLogin() {
    return new Promise((resolve, reject) => {
      wx.login({
        success: resolve,
        fail: reject
      })
    })
  }
})
