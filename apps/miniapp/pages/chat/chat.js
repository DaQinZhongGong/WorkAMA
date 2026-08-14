const app = getApp()

Page({
  data: {
    messages: [],
    inputValue: '',
    socketOpen: false
  },

  socketTask: null,

  onLoad() {
    this.connectWebSocket()
  },

  onUnload() {
    if (this.socketTask) {
      this.socketTask.close()
    }
  },

  connectWebSocket() {
    const token = app.getToken()
    const wsUrl = `ws://localhost:20201?token=${encodeURIComponent(token || '')}`

    this.socketTask = wx.connectSocket({
      url: wsUrl
    })

    this.socketTask.onOpen(() => {
      this.setData({ socketOpen: true })
      console.log('WebSocket connected')
    })

    this.socketTask.onMessage((res) => {
      try {
        const msg = JSON.parse(res.data)
        this.appendMessage(msg)
      } catch {
        this.appendMessage({ role: 'assistant', content: res.data })
      }
    })

    this.socketTask.onClose(() => {
      this.setData({ socketOpen: false })
      console.log('WebSocket closed')
    })

    this.socketTask.onError((err) => {
      console.error('WebSocket error', err)
    })
  },

  onInput(e) {
    this.setData({ inputValue: e.detail.value })
  },

  sendMessage() {
    const content = this.data.inputValue.trim()
    if (!content) return

    const msg = { id: Date.now().toString(), role: 'user', content }
    this.appendMessage(msg)
    this.setData({ inputValue: '' })

    if (this.data.socketOpen && this.socketTask) {
      this.socketTask.send({
        data: JSON.stringify({ type: 'chat', content })
      })
    }
  },

  appendMessage(msg) {
    const messages = [...this.data.messages, msg]
    this.setData({ messages })
  }
})
