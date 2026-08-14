//! WorkAMA 桌面端核心库。
//!
//! 模块组织：
//! - `policy`：本地 MCP 授权策略（端点校验、scope 白名单、授权匹配）
//! - `tray`：系统托盘（图标、菜单、点击切换窗口）
//! - `shortcut`：全局快捷键（Ctrl+Shift+W 唤起窗口）
//! - `updater`：自动更新（检查、下载、安装）
//! - `notification`：原生通知

pub mod notification;
pub mod policy;
pub mod shortcut;
pub mod tray;
pub mod updater;

use std::{collections::BTreeSet, sync::Mutex, time::Instant};

use serde::{Deserialize, Serialize};

#[derive(Default)]
pub struct BridgeState {
    authorizations: Mutex<Vec<StoredAuthorizationEntry>>,
}

pub struct StoredAuthorizationEntry {
    pub id: String,
    pub endpoint: String,
    pub scopes: BTreeSet<String>,
    pub expires_at: Instant,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalMcpAuthorizationRequest {
    pub endpoint: String,
    #[serde(default)]
    pub scopes: Vec<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalMcpStatus {
    pub enabled: bool,
    pub native_transport: bool,
    pub credential_persistence: bool,
    pub active_authorizations: usize,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalMcpAuthorization {
    pub authorization_id: String,
    pub endpoint: String,
    pub scopes: Vec<String>,
    pub expires_in_seconds: u64,
}

fn bridge_enabled() -> bool {
    cfg!(feature = "local-mcp-bridge")
}
#[cfg(not(test))]
mod commands {
    use super::*;
    use std::time::Duration;
    use tauri::State;
    use uuid::Uuid;

    #[tauri::command]
    pub fn local_mcp_status(state: State<'_, BridgeState>) -> LocalMcpStatus {
        let now = Instant::now();
        let active = state.authorizations.lock().map(|mut items| {
            items.retain(|item| item.expires_at > now);
            items.len()
        }).unwrap_or(0);
        LocalMcpStatus {
            enabled: bridge_enabled(),
            native_transport: false,
            credential_persistence: false,
            active_authorizations: active,
        }
    }

    #[tauri::command]
    pub fn authorize_local_mcp(
        request: LocalMcpAuthorizationRequest,
        state: State<'_, BridgeState>,
    ) -> Result<LocalMcpAuthorization, String> {
        if !bridge_enabled() {
            return Err("local MCP authorization bridge is disabled".to_string());
        }
        let endpoint = policy::validate_endpoint(&request.endpoint)?;
        let scopes = request.scopes.into_iter().collect::<BTreeSet<_>>();
        if scopes.is_empty() || scopes.iter().any(|s| !policy::ALLOWED_SCOPES.contains(&s.as_str())) {
            return Err("requested scopes are not allowed".to_string());
        }
        let authorization_id = format!("local-mcp-{}", Uuid::new_v4());
        let mut auths = state.authorizations.lock().map_err(|_| "bridge state unavailable".to_string())?;
        let now = Instant::now();
        auths.retain(|item| item.expires_at > now);
        auths.push(StoredAuthorizationEntry {
            id: authorization_id.clone(),
            endpoint: endpoint.clone(),
            scopes: scopes.clone(),
            expires_at: now + Duration::from_secs(300),
        });
        Ok(LocalMcpAuthorization {
            authorization_id,
            endpoint,
            scopes: scopes.into_iter().collect(),
            expires_in_seconds: 300,
        })
    }
    #[tauri::command]
    pub fn check_local_mcp_authorization(
        authorization_id: String,
        endpoint: String,
        scope: String,
        state: State<'_, BridgeState>,
    ) -> Result<bool, String> {
        if !bridge_enabled() {
            return Ok(false);
        }
        let endpoint = policy::validate_endpoint(&endpoint)?;
        let now = Instant::now();
        let mut auths = state.authorizations.lock().map_err(|_| "bridge state unavailable".to_string())?;
        auths.retain(|item| item.expires_at > now);
        Ok(auths.iter().any(|item| {
            policy::authorization_matches(
                &policy::StoredAuthorization {
                    id: item.id.clone(),
                    endpoint: item.endpoint.clone(),
                    scopes: item.scopes.clone(),
                    expires_at: item.expires_at,
                },
                &authorization_id,
                &endpoint,
                &scope,
                now,
            )
        }))
    }

    #[tauri::command]
    pub fn revoke_local_mcp(authorization_id: String, state: State<'_, BridgeState>) -> Result<bool, String> {
        if authorization_id.trim().is_empty() {
            return Err("authorization id is required".to_string());
        }
        let mut auths = state.authorizations.lock().map_err(|_| "bridge state unavailable".to_string())?;
        let before = auths.len();
        auths.retain(|item| item.id != authorization_id);
        Ok(auths.len() != before)
    }

    /// 检查应用更新（不自动安装），返回更新状态供前端展示。
    #[tauri::command]
    pub async fn check_for_updates(app: tauri::AppHandle) -> Result<updater::UpdateStatus, String> {
        updater::check_for_updates(&app).await
    }

    /// 下载并安装可用更新，安装后通常需要重启应用。
    #[tauri::command]
    pub async fn install_update(app: tauri::AppHandle) -> Result<bool, String> {
        updater::download_and_install(&app).await
    }

    /// 显示系统通知。
    #[tauri::command]
    pub fn show_notification(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
        notification::show(&app, &title, &body)
    }
}
/// 应用入口：装配 Tauri 插件、注册命令、初始化托盘/快捷键/更新。
///
/// 插件装配顺序：
/// 1. `tauri_plugin_global_shortcut` — 全局快捷键
/// 2. `tauri_plugin_notification` — 原生通知
/// 3. `tauri_plugin_updater` — 自动更新
///
/// setup 回调中依次初始化托盘、快捷键、更新检查。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
#[cfg(not(test))]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(BridgeState::default())
        .invoke_handler(tauri::generate_handler![
            commands::local_mcp_status,
            commands::authorize_local_mcp,
            commands::check_local_mcp_authorization,
            commands::revoke_local_mcp,
            commands::check_for_updates,
            commands::install_update,
            commands::show_notification
        ])
        .setup(|app| {
            // 桌面端初始化托盘、快捷键、自动更新
            #[cfg(desktop)]
            {
                tray::setup(app.handle())?;
                shortcut::setup(app.handle())?;
                updater::setup(app.handle())?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running WorkAMA desktop");
}