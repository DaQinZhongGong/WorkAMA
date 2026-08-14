// 聊天页：消息列表 + 输入框 + 发送按钮，调用 chat API
import React, { useState } from 'react'
import { FlatList, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, TextInput, View } from 'react-native'
import { colors } from '../theme/colors'
import { MessageBubble } from '../components/MessageBubble'
import * as api from '../services/api'
import { useAuthStore } from '../stores/authStore'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
}

interface Props {
  // 当前对话的 Agent ID，默认 default
  agentId?: string
}

export function ChatScreen({ agentId = 'default' }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const token = useAuthStore((s) => s.token)

  // 发送消息：立即追加用户消息，调用 API 后追加助手回复
  async function handleSend() {
    const content = input.trim()
    if (!content || !token || sending) return
    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: 'user', content }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setSending(true)
    try {
      const result = await api.chat(token, agentId, content)
      const reply = result.reply ?? result.message ?? result.content ?? ''
      setMessages((prev) => [...prev, { id: `a-${Date.now()}`, role: 'assistant', content: reply }])
    } catch {
      // 错误静默，保留用户消息
    } finally {
      setSending(false)
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <FlatList
        testID="chat-message-list"
        style={styles.list}
        data={messages}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => <MessageBubble role={item.role} content={item.content} />}
        ListEmptyComponent={
          <Text style={styles.empty} testID="chat-empty">
            暂无消息，开始对话吧
          </Text>
        }
      />
      <View style={styles.composer}>
        <TextInput
          testID="chat-input"
          style={styles.input}
          value={input}
          onChangeText={setInput}
          placeholder="输入消息..."
        />
        <Pressable
          testID="chat-send"
          style={[styles.sendButton, sending && styles.sendDisabled]}
          onPress={handleSend}
          disabled={sending}
        >
          <Text style={styles.sendText}>发送</Text>
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  list: {
    flex: 1,
    padding: 12,
  },
  empty: {
    textAlign: 'center',
    color: colors.textSecondary,
    marginTop: 32,
  },
  composer: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 12,
    borderTopWidth: 1,
    borderColor: colors.border,
  },
  input: {
    flex: 1,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    color: colors.text,
  },
  sendButton: {
    backgroundColor: colors.primary,
    paddingHorizontal: 18,
    paddingVertical: 12,
    borderRadius: 10,
    marginLeft: 8,
  },
  sendDisabled: {
    opacity: 0.6,
  },
  sendText: {
    color: '#FFFFFF',
    fontWeight: '600',
  },
})
