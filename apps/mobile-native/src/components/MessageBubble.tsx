// 消息气泡：根据角色区分左右对齐与配色
import React from 'react'
import { StyleSheet, Text, View } from 'react-native'
import { colors } from '../theme/colors'

interface Props {
  role: 'user' | 'assistant'
  content: string
}

export function MessageBubble({ role, content }: Props) {
  const isUser = role === 'user'
  return (
    <View style={[styles.bubble, isUser ? styles.user : styles.assistant]} testID={`bubble-${role}`}>
      <Text style={styles.text}>{content}</Text>
    </View>
  )
}

const styles = StyleSheet.create({
  bubble: {
    marginVertical: 4,
    padding: 10,
    borderRadius: 12,
    maxWidth: '80%',
  },
  user: {
    alignSelf: 'flex-end',
    backgroundColor: colors.primary,
  },
  assistant: {
    alignSelf: 'flex-start',
    backgroundColor: colors.surface,
  },
  text: {
    color: colors.text,
    fontSize: 15,
  },
})
