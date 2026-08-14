const app = getApp()

Page({
  data: {
    userInfo: {}
  },

  onShow() {
    const userInfo = app.globalData.userInfo || {}
    this.setData({
      userInfo,
      userInitial: (userInfo.name || '?')[0]
    })
  },

  goToSettings() {
    wx.showToast({ title: '功能开发中', icon: 'none' })
  },

  onLogout() {
    wx.showModal({
      title: '确认退出',
      content: '确定要退出登录吗？',
      success: (res) => {
        if (res.confirm) {
          app.clearToken()
          app.globalData.userInfo = null
          wx.reLaunch({ url: '/pages/index/index' })
        }
      }
    })
  }
})
