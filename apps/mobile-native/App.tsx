// 应用入口：根据认证状态切换登录页与主 Tab
import React from 'react'
import { SafeAreaProvider } from 'react-native-safe-area-context'
import { NavigationContainer } from '@react-navigation/native'
import { StatusBar } from 'expo-status-bar'
import { useAuthStore } from './src/stores/authStore'
import { LoginScreen } from './src/screens/LoginScreen'
import { RootNavigator } from './src/navigation/RootNavigator'

export default function App() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated())
  if (!isAuthenticated) {
    return <LoginScreen />
  }
  return (
    <SafeAreaProvider>
      <NavigationContainer>
        <StatusBar style="auto" />
        <RootNavigator />
      </NavigationContainer>
    </SafeAreaProvider>
  )
}
