// Agent 列表页：FlatList + 下拉刷新
import React, { useCallback, useEffect, useState } from 'react'
import { FlatList, Pressable, StyleSheet, Text, View } from 'react-native'
import { colors } from '../theme/colors'
import { LoadingSpinner } from '../components/LoadingSpinner'
import * as api from '../services/api'
import type { Agent } from '../services/api'
import { useAuthStore } from '../stores/authStore'

export function AgentsScreen() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const token = useAuthStore((s) => s.token)

  // 加载 Agent 列表
  const loadAgents = useCallback(async () => {
    if (!token) {
      setLoading(false)
      return
    }
    try {
      const list = await api.listAgents(token)
      setAgents(list)
    } catch {
      // 忽略错误，保留空列表
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [token])

  useEffect(() => {
    void loadAgents()
  }, [loadAgents])

  // 下拉刷新
  function handleRefresh() {
    setRefreshing(true)
    void loadAgents()
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>Agents</Text>
        <Pressable testID="agents-refresh" onPress={handleRefresh}>
          <Text style={styles.refreshText}>刷新</Text>
        </Pressable>
      </View>
      {loading ? (
        <LoadingSpinner />
      ) : agents.length === 0 ? (
        <Text style={styles.empty} testID="agents-empty">
          暂无可用 Agent
        </Text>
      ) : (
        <FlatList
          testID="agents-list"
          data={agents}
          keyExtractor={(item) => item.id}
          renderItem={({ item }) => (
            <View style={styles.card} testID={`agent-item-${item.id}`}>
              <Text style={styles.cardTitle}>{item.name}</Text>
              {item.description ? <Text style={styles.cardDesc}>{item.description}</Text> : null}
            </View>
          )}
          refreshing={refreshing}
          onRefresh={handleRefresh}
        />
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
    padding: 12,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 12,
  },
  title: {
    fontSize: 22,
    fontWeight: '700',
    color: colors.text,
  },
  refreshText: {
    color: colors.primary,
    fontSize: 14,
  },
  empty: {
    textAlign: 'center',
    color: colors.textSecondary,
    marginTop: 32,
  },
  card: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
  },
  cardTitle: {
    fontSize: 16,
    fontWeight: '600',
    color: colors.text,
  },
  cardDesc: {
    marginTop: 4,
    fontSize: 13,
    color: colors.textSecondary,
  },
})
