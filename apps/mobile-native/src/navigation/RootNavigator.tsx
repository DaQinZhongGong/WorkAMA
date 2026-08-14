// 根导航：底部 Tab（Chat / Agents / Me）
import React from 'react'
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs'
import { Ionicons } from '@expo/vector-icons'
import { ChatScreen } from '../screens/ChatScreen'
import { AgentsScreen } from '../screens/AgentsScreen'
import { ProfileScreen } from '../screens/ProfileScreen'

export type RootTabParamList = {
  Chat: undefined
  Agents: undefined
  Me: undefined
}

const Tab = createBottomTabNavigator<RootTabParamList>()

export function RootNavigator() {
  return (
    <Tab.Navigator
      screenOptions={{
        tabBarActiveTintColor: '#4F46E5',
        headerShown: true,
      }}
    >
      <Tab.Screen
        name="Chat"
        component={ChatScreen}
        options={{
          title: '聊天',
          tabBarIcon: ({ color, size }) => <Ionicons name="chatbubble-outline" size={size} color={color} />,
        }}
      />
      <Tab.Screen
        name="Agents"
        component={AgentsScreen}
        options={{
          title: 'Agents',
          tabBarIcon: ({ color, size }) => <Ionicons name="bot-outline" size={size} color={color} />,
        }}
      />
      <Tab.Screen
        name="Me"
        component={ProfileScreen}
        options={{
          title: '我的',
          tabBarIcon: ({ color, size }) => <Ionicons name="person-outline" size={size} color={color} />,
        }}
      />
    </Tab.Navigator>
  )
}
