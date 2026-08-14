App({
  globalData: {
    apiBase: 'http://localhost:20200',
    accessToken: null,
    userInfo: null
  },

  onLaunch() {
    console.log('WorkAMA MiniApp launched')
  },

  onShow() {
    console.log('WorkAMA MiniApp shown')
  },

  onHide() {
    console.log('WorkAMA MiniApp hidden')
  },

  setToken(token) {
    this.globalData.accessToken = token
    wx.setStorageSync('access_token', token)
  },

  getToken() {
    if (!this.globalData.accessToken) {
      this.globalData.accessToken = wx.getStorageSync('access_token')
    }
    return this.globalData.accessToken
  },

  clearToken() {
    this.globalData.accessToken = null
    wx.removeStorageSync('access_token')
  },

  request(path, method = 'GET', data = {}) {
    const token = this.getToken()
    return new Promise((resolve, reject) => {
      wx.request({
        url: `${this.globalData.apiBase}${path}`,
        method,
        data,
        header: {
          'content-type': 'application/json',
          ...(token ? { authorization: `Bearer ${token}` } : {})
        },
        success: (res) => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(res.data)
          } else {
            reject(new Error(res.data?.detail || `Request failed (${res.statusCode})`))
          }
        },
        fail: (err) => reject(new Error(err.errMsg || 'Network error'))
      })
    })
  }
})
