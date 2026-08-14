// 通用加载组件
import React from 'react'
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native'
import { colors } from '../theme/colors'

interface Props {
  label?: string
}

export function LoadingSpinner({ label = '加载中...' }: Props) {
  return (
    <View style={styles.container} testID="loading-spinner">
      <ActivityIndicator color={colors.primary} />
      {label ? <Text style={styles.label}>{label}</Text> : null}
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    padding: 16,
    alignItems: 'center',
    justifyContent: 'center',
  },
  label: {
    marginTop: 8,
    color: colors.textSecondary,
    fontSize: 14,
  },
})
