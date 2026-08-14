// 登录页：邮箱密码登录 + 微信登录占位
import React, { useState } from 'react'
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from 'react-native'
import { colors } from '../theme/colors'
import * as api from '../services/api'
import { useAuthStore } from '../stores/authStore'

export function LoginScreen() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const loginStore = useAuthStore((s) => s.login)

  // 邮箱密码登录：调用 API，成功写入 authStore
  async function handleLogin() {
    setError('')
    setLoading(true)
    try {
      const result = await api.login(email, password)
      loginStore(result.access_token, result.user)
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  // 微信登录占位：实际接入需调用原生 SDK
  function handleWeChatLogin() {
    Alert.alert('微信登录', '微信登录待接入')
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>WorkAMA</Text>
        <Text style={styles.subtitle}>登录你的工作空间</Text>
      </View>
      <View style={styles.form}>
        <Text style={styles.label}>邮箱</Text>
        <TextInput
          testID="login-email"
          style={styles.input}
          value={email}
          onChangeText={(v) => {
            setEmail(v)
            setError('')
          }}
          placeholder="请输入邮箱"
          keyboardType="email-address"
          autoCapitalize="none"
        />
        <Text style={styles.label}>密码</Text>
        <TextInput
          testID="login-password"
          style={styles.input}
          value={password}
          onChangeText={(v) => {
            setPassword(v)
            setError('')
          }}
          placeholder="请输入密码"
          secureTextEntry
        />
        {error ? (
          <Text style={styles.error} testID="login-error">
            {error}
          </Text>
        ) : null}
        <Pressable
          testID="login-submit"
          style={[styles.button, loading && styles.buttonDisabled]}
          onPress={handleLogin}
          disabled={loading}
        >
          <Text style={styles.buttonText}>{loading ? '登录中...' : '登录'}</Text>
        </Pressable>
        <Pressable testID="login-wechat" style={styles.wechatButton} onPress={handleWeChatLogin}>
          <Text style={styles.wechatText}>微信登录</Text>
        </Pressable>
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
    padding: 24,
    justifyContent: 'center',
  },
  header: {
    alignItems: 'center',
    marginBottom: 32,
  },
  title: {
    fontSize: 28,
    fontWeight: '700',
    color: colors.primary,
  },
  subtitle: {
    marginTop: 8,
    fontSize: 14,
    color: colors.textSecondary,
  },
  form: {
    width: '100%',
  },
  label: {
    fontSize: 13,
    color: colors.textSecondary,
    marginBottom: 6,
    marginTop: 12,
  },
  input: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 12,
    fontSize: 15,
    color: colors.text,
  },
  error: {
    color: colors.error,
    marginTop: 8,
    fontSize: 13,
  },
  button: {
    backgroundColor: colors.primary,
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: 'center',
    marginTop: 20,
  },
  buttonDisabled: {
    opacity: 0.6,
  },
  buttonText: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '600',
  },
  wechatButton: {
    backgroundColor: colors.wechat,
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: 'center',
    marginTop: 12,
  },
  wechatText: {
    color: '#FFFFFF',
    fontSize: 16,
    fontWeight: '600',
  },
})
