// 个人中心：展示用户信息 + 退出登录
import React from 'react'
import { Pressable, StyleSheet, Text, View } from 'react-native'
import { colors } from '../theme/colors'
import { useAuthStore } from '../stores/authStore'

export function ProfileScreen() {
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  return (
    <View style={styles.container}>
      <Text style={styles.title}>个人中心</Text>
      <View style={styles.profile}>
        <View style={styles.avatar}>
          <Text style={styles.avatarText}>
            {(user?.display_name || user?.email || 'W').slice(0, 1).toUpperCase()}
          </Text>
        </View>
        <View>
          <Text style={styles.name}>{user?.display_name ?? user?.email ?? '未登录'}</Text>
          <Text style={styles.email}>{user?.email ?? ''}</Text>
        </View>
      </View>
      <Pressable testID="profile-logout" style={styles.logoutButton} onPress={logout}>
        <Text style={styles.logoutText}>退出登录</Text>
      </Pressable>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
    padding: 24,
  },
  title: {
    fontSize: 22,
    fontWeight: '700',
    color: colors.text,
    marginTop: 12,
  },
  profile: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: 24,
    padding: 16,
    backgroundColor: colors.surface,
    borderRadius: 12,
  },
  avatar: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: colors.primary,
    alignItems: 'center',
    justifyContent: 'center',
    marginRight: 12,
  },
  avatarText: {
    color: '#FFFFFF',
    fontSize: 20,
    fontWeight: '700',
  },
  name: {
    fontSize: 16,
    fontWeight: '600',
    color: colors.text,
  },
  email: {
    fontSize: 13,
    color: colors.textSecondary,
    marginTop: 2,
  },
  logoutButton: {
    marginTop: 32,
    paddingVertical: 14,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: colors.error,
    alignItems: 'center',
  },
  logoutText: {
    color: colors.error,
    fontSize: 15,
    fontWeight: '600',
  },
})
